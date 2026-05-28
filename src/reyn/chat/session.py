"""ChatSession — long-lived chat loop driving the skill_router stdlib skill."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Literal

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
    MultimodalConfig,
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
    InterventionChoice,
    RequestBus,
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


def _embedding_class_needs_missing_extras(
    class_name: str, embedding_config: Any,
) -> bool:
    """Whether the configured embedding class needs sentence-transformers
    extras that haven't been installed (FP-0043 Phase 4 graceful-degrade).

    The Phase 4 default flip makes ``action_retrieval.embedding_class``
    default to ``"local-mini"``. For fresh installs that don't have
    ``pip install 'reyn[local-embed]'`` yet, we don't want to instantiate
    an ActionEmbeddingIndex whose first embed() call will ImportError —
    we want ``search_actions`` to stay hidden and let list_actions
    surface the hidden-state hint pointing operators at the install
    command. Returns True iff:

      1. ``class_name`` resolves to an entry in ``embedding_config.classes``
      2. that entry's ``model`` starts with ``sentence-transformers/``
      3. ``sentence_transformers`` is NOT importable in this env

    Any other failure mode (= unknown class, malformed config) returns
    False — let the normal try/except path handle it.
    """
    try:
        from reyn.embedding.sentence_transformers_provider import (
            _PREFIX,
            is_available,
        )
        spec = embedding_config.classes.get(class_name)
        if spec is None:
            return False
        model = getattr(spec, "model", None)
        if not isinstance(model, str) or not model.startswith(_PREFIX):
            return False
        return not is_available()
    except Exception:
        return False


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


# issue #268 Phase 2 continuation: canonical channel identifier for
# chat-side interventions (= matches the listener_id that
# ``ChatTUIApp.on_mount`` registers in src/reyn/chat/tui/app.py).
# Production ChatInterventionBus instances stamp ivs with this id so
# the agent layer's origin-pin check + cross-channel observe / claim
# routing work end-to-end for TUI-initiated tasks. Module-level so
# tests can import + assert against a single source of truth.
DEFAULT_CHAT_CHANNEL_ID = "tui"


# B43-NF-W6-1: continuation directive injected when the top-level chat
# router LLM produces an empty stop after a tool round (= same attractor
# as the plan-step case PR #265 closed at the planner.py sub_loop
# construction sites). The chat router runs the full user-facing
# conversation, so the directive references "the user's question" rather
# than the plan-step's "step report". RouterLoop reads
# ``REYN_EMPTY_STOP_RETRY=1`` from the environment at runtime; this
# constant is plumbed in unconditionally but only kicks in when the env
# var is set (= same opt-in mechanic as PR #265).
#
# Trace-patch-replay verified (= B43 W6-S2 top-level router post-plan
# narration call): baseline N=10 = 6/10 EMPTY_STOP (60% trigger rate),
# patched N=10 = 0/10 empty + 10/10 substantive (169-1485 chars).
# Cross-provider documented attractor — Anthropic ``handling-stop-
# reasons`` docs explicitly recommend continuation prompts "as a last
# resort". Lead-coder review heuristic applied: this wires the third
# (and last) RouterLoop construction site — planner.py has the other
# two for plan-step sub_loops.
_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE = (
    "Now write your reply to the user. Summarise the relevant content "
    "from the tool result above and address the user's question. Do "
    "not call another tool. Write the reply text now."
)


@dataclass(frozen=True)
class PendingOpView:
    """Read-only view of a pending / stalled operation surfaced across channels
    (= issue #268 Phase 1, #270 umbrella vocabulary).

    First instance carries ``UserIntervention`` data; Phase B refactor
    (= #270) generalises this dataclass to also describe MCP pending
    calls / peer-delegation pending operations. The ``kind`` field is
    the discriminator. Field shape is **pinned at Phase A landing**
    per tui-coder commitment so the TUI Pending tab + ``/pending``
    slash command code path doesn't churn as new kinds land.

    Pinned fields (= TUI consume contract):
      - ``id``: stable identifier (= iv.id for interventions)
      - ``kind``: discriminator (= "intervention" for now, future
        "mcp_call" / "peer_delegate")
      - ``origin_channel_id``: where the op originated (= "tui:..." /
        "a2a:..." etc.)
      - ``created_at``: ISO timestamp string for age-rendering
      - ``summary``: short human-readable description (= iv.prompt
        first line for interventions)
      - ``detail``: optional second line (= iv.detail for interventions)
    """
    id: str
    kind: str
    origin_channel_id: str
    created_at: str
    summary: str
    detail: str = ""

    @classmethod
    def from_intervention(cls, iv: "UserIntervention") -> "PendingOpView":
        """Build a view from a ``UserIntervention``. ``created_at`` is
        the current time at view construction since iv doesn't carry
        its own timestamp; the TUI uses this for relative age display
        even though it's a view-time stamp.
        """
        from datetime import datetime, timezone  # noqa: PLC0415
        return cls(
            id=iv.id,
            kind="intervention",
            origin_channel_id=iv.origin_channel_id or "",
            created_at=datetime.now(timezone.utc).isoformat(),
            summary=iv.prompt,
            detail=iv.detail or "",
        )


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

    @property
    def session(self) -> "ChatSession":
        """Read-only accessor for the backing ChatSession. Tests verify
        that adapters from the same session share identity through this
        surface."""
        return self._session

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

    def __init__(
        self,
        session: "ChatSession",
        run_id: str | None,
        skill_name: str | None,
        *,
        channel_id: str | None = None,
    ) -> None:
        self._session = session
        self._run_id = run_id
        self._skill_name = skill_name
        # issue #268 Phase 2 continuation: optional channel_id stamping.
        # Production wiring (= ChatSession._build_intervention_bus_for_skill)
        # passes the session's canonical channel_id (e.g. "tui") so
        # skill-emitted ivs carry provenance for cross-channel routing.
        # Test fixtures that construct ChatInterventionBus directly
        # without passing channel_id see unchanged behaviour (= no
        # stamping → no stall check → existing dispatch path).
        self._channel_id = channel_id

    @property
    def channel_id(self) -> str | None:
        """Configured channel identifier for issue #268 origin-pin
        routing. ``None`` means stamping is disabled for this instance
        (= test-fixture default that doesn't engage the new mechanism).
        """
        return self._channel_id

    async def deliver(self, iv: "UserIntervention") -> "InterventionAnswer":
        """``UserChannel.deliver`` — route the prompt to ChatSession's
        outbox/inbox so the attached TUI surfaces it to the user.

        issue #268 Phase 2 continuation: when this bus was constructed
        with a ``channel_id`` AND no chain-override is active for the
        iv's run, stamp ``iv.origin_channel_id`` so the agent layer
        can attribute the iv to this channel for cross-channel
        observe / discard / claim routing. The override-aware skip
        matters because the SAME ChatInterventionBus instance services
        A2A-spawned skills (= ``_build_agent`` constructs one per
        skill spawn regardless of caller), and A2AInterventionBus needs
        a clean slot to stamp ``a2a:<run_id>`` downstream. Respects
        pre-existing stamping (= upstream-set origin wins for
        multi-hop delegation provenance).
        """
        # issue #268 Phase 2 continuation: stamp origin channel for
        # cross-channel routing (only when configured AND no chain
        # override is going to claim this iv first). Use the bus's
        # captured ``_run_id`` for the override lookup since
        # ``iv.run_id`` isn't filled in until below.
        if self._channel_id is not None and iv.origin_channel_id is None:
            override_active = False
            run_id_for_lookup = iv.run_id or self._run_id
            if (
                run_id_for_lookup is not None
                and self._session._intervention_overrides
            ):
                chain_id = self._session.running_skills_chain.get(
                    run_id_for_lookup,
                )
                if chain_id is not None:
                    override_active = (
                        chain_id in self._session._intervention_overrides
                    )
            if not override_active:
                iv.origin_channel_id = self._channel_id
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
            buffered = self._session.consume_buffered_intervention_answer(iv.run_id)
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


@dataclass(init=False)
class ChatMessage:
    """Chat-history entry, shaped to mirror the OpenAI/Anthropic message
    list wire format (issue #383 E-full).

    Each ``ChatMessage`` is one entry in the LLM-facing conversation, so
    ``self.history`` can be serialised straight to the LLM without
    synthesis. Tool turns are represented as their own ``role="tool"``
    entries; assistant turns that emitted tool calls carry the
    ``tool_calls`` field; multi-modal user / tool turns use the
    list-of-parts ``content`` shape.

    Role vocabulary:
      - ``user`` — user input
      - ``assistant`` — LLM reply (= previously ``agent``)
      - ``tool`` — tool response (= new)
      - ``system`` — system prompt (rare; usually built at wire time)
      - ``summary`` — chat-compactor output (Reyn-internal, filtered at wire boundary)
      - ``skill_event`` — TUI display marker (Reyn-internal, filtered at wire boundary)
    """
    role: Literal[
        "user", "assistant", "tool", "system", "summary", "skill_event",
    ]
    # ``content`` is either:
    #   - a ``str`` (= text-only turn), or
    #   - a ``list[dict]`` of litellm-style content parts (= multimodal user
    #     turn / tool response with an image / etc.). Each part is e.g.
    #       {"type": "text", "text": "..."}
    #       {"type": "image_url", "image_url": {"url": "<data url OR file ref>"}}
    #       {"type": "image",     "path": "<abs or cwd-rel>",
    #                             "mime_type": "...", "content_hash": "sha256:..."}
    # The last shape (= ``"image"`` with ``path``) is the **path-ref**
    # introduced by #383: storage points at a file on disk, the
    # wire-shape builder reads and embeds the binary at LLM-call time.
    content: str | list[dict] = ""
    ts: str = ""
    seq: int = 0  # monotonic per-session sequence id; 0 for non-conversational entries
    meta: dict = field(default_factory=dict)
    # OpenAI/Anthropic tool-turn fields ─────────────────────────────────
    # ``tool_calls`` is set ONLY on ``role="assistant"`` entries where the
    # LLM emitted one or more tool calls. Each block follows the OpenAI
    # function-tool shape:
    #   {"id": "<tool_call_id>", "type": "function",
    #    "function": {"name": "<tool>", "arguments": "<json str>"}}
    tool_calls: list[dict] | None = None
    # ``tool_call_id`` is set ONLY on ``role="tool"`` entries. Links the
    # response back to the originating ``tool_call`` block on the
    # preceding assistant message.
    tool_call_id: str | None = None
    # ``name`` is set ONLY on ``role="tool"`` entries (= function name).
    # Mirrors the OpenAI tool-message ``name`` field; some providers
    # require it for tool-result attribution.
    name: str | None = None

    def __init__(
        self,
        role: str,
        content: "str | list[dict]" = "",
        ts: str = "",
        seq: int = 0,
        meta: "dict | None" = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None:
        # Reject the pre-#383 ``"agent"`` spelling. Migration of on-disk
        # ``history.jsonl`` entries happens at load time via
        # ``_migrate_legacy_chat_message``; nothing else should be
        # constructing with ``role="agent"`` anymore.
        if role == "agent":
            raise ValueError(
                "ChatMessage role='agent' was renamed to 'assistant' in "
                "issue #383. Pass role='assistant' instead. "
                "(Legacy on-disk entries are migrated read-time by "
                "_migrate_legacy_chat_message.)"
            )
        self.role = role
        self.content = content
        self.ts = ts
        self.seq = seq
        self.meta = meta if meta is not None else {}
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.name = name

    @property
    def text(self) -> str:
        """Derived view returning a str representation of ``content``.

        - str content → returned as-is.
        - list-of-parts content → the first ``{"type":"text"}`` part's text.
        - neither → empty string.

        This is a convenience accessor, NOT a legacy compatibility shim:
        readers that want a textual rendering of any ChatMessage (text or
        multimodal) call ``m.text`` instead of branching on isinstance.
        Writers update ``content`` directly.
        """
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
        return ""


# ── Legacy ChatMessage migration ───────────────────────────────────────
#
# history.jsonl files written before issue #383 used the pre-Design-B
# shape: ``role`` ∈ {"user","agent","skill_event","summary"}; ``text:
# str``; ``media: list[dict]`` (= inline base64 image_url parts from
# #366). On load, ``_migrate_legacy_chat_message`` rewrites such
# entries into the new wire shape so the runtime only ever sees
# Design-B ChatMessage instances.


def _materialise_path_ref_content(
    content: str | list[dict], media_store: Any,
) -> str | list[dict]:
    """Issue #383 PR-C: convert path-ref content parts to inline data URLs
    at the LLM wire boundary.

    Three input cases:
      - str content → returned unchanged.
      - list content with no path-ref parts → returned unchanged.
      - list content with path-ref parts (= ``{"type":"image","path":...}``)
        → each path-ref is resolved via ``media_store.read_image`` and
        emitted as ``{"type":"image_url","image_url":{"url":"data:..."}}``.

    When ``media_store`` is None OR the path resolves outside the storage
    root OR the file no longer exists, the block is dropped (= conversation
    continues without it, no crash). Already-inline image_url parts pass
    through.
    """
    if isinstance(content, str) or not isinstance(content, list):
        return content
    has_pathref = any(
        isinstance(p, dict) and p.get("type") == "image" and p.get("path")
        for p in content
    )
    if not has_pathref:
        return content
    materialised: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            materialised.append(part)
            continue
        if part.get("type") != "image" or not part.get("path"):
            materialised.append(part)
            continue
        path = part["path"]
        mime = part.get("mime_type") or part.get("mimeType") or "image/png"
        data_bytes = _read_pathref_image(path, media_store)
        if data_bytes is None:
            continue
        import base64
        data_b64 = base64.b64encode(data_bytes).decode("ascii")
        materialised.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data_b64}"},
        })
    return materialised


def _read_pathref_image(path: str, media_store: Any) -> bytes | None:
    """Resolve a path-ref to raw image bytes (issue #383 PR-C).

    Two cases:
      - Path inside the MediaStore's image directory (= Reyn-owned,
        from a tool result): read via ``media_store.read_image``.
      - Path elsewhere (= user-attached via ``/image``): read directly
        from disk so user files don't need to be copied into the
        workspace.

    Returns None when the path can't be resolved (missing file,
    permission denied, etc.). Caller drops the block in that case so
    the LLM message stays valid.
    """
    from pathlib import Path as _Path
    # Try the MediaStore first (= validates inside-media_dir + reads).
    if media_store is not None:
        try:
            data_bytes, found = media_store.read_image(path)
            if found:
                return data_bytes
        except PermissionError:
            # Not inside media_dir — try direct disk read below.
            pass
    # Direct disk read for user-attached files. Resolve relative paths
    # against CWD (= the chat session's project root convention).
    p = _Path(path)
    if not p.is_absolute():
        p = _Path.cwd() / p
    p = p.resolve()
    if not p.exists() or not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def _migrate_legacy_chat_message(raw: dict) -> dict:
    """Read-time migration for pre-#383 history.jsonl entries.

    Detects the legacy shape (= ``text`` key + optional ``media`` list,
    ``role="agent"`` for assistant replies) and emits the Design-B
    shape (= ``content`` field, ``role="assistant"``). Mutates a copy;
    the caller hands the result to ``ChatMessage(**kwargs)``.

    Legacy → new:
      role: "agent"            → "assistant"
      text: "hi"               → content: "hi"
      text + media: [...]      → content: [{"type": "text", "text": "hi"}, ...media]
      (no text, media: [...])  → content: [...media]

    Inline base64 in media blocks is left alone — those entries
    pre-date the path-ref design and rewriting them to files would
    be a one-shot tool, out of scope for read-time migration.
    """
    raw = dict(raw)  # don't mutate the caller's dict
    if "content" in raw:
        # Already new shape (= written post-#383 or already migrated).
        # Still normalise role just in case "agent" snuck in.
        if raw.get("role") == "agent":
            raw["role"] = "assistant"
        return raw

    # Legacy shape: text + optional media.
    text_val = raw.pop("text", "")
    media_val = raw.pop("media", None) or []

    if media_val:
        parts: list[dict] = []
        if text_val:
            parts.append({"type": "text", "text": text_val})
        parts.extend(media_val)
        raw["content"] = parts
    else:
        raw["content"] = text_val

    if raw.get("role") == "agent":
        raw["role"] = "assistant"
    return raw


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_iso_to_epoch(ts: str | None) -> float | None:
    """Best-effort ISO-8601 → epoch-seconds conversion.

    Returns None if *ts* is empty or unparseable. Used by the
    action-usage extractor to source the recency timestamp from each
    ChatMessage's stored ``ts`` field; failure yields a record skipped
    rather than a crash.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_tool_call_records(
    messages: "list[ChatMessage]",
) -> list[tuple[str, float]]:
    """Extract ``(qualified_name, ts_epoch)`` tuples from a list of
    ``ChatMessage`` instances.

    Recognises two emission shapes (mirrors ``router_loop`` recording
    semantics pre-refactor):

      - ``invoke_action`` tool call → ``args["action_name"]`` is the
        qualified name; the ``args`` payload may be a JSON string per
        the OpenAI wire shape.
      - Any other tool call → ``function.name`` itself (a hot-list
        alias or a universal wrapper). Wrapper names like
        ``list_actions`` are caught by the tracker's
        ``_is_valid_qualified_name`` filter and dropped.

    Returns an empty list when no candidate tool_calls are present.
    """
    out: list[tuple[str, float]] = []
    for m in messages:
        if getattr(m, "role", None) != "assistant":
            continue
        tcs = getattr(m, "tool_calls", None) or []
        if not tcs:
            continue
        ts_epoch = _ts_iso_to_epoch(getattr(m, "ts", None))
        if ts_epoch is None:
            continue
        for tc in tcs:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name", "")
            if not isinstance(name, str) or not name:
                continue
            if name == "invoke_action":
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except (json.JSONDecodeError, ValueError):
                        continue
                else:
                    args = raw_args
                target = (
                    args.get("action_name")
                    if isinstance(args, dict) else None
                )
                if isinstance(target, str) and target:
                    out.append((target, ts_epoch))
            else:
                out.append((name, ts_epoch))
    return out


# FP-0041 (#489) PR-A: humanic dispatch attribution helper.
#
# Sender envelope strings follow ``<transport>:<id>[:<display>]``. This
# helper produces a human-readable label for inclusion in state_change
# summaries so the LLM sees "bob (Slack)" instead of "slack:U456:bob".
# Unknown / malformed senders fall through to the raw string.
_SENDER_TRANSPORT_DISPLAY = {
    "user":     "user",
    "slack":    "Slack",
    "line":     "LINE",
    "cron":     "scheduled cron job",
    "a2a":      "peer agent",
    "webhook":  "external webhook",
}


def _format_sender_label(sender: str | None) -> str:
    """Format a sender envelope string for LLM-visible state_change text.

    Examples
    --------
    ``"slack:U456:bob"`` → ``"bob (Slack)"``
    ``"slack:U456"`` → ``"slack user U456"``
    ``"cron:morning_news"`` → ``"scheduled cron job 'morning_news'"``
    ``"user:tui"`` → ``"user (TUI)"``
    ``"a2a:news_agent"`` → ``"peer agent 'news_agent'"``
    ``None`` → ``"an unknown sender"`` (= used in first-turn pre-state)

    Falls through to the raw string when the transport is not in the
    known list — keeps the dispatch resilient to new sources added by
    future PRs without label updates here.
    """
    if sender is None:
        return "an unknown sender"
    parts = sender.split(":", 2)
    if not parts or not parts[0]:
        return sender
    transport = parts[0]
    rest = parts[1:] if len(parts) > 1 else []
    transport_label = _SENDER_TRANSPORT_DISPLAY.get(transport)
    if transport_label is None:
        return sender
    if transport == "user":
        # ``user:tui`` / ``user:web`` / ``user:cli`` → "user (TUI)" etc.
        surface = rest[0].upper() if rest else ""
        return f"user ({surface})" if surface else "user"
    if transport == "slack" or transport == "line":
        # Prefer display name when present, fall back to id.
        if len(rest) >= 2 and rest[1]:
            return f"{rest[1]} ({transport_label})"
        if len(rest) >= 1 and rest[0]:
            return f"{transport_label.lower()} user {rest[0]}"
        return transport_label
    if transport == "cron":
        if rest and rest[0]:
            return f"{transport_label} '{rest[0]}'"
        return transport_label
    if transport == "a2a":
        if rest and rest[0]:
            return f"{transport_label} '{rest[0]}'"
        return transport_label
    if transport == "webhook":
        if rest and rest[0]:
            return f"{transport_label} ({rest[0]})"
        return transport_label
    return sender


# #398 v4 emitter family — events-log subscriber dispatch table.
#
# Maps known emitter event types to (source, template) tuples used by
# ``ChatSession._on_chat_event_for_state_change`` to convert events
# into ``notify_state_change`` calls. Adding a new emitter is one
# entry here + the emitter emitting its event on the session's
# events log (= OpContext.events, bound to ``_chat_events`` for
# chat router-initiated ops).
#
# ``template`` is a ``str.format``-compatible string; the event's
# ``data`` dict is passed as kwargs. Missing keys (= malformed event
# payload) are silently skipped — observability must not crash the
# events bus.
#
# Sister mechanism: PermissionResolver._on_persist_callbacks (= the
# permission_manager emitter wiring landed in PR #456). The two
# mechanisms coexist because their natural integration points differ:
# permission_manager is a singleton service across sessions and
# benefits from a direct subscriber list; op_runtime ops already emit
# session-scoped events so the events log is the natural seam.
_STATE_CHANGE_EVENT_MAPPINGS: dict[str, tuple[str, str]] = {
    # MCP server install success (= ``reyn.op_runtime.mcp_install``
    # emits this on the events log after writing the config).
    "mcp_server_installed": (
        "mcp_install",
        "MCP server '{server_name}' was installed.",
    ),
    # MCP server removal success (= ``reyn.op_runtime.mcp_drop_server``
    # emits this after removing the config entry). Symmetric to
    # mcp_server_installed — surfaces the "no longer available"
    # state-change to the LLM so it doesn't keep trying.
    "mcp_server_removed": (
        "mcp_drop_server",
        "MCP server '{server}' was removed.",
    ),
    # Indexed corpus removal (= ``reyn.op_runtime.index_drop`` emits
    # this after dropping chunks from the backend). Recall against
    # the dropped source will now miss; surfacing the change lets
    # the LLM understand "the source it was citing yesterday doesn't
    # exist today".
    "index_dropped": (
        "index_drop",
        "Indexed source '{source}' was removed.",
    ),
    # Future emitter slots (= add when wired):
    # "config_reloaded":  ("config_watcher", "Reyn configuration was updated."),
    # "sp_version_changed": ("sp_loader",   "Agent system prompt was updated to version {version}."),
}


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
    # Issue #261 — source_agent stamping for the parent_delegate branch.
    # See ``source_agent_var`` in ``services/intervention_handler.py``
    # for the chain semantics. Omitted when the var is at its default
    # (``None``) so the meta shape stays identical to the non-delegated
    # path (Phase 2 ``test_outbox_intervention_meta_shape_is_stable``
    # contract).
    from reyn.chat.services.intervention_handler import source_agent_var
    src = source_agent_var.get()
    if src:
        out["source_agent"] = src
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
        # B46-fix: shared stdlib artifacts (= src/reyn/stdlib/artifacts/<n>.yaml,
        # e.g. user_message) are referenced by many skills as their entry input
        # but live outside the skill directory. Without a fallback, every skill
        # using user_message ends up with input_schema=None → hot-list wrapper
        # surfaces empty {properties: {}, additionalProperties: true} → LLM
        # calls skill__<name> with {} (= word_stats_demo multiline 9/10 empty
        # args in dogfood B46 W2 S6). Verified via trace-patch-replay: when the
        # alias parameters include the shared artifact's properties (= just
        # `text` declaration), flash-lite passes the correct value 10/10.
        stdlib_artifacts_dir = stdlib_root() / "artifacts"
        for art_name in preferred:
            for candidate in (artifacts_dir / f"{art_name}.yaml",
                              stdlib_artifacts_dir / f"{art_name}.yaml"):
                if not candidate.exists():
                    continue
                art_data = _yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                schema = art_data.get("schema") or {}
                props = schema.get("properties") or {}
                if props:
                    input_fields = list(props.keys())
                    input_schema = dict(schema)
                    input_wrapped = bool(art_data.get("wrapped", True))
                    break
            if input_schema is not None:
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
        multimodal_config: "MultimodalConfig | None" = None,
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
        # #398 v4 emitter wiring (= permission_manager → state_change).
        # Subscribe to ``_persist`` events on the shared PermissionResolver
        # so a permission grant / revoke mints a state_change history
        # entry in this session — the LLM sees "permission for X was
        # granted" in its next turn and breaks out of the #352 refusal
        # trap. Stored as a bound method so the same reference can be
        # unregistered on session shutdown.
        if self._perm is not None and hasattr(self._perm, "register_on_persist"):
            self._on_perm_persist_cb = self._on_permission_persisted
            self._perm.register_on_persist(self._on_perm_persist_cb)
        else:
            self._on_perm_persist_cb = None
        _safety = safety or SafetyConfig()
        self._safety = _safety
        # FP-0017 follow-up: declarative sandbox config (reyn.yaml `sandbox:`).
        # Plumbed through to spawned Agents so sandboxed_exec backend selection
        # honors the operator's declared policy.
        self._sandbox_config = sandbox_config
        # Issue #364 — multi-modal cluster: media-size gate config plumbed
        # through to spawned Agents AND to the router host adapter (=
        # chat-router web__fetch / file__read / mcp paths).
        self._multimodal_config = multimodal_config
        # Issue #383 PR-C — single MediaStore instance per ChatSession,
        # constructed from the multimodal config's storage dirs.
        # Subsequently threaded into spawned Agents (= for control-IR
        # ops invoked from skills) AND into the router host adapter
        # (= for ops invoked directly from the chat router via tool
        # calls). ``None`` when no multimodal config is supplied —
        # handlers then fall back to the pre-#383 inline shape.
        from reyn.workspace.media_store import MediaStore, MediaStoreConfig
        if multimodal_config is not None:
            self._media_store: "MediaStore | None" = MediaStore(
                MediaStoreConfig(
                    media_dir=multimodal_config.media_dir,
                    tool_results_dir=multimodal_config.tool_results_dir,
                ),
                project_root=Path.cwd(),
                # #385 β core impl sub-task 1: path-refs minted by this
                # session carry resource_uri / source_agent so cross-host
                # consumers (= other agents via A2A / MCP / Browser) can
                # dispatch back here.
                agent_name=agent_name,
                # #385 β core impl sub-task 3b: when this Reyn instance
                # is reachable over HTTP (= operator sets
                # ``multimodal.base_url`` in reyn.yaml), path-refs also
                # carry a ``url`` field pointing at the resources
                # router so cross-host consumers can HTTP GET the body.
                # When unset, only same-host ``path`` is available.
                base_url=multimodal_config.base_url,
            )
        else:
            self._media_store = None
        # Issue #366: queue of image blocks the user attached via
        # ``/image PATH`` or ``--image PATH``. Drained on the next user
        # message turn (= attached to that ChatMessage's ``media`` field).
        # litellm-style content parts:
        #   {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
        self._pending_user_images: list[dict] = []
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
        # FP-0041 (#489) PR-A: humanic dispatch attribution.
        # Tracks the sender of the most-recently-dispatched inbox item
        # so a sender transition (= different consumer addresses the
        # agent now) can emit a state_change history entry. None until
        # the first attributed turn is dispatched.
        self._last_sender: str | None = None
        # FP-0041 (#489) PR-D2: humanic reply attribution.
        # When an inbox payload carries a ``reply_to`` (= ExternalRef
        # / A2aRef / etc. encoded by the inbound handler), the dispatch
        # attribution captures it here so subsequent agent replies via
        # ``_put_outbox`` default to that reply_to. Cleared / replaced
        # at each sender transition.
        self._last_reply_to: Any = None
        # FP-0041 (#489) PR-D2: outbox interceptor for external transport.
        # An async callable ``(OutboxMessage) -> bool`` invoked from
        # ``_put_outbox`` before queueing. When it returns True, the
        # message is consumed by the interceptor (= dispatched to e.g.
        # Slack via MCP) and NOT queued for TUI display. Set by web
        # lifespan / session factory when external transports are
        # configured; ``None`` skips interception (= default).
        self._outbox_interceptor: Any = None
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
            and not _embedding_class_needs_missing_extras(
                self._action_retrieval.embedding_class,
                embedding_config,
            )
        ):
            try:
                from reyn.embedding import get_provider as _get_provider
                from reyn.tools.action_index import ActionEmbeddingIndex

                # FP-0043 Component C.3: surface the sentence-transformers
                # lazy model-load lifecycle (= downloading / loaded /
                # error) via the session's events bus so the TUI /
                # chainlit surfaces can render a sticky status row + a
                # green "done" frame + a retry-hint error row. The sink
                # is called from the embed worker thread; events.emit is
                # GIL-protected + sync so this is safe without a
                # call_soon_threadsafe bridge.
                #
                # C.4 hotfix (2026-05-27): the sink closure resolves
                # ``self._chat_events`` at *call* time, not at
                # construction time — the EventLog is built later in
                # __init__ (= line ~1482). The previous C.3 wiring
                # captured ``self.events`` (= attribute that does NOT
                # exist on ChatSession), which silently raised
                # AttributeError at this point and the outer ``except``
                # swallowed it, disabling search_actions for every
                # operator who had ``embedding_class`` set. Mirrors the
                # ``_on_hot_list_changed`` closure pattern in the
                # ActionUsageTracker setup below.
                def _embedding_event_sink(
                    kind: str, text: str, meta: dict,
                ) -> None:
                    try:
                        self._chat_events.emit(
                            f"embedding_{kind}",
                            text=text,
                            **meta,
                        )
                    except Exception:
                        pass

                self._embedding_provider = _get_provider(
                    "litellm",
                    embedding_config,
                    event_sink=_embedding_event_sink,
                )
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
        # Per-agent compacted table at
        # ``.reyn/agents/<agent_name>/action_usage.json``. The table is fed
        # by the chat-compactor sink (see ``CompactionController`` wiring
        # below); uncompacted turns are scanned at hot-list-build time.
        self._action_usage_tracker: Any = None
        if (
            self._action_retrieval.universal_wrappers_enabled
            and self._action_retrieval.hot_list_n > 0
        ):
            try:
                from reyn.tools.action_usage_tracker import ActionUsageTracker
                # Issue #192: wire a callback that emits ``hot_list_updated``
                # on every reorder of the compacted ranking. Lambda defers
                # ``self._chat_events`` resolution to call time (it's
                # constructed below at the EventLog init).
                def _on_hot_list_changed(ranking: list[dict]) -> None:
                    try:
                        self._chat_events.emit(
                            "hot_list_updated", ranking=ranking,
                        )
                    except Exception:
                        pass
                self._action_usage_tracker = ActionUsageTracker(
                    persist_path=(
                        Path(".reyn") / "agents" / agent_name
                        / "action_usage.json"
                    ),
                    on_ranking_changed=_on_hot_list_changed,
                )
            except Exception:
                self._action_usage_tracker = None
        self._mcp_servers = mcp_servers
        self.output_language = output_language
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        self._agent_role = agent_role
        # ``agent_role`` (public read-only via @property below) gives callers a
        # stable accessor without exposing the private backing field; mutation
        # still goes through ``self._agent_role = ...`` (= same convention as
        # other per-session knobs flipped at runtime, e.g. ``output_language``
        # / ``model``). The property exists so tests and external read-only
        # consumers don't reach into the underscore attribute directly.
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
        # #398 v4 emitter family — generic events-log subscriber that
        # converts known op-emitted events (= mcp_server_installed,
        # future: config_reloaded / sp_version_changed) to
        # ``state_change`` history entries via the
        # ``_STATE_CHANGE_EVENT_MAPPINGS`` dispatch table. Sister to
        # the permission_manager direct-callback wiring (= PR #456).
        self._chat_events.add_subscriber(
            self._on_chat_event_for_state_change,
        )

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
            get_skill_registry=self.get_skill_registry,
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
            plan_registry_getter=self.get_plan_registry,
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
            uncompacted_tool_call_records_fn=(
                self._uncompacted_tool_call_records
            ),
            action_retrieval_config=self._action_retrieval,
            # FP-0034 Phase 2: sandbox backend for exec D14 visibility gate.
            # None when sandbox_config is None (= noop assumed).
            sandbox_backend=(
                self._sandbox_config.backend if self._sandbox_config is not None
                else None
            ),
            # Issue #364 multi-modal cluster: media-size gate config.
            multimodal_config=self._multimodal_config,
            # Issue #383 PR-C: shared MediaStore for image + tool-result storage.
            media_store=self._media_store,
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
                channel_id=DEFAULT_CHAT_CHANNEL_ID,
            ),
            # FP-0037 S2: yaml mtime watch needs the project root to resolve
            # the 3 yaml scope tier paths. None falls back to user-global only.
            project_root=getattr(self._registry, "_project_root", None),
        )

        # FP-0019 Wave 1: background head/body/tail compaction service.
        # Owns the asyncio.Task lifecycle; session delegates via spawn_maybe()
        # and cancel().  All callbacks resolve against self at call time.
        def _merge_action_usage_from_candidates(
            candidates: "list[ChatMessage]",
        ) -> None:
            if self._action_usage_tracker is None:
                return
            try:
                records = _extract_tool_call_records(candidates)
                if records:
                    self._action_usage_tracker.merge_compacted(records)
            except Exception:
                pass

        self._compaction_controller = CompactionController(
            event_log=self._chat_events,
            config=self._compaction,
            history_access=lambda: self.history,
            latest_summary=self._latest_summary,
            run_compaction_skill=self._skill_runner.run_stdlib,
            history_appender=self._append_history,
            make_summary_message=lambda rendered, structured, covers: ChatMessage(
                role="summary",
                content=rendered,
                ts=_now_iso(),
                meta={"structured": structured, "covers_through_seq": covers},
            ),
            render_summary=_render_summary_for_storage,
            # Feed compacted candidates' tool calls into the per-agent
            # action_usage table so the hot-list survives summarisation.
            merge_action_usage=_merge_action_usage_from_candidates,
        )

        # FP-0019 Wave 3: crash recovery service.
        # Discovers in-flight skill_runs from WAL and re-spawns them on
        # session start.  All business logic lives in AutoResumeHandler;
        # session delegates via _auto_resume_active_skills() (thin wrapper).
        self._auto_resume_handler = AutoResumeHandler(
            event_log=self._chat_events,
            state_log=self._state_log,
            get_skill_registry=self.get_skill_registry,
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
        self._intervention_overrides: dict[str, "RequestBus"] = {}

        # Wave-13 T2-5: TUI-side error box count surfaced to slash commands
        # (/pending list needs-attention, /reset confirm preview).  Starts at
        # 0.  The TUI app's outbox handler increments this on mount_error and
        # decrements on dismiss; slash commands read it via current_state_summary().
        self._error_box_count: int = 0

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

    @property
    def agent_role(self) -> str:
        """Read-only public accessor for the attached agent's role text.

        Mutation still goes through ``self._agent_role = ...`` so the
        intent of "this is a runtime knob, not a constructor-time
        immutable" stays visible at the call site. Reads via the
        property are the encapsulation-respecting surface for slash
        commands and tests that need to verify the role.
        """
        return self._agent_role

    @property
    def error_box_count(self) -> int:
        """Read-only accessor for the TUI error-box count.

        Incremented by ``app_outbox`` on ``mount_error`` and decremented
        on ``dismiss_error``. Slash commands and tests read via this
        public surface; the write path stays on
        ``self._error_box_count`` so the lifecycle stays visible at the
        TUI call sites that own it.
        """
        return self._error_box_count

    @property
    def router_loop_agent_replies(self) -> "list[str] | None":
        """Read-only accessor for the in-flight router-loop agent reply
        tracker. ``None`` outside a router turn; a list while a turn
        is open. Tests verify the post-turn clearing semantics through
        this surface.
        """
        return self._router_loop_agent_replies

    @property
    def router_host(self):
        """Read-only accessor for the session's RouterHostAdapter.

        Tests (Tier-1 protocol-compliance + Tier-2 behavioural) probe
        the adapter via this surface. The adapter instance is set once
        in ``__init__`` and never re-bound.
        """
        return self._router_host

    @property
    def outbox_interceptor(self):
        """Read-only accessor for the per-session outbox interceptor.

        Set by the web layer's ``_wire_external_outbox_interceptor`` when
        external transports are configured; remains ``None`` otherwise.
        Mutation continues to go through ``self._outbox_interceptor``
        so the wire-up call site stays visible.
        """
        return self._outbox_interceptor

    @property
    def last_reply_to(self):
        """Read-only accessor for the most-recent inbox ``reply_to``.

        Captured by the sender-attribution path and used by
        ``_put_outbox`` to default the outbox message's ``reply_to``
        when the caller did not supply one. Tests verify the capture
        + default chain through this surface.
        """
        return self._last_reply_to

    @property
    def on_perm_persist_cb(self):
        """Read-only accessor for the permission-persist callback that this
        session registered on its ``PermissionResolver`` (or None if no
        resolver / no callback was wired). Tests verify the
        register/unregister balance through this surface.
        """
        return self._on_perm_persist_cb

    @property
    def on_limit(self) -> "_OnLimitConfig":
        """Read-only accessor for the safety-loop OnLimit config.

        Captured at construction from ``SafetyConfig.on_limit``; tests
        verify the mode + auto_extend semantics through this surface.
        Production callers in ``session.py`` continue to use the
        underscore name; this property is the read-only public view.
        """
        return self._on_limit

    @property
    def agent_registry(self):
        """Read-only accessor for the session's owning AgentRegistry (or None
        when running outside a registry). Tests verify cross-agent state
        (= e.g. AgentRegistry.last_truncation_ts on shared WAL) via this
        surface.
        """
        return self._registry

    @property
    def interventions(self) -> "InterventionRegistry":
        """Read-only public accessor for the session's InterventionRegistry.

        The registry itself carries rich public API (= ``get`` /
        ``queued_count`` / ``list_active`` / ``has_active_listener`` /
        ``is_listener_enforcement_enabled``), so exposing it directly
        keeps callers off the underscore field without forcing a
        delegate-method explosion on ChatSession. The registry
        instance is set once in ``__init__`` and never re-bound.
        """
        return self._interventions

    @property
    def chains(self) -> "ChainManager":
        """Read-only accessor for the session's ChainManager.

        The manager carries rich public API (``find_chain`` / ``has`` /
        ``get`` / ``all_chain_ids`` / ``register`` / ``update`` /
        ``resolve``), so exposing the holder via a public name keeps
        callers off the underscore field. The manager instance is set
        once in ``__init__`` and never re-bound.
        """
        return self._chains

    @property
    def buffered_intervention_answers(self) -> dict:
        """Read-only accessor for the per-session buffered intervention
        answers map. Used by the crash-recovery / restart path to
        re-deliver answers to runs that finished their ask_user wait
        while the session was offline. Write side stays on
        ``self._buffered_intervention_answers`` so the buffering call
        sites are visible.
        """
        return self._buffered_intervention_answers

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

    @property
    def pending_user_images(self) -> list[dict]:
        """Read-only accessor for the per-session image upload queue.

        Tests and slash commands inspect this queue to verify that an
        uploaded image landed (= ``/image`` slash + chainlit attachment
        button both feed the same list). The write side stays on
        ``self._pending_user_images`` so the lifecycle (= drain on
        send, reset to []) is visible in the production call sites.
        """
        return self._pending_user_images

    @property
    def journal(self) -> "SnapshotJournal":
        """Read-only accessor for the session's SnapshotJournal.

        The journal carries rich public API (``record_plan_started`` /
        ``record_plan_aborted`` / ``append_inbox`` / ``consume_inbox`` /
        ``snapshot``); exposing the holder via a public name keeps slash
        commands and tests off the underscore field. The journal
        instance is set once in ``__init__`` and never re-bound.
        """
        return self._journal

    def current_state_summary(self) -> dict:
        """Return a lightweight snapshot for slash-command display.

        Used by ``/pending list`` (needs-attention section) and
        ``/reset`` (confirm preview line).  Keys:

        - ``running_skills``: count of currently running skill runs.
        - ``running_plans``: count of currently running plan tasks.
        - ``error_box_count``: count of undismissed TUI error boxes
          (maintained by the TUI outbox handler; 0 in non-TUI mode).
        - ``interrupted_plans``: list of ``{plan_id, goal, exc_type}``
          dicts for recently-interrupted plans (event-store scan; empty
          when project_root is unavailable).
        - ``stuck_skills``: list of ``{skill_name, run_id, stuck_at}``
          dicts for skill runs that ended on a non-terminal event.

        All sources are defensive: missing attributes / I/O errors
        return zero / empty rather than raising.
        """
        n_skills = len(self.running_skills) if hasattr(self, "_skill_runner") else 0
        n_plans = len(self.running_plans) if hasattr(self, "_plan_runner") else 0
        error_count = getattr(self, "_error_box_count", 0)

        # Interrupted plans + stuck skills require the event-store scan
        # implemented in the agents-tab helper functions.  We import lazily
        # (avoids pulling TUI widgets at session-bootstrap time).
        interrupted_plans: list[dict] = []
        stuck_skills: list[dict] = []
        try:
            project_root: "Path | None" = None
            registry = getattr(self, "_registry", None)
            if registry is not None:
                project_root = getattr(registry, "_project_root", None)

            if project_root is not None:
                from reyn.chat.tui.widgets.right_panel.agents_tab import (
                    _recent_plans_for_agent,
                    _recent_skill_runs_for_agent,
                )
                running_plan_ids = set(self.running_plans.keys())
                running_run_ids = set(self.running_skills.keys())
                plans = _recent_plans_for_agent(
                    project_root, self.agent_name, running_plan_ids,
                )
                for p in plans:
                    if p.get("status") == "interrupted":
                        interrupted_plans.append({
                            "plan_id": p.get("plan_id", "?"),
                            "goal": p.get("goal", ""),
                            "exc_type": p.get("exc_type", ""),
                        })
                skills = _recent_skill_runs_for_agent(
                    project_root, self.agent_name, running_run_ids,
                )
                for s in skills:
                    if s.get("status") == "stuck":
                        stuck_skills.append({
                            "skill_name": s.get("skill_name", "?"),
                            "run_id": s.get("run_id", "?"),
                            "stuck_at": s.get("stuck_at", "?"),
                        })
        except Exception:  # noqa: BLE001 — best-effort; display must not crash
            pass

        return {
            "running_skills": n_skills,
            "running_plans": n_plans,
            "error_box_count": error_count,
            "interrupted_plans": interrupted_plans,
            "stuck_skills": stuck_skills,
        }

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
            intervention_bus=ChatInterventionBus(
                self, run_id, skill_name,
                channel_id=DEFAULT_CHAT_CHANNEL_ID,
            ),
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

    def _handle_sender_attribution(self, payload: object) -> None:
        """Surface a sender transition to the LLM as a state_change entry
        (= FP-0041 (#489) PR-A humanic dispatch attribution).

        When the sender of an inbox item differs from the prior turn's
        sender, emit a state_change history entry so the LLM reads
        "[context shift] Now responding to <X> via <transport>.
        Previous turn was from <Y>." before processing the new turn.
        Without this, merged-inbox multi-consumer dispatch produces a
        confused linear feed where the LLM can't tell who's talking.

        ``sender`` convention (= envelope shape):
          - ``user:tui`` / ``user:web`` / ``user:cli`` — local human user
          - ``slack:<user_id>[:<display_name>]`` — Slack consumer
          - ``line:<user_id>[:<display_name>]`` — LINE consumer
          - ``cron:<job_name>`` — scheduled fire
          - ``a2a:<peer_agent>`` — peer-agent message
          - ``webhook:<source>`` — external event source (= Phase 2)

        Payloads without a ``sender`` field are dispatched unchanged
        (= backward compat for existing inbox producers that haven't
        adopted the convention yet). No state_change is emitted in
        that case; ``self._last_sender`` is unchanged.
        """
        if not isinstance(payload, dict):
            return
        # FP-0041 #489 PR-D2: capture reply_to from payload regardless
        # of sender transition (= even a same-sender follow-up may have
        # a new reply_to, e.g. different Slack thread). When the payload
        # doesn't carry reply_to, the previous value is preserved (=
        # downstream interceptor handles the "no reply_to" case by
        # falling through to the default surface).
        reply_to = payload.get("reply_to")
        if reply_to is not None:
            self._last_reply_to = reply_to
        new_sender = payload.get("sender")
        if not new_sender or not isinstance(new_sender, str):
            return
        if new_sender == self._last_sender:
            return
        prev_label = _format_sender_label(self._last_sender)
        new_label = _format_sender_label(new_sender)
        if self._last_sender is None:
            summary = (
                f"[context shift] Now responding to {new_label}. "
                f"This is the first attributed turn this session."
            )
        else:
            summary = (
                f"[context shift] Now responding to {new_label}. "
                f"Previous turn was from {prev_label}."
            )
        try:
            self.notify_state_change(summary, source="dispatch_attribution")
        except Exception:
            # Defensive: attribution emission must not crash dispatch.
            pass
        self._last_sender = new_sender

    def last_sender(self) -> str | None:
        """Return the most-recently-attributed sender label or None if no
        message has been routed yet. Read-only accessor for
        ``_last_sender`` — write side stays internal to the dispatch
        attribution path."""
        return self._last_sender

    def _on_chat_event_for_state_change(self, event) -> None:
        """Generic events-log subscriber that converts known emitter events
        to ``state_change`` history entries (= #398 v4 emitter family).

        The chat router's ``OpContext.events`` is bound to this session's
        ``_chat_events`` (= session.py make_router_op_context). When the
        LLM invokes an op like ``mcp_install`` and the op emits its
        success event, this subscriber sees it and mints the
        corresponding state_change so the LLM's next turn sees the
        world-state change without a separate plumbing path per
        emitter.

        Extension shape (= one dict entry per new emitter):
          ``_STATE_CHANGE_EVENT_MAPPINGS[event_type] = (source, template)``
        where ``template`` is a ``str.format``-compatible string and
        receives the event's ``data`` dict as kwargs. New emitters
        only need to (a) emit a known event type on the chat events
        log and (b) register their (source, template) in the mapping.

        Defensive: malformed event payloads (= missing template keys,
        wrong types) are silently skipped — observability must not
        crash the events bus or downstream subscribers.
        """
        mapping = _STATE_CHANGE_EVENT_MAPPINGS.get(getattr(event, "type", ""))
        if mapping is None:
            return
        source, template = mapping
        try:
            summary = template.format(**(event.data or {}))
        except (KeyError, ValueError, AttributeError):
            return
        self.notify_state_change(summary, source=source)

    def _on_permission_persisted(self, key: str, approved: bool) -> None:
        """PermissionResolver subscriber — convert grant/revoke to a
        ``state_change`` history entry (= #398 v4 emitter wiring,
        #352 in-context-learning refusal trap mitigation).

        The LLM reading the next turn's prompt sees this as a
        ``role="system"`` entry containing "Permission for '<key>' was
        granted." (or revoked) — breaking out of the prior-refusal
        learning pattern by surfacing the world-state change.

        Phrasing uses single quotes around the key so the human-
        readable summary stays unambiguous when the key contains
        dots / colons (= common in Reyn approval keys like
        ``mcp.servers.sqlite`` or ``file.write:/path``).
        """
        verb = "granted" if approved else "revoked"
        summary = f"Permission for '{key}' was {verb}."
        self.notify_state_change(summary, source="permission_manager")

    def notify_state_change(
        self, summary: str, *, source: str | None = None,
    ) -> None:
        """Emit a state-change event as a first-class chat history entry
        (#398 v4 design contract, 2026-05-22 frozen).

        Used by Reyn-internal modules (= permission_manager, mcp_install,
        config_watcher, sp_loader, ...) to tell the LLM that the world
        outside its turn-by-turn view has changed — e.g. a permission
        was granted, a new MCP server installed, config edited. Without
        this signal the LLM is locked into in-context learning from
        prior turns (= #352 refusal trap pattern).

        Storage shape:
          - ``role="system"`` — per user judgment "むやみに増やすべきでない、
            system あるならそれで" (= no new role values, reuse existing
            system role for LLM-wire compatibility).
          - ``meta.kind="state_change"`` — distinguishes from genuine
            system-prompt history entries; downstream consumers (TUI,
            replay, future compactor) dispatch on this. ``meta`` is an
            annotation, not a role — adding it doesn't violate the
            "don't add new roles" rule.
          - ``meta.source=<emitter>`` — optional emitter identity for
            audit / debugging (= e.g. "permission_manager"). When None,
            the meta key is omitted to keep the storage minimal.

        Compaction behaviour (= #398 v4 Q3 decision):
          state_change entries are NOT consumed by ``chat_compactor``
          (= CompactionController filters ``role in ("user","agent")``;
          system-role entries are never candidates). Per-event
          preservation is implicit. Phase 2 trigger for threshold-based
          collapse activates when measurement shows real history bloat.

        Audit cross-ref (= #398 v4 Q4 decision):
          No ``meta.event_log_seq`` back-link. The underlying state
          change is already in ``events.jsonl`` (= each emitter has its
          own audit event there); timestamp + source correlation
          suffices for forensic replay without bloating chat history.

        Emission API surface (= #398 v4 Q2 decision):
          Single method, no builder. Batched emission is a Phase 2
          consideration if measurement shows N-per-call patterns.

        Parameters
        ----------
        summary:
            Human-readable one-line state change (= what the LLM reads).
            Example: ``"Permission for mcp.sqlite was granted."``,
            ``"MCP server 'github' was installed."``,
            ``"Reyn configuration was updated."``.
        source:
            Optional emitter identifier (= module / subsystem name).
            Stored on ``meta.source`` for audit. Not LLM-visible —
            the LLM reads only ``summary`` text.
        """
        meta: dict = {"kind": "state_change"}
        if source:
            meta["source"] = source
        msg = ChatMessage(
            role="system",
            content=summary,
            ts=_now_iso(),
            meta=meta,
        )
        self._append_history(msg)
        # Observability event for measurement / debugging (= sub-task 6
        # measurement pipeline can count state_change emission frequency
        # by source without scraping the chat history).
        try:
            self._chat_events.emit(
                "state_change_notified",
                summary=summary,
                source=source or "",
            )
        except Exception:
            # Defensive: observability must not crash the API.
            pass

    def _append_history_for_handler(
        self, role: str, text: str, ts: str, meta: dict,
    ) -> None:
        """Adapter callback injected into InterventionHandler.

        InterventionHandler needs to append a user history entry when an
        intervention is answered.  This adapter bridges the handler's
        ``(role, text, ts, meta)`` signature to ChatSession._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(
            role="assistant" if role == "agent" else role,
            content=text, ts=ts, meta=meta,
        ))

    def _append_history_for_a2a_handler(
        self, role: str, text: str, ts: str, meta: dict,
    ) -> None:
        """Adapter callback injected into A2AHandler.

        A2AHandler uses the same ``(role, text, ts, meta)`` signature as
        InterventionHandler.  This adapter bridges to ChatSession._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(
            role="assistant" if role == "agent" else role,
            content=text, ts=ts, meta=meta,
        ))

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
                    raw = json.loads(line)
                    # Read-time migration for pre-#383 entries (legacy
                    # text + media shape → new content shape).
                    raw = _migrate_legacy_chat_message(raw)
                    self.history.append(ChatMessage(**raw))
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
        # #398 v4 emitter wiring cleanup: unregister the
        # permission-persist subscriber so dead-session references
        # don't accumulate in the shared PermissionResolver. Defensive
        # — the resolver may have been replaced or never have had the
        # method; in either case unregister is a no-op.
        if self._on_perm_persist_cb is not None and self._perm is not None:
            try:
                self._perm.unregister_on_persist(self._on_perm_persist_cb)
            except Exception:
                pass
            self._on_perm_persist_cb = None
        await self.inbox.put(("shutdown", {}))

    # ── PR21: state persistence helpers (WAL + snapshot) ─────────────────────
    # PR-refactor-session-1 wave 2: WAL/snapshot ownership moved to
    # SnapshotJournal; pending_chains lifecycle moved to ChainManager.
    # The methods below are thin delegators kept for the session-internal
    # call sites (inbox enqueue + dequeue, restoration orchestration).

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        """Append `inbox_put` to WAL via journal, then queue on the async
        inbox. Returns the assigned message id (also stamped into payload
        as `_msg_id` so the consumer can look it up).

        **Internal API — plugin authors should NOT call directly**
        (FP-0041 plugins-api). Use ``reyn.plugins.api.push_to_agent``
        instead; this signature may change between Reyn versions.
        Other internal Reyn modules (= A2AHandler, MCP handler,
        InterventionHandler, ChatLifecycleForwarder) keep calling
        this directly because they manage their own additional state
        machines (= chain_id / request_id / etc.) on top.
        """
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
        # FP-0041 (#489) PR-A: humanic dispatch attribution.
        # If this inbox item carries a ``sender`` (= new envelope
        # convention: who/what produced this message — e.g.
        # ``user:tui`` / ``slack:U456:bob`` / ``cron:morning_news`` /
        # ``a2a:peer_agent``) and it differs from the prior turn's
        # sender, surface the transition to the LLM as a state_change
        # history entry. Makes the multi-consumer (humanic) model
        # explicit: the agent knows "I was just talking to Alice via
        # cron, now Bob from Slack just said something" instead of
        # seeing a confused linear feed.
        self._handle_sender_attribution(payload)
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

        # Issue #366 → #383: drain any /image-queued media blocks onto
        # this turn. Each block is a content-part dict (= image_url or
        # image path-ref shape); when present, the user message becomes
        # list-content shape mirroring the LLM wire format.
        attached_media = self._pending_user_images
        self._pending_user_images = []

        if attached_media:
            content: str | list[dict] = (
                ([{"type": "text", "text": text}] if text else []) + attached_media
            )
        else:
            content = text

        self._append_history(ChatMessage(
            role="user", content=content, ts=_now_iso(),
            meta={"chain_id": chain_id},
        ))
        self._chat_events.emit(
            "user_message_received", text=text, chain_id=chain_id,
            media_block_count=len(attached_media),
        )
        await self._put_outbox(OutboxMessage(
            kind="status", text="thinking…", meta={"chain_id": chain_id},
        ))

        # Reset the per-turn router cap counter at the top of each fresh
        # user turn. Subsequent in-chain re-invocations (agent_response on
        # this chain, _resolve_pending_chain) accumulate against the same
        # budget without resetting.
        self._reset_router_turn_counter()

        # FP-0037 S2: check whether any of the 3 yaml scope tier files changed
        # since the last turn. If so, re-probes MCP servers + writes the cache
        # file so the disk-reload step below picks it up. No-op on first call
        # (seeds mtime table without probing). Called BEFORE
        # maybe_reload_mcp_tools_cache_from_disk so yaml-triggered cache writes
        # are already on disk when the disk-reload step runs.
        await self._router_host.maybe_refresh_mcp_tools_from_yaml()

        # FP-0037 S1: check whether the operator ran `reyn mcp refresh`
        # since the last turn. Reloads the in-memory tools cache from disk
        # when the cache file mtime has advanced. No-op on first turn (file
        # absent or mtime unchanged). Called BEFORE ensure_mcp_tools_cached
        # so a refresh written between session start and first turn is still
        # visible on turn 1.
        self._router_host.maybe_reload_mcp_tools_cache_from_disk()

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

    def get_skill_registry(self) -> "SkillRegistry | None":
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

    def get_plan_registry(self) -> "Any":
        """Return the per-agent PlanRegistry, lazily constructed on first call.

        ADR-0023 Phase 2 + ADR-0025: per-plan snapshots persist
        alongside SnapshotJournal's WAL-side bookkeeping. Without this
        registry hook, ADR-0023 forward replay has nothing to read on
        resume (PlanRegistry.load_active() returns empty), and ADR-0025
        sub-loop LLM memoization has nowhere to record.

        Returns None when no state_log is wired — test / standalone
        mode without persistence.

        Truncate hook mirrors get_skill_registry: fires
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
        intervention_bus: RequestBus | None = None,
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
            multimodal_config=self._multimodal_config,
            media_store=self._media_store,
        )

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        """Drop transient kinds while detached; durable kinds are queued.

        While `is_attached=False` (PR10 multi-agent: agent running in the
        background), `status`/`trace` carry no value to a detached display
        and would just accumulate in the queue. `agent`/`skill_done`/
        `intervention`/`error`/`__end__` are kept so they reach the user
        when re-attached or remain in history (history append happens
        independently in callers).

        FP-0041 #489 PR-D2: outbox reply_to + external transport interceptor.
          - When ``msg.reply_to`` is unset and the session has a recent
            inbox-captured ``_last_reply_to``, the outbox message
            inherits it (= so the agent's reply automatically routes
            back to the producer's transport without each emit site
            needing to know).
          - When ``msg.reply_to`` is an external transport (=
            ``ExternalRef``) and an outbox interceptor is registered,
            the interceptor is invoked. If it returns ``True``, the
            message is treated as fully handled (= dispatched to e.g.
            Slack via MCP) and NOT queued for TUI display. If it
            returns ``False`` or raises, the message falls through to
            the normal queue path (= defensive: a failed external
            dispatch surfaces to TUI rather than silently disappearing).
        """
        # PR-D2: default reply_to from last captured inbox reply_to.
        if msg.reply_to is None and self._last_reply_to is not None:
            from dataclasses import replace
            msg = replace(msg, reply_to=self._last_reply_to)
        # PR-D2: external transport interceptor.
        if self._outbox_interceptor is not None:
            from reyn.chat.transport import ExternalRef
            if isinstance(msg.reply_to, ExternalRef):
                try:
                    handled = await self._outbox_interceptor(msg)
                except Exception:
                    logger.exception(
                        "outbox interceptor raised for reply_to=%r; "
                        "falling through to queue", msg.reply_to,
                    )
                    handled = False
                if handled:
                    return
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

    def _uncompacted_tool_call_records(self) -> list[tuple[str, float]]:
        """Return ``(qualified_name, ts_epoch)`` records from the
        portion of history that has NOT yet been folded into a
        compactor summary.

        Used by RouterLoop to build the hot-list each turn without
        relying on a parallel per-call write log; the compacted table
        in :class:`~reyn.tools.action_usage_tracker.ActionUsageTracker`
        already covers the older portion. The watermark is the latest
        summary's ``covers_through_seq`` (= seqs at or below it are
        considered compacted out).
        """
        latest = self._latest_summary()
        watermark = (
            int((latest.meta or {}).get("covers_through_seq", 0))
            if latest is not None else 0
        )
        live = [m for m in self.history if m.seq > watermark]
        return _extract_tool_call_records(live)

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
            role="assistant", content=fallback, ts=_now_iso(),
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
    def router_invocations_this_turn(self) -> int:
        return self._budget._router_invocations_this_turn

    @router_invocations_this_turn.setter
    def router_invocations_this_turn(self, value: int) -> None:
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

    async def _deliver_answer_to(
        self,
        iv: UserIntervention,
        text: str,
        *,
        choice_id_override: str | None = None,
    ) -> bool:
        """Thin wrapper → InterventionHandler.deliver_answer_to.

        ``choice_id_override`` is forwarded so peer-side callers (= A2A
        POST answer with explicit choice_id per PR #285 Gap 4) can bypass
        the TUI's text-based match_choice. issue #292 (α).
        """
        return await self._intervention_handler.deliver_answer_to(
            iv, text, choice_id_override=choice_id_override,
        )

    async def answer_pending_intervention(
        self,
        run_id: str,
        answer: "InterventionAnswer",
    ) -> bool:
        """Deliver ``answer`` to the outstanding intervention for ``run_id``.

        Authoritative entry point for peer answer delivery (= A2A POST
        ``{task_id, answer}`` → ``_handle_answer_injection`` → here).
        issue #292 (α): replaces the pre-#292
        ``RunRegistry.answer_intervention`` path. Under α, the A2A
        override is a side-effect observer and the iv lives in
        ``_interventions._active`` like a TUI iv, so the answer
        delivery uses the same handler path the TUI uses
        (``deliver_answer_to``). R-D12's persistent answer buffer
        applies automatically.

        Looks up the iv by ``run_id`` in active interventions; for
        the peer-answer case there's typically one iv per run (skills
        await serially). Delegates to the handler so history +
        ``user_answered_intervention`` event + outbox cleanup all fire
        the same way as TUI answers — observers on the audit trail
        see a consistent shape regardless of answer origin.

        Returns True when the future was resolved; False for unknown
        run_id, already-answered iv, malformed ``choice_id``, or no
        matching iv. Callers translate False into a
        ``{"answered": false, "reason": ...}`` peer response.
        """
        for iv in self._interventions.list_active():
            if iv.run_id != run_id:
                continue
            if iv.future.done():
                return False
            return await self._deliver_answer_to(
                iv,
                answer.text,
                choice_id_override=answer.choice_id,
            )
        return False

    async def _announce_intervention(self, iv: UserIntervention) -> None:
        """Thin wrapper → InterventionHandler.announce."""
        await self._intervention_handler.announce(iv)

    def register_intervention_override(self, chain_id: str, bus: "RequestBus") -> None:
        """Register a ``RequestBus`` for ask_user prompts emitted by
        skills spawned under this chain_id. Caller must pair with
        ``unregister_intervention_override`` in a try/finally.

        Issue #254 Phase 5: parameter type tightened from the legacy
        ``InterventionBus`` alias to the canonical ``RequestBus`` name —
        functionally identical since the alias points at the same
        Protocol, but the type hint now reflects the OS↔Agent contract
        layer the override participates in.
        """
        self._intervention_overrides[chain_id] = bus

    def unregister_intervention_override(self, chain_id: str) -> None:
        """Remove an override. Idempotent."""
        self._intervention_overrides.pop(chain_id, None)

    def has_intervention_override(self, chain_id: str) -> bool:
        """Return True iff *chain_id* currently has a registered override
        ``RequestBus``. Public read-side counterpart to
        ``register_intervention_override`` / ``unregister_intervention_override``.
        """
        return chain_id in self._intervention_overrides

    def get_intervention_override(self, chain_id: str) -> "RequestBus | None":
        """Return the override bus for *chain_id* or None if absent. Read-only
        accessor for callers (= primarily tests) that need to confirm the
        registered bus identity without consuming the override."""
        return self._intervention_overrides.get(chain_id)

    def intervention_override_count(self) -> int:
        """Return the number of currently-registered overrides. Public
        emptiness probe for cleanup-leak tests (= prefer over reading the
        private mapping directly)."""
        return len(self._intervention_overrides)

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
        """Remove *listener_id* from the active set. Idempotent.

        issue #268 Phase 1: when a listener closes (= channel goes
        away), any iv whose ``origin_channel_id`` equals this
        ``listener_id`` will be observable in the stalled queue via
        ``list_stalled_interventions``. The unregister itself does
        NOT move active ivs to stalled — only the next
        ``handle_intervention`` call (= a fresh iv from a still-running
        skill) sees the change. For existing in-flight ivs that lose
        their origin, the agent layer handles them through
        ``handle_intervention``'s origin-pin check on its next pass.
        """
        self._interventions.unregister_listener(listener_id)

    # ── Cross-channel pending-op operations (issue #268 Phase 1) ──────────

    def list_stalled_interventions(self) -> "list[PendingOpView]":
        """Return a snapshot of all stalled interventions.

        issue #268 Phase 1: any channel can call this to inspect the
        agent's outstanding interventions whose origin channel closed.
        The returned ``PendingOpView`` items carry enough info for the
        TUI Pending tab + slash command to render + dispatch
        discard/claim operations without exposing the underlying
        ``UserIntervention`` object (= internal-only).

        Read-only — caller iterates the returned list without holding
        any registry-internal collection.
        """
        return [
            PendingOpView.from_intervention(iv)
            for iv in self._interventions.list_stalled()
        ]

    def is_intervention_stalled(self, iv_id: str) -> bool:
        """Return True iff ``iv_id`` is in the stalled queue.

        issue #268 Phase 1: point-in-time membership test for the
        stalled queue. Callers (TUI, tests, CLI) can use this instead
        of reading ``_interventions._stalled`` directly. The stalled
        queue is immutable from the caller's perspective — only
        ``_dispatch_intervention``, ``discard_pending_intervention``,
        and ``claim_stalled_intervention`` change membership.
        """
        return self._interventions.get_stalled(iv_id) is not None

    async def discard_pending_intervention(
        self, iv_id: str, *, reason: str = "user_discarded",
    ) -> bool:
        """Discard a stalled intervention — cancel its future, remove
        from the queue.

        Returns True iff the iv was in the stalled queue and was
        discarded. The future is resolved with an empty
        ``InterventionAnswer`` so the awaiter sees a refusal.

        issue #268 Phase 1: cross-channel discard. Used by a different
        channel than the original origin to say "no one will answer,
        give up". Future expansion (= per-kind discard hooks) can
        plug into the policy layer at ``handle_intervention``.
        """
        ok = self._interventions.discard_stalled(iv_id)
        if ok:
            self._chat_events.emit(
                "pending_intervention_discarded",
                iv_id=iv_id,
                reason=reason,
            )
        return ok

    async def claim_pending_intervention(
        self, iv_id: str, new_channel_id: str,
    ) -> "PendingOpView | None":
        """Claim a stalled intervention — rebind origin to the caller's
        channel + re-dispatch through the active path.

        Returns the ``PendingOpView`` of the claimed iv on success, or
        ``None`` when ``iv_id`` is not in the stalled queue.

        issue #268 Phase 1: cross-channel claim. The caller takes
        responsibility for resolving the iv via its own input
        surface. After claim:
          - iv.origin_channel_id is updated to ``new_channel_id``
          - the iv is removed from the stalled queue
          - the dispatch path runs (= `_dispatch_intervention`)
        """
        iv = self._interventions.claim_stalled(iv_id, new_channel_id)
        if iv is None:
            return None
        self._chat_events.emit(
            "pending_intervention_claimed",
            iv_id=iv_id,
            new_origin_channel_id=new_channel_id,
        )
        # Re-dispatch on the new channel. We schedule this in the
        # background so the caller of claim doesn't await the full
        # iv resolution — the iv.future will resolve when the new
        # channel's listener calls deliver_answer, independent of this
        # method's return.
        asyncio.ensure_future(self._dispatch_intervention(iv))
        return PendingOpView.from_intervention(iv)

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Thin wrapper → InterventionHandler.dispatch.

        ChatInterventionBus, _handle_chat_limit_checkpoint, and
        _ask_budget_extension all call this method directly; keeping it
        as a session-level entry keeps those call sites stable.

        issue #268 Phase 2 continuation: the origin-pin check (= stall
        when iv.origin_channel_id is set but the listener has gone)
        lives here so it fires for ALL iv flows, not just the
        handle_intervention path. The check sits AFTER the chain-override
        notification so A2A peers still see the iv's input-required
        signal even if the local TUI listener is absent.

        issue #292 (α): chain overrides now run as **side-effect
        observers** (= notify A2A peer surfaces, write history, post
        webhook) BEFORE the regular dispatch path, instead of replacing
        it. The iv always flows through ``InterventionHandler.dispatch``
        so it lands in ``_interventions._active`` + WAL +
        ``outstanding_interventions`` + becomes eligible for R-D12's
        persistent answer buffer. Pre-#292, the override replaced
        dispatch and A2A ivs were invisible to ChatSession's iv
        machinery — fixed structurally here, not patched.
        """
        # FP-0001 / issue #292: chain_id-scoped override notification
        # (A2A async tasks). The override is now an OBSERVER that runs
        # side effects (= webhook / SSE / RunRegistry status mirror)
        # alongside the normal dispatch — NOT a bypass replacement.
        # Notify before dispatch so the peer learns input-required
        # before the awaiter (= handler.dispatch) blocks the iv future.
        if iv.run_id is not None and self._intervention_overrides:
            chain_id = self.running_skills_chain.get(iv.run_id)
            if chain_id is not None:
                override = self._intervention_overrides.get(chain_id)
                if override is not None:
                    try:
                        await override.on_dispatch(iv)
                    except Exception:  # noqa: BLE001 — side effects are best-effort
                        # Override notification must NOT block dispatch.
                        # A failed webhook / SSE append / status mirror
                        # is logged elsewhere; dispatch proceeds.
                        logger.exception(
                            "intervention override on_dispatch raised "
                            "(chain_id=%s iv_id=%s)", chain_id, iv.id,
                        )
        # issue #268 Phase 2 continuation: origin-pin stall check.
        # When the iv carries an explicit ``origin_channel_id`` whose
        # listener is no longer present (= the origin channel closed
        # mid-call), park the iv in the stalled queue instead of
        # delivering to a fall-through listener. Other channels
        # observe / claim / discard via the cross-channel API. ivs
        # without ``origin_channel_id`` (= legacy default or
        # override-active spawn) skip this check.
        if (
            iv.origin_channel_id is not None
            and iv.origin_channel_id not in self._interventions._listeners
        ):
            self._chat_events.emit(
                "intervention_routed",
                route="user_channel_stalled",
                iv_kind=iv.kind,
                iv_id=iv.id,
                origin_channel_id=iv.origin_channel_id,
            )
            self._interventions._stalled[iv.id] = iv
            try:
                return await iv.future
            except asyncio.CancelledError:
                return InterventionAnswer(text="")
        # Default: route through the regular InterventionHandler.
        return await self._intervention_handler.dispatch(iv)

    # ── Agent-layer intervention entry point (issue #254 Phase 3) ───────────

    async def handle_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Agent-layer entry point for incoming intervention requests.

        This is the Agent's ``RequestBus`` subscriber-side handler.
        Phase 4 implements the 3-way routing decision the Agent makes
        on every incoming request:

          1. **self_answer** (= ``try_self_answer`` hook): the agent
             has a policy that answers without consulting the user
             (e.g. "I've already extended this limit 5 times, refuse").
             Default policy is None — no self-answer — so the request
             falls through. Future incremental PRs add per-kind
             policies (e.g. "max_phase_visits hit + N prior extensions
             → refuse silently") via subclassing or config-driven
             policy injection.
          2. **parent_agent.delegate** (= ``resolve_parent_agent`` hook):
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
        self_ans = await self.try_self_answer(iv)
        if self_ans is not None:
            self._chat_events.emit(
                "intervention_routed",
                route="self_answer",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            return self_ans

        # Branch 2: parent-agent delegation.
        parent = self.resolve_parent_agent(iv)
        if parent is not None:
            self._chat_events.emit(
                "intervention_routed",
                route="parent_delegate",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            # Issue #261 — stamp this agent as the source of the
            # delegation so the parent's downstream ``user_channel``
            # path can surface it on the outbox meta. Token-based
            # set/reset preserves any outer-scope value (= multi-hop
            # chains overwrite the immediate parent on each hop, then
            # restore on return so the original caller's view is
            # unchanged).
            from reyn.chat.services.intervention_handler import (
                source_agent_var,
            )
            token = source_agent_var.set(self.agent_name)
            try:
                return await parent.handle_intervention(iv)
            finally:
                source_agent_var.reset(token)

        # Branch 3: user_channel — emit route decision + delegate to
        # ``_dispatch_intervention``. issue #268 Phase 2 continuation
        # moved the origin-pin stall check INTO ``_dispatch_intervention``
        # so it fires uniformly for the bus-emit path too (= skill
        # ask_user via ChatInterventionBus.deliver bypasses
        # ``handle_intervention``); when the check fires, it emits its
        # own ``user_channel_stalled`` event so the audit trail remains
        # decisive (= one event per actual outcome).
        self._chat_events.emit(
            "intervention_routed",
            route="user_channel",
            iv_kind=iv.kind,
            iv_id=iv.id,
        )
        return await self._dispatch_intervention(iv)

    async def try_self_answer(
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

    def resolve_parent_agent(
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

    def consume_buffered_intervention_answer(
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
        # router SP's TASK_COMPLETED rule can match on ``[task_completed]``
        # reliably. JSON-encode ``data`` so the LLM sees the actual fields
        # (= avoids lossy string coercion).
        #
        # B49 W1-S6 fix (2026-05-22): the "task" abstraction unifies
        # skill-run and plan completions (= same label, same SP handler,
        # ``kind=`` field disambiguates). The prescriptive "summarize in
        # 1-2 sentences" trailer was the dominant signal that drove the
        # LLM to drop substantive content from its reply (= W1-S6 had
        # direct_llm returning code that the LLM compressed away). Per
        # the SP design principle "SP conveys meaning, LLM decides
        # handling", the injection now carries only meaning fields; the
        # SP TASK_COMPLETED rule explains what they mean and the LLM
        # decides how to narrate.
        try:
            data_str = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            data_str = repr(data)
        injected_text = (
            f"[task_completed] kind=skill run_id={run_id} chain_id={chain_id}\n"
            f"skill: {skill_name}  status: {status}\n"
            f"result: {data_str}"
        )

        self._append_history(ChatMessage(
            role="user", content=injected_text, ts=_now_iso(),
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
            # Suggest the 3 closest matches rather than dumping the full
            # 20+ command catalog into the ErrorBox header (= the box caps
            # at 72 cells and the previous list truncated mid-name at
            # ``try: /agent, /agents, /answer, /attach,``, hiding the
            # actionable tail). ``suggest_for_unknown`` is a pure helper
            # in ``reyn.chat.slash`` so the suggestion contract is
            # directly testable without the surrounding session machinery.
            from reyn.chat.slash import suggest_for_unknown
            suggestions = suggest_for_unknown(cmd)
            known = ", ".join(f"/{n}" for n in suggestions)
            # ``kind="error"`` so the TUI mounts an ErrorBox (= red ✗ icon,
            # collapsible, Esc-to-dismiss). The previous ``kind="system"``
            # rendered as a dim grey line indistinguishable from a successful
            # slash-command reply — a typo'd command silently looked OK.
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"unknown command /{cmd}; try: {known}",
            ))
            return True
        # Wave-13 T2-3: recall hint — detect if the handler emitted an error
        # and surface a one-shot "↑ to recall" sticky so the user can
        # re-edit instead of retyping from scratch.  We snapshot the queue
        # size before the call and inspect only the new items afterwards via
        # ``outbox._queue[pre_size:]`` (= asyncio.Queue internal deque slice;
        # read-only, best-effort — the try/except ensures a CPython internals
        # change never breaks slash dispatch).
        pre_size = self.outbox.qsize()
        await slash_cmd.handler(self, args)
        try:
            new_items = list(self.outbox._queue)[pre_size:]  # type: ignore[attr-defined]
            had_error = any(
                getattr(item, "kind", None) == "error" for item in new_items
            )
        except Exception:
            had_error = False
        if had_error:
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"↑ to recall `/{cmd}`",
                meta={"source": "slash_recall_hint"},
            ))
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
        [task_completed] user-role message (kind=plan) into history so
        the router LLM sees step_results and synthesises a user reply.
        """
        # B49 W1-S6 fix (2026-05-22): unified "task" abstraction with
        # skill completion. Both emit [task_completed] with kind= field
        # for disambiguation; the SP TASK_COMPLETED rule covers both via
        # the meaning of status + result fields, no prescriptive
        # "summarize in 1-2 sentences" or "synthesize the step results"
        # trailer (= those were handling prescriptions that pre-empted
        # the LLM's own judgment).
        plan_id = payload.get("plan_id", "")
        chain_id = payload.get("chain_id") or _new_chain_id()
        goal = payload.get("goal", "")
        step_results = payload.get("step_results") or {}
        step_failures = payload.get("step_failures") or {}
        try:
            results_str = json.dumps(step_results, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            results_str = repr(step_results)
        # status='finished' is the implicit success state for plans; if any
        # step failed, that surfaces via step_failures + the LLM sees both.
        injected_text = (
            f"[task_completed] kind=plan plan_id={plan_id} chain_id={chain_id}\n"
            f"goal: {goal}  status: {'failed' if step_failures else 'finished'}\n"
            f"step_results:\n{results_str}"
        )
        if step_failures:
            try:
                failures_str = json.dumps(step_failures, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                failures_str = repr(step_failures)
            injected_text += f"\n\nstep_failures:\n{failures_str}\n"
        self._append_history(ChatMessage(
            role="user", content=injected_text, ts=_now_iso(),
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

        # #571 collapse arc Phase 5: the legacy ``mcp_install`` /
        # ``index_drop`` bool axes are replaced with the explicit list
        # axes the corresponding op handlers now consume. The chat
        # router declares the canonical mutation paths + the registry
        # host so LLM-emitted mcp_install / index_drop / mcp_drop_server
        # ops pass the OS's uniform permission gates without per-op
        # prompts (= operator-level config / session approvals carry
        # the authorisation).
        file_write = list(file_write) + [
            {"path": ".reyn/mcp.yaml", "scope": "just_path"},
            {"path": ".reyn/cron.yaml", "scope": "just_path"},
            {"path": ".reyn/index/sources.yaml", "scope": "just_path"},
        ]
        decl = PermissionDecl(
            file_read=file_read,
            file_write=file_write,
            mcp=mcp_names,
            allowed_mcp=self._allowed_mcp,
            # #571 Phase 7: wildcard http.get authorises LLM-driven
            # ``web_fetch`` ops to any host via a per-host 4-layer
            # prompt at runtime — unifies the safe.http + web_fetch
            # paths under a single ``http.get`` axis. The MCP registry
            # host is also listed specifically so mcp_install's
            # startup_guard pre-approves it without an extra prompt.
            http_get=[
                {"host": "registry.modelcontextprotocol.io"},
                {"host": "*"},
            ],
            # #571 Phase 6: wildcard secret.write authorises LLM-emitted
            # mcp_install ops to save runtime-determined ``isSecret`` env
            # vars from the registry — the actual security gate is the
            # operator's per-value prompt at op-execution time.
            secret_write=["*"],
        )
        # Session-approve the canonical OS mutation paths so the runtime
        # require_file_write check passes silently for LLM-emitted ops
        # in the chat router context. Skipped when no resolver is wired
        # (= ad-hoc test contexts that pass permission_resolver=None).
        if self._perm is not None:
            for canonical in (".reyn/mcp.yaml", ".reyn/cron.yaml", ".reyn/index/sources.yaml"):
                self._perm.session_approve_path(canonical, "chat_router", "file.write")

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
        """Invoke an MCP tool and return its result.

        Close the per-call MCP clients in the same task that opened
        them — the MCP SDK's ``stdio_client`` uses anyio cancel scopes
        that are task-affine, and leaving them open until asyncio loop
        teardown produces a "cancel scope crossed task boundary"
        RuntimeError (= recurring crash on every chat session end
        observed during the 2026-05-20 8-server smoke round).

        Per-call open/close is fine for now: the chat router invokes
        MCP tools individually and there's no value in caching across
        unrelated tool calls. A future optimisation could pool clients
        per-server within a single chat-turn act batch, but the same-
        task-close discipline still applies.
        """
        from reyn.op_runtime import execute_op
        from reyn.permissions.permissions import PermissionDecl
        from reyn.schemas.models import MCPIROp

        op = MCPIROp(kind="mcp", server=server, tool=tool, args=args)
        ctx = self._make_router_op_context()
        # MCP handler requires intervention_bus; wire the session's bus
        ctx.intervention_bus = ChatInterventionBus(
            self, run_id=None, skill_name="chat_router",
            channel_id=DEFAULT_CHAT_CHANNEL_ID,
        )
        # Narrow mcp scope to just this server while preserving file perms from the
        # populated decl. PermissionDecl.mcp must include the server for require_mcp to pass.
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        try:
            return await execute_op(op, ctx, caller="control_ir")
        finally:
            # Close every MCPClient that op_runtime/mcp.py cached on
            # ``ctx.mcp_clients`` during this call. Failures are
            # swallowed — best-effort; the op result is already
            # captured at this point.
            for client in list(ctx.mcp_clients.values()):
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    logger.debug(
                        "mcp client close failed for chat router",
                        exc_info=True,
                    )
            ctx.mcp_clients.clear()

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
        # E-full (#383): include tool-turn entries (= assistant w/ tool_calls,
        # tool responses) in the slice. The wire-shape builder below
        # forwards them as-is to the LLM. ``summary`` / ``skill_event``
        # remain Reyn-internal and filtered out.
        turns = [
            m for m in self.history
            if m.role in ("user", "assistant", "tool", "agent")
        ]

        if len(turns) <= cfg.head_size + cfg.tail_size:
            # No compaction needed — head+tail would overlap and duplicate.
            selected = turns
        else:
            # Head+tail with optional summary bridge for the elided middle.
            head = turns[:cfg.head_size]
            tail = turns[-cfg.tail_size:] if cfg.tail_size else []
            summary = self._latest_summary()
            if summary:
                summary_text = (
                    summary.content if isinstance(summary.content, str)
                    else json.dumps(summary.content, ensure_ascii=False)
                )
                bridge = [ChatMessage(
                    role="assistant",
                    content=f"[summary of earlier conversation]\n{summary_text}",
                    ts=summary.ts,
                )]
                selected = head + bridge + tail
            else:
                selected = head + tail

        # E-full (#383) pass-through: ChatMessage IS the wire shape, so the
        # builder just serialises each entry into a litellm-compatible
        # message dict. Path-ref content parts (= ``{"type":"image","path":...}``)
        # are materialised to data URLs **at this boundary** so storage
        # stays light and the LLM sees the inline form it expects.
        messages: list[dict] = []
        media_store = getattr(self, "_media_store", None)
        for m in selected:
            # Legacy "agent" stragglers (= migrated entries that somehow
            # bypassed _migrate_legacy_chat_message) → normalise on read.
            role = "assistant" if m.role == "agent" else m.role
            content = _materialise_path_ref_content(m.content, media_store)
            msg: dict = {"role": role, "content": content}
            if m.tool_calls is not None:
                msg["tool_calls"] = m.tool_calls
            if m.tool_call_id is not None:
                msg["tool_call_id"] = m.tool_call_id
            if m.name is not None:
                msg["name"] = m.name
            messages.append(msg)
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
        # B51 NF-W6-3: plan_invalid self-correction cap, sourced from
        # safety.loop.plan_invalid_retries (default 1). When set to 0
        # the retry is disabled and the LLM sees the plain tool error.
        _plan_invalid_retries_cap = getattr(
            getattr(self._safety, "loop", None),
            "plan_invalid_retries",
            1,
        )
        loop = RouterLoop(
            host=self._router_host, chain_id=chain_id, max_iterations=5,
            budget=self._budget_tracker,
            # B43-NF-W6-1: chat router empty-stop retry. Same opt-in
            # mechanic as PR #265's plan-step wiring — directive plumbed
            # unconditionally, runtime gated by ``REYN_EMPTY_STOP_RETRY=1``.
            empty_stop_retry_directive=_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE,
            plan_invalid_retries=_plan_invalid_retries_cap,
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
