"""ChatSession — long-lived chat loop driving the skill_router stdlib skill."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reyn.agent import Agent
from reyn.budget.budget import (
    BudgetTracker,
    format_budget_full,
    format_cost_line,
    format_refusal_message,
    format_warn_message,
)
from reyn.chat.error_format import classify_router_error
from reyn.chat.outbox import OutboxMessage
from reyn.chat.services import (
    AutoResumeHandler,
    BudgetGateway,
    ChainManager,
    CompactionController,
    InterventionHandler,
    InterventionRegistry,
    MemoryService,
    PlanRunner,
    RouterHostAdapter,
    SnapshotJournal,
)
from reyn.chat.services.a2a_handler import A2AHandler
from reyn.chat.services.chain_manager import _PendingChain
from reyn.chat.services.skill_runner import SkillRunner
from reyn.compiler import load_dsl_skill
from reyn.compiler.parser import _split_frontmatter
from reyn.config import (  # noqa: F401
    ActionRetrievalConfig,
    EmbeddingConfig,
    EventsConfig,
    OnLimitConfig,
    SafetyConfig,
    SandboxConfig,
)
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.event_store import EventStore
from reyn.events.events import EventLog
from reyn.events.state_log import StateLog
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.safety.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root
from reyn.skill.skill_registry import SkillRegistry
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    InterventionChoice,
    UserIntervention,
)

ROUTER_SKILL_NAME = "skill_router"

# Localized user-facing messages for the router retry-exhausted fallback (F8).
# Keys are BCP-47-style language codes matching config `output_language`.
# Unsupported codes fall back to "en".
_ROUTER_RETRY_EXHAUSTED_MSG: dict[str, str] = {
    "ja": (
        "このターン内で処理を完結できませんでした (router 予算使い切り)。"
        " 別の言い回しで試すか、リクエストを分割してみてください。"
    ),
    "en": (
        "I couldn't find a way to handle that within this turn's routing budget."
        " Please try rephrasing or breaking the request into smaller pieces."
    ),
}


def _no_reply_marker(agent_name: str, reason: str) -> str:
    """Generate a structured upstream message when this agent's router
    couldn't produce a real reply for an inbound agent_request (F6/F7).

    Sending an empty string is ambiguous — the upstream LLM cannot
    distinguish "empty success" from "failure" and tends to interpret
    silence as in-progress, re-delegating in a tight loop until the
    router cap fires (= F7 cascade). A clear text marker tells the
    upstream LLM exactly what happened so it can produce a coherent
    user-facing reply instead of retrying.

    The marker is intentionally English + structural — the receiving
    agent's LLM is supposed to interpret it and emit a user-facing reply
    in the user's `output_language`, not forward it verbatim.
    """
    return f"[{agent_name}: could not produce a reply — {reason}]"


# B2-H2 fix: detect and parse the structured peer-failure marker deterministically
# so the OS can surface the failure to the user without consulting the LLM (which
# tends to silently absorb the marker as a polite conversational reply).

_NO_REPLY_MARKER_RE = re.compile(
    r"^\s*\[([^:]+):\s*could not produce a reply\s*[—\-]\s*(.+?)\s*\]\s*$",
    re.DOTALL,
)


def _is_no_reply_marker(text: str) -> bool:
    """Detect whether `text` is a `_no_reply_marker(...)`-formatted
    failure signal from a peer agent (B2-H2 fix).

    The format produced by `_no_reply_marker` is
    `[<agent_name>: could not produce a reply — <reason>]`. We detect
    by structural signature (leading `[`, contains the canonical
    "could not produce a reply" substring) rather than parsing the
    full string — minor format drift in `<reason>` should still match.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith("[") and "could not produce a reply" in stripped


def _parse_no_reply_marker(text: str) -> tuple[str, str] | None:
    """Parse `_no_reply_marker(...)` text into (peer, reason).

    Returns None if the text does not match the expected format.
    """
    m = _NO_REPLY_MARKER_RE.match(text or "")
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


# Localized user-facing message when a peer agent's reply signals failure (B2-H2).
# "en" is the global-safe default (no regional fallback to "ja" per the Q2
# i18n principle). Placeholders: {peer} = peer agent name, {reason} = failure reason.
_PEER_REPLY_FAILED_MSG: dict[str, str] = {
    "ja": (
        "エージェント '{peer}' から処理結果が得られませんでした"
        " (理由: {reason})。"
    ),
    "en": (
        "Could not get a result from agent '{peer}' "
        "(reason: {reason})."
    ),
}

# Localized user-facing message when an invoke_skill tool call fails (G10 / B2-M2).
# Deterministic i18n replaces LLM-generated fallback on the tool_failed path so
# output_language is always honoured regardless of LLM default behaviour.
# "en" is the global-safe default. Placeholders: {tool_name}, {error}.
_TOOL_FAILED_FALLBACK_MSG: dict[str, str] = {
    "ja": (
        "ツール呼び出しに失敗しました ({tool_name}: {error})。"
        " 別の方法を試すか、リクエストを言い換えてください。"
    ),
    "en": (
        "Tool call failed ({tool_name}: {error})."
        " Please try a different approach or rephrase the request."
    ),
}


class RouterCapExceeded(Exception):
    """Raised when a user turn (or top-level agent_request) drives more
    skill_router invocations than the configured cap. Caught by handlers,
    which surface a structured fallback reply to the user / requester.

    FP-0004: ``hint_config_key`` is the user-facing config knob to raise
    when an operator decides the cap is too tight for their workload.
    """

    hint_config_key: str = "safety.loop.max_router_calls_per_turn"

    def __init__(self, count: int, cap: int, last_reason: str = "") -> None:
        super().__init__(
            f"Router exhausted retry budget ({count}/{cap}) for this turn. "
            f"→ Raise {RouterCapExceeded.hint_config_key} to allow more "
            f"router invocations per turn (0 = unlimited)."
        )
        self.count = count
        self.cap = cap
        self.last_reason = last_reason


class AgentRequestBus:
    """``RequestBus`` adapter that subscribes to a ChatSession (= Agent).

    issue #254 Phase 3: OS-layer callers (= ``handle_limit_exceeded``,
    permission gates, ``ask_user`` op) hold a ``RequestBus``-typed
    reference; this adapter forwards ``request(iv)`` to the Agent's
    ``handle_intervention(iv)`` so the Agent owns the routing decision.

    Phase 3 ships behaviour parity (= ``handle_intervention`` just
    forwards to ``_dispatch_intervention``); Phase 4 will add
    ``self_answer`` / ``parent_delegate`` branches on the Agent side
    without changing this adapter's surface.

    The adapter satisfies the ``RequestBus`` runtime_checkable Protocol
    so OS code typed against ``bus: RequestBus`` (or the legacy
    ``InterventionBus`` alias) accepts it without further wiring.
    """

    def __init__(self, session: "ChatSession") -> None:
        self._session = session

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``RequestBus.request`` — delegate to the Agent's intervention handler."""
        return await self._session.handle_intervention(iv)


