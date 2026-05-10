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
from reyn.chat.outbox import OutboxMessage
from reyn.chat.services import (
    BudgetGateway,
    ChainManager,
    InterventionRegistry,
    MemoryService,
    RouterHostAdapter,
    SnapshotJournal,
)
from reyn.chat.services.chain_manager import _PendingChain
from reyn.compiler import load_dsl_skill
from reyn.compiler.parser import _split_frontmatter
from reyn.config import EventsConfig, OnLimitConfig, SafetyConfig
from reyn.safety.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.event_store import EventStore
from reyn.events.events import EventLog
from reyn.events.state_log import StateLog
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.skill.skill_paths import SkillNotFoundError, resolve_skill_path, stdlib_root
from reyn.skill.skill_registry import SkillRegistry
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    InterventionChoice,
    UserIntervention,
    match_choice,
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


class ChatInterventionBus:
    """InterventionBus impl that routes through ChatSession's outbox/inbox.

    One instance per skill spawn — captures `run_id` and a default `skill_name`
    so the chat session can drop pending interventions when the spawn is
    cancelled. Interventions emitted by ops carry their own `skill_name` from
    `OpContext`; this bus only fills in `run_id` (which the OS layer doesn't
    have, since chat tracks runs separately from `Agent.run_id`).
    """

    def __init__(self, session: "ChatSession", run_id: str | None, skill_name: str | None) -> None:
        self._session = session
        self._run_id = run_id
        self._skill_name = skill_name

    async def request(self, iv: "UserIntervention") -> "InterventionAnswer":
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
    """
    out = {"intervention_id": iv.id, "intervention_kind": iv.kind}
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
                break

        return {"input_artifact": input_artifact, "input_fields": input_fields}
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
        self._compacting = False
        self._compaction_task: asyncio.Task | None = None

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
        self._chat_events = EventLog(subscribers=[self._event_store])

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

        self.running_skills: dict[str, asyncio.Task] = {}
        # Per-run wall-clock start (monotonic) for `:list` elapsed-seconds display.
        self.running_skills_started_at: dict[str, float] = {}
        # R-D14: per-run chain_id tracking. When a skill_run was spawned to
        # process an agent_request (or any chain-tagged invocation), the
        # chain_id is recorded here so /skill discard can notify the
        # upstream waiting agent without having to wait for chain_timeout.
        # ``None`` value means the run is not chain-tagged (e.g. user-
        # initiated invocations that don't participate in a chain).
        self.running_skills_chain: dict[str, str | None] = {}

        # ADR-0023 Phase 2 step 7d: per-plan resume task tracking.
        # Populated by ``_spawn_resumed_plan`` after restart cleanup;
        # tasks are awaited at shutdown like running_skills.
        self.running_plans: dict[str, asyncio.Task] = {}

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
            run_skill_awaitable=self._run_skill_awaitable,
            spawn_skill=self._spawn_skill_for_router,
            send_to_agent=self._send_to_agent,
            put_outbox=self._put_outbox,
            append_history=self._append_history,
            spawn_plan_task=self.spawn_plan_task,
            delegation_tracker=lambda: self._router_loop_delegations,
            agent_replies_tracker=lambda: self._router_loop_agent_replies,
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

    async def run(self) -> None:
        self._chat_events.emit("chat_started", agent_name=self.agent_name, model=self.model)

        try:
            while True:
                kind, payload = await self._consume_inbox()
                if kind == "shutdown":
                    break
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
        finally:
            await self._drain_on_shutdown()
            self._chat_events.emit("chat_stopped", agent_name=self.agent_name)
            await self._put_outbox(OutboxMessage(kind="__end__", text=""))

    async def _drain_on_shutdown(self) -> None:
        """Cancel any in-flight user-initiated skill runs and await compaction.

        Memory writes happen inline during each router turn, so there is no
        background extraction to drain — shutdown is now strictly a teardown
        of whatever the user explicitly launched, plus a final await on the
        compaction task (if any) so the summary entry gets persisted before
        the process exits.
        """
        for task in self.running_skills.values():
            task.cancel()
        if self.running_skills:
            await asyncio.gather(*self.running_skills.values(), return_exceptions=True)

        # PR18: cancel any pending chain-timeout watchdogs so they don't keep
        # the loop alive past shutdown. Late-firing timers swallow their work
        # (the pending entry is gone) but cancellation is cleaner.
        # PR-refactor-session-1 wave 2: cancellation delegated to ChainManager.
        await self._chains.shutdown()

        if self._compaction_task is not None and not self._compaction_task.done():
            try:
                await self._compaction_task
            except Exception as exc:
                logger.warning("compaction task failed during shutdown: %s", exc)
                self._chat_events.emit("compaction_failed", error=str(exc), phase="shutdown")

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

        try:
            await self._run_router_loop(text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed: {exc}",
                meta={"chain_id": chain_id},
            ))
            return

        # Fire-and-forget compaction check after the user has the reply.
        # Reuses self._compacting as a single-flight lock; no await here so
        # the user's next prompt isn't blocked. _drain_on_shutdown awaits any
        # in-flight compaction task so a quick /quit after a heavy turn does
        # not lose the summary.
        if self._compaction_task is None or self._compaction_task.done():
            self._compaction_task = asyncio.create_task(self._maybe_compact())

    # ── skill invocation helpers ────────────────────────────────────────────────

    async def _auto_resume_active_skills(
        self,
        *,
        coordinator: "SkillResumeCoordinator | None" = None,
        config: "SkillResumeConfig | None" = None,
        launcher: "Callable[[Any], Awaitable[None]] | None" = None,
    ) -> list:
        """Discover active skill runs, apply resume policy, launch tasks.

        Headline UX of PR-resume-auto: after restore_state rehydrates
        the agent's snapshot + WAL, this auto-launches resume tasks for
        every still-active skill_run with no interactive prompt.

        Algorithm:
          1. Discover active runs via SkillResumeCoordinator
             (per-skill SkillSnapshot files + WAL)
          2. For each, build a ResumePlan and apply the operator's
             ``reyn.yaml`` policy (default = retry)
          3. ``discard`` decisions: call SkillRegistry.complete +
             drop pending interventions (no task launched)
          4. All other decisions: invoke ``launcher(decision)`` so
             the caller (production = ``self._spawn_resumed_skill``)
             can wire the actual asyncio task

        ``launcher`` is dependency-injected so tests can inspect
        decisions without launching real skill runtimes. Production
        callers pass None to use the default launcher.

        Returns the list of decisions that were launched (= decisions
        minus discards).
        """
        from reyn.config import SkillResumeConfig as _Config
        from reyn.skill.skill_resume_coordinator import (
            SkillResumeCoordinator as _Coord,
        )
        coord = coordinator or _Coord()
        cfg = config or _Config()
        registry = self._get_skill_registry()
        if registry is None or self._state_log is None:
            return []
        decisions = coord.discover_and_decide(
            skill_registry=registry,
            state_log=self._state_log,
            policy=cfg,
        )
        if not decisions:
            return []
        remaining = await coord.apply_decisions(
            decisions, skill_registry=registry,
            drop_interventions_for_run=self._drop_interventions_for_run,
        )
        actual_launcher = launcher or self._spawn_resumed_skill
        for decision in remaining:
            await actual_launcher(decision)
        return remaining

    async def _spawn_resumed_skill(self, decision: "Any") -> None:
        """Default launcher used by ``_auto_resume_active_skills``.

        Loads the skill by name from the resume plan, builds an Agent,
        and spawns ``Agent.run`` as a tracked asyncio task with the
        resume_plan threaded in. Exists as a separate method so the
        auto-resume hook can be tested with a stub launcher (see
        ``tests/test_session_auto_resume.py``).
        """
        plan = decision.plan
        skill_name = plan.skill_name
        run_id = plan.run_id
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
            skill = load_dsl_skill(
                str(skill_dir / "skill.md"), skill_root=str(skill_root),
            )
        except (SkillNotFoundError, Exception) as exc:
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"resume failed to load: {exc}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"resume failed: {exc}", meta=meta,
            ))
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )

        async def _runner():
            try:
                await agent.run(
                    skill, plan.skill_input,
                    output_language=self.output_language,
                    skill_registry=self._get_skill_registry(),
                    state_log=self._state_log,
                    resume_plan=plan,
                    run_id=run_id,
                )
            except asyncio.CancelledError:
                await self._put_outbox(OutboxMessage(
                    kind="status", text="cancelled", meta=meta,
                ))
                raise
            except Exception as exc:  # noqa: BLE001 — surface to outbox
                self._chat_events.emit(
                    "skill_run_failed", run_id=run_id, skill=skill_name,
                    error=str(exc),
                )
                await self._put_outbox(OutboxMessage(
                    kind="error", text=f"resume failed: {exc}", meta=meta,
                ))

        self.running_skills_started_at[run_id] = time.monotonic()
        # R-D14: resumed skill_run is generally not chain-tagged (the
        # original chain has long-since either completed or been wedged
        # by the timeout watchdog). If a future re-issue path needs to
        # carry chain_id across resume, plumb it through ``decision``.
        self.running_skills_chain[run_id] = None
        await self._put_outbox(OutboxMessage(
            kind="status", text="resuming…", meta=meta,
        ))
        task = asyncio.create_task(_runner())
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self.running_skills_chain.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

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
        """Load a stdlib skill, build an Agent under workspace/<state_subdir>, run it.

        When `forward_events` is True, phase_started/phase_completed events
        from this run are surfaced as `trace` messages on the chat outbox so
        the user sees progress between LLM hops. Off by default to keep
        memory/admin runs silent unless the caller opts in.

        Returns the RunResult. Callers handle exceptions.
        """
        skill = self._load_stdlib_skill(skill_name)
        subscribers = None
        if forward_events:
            from reyn.chat.forwarder import ChatEventForwarder
            subscribers = [ChatEventForwarder(skill_name, self.outbox)]
        # Inline stdlib runs (router/compactor) aren't tracked in running_skills,
        # so run_id is None — _drop_interventions_for_run won't fire on them
        # (they complete on their own, no cancellation path).
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id=None, skill_name=skill_name),
            mcp_servers=mcp_servers,
            subscribers=subscribers,
        )
        result = await agent.run(
            skill, input_artifact,
            output_language=self.output_language,
        )
        self._accumulate(result)
        return result

    # ── compaction (Head/Body/Tail) ─────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Cheap chars/4 token estimate. Same heuristic used by other Reyn paths."""
        return max(1, len(text or "") // 4)

    def _latest_summary(self) -> ChatMessage | None:
        for m in reversed(self.history):
            if m.role == "summary":
                return m
        return None

    async def _maybe_compact(self) -> None:
        """Fold the uncovered middle into a structured summary when token-heavy.

        Trigger: estimated tokens of user/agent turns whose seq is BOTH
          - > head_size (those are HEAD, never compacted)
          - > latest_summary.covers_through_seq (already covered)
          - <= max_seq - tail_size (TAIL is preserved as raw)
        exceeds compaction.trigger_total_tokens and contains at least
        `min_compact_batch` turns.
        """
        if self._compacting:
            self._chat_events.emit("compaction_check", outcome="already_running")
            return
        cfg = self._compaction
        turns = [m for m in self.history if m.role in ("user", "agent")]
        if len(turns) <= cfg.head_size + cfg.tail_size:
            self._chat_events.emit(
                "compaction_check", outcome="too_few_turns",
                turns=len(turns), head=cfg.head_size, tail=cfg.tail_size,
            )
            return

        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        cover_floor = max(prev_cover, cfg.head_size)

        max_seq = max((t.seq for t in turns), default=0)
        tail_threshold = max_seq - cfg.tail_size
        candidates = [t for t in turns if cover_floor < t.seq <= tail_threshold]
        if len(candidates) < cfg.min_compact_batch:
            self._chat_events.emit(
                "compaction_check", outcome="below_min_batch",
                candidate_count=len(candidates), min_batch=cfg.min_compact_batch,
            )
            return

        total_tokens = sum(self._estimate_tokens(t.text) for t in candidates)
        if total_tokens < cfg.trigger_total_tokens:
            self._chat_events.emit(
                "compaction_check", outcome="below_threshold",
                total_tokens=total_tokens, threshold=cfg.trigger_total_tokens,
                candidate_count=len(candidates),
            )
            return
        self._chat_events.emit(
            "compaction_check", outcome="triggering",
            total_tokens=total_tokens, candidate_count=len(candidates),
        )

        self._compacting = True
        try:
            await self._run_compaction(candidates, latest)
        except Exception as exc:
            self._chat_events.emit("compaction_failed", error=str(exc))
        finally:
            self._compacting = False

    async def _run_compaction(
        self,
        candidates: list[ChatMessage],
        previous_summary: ChatMessage | None,
    ) -> None:
        """Invoke chat_compactor and persist the resulting summary entry."""
        cfg = self._compaction
        prev_structured: dict | None = None
        if previous_summary is not None:
            meta = previous_summary.meta or {}
            structured = meta.get("structured")
            if isinstance(structured, dict):
                prev_structured = structured
                # carry forward the prior covers_through_seq for continuity
                if "covers_through_seq" not in prev_structured:
                    prev_structured = {
                        **prev_structured,
                        "covers_through_seq": meta.get("covers_through_seq", 0),
                    }

        input_artifact = {
            "type": "history_chunk_to_compact",
            "data": {
                "previous_summary": prev_structured,
                "new_turns": [
                    {"role": t.role, "text": t.text, "seq": t.seq} for t in candidates
                ],
                "section_token_caps": {
                    "topic_arc": cfg.section_token_caps.topic_arc,
                    "decisions": cfg.section_token_caps.decisions,
                    "pending": cfg.section_token_caps.pending,
                    "session_user_facts": cfg.section_token_caps.session_user_facts,
                    "artifacts_referenced": cfg.section_token_caps.artifacts_referenced,
                },
            },
        }

        self._chat_events.emit(
            "compaction_started",
            new_turn_count=len(candidates),
            covers_through_seq=candidates[-1].seq,
            had_previous=previous_summary is not None,
        )
        result = await self._run_stdlib_skill(
            "chat_compactor", input_artifact, state_subdir="compaction",
        )
        if not result.ok:
            self._chat_events.emit(
                "compaction_aborted", reason=f"compactor result status={result.status}",
            )
            return

        structured = dict(result.data or {})
        covers = int(structured.get("covers_through_seq") or candidates[-1].seq)
        # Render once for the persisted text field; the slicer can re-render
        # from `structured` if the stored text drifts from formatting changes.
        rendered = _render_summary_for_storage(structured)

        summary_msg = ChatMessage(
            role="summary",
            text=rendered,
            ts=_now_iso(),
            meta={"structured": structured, "covers_through_seq": covers},
        )
        self._append_history(summary_msg)
        self._chat_events.emit(
            "compaction_completed",
            covers_through_seq=covers,
            section_lengths={k: len(v) if isinstance(v, list) else len(str(v))
                             for k, v in structured.items() if k != "covers_through_seq"},
        )

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

    # ── intervention routing ─────────────────────────────────────────────────────

    async def _maybe_answer_oldest_intervention(self, text: str) -> bool:
        """If any intervention is pending, deliver `text` to the oldest and
        return True. Stale heads are evicted by the registry on `head()`."""
        head = self._interventions.head()
        if head is None:
            return False
        return await self._deliver_answer_to(head, text)

    async def _deliver_answer_to(self, iv: UserIntervention, text: str) -> bool:
        """Resolve `iv` with `text`, append a user-history entry, emit the
        `user_answered_intervention` event.

        Wraps `InterventionRegistry.deliver_answer` with the session-level
        side effects (history + audit event + unknown-choice hint). Returns
        True when the user input was consumed (answer set OR unrecognized
        choice hint emitted, both of which suppress a fresh router turn).
        """
        if iv.future.done():
            return False
        resolved = await self._interventions.deliver_answer(iv, text)
        if not resolved and iv.choices:
            # No-match path: surface hint, but consume the input so the
            # router doesn't run on a stray hotkey-attempt.
            hint = " / ".join(c.label for c in iv.choices)
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"unknown choice; expected one of: {hint}",
                meta=_iv_meta(iv),
            ))
            return True
        if not resolved:
            return False
        # Successfully resolved: append history + emit audit event.
        choice = match_choice(text, iv.choices) if iv.choices else None
        self._append_history(ChatMessage(
            role="user", text=text, ts=_now_iso(),
            meta={
                "answered_skill": iv.skill_name or "",
                "answered_run_id": iv.run_id or "",
                "intervention_id": iv.id,
                "intervention_kind": iv.kind,
            },
        ))
        self._chat_events.emit(
            "user_answered_intervention",
            intervention_id=iv.id,
            kind=iv.kind,
            run_id=iv.run_id,
            skill=iv.skill_name,
            choice_id=choice.id if choice else None,
            answer_text=text if not iv.choices else "",
        )
        return True

    async def _announce_intervention(self, iv: UserIntervention) -> None:
        """Format and publish an intervention to the outbox for the renderer.

        Skill / run_id provenance lives in `meta` — the renderer prepends a
        `[skill#abcd]` tag, so we don't repeat it in `text`.
        """
        lines: list[str] = []
        if iv.kind == "ask_user":
            lines.append(f"Question: {iv.prompt}")
        else:
            lines.append(iv.prompt)
        if iv.detail:
            lines.append(f"  {iv.detail}")
        if iv.suggestions:
            lines.append(f"  options: {' / '.join(iv.suggestions)}")
        if iv.choices:
            labels = " / ".join(c.label for c in iv.choices)
            lines.append(f"  {labels}")
        await self._put_outbox(OutboxMessage(
            kind="intervention",
            text="\n".join(lines),
            meta=_iv_meta(iv),
        ))

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Register an intervention via the registry. Emits a "queued" status
        when the registry already has pending entries — the registry itself
        only auto-announces the head intervention.

        Wraps `InterventionRegistry.dispatch` with the session-level
        "awaiting answer (N queued)" UX hint and the WAL persistence step
        (PR-intervention-link L3) so a crash mid-await leaves the dispatch
        on disk for resume to re-enqueue.
        """
        # Persist BEFORE awaiting so a crash mid-await leaves the WAL
        # with the dispatch event. UserIntervention.to_dict excludes the
        # volatile future field.
        await self._journal.record_intervention_dispatched(
            intervention_id=iv.id, iv_dict=iv.to_dict(),
        )
        # Pre-emit the queued-status hint when this iv won't be the head.
        if not self._interventions.is_empty():
            queued = self._interventions.queued_count()
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"awaiting answer ({queued} queued)",
                meta=_iv_meta(iv),
            ))
        try:
            return await self._interventions.dispatch(iv)
        finally:
            # Resolve event covers all exit paths (answered, cancelled,
            # task abort). Idempotent in the journal so duplicate cleanup
            # via _drop_interventions_for_run is safe.
            await self._journal.record_intervention_resolved(
                intervention_id=iv.id,
            )

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

    async def _send_to_agent(
        self, *, to: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Route a delegation request from this agent to `to`.

        depth is the hop count from the original user request (user → A = 1,
        A → B = 2, ...). Refused when depth > max_hop_depth (LangGraph-style
        guard, default 3). chain_id (PR14) identifies the logical request
        thread for cross-agent trace; it propagates verbatim to the target's
        inbox payload and is recorded in history meta + events.
        """
        # FP-0005: extension granted by safety-limit checkpoint raises
        # the effective hop cap for this chain.
        effective_max_hops = int(
            self._max_hop_depth
            + self._safety_extensions.get(f"max_agent_hops:{chain_id}", 0.0)
        )
        if depth > effective_max_hops:
            # FP-0005: ask before refusing when on_limit.mode opts in.
            decision = await self._handle_chat_limit_checkpoint(
                kind=f"max_agent_hops:{chain_id}",
                prompt=(
                    f"Delegation depth {depth} exceeds max_agent_hops "
                    f"({effective_max_hops}). Allow chain {chain_id} to "
                    f"continue?"
                ),
                detail=f"to={to} depth={depth} cap={effective_max_hops}",
                extension_amount=1.0,
                run_id=chain_id,
            )
            if not decision.allow_continue:
                # FP-0004: hint at the config key the operator can raise.
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=(
                        f"agent message depth {depth} exceeds limit "
                        f"{effective_max_hops}; chain refused. "
                        f"→ Raise safety.loop.max_agent_hops to allow deeper "
                        f"delegation chains."
                    ),
                    meta={"chain_id": chain_id},
                ))
                self._chat_events.emit(
                    "agent_message_refused",
                    reason="max_hop_depth",
                    to_agent=to, depth=depth, chain_id=chain_id,
                )
                return
            # Approved — continue. ``_safety_extensions[max_agent_hops:<chain_id>]``
            # was bumped by the checkpoint helper so re-entry on the same
            # chain at this depth would not re-prompt unless depth grows again.
        if to == self.agent_name:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r}: cannot self-message",
                meta={"chain_id": chain_id},
            ))
            return
        if self._registry is None or not self._registry.exists(to):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"agent {to!r} not found",
                meta={"chain_id": chain_id},
            ))
            return
        # PR12: topology gate. Defense in depth alongside the
        # `iter_reachable_agents` filter that hides unreachable agents from
        # the router LLM in the first place.
        if not self._registry.permit(self.agent_name, to):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"agent {to!r}: blocked by topology rules",
                meta={"chain_id": chain_id},
            ))
            return

        # Boot the target session if not yet loaded so its session.run() is
        # ready to consume the inbox put. attach() handles task creation
        # idempotently.
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)

        # Sender-side audit: A's history records the delegation outgoing.
        self._append_history(ChatMessage(
            role="agent", text=request, ts=_now_iso(),
            meta={
                "source": "agent_request_outgoing",
                "to_agent": to, "depth": depth, "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_message_sent",
            kind="agent_request",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        await target.submit_agent_request(
            from_agent=self.agent_name, request=request,
            depth=depth, chain_id=chain_id,
        )

    async def _send_agent_response(
        self, *, to: str, response: str, depth: int, chain_id: str,
    ) -> None:
        """Route a reply from this agent back to the requester `to`.

        depth is propagated from the original request (B replying to A's
        depth-1 request stays at depth 1; A's next hop will increment).
        Empty response is still sent so chains never silently stall.
        chain_id (PR14) carries the same value the original request did so
        the requester can correlate the reply with its pending chain.
        """
        if depth > self._max_hop_depth:
            return  # silently drop — sender already gave up the chain
        if self._registry is None or not self._registry.exists(to):
            return
        target = self._registry.get_or_load(to)
        await self._registry.ensure_running(to)
        self._chat_events.emit(
            "agent_message_sent",
            kind="agent_response",
            from_agent=self.agent_name, to_agent=to,
            depth=depth, chain_id=chain_id,
        )
        await target.submit_agent_response(
            from_agent=self.agent_name, response=response,
            depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_request(self, payload: dict) -> None:
        """Process an incoming agent_request.

        PR14 deferred-reply path: if the router emits `messages_to_agents`
        (= this agent wants to consult others before answering), the reply
        to the requester is held back. A `_PendingChain` entry is created
        keyed by chain_id; when every delegated agent has responded, the
        router runs again with all replies in history and the synthesized
        reply_text is finally sent upstream.

        If no delegations are emitted, behavior matches PR11: send the
        router's reply_text (or empty) right back to the requester.
        """
        from_agent = payload.get("from_agent", "")
        request = payload.get("request", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        # Receiver-side audit
        self._append_history(ChatMessage(
            role="user", text=request, ts=_now_iso(),
            meta={
                "source": "agent_request",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_request_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        # Reset the per-turn router cap counter — an inbound agent_request
        # is a fresh top-level entry into this agent's loop.
        self._reset_router_turn_counter()

        # Arm delegation and reply capture before running RouterLoop.
        self._router_loop_delegations = []
        self._router_loop_agent_replies = []
        try:
            await self._run_router_loop(request, chain_id)
        except RouterCapExceeded as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                    f"for incoming agent_request from {from_agent!r}. "
                    f"Last reason: {exc.last_reason or '(none)'}."
                ),
                meta={"chain_id": chain_id, "from_agent": from_agent},
            ))
            # F6/F7 fix: send a structured failure marker (not "") so the
            # upstream LLM doesn't mistake silence for "in-progress" and
            # retry in a tight loop.
            await self._send_agent_response(
                to=from_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router retry budget exhausted ({exc.count}/{exc.cap})",
                ),
                depth=depth, chain_id=chain_id,
            )
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_request): {exc}",
                meta={"chain_id": chain_id},
            ))
            # F6/F7 fix: send a structured failure marker so the requester
            # chain receives a clear "no reply produced" instead of an
            # ambiguous empty string.
            await self._send_agent_response(
                to=from_agent,
                response=_no_reply_marker(
                    self.agent_name, f"router error: {exc}"
                ),
                depth=depth, chain_id=chain_id,
            )
            return
        finally:
            dispatched = list(self._router_loop_delegations or [])
            agent_replies = list(self._router_loop_agent_replies or [])
            self._router_loop_delegations = None
            self._router_loop_agent_replies = None

        if dispatched:
            # PR14 deferred path: RouterLoop called send_to_agent for one or
            # more peers. Register a pending chain so the reply is held until
            # all delegates respond. ChainManager persists via the journal +
            # arms the timeout watchdog.
            waiting_on = {d["to"] for d in dispatched}
            await self._chains.register(
                chain_id=chain_id,
                from_user=False,
                depth=depth,
                original_text=request,
                sender=from_agent,
                waiting_on=waiting_on,
                origin_agent=from_agent,
                origin_depth=depth,
            )
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        # PR11-compatible single-hop reply path. RouterLoop emitted reply_text
        # via put_outbox → captured in agent_replies. Forward upstream.
        # F6/F7 fix: when no clean text reply was captured (max_iterations,
        # empty content, async-only dispatch with no follow-up text), send
        # a structured marker rather than "" so the upstream LLM doesn't
        # interpret silence as "in-progress" and re-delegate.
        if agent_replies:
            reply_text = agent_replies[0]
        else:
            reply_text = _no_reply_marker(
                self.agent_name,
                "router completed without producing a text reply",
            )
        # Note: history was already appended by put_outbox; add routing meta.
        await self._send_agent_response(
            to=from_agent, response=reply_text, depth=depth, chain_id=chain_id,
        )

    async def _handle_agent_response(self, payload: dict) -> None:
        """Process an incoming agent_response.

        Two branches:
        - chain_id ∈ self._chains → multi-hop relay. Drop sender
          from waiting_on; when waiting_on becomes empty, re-invoke router
          and forward the synthesized reply (or fresh delegations) on the
          same chain. Reply goes to the chain's `origin_agent`, NOT
          `from_agent`.
        - chain_id ∉ self._chains → user-initiated chain (PR11
          compatibility). The router's reply_text is treated as a
          user-facing message (outbox + history); further delegations
          continue with depth+1 on the same chain_id.
        """
        from_agent = payload.get("from_agent", "")
        response = payload.get("response", "")
        depth = int(payload.get("depth", 1))
        chain_id = payload.get("chain_id") or _new_chain_id()

        self._append_history(ChatMessage(
            role="user", text=response, ts=_now_iso(),
            meta={
                "source": "agent_response",
                "from_agent": from_agent, "depth": depth,
                "chain_id": chain_id,
            },
        ))
        self._chat_events.emit(
            "agent_response_received",
            from_agent=from_agent, depth=depth, chain_id=chain_id,
        )

        pending = self._chains.get(chain_id)
        if pending is not None:
            await self._resolve_pending_chain(
                pending, from_agent=from_agent, last_response=response,
            )
            return

        # User-initiated chain: PR11 path, reply goes to user.

        # B2-H2 fix: if the peer's reply is a no-reply marker, bypass the LLM
        # entirely and surface the failure deterministically. Without this,
        # gemini-2.5-flash-lite interprets the marker as a polite conversational
        # reply (e.g. "かしこまりました") and the user never learns that the peer
        # failed.
        if _is_no_reply_marker(response):
            parsed = _parse_no_reply_marker(response)
            peer = parsed[0] if parsed else from_agent or "<unknown>"
            reason = parsed[1] if parsed else "no reply produced"
            msg_template = _PEER_REPLY_FAILED_MSG.get(
                self.output_language or "en", _PEER_REPLY_FAILED_MSG["en"],
            )
            user_text = msg_template.format(peer=peer, reason=reason)
            await self._put_outbox(OutboxMessage(
                kind="agent",
                text=user_text,
                meta={"chain_id": chain_id, "peer_failure": True},
            ))
            self._chat_events.emit(
                "peer_reply_failed_surfaced",
                chain_id=chain_id,
                peer=peer,
                reason=reason,
            )
            return

        try:
            await self._run_router_loop(response, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"router failed (agent_response): {exc}",
                meta={"chain_id": chain_id},
            ))
            return

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
            try:
                await asyncio.wait_for(
                    self._handle_skill_completed(payload),
                    timeout=remaining,
                )
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

    async def _resolve_pending_chain(
        self, pending: "_PendingChain", *, from_agent: str, last_response: str = "",
    ) -> None:
        """Drive a multi-hop pending chain forward by one delegate response.

        Drops `from_agent` from `pending.waiting_on`. If others remain,
        no-op (the chain is still gathering replies). Otherwise, re-runs
        the router on the original request — by now every delegate's
        response is appended to history, so the LLM has all the material
        to compose a synthesized answer (or decide on more delegations,
        which keeps the chain pending).
        """
        chain_id = pending.chain_id
        pending.waiting_on.discard(from_agent)
        if pending.waiting_on:
            # Partial resolution — record the new waiting_on for recovery.
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            return  # still waiting on other delegates

        # B2-H2 fix: if the peer's reply (the last incoming response) is a
        # no-reply marker, bypass the LLM and surface the failure deterministically.
        # Without this, weak models like gemini-2.5-flash-lite interpret the marker
        # as a normal conversational reply and emit a polite close (e.g.
        # "かしこまりました") while the user never learns the peer failed.
        if _is_no_reply_marker(last_response):
            parsed = _parse_no_reply_marker(last_response)
            peer = parsed[0] if parsed else from_agent
            reason = parsed[1] if parsed else "no reply produced"
            msg_template = _PEER_REPLY_FAILED_MSG.get(
                self.output_language or "en", _PEER_REPLY_FAILED_MSG["en"],
            )
            user_text = msg_template.format(peer=peer, reason=reason)
            # Forward the failure upstream — the pending chain always has an
            # origin_agent (chains from user requests don't reach _resolve_pending_chain;
            # they are handled by the `chain_id ∉ self._chains` branch above).
            await self._send_agent_response(
                to=pending.origin_agent,
                response=user_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
            )
            self._chat_events.emit(
                "peer_reply_failed_surfaced",
                chain_id=chain_id,
                peer=peer,
                reason=reason,
                from_user=False,
            )
            await self._chains.resolve(chain_id)
            return

        # Arm delegation and reply capture for the re-run.
        self._router_loop_delegations = []
        self._router_loop_agent_replies = []
        try:
            await self._run_router_loop(pending.original_request, chain_id)
        except RouterCapExceeded as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"Router exhausted retry budget ({exc.count}/{exc.cap}) "
                    f"resolving chain {chain_id}. "
                    f"Last reason: {exc.last_reason or '(none)'}."
                ),
                meta={"chain_id": chain_id},
            ))
            # F6/F7 fix: structured marker upstream, not "".
            await self._send_agent_response(
                to=pending.origin_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router retry budget exhausted ({exc.count}/{exc.cap}) "
                    f"resolving chain",
                ),
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        except Exception as exc:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"router failed (chain resolve): {exc}",
                meta={"chain_id": chain_id},
            ))
            # F6/F7 fix: structured marker upstream, not "".
            await self._send_agent_response(
                to=pending.origin_agent,
                response=_no_reply_marker(
                    self.agent_name,
                    f"router error during chain resolve: {exc}",
                ),
                depth=pending.origin_depth, chain_id=chain_id,
            )
            await self._chains.resolve(chain_id)
            return
        finally:
            new_dispatched = list(self._router_loop_delegations or [])
            agent_replies = list(self._router_loop_agent_replies or [])
            self._router_loop_delegations = None
            self._router_loop_agent_replies = None

        if new_dispatched:
            # Continue the chain with a fresh wave of delegations.
            pending.waiting_on = {d["to"] for d in new_dispatched}
            await self._chains.update(chain_id, waiting_on=pending.waiting_on)
            # PR18: re-arm watchdog for the continued chain.
            self._chains.arm_timeout(
                chain_id, on_fire=self._on_chain_timeout_fire,
            )
            return

        # F6/F7 fix: when no clean text reply was captured during chain
        # resolve, send a structured marker upstream rather than "".
        if agent_replies:
            final_reply = agent_replies[0]
        else:
            final_reply = _no_reply_marker(
                self.agent_name,
                "chain resolved without producing a text reply",
            )
        # History already appended by put_outbox.
        await self._send_agent_response(
            to=pending.origin_agent, response=final_reply,
            depth=pending.origin_depth, chain_id=chain_id,
        )
        await self._chains.resolve(chain_id)

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
        """
        from reyn.chat.slash import REGISTRY

        body = text[1:].lstrip()
        if not body:
            known = ", ".join(f"/{n}" for n in REGISTRY.names())
            await self._put_outbox(OutboxMessage(
                kind="status",
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
                kind="status",
                text=f"unknown command /{cmd}; try: {known}",
            ))
            return True
        await slash_cmd.handler(self, args)
        return True

    async def _slash_list(self, args: str) -> None:
        """`/list` — running skills + pending interventions."""
        now = time.monotonic()
        lines: list[str] = []
        if self.running_skills:
            lines.append("running skills:")
            for rid, _task in self.running_skills.items():
                started = self.running_skills_started_at.get(rid)
                elapsed = f"{int(now - started)}s" if started is not None else "?s"
                # Recover skill_name from the run_id format
                # "TIMESTAMP_<skill>_<short>" — split between first and last underscore.
                short = _run_short(rid)
                # skill_name is everything between first '_' after timestamp and the trailing _short
                trimmed = rid[: -len(short) - 1] if short else rid  # drop "_abcd"
                # trimmed = "TIMESTAMP_skill_name"; drop the leading TIMESTAMP_
                _, _, skill_part = trimmed.partition("_")
                lines.append(f"  {short}  {skill_part:<24} {elapsed:>5}  (run_id={rid})")
        else:
            lines.append("running skills: (none)")
        active_ivs = self._interventions.list_active()
        if active_ivs:
            lines.append("pending interventions:")
            for iv in active_ivs:
                short = (iv.run_id[-4:] if iv.run_id else "----")
                lines.append(
                    f"  {iv.id[:8]}  {iv.kind:<20}  {iv.skill_name or '?'}#{short}"
                )
        await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))

    async def _slash_cancel(self, args: str) -> None:
        """`/cancel <id-prefix>` — cancel a running skill task."""
        prefix = args.strip()
        if not prefix:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /cancel <id-prefix>",
            ))
            return
        rid, candidates = self._resolve_run_id(prefix)
        if rid is None:
            if not candidates:
                await self._put_outbox(OutboxMessage(
                    kind="error", text=f"no running skill matches {prefix!r}",
                ))
            else:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"ambiguous prefix {prefix!r}; matches: {', '.join(_run_short(c) for c in candidates)}",
                ))
            return
        task = self.running_skills.get(rid)
        if task is None or task.done():
            await self._put_outbox(OutboxMessage(
                kind="status", text=f"skill {_run_short(rid)} already finished",
            ))
            return
        task.cancel()
        await self._put_outbox(OutboxMessage(
            kind="status", text="cancel requested",
            meta=_run_meta(rid, None),
        ))

    async def _slash_answer(self, args: str) -> None:
        """`/answer <id-prefix> <text>` — deliver answer to a non-head intervention."""
        parts = args.split(maxsplit=1)
        if not parts:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /answer <id-prefix> <text>",
            ))
            return
        prefix = parts[0]
        text = parts[1] if len(parts) > 1 else ""
        iid, candidates = self._resolve_intervention_id(prefix)
        if iid is None:
            if not candidates:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"no pending intervention matches {prefix!r}",
                ))
            else:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"ambiguous prefix {prefix!r}; matches: {', '.join(c[:8] for c in candidates)}",
                ))
            return
        iv = self._interventions.get(iid)
        if iv is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"intervention {prefix!r} disappeared mid-resolution",
            ))
            return
        await self._deliver_answer_to(iv, text)

    async def _slash_agents(self, args: str) -> None:
        """`/agents` — list known agents (registry-backed)."""
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; /agents only works in `reyn chat`",
            ))
            return
        names = self._registry.list_names()
        if not names:
            await self._put_outbox(OutboxMessage(
                kind="status", text="no agents (this should not happen — default auto-creates)",
            ))
            return
        attached = self._registry.attached_name
        loaded = set(self._registry.loaded_names())
        lines = ["agents:"]
        for n in names:
            try:
                profile = self._registry.load_profile(n)
                role_excerpt = (profile.role or "").strip().splitlines()
                role = role_excerpt[0] if role_excerpt else ""
            except Exception:
                role = "(profile load failed)"
            last = self._registry.last_activity_at(n)
            last_str = last.strftime("%Y-%m-%dT%H:%M") if last else "—"
            mark = "*" if n == attached else (" " if n not in loaded else "·")
            lines.append(f"  {mark} {n:<24} {last_str:<17} {role[:60]}")
        lines.append("(* = attached, · = loaded, blank = not yet loaded)")
        await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))

    async def _slash_attach(self, args: str) -> None:
        """`/attach <name>` — switch attached agent.

        The actual switch happens in repl._input_loop, which owns the display
        wiring. Here we only validate the name and put a sentinel attach
        request on this session's outbox; the REPL listens for the kind.
        """
        name = args.strip()
        if not name:
            await self._put_outbox(OutboxMessage(
                kind="error", text="usage: /attach <name>",
            ))
            return
        if self._registry is None:
            await self._put_outbox(OutboxMessage(
                kind="error",
                text="agent registry not wired; /attach only works in `reyn chat`",
            ))
            return
        if not self._registry.exists(name):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"agent {name!r} not found; create with `reyn agent new {name}`",
            ))
            return
        if name == self._registry.attached_name:
            await self._put_outbox(OutboxMessage(
                kind="status", text=f"already attached to {name!r}",
            ))
            return
        # The REPL drains its own outbox loop. Send the attach request as a
        # specially-kinded message so the input loop can recognize it.
        await self._put_outbox(OutboxMessage(
            kind="__attach_request__", text=name,
        ))

    async def _slash_cost(self, args: str) -> None:
        """`/cost` — quick token + USD line for the attached agent."""
        line = self._budget.cost_line()
        if line is None:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text="budget tracker is disabled (no `cost:` config or non-chat mode)",
            ))
            return
        await self._put_outbox(OutboxMessage(kind="status", text=line))

    async def _slash_budget(self, args: str) -> None:
        """`/budget` (full breakdown) / `/budget reset` (clear counters)."""
        sub = args.strip()
        if sub == "reset":
            before = self._budget.reset_all()
            if before is None:
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text="budget tracker is disabled (no `cost:` config or non-chat mode)",
                ))
                return
            lines = ["Budget counters reset."]
            if before.get("agent_tokens"):
                for a, t in before["agent_tokens"].items():
                    cost = before.get("agent_cost_usd", {}).get(a, 0.0)
                    lines.append(f"  per-agent ({a}) tokens:    {t:>10,} → 0")
                    lines.append(f"  per-agent ({a}) cost_usd:  ${cost:.4f} → $0.00")
            if before.get("chain_skill_calls"):
                lines.append("  per-chain skill calls:        cleared")
            if before.get("rate_window_sizes"):
                lines.append("  rate-limit window:            cleared")
            lines.append(
                "Note: daily / monthly counters are NOT reset — "
                "they auto-reset at period boundary."
            )
            lines.append("Use `/budget` to verify.")
            await self._put_outbox(OutboxMessage(kind="status", text="\n".join(lines)))
            return
        text = self._budget.budget_full()
        if text is None:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text="budget tracker is disabled (no `cost:` config or non-chat mode)",
            ))
            return
        await self._put_outbox(OutboxMessage(kind="status", text=text))

    # ── skill spawn ─────────────────────────────────────────────────────────────

    async def _spawn_skill_for_router(
        self, spec: dict, *, chain_id: str
    ) -> dict:
        """FP-0012 non-blocking router-side spawn entry point.

        Wrapper over ``_spawn_skill`` that returns the spawn-ack dict
        the LLM consumes via ``invoke_skill``'s tool_result. The actual
        skill task is created by ``_spawn_skill`` (which already does
        ``asyncio.create_task`` + populates ``running_skills`` /
        ``running_skills_started_at`` / ``running_skills_chain``); we
        capture the run_id assigned during spawn by snapshotting the
        ``running_skills`` dict before / after the call.

        Refusals (= allowlist deny / budget hard-cap) are surfaced as
        ``{"status": "error", "data": {"error": ...}}`` so the router
        LLM narrates them per the SP's anti-optimism rule (FP-0011
        Component B). The ack shape is contract-stable:

            {"status": "spawned", "run_id": "...",
             "chain_id": "...",   "note": "..."}
        """
        before = set(self.running_skills.keys())
        await self._spawn_skill(spec, chain_id=chain_id)
        after = set(self.running_skills.keys())
        new_run_ids = after - before
        if not new_run_ids:
            # _spawn_skill returned without creating a task (= refused
            # by allowlist / budget gate / invalid spec). The session
            # already surfaced an error outbox message; mirror it as
            # tool_result so the router LLM narrates the refusal text.
            return {
                "status": "error",
                "data": {
                    "error": (
                        f"skill {spec.get('skill')!r} could not be spawned "
                        f"(see prior outbox message for the specific reason)"
                    ),
                    "skill": spec.get("skill"),
                },
            }
        # In the rare case multiple tasks landed concurrently, pick the
        # one matching the requested skill name.
        run_id = next(iter(new_run_ids))
        for rid in new_run_ids:
            if rid.split("_", 2)[1:2] == [str(spec.get("skill"))]:
                run_id = rid
                break
        return {
            "status": "spawned",
            "run_id": run_id,
            "chain_id": chain_id,
            "skill": spec.get("skill"),
            "note": (
                "Running in the background. "
                "I will notify you when it completes. "
                "Use /tasks to check progress."
            ),
        }

    async def _spawn_skill(self, spec: dict, *, chain_id: str | None = None) -> None:
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"invalid skill spec: {spec}",
            ))
            return
        # PR15: defense-in-depth allowlist check. The router-side filter
        # already hides blocked skills from the LLM, so reaching this branch
        # implies hallucination or a stale routing_decision. Refuse + audit.
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=(
                    f"skill {skill_name!r} is not in allowed_skills for agent "
                    f"{self.agent_name!r}; refused"
                ),
            ))
            self._chat_events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self.agent_name,
            )
            return

        # PR22: per-chain per-skill cap check. Refuse spawn if hard limit
        # reached. Warn dimensions are emitted via events + outbox status.
        if chain_id is not None:
            check = self._budget.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            # FP-0003: opt-in user-approval flow on hard-limit hit.
            # When the cap is configured with `ask_on_exceed: true` AND
            # `extension_calls > 0`, dispatch an ask_user prompting the
            # user to extend the chain's effective cap by N additional
            # spawns. Approval grants the extension and we re-check;
            # decline / timeout falls through to the original refusal
            # path. The flag is opt-in so existing users see no change.
            if (
                not check.allowed
                and check.context.get("ask_on_exceed")
                and int(check.context.get("extension_calls") or 0) > 0
            ):
                approved = await self._ask_budget_extension(
                    chain_id=chain_id,
                    skill_name=skill_name,
                    check=check,
                )
                if approved:
                    extension = int(check.context["extension_calls"])
                    new_total = self._budget.extend_chain_calls(
                        chain_id=chain_id,
                        skill=skill_name,
                        additional=extension,
                    )
                    self._chat_events.emit(
                        "budget_extended",
                        dimension=check.hard_dimension,
                        skill=skill_name,
                        chain_id=chain_id,
                        granted=extension,
                        total_extension=new_total,
                    )
                    # Re-check after extension. Should normally allow now.
                    check = self._budget.check_pre_spawn(
                        chain_id=chain_id, skill=skill_name,
                    )
            if not check.allowed:
                self._chat_events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=format_refusal_message(check),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
                return
            for dim in check.warn_dimensions:
                self._chat_events.emit(
                    "budget_warn",
                    dimension=dim, chain_id=chain_id, skill=skill_name,
                    **check.context,
                )
                await self._put_outbox(OutboxMessage(
                    kind="status",
                    text=format_warn_message(dim, check.context),
                    meta={"chain_id": chain_id, "skill": skill_name},
                ))
            self._budget.record_spawn(chain_id=chain_id, skill=skill_name)

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)
        # Track elapsed time for `:list` and provenance for outbox messages
        self.running_skills_started_at[run_id] = time.monotonic()
        # R-D14: stash chain_id so /skill discard can notify the upstream
        # waiting agent (chain_id is None for user-initiated invocations
        # that don't participate in a chain).
        self.running_skills_chain[run_id] = chain_id
        await self._put_outbox(OutboxMessage(
            kind="status", text="starting…",
            meta=_run_meta(run_id, skill_name),
        ))

        task = asyncio.create_task(
            self._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
        )
        self.running_skills[run_id] = task

        def _cleanup(_t: asyncio.Task, rid: str = run_id) -> None:
            self.running_skills.pop(rid, None)
            self.running_skills_started_at.pop(rid, None)
            self.running_skills_chain.pop(rid, None)
            self._drop_interventions_for_run(rid)

        task.add_done_callback(_cleanup)

    async def _run_one_skill(
        self,
        run_id: str,
        skill_name: str,
        input_artifact: dict,
        *,
        chain_id: str | None = None,
    ) -> None:
        meta = _run_meta(run_id, skill_name)
        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            # P6 audit completeness: skill_run_spawned was emitted earlier; emit
            # skill_run_failed for the error path so events log captures every
            # state transition (dogfood S13b).
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"skill not found: {skill_name}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": f"skill not found: {skill_name}"},
            )
            return
        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), skill_root=str(skill_root))
        except Exception as exc:
            # P6 audit completeness: pair with skill_run_spawned above.
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed to load {skill_name}: {exc}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": f"failed to load {skill_name}: {exc}"},
            )
            return

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )
        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self.output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
            )
        except asyncio.CancelledError:
            await self._put_outbox(OutboxMessage(
                kind="status", text="cancelled", meta=meta,
            ))
            # FP-0012: do NOT enqueue skill_completed for cancellation — the
            # cancelling code path (e.g. /skill discard, shutdown drain)
            # owns the user-facing notification + cross-agent chain notify
            # via R-D14. Re-raise so asyncio task done-callback propagates.
            raise
        except Exception as exc:
            self._chat_events.emit("skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc))
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"failed: {exc}", meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="error", data={"error": str(exc)},
            )
            return

        # PR22: surface budget_exceeded result as a user-facing error.
        if result.status == "budget_exceeded":
            self._chat_events.emit(
                "skill_run_failed",
                run_id=run_id, skill=skill_name,
                error="budget_exceeded",
            )
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=result.error or "budget exceeded",
                meta=meta,
            ))
            await self._enqueue_skill_completed(
                run_id=run_id, skill=skill_name, chain_id=chain_id,
                status="budget_exceeded",
                data={"error": result.error or "budget exceeded"},
            )
            return

        self._accumulate(result)
        self._chat_events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )

        # FP-0012: enqueue completion message into inbox so the chat router
        # picks it up on the next ``run()`` iteration. The handler injects
        # a user-role message into the existing conversation thread and
        # runs one router LLM turn for narration (= replaces the synchronous
        # tool_result narration of FP-0011). The router LLM has full thread
        # context (= original spawn ack + intermediate exchanges + completion)
        # so it can correlate via ``chain_id`` and narrate accurately.
        await self._enqueue_skill_completed(
            run_id=run_id, skill=skill_name, chain_id=chain_id,
            status=result.status or "finished",
            data=result.data or {},
        )

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

    async def _run_remember(self, **kwargs) -> dict:
        """Delegates to MemoryService.remember (wave 3 PR2)."""
        return await self._memory.remember(**kwargs)

    async def _read_memory_body(self, **kwargs) -> dict:
        """Delegates to MemoryService.read_body (wave 3 PR2)."""
        return await self._memory.read_body(**kwargs)

    async def _run_forget(self, **kwargs) -> dict:
        """Delegates to MemoryService.forget (wave 3 PR2)."""
        return await self._memory.forget(**kwargs)

    async def _run_skill_awaitable(self, spec: dict, *, chain_id: str) -> dict:
        """Awaitable variant of _spawn_skill. Runs a single skill, awaits its
        completion, returns the final_output dict.

        spec format: {"skill": <name>, "input": <artifact dict>}
        Returns: {"status": "finished"|"error", "data": <final_output>}

        FP-0011: narration is the router LLM's responsibility (post-invoke_skill
        SP guidance). This method no longer pushes to outbox — the caller (= the
        invoke_skill tool dispatcher) returns the dict to the router loop, and
        the router LLM narrates from `data` on its next turn.
        """
        skill_name = spec.get("skill")
        input_artifact = spec.get("input")
        if not skill_name or not isinstance(input_artifact, dict):
            return {"status": "error", "data": {"error": f"invalid skill spec: {spec}"}}

        # PR15: allowlist check — same defense as _spawn_skill
        if (
            self._allowed_skills is not None
            and skill_name not in self._allowed_skills
        ):
            self._chat_events.emit(
                "skill_spawn_refused",
                reason="allowlist", skill=skill_name, agent=self.agent_name,
            )
            return {
                "status": "error",
                "data": {
                    "error": (
                        f"skill {skill_name!r} is not in allowed_skills for agent "
                        f"{self.agent_name!r}; refused"
                    )
                },
            }

        # PR22: budget cap check
        if self._budget_tracker is not None:
            check = self._budget_tracker.check_pre_spawn(
                chain_id=chain_id, skill=skill_name,
            )
            if not check.allowed:
                self._chat_events.emit(
                    "budget_exceeded",
                    dimension=check.hard_dimension,
                    detail=check.detail,
                    skill=skill_name,
                    chain_id=chain_id,
                )
                return {
                    "status": "error",
                    "data": {"error": check.detail or "budget exceeded"},
                }
            self._budget_tracker.record_spawn(chain_id=chain_id, skill=skill_name)

        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{skill_name}_{uuid.uuid4().hex[:4]}"
        )
        self._chat_events.emit("skill_run_spawned", run_id=run_id, skill=skill_name)

        try:
            skill_dir, skill_root = resolve_skill_path(skill_name)
        except SkillNotFoundError:
            # P6 audit completeness: skill_run_spawned was emitted above; we must
            # emit a corresponding skill_run_failed for the error path so the
            # event log records every state transition (dogfood S13b).
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"skill not found: {skill_name}",
            )
            return {"status": "error", "data": {"error": f"skill not found: {skill_name}"}}

        try:
            skill = load_dsl_skill(str(skill_dir / "skill.md"), skill_root=str(skill_root))
        except Exception as exc:
            # P6 audit completeness: pair with skill_run_spawned above.
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name,
                error=f"failed to load: {exc}",
            )
            return {"status": "error", "data": {"error": f"failed to load {skill_name}: {exc}"}}

        from reyn.chat.forwarder import ChatEventForwarder
        agent = self._build_agent(
            intervention_bus=ChatInterventionBus(self, run_id, skill_name),
            mcp_servers=self._mcp_servers,
            subscribers=[ChatEventForwarder(skill_name, self.outbox, run_id=run_id)],
        )

        try:
            result = await agent.run(
                skill, input_artifact,
                output_language=self.output_language,
                chain_id=chain_id,
                skill_registry=self._get_skill_registry(),
                state_log=self._state_log,
            )
        except asyncio.CancelledError:
            return {"status": "error", "data": {"error": "cancelled"}}
        except Exception as exc:
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name, error=str(exc),
            )
            return {"status": "error", "data": {"error": str(exc)}}

        if result.status == "budget_exceeded":
            self._chat_events.emit(
                "skill_run_failed", run_id=run_id, skill=skill_name, error="budget_exceeded",
            )
            return {"status": "error", "data": {"error": result.error or "budget exceeded"}}

        self._accumulate(result)
        self._chat_events.emit(
            "skill_run_completed", run_id=run_id, skill=skill_name, status=result.status,
        )

        # FP-0011: skill_narrator removed — the router LLM narrates inline on
        # its post-invoke_skill turn (see router_system_prompt.py guidance).
        # The tool-result dict returned below flows back to the router loop
        # unchanged; the LLM picks user-relevant fields from `data` and surfaces
        # error fields verbatim per the strengthened anti-optimism rule.
        return {"status": result.status or "finished", "data": result.data or {}}

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
            client = MCPClient(expanded)
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
    async def spawn_plan_task(
        self, *, plan_id: str, runtime: Any, chain_id: str,
        parent_chain_id: str | None = None,
    ) -> None:
        """Run a PlanRuntime as a background task (ADR-0023 Phase 2.1).

        Tracks the task in ``self.running_plans`` for `/plan discard`
        and shutdown await. On clean exit, emits the runtime's
        aggregated text to the user outbox + cleans up the
        decomposition artifact (= P5 cleanup mirror of
        dispatch_plan_tool's old finally clause).

        Errors during runtime.run() are logged and swallowed (= the
        task is fire-and-forget; the runtime's own finally emits
        plan_run_interrupted via events for forensic visibility, and
        crash cases preserve the artifact for restart resume).
        """

        async def _run_plan_task() -> None:
            from reyn.chat.planner import _is_workflow_abort
            clean_exit = False
            result_text: str | None = None
            try:
                result = await runtime.run()
                clean_exit = True
                result_text = result.text if result is not None else None
            except BaseException as exc:
                if _is_workflow_abort(type(exc)):
                    clean_exit = True
                else:
                    logger.warning(
                        "plan task crashed for %s: %r", plan_id, exc,
                    )
            # Emit terminal aggregator text on clean exit.
            if clean_exit and result_text:
                try:
                    await self._put_outbox(OutboxMessage(
                        kind="agent",
                        text=result_text,
                        meta={
                            "plan_id": plan_id,
                            "chain_id": chain_id,
                            "source": "plan",
                        },
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan task outbox emit failed for %s: %r",
                        plan_id, exc,
                    )
                # Batch 16 / G27: also append to history so the
                # terminal text is visible to A2A reply harvesting,
                # `dogfood_trace --mode plan-trace`, and any future
                # caller iterating session.history. Mirror the regular
                # agent-reply pattern (= _put_outbox + _append_history
                # always together for kind="agent").
                #
                # The history meta uses ``parent_chain_id`` (= the
                # chat-turn / A2A caller's chain) so chain_id-filtered
                # harvest picks up this entry. ``plan_chain_id`` is
                # internal plan-mode bookkeeping (per-plan chain) —
                # tracked in meta.plan_id for forensic continuity.
                history_chain_id = parent_chain_id or chain_id
                try:
                    self._append_history(ChatMessage(
                        role="agent", text=result_text, ts=_now_iso(),
                        meta={
                            "plan_id": plan_id,
                            "chain_id": history_chain_id,
                            "plan_chain_id": chain_id,
                            "source": "plan",
                        },
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan task history append failed for %s: %r",
                        plan_id, exc,
                    )
            # Artifact cleanup mirrors the old dispatch_plan_tool finally.
            if clean_exit:
                try:
                    await self._router_host.delete_plan_decomposition(plan_id=plan_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "plan task delete_decomposition failed for %s: %r",
                        plan_id, exc,
                    )
            self.running_plans.pop(plan_id, None)

        task = asyncio.create_task(_run_plan_task())
        self.running_plans[plan_id] = task

    async def _spawn_resumed_plan(
        self,
        *,
        decision: Any,
        budget: Any = None,
        router_model: str = "light",
    ) -> None:
        """Launch a PlanRuntime for a resume decision (ADR-0023 §3.4).

        Reads the decomposition artifact for ``decision.plan.plan_id``,
        constructs ``PlanRuntime(resume_plan=decision.plan)``, and
        registers the resulting task on ``self.running_plans``. The
        task is fire-and-forget; the runtime's finally clause emits
        plan_completed / plan_run_interrupted as usual.

        Errors during decomposition load surface as outbox notices and
        the plan is marked aborted (= ADR-0023 §3.5 corruption fallback,
        even after the coordinator earlier-validated path).
        """
        from reyn.plan import (
            PlanRuntime,
            read_decomposition,
        )

        plan_id = decision.plan.plan_id
        agent_state_dir = (
            Path(".reyn") / "agents" / self.agent_name / "state"
        )
        try:
            decomposition = read_decomposition(agent_state_dir, plan_id)
        except Exception as exc:  # noqa: BLE001 — defensive, surface to user
            logger.warning(
                "_spawn_resumed_plan: cannot load decomposition for %s: %r",
                plan_id, exc,
            )
            try:
                from reyn.chat.outbox import OutboxMessage
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=(
                        "A plan-mode reply was interrupted; the saved "
                        "decomposition could not be loaded — please "
                        "re-issue your request."
                    ),
                    meta={"plan_id": plan_id, "reason": "decomposition_load_failed"},
                ))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._journal.record_plan_aborted(
                    plan_id=plan_id, reason="decomposition_load_failed",
                )
            except Exception as exc2:  # noqa: BLE001
                logger.warning("plan_aborted emit failed: %r", exc2)
            return

        runtime = PlanRuntime(
            decomposition,
            host=self._router_host,
            chain_id=decision.plan.chain_id,
            plan_id=plan_id,
            budget=budget,
            router_model=router_model,
            resume_plan=decision.plan,
        )

        async def _run_resumed_plan() -> None:
            try:
                await runtime.run()
            except Exception as exc:  # noqa: BLE001 — top-level swallow
                logger.warning(
                    "_spawn_resumed_plan task crashed for %s: %r",
                    plan_id, exc,
                )
            finally:
                self.running_plans.pop(plan_id, None)

        task = asyncio.create_task(_run_resumed_plan())
        self.running_plans[plan_id] = task

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