class ChatInterventionBus:
    """``UserChannel`` implementation that routes through ChatSession's
    outbox/inbox to the attached TUI listener.

    One instance per skill spawn — captures `run_id` and a default `skill_name`
    so the chat session can drop pending interventions when the spawn is
    cancelled. Interventions emitted by ops carry their own `skill_name` from
    `OpContext`; this bus only fills in `run_id` (which the OS layer doesn't
    have, since chat tracks runs separately from `Agent.run_id`).

    Phase 2 (issue #254): the canonical method is ``deliver`` (= the
    Agent↔User contract).  ``request`` is retained as an alias so
    callers typed against ``InterventionBus`` / ``RequestBus`` continue
    to work unchanged.  Phase 3 will route OS-level requests through the
    Agent layer, which will then call ``deliver`` on this channel — at
    that point ``request`` becomes unused at top-level (= a candidate
    for Phase 5 removal).
    """

    def __init__(self, session: "ChatSession", run_id: str | None, skill_name: str | None) -> None:
        self._session = session
        self._run_id = run_id
        self._skill_name = skill_name

    async def deliver(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``UserChannel.deliver`` — route the prompt to ChatSession's
        outbox/inbox so the attached TUI surfaces it to the user.
        """
        if iv.run_id is None:
            iv.run_id = self._run_id
        if not iv.skill_name:
            iv.skill_name = self._skill_name
        # PR-intervention-link L6: short-circuit if a previous (crashed-then-
        # restored) run's intervention was already answered post-restart.
        # The L5 watcher buffered the answer keyed by run_id; the resuming
        # skill's first ask_user picks it up here without dispatching a
        # duplicate prompt.
        if iv.run_id is not None:
            buffered = self._session._consume_buffered_intervention_answer(iv.run_id)
            if buffered is not None:
                return buffered
        return await self._session._dispatch_intervention(iv)

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``RequestBus.request`` — Phase 2 backwards-compat alias.

        Delegates to ``deliver``; preserved so existing call sites typed
        against ``InterventionBus`` keep working until the Phase 3 Agent
        migration moves them onto the Agent-mediated path.
        """
        return await self.deliver(iv)

    # Note: _dispatch_intervention on session.py is now a thin wrapper around
    # InterventionRegistry.dispatch (wave 2 of PR-refactor-session-1). Kept
    # method-level call so the bus signature stays stable.


@dataclass
class ChatMessage:
    role: str  # "user" | "agent" | "skill_event" | "summary"
    text: str
    ts: str
    seq: int = 0  # monotonic per-session sequence id; 0 for non-conversational entries
    meta: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_short(run_id: str) -> str:
    """Last 4 chars of a chat-side run_id, used as a display tag."""
    return run_id[-4:] if run_id else ""


def _run_meta(run_id: str | None, skill_name: str | None) -> dict:
    """Standard `meta` payload for OutboxMessage produced inside a skill spawn."""
    if run_id is None:
        return {"skill_name": skill_name} if skill_name else {}
    return {
        "run_id": run_id,
        "run_id_short": _run_short(run_id),
        "skill_name": skill_name,
    }


def _new_chain_id() -> str:
    """Mint a fresh chain_id for a top-level user request. Each user submission
    starts a new chain; agent_request / agent_response payloads forward the
    chain_id they received without minting new ones."""
    return uuid.uuid4().hex


def _read_memory_index(path: Path) -> str:
    """Return MEMORY.md contents at `path` or empty string if absent."""
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _merge_memory_indexes(
    *, shared_path: Path, agent_path: Path, agent_name: str,
) -> dict:
    """Combine the shared and agent-scoped MEMORY.md files into a single
    `data.memory_index` payload (PR15).

    The router phase used to read `.reyn/memory/MEMORY.md` via a preprocessor
    `file/read` step; that step is removed because the agent-scoped path
    `.reyn/agents/<name>/memory/MEMORY.md` is dynamic and a static phase
    YAML cannot interpolate it. ChatSession synthesizes the merged view
    here and stuffs it directly into the artifact.

    The two layers are kept separate in the output markdown — `(shared)` and
    `(agent: <name>)` — so the LLM can decide which slug path to use when
    writing new memory entries.
    """
    shared = _read_memory_index(shared_path).strip()
    agent  = _read_memory_index(agent_path).strip()

    if not shared and not agent:
        return {"status": "not_found", "content": ""}

    parts: list[str] = []
    if shared:
        parts.append(f"# Memory Index (shared)\n\n{_strip_index_header(shared)}")
    else:
        parts.append("# Memory Index (shared)\n\n(empty)")
    parts.append(
        f"# Memory Index (agent: {agent_name})\n\n"
        f"{_strip_index_header(agent) if agent else '(empty)'}"
    )
    return {"status": "ok", "content": "\n\n".join(parts).strip() + "\n"}


def _strip_index_header(content: str) -> str:
    """Drop a leading `# Memory Index` heading (with optional trailing blank
    lines) from a stored MEMORY.md so we don't render two headings when
    merging. Anything else is returned verbatim."""
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("# Memory Index"):
        # Skip the heading and any immediately-following blank lines.
        i = 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        lines = lines[i:]
    return "\n".join(lines).strip()


# NOTE: `_PendingChain` lives in `reyn.chat.services.chain_manager` (PR-refactor-session-1
# wave 2). Kept import at top of file for backward-compat references.


def _iv_meta(iv: "UserIntervention") -> dict:
    """Standard `meta` payload for OutboxMessage announcing an intervention.

    Includes structured choice data so TUI renderers can build chip buttons
    without re-parsing the formatted text string.

    Issue #163 — adds ``prompt`` and ``detail`` as structured fields so
    the TUI widget can render visual hierarchy (kept in sync with the
    sibling helper in ``services/intervention_handler.py``).
    """
    out: dict = {
        "intervention_id": iv.id,
        "intervention_kind": iv.kind,
        "prompt": iv.prompt,
    }
    if iv.detail:
        out["detail"] = iv.detail
    if iv.run_id:
        out["run_id"] = iv.run_id
        out["run_id_short"] = _run_short(iv.run_id)
    if iv.skill_name:
        out["skill_name"] = iv.skill_name
    if iv.choices:
        out["choices"] = [
            {"id": c.id, "label": c.label, "hotkey": c.hotkey}
            for c in iv.choices
        ]
    if iv.suggestions:
        out["suggestions"] = list(iv.suggestions)
    return out


def _render_summary_for_storage(structured: dict) -> str:
    """Render a chat_summary structured dict to a quick-display text blob.

    Stored in ChatMessage.text so REPL traces and audit dumps don't need
    to re-render the structured form. The slicer prefers the structured
    form for LLM consumption — this is for human consumption only.
    """
    parts: list[str] = []
    topic = (structured.get("topic_arc") or "").strip()
    if topic:
        parts.append(f"[topic] {topic}")
    for key in ("decisions", "pending", "session_user_facts", "artifacts_referenced"):
        items = structured.get(key) or []
        if not items:
            continue
        parts.append(f"[{key}]")
        parts.extend(f"  - {item}" for item in items)
    return "\n".join(parts)


def _extract_skill_input_hint(skill_dir: "Path", entry_phase_name: str) -> dict:
    """Extract input artifact name and top-level field list from a skill's entry phase.

    Returns a dict with:
      - ``input_artifact``: "|"-joined artifact type names from the phase ``input:`` field
        (e.g. ``"user_message | eval_builder_request"``).
      - ``input_fields``: flat list of top-level property names from the first
        non-``user_message`` artifact schema (or from the only artifact if all
        are ``user_message``). Empty list on any read/parse failure.

    Failures are silently swallowed — the hint is best-effort and must not
    break the catalogue enumeration.
    """
    import yaml as _yaml

    try:
        phase_path = skill_dir / "phases" / f"{entry_phase_name}.md"
        if not phase_path.exists():
            return {}
        phase_fm, _ = _split_frontmatter(phase_path.read_text(encoding="utf-8"))
        inputs_raw = phase_fm.get("input", "")
        if not inputs_raw:
            return {}
        artifact_names = [n.strip() for n in str(inputs_raw).split("|") if n.strip()]
        if not artifact_names:
            return {}

        input_artifact = " | ".join(artifact_names)

        # Resolve top-level fields from the first non-user_message artifact,
        # falling back to user_message if that's the only one.
        preferred = [n for n in artifact_names if n != "user_message"] or artifact_names
        input_fields: list[str] = []
        input_schema: dict | None = None
        input_wrapped: bool = True
        artifacts_dir = skill_dir / "artifacts"
        for art_name in preferred:
            art_path = artifacts_dir / f"{art_name}.yaml"
            if not art_path.exists():
                continue
            art_data = _yaml.safe_load(art_path.read_text(encoding="utf-8")) or {}
            schema = art_data.get("schema") or {}
            props = schema.get("properties") or {}
            if props:
                input_fields = list(props.keys())
                input_schema = dict(schema)
                input_wrapped = bool(art_data.get("wrapped", True))
                break

        result: dict = {
            "input_artifact": input_artifact,
            "input_fields": input_fields,
        }
        if input_schema is not None:
            # FP-0034 D2-full step 2: the hot-list alias for skill__<name>
            # exposes the skill's actual input shape on the LLM-facing
            # parameters. ``input_wrapped`` lets the alias builder decide
            # whether to peel the {type, data} envelope.
            result["input_schema"] = input_schema
            result["input_wrapped"] = input_wrapped
        return result
    except Exception:  # noqa: BLE001 — best-effort; never break catalogue
        return {}


def enumerate_available_skills(exclude: set[str]) -> list[dict]:
    """Walk reyn/project, reyn/local, stdlib/skills and collect skill catalogue entries.

    Each entry has ``{name, description}`` always, plus optional fields:
      - ``routing``: block lifted from skill.md frontmatter (intents, examples, …).
      - ``input_artifact``: "|"-joined artifact type names accepted by the entry phase
        (e.g. ``"user_message | eval_builder_request"``). Absent when unavailable.
      - ``input_fields``: flat list of top-level property names from the structured
        input artifact (e.g. ``["target_skill"]``). Empty list = unknown / no
        structured fields. Absent when unavailable.

    The router uses ``routing.intents``, ``routing.when_to_use``,
    ``routing.when_not_to_use``, and ``routing.examples`` to decide whether the
    user's request matches the skill.

    ``input_artifact`` and ``input_fields`` are exposed via ``list_skills``
    so the LLM sees the correct input field names before calling ``invoke_skill``
    (RETRO-H2 fix — plan D: pre-call structural context provision).
    """
    sl = stdlib_root()
    roots = [
        Path("reyn") / "project",
        Path("reyn") / "local",
        sl / "skills",
    ]
    seen: set[str] = set()
    results: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen or d.name in exclude:
                continue
            md = d / "skill.md"
            if not md.exists():
                continue
            try:
                fm, _ = _split_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            description = ""
            if fm.get("description"):
                description = str(fm["description"]).strip().splitlines()[0]
            entry: dict = {"name": fm.get("name") or d.name, "description": description}
            routing = fm.get("routing")
            if isinstance(routing, dict) and routing:
                entry["routing"] = routing
            # RETRO-H2 fix (plan D): inject input artifact + field hint for list_skills.
            entry_phase_name = str(fm.get("entry") or "").strip()
            if entry_phase_name:
                hint = _extract_skill_input_hint(d, entry_phase_name)
                entry.update(hint)
            results.append(entry)
            seen.add(d.name)
    return results


class ChatSession:
    def __init__(
        self,
        agent_name: str,
        model: str = "standard",
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        safety: "SafetyConfig | None" = None,
        mcp_servers: dict | None = None,
        output_language: str | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        agent_role: str = "",
        compaction_config: "CompactionConfig | None" = None,
        registry: "AgentRegistry | None" = None,
        allowed_skills: list[str] | None = None,
        allowed_mcp: list[str] | None = None,
        events_config: EventsConfig | None = None,
        state_log: StateLog | None = None,
        budget_tracker: BudgetTracker | None = None,
        snapshot_path: "Path | None" = None,
        sandbox_config: "SandboxConfig | None" = None,
        action_retrieval_config: "ActionRetrievalConfig | None" = None,
        embedding_config: "EmbeddingConfig | None" = None,
        eager_embedding_build: bool = False,
        agent_id: str | None = None,
    ) -> None:
        """
        snapshot_path: optional override for the per-agent snapshot file
            location. Default: ``.reyn/agents/<agent_name>/state/snapshot.json``
            relative to the current working directory. Tests use this to
            redirect snapshot I/O to a tmp_path without touching private
            attributes.
        """
        self.agent_name = agent_name
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        _safety = safety or SafetyConfig()
        self._safety = _safety
        # FP-0017 follow-up: declarative sandbox config (reyn.yaml `sandbox:`).
        # Plumbed through to spawned Agents so sandboxed_exec backend selection
        # honors the operator's declared policy.
        self._sandbox_config = sandbox_config
        # FP-0034 PR-3b-iii: action_retrieval config — drives whether the
        # universal catalog wrappers appear in the router tools=. Default
        # constructs an off-flag ActionRetrievalConfig so existing chat
        # behaviour is preserved when callers don't pass one.
        self._action_retrieval = action_retrieval_config or ActionRetrievalConfig()
        # B25-S5-1 fix: when True, RouterLoop awaits the embedding index build
        # synchronously on the first turn (= Turn 1 blocks for ~2-5s) so the
        # search_actions wrapper is visible to the LLM from the very first
        # call. Default False keeps the existing lazy background-build path.
        self._eager_embedding_build = eager_embedding_build
        # FP-0016 Component E: agent_id flows from reyn.yaml `agent.id`
        # (= ReynConfig.agent.id) via the session factory. Falls back to
        # `reyn/<hostname>` when callers (= old tests) don't pass one so
        # there's always a non-empty identifier for events / headers.
        if agent_id is None:
            from reyn.config import _default_agent_id
            agent_id = _default_agent_id()
        self._agent_id: str = agent_id
        # FP-0034 Phase 2 step 1: build the ActionEmbeddingIndex +
        # EmbeddingProvider once per session when the operator has
        # configured ``action_retrieval.embedding_class``.  Both stay
        # None when embedding is not configured, in which case the
        # ``search_actions`` wrapper is hidden by ``build_tools`` and
        # the handler degrades to an empty-result response.
        self._action_embedding_index: Any = None
        self._embedding_provider: Any = None
        self._embedding_model_class: str | None = None
        if (
            self._action_retrieval.universal_wrappers_enabled
            and self._action_retrieval.embedding_class
            and embedding_config is not None
        ):
            try:
                from reyn.embedding import get_provider as _get_provider
                from reyn.tools.action_index import ActionEmbeddingIndex
                self._embedding_provider = _get_provider("litellm", embedding_config)
                self._embedding_model_class = self._action_retrieval.embedding_class
                self._action_embedding_index = ActionEmbeddingIndex(
                    persist_dir=Path(".reyn") / "action_index",
                )
            except Exception:
                # If provider construction fails for any reason (= missing
                # dependency / malformed config), fall through to "no index"
                # so the rest of the session continues without
                # search_actions rather than refusing to start.
                self._embedding_provider = None
                self._action_embedding_index = None
                self._embedding_model_class = None
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list freq+recency.
        # Created when universal_wrappers_enabled=True and hot_list_n > 0.
        self._action_usage_tracker: Any = None
        if (
            self._action_retrieval.universal_wrappers_enabled
            and self._action_retrieval.hot_list_n > 0
        ):
            try:
                from reyn.tools.action_usage_tracker import ActionUsageTracker
                # Issue #192: wire a callback that emits ``hot_list_updated``
                # on every reorder of the ranking. Lambda defers
                # ``self._chat_events`` resolution to call time (it's
                # constructed below at the EventLog init); record() runs
                # only during user turns, well after construction completes.
                def _on_hot_list_changed(ranking: list[dict]) -> None:
                    try:
                        self._chat_events.emit(
                            "hot_list_updated", ranking=ranking,
                        )
                    except Exception:
                        pass
                self._action_usage_tracker = ActionUsageTracker(
                    persist_path=Path(".reyn") / "state" / "action_usage.jsonl",
                    on_ranking_changed=_on_hot_list_changed,
                )
            except Exception:
                self._action_usage_tracker = None
        self._mcp_servers = mcp_servers
        self.output_language = output_language
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        self._agent_role = agent_role
        # Optional back-reference for slash commands like /agents / /attach
        # and for agent-to-agent message routing (PR11). The factory in
        # cli/commands/chat.py wires this; tests can leave it None.
        self._registry = registry
        # PR11: max delegation hop depth (LangGraph-style). 0 = user input,
        # each `_send_to_agent` increments. Refuse send when depth > limit.
        self._max_hop_depth = _safety.loop.max_agent_hops
        # PR18: per-chain wall-clock budget. Non-positive disables. When the
        # budget elapses, the runtime synthesizes an error response upstream
        # so a chain stuck on a non-responsive delegate doesn't hang forever.
        self._chain_timeout_seconds = _safety.timeout.chain_seconds
        # FP-0005: per-session safety-limit checkpoint policy.
        self._on_limit = _safety.on_limit
        # FP-0005: per-(turn or chain) extension counters granted by
        # `_handle_limit_checkpoint`. Cleared on turn / chain boundary
        # by the relevant call sites.
        self._safety_extensions: dict[str, float] = {}
        # PR15: optional skill allowlist sourced from profile.allowed_skills.
        # None = unrestricted (default, BC). Empty list = router runs but no
        # skill spawn. stdlib router/compactor are NOT subject to this — they're
        # always available regardless. (FP-0011: skill_narrator removed.)
        self._allowed_skills: list[str] | None = (
            list(allowed_skills) if allowed_skills is not None else None
        )
        # PR37: optional MCP server allowlist from agent profile. None = no
        # per-agent restriction (inherits project config). list[str] = only
        # these servers pass the per-agent check in require_mcp.
        self._allowed_mcp: list[str] | None = (
            list(allowed_mcp) if allowed_mcp is not None else None
        )

        # PR20: per-chat rotation policy. Defaults match EventsConfig.
        self._events_config = events_config or EventsConfig()

        # PR21: WAL + per-agent snapshot for crash recovery. state_log is
        # process-shared (owned by AgentRegistry); when None, persistence
        # is disabled (tests / non-chat invocation).
        # PR-refactor-session-1 wave 2: persistence now flows through
        # SnapshotJournal (extracted service). The session keeps the
        # snapshot_path here only because other init code references it
        # for diagnostic logging — the journal owns the actual I/O.
        self._snapshot_path = snapshot_path or (
            Path(".reyn") / "agents" / self.agent_name / "state" / "snapshot.json"
        )
        self._journal = SnapshotJournal(
            agent_name=self.agent_name,
            snapshot_path=self._snapshot_path,
            state_log=state_log,
        )
        # Track state_log directly for skill resume (PR-skill-resume): the
        # journal owns it for inbox / chain mutations, but skills launched
        # from this session also need it so dispatch_tool can emit step
        # events into the same WAL.
        self._state_log = state_log
        # PR-intervention-link L6: in-memory buffer of answers from
        # restored-then-resolved interventions, keyed by run_id. The first
        # bus.request from the resuming skill at that run_id consumes the
        # entry and returns it without re-dispatching. Persistence across
        # the (user_answered → process_crashed → skill_not_yet_resumed)
        # window is R-D12 follow-up.
        self._buffered_intervention_answers: dict[str, "InterventionAnswer"] = {}
        # Per-agent SkillRegistry — lazily constructed on first skill run.
        # Tracks active skill_run_ids and emits skill lifecycle events.
        # Truncation auto-trigger flows through registry.truncate_wal_if_eligible
        # when an AgentRegistry back-reference is wired (production path);
        # tests with registry=None see no truncation triggers (acceptable).
        self._skill_registry: SkillRegistry | None = None
        # ADR-0023 Phase 2 + ADR-0025: lazy per-agent PlanRegistry. Created
        # on first plan-mode invocation so per-plan snapshots persist
        # alongside SnapshotJournal's WAL-side bookkeeping. Mirrors the
        # SkillRegistry lazy-init pattern.
        self._plan_registry: "Any" = None

        # PR22: budget / rate-limit tracker (process-shared). When None,
        # checks are noops and counters are not maintained.
        # Kept as a direct reference so RouterLoop and other callers that
        # receive the tracker by value can continue to do so unchanged.
        self._budget_tracker = budget_tracker

        # Per-turn router cap: read from safety config.
        _router_cap: int = _safety.loop.max_router_calls_per_turn

        from reyn.config import CompactionConfig
        self._compaction = compaction_config or CompactionConfig()
        self._next_seq = 1

        # `agents/<name>/` is state-only as of PR20: profile / history /
        # memory / .input_history. Audit log lives under `events/`.
        self.workspace_dir = Path(".reyn") / "agents" / self.agent_name
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        # PR20: chat events live at `events/agents/<name>/chat/<YYYY-MM>/...`.
        # The folder is created lazily by EventStore on first write.
        self.events_dir = (
            Path(".reyn") / "events" / "agents" / self.agent_name / "chat"
        )

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        # Detached by default — AgentRegistry.attach() flips this on. Outbox
        # `status`/`trace` emissions are dropped while detached so background
        # agents don't accumulate display noise.
        self.is_attached: bool = False

        self._event_store = EventStore(
            self.events_dir,
            max_bytes=self._events_config.max_bytes,
            max_age_seconds=self._events_config.max_age_seconds,
        )
        self._chat_events = EventLog(
            subscribers=[self._event_store],
            agent_id=self._agent_id,  # FP-0016 E: auto-inject agent_id into every event
        )
        # Issue #162: surface session-level lifecycle events (compaction
        # today; attach/detach + budget warnings as growth) into the
        # conv pane via OutboxMessage(kind="system"). Sibling of the
        # per-skill ChatEventForwarder; both subscribe to event logs but
        # at different scopes.
        from reyn.chat.lifecycle_forwarder import ChatLifecycleForwarder
        self._chat_events.add_subscriber(ChatLifecycleForwarder(self.outbox))

        # PR-refactor-session-1 wave 3 PR1: per-session budget adapter.
        # Absorbs total_usage / total_cost_usd / router-cap state that
        # previously lived as scattered attributes on ChatSession.
        self._budget = BudgetGateway(
            budget_tracker=budget_tracker,
            events=self._chat_events,
            agent_name=self.agent_name,
            default_router_cap=_router_cap,
        )

        # PR-refactor-session-1 wave 3 PR2: memory persistence adapter.
        # Absorbs memory path resolution + remember / forget / read_body.
        # PR3 (RouterHostAdapter) holds a direct reference; session delegates
        # via the adapter's memory_path / memory_dir.
        self._memory = MemoryService(
            agent_workspace_dir=self.workspace_dir,
            events=self._chat_events,
            file_write=self._file_write,
            file_read=self._file_read,
            file_delete=self._file_delete,
            file_regenerate_index=self._file_regenerate_index,
        )

        # FP-0019 Wave 1b: running_skills dicts now owned by SkillRunner.
        # Session exposes forwarding properties for slash commands that
        # access them directly (slash/skill.py, slash/tasks.py).
        # SkillRunner is constructed below after _interventions is ready.

        # ADR-0023 Phase 2 step 7d: per-plan resume task tracking is now
        # owned by PlanRunner (constructed below). ``self.running_plans``
        # remains accessible via a forwarding property — slash commands
        # and mcp_server.py read it directly.

        # PR-refactor-session-1 wave 2: pending-chain lifecycle and intervention
        # queue ownership extracted into services. The session orchestrates the
        # callbacks (_announce_intervention, _on_chain_timeout_fire) but holds
        # no state for them.
        self._chains = ChainManager(
            journal=self._journal,
            events=self._chat_events,
            chain_timeout_seconds=self._chain_timeout_seconds,
            max_hop_depth=self._max_hop_depth,
        )
        self._interventions = InterventionRegistry(
            on_announce=self._announce_intervention,
            # issue #254 Phase 1: fail-closed when no listener is wired
            # (= no TUI mounted, no A2A override, no test fixture
            # registered). Without this, ``handle_limit_exceeded`` with
            # ``ask_timeout_seconds=0`` would await an unresolvable future
            # in test / headless contexts.
            enforce_listener_presence=True,
        )

        # FP-0019 Wave 2 part 1: InterventionHandler — ask_user dispatch service.
        # Extracted from ChatSession.  Session keeps thin wrappers on
        # _dispatch_intervention / _maybe_answer_oldest_intervention /
        # _announce_intervention / _deliver_answer_to so the existing test
        # surface (and ChatInterventionBus) remain stable.
        self._intervention_handler = InterventionHandler(
            intervention_registry=self._interventions,
            journal=self._journal,
            event_log=self._chat_events,
            put_outbox=self._put_outbox,
            append_history=self._append_history_for_handler,
        )

        # FP-0019 Wave 1b: SkillRunner — skill task lifecycle service.
        # Owns running_skills / running_skills_started_at / running_skills_chain.
        # Constructed after _interventions (needed for drop_interventions_for_run
        # callback) and before RouterHostAdapter (which receives spawn_for_router).
        self._skill_runner = SkillRunner(
            event_log=self._chat_events,
            agent_name=self.agent_name,
            output_language=self.output_language,
            mcp_servers=self._mcp_servers,
            allowed_skills=self._allowed_skills,
            budget=self._budget,
            state_log=self._state_log,
            build_agent_fn=self._build_agent_for_skill_runner,
            put_outbox=self._put_outbox,
            enqueue_skill_completed=self._enqueue_skill_completed,
            accumulate=self._accumulate,
            drop_interventions_for_run=self._drop_interventions_for_run,
            get_skill_registry=self._get_skill_registry,
            ask_budget_extension=self._ask_budget_extension,
            outbox=self.outbox,
        )

        # F2: Delegation tracking for RouterLoop runs. Set to a list before
        # calling RouterLoop.run(); send_to_agent appends dispatched targets.
        # None when not inside a RouterLoop run (send_to_agent from old paths
        # does not accumulate). Cleared after each loop run.
        self._router_loop_delegations: list[dict] | None = None

        # F2: Agent-reply capture for agent-to-agent RouterLoop paths.
        # Set to [] before running RouterLoop in agent_request / chain_resolve
        # context; put_outbox appends "agent" kind text here so callers can
        # forward the reply upstream. None = not capturing (user-turn context).
        self._router_loop_agent_replies: list[str] | None = None

        # RunSpawner wave: PlanRunner — plan task lifecycle (spawn / resume).
        # Owns ``running_plans``; session exposes a forwarding property.
        # Constructed BEFORE RouterHostAdapter because the adapter binds
        # ``spawn_plan_task=self._plan_runner.spawn_plan_task`` as one of
        # its callbacks. PlanRunner needs ``_router_host`` for plan
        # artifact cleanup, resolved lazily via ``get_router_host``.
        self._plan_runner = PlanRunner(
            agent_name=self.agent_name,
            put_outbox=self._put_outbox,
            enqueue_plan_completed=self._enqueue_plan_completed,
            journal=self._journal,
            get_router_host=lambda: self._router_host,
        )

        # PR-refactor-session-1 wave 3 PR3: RouterHostAdapter — concrete
        # RouterLoopHost implementation extracted from ChatSession. Constructed
        # last in __init__ because it receives callbacks that reference self
        # (all of which are bound methods, resolved at call time not here).
        self._router_host = RouterHostAdapter(
            agent_name=self.agent_name,
            agent_role=self._agent_role,
            output_language=self.output_language,
            allowed_skills=self._allowed_skills,
            allowed_mcp=self._allowed_mcp,
            permission_resolver=self._perm,
            mcp_servers=self._mcp_servers,
            project_context=self._project_context,
            events=self._chat_events,
            resolver=self._resolver,
            memory=self._memory,
            journal=self._journal,
            agent_registry=self._registry,
            skill_enumerate_fn=enumerate_available_skills,
            agent_workspace_dir=self.workspace_dir,
            plan_registry_getter=self._get_plan_registry,
            file_read=self._file_read,
            file_write=self._file_write,
            file_delete=self._file_delete,
            file_list_directory=self._file_list_directory,
            file_regenerate_index=self._file_regenerate_index,
            mcp_list_servers=self._mcp_list_servers,
            mcp_list_tools=self._mcp_list_tools,
            mcp_call_tool=self._mcp_call_tool,
            run_skill_awaitable=self._skill_runner.run_skill_awaitable,
            spawn_skill=self._skill_runner.spawn_for_router,
            send_to_agent=self._send_to_agent,
            put_outbox=self._put_outbox,
            append_history=self._append_history,
            spawn_plan_task=self._plan_runner.spawn_plan_task,
            delegation_tracker=lambda: self._router_loop_delegations,
            agent_replies_tracker=lambda: self._router_loop_agent_replies,
            universal_wrappers_enabled=self._action_retrieval.universal_wrappers_enabled,
            action_embedding_index=self._action_embedding_index,
            embedding_provider=self._embedding_provider,
            embedding_model_class=self._embedding_model_class,
            # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list.
            action_usage_tracker=self._action_usage_tracker,
            action_retrieval_config=self._action_retrieval,
            # FP-0034 Phase 2: sandbox backend for exec D14 visibility gate.
            # None when sandbox_config is None (= noop assumed).
            sandbox_backend=(
                self._sandbox_config.backend if self._sandbox_config is not None
                else None
            ),
            # B25-S5-1: thread eager-build flag so RouterLoop awaits build
            # before computing _search_visible on the first turn.
            eager_embedding_build=self._eager_embedding_build,
            # FP-0022 fix (#53): give the router OpContext a real
            # InterventionBus so web_fetch / mcp install / mcp drop
            # handlers can run their interactive (Layer 4) approval
            # flow. The bus is built per make_router_op_context() call
            # — short-lived, scoped to the chat_router skill, identical
            # to what session._mcp_call_tool wires manually today.
            intervention_bus_factory=lambda: ChatInterventionBus(
                self, run_id=None, skill_name="chat_router",
            ),
        )

        # FP-0019 Wave 1: background head/body/tail compaction service.
        # Owns the asyncio.Task lifecycle; session delegates via spawn_maybe()
        # and cancel().  All callbacks resolve against self at call time.
        self._compaction_controller = CompactionController(
            event_log=self._chat_events,
            config=self._compaction,
            history_access=lambda: self.history,
            latest_summary=self._latest_summary,
            run_compaction_skill=self._skill_runner.run_stdlib,
            history_appender=self._append_history,
            make_summary_message=lambda rendered, structured, covers: ChatMessage(
                role="summary",
                text=rendered,
                ts=_now_iso(),
                meta={"structured": structured, "covers_through_seq": covers},
            ),
            render_summary=_render_summary_for_storage,
        )

        # FP-0019 Wave 3: crash recovery service.
        # Discovers in-flight skill_runs from WAL and re-spawns them on
        # session start.  All business logic lives in AutoResumeHandler;
        # session delegates via _auto_resume_active_skills() (thin wrapper).
        self._auto_resume_handler = AutoResumeHandler(
            event_log=self._chat_events,
            state_log=self._state_log,
            get_skill_registry=self._get_skill_registry,
            drop_interventions_for_run=self._drop_interventions_for_run,
            launcher=self._skill_runner.spawn_resumed_skill,
        )

        # FP-0019 Wave 2 part 2: A2AHandler — agent-to-agent messaging service.
        # Extracts _send_to_agent / _send_agent_response / _handle_agent_request /
        # _handle_agent_response / _resolve_pending_chain from ChatSession.
        # Hybrid design (案 C): A2AHandler owns agent-side logic; transport-side
        # routing handled by FP-0013 RoutingLayer via send_request_callback /
        # send_response_callback injection.
        # FP-0001: chain_id-scoped intervention bus overrides.
        # Allows A2A async-mode tasks to redirect ask_user prompts to
        # their RunRegistry-backed A2AInterventionBus while the agent's
        # default ChatInterventionBus continues to serve chat-mode interactions.
        self._intervention_overrides: dict[str, "InterventionBus"] = {}

        self._a2a_handler = A2AHandler(
            event_log=self._chat_events,
            chain_manager=self._chains,
            agent_name=self.agent_name,
            max_hop_depth=self._max_hop_depth,
            safety_extensions=self._safety_extensions,
            output_language=self.output_language,
            append_history=self._append_history_for_a2a_handler,
            put_outbox=self._put_outbox,
            handle_chat_limit_checkpoint=self._handle_chat_limit_checkpoint,
            run_router_loop=lambda text, cid: self._run_router_loop(text, cid),
            reset_router_turn_counter=self._reset_router_turn_counter,
            send_request_callback=self._a2a_send_request,
            send_response_callback=self._a2a_send_response,
            on_chain_timeout_fire=self._on_chain_timeout_fire,
            get_router_loop_delegations=lambda: self._router_loop_delegations,
            set_router_loop_delegations=lambda v: setattr(self, "_router_loop_delegations", v),
            get_router_loop_agent_replies=lambda: self._router_loop_agent_replies,
            set_router_loop_agent_replies=lambda v: setattr(self, "_router_loop_agent_replies", v),
        )

    # ── cost accumulation ───────────────────────────────────────────────────────

    def _accumulate(self, result) -> None:
        self._budget.accumulate(result)

    @property
    def total_usage(self):
        return self._budget.total_usage

    @property
    def total_cost_usd(self) -> float:
        return self._budget.total_cost_usd

    # ── SkillRunner forwarding (FP-0019 Wave 1b) ────────────────────────────────
    # slash/skill.py and slash/tasks.py access these dicts directly via session.
    # Forward to SkillRunner so external callers see the same live dict.

    @property
    def running_skills(self) -> dict:
        """Forwarding property → SkillRunner.running_skills."""
        return self._skill_runner.running_skills

    @property
    def running_skills_started_at(self) -> dict:
        """Forwarding property → SkillRunner.running_skills_started_at."""
        return self._skill_runner.running_skills_started_at

    @property
    def running_skills_chain(self) -> dict:
        """Forwarding property → SkillRunner.running_skills_chain."""
        return self._skill_runner.running_skills_chain

    @property
    def running_plans(self) -> dict:
        """Forwarding property → PlanRunner.running_plans.

        Slash commands (slash/plan.py), mcp_server.py shutdown gather,
        and the TUI app read this directly; the dict itself is owned by
        PlanRunner.
        """
        return self._plan_runner.running_plans

    def _build_agent_for_skill_runner(
        self,
        run_id: str | None,
        skill_name: str | None,
        *,
        subscribers: list | None = None,
    ) -> "Agent":
        """Build an Agent wired with a per-spawn ChatInterventionBus.

        Supplied as ``build_agent_fn`` to SkillRunner so SkillRunner
        never imports ChatInterventionBus or holds a session reference.
        """
        return self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=subscribers,
        )

    # ── persistence ─────────────────────────────────────────────────────────────

    def _append_history(self, msg: ChatMessage) -> None:
        # Assign monotonic seq for conversational entries (user/agent). Other
        # roles (skill_event, summary) keep seq=0 — they aren't part of the
        # turn ordering used by the slicer.
        if msg.role in ("user", "agent") and msg.seq == 0:
            msg.seq = self._next_seq
            self._next_seq += 1
        self.history.append(msg)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    def _append_history_for_handler(
        self, role: str, text: str, ts: str, meta: dict,
    ) -> None:
        """Adapter callback injected into InterventionHandler.

        InterventionHandler needs to append a user history entry when an
        intervention is answered.  This adapter bridges the handler's
        ``(role, text, ts, meta)`` signature to ChatSession._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(role=role, text=text, ts=ts, meta=meta))

    def _append_history_for_a2a_handler(
        self, role: str, text: str, ts: str, meta: dict,
    ) -> None:
        """Adapter callback injected into A2AHandler.

        A2AHandler uses the same ``(role, text, ts, meta)`` signature as
        InterventionHandler.  This adapter bridges to ChatSession._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(role=role, text=text, ts=ts, meta=meta))

    # ── A2A transport callbacks (FP-0019 Wave 2 part 2) ─────────────────────────
    # Session-side wrappers that perform registry topology checks and the
    # actual submit_agent_request / submit_agent_response transport calls.
    # A2AHandler delegates here after its own depth / guard logic; these
    # callbacks are the FP-0013 RoutingLayer integration seam.

    async def _a2a_send_request(
        self,
        to: str, from_agent: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Transport callback: validate topology and submit agent_request to ``to``.

        Checks existence + topology permit via AgentRegistry, then boots the
        target session (idempotent) and calls ``submit_agent_request``.
        """
        if self._registry is None or not self._registry.exists(to):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r} not found",
                meta={"chain_id": chain_id},
            ))
            return
        # PR12: topology gate.
        if not self._registry.permit(from_agent, to):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"agent {to!r}: blocked by topology rules",
                meta={"chain_id": chain_id},
            ))
            return
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)
        await target.submit_agent_request(
            from_agent=from_agent, request=request,
            depth=depth, chain_id=chain_id,
        )

    async def _a2a_send_response(
        self,
        to: str, from_agent: str, response: str, depth: int, chain_id: str,
    ) -> None:
        """Transport callback: submit agent_response to ``to``.

        Silently drops when the target no longer exists (race on shutdown).
        """
        if self._registry is None or not self._registry.exists(to):
            return
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)
        await target.submit_agent_response(
            from_agent=from_agent, response=response,
            depth=depth, chain_id=chain_id,
        )

    def load_history(self) -> None:
        if not self.history_path.exists():
            return
        with self.history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.history.append(ChatMessage(**json.loads(line)))
                except Exception:
                    continue
        # Initialize the seq counter past any seqs already in the file. Old
        # entries without seq fall back to 0; the synthetic seq for them is
        # assigned by the slicer at read time, so we only care about the
        # max of explicitly-stored seqs here for the next-write counter.
        max_seen = max((m.seq for m in self.history if m.seq), default=0)
        self._next_seq = max_seen + 1

    # ── inbox API ───────────────────────────────────────────────────────────────

    async def submit_user_text(self, text: str) -> None:
        # PR14: every top-level user submission starts a fresh chain_id that
        # propagates through any agent_request / agent_response generated in
        # response. Logged in history meta + events.jsonl for cross-agent trace.
        await self._put_inbox(
            "user", {"text": text, "chain_id": _new_chain_id()},
        )

    async def submit_agent_request(
        self, *, from_agent: str, request: str, depth: int, chain_id: str,
    ) -> None:
        await self._put_inbox("agent_request", {
            "from_agent": from_agent, "request": request, "depth": depth,
            "chain_id": chain_id,
        })

    async def submit_agent_response(
        self, *, from_agent: str, response: str, depth: int, chain_id: str,
    ) -> None:
        await self._put_inbox("agent_response", {
            "from_agent": from_agent, "response": response, "depth": depth,
            "chain_id": chain_id,
        })

    async def shutdown(self) -> None:
        # `shutdown` is a control signal, not recovery state — skip WAL/snapshot.
        await self.inbox.put(("shutdown", {}))

    # ── PR21: state persistence helpers (WAL + snapshot) ─────────────────────
    # PR-refactor-session-1 wave 2: WAL/snapshot ownership moved to
    # SnapshotJournal; pending_chains lifecycle moved to ChainManager.
    # The methods below are thin delegators kept for the session-internal
    # call sites (inbox enqueue + dequeue, restoration orchestration).

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        """Append `inbox_put` to WAL via journal, then queue on the async
        inbox. Returns the assigned message id (also stamped into payload
        as `_msg_id` so the consumer can look it up)."""
        msg_id = await self._journal.append_inbox(kind=kind, payload=payload)
        full_payload = {**payload, "_msg_id": msg_id}
        await self.inbox.put((kind, full_payload))
        return msg_id

    async def _consume_inbox(self) -> tuple[str, dict]:
        """Wait for next inbox message; on receive, record `inbox_consume`
        via journal (skipped for shutdown signals which are out-of-band)."""
        kind, payload = await self.inbox.get()
        msg_id = payload.get("_msg_id") if isinstance(payload, dict) else None
        if kind != "shutdown":
            await self._journal.consume_inbox(msg_id=msg_id)
        return kind, payload

    def restore_state(self, snapshot: AgentSnapshot) -> None:
        """Adopt a recovered snapshot: install in journal, repopulate the
        async inbox, restore pending chains via ChainManager (which re-arms
        timeout watchdogs), and re-enqueue outstanding interventions
        (PR-intervention-link L5) so the user can clear them after restart.

        Callable from async context only — restoration schedules asyncio
        tasks."""
        self._journal.install(snapshot)
        for msg in snapshot.inbox:
            self.inbox.put_nowait((msg["kind"], msg["payload"]))
        self._chains.restore(on_fire=self._on_chain_timeout_fire)
        # R-D12: rehydrate the durable buffered intervention answers from
        # the snapshot. If a previous restart had buffered an answer (user
        # answered a restored intervention) and a SECOND crash hit before
        # the resuming skill consumed it, we still have the answer here.
        for run_id, ans in snapshot.buffered_intervention_answers.items():
            if not isinstance(ans, dict):
                continue
            self._buffered_intervention_answers[run_id] = InterventionAnswer(
                text=ans.get("text", ""),
                choice_id=ans.get("choice_id"),
            )
        # Re-enqueue interventions in FIFO insertion order (dict preserves
        # insertion order in py3.7+). Each restored iv gets a fresh future
        # and a watcher task so dispatch's finally clause fires
        # ``intervention_resolved`` to prune the snapshot when the user
        # answers.
        if snapshot.outstanding_interventions:
            restored = [
                UserIntervention.from_dict(iv_dict)
                for iv_dict in snapshot.outstanding_interventions.values()
            ]

            async def _on_restored_resolved(iv: UserIntervention) -> None:
                # Restored interventions DON'T re-emit ``intervention_dispatched``
                # (that event is already in the WAL from the original run).
                # We do TWO things here:
                #   1. Buffer the user's answer keyed by run_id so the
                #      resuming skill's first ask_user picks it up (L6).
                #      R-D12: buffer is also durably persisted via
                #      ``record_intervention_answer_buffered`` so the
                #      answer survives a second crash before the skill
                #      resumes.
                #   2. Emit ``intervention_resolved`` to prune the snapshot's
                #      outstanding_interventions entry.
                if iv.future.done() and iv.run_id:
                    try:
                        answer = iv.future.result()
                    except (asyncio.CancelledError, Exception):
                        answer = None
                    if answer is not None:
                        self._buffered_intervention_answers[iv.run_id] = answer
                        await self._journal.record_intervention_answer_buffered(
                            run_id=iv.run_id,
                            text=answer.text,
                            choice_id=answer.choice_id,
                        )
                await self._journal.record_intervention_resolved(
                    intervention_id=iv.id,
                )

            self._restore_intervention_tasks = self._interventions.restore(
                restored, watcher=_on_restored_resolved,
            )
        self._chat_events.emit(
            "session_restored",
            applied_seq=snapshot.applied_seq,
            inbox_size=len(snapshot.inbox),
            pending_chains=len(snapshot.pending_chains),
            outstanding_interventions=len(snapshot.outstanding_interventions),
        )

    # ── main loop ───────────────────────────────────────────────────────────────

    async def run_one_iteration(self) -> bool:
        """Process exactly one inbox kind.  Returns False on shutdown, True otherwise.

        Same handler dispatch as run(); the only difference is no while-loop.
        Callers decide when to pump again — long-lived sessions loop forever
        (CUI), request-driven sessions pump until idle (MCP / A2A via
        MessageBus).

        FP-0013 Component B: this is the pumping primitive.  MessageBus.request
        drives this from the MCP / A2A request-handler task so the LLM call
        executes on the same task that holds the event loop, sidestepping the
        anyio stdio-starvation failure mode documented in FP-0013 §ADR-A.

        Does NOT emit chat_started / chat_stopped events — those are emitted by
        run() which owns the session lifetime.  Does NOT call _drain_on_shutdown;
        that is also run()'s responsibility on loop exit.
        """
        kind, payload = await self._consume_inbox()
        if kind == "shutdown":
            return False
        if kind == "user":
            await self._handle_user_message(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or _new_chain_id(),
            )
        elif kind == "agent_request":
            await self._handle_agent_request(payload)
        elif kind == "agent_response":
            await self._handle_agent_response(payload)
        elif kind == "skill_completed":
            # FP-0012: a background-spawned skill finished. Inject a
            # user-role completion message into the existing thread
            # and run one router LLM turn for narration.
            await self._handle_skill_completed(payload)
        elif kind == "plan_completed":
            # FP-0025 C: a background plan finished. Inject a
            # user-role message with step_results and run one
            # router LLM turn for synthesis narration.
            await self._handle_plan_completed(payload)
        return True

    async def run(self) -> None:
        self._chat_events.emit("chat_started", agent_name=self.agent_name, model=self.model)

        try:
            while await self.run_one_iteration():
                pass
        finally:
            await self._drain_on_shutdown()
            self._chat_events.emit("chat_stopped", agent_name=self.agent_name)
            await self._put_outbox(OutboxMessage(kind="__end__", text=""))

    async def _drain_on_shutdown(self) -> None:
        """Wait for in-flight skill runs to complete, then cancel stragglers.

        Memory writes happen inline during each router turn, so there is no
        background extraction to drain — shutdown is teardown of whatever the
        user explicitly launched, plus a final await on the compaction task
        (if any) so the summary entry gets persisted before the process exits.

        B27-H4 fix: give in-flight skill tasks a 30-second grace window to
        complete naturally before the hard cancel.  Without the grace window,
        skills whose LLM call is in-progress at session shutdown receive
        ``asyncio.CancelledError``, which propagates through
        ``RunOrchestrator.run()`` → ``skill_run_interrupted`` instead of
        ``skill_run_completed``.  The 30-second limit prevents hanging
        indefinitely on a stalled LLM call.

        #52 fix: also suppress the benign ``coroutine
        'OpenAIChatCompletion.acompletion' was never awaited`` RuntimeWarning
        that litellm 1.84.0 ``main.py:614-622`` emits when our forced
        ``cancel_all()`` delivers ``CancelledError`` at the exact checkpoint
        between ``init_response = await loop.run_in_executor(...)`` and the
        downstream ``await init_response``. The inner coroutine being
        unawaited is the cancelled LLM request — semantically correct
        behaviour for a forced shutdown. The filter is scoped to the
        cancel_all() block so genuine missing-await bugs elsewhere stay
        visible.
        """
        import warnings

        # FP-0019 Wave 1b: delegated to SkillRunner.
        # Grace window: wait up to 30 s for background skills to land their
        # skill_run_completed event before resorting to cancellation.
        await self._skill_runner.wait_for_completion(timeout_sec=30.0)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    r".*coroutine 'OpenAIChatCompletion\.acompletion' "
                    r"was never awaited.*"
                ),
                category=RuntimeWarning,
            )
            await self._skill_runner.cancel_all()

        # PR18: cancel any pending chain-timeout watchdogs so they don't keep
        # the loop alive past shutdown. Late-firing timers swallow their work
        # (the pending entry is gone) but cancellation is cleaner.
        # PR-refactor-session-1 wave 2: cancellation delegated to ChainManager.
        await self._chains.shutdown()

        # FP-0019 Wave 1: delegated to CompactionController.
        await self._compaction_controller.cancel()

    async def _handle_user_message(self, text: str, *, chain_id: str) -> None:
        # Slash commands (`/list`, `/cancel <id>`, `/answer <id> <text>`) take
        # precedence over both the active-intervention router and a fresh
        # router turn.
        if text.startswith("/"):
            if await self._maybe_handle_slash(text):
                return
        # If a spawned skill is waiting on a user intervention (ask_user or
        # permission prompt), route this input to that intervention instead of
        # starting a fresh router turn.
        if await self._maybe_answer_oldest_intervention(text):
            return

        # R-D4: chat turn boundary — opportunistically check WAL size and
        # truncate if it has grown past the safety-net threshold. Long-idle
        # skills (1 phase + LLM-only loop) and multi-agent / multi-chain
        # idle sessions don't fire phase-completion events, so without this
        # the WAL would grow unboundedly between turns. The check is cheap
        # (one stat() call); the rewrite only fires on bloat. Fire-and-
        # forget so a slow rewrite doesn't block the user's turn.
        if self._registry is not None:
            asyncio.create_task(
                self._registry.maybe_truncate_for_size(),
                name="wal-size-safety-net",
            )

        self._append_history(ChatMessage(
            role="user", text=text, ts=_now_iso(),
            meta={"chain_id": chain_id},
        ))
        self._chat_events.emit("user_message_received", text=text, chain_id=chain_id)
        await self._put_outbox(OutboxMessage(
            kind="status", text="thinking…", meta={"chain_id": chain_id},
        ))

        # Reset the per-turn router cap counter at the top of each fresh
        # user turn. Subsequent in-chain re-invocations (agent_response on
        # this chain, _resolve_pending_chain) accumulate against the same
        # budget without resetting.
        self._reset_router_turn_counter()

        # FP-0037 issue #160: lazy MCP tool discovery cache. First user
        # turn probes every configured MCP server's tool list once;
        # subsequent turns no-op. Zero startup latency; first-turn cost
        # is bounded by per_server_timeout (default 5s, parallel).
        # When no MCP servers are configured this is a near-free no-op.
        await self._router_host.ensure_mcp_tools_cached()

        try:
            await self._run_router_loop(text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=classify_router_error(exc),
                meta={"chain_id": chain_id},
            ))
            return

        # FP-0019 Wave 1: fire-and-forget compaction check after the user has
        # the reply.  CompactionController owns the single-flight lock and the
        # background asyncio.Task.  _drain_on_shutdown awaits it via cancel().
        self._compaction_controller.spawn_maybe()

    # ── skill invocation helpers ────────────────────────────────────────────────

    async def _auto_resume_active_skills(
        self,
        *,
        coordinator: "SkillResumeCoordinator | None" = None,
        config: "SkillResumeConfig | None" = None,
        launcher: "Callable[[Any], Awaitable[None]] | None" = None,
    ) -> list:
        """Thin delegation wrapper → AutoResumeHandler._resume_and_collect.

        FP-0019 Wave 3: business logic extracted to
        ``src/reyn/chat/services/auto_resume_handler.py``.  This wrapper
        preserves the original call signature and list-return type so
        existing callers (tests + startup chain) continue to work unchanged.

        ``launcher`` is dependency-injected so tests can inspect decisions
        without launching real skill runtimes.  Production callers pass
        ``None`` to use the default launcher (``SkillRunner.spawn_resumed_skill``).

        Returns the list of decisions that were launched (= decisions
        minus discards).
        """
        return await self._auto_resume_handler._resume_and_collect(
            coordinator=coordinator,
            config=config,
            launcher=launcher,
        )

    # NOTE: ``_spawn_resumed_skill`` moved to SkillRunner.spawn_resumed_skill
    # (RunSpawner wave). Callers go through ``self._skill_runner`` directly;
    # the AutoResumeHandler wiring in __init__ uses the method reference.

    def _get_skill_registry(self) -> "SkillRegistry | None":
        """Return the per-agent SkillRegistry, lazily constructed on first call.

        Returns None when no state_log is wired (test / standalone mode) —
        with no WAL to write to, the registry would be a no-op anyway.

        The truncate-eligible hook closes over the back-reference to the
        owning AgentRegistry; if `registry` is None (test fixtures that
        don't construct a full process tree), the hook is None and
        ``advance_phase`` / ``complete`` skip the truncation trigger. This
        keeps truncation a production concern, not a test concern.
        """
        if self._state_log is None:
            return None
        if self._skill_registry is None:
            agent_state_dir = (
                Path(".reyn") / "agents" / self.agent_name / "state"
            )
            hook = None
            if self._registry is not None:
                # Bind self._registry into a hook that fires after every
                # ``skill_phase_advanced`` / ``skill_completed``. Throttle
                # + floor calc happen inside truncate_wal_if_eligible.
                async def _truncate_hook() -> None:
                    if self._registry is not None:
                        await self._registry.truncate_wal_if_eligible()
                hook = _truncate_hook
            self._skill_registry = SkillRegistry(
                agent_name=self.agent_name,
                agent_state_dir=agent_state_dir,
                state_log=self._state_log,
                truncate_eligible_hook=hook,
            )
        return self._skill_registry

    def _get_plan_registry(self) -> "Any":
        """Return the per-agent PlanRegistry, lazily constructed on first call.

        ADR-0023 Phase 2 + ADR-0025: per-plan snapshots persist
        alongside SnapshotJournal's WAL-side bookkeeping. Without this
        registry hook, ADR-0023 forward replay has nothing to read on
        resume (PlanRegistry.load_active() returns empty), and ADR-0025
        sub-loop LLM memoization has nowhere to record.

        Returns None when no state_log is wired — test / standalone
        mode without persistence.

        Truncate hook mirrors _get_skill_registry: fires
        AgentRegistry.truncate_wal_if_eligible after every durable
        per-plan mutation (= last_step_applied_seq bump).
        """
        if self._state_log is None:
            return None
        if self._plan_registry is None:
            from reyn.plan import PlanRegistry
            agent_state_dir = (
                Path(".reyn") / "agents" / self.agent_name / "state"
            )
            hook = None
            if self._registry is not None:
                async def _truncate_hook() -> None:
                    if self._registry is not None:
                        await self._registry.truncate_wal_if_eligible()
                hook = _truncate_hook
            self._plan_registry = PlanRegistry(
                agent_name=self.agent_name,
                agent_state_dir=agent_state_dir,
                truncate_eligible_hook=hook,
            )
        return self._plan_registry

    def _build_agent(
        self,
        *,
        intervention_bus: InterventionBus | None = None,
        mcp_servers: dict | None = None,
        subscribers: list | None = None,
    ) -> Agent:
        """Construct an Agent with this session's shared defaults applied."""
        return Agent(
            model=self.model,
            resolver=self._resolver,
            permission_resolver=self._perm,
            safety=self._safety,
            mcp_servers=mcp_servers,
            intervention_bus=intervention_bus,
            subscribers=subscribers,
            prompt_cache_enabled=self._prompt_cache_enabled,
            project_context=self._project_context,
            agent_role=self._agent_role,
            caller=f"agents/{self.agent_name}",
            budget_tracker=self._budget_tracker,
            sandbox_config=self._sandbox_config,
        )

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        """Drop transient kinds while detached; durable kinds are queued.

        While `is_attached=False` (PR10 multi-agent: agent running in the
        background), `status`/`trace` carry no value to a detached display
        and would just accumulate in the queue. `agent`/`skill_done`/
        `intervention`/`error`/`__end__` are kept so they reach the user
        when re-attached or remain in history (history append happens
        independently in callers).
        """
        if not self.is_attached and msg.kind in {"status", "trace"}:
            return
        await self.outbox.put(msg)

    def _load_stdlib_skill(self, skill_name: str):
        """Load a stdlib skill by its directory name. Propagates parse errors."""
        sl = stdlib_root()
        skill_md = sl / "skills" / skill_name / "skill.md"
        return load_dsl_skill(str(skill_md), skill_root=str(sl))

    async def _run_stdlib_skill(
        self,
        skill_name: str,
        input_artifact: dict,
        *,
        state_subdir: str,
        mcp_servers: dict | None = None,
        forward_events: bool = False,
    ):
        """Thin delegation to SkillRunner.run_stdlib (FP-0019 Wave 1b).

        Kept for callers that still reference this name directly.
        Returns the RunResult. Callers handle exceptions.
        """
        return await self._skill_runner.run_stdlib(
            skill_name, input_artifact,
            state_subdir=state_subdir,
            mcp_servers=mcp_servers,
            forward_events=forward_events,
        )

    # ── compaction helpers (FP-0019 Wave 1) ────────────────────────────────────
    # Business logic lives in CompactionController.  Session keeps only the
    # helpers that are still needed as injected callbacks.

    def _latest_summary(self) -> ChatMessage | None:
        """Return the most recent summary message, or None."""
        for m in reversed(self.history):
            if m.role == "summary":
                return m
        return None

    # ── router ──────────────────────────────────────────────────────────────────

    async def _emit_router_cap_exhausted_user(
        self, exc: "RouterCapExceeded", *, chain_id: str,
    ) -> None:
        """User-facing fallback when the per-turn router cap is reached.
        Emits a structured error + a polite agent reply on the outbox so
        the chat loop recovers cleanly. The underlying event was already
        emitted by `_check_and_increment_router_cap`."""
        await self._put_outbox(OutboxMessage(
            kind="error",
            text=(
                f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                f"for this turn. Last reason: "
                f"{exc.last_reason or '(none)'}. Falling back to direct reply."
            ),
            meta={"chain_id": chain_id},
        ))
        fallback = _ROUTER_RETRY_EXHAUSTED_MSG.get(
            self.output_language,
            _ROUTER_RETRY_EXHAUSTED_MSG["en"],
        )
        await self._put_outbox(OutboxMessage(
            kind="agent", text=fallback, meta={"chain_id": chain_id},
        ))
        self._append_history(ChatMessage(
            role="agent", text=fallback, ts=_now_iso(),
            meta={
                "chain_id": chain_id,
                "source": "router_cap_exhausted",
            },
        ))

    def _reset_router_turn_counter(self) -> None:
        """Reset the per-turn router invocation counter. Called at the top
        of each fresh turn (`_handle_user_message`, `_handle_agent_request`).
        Re-entrant in-chain paths (`_handle_agent_response` continuation,
        `_resolve_pending_chain`) intentionally do NOT reset — their
        invocations count against the same budget."""
        self._budget.reset_router_turn_counter()

    async def _handle_chat_limit_checkpoint(
        self,
        *,
        kind: str,
        prompt: str,
        detail: str,
        extension_amount: float,
        run_id: str | None = None,
    ) -> "LimitDecision":
        """FP-0005: chat-side wrapper for ``handle_limit_exceeded``.

        Mirrors ``OSRuntime._handle_limit_checkpoint`` but uses the
        ChatSession's intervention dispatcher (= ``_dispatch_intervention``,
        which records the WAL ``intervention_dispatched`` event before
        delivering the prompt) + on_limit + a session-stable run_id
        (= the agent name when no narrower scope applies, or the
        current chain_id for chain-scoped checkpoints). Emits a
        ``safety_limit_checkpoint`` audit event so the decision is
        visible alongside the existing chat events.
        """
        # Adapter that conforms to the InterventionBus Protocol by
        # delegating to ChatSession's existing intervention dispatcher.
        # _dispatch_intervention records the intervention_dispatched /
        # intervention_resolved WAL events automatically, so per-site
        # callers don't need to.
        session_dispatch = self._dispatch_intervention

        class _ChatLimitBus:
            async def request(self, iv):  # type: ignore[no-untyped-def]
                return await session_dispatch(iv)

        decision = await handle_limit_exceeded(
            bus=_ChatLimitBus(),
            on_limit=self._on_limit,
            kind=kind,
            run_id=run_id or self.agent_name,
            prompt=prompt,
            detail=detail,
            extension_amount=extension_amount,
        )
        if decision.allow_continue:
            self._safety_extensions[kind] = (
                self._safety_extensions.get(kind, 0.0) + decision.extension
            )
        self._chat_events.emit(
            "safety_limit_checkpoint",
            kind=kind,
            allow_continue=decision.allow_continue,
            reason=decision.reason,
            extension=decision.extension,
        )
        return decision

    async def _check_and_increment_router_cap(self, user_text: str) -> None:
        """Increment the per-turn router invocation counter and enforce the
        cap. Raises RouterCapExceeded when the counter would exceed the
        configured cap. cap=0 disables the check.

        FP-0005: when ``safety.on_limit.mode`` is ``interactive`` /
        ``auto_extend`` and the cap is hit, ask the user / auto-extend
        before re-raising. On approval the cap is extended by the
        configured amount and the run continues.
        """
        try:
            self._budget.check_and_increment_router_cap(user_text)
        except RouterCapExceeded as exc:
            decision = await self._handle_chat_limit_checkpoint(
                kind="router_cap",
                prompt=(
                    f"Router hit the per-turn cap of {exc.cap} invocations. "
                    f"Allow more invocations this turn?"
                ),
                detail=(
                    f"count={exc.count} cap={exc.cap} "
                    f"last_reason={exc.last_reason}"
                ),
                extension_amount=1.0,
            )
            if not decision.allow_continue:
                raise
            # Approved — extend the cap and increment for THIS attempt.
            self._budget.extend_router_cap(int(decision.extension))
            self._budget.check_and_increment_router_cap(user_text)

    # ── backward-compat shims for Tier-4 scaffold tests ─────────────────────
    # These proxy the gateway's private counter/reason through the session
    # surface so existing tests that directly read/write these attributes
    # continue to pass until the Tier-4 tests are replaced.

    @property
    def _router_invocations_this_turn(self) -> int:
        return self._budget._router_invocations_this_turn

    @_router_invocations_this_turn.setter
    def _router_invocations_this_turn(self, value: int) -> None:
        self._budget._router_invocations_this_turn = value

    @property
    def _router_last_reason(self) -> str:
        return self._budget._router_last_reason

    @_router_last_reason.setter
    def _router_last_reason(self, value: str) -> None:
        self._budget._router_last_reason = value

    # ── intervention routing (thin wrappers → InterventionHandler) ──────────────
    # Business logic lives in InterventionHandler (FP-0019 Wave 2 part 1).
    # These thin wrappers preserve the session-level surface used by
    # ChatInterventionBus, slash commands, and existing Tier 2 tests.

    async def _maybe_answer_oldest_intervention(self, text: str) -> bool:
        """Thin wrapper → InterventionHandler.maybe_answer."""
        return await self._intervention_handler.maybe_answer(text)

    async def _deliver_answer_to(self, iv: UserIntervention, text: str) -> bool:
        """Thin wrapper → InterventionHandler.deliver_answer_to."""
        return await self._intervention_handler.deliver_answer_to(iv, text)

    async def _announce_intervention(self, iv: UserIntervention) -> None:
        """Thin wrapper → InterventionHandler.announce."""
        await self._intervention_handler.announce(iv)

    def register_intervention_override(self, chain_id: str, bus: "InterventionBus") -> None:
        """Register an InterventionBus for ask_user prompts emitted by
        skills spawned under this chain_id. Caller must pair with
        unregister_intervention_override in a try/finally."""
        self._intervention_overrides[chain_id] = bus

    def unregister_intervention_override(self, chain_id: str) -> None:
        """Remove an override. Idempotent."""
        self._intervention_overrides.pop(chain_id, None)

    # ── Listener registration (issue #254 Phase 1) ──────────────────────────

    def register_intervention_listener(self, listener_id: str) -> None:
        """Declare that *listener_id* will route user answers back into
        the session (= call ``_maybe_answer_oldest_intervention`` /
        ``_deliver_answer_to`` when the user responds).

        Without an active listener, ``_dispatch_intervention`` would
        enqueue a prompt that nothing will resolve — under
        ``ask_timeout_seconds=0`` that turns into an infinite await.
        Callers in real entry points register on mount (TUI app on
        compose, A2A async-task wiring, etc.); tests register a
        placeholder when they intend to drive the answer themselves via
        ``_maybe_answer_oldest_intervention``. issue #254 Phase 1.
        """
        self._interventions.register_listener(listener_id)

    def unregister_intervention_listener(self, listener_id: str) -> None:
        """Remove *listener_id* from the active set. Idempotent."""
        self._interventions.unregister_listener(listener_id)

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Thin wrapper → InterventionHandler.dispatch.

        ChatInterventionBus, _handle_chat_limit_checkpoint, and
        _ask_budget_extension all call this method directly; keeping it
        as a session-level entry keeps those call sites stable.
        """
        # FP-0001: chain_id-scoped override path (A2A async tasks).
        if iv.run_id is not None and self._intervention_overrides:
            chain_id = self.running_skills_chain.get(iv.run_id)
            if chain_id is not None:
                override = self._intervention_overrides.get(chain_id)
                if override is not None:
                    return await override.request(iv)
        # Default: route through the regular InterventionHandler.
        return await self._intervention_handler.dispatch(iv)

    # ── Agent-layer intervention entry point (issue #254 Phase 3) ───────────

    async def handle_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Agent-layer entry point for incoming intervention requests.

        This is the Agent's ``RequestBus`` subscriber-side handler.
        Phase 4 implements the 3-way routing decision the Agent makes
        on every incoming request:

          1. **self_answer** (= ``_try_self_answer`` hook): the agent
             has a policy that answers without consulting the user
             (e.g. "I've already extended this limit 5 times, refuse").
             Default policy is None — no self-answer — so the request
             falls through. Future incremental PRs add per-kind
             policies (e.g. "max_phase_visits hit + N prior extensions
             → refuse silently") via subclassing or config-driven
             policy injection.
          2. **parent_agent.delegate** (= ``_resolve_parent_agent`` hook):
             forward to a chain-upstream agent so the originating
             user-facing agent owns the decision. Default returns None
             — no parent resolution — so the request falls through.
             Phase 5+ adds the chain-walk via the running_skills_chain
             registry + an agent-lookup factory.
          3. **user_channel.deliver** (= default branch): route the
             prompt through ``_dispatch_intervention``, which preserves
             the chain-override path (A2A peer) + the regular
             ``InterventionHandler.dispatch`` (TUI) fall-through. This
             is the only branch active by default in Phase 4, so the
             behaviour is identical to Phase 3 for unmodified agents.

        Each branch emits an ``intervention_routed`` event so observers
        (= TUI events tab, debug traces, future routing-policy A/B
        analysis) can see which routing decision fired without
        instrumenting the hook implementations themselves.

        Callers that obtain a ``RequestBus``-typed view of an Agent use
        ``ChatSession.as_request_bus()`` (which returns an
        ``AgentRequestBus`` adapter forwarding ``request(iv)`` here).
        """
        # Branch 1: self_answer policy.
        self_ans = await self._try_self_answer(iv)
        if self_ans is not None:
            self._chat_events.emit(
                "intervention_routed",
                route="self_answer",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            return self_ans

        # Branch 2: parent-agent delegation.
        parent = self._resolve_parent_agent(iv)
        if parent is not None:
            self._chat_events.emit(
                "intervention_routed",
                route="parent_delegate",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            return await parent.handle_intervention(iv)

        # Branch 3: default — deliver to user via existing dispatch path.
        self._chat_events.emit(
            "intervention_routed",
            route="user_channel",
            iv_kind=iv.kind,
            iv_id=iv.id,
        )
        return await self._dispatch_intervention(iv)

    async def _try_self_answer(
        self, iv: UserIntervention,
    ) -> InterventionAnswer | None:
        """Hook for self-answer routing policies (issue #254 Phase 4).

        Return an ``InterventionAnswer`` to bypass the user and resolve
        the request from agent-internal state; return ``None`` to fall
        through to subsequent routing branches.

        Default implementation returns ``None`` (= no self-answer
        policy). Subclasses or future config-driven policy injection
        override this to encode per-kind policies. The default keeps
        Phase 4 behaviour identical to Phase 3 for unmodified agents.

        Examples of future overrides (NOT in this PR):
          - "max_phase_visits limit hit + we've already auto-extended
            ``N`` times this chain → refuse with text='no'"
          - "permission.shell on a command in the always-allow set →
            return InterventionAnswer(choice_id='always')"
        """
        return None

    def _resolve_parent_agent(
        self, iv: UserIntervention,
    ) -> "ChatSession | None":
        """Hook for parent-agent delegation routing (issue #254 Phase 4).

        Return a ChatSession to forward the request to a chain-upstream
        agent; return ``None`` to fall through to user_channel delivery.

        Default implementation returns ``None`` (= no parent resolution).
        Phase 5+ will walk ``running_skills_chain`` to find the
        originating agent and look it up via an agent-registry factory;
        Phase 4 only establishes the routing branch.
        """
        return None

    def as_request_bus(self) -> "AgentRequestBus":
        """Return a ``RequestBus``-typed adapter for this ChatSession.

        OS-layer callers (= ``handle_limit_exceeded``, permission gates,
        ``ask_user`` op) can hold an ``AgentRequestBus`` without
        importing ChatSession or knowing about the Agent's downstream
        routing choices. The adapter forwards ``request(iv)`` to
        ``handle_intervention(iv)``.

        issue #254 Phase 3 — the type-level realisation of the [A]
        contract from Phase 2: OS owns a ``RequestBus``, the bus is
        backed by an Agent (= ChatSession), the Agent owns the routing
        decision and the downstream ``UserChannel`` selection.
        """
        return AgentRequestBus(self)

    async def _ask_budget_extension(
        self,
        *,
        chain_id: str,
        skill_name: str,
        check,  # BudgetCheck
    ) -> bool:
        """FP-0003: ask the user to approve extending a hard-limit cap.

        FP-0005: now generalised to call the shared
        ``handle_limit_exceeded`` helper so all seven safety / budget
        checkpoints share one implementation. Returns True iff the
        decision allows continuing (= ``user_approved`` or
        ``auto_extended``); any other outcome (refused / timeout /
        bus failure / unattended) returns False so the caller falls
        through to the original refusal path.

        Note: the per-(chain, skill) extension bookkeeping is owned by
        ``BudgetTracker.extend_chain_calls`` (= the count counter is
        the FP-0003 source of truth, not ``self._safety_extensions``).
        This method only signals approval; the caller applies the
        extension via the tracker.
        """
        ctx = check.context or {}
        used = int(ctx.get("current") or 0)
        base = int(ctx.get("base_hard") or 0)
        granted = int(ctx.get("extensions_granted") or 0)
        extension = int(ctx.get("extension_calls") or 0)
        prompt = (
            f"Skill {skill_name!r} has hit the chain hard-limit "
            f"({used} of {base + granted}). "
            f"Approve {extension} additional spawn(s) for this chain?"
        )
        detail = (
            f"chain={chain_id} dimension={check.hard_dimension} "
            f"detail={check.detail}"
        )
        # FP-0005: per_chain_skill_calls.ask_on_exceed implies
        # interactive intent regardless of the global on_limit.mode
        # — the user explicitly opted into prompting via
        # ``cost.per_chain_skill_calls.ask_on_exceed: true``. Build a
        # local OnLimitConfig that reflects this so the helper
        # dispatches the prompt rather than falling through.
        from reyn.config import OnLimitConfig as _OnLimitConfig
        local_on_limit = _OnLimitConfig(
            mode="interactive",
            ask_timeout_seconds=self._on_limit.ask_timeout_seconds,
        )
        # Reuse the chat-side bus adapter from _handle_chat_limit_checkpoint.
        session_dispatch = self._dispatch_intervention

        class _ChatLimitBus:
            async def request(self, iv):  # type: ignore[no-untyped-def]
                return await session_dispatch(iv)

        decision = await handle_limit_exceeded(
            bus=_ChatLimitBus(),
            on_limit=local_on_limit,
            kind=f"per_chain_skill_calls:{chain_id}:{skill_name}",
            run_id=chain_id,
            prompt=prompt,
            detail=detail,
            extension_amount=float(extension),
            skill_name=skill_name,
        )
        self._chat_events.emit(
            "safety_limit_checkpoint",
            kind="per_chain_skill_calls",
            allow_continue=decision.allow_continue,
            reason=decision.reason,
            extension=decision.extension,
        )
        return decision.allow_continue

    def _drop_interventions_for_run(self, run_id: str | None) -> None:
        """Cancel any pending interventions tagged with `run_id`.

        The registry's drop cancels the futures; ``_dispatch_intervention``'s
        finally clause then fires ``intervention_resolved`` to the WAL for
        each cancelled coroutine, so the snapshot's
        ``outstanding_interventions`` is pruned correctly.

        R-D12: also clears any durable buffered answer for this run — the
        run is gone, nothing should consume the answer. Both the
        in-memory dict AND the on-disk buffer are dropped.
        """
        self._interventions.drop_for_run(run_id)
        if run_id is not None:
            had_buffered = (
                self._buffered_intervention_answers.pop(run_id, None) is not None
            )
            # If we cleared an in-memory buffered answer, also fire the
            # consumed event so the durable copy in the snapshot gets
            # pruned. Fire-and-forget; if the loop is gone (teardown),
            # the durable entry persists harmlessly until next restart.
            if had_buffered:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        self._journal.record_intervention_answer_consumed(
                            run_id=run_id,
                        ),
                        name=f"buffered-answer-dropped-{run_id}",
                    )

    def _consume_buffered_intervention_answer(
        self, run_id: str,
    ) -> "InterventionAnswer | None":
        """Pop and return the buffered answer for ``run_id`` if any.

        PR-intervention-link L6 — used by ChatInterventionBus.request to
        short-circuit dispatch when a previous (crashed-then-restored)
        run's intervention was already answered post-restart.

        R-D12: when an answer is consumed, fire the durable
        ``intervention_answer_consumed`` event so the on-disk buffer
        also drops. Async-fire-and-forget keeps the consume path sync
        for the bus to call from request().
        """
        answer = self._buffered_intervention_answers.pop(run_id, None)
        if answer is not None:
            # Schedule the durable consume on the running loop. Outside
            # an async context (test teardown, sync helpers), no loop
            # is available — the in-memory buffer is already cleared,
            # and a future restart's stale snapshot entry is corrected
            # at restore time when the buffered answer is actually
            # consumed by a resumed skill.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(
                    self._journal.record_intervention_answer_consumed(
                        run_id=run_id,
                    ),
                    name=f"buffered-answer-consumed-{run_id}",
                )
        return answer

    # ── agent-to-agent messaging (PR11 / PR14) ──────────────────────────────────
    # FP-0019 Wave 2 part 2: business logic extracted to A2AHandler service.
    # Session keeps thin delegators here so existing internal call sites
    # (_on_chain_timeout_fire, _on_chain_peer_discarded, RouterHostAdapter
    # send_to_agent callback) continue to resolve without changes.

    async def _send_to_agent(
        self, *, to: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Thin delegator — business logic lives in A2AHandler.send_to_agent."""
        await self._a2a_handler.send_to_agent(
            to=to, request=request, depth=depth, chain_id=chain_id,
        )

    async def _send_agent_response(
        self, *, to: str, response: str, depth: int, chain_id: str,
    ) -> None:
        """Thin delegator — business logic lives in A2AHandler.send_agent_response."""
        await self._a2a_handler.send_agent_response(
            to=to, response=response, depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_request(self, payload: dict) -> None:
        """Thin delegator — business logic lives in A2AHandler.handle_agent_request."""
        await self._a2a_handler.handle_agent_request(payload)

    async def _handle_agent_response(self, payload: dict) -> None:
        """Thin delegator — business logic lives in A2AHandler.handle_agent_response."""
        await self._a2a_handler.handle_agent_response(payload)

    async def _handle_skill_completed(self, payload: dict) -> None:
        """FP-0012: drive narration of a background-spawned skill's completion.

        Called from ``run()`` when a ``skill_completed`` inbox message
        arrives (= enqueued by ``_run_one_skill`` on terminal status).
        Injects a synthesized ``user``-role message into the existing
        conversation thread carrying the structured completion data,
        then runs one router LLM turn so the LLM extracts user-relevant
        fields and produces a 1-2 sentence narration.

        The user-role injection is the only currently-supported way to
        re-engage the router LLM mid-conversation: tool_result messages
        require a paired ``tool_use`` block that has already been
        consumed (the spawn-ack), so a second tool_result for the same
        invocation isn't valid per OpenAI / Anthropic API rules.

        ``meta.source="skill_completion"`` distinguishes this from a
        genuine user-typed message in audit / replay paths; the LLM
        sees the text content but not the meta envelope.
        """
        run_id = payload.get("run_id", "")
        skill_name = payload.get("skill", "")
        status = payload.get("status") or "finished"
        chain_id_raw = payload.get("chain_id") or ""
        chain_id = chain_id_raw or _new_chain_id()
        data = payload.get("data") or {}

        # Build the user-role message text. Use a stable header so the
        # router SP's completion-narration rule (Component C) can match
        # on `[task_completed]` reliably. JSON-encode `data` so the LLM
        # sees the actual fields (= avoids lossy string coercion).
        try:
            data_str = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            data_str = repr(data)
        injected_text = (
            f"[task_completed] chain_id={chain_id} run_id={run_id}\n"
            f"skill: {skill_name}  status: {status}\n"
            f"result: {data_str}\n\n"
            "Please summarize what completed for the user in 1-2 sentences "
            "per the post-invoke_skill narration rules in your instructions."
        )

        self._append_history(ChatMessage(
            role="user", text=injected_text, ts=_now_iso(),
            meta={
                "source": "skill_completion",
                "skill": skill_name,
                "run_id": run_id,
                "status": status,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "skill_completion_injected",
            run_id=run_id, skill=skill_name, status=status, chain_id=chain_id,
        )

        # Reset the per-turn router cap counter — completion narration is a
        # fresh turn boundary from the user's perspective (a new outbox reply
        # will be produced).
        self._reset_router_turn_counter()

        try:
            await self._run_router_loop(injected_text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (skill_completed): {exc}",
                meta={"chain_id": chain_id, "skill": skill_name, "run_id": run_id},
            ))
            return

    async def drain_skill_completed_inbox(
        self, *, deadline_monotonic: float,
    ) -> bool:
        """FP-0012 / R-A2A-COMPLETION-DRAIN: dispatch queued
        ``skill_completed`` inbox kinds inline up to a deadline.

        ``session.run()`` is the normal consumer of the inbox, but the
        A2A / MCP bypass path (= ``mcp_server.send_to_agent_impl``)
        drives ``_handle_user_message`` directly without ever starting
        ``session.run()`` (asyncio-starvation under the stdio transport).
        Without this drain, a non-blocking ``invoke_skill`` that spawns
        a skill in the background never gets its completion narration
        produced under A2A — the caller only sees the spawn ack.

        Behaviour:

        - Pops every queued inbox item non-blockingly.
        - For ``skill_completed`` kinds, records the WAL consume entry
          (mirrors what ``_consume_inbox`` would do) and dispatches to
          ``_handle_skill_completed`` within the remaining deadline
          budget so the router LLM produces the narration.
        - Other kinds are preserved (re-queued in original order) so
          the next consumer / call can pick them up.

        Returns ``True`` if the drain completed before the deadline,
        ``False`` if the deadline fired mid-drain (= partial reply).
        """
        import time as _time

        deferred: list[tuple[str, dict]] = []
        drained_ok = True
        while True:
            try:
                item = self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            kind, payload = item
            if kind != "skill_completed":
                deferred.append(item)
                continue
            # Mirror the journal-consume bookkeeping that
            # ``_consume_inbox`` performs so the WAL record matches.
            msg_id = (
                payload.get("_msg_id") if isinstance(payload, dict) else None
            )
            try:
                await self._journal.consume_inbox(msg_id=msg_id)
            except Exception as exc:  # noqa: BLE001 — best effort, drain proceeds
                logger.warning(
                    "drain_skill_completed_inbox: WAL consume failed "
                    "msg_id=%s: %s",
                    msg_id, exc,
                )
            remaining = max(0.1, deadline_monotonic - _time.monotonic())
            # ``asyncio.timeout()`` (Python 3.11+) instead of
            # ``asyncio.wait_for`` because ``_handle_skill_completed``
            # drives a router LLM turn (= litellm → httpx async →
            # internal anyio cancel scopes). If wait_for wraps the
            # coroutine in a new task and the timeout fires mid-LLM
            # call, the httpx cleanup runs in a different task than
            # the entry → ``RuntimeError: Attempted to exit cancel
            # scope in a different task...``. ``asyncio.timeout()``
            # is a task-local deadline so the cleanup stays in-task.
            try:
                async with asyncio.timeout(remaining):
                    await self._handle_skill_completed(payload)
            except asyncio.TimeoutError:
                drained_ok = False
                break
            except Exception as exc:  # noqa: BLE001 — log + continue
                logger.warning(
                    "drain_skill_completed_inbox: handler failed "
                    "run_id=%s skill=%s: %s",
                    payload.get("run_id"), payload.get("skill"), exc,
                )
        # Restore non-skill_completed kinds (FIFO order preserved).
        for item in deferred:
            self.inbox.put_nowait(item)
        return drained_ok

    # ── chain timeout (PR18) ───────────────────────────────────────────────────
    # PR-refactor-session-1 wave 2: timer arm/cancel + sleep-and-fire loop are
    # now owned by ChainManager. The session keeps the on-fire callback below
    # so the upstream-error UX (synthesised response + chain_timeout event)
    # stays out of the service layer.

    async def _on_chain_timeout_fire(self, chain_id: str) -> None:
        """ChainManager invokes this when a chain's timeout watchdog fires.

        Pops the pending chain via `_chains.fire_timeout` (which also
        records the WAL `chain_timeout_fired` event), emits the
        `chain_timeout` audit event, and synthesises an error response
        upstream so the parent chain doesn't hang.

        FP-0005: when ``safety.on_limit.mode`` opts in (interactive /
        auto_extend), the watchdog peeks at the pending chain *before*
        firing and asks whether to re-arm with a fresh deadline.
        ``unattended`` (= default) preserves the legacy fire-and-error
        behaviour byte-for-byte.
        """
        # FP-0005: try to re-arm the watchdog before firing if the
        # operator opted in. The ChainManager's fire_timeout pop is
        # destructive, so peek first via the registry's `get` accessor.
        if self._on_limit.mode != "unattended":
            pending_peek = self._chains.get(chain_id)
            if pending_peek is not None:
                waiting_peek = sorted(pending_peek.waiting_on)
                decision = await self._handle_chat_limit_checkpoint(
                    kind=f"chain_seconds:{chain_id}",
                    prompt=(
                        f"Chain {chain_id} timed out waiting for "
                        f"{', '.join(waiting_peek) or 'unknown'} after "
                        f"{self._chain_timeout_seconds:g}s. Wait longer?"
                    ),
                    detail=(
                        f"chain={chain_id} waiting_on={waiting_peek} "
                        f"timeout={self._chain_timeout_seconds:g}s"
                    ),
                    extension_amount=float(self._chain_timeout_seconds),
                    run_id=chain_id,
                )
                if decision.allow_continue:
                    # Re-arm the watchdog for another window.
                    self._chains.arm_timeout(
                        chain_id, on_fire=self._on_chain_timeout_fire,
                    )
                    self._chat_events.emit(
                        "chain_timeout_extended",
                        chain_id=chain_id,
                        waiting_on=waiting_peek,
                        extension_seconds=decision.extension,
                        reason=decision.reason,
                    )
                    return  # do NOT fire timeout
        pending = await self._chains.fire_timeout(chain_id)
        if pending is None:
            return  # resolved between sleep wake and fire — nothing to do
        waiting = sorted(pending.waiting_on)
        # FP-0004: hint at the config key the operator can raise.
        error_text = (
            f"chain timeout: {len(waiting)} delegate(s) "
            f"({', '.join(waiting) or 'unknown'}) did not respond within "
            f"{self._chain_timeout_seconds:g}s. "
            f"→ Raise safety.timeout.chain_seconds to wait longer "
            f"(0 = no timeout)."
        )
        self._chat_events.emit(
            "chain_timeout",
            chain_id=chain_id,
            waiting_on=waiting,
            timeout_seconds=self._chain_timeout_seconds,
            origin_agent=pending.origin_agent,
        )
        try:
            await self._send_agent_response(
                to=pending.origin_agent,
                response=error_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
            )
        except Exception as exc:
            # Don't let send failures wedge the loop — we already removed
            # the pending entry, so the worst case is a chain that lost its
            # error message but already won't hang.
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain timeout: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    async def _on_chain_peer_discarded(
        self, *, chain_id: str, peer: str, reason: str,
    ) -> None:
        """R-D14: AgentRegistry calls this when a peer agent's
        skill_run for ``chain_id`` was discarded by the user.

        Mirrors ``_on_chain_timeout_fire`` but for the discard path:
        force-resolves the pending chain immediately, emits a
        ``chain_peer_discarded`` audit event, and sends a synthesised
        agent_response upstream so the user-visible reply doesn't
        hang waiting for the (now-dead) peer.

        Idempotent: returns silently if the chain has already been
        resolved (by a parallel agent_response or earlier timeout).
        """
        pending = await self._chains.resolve(chain_id)
        if pending is None:
            return
        waiting = sorted(pending.waiting_on)
        error_text = (
            f"chain interrupted: peer agent {peer!r} discarded its "
            f"skill_run ({reason}); waiting_on={waiting}"
        )
        self._chat_events.emit(
            "chain_peer_discarded",
            chain_id=chain_id,
            peer=peer,
            reason=reason,
            waiting_on=waiting,
            origin_agent=pending.origin_agent,
        )
        try:
            await self._send_agent_response(
                to=pending.origin_agent,
                response=error_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
            )
        except Exception as exc:  # noqa: BLE001 — never wedge the loop
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain peer discarded: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    # ── slash command dispatch ──────────────────────────────────────────────────

    def _resolve_run_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Find a unique run_id matching `prefix` (anywhere within the id).

        Matches against the full id OR the trailing 4-char short tag, since
        users see `[skill#abcd]` and naturally type the short tag.

        Returns (run_id, candidates). `run_id` is non-None only when exactly
        one candidate matches; otherwise inspect `candidates`.
        """
        prefix = prefix.strip()
        if not prefix:
            return None, []
        candidates = [
            rid for rid in self.running_skills
            if rid.startswith(prefix) or rid.endswith(prefix)
        ]
        return (candidates[0] if len(candidates) == 1 else None), candidates

    def _resolve_intervention_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Same shape as `_resolve_run_id` but over the intervention registry."""
        return self._interventions.resolve_id_prefix(prefix)

    async def _maybe_handle_slash(self, text: str) -> bool:
        """Dispatch `/command args...` lines. Returns True when consumed.

        Delegates to the SlashRegistry in `reyn.chat.slash` so new commands
        can be added without touching this method.

        Unknown slash commands also return True (with a hint on outbox) to
        keep the router from running on user typos like "/halp".

        Multi-line slash input: slash commands today are line-oriented and
        do not accept multi-line args. When the user submits `/cmd …\nmore`,
        the trailing content was previously bundled into `args` and then
        silently dropped by handlers that ignore their args (e.g. `/cost`,
        `/help`, `/list`). We now warn before dispatching and feed the
        handler only the first line, so the user sees that the extra lines
        were not part of the command.
        """
        from reyn.chat.slash import REGISTRY

        # Multi-line guard — keep only the first line for dispatch, warn if
        # any non-whitespace content exists on later lines.
        first_line, sep, rest = text.partition("\n")
        if sep and rest.strip():
            await self._put_outbox(OutboxMessage(
                kind="system",
                text=(
                    f"note: {first_line.split(maxsplit=1)[0]} ignored extra "
                    "lines; only the first line is treated as the command."
                ),
            ))
        text = first_line

        body = text[1:].lstrip()
        if not body:
            known = ", ".join(f"/{n}" for n in REGISTRY.names())
            await self._put_outbox(OutboxMessage(
                kind="system",
                text=f"known commands: {known}",
            ))
            return True
        parts = body.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        slash_cmd = REGISTRY.get(cmd)
        if slash_cmd is None:
            known = ", ".join(f"/{n}" for n in REGISTRY.names())
            await self._put_outbox(OutboxMessage(
                kind="system",
                text=f"unknown command /{cmd}; try: {known}",
            ))
            return True
        await slash_cmd.handler(self, args)
        return True

    # NOTE: the 7 ``_slash_*`` handlers (list / cancel / answer / agents /
    # attach / cost / budget) live in ``src/reyn/chat/slash/`` per the
    # cli-redesign plan. ``_resolve_run_id`` / ``_resolve_intervention_id``
    # / ``_deliver_answer_to`` stay here as session-state helpers the slash
    # modules call back into.

    # ── skill spawn (FP-0019 Wave 1b) ───────────────────────────────────────────
    # Business logic lives in SkillRunner. Session keeps thin delegating
    # methods for backward compat with any remaining internal callers.

    async def _spawn_skill_for_router(
        self, spec: dict, *, chain_id: str
    ) -> dict:
        """Thin delegation to SkillRunner.spawn_for_router (FP-0019 Wave 1b)."""
        return await self._skill_runner.spawn_for_router(spec, chain_id=chain_id)

    async def _spawn_skill(self, spec: dict, *, chain_id: str | None = None) -> None:
        """Thin delegation to SkillRunner.spawn (FP-0019 Wave 1b)."""
        await self._skill_runner.spawn(spec, chain_id=chain_id)

    async def _enqueue_skill_completed(
        self,
        *,
        run_id: str,
        skill: str,
        chain_id: str | None,
        status: str,
        data: dict,
    ) -> None:
        """FP-0012: enqueue a ``skill_completed`` inbox message so the
        chat ``run()`` loop picks up the completion on its next iteration.

        Bounded by a try/except — if the session is shutting down (= journal
        closed) we swallow the error and rely on the outbox already-emitted
        status/error message for user visibility. The skill_run_completed /
        skill_run_failed event was emitted by the caller; that's the audit
        truth (P6). The inbox message is just the narration trigger.
        """
        try:
            await self._put_inbox(
                "skill_completed",
                {
                    "run_id": run_id,
                    "skill": skill,
                    "chain_id": chain_id or "",
                    "status": status,
                    "data": data,
                },
            )
        except Exception as exc:
            logger.warning(
                "skill_completed inbox enqueue failed for run_id=%s skill=%s: %s",
                run_id, skill, exc,
            )

    async def _enqueue_plan_completed(
        self,
        *,
        plan_id: str,
        chain_id: str,
        goal: str,
        step_results: dict[str, str],
        step_failures: dict[str, str],
        n_steps: int,
    ) -> None:
        """FP-0025 C: enqueue plan_completed inbox for router narration."""
        try:
            await self._put_inbox(
                "plan_completed",
                {
                    "plan_id": plan_id,
                    "chain_id": chain_id,
                    "goal": goal,
                    "step_results": step_results,
                    "step_failures": step_failures,
                    "n_steps": n_steps,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_enqueue_plan_completed failed for %s: %r", plan_id, exc)

    async def _handle_plan_completed(self, payload: dict) -> None:
        """FP-0025 C: narrate plan completion via one router LLM turn.

        Symmetric with _handle_skill_completed (FP-0012). Injects a
        [plan_completed] user-role message into history so the router
        LLM sees step_results and synthesises a user reply.
        """
        plan_id = payload.get("plan_id", "")
        chain_id = payload.get("chain_id") or _new_chain_id()
        goal = payload.get("goal", "")
        step_results = payload.get("step_results") or {}
        step_failures = payload.get("step_failures") or {}
        try:
            results_str = json.dumps(step_results, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            results_str = repr(step_results)
        injected_text = (
            f"[plan_completed] plan_id={plan_id}\n"
            f"goal: {goal}\n"
            f"step_results:\n{results_str}\n\n"
            "Please synthesize the step results into a complete response for the user."
        )
        if step_failures:
            try:
                failures_str = json.dumps(step_failures, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                failures_str = repr(step_failures)
            injected_text += f"\n\nstep_failures:\n{failures_str}\n"
        self._append_history(ChatMessage(
            role="user", text=injected_text, ts=_now_iso(),
            meta={
                "source": "plan_completion",
                "plan_id": plan_id,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "plan_completion_injected",
            plan_id=plan_id, chain_id=chain_id,
        )
        self._reset_router_turn_counter()
        try:
            await self._run_router_loop(injected_text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (plan_completed): {exc}",
                meta={"chain_id": chain_id, "plan_id": plan_id},
            ))
            return

    # ── RouterLoop helper methods (Wave 3 F1, kept for session callbacks) ──────────
    # _make_router_op_context + 3 helpers remain on ChatSession because the
    # session's internal MCP/file callbacks (_mcp_list_tools, _mcp_call_tool,
    # _file_op) use them. The adapter has its own private copies.

    def _get_file_permissions_for_router(self) -> dict | None:
        """Return file permissions in the form {read: [paths], write: [paths]}
        for the router's tool catalog. None if no file permissions configured.

        Reads from self._perm (PermissionResolver) config to expose what
        paths are permitted. Returns None when no PermissionResolver is
        wired or when no file.read/file.write is configured.
        """
        if self._perm is None:
            return None
        config = self._perm._config or {}
        read_val = config.get("file.read") or (config.get("file") or {}).get("read")
        write_val = config.get("file.write") or (config.get("file") or {}).get("write")

        # "allow" string → treat as project-wide wildcard
        read_paths: list[str] = []
        write_paths: list[str] = []

        if read_val == "allow":
            read_paths = ["*"]
        elif isinstance(read_val, list):
            for entry in read_val:
                if isinstance(entry, str):
                    read_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    read_paths.append(str(entry["path"]))

        if write_val == "allow":
            write_paths = ["*"]
        elif isinstance(write_val, list):
            for entry in write_val:
                if isinstance(entry, str):
                    write_paths.append(entry)
                elif isinstance(entry, dict) and entry.get("path"):
                    write_paths.append(str(entry["path"]))

        if not read_paths and not write_paths:
            return None
        return {"read": read_paths, "write": write_paths}

    def _mcp_servers_flat(self) -> dict:
        """Unwrap config.mcp's `{servers: {...}}` shape to flat `{name: cfg}`.

        ChatSession receives the wrapped form from CLI bootstrap (config.mcp).
        The Agent / control_ir_executor unwraps via `.get("servers", {})`;
        chat-router-side helpers historically did not (PR35 oversight) and
        treated "servers" as if it were a server name. Centralized unwrap.
        """
        raw = self._mcp_servers or {}
        if isinstance(raw, dict) and "servers" in raw:
            inner = raw.get("servers") or {}
            return inner if isinstance(inner, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def _get_mcp_servers_for_router(self) -> list[dict]:
        """Return [{name, description}, ...] for configured MCP servers
        accessible to this agent. [] if none."""
        servers = self._mcp_servers_flat()
        if not servers:
            return []
        result: list[dict] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            result.append({
                "name": name,
                "description": cfg.get("description", ""),
            })
        return result

    def _make_router_op_context(self) -> "OpContext":
        """Build a minimal OpContext for router-initiated file/MCP ops.

        Uses the session's events log and permission resolver. The skill_name
        "chat_router" is used for permission key lookups — it matches what the
        PermissionResolver uses to gate paths. All .reyn/ paths are in the
        default write zone so memory ops pass without additional approval.

        PermissionDecl is populated from the agent's effective permissions
        (file_read / file_write from config, mcp from configured servers) so
        that op_runtime layer permission checks actually gate access rather than
        silently allowing everything through an empty decl.
        """
        from reyn.op_runtime.context import OpContext
        from reyn.permissions.permissions import PermissionDecl
        from reyn.workspace.workspace import Workspace

        file_perms = self._get_file_permissions_for_router() or {}
        mcp_servers = self._get_mcp_servers_for_router() or []

        # Convert flat path strings to the {path, scope} dict format used by PermissionDecl
        file_read = [{"path": p, "scope": "recursive"} for p in file_perms.get("read", [])]
        file_write = [{"path": p, "scope": "recursive"} for p in file_perms.get("write", [])]
        mcp_names = [s["name"] for s in mcp_servers]

        decl = PermissionDecl(
            file_read=file_read,
            file_write=file_write,
            mcp=mcp_names,
            allowed_mcp=self._allowed_mcp,
            mcp_install=True,   # ADR-0029: allow ask gate to fire for MCP install
            index_drop=True,    # B17-S8-3: allow ask gate to fire for index drop
        )

        workspace = Workspace(
            events=self._chat_events,
            permission_resolver=self._perm,
            skill_name="chat_router",
        )
        return OpContext(
            workspace=workspace,
            events=self._chat_events,
            permission_decl=decl,
            permission_resolver=self._perm,
            skill_name="chat_router",
            mcp_servers=self._mcp_servers_flat(),
            run_id=None,  # FP-0021: chat router is outside run scope
            agent_id=self._agent_id,  # FP-0016 E
        )

    async def _file_op(self, op_dict: dict) -> dict:
        """Dispatch a file op via op_runtime. Returns result dict."""
        from reyn.op_runtime import execute_op
        from reyn.schemas.models import FileIROp

        op = FileIROp(**op_dict)
        ctx = self._make_router_op_context()
        return await execute_op(op, ctx, caller="control_ir")

    async def _file_read(self, path: str) -> dict:
        """Read a file through op_runtime.

        Returns: {"path": path, "content": <text>} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "read", "path": path})
        if result.get("status") == "ok":
            return {"path": path, "content": result.get("content", "")}
        if result.get("status") == "not_found":
            return {"error": f"file not found: {path}"}
        return {"error": result.get("error", "read failed")}

    async def _file_write(self, path: str, content: str) -> dict:
        """Write a file through op_runtime.

        Returns: {"path": path, "written": True} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "write", "path": path, "content": content})
        if result.get("status") == "ok":
            return {"path": path, "written": True}
        return {"error": result.get("error", "write failed")}

    async def _file_delete(self, path: str) -> dict:
        """Delete a file through op_runtime.

        Returns: {"path": path, "deleted": bool} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "delete", "path": path})
        if result.get("status") == "ok":
            return {"path": path, "deleted": result.get("deleted", True)}
        return {"error": result.get("error", "delete failed")}

    async def _file_list_directory(self, path: str) -> dict:
        """List directory contents through op_runtime (glob).

        Returns: {"path": path, "entries": [...]} or {"error": ...}.

        Path normalisation: the LLM frequently sends ``"/"`` or ``""`` when
        it really means "the project root I'm allowed to read". A literal
        ``"/"`` resolves to the filesystem root, which is outside the
        permission scope and triggers a misleading "no read permission"
        error. Map both to ``"."`` (= cwd) so the typical "list files
        here" intent works on a fresh project without requiring path
        education.
        """
        normalised = path
        if normalised in ("", "/", "./"):
            normalised = "."
        result = await self._file_op(
            {"kind": "file", "op": "glob", "path": f"{normalised.rstrip('/')}/*"}
        )
        if result.get("status") == "ok":
            return {"path": normalised, "entries": result.get("matches", [])}
        return {"error": result.get("error", "list_directory failed")}

    async def _file_regenerate_index(
        self, *, path: str, output_path: str, entry_template: str, header: str,
    ) -> dict:
        """Regenerate an index file through op_runtime.

        Returns: {"path": path, "output_path": output_path, "entries": n} or {"error": ...}.
        """
        result = await self._file_op({
            "kind": "file", "op": "regenerate_index",
            "path": path,
            "output_path": output_path,
            "entry_template": entry_template,
            "header": header,
        })
        if result.get("status") == "ok":
            return {
                "path": path,
                "output_path": output_path,
                "entries": result.get("entries", 0),
            }
        return {"error": result.get("error", "regenerate_index failed")}

    # NOTE: ``_run_skill_awaitable`` moved to
    # SkillRunner.run_skill_awaitable (RunSpawner wave). The
    # RouterHostAdapter wiring in __init__ now binds the method directly.

    async def _mcp_list_servers(self) -> list[dict]:
        """Returns the configured MCP server list with descriptions."""
        return self._get_mcp_servers_for_router()

    async def _mcp_list_tools(self, server: str) -> list[dict]:
        """Query the MCP server for its tools list."""
        from reyn.mcp_client import MCPClient, MCPError, expand_env

        servers = self._mcp_servers_flat()
        if not servers:
            return [{"error": "no MCP servers configured"}]
        server_cfg = servers.get(server)
        if not server_cfg:
            return [{"error": f"MCP server {server!r} not configured"}]

        expanded = expand_env(server_cfg)
        if not isinstance(expanded, dict):
            return [{"error": f"MCP server {server!r} config must be a dict"}]
        if "type" not in expanded and expanded.get("url"):
            expanded = {**expanded, "type": "http"}

        try:
            client = MCPClient(expanded, agent_id=self._agent_id)
            tools = await client.list_tools()
            await client.close()
            return tools
        except MCPError as exc:
            return [{"error": str(exc)}]
        except Exception as exc:
            return [{"error": str(exc)}]

    async def _mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        """Invoke an MCP tool and return its result."""
        from reyn.op_runtime import execute_op
        from reyn.permissions.permissions import PermissionDecl
        from reyn.schemas.models import MCPIROp

        op = MCPIROp(kind="mcp", server=server, tool=tool, args=args)
        ctx = self._make_router_op_context()
        # MCP handler requires intervention_bus; wire the session's bus
        ctx.intervention_bus = ChatInterventionBus(self, run_id=None, skill_name="chat_router")
        # Narrow mcp scope to just this server while preserving file perms from the
        # populated decl. PermissionDecl.mcp must include the server for require_mcp to pass.
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        return await execute_op(op, ctx, caller="control_ir")

    # NOTE: ``spawn_plan_task`` and ``_spawn_resumed_plan`` moved to
    # PlanRunner.spawn_plan_task / spawn_resumed_plan (RunSpawner wave).
    # RouterHostAdapter binds the method reference; registry.py calls
    # ``session._plan_runner.spawn_resumed_plan(...)`` directly.

    # --- RouterLoop orchestration ---

    def _build_history_for_router(self) -> list[dict]:
        """Slice self.history into OpenAI-style messages for RouterLoop.

        Mirrors the head/tail compaction config so the LLM sees the same
        context window the old skill_router preprocessor produced.
        Returns [{role: 'user'|'assistant', content: str}, ...] ordered
        chronologically. The system prompt is prepended by RouterLoop itself.

        Only user/agent conversational turns are included. The compaction
        head_size + tail_size governs which turns to keep.

        Slicing correctness: when ``len(turns) <= head_size + tail_size``,
        ``head`` and ``tail`` overlap (= the same turns appear at both
        slice ends). Concatenating ``head + tail`` in that regime
        produces a fully-duplicated history — observed via dogfood
        trace v6 as the ROOT CAUSE of the Q4 ``list_skills`` empty-stop
        attractor (= the LLM saw the same user query twice with the
        history reset between them, got confused, exited silently).
        Pre-fix had no overlap guard; this branch returns the full
        ``turns`` unchanged when no slicing is needed and only takes
        the head+tail (with optional summary bridge) when the history
        actually exceeds the window.
        """
        cfg = self._compaction
        turns = [m for m in self.history if m.role in ("user", "agent")]

        if len(turns) <= cfg.head_size + cfg.tail_size:
            # No compaction needed — head+tail would overlap and duplicate.
            selected = turns
        else:
            # Head+tail with optional summary bridge for the elided middle.
            head = turns[:cfg.head_size]
            tail = turns[-cfg.tail_size:] if cfg.tail_size else []
            summary = self._latest_summary()
            if summary:
                bridge = [ChatMessage(
                    role="agent",
                    text=f"[summary of earlier conversation]\n{summary.text}",
                    ts=summary.ts,
                )]
                selected = head + bridge + tail
            else:
                selected = head + tail

        messages: list[dict] = []
        for m in selected:
            role = "user" if m.role == "user" else "assistant"
            messages.append({"role": role, "content": m.text})
        return messages

    async def _run_router_loop(
        self,
        user_text: str,
        chain_id: str,
    ) -> None:
        """Run RouterLoop for one user utterance. Enforces the per-turn cap,
        builds history, and calls RouterLoop.run(). Does NOT modify history
        or outbox directly — RouterLoop calls host callbacks.

        Raises RouterCapExceeded when the per-turn cap is reached.
        """
        # FP-0005: now async (consults safety.on_limit on hit).
        await self._check_and_increment_router_cap(user_text)
        from reyn.chat.router_loop import RouterLoop
        loop = RouterLoop(
            host=self._router_host, chain_id=chain_id, max_iterations=5,
            budget=self._budget_tracker,
        )
        history = self._build_history_for_router()
        router_usage = await loop.run(user_text=user_text, history=history)

        # F4 Bug 2 / F4 Bug 1: accumulate router LLM usage (with proxy-prefix
        # stripping) into per-session totals via the gateway.
        if router_usage is not None:
            self._budget.add_router_usage(
                usage=router_usage,
                resolver=self._resolver,
                router_model_name=loop.router_model,
            )
