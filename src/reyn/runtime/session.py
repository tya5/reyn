"""Session — long-lived chat loop driving the router turn.

See docs/reference/runtime/session-construction.md for __init__ construction
rationale (Family decomposition).
"""
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

from reyn.config import (  # noqa: F401
    ActionRetrievalConfig,
    CostWarnConfig,
    EmbeddingConfig,
    EventsConfig,
    MultimodalConfig,
    OffloadConfig,
    OnLimitConfig,
    RenderTemplateConfig,
    RouterConfig,
    SafetyConfig,
)
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import truncate_anchor as _truncate_anchor
from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.registry import PipelineNotFoundError, PipelineRegistry
from reyn.hooks.dispatcher import HOOK_INBOX_KIND
from reyn.hooks.schema_registry import build_hook_payload
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.agent import Agent
from reyn.runtime.budget.budget import (
    BudgetTracker,
    format_budget_full,
    format_cost_line,
    format_refusal_message,
    format_warn_message,
)
from reyn.runtime.capability_visibility import CapabilityVisibility
from reyn.runtime.chat_message import (  # #312 C1: extracted VO + helpers
    ChatMessage,
    _migrate_legacy_chat_message,
    _now_iso,
)
from reyn.runtime.error_format import classify_router_error
from reyn.runtime.errors import RouterCapExceeded, StructuredOutputError
from reyn.runtime.limits.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import OutboxHub
from reyn.runtime.pending_op_view import PendingOpView
from reyn.runtime.presentation_consumer import OutboxPresentationConsumer
from reyn.runtime.services import (
    BudgetGateway,
    ChainManager,
    CompactionController,
    InterventionCoordinator,
    InterventionHandler,
    InterventionRegistry,
    MemoryService,
    RouterHostAdapter,
    SnapshotJournal,
)
from reyn.runtime.services.chain_manager import _PendingChain
from reyn.runtime.services.execution_driver import ExecutionDriver
from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
from reyn.runtime.services.task_wake import WAKE_READY_KIND, WAKE_REQUESTER_KIND
from reyn.runtime.session_buses import (
    AgentRequestBus,
    AuditOnlyInterventionBridge,
    ChatInterventionBus,
)
from reyn.runtime.session_params import (
    CapabilityScope,
    PresentationWiring,
    ReactivityConfig,
    TaskWiring,
)
from reyn.runtime.spawn_tracker import SpawnTracker
from reyn.security.permissions.permissions import PermissionResolver
from reyn.services.compaction.engine import CompactionEngine
from reyn.task.subscription import SubscriptionWriter
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    RequestBus,
    UserIntervention,
)

# #2115: cap for await_quiescent's re-drain loop. In-flight WAL-append tasks are
# finite + cancel-requested + spawn no new user-work under a rewind, so the drain
# converges in 1-2 rounds; the cap is purely a guard against a pathological spin
# (logged, never silently looped).
_QUIESCE_MAX_ROUNDS = 50

# #2103 S1bc-exec: cap on the in-flight spawned-task correlation record (sid → task) —
# moved to spawn_tracker.py's _MAX_SPAWNED_TASKS (#3133 P3 Extract Class).

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


def _exec_gate_backend_name(sandbox_backend: Any, sandbox_config: Any) -> str | None:
    """#1417: resolve the ``exec`` D14 visibility-gate backend name.

    The ``exec`` category is gated on the ACTUAL exec backend, not the reyn.yaml
    config string. When a sandbox backend INSTANCE is injected (e.g.
    ``--env-backend=docker`` → ``DockerEnvironmentBackend.name == "docker"``),
    its ``.name`` is the gate value — so ``exec`` stays discoverable even with a
    ``sandbox.backend = noop`` config (the construction-forwarding-gap: the
    config string is NOT the live injected instance, and the instance is what
    actually executes via ``sandboxed_exec``). With no injected instance, fall
    back to the config string (``auto`` / host-default behaviour unchanged).

    A defensive ``getattr`` keeps an instance without a ``name`` from raising
    (degrades to None → exec hidden, the safe direction).
    """
    if sandbox_backend is not None:
        return getattr(sandbox_backend, "name", None)
    if sandbox_config is not None:
        return sandbox_config.backend
    return None


# issue #268 Phase 2 continuation: canonical channel identifier for
# chat-side interventions (= matches the listener_id that
# ``ChatTUIApp.on_mount`` registers in src/reyn/interfaces/tui/app.py).
# Production ChatInterventionBus instances stamp ivs with this id so
# the agent layer's origin-pin check + cross-channel observe / claim
# routing work end-to-end for TUI-initiated tasks. Module-level so
# tests can import + assert against a single source of truth.
DEFAULT_CHAT_CHANNEL_ID = "tui"


# B43-NF-W6-1 / #187: the chat router's empty-stop continuation directive is
# now the SHARED uniform ``EMPTY_STOP_RETRY_DIRECTIVE`` ("resume") from
# router_loop.py — see its definition for the owner decision (no per-site
# differentiation). Imported function-locally at the construction site below
# (session→router_loop is a function-local import to avoid the module-level
# cycle: router_loop imports from session).


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
# ``Session._on_chat_event_for_state_change`` to convert events
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
    # MCP server install success (= ``reyn.core.op_runtime.mcp_install``
    # emits this on the events log after writing the config).
    "mcp_server_installed": (
        "mcp_install",
        "MCP server '{server_name}' was installed.",
    ),
    # MCP server removal success (= ``reyn.core.op_runtime.mcp_drop_server``
    # emits this after removing the config entry). Symmetric to
    # mcp_server_installed — surfaces the "no longer available"
    # state-change to the LLM so it doesn't keep trying.
    "mcp_server_removed": (
        "mcp_drop_server",
        "MCP server '{server}' was removed.",
    ),
    # Indexed corpus removal (= ``reyn.core.op_runtime.index_drop`` emits
    # this after dropping chunks from the backend). Recall against
    # the dropped source will now miss; surfacing the change lets
    # the LLM understand "the source it was citing yesterday doesn't
    # exist today".
    "index_dropped": (
        "index_drop",
        "Indexed source '{source}' was removed.",
    ),
    # Config hot-reload (#2073): the HotReloader emits this at the turn boundary
    # after re-reading the IN-set (.reyn/*.yaml) + reapplying components, so the LLM
    # sees that its runtime config changed (e.g. a newly-reloaded MCP server / hook).
    "config_reloaded": (
        "config_watcher",
        "Reyn configuration was hot-reloaded (source: {source}).",
    ),
    # Future emitter slots (= add when wired):
    # "sp_version_changed": ("sp_loader",   "Agent system prompt was updated to version {version}."),
}


def _run_short(run_id: str) -> str:
    """Last 4 chars of a chat-side run_id, used as a display tag."""
    return run_id[-4:] if run_id else ""


def _run_meta(run_id: str | None, actor: str | None) -> dict:
    """Standard `meta` payload for OutboxMessage produced inside a run."""
    if run_id is None:
        return {"actor": actor} if actor else {}
    return {
        "run_id": run_id,
        "run_id_short": _run_short(run_id),
        "actor": actor,
    }


def _new_chain_id() -> str:
    """Mint a fresh chain_id for a top-level user request. Each user submission
    starts a new chain; agent_request / agent_response payloads forward the
    chain_id they received without minting new ones."""
    return uuid.uuid4().hex


def _user_frame_meta(attribution: "dict | None") -> dict:
    """Build ``meta`` for a ``kind="user"`` outbox frame (ADR-0039 multi-client
    input-broadcast fix).

    ``attribution`` mirrors the P3 ``user_answered_intervention`` shape
    (``auth_user_id`` / ``auth_connection_id`` — see
    ``agui/endpoint.py._handle_answer``): the AG-UI POST identity for a remote
    submit/answer. Local/in-process callers (the inline CUI, slash)
    pass ``None`` — the frame carries no attribution, so the renderer's
    ``_meta_prefix`` (``interfaces/repl/renderer.py``) shows the bare operator
    line, byte-identical to the pre-fix single-client echo.

    When ``auth_user_id`` is present it is ALSO copied to the generic
    ``actor`` key so the EXISTING ``_meta_prefix`` provenance-prefix path
    (already used for agent / status kind lines) renders it as ``[alice] ``
    with no new renderer branch — one prefix mechanism for every kind.
    """
    if not attribution:
        return {}
    meta = dict(attribution)
    auth_user_id = attribution.get("auth_user_id")
    if auth_user_id:
        meta["actor"] = auth_user_id
    return meta


def _format_hook_attribution(name: str, text: str) -> str:
    """Render an attributed hook message (#1800 slice 5b). The single source for
    the ``[hook:<name>]`` system-role prefix, shared by the staged-context
    consumer (C — wake=false ride-along) and ``_handle_hook_message`` (E —
    wake=true trigger) so the two paths can never drift."""
    return f"[hook:{name}] {text}"


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
    YAML cannot interpolate it. Session synthesizes the merged view
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


# NOTE: `_PendingChain` lives in `reyn.runtime.services.chain_manager` (PR-refactor-session-1
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
    if iv.actor:
        out["actor"] = iv.actor
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
    from reyn.runtime.services.intervention_handler import source_agent_var
    src = source_agent_var.get()
    if src:
        out["source_agent"] = src
    return out


# #1092 PR-F2b: max force-close handoffs per user turn. ONE is enough by
# construction — after a handoff the F2a reset slices [consolidation (≤
# output_reserve < threshold)] + new turn, which fits for any turn whose NEW
# message fits the post-consolidation budget (the normal case). The only input a
# 2nd handoff couldn't help is a single new message too large to ever fit — so at
# the cap we raise the genuine dead-end. This is chat's bounded analogue of
# phase's max_phase_visits (25), made tight (1) by the by-construction floor.
_MAX_FORCE_CLOSE_HANDOFFS = 1



def _render_summary_for_storage(structured: dict) -> str:
    """Render a chat_summary structured dict to a quick-display text blob.

    Stored in ChatMessage.text so REPL traces and audit dumps don't need
    to re-render the structured form. The slicer prefers the structured
    form for LLM consumption — this is for human consumption only.
    """
    parts: list[str] = []
    # #1092 PR-F2a: a force-close handoff consolidation carries its (free-text)
    # body in the dedicated ``consolidation`` field — render it verbatim and
    # first (it IS the conversation's carried-forward essence). Absent on normal
    # compaction summaries → no output change for them (byte-identical).
    consolidation = (structured.get("consolidation") or "").strip()
    if consolidation:
        parts.append(consolidation)
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


class DurabilityHaltError(RuntimeError):
    """#2259 PR-3: raised when an operation is submitted to an agent whose durability has FAILED
    persistently (a §4-retry-exhausted fire-and-forget durable write — disk full / dead). The agent
    has FAIL-STOPPED: it no longer accepts operations, because in-memory state must not race ahead
    of a dead disk (the owner's "no silent unbounded loss"). The raise IS the operator-surface — the
    caller sees it synchronously on their next op, not only a CRITICAL log they would scroll past."""


@dataclass(frozen=True)
class _AuditEventBundle:
    """#3082 Family 1: the audit-event spine (P6) — ``event_store`` (disk-backed
    log) → ``chat_events`` (the ``EventLog`` nearly every other Session
    sub-component consumes) → ``outbox_hub`` (the outbox fan-out), plus the
    opt-in OTEL subscriber attached to ``chat_events``. Pure output→input
    value object: :meth:`Session._build_audit_event_bundle` is a byte-identical
    extraction of the construction sequence that used to run inline in
    ``Session.__init__`` — same objects, same order, same args. This is the
    FIRST family built (the spine); later families' builders take its fields
    as explicit inputs instead of reaching into ``self`` mid-construction."""

    event_store: EventStore
    chat_events: EventLog
    outbox_hub: OutboxHub
    otel_exporter: "object | None"


@dataclass(frozen=True)
class _RecoveryBundle:
    """#3082 Family 2: the WAL-event/recovery pair — ``generation_store``
    (ADR-0038 Stage 1a PITR generation store) → ``journal`` (``SnapshotJournal``,
    the WAL append + snapshot-restore seam nearly every recovery path goes
    through), constructed in that order because the journal is wired to this
    same generation_store instance. Pure output→input value object:
    :meth:`Session._build_recovery_bundle` is a byte-identical extraction of
    the construction sequence that used to run inline in ``Session.__init__``
    — same objects, same order, same args (including reading the LOCAL
    ``state_log`` __init__ parameter, not ``self._state_log``, which is a
    separate tracking assignment made later and is untouched by this
    extraction). Same shape as ``_AuditEventBundle`` (#3082 Family 1)."""

    generation_store: SnapshotGenerationStore
    journal: SnapshotJournal


@dataclass(frozen=True)
class _HookEventBundle:
    """#3082 Family 3: the hook-event / reactivity spine — ``hook_bus``
    (the per-Session HookBus, Phase 4a) → ``hook_dispatcher`` (the awaited
    HookDispatcher every lifecycle dispatch() site routes through) →
    ``fs_watcher`` (the session-owned filesystem watcher, #2608 H4) →
    ``composer_registry`` (the composed:* Composers) → ``composed_consumer``
    (the composed:*→Sync bridge) → ``hot_reloader`` (the IN-set config
    hot-reloader). Pure output→input value object:
    :meth:`Session._build_hook_event_bundle` is a byte-identical extraction of
    the construction sequence that used to run inline in ``Session.__init__``
    — same objects, same construction order, same args (eager sibling reads
    use the builder's LOCAL variables; deferred lambdas keep resolving
    ``self._hook_dispatcher`` / ``self._chat_events`` at call time, exactly as
    before). This family CONSUMES Family 1's ``chat_events`` (the
    ``hot_reloader`` reads it eagerly at construction), so the builder is
    invoked AFTER the audit-event bundle is unpacked — the #3082 pipeline's
    output→input order. Config-derivation (``_boot_in_set`` /
    ``_composer_defs`` / ``_composed_schemas`` / ``_fs_watch_cfg``) is a
    precursor that stays inline and is threaded in as explicit inputs."""

    hook_bus: "HookBus"
    hook_dispatcher: "HookDispatcher"
    fs_watcher: "FsWatcher"
    composer_registry: "ComposerRegistry"
    composed_consumer: "ComposedEventConsumer"
    hot_reloader: "HotReloader"


@dataclass(frozen=True)
class _RetrievalBundle:
    """#3082 Family 5: the retrieval spine — the embedding block
    (``embedding_provider`` / ``embedding_event_sink`` / ``embedding_model_class``
    / ``action_embedding_index``, four attrs, one conditional construction
    guarded by ``universal_wrappers_enabled and embedding_class`` with a
    try/except None-fallback) plus ``action_usage_tracker`` (hot-list
    freq+recency, a SEPARATE conditional guarded by
    ``universal_wrappers_enabled and hot_list_n > 0``, also with a
    try/except None-fallback). Regrouped here per the #3082 DAG correction
    in the Family 4 spec (``action_usage_tracker`` was originally mis-grouped
    under Family 4/budget; it has no dependency on ``BudgetGateway`` and is
    retrieval-adjacent — co-located with ``action_embedding_index`` under the
    shared ``action_retrieval`` config). Two DAG corrections also apply here:
    the originally-listed ``render_bounds`` does not exist in this codebase
    (dropped) and ``subscription_writer`` is WAL-derived task-subscription
    state, not retrieval (excluded, reassigned to a later family).

    Pure output→input value object, with one inversion from Families 3/4:
    :meth:`Session._build_retrieval_bundle` is a byte-identical extraction of
    the construction sequence that used to run inline in ``Session.__init__``
    at its ORIGINAL position (line ~1152), which is BEFORE Family 1
    (``_build_audit_event_bundle``) runs — so unlike ``hot_reloader``
    (Family 3) or ``budget`` (Family 4), this family's two closures
    (``_embedding_event_sink`` / ``_on_hot_list_changed``) do NOT take
    ``chat_events`` as an eager builder input. They keep resolving
    ``self._chat_events`` at CALL time (the EventLog is constructed later in
    ``__init__``), exactly as the pre-extraction closures did — eager-izing
    that reference here would raise ``AttributeError`` at construction, since
    ``self._chat_events`` does not exist yet at line ~1152. The builder is an
    instance method precisely so these closures can keep capturing ``self``."""

    embedding_provider: "object | None"
    embedding_event_sink: "object | None"
    embedding_model_class: "str | None"
    action_embedding_index: "object | None"
    action_usage_tracker: "object | None"


@dataclass(frozen=True)
class _HistoryCompactionBundle:
    """#3082 Family 6b: the history-compaction chain — ``history_buffer``
    (``RouterHistoryBuffer``), ``compaction_controller``
    (``CompactionController`` wrapping a ``CompactionEngine``), and
    ``budget_advisor`` (``ContextBudgetAdvisor``). Family 6a
    (``router_host``, the WAIST) was extracted separately (#3113) and is
    NOT touched here — this builder only reads it as an already-built
    cross-family dependency (``self._router_host``).

    ★ Bidirectional circular dependency between ``history_buffer`` and
    ``compaction_controller``, both directions preserved verbatim:
    ``compaction_controller``'s inner ``CompactionEngine`` needs
    ``history_buffer.build_system_prompt`` (called during
    ``recompute_budgets()`` at ``CompactionEngine`` construction time), so
    ``history_buffer`` must exist FIRST — but ``history_buffer`` also needs
    a (circular) reference to ``compaction_controller`` for its own
    ``force_compact_now`` path. The pre-extraction code broke this cycle
    with a None-then-patch: construct ``history_buffer`` with
    ``compaction_controller=None``, construct ``compaction_controller``
    (reading ``history_buffer.build_system_prompt``, already available),
    then patch ``history_buffer._compaction_controller =
    compaction_controller`` once both exist. The builder reproduces this
    sequence byte-identically, entirely with LOCAL variables (see below).

    ★★ Why LOCAL, not ``self._history_buffer`` — the crash this builder
    must avoid: ``self._history_buffer`` is assigned by ``__init__`` only
    AFTER this builder RETURNS (unpacking the bundle). Reading
    ``self._history_buffer`` from INSIDE this builder — e.g. for
    ``system_prompt_provider`` or the patch line — would raise
    ``AttributeError`` (the attribute does not exist yet). Every reference
    among this family's OWN three components (history_buffer ↔
    compaction_controller ↔ budget_advisor) is therefore threaded through
    the builder's LOCAL variables (``history_buffer`` /
    ``compaction_controller``), never ``self._X``. Three reference
    classes, judged one at a time:
      - **intra-6b eager** (this family's own components referencing each
        other at CONSTRUCTION time): LOCAL variable —
        ``system_prompt_provider=history_buffer.build_system_prompt``, the
        patch line, ``compaction_controller=compaction_controller`` and
        ``history_fn=history_buffer.build_history`` on ``budget_advisor``.
      - **deferred** (a lambda resolved at CALL time, long after
        ``__init__`` returns, by which point ``self._history_buffer`` IS
        set): kept as ``self.*`` — ``model_fn=lambda:
        self._resolver.resolve(self.model).model`` and
        ``history_access=lambda: self.history`` (the latter reaches
        ``history_buffer`` indirectly via ``self.history``, once it
        exists).
      - **cross-family** (Families 1/5/6a's already-built outputs, or
        early ``__init__`` params/config, all set on ``self`` before this
        builder runs): kept as ``self._X`` — ``self._chat_events``
        (Family 1), ``self._router_host`` (Family 6a), ``self._resolver``
        / ``self._compaction`` / ``self._media_store`` /
        ``self._offload_config`` / ``self._budget_tracker`` /
        ``self._safety`` / ``self._latest_summary`` /
        ``self._action_retrieval`` / ``self._non_interactive`` /
        ``self._reasoning`` / ``self._active_branch_history`` /
        ``self._append_history`` / ``self.agent_name``.

    ``merge_action_usage`` (the ``_merge_action_usage_from_candidates``
    closure, defined in ``__init__`` immediately before this builder is
    called, at its original position — it is not itself one of this
    family's three components, so it is NOT moved into the builder body)
    is threaded through as an explicit LOCAL param, mirroring Family
    2/4's pattern of passing a ``__init__``-local value the builder
    cannot reach via ``self``.

    ★ ``budget_advisor`` UP-move: originally constructed AFTER
    ``InterAgentMessaging`` (Family 8) at line ~1893; this builder
    constructs it BEFORE ``InterAgentMessaging`` (which stays untouched,
    still constructed directly in ``__init__`` right after this builder
    returns) so the whole history-compaction chain — including the
    forward-patch — lands as one contiguous builder call. Safe because
    every one of ``budget_advisor``'s dependencies (``compaction_controller``
    / ``history_buffer`` / ``media_store`` / ``offload_config``, all
    LOCAL-or-cross-family-available at this point) is already resolved,
    nothing between the old and new position reads ``budget_advisor``, and
    ``InterAgentMessaging`` does not depend on any of this family's three
    components.

    Pure output→input value object: :meth:`Session._build_history_compaction_bundle`
    is a byte-identical extraction of the construction sequence that used
    to run inline in ``Session.__init__`` at its ORIGINAL position (line
    ~1797, no-move — every cross-family dep is already set on ``self`` by
    this point, since ``history_buffer`` eager-depends on Family 6a's
    ``router_host``)."""

    history_buffer: "RouterHistoryBuffer"
    compaction_controller: "CompactionController"
    budget_advisor: "ContextBudgetAdvisor"


@dataclass(frozen=True)
class _InterventionBundle:
    """#3082 Family 7: the intervention/chain-lifecycle group — ``chains``
    (``ChainManager``), ``interventions`` (``InterventionRegistry``),
    ``intervention_handler`` (``InterventionHandler``),
    ``intervention_coordinator`` (``InterventionCoordinator``), and
    ``chain_timeout_glue`` (``ChainTimeoutGlue``). Five components; the DAG
    grouping is accurate here — all five belong together (unlike Families
    4/5, which needed a mid-arc correction).

    ★ NO forward-patch / circular dependency (simpler than Family 6b's
    history_buffer ↔ compaction_controller cycle): ``chains`` and
    ``chain_timeout_glue`` reference each other, but ASYMMETRICALLY —
    ``chain_timeout_glue`` reads ``chains`` EAGERLY
    (``chains=self._chains`` at construction time), while ``chains`` only
    reaches ``chain_timeout_glue`` INDIRECTLY, through the bound method
    ``_on_chain_timeout_fire`` wired into ``InterAgentMessaging`` (Family
    8, unmoved) — that bound method forwards to
    ``self._chain_timeout_glue.on_chain_timeout_fire`` only when CALLED,
    long after both exist. So construction is strictly LINEAR: chains →
    interventions → intervention_handler → intervention_coordinator →
    chain_timeout_glue. No None-then-patch needed.

    ★ ``chain_timeout_glue`` Family-8-straddling UP-move: originally
    constructed at line ~1979, ~160 lines AFTER ``InterAgentMessaging``
    (Family 8, line ~1906); this builder constructs it immediately after
    ``intervention_coordinator`` (mirroring Family 6b's ``budget_advisor``
    UP-move) so all five Family 7 components land as one contiguous
    builder call BEFORE ``InterAgentMessaging`` (which stays untouched,
    still constructed directly in ``__init__`` right after this builder
    returns). Safe: every one of ``chain_timeout_glue``'s deps (LOCAL
    ``chains``, cross-family ``self._journal`` [Family 2] /
    ``self._chat_events`` [Family 1], plus a handful of already-set bound
    methods / config) is already resolved at the new position; nothing
    between the old and new position ever reads ``chain_timeout_glue``
    (its only caller outside ``__init__`` is at line ~6774); and
    ``InterAgentMessaging`` does not depend on ``chain_timeout_glue``.

    ★★ Family-8 cross-dep preserved: ``InterAgentMessaging`` (unmoved, at
    line ~1906) reads ``chain_manager=self._chains``. This builder's call
    site is placed at ``chains``'s ORIGINAL position (line ~1784), so
    ``self._chains`` is assigned well before ``InterAgentMessaging`` is
    constructed — the F8→F7 cross-family dependency resolves exactly as
    before.

    ★ intra-Family-7 local-vs-self (mirrors Family 6b's local-vs-self
    split): ``self._interventions`` / ``self._intervention_handler`` /
    ``self._chains`` are all assigned by ``__init__`` only AFTER this
    builder RETURNS — reading them as ``self._X`` from INSIDE the builder
    would raise ``AttributeError``. Every eager reference among this
    family's OWN five components is therefore threaded through LOCAL
    variables:
      - ``intervention_handler``'s ``registry=interventions`` (not
        ``self._interventions``);
      - ``intervention_coordinator``'s ``registry=interventions`` /
        ``handler=intervention_handler`` (not ``self._interventions`` /
        ``self._intervention_handler``);
      - ``chain_timeout_glue``'s ``chains=chains`` (not ``self._chains``).
    Deferred bound methods that resolve at CALL time (long after
    ``__init__`` returns, by which point the attributes ARE set) are kept
    as ``self.*`` — ``on_announce=self._announce_intervention`` on
    ``interventions``. Cross-family / config dependencies (already set on
    ``self`` before this builder runs) are kept as ``self._X`` —
    ``self._journal`` (Family 2), ``self._chat_events`` (Family 1),
    ``self._chain_timeout_seconds``, ``self._max_hop_depth``, plus
    ``chain_timeout_glue``'s bound-method callbacks
    (``self._append_history`` / ``self._reset_router_turn_counter`` /
    ``self._run_router_loop`` / ``self._emit_router_cap_exhausted_user`` /
    ``self._put_outbox`` / ``self.inbox`` / ``self._on_limit`` /
    ``self._handle_chat_limit_checkpoint`` / ``self._send_agent_response`` /
    ``self._put_inbox``).

    Pure output→input value object: :meth:`Session._build_intervention_bundle`
    is a byte-identical extraction of the construction sequence that used
    to run inline in ``Session.__init__`` — four of the five components
    stay at their ORIGINAL position (line ~1784); only
    ``chain_timeout_glue`` moves UP from line ~1979 to become part of this
    same contiguous builder call, straddling Family 8's
    ``InterAgentMessaging``."""

    chains: "ChainManager"
    interventions: "InterventionRegistry"
    intervention_handler: "InterventionHandler"
    intervention_coordinator: "InterventionCoordinator"
    chain_timeout_glue: "ChainTimeoutGlue"


class Session:
    def __init__(
        self,
        # Identity value object (single source of truth; FP-0043, see
        # session-construction.md#identity-the-agent-value-object-fp-0043-stage-2).
        # Required — #3133 Priority-0 step-2 removed the 9 flat identity params
        # (agent_name / agent_role / model / permission_resolver /
        # workspace_base_dir / workspace_state_dir / sandbox_config /
        # sandbox_backend / environment_backend) and the fallback construction
        # path they fed, so agent_name != agent.agent_name is no longer
        # constructible.
        agent: "Agent",
        resolver: ModelResolver | None = None,
        safety: "SafetyConfig | None" = None,
        mcp_servers: dict | None = None,
        output_language: str | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        compaction_config: "CompactionConfig | None" = None,
        reasoning_config: "ReasoningConfig | None" = None,  # #1652 chat.reasoning
        registry: "AgentRegistry | None" = None,
        allowed_mcp: list[str] | None = None,
        events_config: EventsConfig | None = None,
        # Resolved cost_warn: config for the high-cost-model gate (#2230)
        cost_warn_config: CostWarnConfig | None = None,
        # Debug lever disabling tool-result size gates (see session-construction.md#family-4-cost-budget)
        offload_config: OffloadConfig | None = None,
        # Operator render_template output bounds (FP-0055 / #2679)
        render_template_config: RenderTemplateConfig | None = None,
        state_log: StateLog | None = None,
        budget_tracker: BudgetTracker | None = None,
        snapshot_path: "Path | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        action_retrieval_config: "ActionRetrievalConfig | None" = None,
        # Chat-layer tool-use scheme name, threaded to RouterLoop (#1593 PR-2, default per #1657)
        chat_tool_use_scheme: str = "enumerate-all",
        embedding_config: "EmbeddingConfig | None" = None,
        eager_embedding_build: bool = False,
        # Resolved observability: block; opt-in OTLP export on chat_events (P5 ADR-0039)
        observability_config: "object | None" = None,
        # reyn.yaml llm.router.* ambient ContextVar (#1829 S3b)
        router_config: "RouterConfig | None" = None,
        retry_config: "object | None" = None,  # #1835: reyn.yaml llm.retry.* timing config
        agent_id: str | None = None,
        router_max_iterations: int = 5,  # #187: per-message tool-call budget for the MAIN chat loop (interactive=5; one-shot autonomous SWE sets higher)
        non_interactive: bool = False,  # #1439 Fix #1: run-once (piped, no TTY) — no user to ask, so the SP directs proceed-with-assumption instead of clarifying
        # Conversation session id WAL entries are recorded under, default "main" (FP-0043 Stage 5)
        session_id: str = "main",
        # Injectable execution driver seam; None -> default RouterLoopDriver construction
        loop_driver: "ExecutionDriver | None" = None,
        # Pre-built PipelineRegistry from the session factory; None -> empty registry (#2575)
        pipeline_registry: "PipelineRegistry | None" = None,
        # 4 cohesive param objects replacing 12 flat params (#3121 step1, see session-construction.md#3121-step1-parameter-objects)
        reactivity: "ReactivityConfig | None" = None,
        capability_scope: "CapabilityScope | None" = None,
        task_wiring: "TaskWiring | None" = None,
        presentation_wiring: "PresentationWiring | None" = None,
    ) -> None:
        """
        snapshot_path: optional override for the per-agent snapshot file
            location. Default: ``.reyn/agents/<agent_name>/state/snapshot.json``
            relative to the current working directory. Tests use this to
            redirect snapshot I/O to a tmp_path without touching private
            attributes.
        """
        # Default each omitted parameter object, unpack into pre-#3121 local names (#3121 step1, see session-construction.md#3121-step1-parameter-objects)
        reactivity = reactivity if reactivity is not None else ReactivityConfig()
        capability_scope = capability_scope if capability_scope is not None else CapabilityScope()
        task_wiring = task_wiring if task_wiring is not None else TaskWiring()
        presentation_wiring = (
            presentation_wiring if presentation_wiring is not None else PresentationWiring()
        )
        hooks_config = reactivity.hooks_config
        composers_config = reactivity.composers_config
        fs_watch_config = reactivity.fs_watch_config
        exclude_tools = capability_scope.exclude_tools
        excluded_categories = capability_scope.excluded_categories
        contextual_permission = capability_scope.contextual_permission
        available_skills = capability_scope.available_skills
        task_backend = task_wiring.task_backend
        task_waker = task_wiring.task_waker
        presentation_registry = presentation_wiring.presentation_registry
        presentation_consumer = presentation_wiring.presentation_consumer
        intervention_bridge = presentation_wiring.intervention_bridge
        # Identity cluster owned by Agent — single source of truth, no fallback
        # construction (#3133 Priority-0 step-2; the 9 flat identity params +
        # the None-fallback Agent(...) build were removed here).
        self._agent = agent
        self._resolver = resolver or ModelResolver({})
        # Per-session runtime model override set by /model <class>; None -> Agent identity default, in-memory only
        self._model_override: str | None = None
        # Mints a state_change entry on permission grant/revoke, breaking the #352 refusal trap (#398 v4, see session-construction.md#misc-lifecycle-wiring)
        if self._perm is not None and hasattr(self._perm, "register_on_persist"):
            self._on_perm_persist_cb = self._on_permission_persisted
            self._perm.register_on_persist(self._on_perm_persist_cb)
        else:
            self._on_perm_persist_cb = None
        _safety = safety or SafetyConfig()
        self._safety = _safety
        # Tool names excluded from the MAIN chat RouterLoop's LLM-visible catalog (#187, see session-construction.md#capability-permission-visibility)
        self._exclude_tools = frozenset(exclude_tools or ())
        # contextual_permission (#1827 S3) is owned by CapabilityVisibility, constructed below
        # once registry/router_host/session_id exist; `contextual_permission` (this local) is
        # threaded in as its initial value (see #3121 step3 Extract Class).
        # Session-scoped Task backend instance, threaded to task.* op handlers (#1953 slice 3a, see session-construction.md#capability-permission-visibility)
        self._task_backend = task_backend
        self._task_waker = task_waker  # #1953 slice 7
        # Present-sink consumer; production always supplies one, direct/test construction falls back to outbox-backed default (#2708 P1, see session-construction.md#misc-lifecycle-wiring)
        self._presentation_consumer = (
            presentation_consumer
            if presentation_consumer is not None
            else OutboxPresentationConsumer()
        )
        # Spawn-time intervention bridge; binds an attached driver's ask_user to the parent's listener (#2708 P3.2a, see session-construction.md#misc-lifecycle-wiring)
        self._intervention_bridge = intervention_bridge
        # task_id this session is EXECUTING as a task-as-request, read by op-ctx builders for task.create ownership (#1953 §16, see session-construction.md#safety-limits-interactive-mode)
        self._current_task_id: "str | None" = None
        # OS-authoritative provenance classification of the current turn, stamps entry["provenance"] (proposal 0060 Phase1 A7, see session-construction.md#safety-limits-interactive-mode)
        self._current_turn_origin: str = "auto_improvement"
        # Spawned EPHEMERAL flag, set post-construction by the registry (#2103, see session-construction.md#safety-limits-interactive-mode).
        # The vanish-scheduling state (_vanish_scheduled / _vanish_task) is owned by
        # SpawnTracker, constructed below (see #3133 P3 Extract Class, spawn_tracker.py).
        self._ephemeral: bool = False
        # Lazily-resolved minimal _untrusted ContextualPermission cache (#1827 S4b context-auto)
        self._untrusted_contextual_cache = None
        # excluded_categories (#1667) + the visibility override (#2285) are owned by
        # CapabilityVisibility, constructed below (see #3121 step3 Extract Class).
        # Session-scoped hook APPLICABILITY override, per-session by construction (#2285, see session-construction.md#capability-permission-visibility)
        self._disabled_hooks: "set[str]" = set()
        # Per-message tool-call budget for the MAIN chat RouterLoop (#187, see session-construction.md#safety-limits-interactive-mode)
        self._router_max_iterations = int(router_max_iterations)
        # Run-once mode: the router SP must not ask a clarifying question nobody can answer (#1439 Fix #1, see session-construction.md#safety-limits-interactive-mode)
        self._non_interactive = bool(non_interactive)
        # Media-size gate config, plumbed to spawned Agents + router host adapter (#364, see session-construction.md#multimodal-media)
        self._multimodal_config = multimodal_config
        # Single MediaStore instance per Session (#383 PR-C, see session-construction.md#multimodal-media)
        from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
        if multimodal_config is not None:
            self._media_store: "MediaStore | None" = MediaStore(
                MediaStoreConfig(
                    media_dir=multimodal_config.media_dir,
                    tool_results_dir=multimodal_config.tool_results_dir,
                ),
                project_root=Path.cwd(),
                # path-refs carry resource_uri/source_agent for cross-host dispatch (#385 β sub-task 1)
                agent_name=self.agent_name,
                # path-refs carry a url when this instance is HTTP-reachable (#385 β sub-task 3b)
                base_url=multimodal_config.base_url,
            )
        else:
            self._media_store = None
        # Queue of /image-attached blocks drained on the next user-message turn (#366, see session-construction.md#multimodal-media)
        self._pending_user_images: list[dict] = []
        # Drives whether universal catalog wrappers appear in router tools= (FP-0034 PR-3b-iii, see session-construction.md#family-5-retrieval)
        self._action_retrieval = action_retrieval_config or ActionRetrievalConfig()
        # Enabled skill registry snapshot for the ## Skills block; None -> omitted section (#2548 PR-A)
        self._available_skills = available_skills
        self._chat_tool_use_scheme = chat_tool_use_scheme  # #1593 PR-2, passed to RouterLoopDriver below
        # RouterLoop awaits the embedding index build synchronously on turn 1 when True (B25-S5-1 fix, see session-construction.md#family-5-retrieval)
        self._eager_embedding_build = eager_embedding_build
        # Falls back to a default identifier when the factory doesn't supply agent.id (FP-0016 Component E)
        if agent_id is None:
            from reyn.config import _default_agent_id
            agent_id = _default_agent_id()
        self._agent_id: str = agent_id
        # Sender of the most-recently-dispatched inbox item, for sender-transition state_change entries (FP-0041 #489 PR-A)
        self._last_sender: str | None = None
        # Reply-to attribution captured from an inbound payload's reply_to (FP-0041 #489 PR-D2)
        self._last_reply_to: Any = None
        # Outbox interceptor for external transport (e.g. Slack via MCP); None skips interception (FP-0041 #489 PR-D2)
        self._outbox_interceptor: Any = None
        # Embedding block + action_usage_tracker, byte-identical extraction, unmoved (#3082 Family 5, see session-construction.md#family-5-retrieval)
        _retrieval_bundle = self._build_retrieval_bundle(
            self._action_retrieval, embedding_config, self.agent_name,
        )
        self._action_embedding_index = _retrieval_bundle.action_embedding_index
        self._embedding_provider = _retrieval_bundle.embedding_provider
        self._embedding_model_class = _retrieval_bundle.embedding_model_class
        self._embedding_event_sink = _retrieval_bundle.embedding_event_sink
        self._action_usage_tracker = _retrieval_bundle.action_usage_tracker
        self._mcp_servers = mcp_servers
        # mcp_connection_service; 4 lambdas deferred-resolve sibling deps at call time (#3082 Family 8c, see session-construction.md#family-8c-mcp-connection-service)
        self._mcp_connection_service = self._build_mcp_connection_service()
        # Resolve fs_watch: as a builder input; FsWatcher itself is built in _build_hook_event_bundle (#2608 H4 / #3082 Family 3, see session-construction.md#family-3-hook-event-reactivity)
        from reyn.config.infra import FsWatchConfig
        _fs_watch_cfg = (
            fs_watch_config if isinstance(fs_watch_config, FsWatchConfig) else FsWatchConfig()
        )
        self.output_language = output_language
        self._prompt_cache_enabled = prompt_cache_enabled
        self._project_context = project_context
        # Back-reference for slash commands (/agents, /attach) and agent-to-agent routing; wired by the chat factory (PR11)
        self._registry = registry
        # Session owns a live PipelineRegistry so run_pipeline has a lookup target; None -> empty registry (IS-5 / #2575, see session-construction.md#misc-lifecycle-wiring)
        self._pipeline_registry = (
            pipeline_registry if pipeline_registry is not None else PipelineRegistry()
        )
        # Session's named-presentation-template registry; hot-reload swaps this + the adapter's captured copy (FP-0054 PR-C, see session-construction.md#misc-lifecycle-wiring)
        from reyn.data.presentations import PresentationRegistry
        self._presentation_registry = (
            presentation_registry if presentation_registry is not None
            else PresentationRegistry()
        )
        self._max_hop_depth = _safety.loop.max_agent_hops  # PR11: max delegation hop depth, refuse send beyond limit
        self._chain_timeout_seconds = _safety.timeout.chain_seconds  # PR18: per-chain wall-clock budget, non-positive disables
        self._on_limit = _safety.on_limit  # FP-0005: per-session safety-limit checkpoint policy
        self._safety_extensions: dict[str, float] = {}  # FP-0005: per-(turn or chain) extension counters, cleared at boundary
        self._hook_driven_turns: int = 0  # #1800 slice 7: loop-valve counter, in-memory, resets each user turn
        # Optional MCP server allowlist from agent profile; None = inherits project config (PR37)
        self._allowed_mcp: list[str] | None = (
            list(allowed_mcp) if allowed_mcp is not None else None
        )

        self._events_config = events_config or EventsConfig()  # PR20: per-chat rotation policy
        self._cost_warn_config = cost_warn_config or CostWarnConfig()  # #2230, see session-construction.md#family-4-cost-budget
        self._offload_config = offload_config or OffloadConfig()  # tool-result-schema-redesign §5 debug lever
        # Resolve operator render_template bounds once, threaded to every router OpContext builder (FP-0055 / #2679, see session-construction.md#family-4-cost-budget)
        _rt_cfg = render_template_config or RenderTemplateConfig()
        from reyn.core.op_runtime.render_template import RenderTemplateBounds
        self._render_template_bounds = RenderTemplateBounds(
            max_output_chars=_rt_cfg.max_output_chars,
            wall_clock_seconds=_rt_cfg.wall_clock_seconds,
        )

        # WAL + per-agent snapshot for crash recovery via SnapshotJournal; snapshot_path kept only for diagnostics (PR21 / PR-refactor-session-1, see session-construction.md#family-2-recovery-wal-journal)
        self._session_id = session_id
        self._snapshot_path = snapshot_path or (
            Path(".reyn") / "agents" / self.agent_name / "state" / "snapshot.json"
        )
        # generation_store -> journal, byte-identical extraction (#3082 Family 2, see session-construction.md#family-2-recovery-wal-journal)
        _recovery_bundle = self._build_recovery_bundle(
            self.agent_name, self._snapshot_path, state_log, session_id,
        )
        self._generation_store = _recovery_bundle.generation_store
        self._journal = _recovery_bundle.journal
        # Turn-idle event for quiescence; lets a global rewind await_quiescent before the reset-record append (ADR-0038 Stage 1c, see session-construction.md#family-2-recovery-wal-journal)
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        self._turn_owner_task: "asyncio.Task | None" = None  # lets await_quiescent skip its wait when called re-entrantly from the owning task
        # #2242: True only for the window between cancel_inflight() calling
        # `_turn_owner_task.cancel()` and run_one_iteration observing the
        # resulting CancelledError. Distinguishes OUR OWN hard-cancel (swallowed,
        # so the run-loop / driver task survives) from an externally-cancelled
        # driver task (e.g. an anyio scope teardown cancelling the MCP/A2A
        # request-handler task that is pumping run_one_iteration directly, FP-0013
        # §ADR-A) — in the external case `await self._turn_owner_task` ALSO
        # raises CancelledError (asyncio propagates an awaiting task's cancel into
        # whatever Task/Future it is suspended on), but that cancellation must be
        # RE-RAISED, not swallowed, so the driver's own cancellation completes
        # normally instead of silently surviving a cancel that was never ours.
        self._turn_cancel_self_initiated: bool = False
        # Joinable handle for fire-and-forget WAL-append tasks so await_quiescent can join them too (ADR-0038 Stage 1c coverage, see session-construction.md#family-2-recovery-wal-journal)
        self._inflight_wal_tasks: set[asyncio.Task] = set()
        # Kept directly (not only via journal) so ops launched from this session can emit step events into the same WAL
        self._state_log = state_log
        self._halted_reason: "str | None" = None  # #2259 PR-3: set on FAIL-STOP, see session-construction.md#family-2-recovery-wal-journal
        self._task_subscription_writer = SubscriptionWriter(state_log) if state_log is not None else None  # #2187 backend-master, mirrors task_waker
        # In-memory buffer of restored-then-resolved intervention answers, keyed by run_id (PR-intervention-link L6)
        self._buffered_intervention_answers: dict[str, "InterventionAnswer"] = {}
        # In-memory staging for wake=false ride-along messages, durably persisted in the snapshot (#1800 slice 4b, see session-construction.md#safety-limits-interactive-mode)
        self._next_turn_context: list[dict] = []

        # HookBus/HookDispatcher/fs_watcher/composers/hot_reloader built together below; the config-derivation feeding them stays inline as builder inputs (#1800 slice 5b / #3082 Family 3, see session-construction.md#family-3-hook-event-reactivity)
        self._startup_hooks_raw: list = hooks_config if isinstance(hooks_config, list) else []
        # composers: startup (OUT-set) layer, combined with the other 3 layers by _build_composer_defs; v1 startup-only, no hot-reload (Hook-Event Redesign Phase 4b/5, #2880/#2881, see session-construction.md#family-3-hook-event-reactivity)
        self._startup_composers_raw: list = (
            composers_config if isinstance(composers_config, list) else []
        )
        from reyn.config.loader import load_hot_reload_config as _load_in_set
        _boot_in_set = _load_in_set(
            getattr(self._registry, "_project_root", None) or Path.cwd()
        )
        # Run before _build_hook_registry so composed:* hook matchers can be schema-checked against the full composer set (#2889, see session-construction.md#family-3-hook-event-reactivity)
        self._composer_defs = self._build_composer_defs(_boot_in_set)
        self._composed_schemas: "dict[str, frozenset[str]]" = {
            d.emit_kind: frozenset({"inputs", "correlation_key"}) for d in self._composer_defs
        }
        # RUNTIME (.reyn/cron.yaml) cron job names, so the reapply seam can unschedule removed jobs without touching startup jobs (#2073 S4, see session-construction.md#family-3-hook-event-reactivity)
        self._runtime_cron_names: set = {
            j["name"] for j in ((_boot_in_set.get("cron") or {}).get("jobs") or [])
            if isinstance(j, dict) and j.get("name")
        }

        self._budget_tracker = budget_tracker  # PR22: process-shared budget/rate-limit tracker; None -> checks noop

        _router_cap: int = _safety.loop.max_router_calls_per_turn  # per-turn router cap from safety config

        from reyn.config import CompactionConfig, ReasoningConfig
        self._compaction = compaction_config or CompactionConfig()
        self._reasoning = reasoning_config or ReasoningConfig()  # #1652: reasoning capture/continuity/display, on-by-default
        self._next_seq = 1

        # agents/<name>/ is state-only (PR20); Agent-derived workspace_dir, ensure it exists (FP-0043 Stage 2)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        self.events_dir = (  # PR20: chat events dir, created lazily by EventStore on first write
            Path(".reyn") / "events" / "agents" / self.agent_name / "chat"
        )

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        # event_store -> chat_events -> outbox_hub (+ opt-in OTEL), byte-identical extraction (#3082 Family 1, see session-construction.md#family-1-audit-event-spine-p6)
        _audit_bundle = self._build_audit_event_bundle(observability_config)
        self.outbox_hub = _audit_bundle.outbox_hub
        self._event_store = _audit_bundle.event_store
        self._chat_events = _audit_bundle.chat_events
        self._otel_exporter = _audit_bundle.otel_exporter
        # hook_bus -> hook_dispatcher -> fs_watcher -> composer_registry -> composed_consumer -> hot_reloader; runs right after Family 1 since hot_reloader reads chat_events eagerly (#3082 Family 3, see session-construction.md#family-3-hook-event-reactivity)
        _hook_bundle = self._build_hook_event_bundle(
            _boot_in_set,
            self._composer_defs,
            _fs_watch_cfg,
            self._chat_events,
            self._registry,
            self._session_id,
        )
        self._hook_bus = _hook_bundle.hook_bus
        self._hook_dispatcher = _hook_bundle.hook_dispatcher
        self._fs_watcher = _hook_bundle.fs_watcher
        self._composer_registry = _hook_bundle.composer_registry
        self._composed_consumer = _hook_bundle.composed_consumer
        self._hot_reloader = _hook_bundle.hot_reloader
        # Publish as the process-wide active reloader so the hooks-write LLM-op can request_reload (#2073 S3, see session-construction.md#family-3-hook-event-reactivity)
        from reyn.runtime.hot_reload import set_active_hot_reloader
        set_active_hot_reloader(self._hot_reloader)
        # Detached by default; AgentRegistry.attach() flips this on to stop background display noise
        self.is_attached: bool = False

        # Publish this session's EventLog as the ambient LLM-chokepoint sink for observable llm_request events (#1669)
        from reyn.core.events.events import set_llm_request_event_log
        set_llm_request_event_log(self._chat_events)
        # Publish reyn.yaml llm.router.* as the ambient router config; guarded so nested construction never clobbers an inherited ContextVar (#1829 S3b)
        if router_config is not None:
            from reyn.llm.llm import set_router_config
            set_router_config(router_config)
        if retry_config is not None:  # #1835: same guard, ambient retry timing config
            from reyn.llm.llm import set_retry_config
            set_retry_config(retry_config)
        # Publish the budget-exceed policy for the chat path's per-LLM-call cost gate, bridge-aware so an attached driver's prompt reaches the parent's operator (#1868 / #3053, see session-construction.md#family-4-cost-budget)
        _make_router_bus = self._make_router_intervention_bus

        class _ChatBudgetBus:
            async def request(self, iv):  # type: ignore[no-untyped-def]
                return await _make_router_bus().request(iv)

        from reyn.llm.llm import set_llm_call_limit_context
        # Publish per-call timeout/retries so the chat ROUTER path bounds each call and routes hangs through on_limit (#2210)
        set_llm_call_limit_context(
            _ChatBudgetBus(), self._on_limit, self.agent_name, self._non_interactive,
            llm_call_timeout=self._safety.timeout.llm_call_seconds,
            llm_max_retries=self._safety.timeout.llm_max_retries,
        )
        # Surfaces session-level lifecycle events (compaction, attach/detach, budget warnings) into the conv pane (#162, see session-construction.md#misc-lifecycle-wiring)
        from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
        self._chat_events.add_subscriber(
            ChatLifecycleForwarder(
                self.outbox, registry=self._registry, events=self._chat_events
            )
        )
        # Generic events-log subscriber converting op-emitted events to state_change history entries (#398 v4 emitter family, see session-construction.md#misc-lifecycle-wiring)
        self._chat_events.add_subscriber(
            self._on_chat_event_for_state_change,
        )

        # Budget adapter, byte-identical extraction, simplest of the #3082 families (Family 4, see session-construction.md#family-4-cost-budget)
        self._budget = self._build_budget(
            budget_tracker, self._chat_events, self.agent_name, _router_cap,
        )

        # Memory persistence adapter, byte-identical extraction, pre-waist position (#3082 Family 8b, see session-construction.md#family-8b-memory)
        self._memory = self._build_memory()

        # One-shot command-UI request (e.g. /rewind checkpoint picker); None = nothing pending, dict carries {"kind", ...} (F4)
        self._pending_command_ui: dict | None = None

        # chains / interventions / intervention_handler / intervention_coordinator / chain_timeout_glue, byte-identical extraction; chain_timeout_glue UP-moved ahead of Family 8 (#3082 Family 7, see session-construction.md#family-7-intervention)
        _intervention_bundle = self._build_intervention_bundle()
        self._chains = _intervention_bundle.chains
        self._interventions = _intervention_bundle.interventions
        self._intervention_handler = _intervention_bundle.intervention_handler
        self._intervention_coordinator = _intervention_bundle.intervention_coordinator
        self._chain_timeout_glue = _intervention_bundle.chain_timeout_glue

        # Owns the spawned-task correlation record + ephemeral auto-vanish scheduling
        # state (#2103, see #3133 P3 Extract Class); Session holds one reference +
        # delegates via thin forwarders, does not re-own the state (see spawn_tracker.py).
        # session_id / ephemeral are read through LIVE providers -- both are reassigned
        # post-construction by the registry (spawn-time re-key / ephemeral-spawn flip),
        # so a value snapshot copied here would go stale (same hazard CapabilityVisibility,
        # constructed below, documents for its own session_id_provider).
        self._spawn_tracker = SpawnTracker(
            registry=self._registry,
            journal=self._journal,
            chains=self._chains,
            inbox=self.inbox,
            agent_name=self.agent_name,
            session_id_provider=lambda: self._session_id,
            ephemeral_provider=lambda: self._ephemeral,
        )

        # Delegation tracking for RouterLoop runs; None outside a run, cleared after each (F2)
        self._router_loop_delegations: list[dict] | None = None

        # Agent-reply capture for agent-to-agent RouterLoop paths; None = not capturing (F2)
        self._router_loop_agent_replies: list[str] | None = None

        # Router-host WAIST: RouterHostAdapter aggregates ~40 already-built sub-components most later families read through, byte-identical (#3082 Family 6a, see session-construction.md#family-6a-router-waist-routerhostadapter)
        # contextual_permission is the RAW constructor-supplied initial value here (#3121 step3:
        # CapabilityVisibility, which owns the LIVE composed value, does not exist yet -- it needs
        # router_host, which THIS call builds -- so this one eager pre-waist consumer is threaded
        # the local var explicitly rather than reading a not-yet-constructed self._capability_visibility).
        self._router_host = self._build_router_waist(contextual_permission=contextual_permission)

        # Owns the per-session capability/skill visibility override + the envelope-composed
        # contextual_permission/excluded_categories it derives (#2285, see #3121 step3 Extract Class);
        # Session holds one reference + delegates, does not re-own the state (see capability_visibility.py).
        self._capability_visibility = CapabilityVisibility(
            registry=self._registry,
            router_host=self._router_host,
            session_id_provider=lambda: self._session_id,  # live -- session_id is re-keyed post-construction (registry.py spawn_session_recorded)
            agent_name=self.agent_name,
            available_skills_provider=lambda: self._available_skills,
            contextual_permission=contextual_permission,
            excluded_categories=excluded_categories,
        )

        # owns + orchestrates them in one method (#2073 S2, see session-construction.md#family-3-hook-event-reactivity)
        self._register_hot_reload_seams()

        # Synchronous head/body/tail compaction callback; session drives compaction via force_compact_now() (FP-0019 Wave 1 / #1128 PR-a)
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

        # Adaptive per-user token-estimation learner (PR-N6)
        from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
        self._token_learner: TokenMultiplierLearner = TokenMultiplierLearner(
            chars4_mode=self._compaction.use_chars4_estimate,
        )

        # history_buffer / compaction_controller (incl. the None-then-patch breaking their circular dep) / budget_advisor, byte-identical extraction (#3082 Family 6b, see session-construction.md#family-6b-history-compaction)
        _history_compaction_bundle = self._build_history_compaction_bundle(
            merge_action_usage=_merge_action_usage_from_candidates,
        )
        self._history_buffer = _history_compaction_bundle.history_buffer
        self._compaction_controller = _history_compaction_bundle.compaction_controller
        self._budget_advisor = _history_compaction_bundle.budget_advisor

        # InterAgentMessaging: agent-to-agent messaging service, hybrid design (FP-0019 Wave 2 part 2); byte-identical extraction, post-waist (#3082 Family 8a, see session-construction.md#family-8a-inter-agent-messaging)
        self._inter_agent_messaging = self._build_inter_agent_messaging()

        # RouterLoopDriver owns the per-turn loop orchestration (session.py refactor PR-3, see session-construction.md#misc-lifecycle-wiring)
        from reyn.runtime.services.router_loop_driver import RouterLoopDriver
        self._loop_driver: ExecutionDriver = (
            loop_driver if loop_driver is not None else RouterLoopDriver(
                router_host=self._router_host,
                safety=self._safety,
                router_max_iterations=self._router_max_iterations,
                budget_tracker=self._budget_tracker,
                non_interactive=self._non_interactive,
                exclude_tools=self._exclude_tools,
                contextual_permission=self._capability_visibility.contextual_permission,  # #1827 S3 → RouterLoop live gate
                contextual_for_turn_fn=self._effective_contextual_for_turn,  # #1827 S4b context-auto
                excluded_categories=self._capability_visibility.excluded_categories,
                budget=self._budget,
                resolver=self._resolver,
                compaction=self._compaction,
                compaction_controller=self._compaction_controller,
                token_learner=self._token_learner,
                events=self._chat_events,
                model_override_fn=lambda: self._model_override,
                history_buffer=self._history_buffer,
                budget_advisor=self._budget_advisor,
                limit_checkpoint_fn=self._handle_chat_limit_checkpoint,
                next_seq_fn=lambda: self._next_seq,
                append_history_fn=self._append_history,
                chat_scheme_name=self._chat_tool_use_scheme,  # #1593 PR-2
            )
        )

        # Additional cancel-forward targets fired by cancel_inflight alongside this session's own _loop_driver; empty for an ordinary turn (#2588, see session-construction.md#misc-lifecycle-wiring)
        self._cancel_forward_targets: list[Callable[[], None]] = []

    # ── cost accumulation ───────────────────────────────────────────────────────

    def _accumulate(self, result) -> None:
        self._budget.accumulate(result)

    def subscribe_chat_events(self, cb: "Callable[..., None]") -> None:
        """Register ``cb`` for this session's chat events (narrow public API).

        Encapsulates the internal EventLog so UI callers (e.g. the inline CUI
        working indicator) subscribe without reaching into ``_chat_events``.
        ``cb`` receives an ``Event`` (``.type`` / ``.data``) synchronously on the
        session loop. Pair with :meth:`unsubscribe_chat_events`.
        """
        self._chat_events.add_subscriber(cb)

    def unsubscribe_chat_events(self, cb: "Callable[..., None]") -> bool:
        """Remove a callback registered via :meth:`subscribe_chat_events`."""
        return self._chat_events.remove_subscriber(cb)

    def set_events_dir(self, events_dir: Path) -> None:
        """#2348: re-point this session's chat EventStore to a per-session directory.

        Spawned sessions share the agent identity (and thus the name-only
        ``events_dir`` built in ``__init__``), so the chat audit events of all of an
        agent's sessions bled into one ``events/agents/<name>/chat`` tree. The
        registry's ``spawn_session`` fixup calls this — parallel to the snapshot/WAL
        re-key — before the run-loop goes live, so no event lands in the shared tree.

        Swaps ONLY the ``EventStore`` subscriber on ``_chat_events`` (remove old, add
        new); every OTHER subscriber (the ``ChatLifecycleForwarder`` outbox bridge, the
        state-change converter, any attach-time focus listener) is preserved. A rebuild
        of the subscriber list would silently drop them and chat events would stop
        reaching the outbox / TUI — so the swap is surgical, not a reconstruction.
        """
        new_store = EventStore(
            events_dir,
            max_bytes=self._events_config.max_bytes,
            max_age_seconds=self._events_config.max_age_seconds,
        )
        self._chat_events.remove_subscriber(self._event_store)
        self._chat_events.add_subscriber(new_store)
        self.events_dir = events_dir
        self._event_store = new_store

    @property
    def non_interactive(self) -> bool:
        """#2585 PR2: read-only public surface for ``_non_interactive`` (set at
        construction from the frontend's session_factory, and force-overridden
        to True for ephemeral spawns by ``AgentRegistry.spawn_session_recorded``
        — see its ``mode == "ephemeral"`` branch). Lets callers/tests observe
        the effective ask-vs-proceed SP branch without reaching into the
        "private" attribute."""
        return self._non_interactive

    @property
    def total_usage(self):
        return self._budget.total_usage

    @property
    def last_call_usage(self):
        """TokenUsage of the single most recent LLM call only (distinct from
        BOTH the session-cumulative ``total_usage`` and a turn-summed figure —
        a turn can make several LLM calls via tool-loop iterations) — status-
        bar ctx chip's "current context size" headline figure."""
        return self._budget.last_call_usage

    @property
    def total_cost_usd(self) -> float:
        return self._budget.total_cost_usd

    @property
    def total_cost_breakdown(self):
        """Cache-aware ``CostBreakdown`` for this session (Session-scope row
        source for the cost panel's Input/Output/Saved/Saved% breakdown)."""
        return self._budget.total_cost_breakdown

    @property
    def embedding_cost(self):
        """FP-0063 PC: this session's INDEPENDENT ``EmbeddingCost`` aggregate —
        the Session-scope reader of the session/agent/project trio (agent and
        project scope are read via ``Registry.agent_embedding_cost`` /
        ``.project_embedding_cost``).

        Deliberately separate from ``total_cost_breakdown`` above, which stays
        chat-only: an embedding call is input-only and structurally
        uncacheable, so folding it in would dilute that breakdown's
        ``cache_hit_rate`` / ``cache_savings``."""
        return self._budget.embedding_cost

    # ── FP-0043 Stage 2: identity-field delegations to the Agent value object ──
    # Read-only by construction (identity is immutable for the session lifetime;
    # no field is reassigned post-__init__, verified). Every former direct
    # attribute (public agent_name/model/workspace_dir + the "private" _perm /
    # _workspace_* / _environment_backend / _sandbox_* read internally AND by
    # external consumers) keeps the SAME name + value via these properties →
    # byte-identical surface; the single source of truth is self._agent.
    @property
    def agent_name(self) -> str:
        return self._agent.agent_name

    @property
    def model(self) -> str:
        return self._model_override if self._model_override is not None else self._agent.model

    def known_model_classes(self) -> list[str]:
        """Operator-configured model classes selectable via ``/model <class>``.

        The same list ``/model`` (no-arg) prints under ``available:``. Lets a UI
        offer an actionable model picker without reaching into the resolver; the
        switch itself stays the ``/model`` slash path (cost-warn + budget rebuild).
        """
        return self._resolver.known_classes()

    def active_model_class(self) -> str | None:
        """Return the class name for the currently-active model, or None.

        When a ``/model`` override is active the override IS already a class name.
        When no override is set ``session.model`` is the full LiteLLM model ID
        (e.g. ``"claude-opus-4-8"``); this reverse-looks up which configured
        class maps to that ID so callers (e.g. the model picker) can highlight
        the active entry without knowing about the resolver internals.
        Returns None when the current model ID is not found in any configured
        class (= custom/passthrough model not declared in reyn.yaml).
        """
        if self._model_override is not None:
            return self._model_override
        model_id = self._agent.model
        for cls in self._resolver.known_classes():
            if self._resolver.resolve(cls).model == model_id:
                return cls
        return None

    def _rebuild_turn_budget_engine_for_model(self) -> None:
        """#1752: rebuild the chat turn_budget engine for the active model.

        The engine bakes derived headroom (max_input + wrap-up-SP token cost)
        for one resolved (model, config) at construction (a deliberate
        compute-once invariant, mirroring CompactionEngine). A ``/model``
        override changes the context window, so on switch we rebuild the engine
        for the new resolved model and rewire it into the RouterHostAdapter —
        rather than recomputing per turn for a rare event. ``try_build_*``
        returns ``None`` for a small-context model (force-close stays inert),
        matching the original construction at ``__init__``.
        """
        from reyn.services.turn_budget import try_build_default_turn_budget_engine
        engine = try_build_default_turn_budget_engine(
            self._resolver.resolve(self.model).model,
            use_chars4=getattr(self._compaction, "use_chars4_estimate", False),
        )
        self._router_host.set_turn_budget_engine(engine)

    @property
    def workspace_dir(self) -> "Path":
        return self._agent.workspace_dir

    @property
    def _perm(self) -> "PermissionResolver | None":
        return self._agent.permission_resolver

    @property
    def _workspace_base_dir(self) -> "Path | None":
        return self._agent.workspace_base_dir

    @property
    def _workspace_state_dir(self) -> "Path | None":
        return self._agent.workspace_state_dir

    @property
    def _environment_backend(self) -> Any:
        return self._agent.environment_backend

    @property
    def _sandbox_config(self) -> Any:
        return self._agent.sandbox_config

    @property
    def _sandbox_backend(self) -> Any:
        return self._agent.sandbox_backend

    @property
    def _agent_role(self) -> str:
        # Internal backing-name for agent_role, kept as a delegating property so
        # existing internal read-sites (agent_role= passthrough to the router host,
        # etc.) keep working over the Agent identity object.
        return self._agent.role

    @property
    def agent_role(self) -> str:
        """Read-only public accessor for the attached agent's role text.

        FP-0043 Stage 2: delegates to the Agent identity object (read-only —
        identity is immutable for the session's lifetime). Reads via the property
        are the encapsulation-respecting surface for slash commands and tests.
        """
        return self._agent.role

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
    def pipeline_registry(self) -> "PipelineRegistry":
        """Read-only accessor for the session's owning PipelineRegistry.

        IS-5: Session constructs + owns a real (initially empty)
        ``PipelineRegistry`` instance — populating it from disk / a YAML
        DSL parser is a later slice; this property + the constructor
        wiring below exist so ``run_pipeline`` has a real registry to
        look up against in production, not the ``None`` landmine
        (``ctx.router_state.pipeline_registry`` was never populated
        before this). Threaded into ``RouterHostAdapter`` at
        construction (mirrors ``agent_registry`` above), then onto
        ``RouterCallerState`` by ``RouterLoop._build_router_caller_state``.
        """
        return self._pipeline_registry

    @property
    def contextual_permission(self) -> "object | None":
        """#3097: read-only accessor for the live ``ContextualPermission`` (#1827
        S3) — the per-turn gate value ``CapabilityVisibility.reapply_visibility_override``
        maintains (envelope ∩ session override, restrict-only, narrow-only). A
        ``snapshot()``-style public read so a test can verify the security-core
        seam narrows correctly (``visible ⊆ authorized``) without reaching into
        the private field directly (#3121 step3 Extract Class)."""
        return self._capability_visibility.contextual_permission

    @property
    def presentation_registry(self):
        """Read-only accessor for the session's owning PresentationRegistry
        (FP-0054 PR-C — operator named templates from presentations.yaml). Mirrors
        ``pipeline_registry`` above; threaded into ``RouterHostAdapter`` at
        construction and swapped by ``_reapply_presentations`` on hot-reload. Tests
        verify a registered template is live via this surface."""
        return self._presentation_registry

    @property
    def presentation_consumer(self):
        """Read-only accessor for this session's present-sink ``PresentationConsumer``
        (#2708 P1 stores it; P3.1 reads it here). The spawn-bridge uses this to bind a
        driver-session's present output to the PARENT: an attached pipeline driver spawn
        wraps ``parent.presentation_consumer`` in a ``SpawnBridgePresentationConsumer`` so
        the driver's present reaches the parent surface by construction."""
        return self._presentation_consumer

    @property
    def intervention_bridge(self):
        """Read-only accessor for this session's spawn-time intervention bridge (#2708 P3.2a /
        P3-item3), or ``None`` for a self-bound session. An attached pipeline driver carries a
        ``SpawnBridgeInterventionListener`` (ask_user reaches the parent operator); a detached /
        headless spawn carries an ``AuditOnlyInterventionBridge`` (ask_user is a typed refusal)."""
        return self._intervention_bridge

    @property
    def interventions(self) -> "InterventionRegistry":
        """Read-only public accessor for the session's InterventionRegistry.

        The registry itself carries rich public API (= ``get`` /
        ``queued_count`` / ``list_active`` / ``has_active_listener`` /
        ``is_listener_enforcement_enabled``), so exposing it directly
        keeps callers off the underscore field without forcing a
        delegate-method explosion on Session. The registry
        instance is set once in ``__init__`` and never re-bound.
        """
        return self._interventions

    @property
    def pending_command_ui(self) -> dict | None:
        """F4: a pending command-UI request (e.g. the /rewind picker) for a
        front-end to render, or None. The inline region polls this; --cui renders
        a text fallback. Set by the producing slash handler, cleared on consume."""
        return self._pending_command_ui

    def set_pending_command_ui(self, payload: dict | None) -> None:
        """Set (or clear, with None) the pending command-UI request."""
        self._pending_command_ui = payload

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

    @property
    def hook_driven_turns(self) -> int:
        """Read-only accessor for the hook-driven-turns loop-valve counter (#2884).

        Snapshot-backed (``AgentSnapshot.hook_driven_turns`` — see
        ``restore_state`` / ``SnapshotJournal.record_hook_driven_turns``) so
        tests and observability can read the crash-durable value without
        reaching into the private ``_hook_driven_turns`` field.
        """
        return self._hook_driven_turns

    def _is_turn_cancel_requested(self) -> bool:
        """Forwarding → RouterLoopDriver.is_cancel_requested (PR-3)."""
        return self._loop_driver.is_cancel_requested()

    def set_pipeline_registry(self, registry: "PipelineRegistry") -> None:
        """Swap this session's ``PipelineRegistry`` post-construction (#3093).

        Dual-write — mirrors ``_reapply_pipelines``'s tail exactly:
        ``RouterHostAdapter`` holds its OWN ``_pipeline_registry`` attribute
        captured at construction and never re-reads Session, so both holders
        must be reassigned or the adapter's copy (the one ``run_pipeline``
        actually resolves ``call``/``match`` targets against, via
        ``get_pipeline_registry()``) would silently keep serving the stale
        registry.

        Used by two callers: (1) ``_reapply_pipelines`` itself (the hot-reload
        seam, after a full rebuild-from-disk), and (2)
        ``session_api._spawn_pipeline_driver_session`` (#3093), which seeds a
        freshly-spawned PIPELINE DRIVER session with the LAUNCHING caller's
        current (already hot-reloaded) registry instead of the frozen
        ``SessionFactoryConfig.pipeline_registry`` snapshot every spawn
        otherwise inherits (built ONCE per frontend at startup — a plugin/
        pipeline installed mid-conversation is invisible to it). Without this,
        a driver-session resolves its OWN pipeline by VALUE (no lookup — the
        whole ``Pipeline`` is serialized into ``invocation.json``), but a
        ``call``/``match`` step's SIBLING target is looked up BY NAME against
        this registry at run time — so a just-installed pipeline's main entry
        appears to work while any sibling it calls fails "not registered"."""
        self._pipeline_registry = registry
        self._router_host._pipeline_registry = registry

    def set_loop_driver(self, driver: "ExecutionDriver") -> None:
        """IS-2: swap this session's execution driver post-construction.

        The pipeline driver-session seam: ``spawn_session`` builds every
        session through the fixed one-arg factory (default ``RouterLoopDriver``),
        and the crash-recovery scan re-creates driver-sessions through that
        same factory — so per-session driver injection happens HERE, after
        construction, at both birth sites uniformly (the post-ctor observer
        seam; the discarded default driver is accepted overhead). Safe by
        construction: ``_loop_driver`` is only read at call time (run_turn /
        cancel forwarding), and the swap always precedes the run-loop start.

        A driver exposing ``bind_session`` (``PipelineExecutorDriver``) is
        handed this session + its RouterHostAdapter so it can build the
        tool-step ToolContext from the session's OWN (narrowed) context."""
        self._loop_driver = driver
        bind = getattr(driver, "bind_session", None)
        if callable(bind):
            bind(self, self._router_host)

    async def cancel_inflight(self) -> str:
        """#1468/#2242: cancel all in-flight work — running turn + tasks/plans.

        Single seam called by both TUI (local mode) and WS handler (remote
        mode). Returns a human-readable summary string.

        #1468 cooperative layer: sets the cooperative cancellation flag so the
        turn's run_loop breaks at the next tool-iteration boundary. A slow tool
        already in flight completes before the cancel takes effect (subprocess
        kill is a follow-up scope). Any spawned tasks are cancelled immediately
        via asyncio task cancellation (existing behaviour, preserved here).

        #2242 hard layer: ALSO cancels ``_turn_owner_task`` directly (the
        per-turn sub-task ``run_one_iteration`` spawns to run ``_run_turn_body``
        — see that method). This is what actually stops a mid-flight LLM call:
        the cooperative flag above is only checked at the top of each router-loop
        iteration (BEFORE the next LLM call), so it cannot interrupt one already
        in flight; a direct ``Task.cancel()`` injects ``CancelledError`` at
        whatever await point the task is currently suspended on — for a
        generating turn, that is the ``litellm.acompletion`` await itself, so
        the underlying HTTP request aborts and the spinner stops immediately
        instead of waiting out the response. ``_turn_cancel_self_initiated`` is
        set first so ``run_one_iteration`` can tell this cancellation apart from
        an externally-cancelled driver task and swallow only this one (see that
        flag's docstring). ``Task.cancel()`` returns False (no-op, flag left
        unset) when the task is already done, so a cancel racing turn completion
        never mis-tags a later, unrelated cancellation.

        #2588: after cancelling this session's own turn, forward the cancel to
        every registered cancel-forward target (see ``register_cancel_forward``).
        No-op for an ordinary turn (the list is empty), so the normal turn-cancel
        path is unchanged; the one live user is ``run_pipeline_attached``, which
        registers the spawned pipeline driver-session's ``request_cancel`` for the
        duration of a sync attached run so a Ctrl-C here reaches the driver.
        """
        self._loop_driver.request_cancel()
        if self._turn_owner_task is not None and self._turn_owner_task.cancel():
            self._turn_cancel_self_initiated = True
        for forward in list(self._cancel_forward_targets):
            forward()
        return "✗ cancelled turn"

    def register_cancel_forward(self, forward: "Callable[[], None]") -> "Callable[[], None]":
        """#2588: register ``forward`` to also fire on the next ``cancel_inflight``.

        Returns an idempotent unregister closure — the caller MUST invoke it
        (try/finally) when the forward is no longer relevant so it does not leak
        past its window. Used by ``run_pipeline_attached``: while the caller is
        attached-and-pumping a spawned pipeline driver-session, register that
        driver's ``request_cancel`` so a Ctrl-C on THIS (the attached caller)
        session — which only cancels THIS session's own ``_loop_driver`` — also
        reaches the driver-session's cooperative cancel flag (the executor polls
        it at each step boundary). Unregistered when the attached run ends, so
        the bridge never fires for a later, unrelated turn."""
        self._cancel_forward_targets.append(forward)

        def _unregister() -> None:
            try:
                self._cancel_forward_targets.remove(forward)
            except ValueError:
                pass  # already removed — idempotent

        return _unregister

    async def await_quiescent(self) -> None:
        """Block until every append-capable task has settled (ADR-0038 Stage 1c).

        Used by global rewind: after ``cancel_inflight()``, the caller awaits this
        so the rewind reset-record is appended only once every in-flight operation
        has settled. **Critical invariant**: when this returns, no WAL append can
        still land — a straggler past the reset-record seq would contaminate the
        active branch. It *waits for* cooperative in-flight tool/subprocess work to
        settle (whose append lands before the reset-record, inside the abandoned
        segment) rather than returning early; subprocess hard-kill is a wall-clock
        optimization, not a correctness prerequisite.

        Coverage (the exhaustive set of append-capable spawned tasks in this
        surface — see #1533 source→gated-by table): the current turn (``_turn_idle``),
        chain-timeout watchdogs (``_chains`` timers, cancel+join), and fire-and-forget
        WAL-append tasks — intervention dispatch + intervention_answer_consumed
        (``_inflight_wal_tasks``).
        """
        # 1. wait for the current turn (if any) to finish its WAL appends.
        # Re-entrancy guard: if the caller IS the current turn task (e.g. a slash
        # handler calling registry.checkout while the turn is still in progress),
        # skip the wait — awaiting _turn_idle from the same task that cleared it
        # would deadlock (single-task asyncio: nobody else can set it).  The slash
        # handler makes no WAL appends before calling checkout, so skipping is safe.
        if asyncio.current_task() is not self._turn_owner_task:
            await self._turn_idle.wait()
        # 2. cancel + join chain-timeout watchdogs. A cancelled timer cannot fire
        #    (no chain_timeout_fired append); join settles any callback already
        #    in-progress before this returns. On reconstruct, restore() re-arms a
        #    fresh watchdog from the recovered snapshot, so cancelling is reversible.
        await self._chains.cancel_and_join_timers()
        # 3-4. RE-DRAIN LOOP (#2115): cancel the tracked fire-and-forget WAL tasks
        #    (cancel — not join-only — is required: the intervention-dispatch task
        #    awaits the user-answer future indefinitely; the tasks are drop-safe so
        #    cancelling is correct) + join the append-capable tasks
        #    (_inflight_wal_tasks), then RE-CHECK. A joined task may schedule a NEW
        #    tracked append (or re-spawn) DURING the gather — which the prior
        #    one-shot snapshot would miss (#2115). Loop to a fixpoint (both sets fully
        #    drained) so no append can land after this returns. On reconstruct,
        #    restore() re-arms timers from the recovered snapshot, so cancelling is
        #    reversible.
        for _ in range(_QUIESCE_MAX_ROUNDS):
            for task in list(self._inflight_wal_tasks):
                if not task.done():
                    task.cancel()
            pending = [
                t for t in self._inflight_wal_tasks
                if not t.done()
            ]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        else:
            logger.warning(
                "await_quiescent: WAL-append tasks did not drain to a fixpoint in "
                "%d rounds — a straggler append may race the reset-record",
                _QUIESCE_MAX_ROUNDS,
            )
        # 5. re-confirm turn-idle — a joined task may have enqueued a follow-up
        #    turn; with cancel already requested it breaks immediately, so this
        #    settles. The double wait closes the join↔turn race.
        if asyncio.current_task() is not self._turn_owner_task:
            await self._turn_idle.wait()

    def _track_wal_task(self, task: asyncio.Task) -> asyncio.Task:
        """Register a fire-and-forget WAL-append task for quiescence (Stage 1c).

        Fire-and-forget tasks that append to the WAL (intervention dispatch,
        intervention_answer_consumed) have no natural join handle, so they would
        escape ``await_quiescent`` and could append past a rewind reset-record.
        Tracking them in ``_inflight_wal_tasks`` (with discard-on-done to keep the
        set bounded) makes them joinable. Returns the task for call-site chaining.

        #2115 CONVENTION: every async WAL-append spawned outside the current turn —
        ESPECIALLY any completion append (WAL writes outside the current turn) — MUST be tracked here so
        ``await_quiescent``'s re-drain joins it before
        the rewind reset-record. A new untracked append path would leak past a
        rewind (the #2115 bug class).
        """
        self._inflight_wal_tasks.add(task)
        task.add_done_callback(self._inflight_wal_tasks.discard)
        return task

    def attach_anchor_store(self, anchor_store) -> None:
        """Attach the shared per-checkpoint anchor store (#1547). Thin forwarder — see
        ``SpawnTracker.attach_anchor_store`` for the full rationale (#3133 P3 Extract Class)."""
        self._spawn_tracker.attach_anchor_store(anchor_store)

    def apply_per_session_narrowing(
        self, contextual_permission: "object | None", excluded_categories,
    ) -> None:
        """#2126: re-inject the spawner-set per-session capability narrowing AFTER
        spawn-time config resolution. Thin forwarder — see
        ``CapabilityVisibility.apply_per_session_narrowing`` for the full
        rationale (#3121 step3 Extract Class)."""
        self._capability_visibility.apply_per_session_narrowing(
            contextual_permission, excluded_categories,
        )

    # ── #2285: session-scoped LLM tool-VISIBILITY toggle (the status-bar seam) ──────────────
    # Owned by CapabilityVisibility (#3121 step3 Extract Class); Session forwards.

    async def _reapply_visibility_override_seam(self, in_set: dict) -> bool:
        """#3097: ``HotReloader``-seam wrapper for
        ``CapabilityVisibility.reapply_visibility_override`` (security-core —
        see that method's docstring for the restrict-only compose that keeps
        ``visible ⊆ authorized`` by construction). Registered so both the
        operator ``/reload`` path and ``Session.refresh_config_projections()``'s
        spawn-time family gate cover it uniformly, DERIVED from the seam registry
        rather than a hand-picked call site.

        ``in_set`` is unused — the envelope's own source is
        ``AgentRegistry.resolved_profile_for`` (topology ∩ delegate floor ∩ the
        persisted per-session narrowing config), independent of the hot-reload
        IN-set — same "in_set ignored, re-derive from the real source" shape as
        ``_reapply_skills``/``_reapply_pipelines``. Always reports a fire (there is
        no cheap way to detect a true no-op short of re-running the compose and
        diffing the result, which is exactly what re-resolving already does) —
        matches ``_reapply_hooks``'s always-True posture."""
        self._capability_visibility.reapply_visibility_override()
        return True

    def set_capability_visible(self, kind: str, name: str, visible: bool) -> None:
        """#2285: toggle the session-visibility of a tool / mcp / category / skill
        (status-bar seam). Thin forwarder — see
        ``CapabilityVisibility.set_capability_visible`` for the full rationale
        (#3121 step3 Extract Class)."""
        self._capability_visibility.set_capability_visible(
            kind, name, visible, self._toggle_store_dir(),
        )

    def capability_visibility_state(self) -> dict:
        """#2285: the status-bar's read model. Thin forwarder — see
        ``CapabilityVisibility.capability_visibility_state`` for the full
        rationale (#3121 step3 Extract Class)."""
        return self._capability_visibility.capability_visibility_state()

    # ── #2285: session-scoped hook APPLICABILITY toggle (the status-bar seam) ──────────────

    def set_hook_enabled(self, name: str, enabled: bool) -> None:
        """#2285: enable/disable a hook by name for THIS session (status-bar seam).

        Live at the next dispatch — the per-session HookDispatcher gate consults ``_disabled_hooks``.
        Session-scoped by construction: each session owns its dispatcher + disabled-set, so S1's
        disable does NOT affect S2 (even though hook CONFIG is shared). Persists across restart (step2)."""
        if enabled:
            self._disabled_hooks.discard(name)
        else:
            self._disabled_hooks.add(name)
        self._persist_hook_disabled()  # #2285 step2 — survive restart (best-effort)

    # ── #2285 step2: persist / restore the session toggles (SEPARATE from the envelope floor) ──

    def _toggle_store_dir(self) -> Path:
        """The per-session state dir holding the toggle stores (parent of the snapshot path — set
        per (name, sid) by spawn_session; the agent state dir for the main session)."""
        return Path(self._snapshot_path).parent

    def _persist_hook_disabled(self) -> None:
        """#2285 step2: persist the hook disabled-set to ``<state dir>/hooks.yaml``'s ``disabled:``
        list — distinct from that file's session-DEFINED ``hooks:`` (the 4th config layer). Preserves
        the ``hooks:`` section. Best-effort."""
        import yaml
        try:
            path = self._toggle_store_dir() / "hooks.yaml"
            data: dict = {}
            if path.is_file():
                try:
                    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                    data = loaded if isinstance(loaded, dict) else {}
                except Exception:  # noqa: BLE001
                    data = {}
            if self._disabled_hooks:
                data["disabled"] = sorted(self._disabled_hooks)
            else:
                data.pop("disabled", None)
            if data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(data), encoding="utf-8")
            elif path.exists():
                path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("#2285: persist hook disabled-set failed: %r", exc)

    def load_persisted_toggles(self) -> None:
        """#2285 step2: restore the persisted visibility override + hook disabled-set from the
        per-session stores into the in-memory sets, then re-apply visibility. Called at BOTH
        session-creation paths (spawn fixup + construction/restore) so a restarted session recovers
        its toggles. The loaded override composes ON TOP of the authoritative envelope exactly like
        the live path (just file-sourced) → visible ⊆ authorized survives persist + reload (the floor
        is re-resolved fresh from ``resolved_profile_for``; the loaded override never touches it).
        Best-effort. The visibility-override half of the load is delegated to
        ``CapabilityVisibility.load_persisted`` (#3121 step3 Extract Class — out of
        scope for the move itself: this method also loads the hook disabled-set, a
        distinct subsystem this step does not touch)."""
        import yaml
        state_dir = self._toggle_store_dir()
        self._disabled_hooks = set()
        vdata: dict = {}
        try:
            vpath = state_dir / "visibility.yaml"
            if vpath.is_file():
                loaded = yaml.safe_load(vpath.read_text(encoding="utf-8"))
                vdata = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("#2285: load visibility override failed: %r", exc)
            vdata = {}
        # ``load_persisted`` unconditionally resets to a clean baseline first so the load fully
        # re-derives from THIS (final) state dir — idempotent + leak-free if called more than once
        # or after the per-session dir is re-keyed (matches the pre-extraction unconditional reset).
        loaded_visibility, loaded_skill_visibility = self._capability_visibility.load_persisted(vdata)
        try:
            hpath = state_dir / "hooks.yaml"
            if hpath.is_file():
                data = yaml.safe_load(hpath.read_text(encoding="utf-8"))
                disabled = data.get("disabled") if isinstance(data, dict) else None
                if isinstance(disabled, list):
                    self._disabled_hooks = {str(n) for n in disabled}
        except Exception as exc:  # noqa: BLE001
            logger.warning("#2285: load hook disabled-set failed: %r", exc)
        if loaded_visibility:
            self._capability_visibility.reapply_visibility_override()  # only re-resolve when something was actually loaded
        if loaded_skill_visibility:
            self._capability_visibility.reapply_skill_visibility()  # #2548 PR-B: restore skill filter on the host

    def hook_state(self) -> "list[dict]":
        """#2285: the status-bar's hook read model — each NAMED hook in this session's merged
        registry (startup ∪ runtime ∪ per-agent ∪ per-session) as ``{name, scope, enabled}``.
        ``scope`` = the most-specific layer that defines the name; a hook with no name is omitted
        (it can't be individually toggled). ``enabled`` = not in this session's disabled-set."""
        runtime_hooks: list = []
        try:
            from reyn.runtime.hot_reload import load_hot_reload_config
            runtime_hooks = (load_hot_reload_config(self._hot_reload_project_root()) or {}).get("hooks") or []
        except Exception:  # noqa: BLE001 — scope is best-effort display metadata
            runtime_hooks = []
        scope_by_name: "dict[str, str]" = {}
        for scope, raw in (
            ("startup", self._startup_hooks_raw),
            ("runtime", runtime_hooks),
            ("per-agent", self._read_per_agent_hooks()),
            ("per-session", self._read_per_session_hooks()),
        ):
            for hook_cfg in (raw or []):
                n = hook_cfg.get("name") if isinstance(hook_cfg, dict) else None
                if n:
                    scope_by_name[n] = scope  # more-specific layer wins (later in this order)

        registry = getattr(self._hook_dispatcher, "_registry", None)
        out: "list[dict]" = []
        seen: "set[str]" = set()
        for hook in getattr(registry, "_defs", []) if registry is not None else []:
            n = getattr(hook, "name", None)
            if n is None or n in seen:
                continue
            seen.add(n)
            out.append({
                "name": n,
                "scope": scope_by_name.get(n, "unknown"),
                "enabled": n not in self._disabled_hooks,
            })
        return out

    async def dispatch_external_event(self, point: str, template_vars: dict) -> None:
        """#2608 H5: public entry point for an OUT-OF-SESSION external-event
        source (cron / webhook ingress) to fire a hook on THIS session's
        dispatcher.

        H1 (``mcp_resource_updated``) and H4 (``file_changed``) both fire
        their hook via a ``hook_trigger`` closure captured over
        ``self._hook_dispatcher.dispatch`` INSIDE ``__init__`` (the source is
        constructed there too — ``MCPConnectionService`` / ``FsWatcher``).
        Cron and webhook ingress resolve a Session from the ``AgentRegistry``
        at fire/request time (``reyn.runtime.cron.routing.
        resolve_cron_session`` / ``reyn.runtime.webhook_routing.
        resolve_webhook_session``), long after ``__init__`` — they have no
        closure to capture, so they need a public method to reach the same
        dispatcher instead. This is a thin pass-through: ``HookDispatcher.
        dispatch`` already gives every H1/H4 guarantee (per-hook isolation —
        never raises; H2 matcher evaluated before a hook's action runs;
        empty-registry is a byte-identical no-op).
        """
        await self._hook_dispatcher.dispatch(point, template_vars)

    @property
    def current_snapshot(self) -> "AgentSnapshot":
        """Read-only view of the live in-memory AgentSnapshot (ADR-0038).

        Public accessor over the journal's snapshot so callers (e.g. the
        live-rewind gate) can assert the live session reflects as-of-N AFTER a
        global rewind — ``reset_for_rewind`` + ``restore_state`` update this live
        snapshot via ``journal.install``, a wiring distinct from the on-disk save.
        """
        return self._journal.snapshot

    async def reset_for_rewind(self) -> None:
        """Clear all in-memory state ``restore_state`` repopulates (ADR-0038 1c-2).

        Called in the global-rewind path **after** ``await_quiescent`` (every
        WAL-append task settled) and **before** ``restore_state(reconstructed)``.
        Its clear-scope EXACTLY mirrors ``restore_state``'s set-scope so that
        re-adopting the reconstructed snapshot leaves ZERO pre-rewind residue —
        a single missed holder would be stale state on the rewound branch.

        ``journal.install`` (inside restore_state) replaces the AgentSnapshot
        *data* wholesale; this clears the separate in-memory holders that
        restore_state writes into, mapped to AgentSnapshot fields:

            inbox                          → self.inbox (drain queue)
            pending_chains                 → self._chains (reset: timers + chains)
            outstanding_interventions      → self._interventions (clear)
                                             + self._restore_intervention_tasks
            buffered_intervention_answers  → self._buffered_intervention_answers
            next_turn_context              → self._next_turn_context
            hook_driven_turns              → self._hook_driven_turns (#2884; restore_state's
                                               plain assignment is unconditional, so no
                                               separate clear step is needed here)

        The _inflight_wal_tasks task handles are already settled by
        await_quiescent; this drops the (now-done) handles so the rewound
        session starts clean.
        """
        # inbox (AgentSnapshot.inbox)
        while True:
            try:
                self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
        # pending_chains
        await self._chains.reset()
        # outstanding_interventions + restore watcher tasks
        self._interventions.clear()
        restore_tasks = getattr(self, "_restore_intervention_tasks", None)
        if restore_tasks:
            for t in restore_tasks:
                if not t.done():
                    t.cancel()
            self._restore_intervention_tasks = []
        # buffered_intervention_answers
        self._buffered_intervention_answers.clear()
        # next_turn_context (#1800-4b)
        self._next_turn_context.clear()
        # hook_driven_turns (#2884): reset the loop-valve counter mirror. restore_state
        # re-assigns it wholesale from the reconstructed snapshot, but clearing here keeps
        # the zero-residue guarantee robust independent of that assignment.
        self._hook_driven_turns = 0
        self._inflight_wal_tasks.clear()

    @property
    def pending_user_images(self) -> list[dict]:
        """Read-only accessor for the per-session image upload queue.

        Tests and slash commands inspect this queue to verify that an
        uploaded image landed (= ``/image`` slash feeds this list). The write side stays on
        ``self._pending_user_images`` so the lifecycle (= drain on
        send, reset to []) is visible in the production call sites.
        """
        return self._pending_user_images

    @property
    def journal(self) -> "SnapshotJournal":
        """Read-only accessor for the session's SnapshotJournal.

        The journal carries rich public API (``append_inbox`` / ``consume_inbox`` /
        ``snapshot``); exposing the holder via a public name keeps slash
        commands and tests off the underscore field. The journal
        instance is set once in ``__init__`` and never re-bound.
        """
        return self._journal

    @property
    def task_backend(self) -> "object | None":
        """Read-only accessor for this session's Task backend (#1953 slice 3a).

        The registry hands the GLOBAL backend in at construction
        (``_construct_session``). None when the session carries no backend
        (op-runtime in-memory fallback)."""
        return self._task_backend

    def iter_applied_seqs(
        self, *, now_ts: float, long_await_threshold: float,
    ) -> "list[int]":
        """Return in-memory applied_seqs for WAL truncation floor calc.

        Surfaces the watermarks AgentRegistry.compute_truncate_floor
        needs from this session, sourced exclusively from in-memory
        state (= journal snapshot). No disk I/O — preserves the
        existing reyn architecture choice
        (event loop friendly, no thread offload, in-memory state is
        event-sourced from WAL apply).

        Yielded watermarks:
          - ``journal.snapshot.applied_seq`` when > 0 (dormant agents
            with applied_seq == 0 are skipped — the same skip the
            disk-read path used so behaviour matches)

        The ``now_ts`` / ``long_await_threshold`` parameters are retained
        for the caller's uniform signature; there is no longer a per-run
        registry contributing additional watermarks (stage1 decouple).
        """
        out: list[int] = []
        snap_applied = int(self._journal.snapshot.applied_seq)
        if snap_applied > 0:
            out.append(snap_applied)
        # Skill-execution machinery removed (stage1 decouple): there is no live
        # skill registry contributing per-skill last_phase_applied_seq floors.
        return out

    def _effective_contextual_for_turn(self) -> "object | None":
        """#1827 S4b (context-auto): the per-session contextual narrowing for THIS
        turn.

        When untrusted external content is live in the active context (a history
        entry carrying the #1862 ``external_source`` marker), compose the minimal
        ``_untrusted`` profile with the static (topology) narrowing —
        most-restrictive (union-of-excludes) — so a partial prompt-injection has
        no dangerous tools to reach. The taint is derived from the active history,
        so it **self-clears** once the untrusted entry compacts out
        (until-compaction scope). Untrusted absent → the static contextual
        (byte-identical to pre-S4b).
        """
        from reyn.security.permissions.capability_profile import metas_have_untrusted

        if not metas_have_untrusted(m.meta for m in self.history):
            return self._capability_visibility.contextual_permission
        from reyn.security.permissions.capability_profile import (
            compose_resolved,
            load_untrusted_profile,
            resolve_profile,
        )
        if self._untrusted_contextual_cache is None:
            root = self._perm.project_root if self._perm is not None else Path.cwd()
            self._untrusted_contextual_cache = resolve_profile(
                load_untrusted_profile(root)
            )[0]
        resolved = [(self._untrusted_contextual_cache, frozenset())]
        if self._capability_visibility.contextual_permission is not None:
            resolved.insert(0, (self._capability_visibility.contextual_permission, frozenset()))
        return compose_resolved(resolved)[0]

    # ── persistence ─────────────────────────────────────────────────────────────

    def _append_history(self, msg: ChatMessage) -> None:
        # Assign monotonic seq for conversational entries (user/agent). Other
        # roles (summary) keep seq=0 — they aren't part of the
        # turn ordering used by the slicer.
        if msg.role in ("user", "agent") and msg.seq == 0:
            msg.seq = self._next_seq
            self._next_seq += 1
        # #2360: anchor each turn to the WAL seq at append time so the conversation
        # rides the GLOBAL rewind/branch derivation (is_active_seq). Time-travel is
        # global (checkout jumps the whole world's active cut), so a rewound world
        # must hide conversation turns whose anchor is on an abandoned branch — else
        # runtime state rewinds but the LLM still sees post-cut turns. meta is
        # excluded from the wire dicts build_history emits, so wal_seq never reaches
        # the LLM. Guarded on state_log presence (no WAL → no rewind → always visible)
        # and skipped if already anchored (a re-append keeps its original anchor).
        if self._state_log is not None and "wal_seq" not in msg.meta:
            msg.meta["wal_seq"] = self._state_log.current_seq
        self.history.append(msg)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    def _active_branch_history(self) -> "list[ChatMessage]":
        """#2360: the conversation turns visible on the current active branch.

        The LLM-facing ``build_history`` slices whatever this returns, so filtering
        here makes the conversation follow the GLOBAL time-travel cut without
        touching the append-only ``history.jsonl``. Each turn carries a WAL anchor
        (``meta['wal_seq']``, stamped at append); a turn is visible iff its anchor is
        on the active branch as-of the current rewind cut — reusing the WAL
        branch-derivation (``is_active_seq``). Rewind moves the cut back (higher
        anchors drop out); fork-switch makes an alternate branch's anchors active;
        the future/other-branch turns stay in the file, just outside the visible
        prefix. Turns without an anchor (pre-#2360 entries, or no state_log) are
        always visible (backward-compatible, no migration)."""
        if self._state_log is None:
            return self.history
        from reyn.core.events.snapshot_generations import build_active_predicate

        # #2941: hoisted OUT of the per-message loop below. The abandoned-interval
        # predicate depends only on the state_log's rewind records, never on a
        # per-message seq — so it is computed ONCE per call (one WAL scan) and
        # reused for every message, instead of re-scanning the whole WAL per
        # message (was O(N messages x M WAL entries) per turn; now O(N + M)).
        is_active = build_active_predicate(self._state_log)

        def _active(seq: "int | None") -> bool:
            return seq is None or is_active(seq)

        # #2360 (tool-cycle-aware): a GLOBAL cut lands at a WAL seq that may be a turn boundary for
        # the rewound session but fall MID-tool-cycle for another session's conversation (the
        # assistant tool_calls turn's anchor ≤ cut while its tool result turns' anchors > cut, or
        # the reverse). A flat per-turn filter would then emit a dangling tool_calls-without-results
        # or tool-result-without-tool_calls → provider BadRequest (the #2290/#2289 adjacency class).
        # So a tool cycle (an assistant tool_calls turn + its immediately-following tool result
        # turns) is ONE atomic visible unit, governed by the assistant turn's anchor: the whole
        # cycle is visible iff that anchor is active. Well-formed by construction.
        out: list[ChatMessage] = []
        governing_seq: "int | None" = None  # the open cycle's assistant-tool_calls anchor
        cycle_open = False
        for m in self.history:
            if m.role == "tool" and cycle_open:
                eff = governing_seq  # a tool result inherits its cycle's visibility
            else:
                eff = m.meta.get("wal_seq")
                cycle_open = m.role == "assistant" and bool(m.tool_calls)
                governing_seq = eff if cycle_open else None
            if _active(eff):
                out.append(m)
        return out

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
          state_change entries are NOT consumed by compaction
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
        ``(role, text, ts, meta)`` signature to Session._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(
            role="assistant" if role == "agent" else role,
            content=text, ts=ts, meta=meta,
        ))

    def _append_history_for_inter_agent_messaging(
        self, role: str, text: str, ts: str, meta: dict,
    ) -> None:
        """Adapter callback injected into InterAgentMessaging.

        InterAgentMessaging uses the same ``(role, text, ts, meta)`` signature as
        InterventionHandler.  This adapter bridges to Session._append_history
        (which takes a ChatMessage).
        """
        self._append_history(ChatMessage(
            role="assistant" if role == "agent" else role,
            content=text, ts=ts, meta=meta,
        ))

    # ── A2A transport callbacks (FP-0019 Wave 2 part 2) ─────────────────────────
    # Session-side wrappers that perform registry topology checks and the
    # actual submit_agent_request / submit_agent_response transport calls.
    # InterAgentMessaging delegates here after its own depth / guard logic; these
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
        # #2081: the A2A REQUEST path is a delegation by definition → mark the
        # target a delegate (recorded on first construction; recursive — a
        # sub-delegate's own delegations pass is_delegate=True too). The response
        # path (_a2a_send_response) does NOT — the delegator's own delegate-ness was
        # decided when it was constructed.
        target = self._registry.get_or_load(to, is_delegate=True)
        await self._registry.ensure_running(to)
        await target.submit_agent_request(
            from_agent=from_agent, request=request,
            depth=depth, chain_id=chain_id,
            # #2130: thread THIS delegating session's sid so the peer's reply routes back
            # to (from_agent, from_sid) — a non-main session that DELEGATES (not just spawns)
            # gets its reply, not the agent's main. "main" → the default path (byte-identical;
            # the _a2a_send_response branch treats absent/"main" as the unchanged main-case).
            # In-process delegation only; a cross-process external peer that doesn't echo
            # from_sid degrades to None→main (safe).
            from_sid=self._session_id,
        )

    async def _a2a_send_response(
        self,
        to: str, from_agent: str, response: str, depth: int, chain_id: str,
        responder_sid: "str | None" = None, to_sid: "str | None" = None,
    ) -> None:
        """Transport callback: submit agent_response to ``to`` (#2130: at ``to_sid``).

        Silently drops when the target no longer exists (race on shutdown).
        ``responder_sid`` (#2103 S1bc-exec) carries the responder's own sid when it is a
        spawned session, so the receiver can correlate the result to its spawn record.

        #2130 first-class (agent, sid) routing: ``to_sid`` is the REQUESTER's session id.
        - absent / "main" → the DEFAULT path, byte-identical to pre-#2130: ``get_or_load``
          (disk-loads a cold main) + ``ensure_running`` (run() + the user-facing forwarder).
          This serves the classic peer-A2A case where ``to``'s main may be unloaded.
        - a non-main sid → deliver to that SPECIFIC spawned (spawner) session via the
          in-memory ``get_session`` (the spawner is always warm at result-route time — its
          run-loop idles on a pending chain that suppresses ephemeral-vanish; and
          ``get_or_load`` cannot reconstruct a non-main sid from disk anyway). No forwarder
          is needed (inbound arrives via inbox+run(); the forwarder is user-facing-output
          only, and a non-main session has none). FAIL-SAFE: a gone spawner (get_session
          None) is LOGGED + DROPPED — never a fallback to main, which would re-introduce the
          very misroute #2130 fixes (a logged drop > a silent misroute).
        """
        if self._registry is None:
            # #2103 S1bc-exec hardening: a result-routing path that silently no-ops on an
            # unwired registry is a bad failure mode — fail LOUD (logged) so a mis-wiring
            # surfaces. Production wires the registry; this guards the regression.
            logger.warning(
                "a2a response to %r dropped: session has no registry wired (mis-wiring; "
                "the result-routing path is inert)", to,
            )
            return
        if not self._registry.exists(to):
            return
        if to_sid is not None and to_sid != "main":  # "main" = registry._DEFAULT_SID (no import cycle)
            # #2130 spawner-sid delivery: the specific non-main session, in-memory only.
            target = self._registry.get_session(to, to_sid)
            if target is None:
                logger.warning(
                    "a2a response to (%r, %r) dropped: the spawner session is no longer "
                    "loaded (fail-safe — NOT routed to main, which would misroute)",
                    to, to_sid,
                )
                return
            self._registry.ensure_session_running(to, to_sid)
        else:
            # default / main-case: UNCHANGED (cold-load + forwarder) — byte-identical.
            target = self._registry.get_or_load(to)
            await self._registry.ensure_running(to)
        await target.submit_agent_response(
            from_agent=from_agent, response=response,
            depth=depth, chain_id=chain_id, responder_sid=responder_sid,
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

    async def submit_user_text(
        self, text: str, *, attribution: "dict | None" = None,
    ) -> None:
        # PR14: every top-level user submission starts a fresh chain_id that
        # propagates through any agent_request / agent_response generated in
        # response. Logged in history meta + events.jsonl for cross-agent trace.
        await self._put_inbox(
            "user", {"text": text, "chain_id": _new_chain_id()},
        )
        # ADR-0039 multi-client input-broadcast fix: put the user's OWN turn on
        # `session.outbox` too (not just the inbox that drives the turn), so it
        # rides the SAME `outbox_hub` fan-out (P6b-1) the agent's reply already
        # broadcasts through. Before this, a 2nd+ thin client (`reyn chat
        # --connect`) saw the agent's reply with no prompt (half a conversation)
        # — the local scrollback echo was a LOCAL-ONLY `transport.put_display`
        # injection that never reached the hub (removed from
        # `interfaces/inline/app.py._do_submit`; every client, including this
        # submitting one, now renders its own line from THIS broadcast frame —
        # single source of truth, no double-render).
        #
        # The DISPLAY copy is neutralized (ESC/control strip — same
        # `core/present/guard.get_neutralizer("terminal")` seam #2770 uses for
        # intervention content) because this text now reaches every attached
        # peer's terminal, not only the operator's own (a new cross-client
        # surface a purely-local echo never had). The INBOX copy above (what the
        # agent/router actually reads) stays raw — display neutralization never
        # touches conversation content.
        from reyn.core.present.guard import get_neutralizer
        await self._put_outbox(OutboxMessage(
            kind="user",
            text=get_neutralizer("terminal").neutralize(text)[0],
            meta=_user_frame_meta(attribution),
        ))

    async def submit_agent_request(
        self, *, from_agent: str, request: str, depth: int, chain_id: str,
        from_sid: "str | None" = None,
    ) -> None:
        await self._put_inbox("agent_request", {
            "from_agent": from_agent, "request": request, "depth": depth,
            "chain_id": chain_id,
            # #2130: the REQUESTER's session id — so this request's response routes back to
            # the specific (from_agent, from_sid), not the requester agent's main session.
            # None → main-case (byte-identical to pre-#2130).
            "from_sid": from_sid,
        })

    async def submit_agent_response(
        self, *, from_agent: str, response: str, depth: int, chain_id: str,
        responder_sid: "str | None" = None,
    ) -> None:
        await self._put_inbox("agent_response", {
            "from_agent": from_agent, "response": response, "depth": depth,
            "chain_id": chain_id,
            # #2103 S1bc-exec: the responder's own session id when it is a SPAWNED
            # (non-main) session — the correlation key the receiver matches against its
            # _spawned_tasks record to render the trusted "task=" header.
            "responder_sid": responder_sid,
        })

    async def submit_pipeline_result(
        self, *, run_id: str, pipeline_name: str, status: str, text: str,
        chain_id: "str | None" = None,
    ) -> None:
        """IS-2: deliver an async pipeline run's terminal result to this session.

        The ``agent_response`` mirror for the pipeline driver-session
        architecture: the invoker's ``run_pipeline_async`` returned
        ``{status: started}`` immediately (no pending chain), so the result
        arrives as a NEW turn trigger — ``run_one_iteration`` routes the
        ``pipeline_result`` kind to one router turn (like a task wake), with
        ``text`` the OS-framed message the driver formatted. Delivery is
        at-least-once (the driver's terminal marker is written only after this
        lands — see ``reyn.core.pipeline.work_order``), so a consumer that
        must dedup can key on ``run_id``."""
        await self._put_inbox("pipeline_result", {
            "run_id": run_id, "pipeline_name": pipeline_name, "status": status,
            "text": text, "chain_id": chain_id or _new_chain_id(),
            "sender": "pipeline:os",
        })

    # ── #2103 S1bc-exec: spawned-task correlation record (bounded) ──────────────
    # Owned by SpawnTracker (#3133 P3 Extract Class); Session forwards.

    def record_spawned_task(self, sid: str, task: str) -> None:
        """Record a session-I-spawned's ``sid → task`` BEFORE submitting it. Thin
        forwarder — see ``SpawnTracker.record_spawned_task`` for the full rationale."""
        self._spawn_tracker.record_spawned_task(sid, task)

    def lookup_and_evict_spawned_task(self, sid: "str | None") -> "str | None":
        """The TRUSTED task for a spawned ``sid``, or None. Thin forwarder — see
        ``SpawnTracker.lookup_and_evict_spawned_task`` for the full rationale."""
        return self._spawn_tracker.lookup_and_evict_spawned_task(sid)

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

    async def refresh_mcp_servers(self) -> dict:
        """Programmatic MCP-tools refresh — re-probe configured servers + reload cache.

        Calls the same 3-step turn-boundary chain that fires implicitly on each
        user message:

          1. ``RouterHostAdapter.maybe_refresh_mcp_tools_from_yaml()`` (S2)
             — re-stats yaml scope tiers, re-probes when any mtime advanced.
          2. ``RouterHostAdapter.maybe_reload_mcp_tools_cache_from_disk()`` (S1)
             — picks up the on-disk cache file if newer than the in-memory cache.
          3. ``RouterHostAdapter.ensure_mcp_tools_cached()`` (#160 lazy probe)
             — first-call fallback when neither (1) nor (2) populated the cache.

        Use cases (FP-0037 #164):
          - Test scenarios where MCP config changes mid-test.
          - Chat turns that install a new MCP server and want it visible within
            the same chat session (= without waiting for the operator to
            run ``reyn mcp refresh`` or for a yaml mtime advance).

        Returns a dict snapshot::

            {
              "refreshed": bool,        # True iff (1) or (2) actually swapped the cache
              "servers": {<name>: <tool_count>, ...},  # in-memory cache after refresh
            }

        On failure a defensive ``"error"`` key is added and ``"refreshed"``
        is False — the method never raises.
        """
        # Snapshot the cache before the chain so we can detect a swap.
        snapshot_before = self._router_host.mcp_tools_cache_snapshot

        # #2372: re-read the server ROSTER from the config cascade BEFORE the tool-probe
        # chain. Refreshing the tools cache alone is insufficient — the LLM-facing
        # enumeration (_get_mcp_servers_for_router → _mcp_servers_flat) gates on the roster,
        # which is otherwise frozen at ctor (self._mcp_servers → adapter). A server installed
        # mid-session (mcp_install writes the IN-set .reyn/config/mcp.yaml) has no roster entry
        # to attach its tools to → never enumerated. load_config's cascade MERGES that IN-set
        # (loader.py: dynamic_mcp), so re-reading here picks up the install. Multi-holder swap
        # (mirrors _reapply_per_agent_capability): the Session field AND the adapter's roster —
        # the enumeration reads the adapter's. Best-effort: a re-read failure keeps the old
        # roster (never breaks the refresh).
        try:
            from reyn.config.loader import load_config
            fresh_roster = load_config(self._hot_reload_project_root()).mcp
            self._mcp_servers = fresh_roster
            self._router_host._mcp_servers = fresh_roster
        except Exception as exc:  # noqa: BLE001 — roster re-read is best-effort
            logger.warning("refresh_mcp_servers: roster re-read failed: %r", exc)

        try:
            # Step 1 (S2): yaml mtime watch — re-probes when any yaml changed.
            await self._router_host.maybe_refresh_mcp_tools_from_yaml()
            # Step 2 (S1): disk-reload — picks up CLI refresh written between turns.
            self._router_host.maybe_reload_mcp_tools_cache_from_disk()
            # Step 3 (#160): lazy probe — fills cache on first call.
            await self._router_host.ensure_mcp_tools_cached()
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh_mcp_servers: turn-boundary chain raised: %r", exc)
            snapshot_after = self._router_host.mcp_tools_cache_snapshot or {}
            return {
                "refreshed": False,
                "servers": {
                    name: len(tools)
                    for name, tools in snapshot_after.items()
                },
                "error": str(exc),
            }

        snapshot_after = self._router_host.mcp_tools_cache_snapshot or {}

        # Detect cache swap: compare id() of the snapshot objects.
        # snapshot_before is a *copy* taken before the chain (or None when no
        # cache existed yet). snapshot_after is a fresh copy taken after.
        # The underlying adapter replaces _mcp_tools_cache with a new dict
        # whenever a reload/probe fires. Because both snapshots are independent
        # copies, we compare their content rather than identity to decide
        # whether the visible cache actually changed.
        refreshed = snapshot_before != snapshot_after

        return {
            "refreshed": refreshed,
            "servers": {
                name: len(tools)
                for name, tools in snapshot_after.items()
            },
        }

    # ── #3097: config-projection refresh family-gate ─────────────────────────

    async def refresh_config_projections(self) -> dict:
        """#3097 (#3061 follow-up): fire EVERY registered config hot-reload seam
        (``_register_hot_reload_seams()``) at THIS session's own ephemeral/spawn
        action-boundary — EXCLUDING ``cron`` (the one genuinely SIDE-EFFECTING
        seam: it mutates the global scheduler; a short-lived programmatically-
        spawned worker must never reschedule cron on its own, and has no active
        scheduler to reschedule anyway).

        Closes the #3036/#3061 gap the RAG turnkey flow hit: a programmatic spawn
        (``AgentRegistry.spawn_session_recorded`` — every agent-step ephemeral
        worker, pipeline driver-session, and ``session_spawn``/``delegate_to_agent``
        target) never fires a chat "turn boundary" of its own before its first
        dispatch, so every one of its config-derived projections (MCP roster,
        pipeline/presentation/skill registries, hooks, per-agent capability, the
        session visibility override, …) is otherwise frozen at whatever the
        (baked-once-at-registry-construction) ``session_factory`` closure
        captured — stale even for config an install wrote moments before this
        spawn. #3061 closed this for MCP alone; #3094 point-fixed the pipeline
        registry alone after it surfaced live — this closes the WHOLE family
        uniformly, DERIVED from the seam registry (never a hand-picked subset),
        so a future ``register_seam`` addition is covered on registration, with
        no one needing to remember it.

        Every included seam is a read-only projection (re-read the IN-set /
        cascade → swap or re-derive) or a confirming no-op
        (``_reapply_new_agent``) — never a mutation of anything outside this
        session's own in-memory holders — so firing them off the chat turn
        boundary is idempotent-safe: a spawn with unchanged config is a no-op,
        and a spawn racing a fresh install simply picks up the fresher state.

        ``_reapply_visibility_override`` (security-core: visible ⊆ authorized)
        is included — firing it here re-resolves the JUST-SPAWNED session's OWN
        envelope from its CURRENT base (topology ∩ delegate floor ∩ persisted
        per-session narrowing) ∩ its (empty, freshly-constructed) override, which
        can only narrow relative to the authorized envelope, never grant beyond
        it (see that method's own docstring for why the compose is restrict-only
        by construction).

        ``_reapply_skill_visibility`` has no seam of its own — it is already
        re-derived as the tail of the ``skills`` seam (``_reapply_skills``)
        whenever the base skill set changes, so it is covered transitively.

        MUST NOT be called on a crash-recovery re-wake (``AgentRegistry.
        restore_all`` / ``_rewake_pipeline_runs``, registry.py): those paths
        call the lower-level ``spawn_session`` directly (never
        ``spawn_session_recorded``, the sole caller of this method), by
        construction — recovery RESTORES the pre-crash snapshot (snapshot
        fidelity), it must not overwrite it with whatever the CURRENT on-disk
        config happens to be.

        Returns the ``HotReloader.apply_all()`` summary
        (``{"source", "invoked", "applied", "failed"}``) — ``invoked`` is the
        set a completeness gate checks against ``hot_reload_seam_names()`` minus
        ``{"cron"}``. Never raises (each seam is isolated by the applier)."""
        return await self._hot_reloader.apply_all(exclude=frozenset({"cron"}))

    def hot_reload_seam_names(self) -> "tuple[str, ...]":
        """#3097: the public read of every hot-reload seam name registered on
        this session's ``HotReloader`` (``_register_hot_reload_seams()``), in
        registration order. The completeness-gate test for
        ``refresh_config_projections()`` derives its expected-coverage set from
        THIS (never a hand-written marker subset), so a future
        ``register_seam`` addition is covered automatically — the same
        registry-derived-enumeration discipline the family gate itself uses."""
        return self._hot_reloader.seam_names()

    # ── #3082 Family 1: audit-event spine builder ──

    def _build_audit_event_bundle(
        self, observability_config: "object | None"
    ) -> "_AuditEventBundle":
        """#3082 Family 1: build the audit-event (P6) spine — ``event_store``
        (disk-backed) -> ``chat_events`` (the ``EventLog`` nearly every other
        Session sub-component consumes) -> ``outbox_hub`` (the outbox
        fan-out), plus the opt-in OTEL subscriber attached to ``chat_events``.

        Byte-identical extraction of the sequence that used to run inline in
        ``__init__`` — same objects, same construction order, same args.
        Reads only attributes ``__init__`` has already set by this point
        (``self.outbox`` / ``self.agent_name`` / ``self.events_dir`` /
        ``self._events_config`` / ``self._agent_id``); takes
        ``observability_config`` explicitly since it is an ``__init__``
        parameter, not a ``self`` attribute.

        ADR-0039 P6b: the outbox is single-consumer (asyncio.Queue hands each
        item to exactly ONE getter). The hub is the SOLE ``outbox.get()``
        consumer and fans every message out to N per-surface subscriptions, so
        the local REPL forwarder and each AG-UI surface receive the FULL
        stream instead of stealing frames from one another. Drain starts
        lazily on the first ``subscribe`` (no running loop needed here at
        construction).

        P5 ADR-0039: opt-in OpenTelemetry export. Attaches a fail-open,
        off-loop OTLP subscriber to this session's EventLog ONLY when an OTLP
        endpoint is configured (observability.otel.endpoint or the
        OTEL_EXPORTER_OTLP_ENDPOINT env). With no endpoint build_otel_exporter
        returns None -> nothing attached, zero overhead, behavior
        byte-identical to no OTEL. The exporter is a lossy downstream: it
        never writes to .reyn/events or the WAL, so recovery/replay is
        independent of it (SR4)."""
        outbox_hub = OutboxHub(self.outbox, name=self.agent_name)
        event_store = EventStore(
            self.events_dir,
            max_bytes=self._events_config.max_bytes,
            max_age_seconds=self._events_config.max_age_seconds,
        )
        chat_events = EventLog(
            subscribers=[event_store],
            agent_id=self._agent_id,  # FP-0016 E: auto-inject agent_id into every event
        )
        otel_exporter = None
        try:
            from reyn.observability.otel_exporter import build_otel_exporter
            otel_exporter = build_otel_exporter(observability_config)
            if otel_exporter is not None:
                chat_events.add_subscriber(otel_exporter)
        except Exception:  # noqa: BLE001 — OTEL attach must never break session init
            otel_exporter = None
        return _AuditEventBundle(
            event_store=event_store,
            chat_events=chat_events,
            outbox_hub=outbox_hub,
            otel_exporter=otel_exporter,
        )

    # ── #3082 Family 2: WAL-event/recovery bundle builder ──

    def _build_recovery_bundle(
        self,
        agent_name: str,
        snapshot_path: Path,
        state_log: "StateLog | None",
        session_id: str,
    ) -> "_RecoveryBundle":
        """#3082 Family 2: build the WAL-event/recovery pair —
        ``generation_store`` (ADR-0038 Stage 1a PITR generation store, kept
        beside snapshot.json) -> ``journal`` (``SnapshotJournal``, wired to
        this same generation_store instance).

        Byte-identical extraction of the sequence that used to run inline in
        ``__init__`` — same objects, same construction order, same args.
        Takes ``agent_name`` / ``snapshot_path`` / ``state_log`` / ``session_id``
        explicitly rather than reaching into ``self`` mid-construction:
        ``agent_name`` is the property value already resolvable at the
        original call site, ``snapshot_path`` is ``self._snapshot_path``
        (constructed immediately before this builder ran), and — critically —
        ``state_log`` is the LOCAL ``__init__`` parameter, NOT
        ``self._state_log``. ``self._state_log = state_log`` is a separate
        tracking assignment made later in ``__init__`` (for ops that need
        direct WAL access outside the journal) and is out of scope for this
        extraction; it keeps reading the same local parameter untouched.

        ADR-0038 Stage 1a: PITR generation store, kept beside snapshot.json.

        PR21: WAL + per-agent snapshot for crash recovery. state_log is
        process-shared (owned by AgentRegistry); when None, persistence is
        disabled (tests / non-chat invocation). PR-refactor-session-1 wave 2:
        persistence now flows through SnapshotJournal (extracted service).

        FP-0043 Stage 5: session_id is the conversation session id, threaded
        to the journal so every WAL append carries it."""
        generation_store = SnapshotGenerationStore(
            agent_name, snapshot_path.parent / "generations",
        )
        journal = SnapshotJournal(
            agent_name=agent_name,
            snapshot_path=snapshot_path,
            state_log=state_log,
            generation_store=generation_store,
            session_id=session_id,  # FP-0043 S5: per-session WAL routing
        )
        return _RecoveryBundle(
            generation_store=generation_store,
            journal=journal,
        )

    # ── #3082 Family 3: hook-event / reactivity bundle builder ──

    def _build_hook_event_bundle(
        self,
        boot_in_set: "dict",
        composer_defs: list,
        fs_watch_cfg: "object",
        chat_events: "EventLog",
        registry: "AgentRegistry | None",
        session_id: str,
    ) -> "_HookEventBundle":
        """#3082 Family 3: build the hook-event / reactivity spine —
        ``hook_bus`` → ``hook_dispatcher`` → ``fs_watcher`` →
        ``composer_registry`` → ``composed_consumer`` → ``hot_reloader``, in
        dependency order.

        Byte-identical extraction of the sequence that used to run inline in
        ``__init__`` — same objects, same construction order, same args. The
        subtlety this family carries: eager sibling references use this
        builder's LOCAL variables, while deferred lambdas keep resolving
        ``self.*`` at CALL time exactly as before. Concretely — the
        HookDispatcher's ``bus=``, the ComposedEventConsumer's ``bus=`` /
        ``dispatcher=``, and each Composer's ``bus=`` are read AT
        construction, before ``__init__`` unpacks the bundle onto ``self``, so
        they must read the local ``hook_bus`` / ``hook_dispatcher``; whereas
        fs_watcher's ``hook_trigger`` and every ``emit_event`` sink are
        lambdas that fire only from ``run()`` / dispatch (long after
        __init__), so they keep resolving ``self._hook_dispatcher`` /
        ``self._chat_events`` unchanged.

        Placement (call-site in ``__init__``): this family is built AFTER the
        Family 1 audit-event bundle because it CONSUMES ``chat_events`` —
        ``hot_reloader`` reads it EAGERLY (``events=chat_events``). That is the
        #3082 pipeline's output→input order (Family 1 → Family 3), and it is
        also byte-identical to the original inline code, where the
        hot_reloader was likewise constructed after the ``chat_events`` EventLog.

        Config-derivation is a precursor threaded in explicitly rather than
        folded in: ``boot_in_set`` (the IN-set — ALSO read by cron, so it must
        stay a shared precursor, not a hook-only concern), ``composer_defs``
        (the resolved ComposerDefs — ALSO the source of ``_composed_schemas``),
        and ``fs_watch_cfg`` (the resolved FsWatchConfig). ``registry`` supplies
        the hot-reloader's project_root; ``session_id`` is the dispatcher's
        cross-session-routing self-id.

        None of the six constructors (nor FsWatcher's inner FsIngressAdapter)
        starts a thread / task / observer — each just stores its args
        (FsWatcher keeps ``_observer=None`` / ``_started=False``;
        ComposerRegistry / ComposedEventConsumer keep ``_tasks=[]`` /
        ``_task=None`` until ``start()`` is called from ``run()``), so gathering
        them here (moving the FsWatcher / HookBus constructions down from their
        former, earlier positions) re-times no side effect."""
        from reyn.hooks.bus import HookBus
        from reyn.hooks.composed_consumer import ComposedEventConsumer
        from reyn.hooks.composer import Composer, ComposerRegistry
        from reyn.hooks.dispatcher import HookDispatcher
        from reyn.runtime.fs_watcher import FsWatcher
        from reyn.runtime.hot_reload import HotReloader
        # Hook-Event Redesign Phase 4a (proposal 0059 §3.2/§3.3): one HookBus
        # PER SESSION, constructed here alongside the HookDispatcher it feeds
        # and never shared across sessions (§3.3 v1 = per-Session scope — no
        # cross-session event observation/correlation). No subscriber ever
        # attaches unless something explicitly calls ``session._hook_bus.
        # subscribe()`` (nothing does yet in Phase 4a — the Composer, Phase
        # 4b, is the first consumer) — until then this is a no-op alongside
        # every dispatch() call (see HookBus.publish's zero-subscriber path).
        # #2886: the same deferred-lambda emit_event sink threaded into
        # HookDispatcher/Composer below — the lambda resolves ``self._chat_events``
        # only at first-drop time, never at construction — so a subscriber-queue
        # drop is fail-visible via a metadata-only bus_subscriber_dropped P6
        # audit-event.
        hook_bus = HookBus(emit_event=lambda et, **d: self._chat_events.emit(et, **d))
        # #1800 slice 5b: the awaited HookDispatcher. Hooks load from the resolved
        # ``hooks:`` block; None/absent → empty registry → every dispatch() is a
        # no-op (run-loop byte-identical to a hooks-free build). Constructed
        # unconditionally so the 4 lifecycle dispatch() sites are uniform.
        hook_dispatcher = HookDispatcher(
            self._build_hook_registry(boot_in_set),
            put_inbox=self._put_inbox,
            stage_next_turn_context=self._stage_next_turn_context,
            # #2072: route a push whose `session` names a different session to THAT session
            # (cross-session); `current_session_id` keeps a self/unnamed push local.
            cross_session_put=self._cross_session_hook_put,
            current_session_id=session_id,
            # #2608 H3: launch a registered pipeline from a hook's
            # pipeline_launch action (async/detached start_pipeline_run) —
            # the closure resolves against THIS session's own PipelineRegistry
            # / AgentRegistry / StateLog / (agent, sid) identity.
            launch_pipeline=self._launch_pipeline_from_hook,
            # #2285: per-session hook applicability gate — skip a hook this session disabled. A
            # callable (not a snapshot) so a toggle applies live to the next dispatch.
            is_hook_disabled=lambda hook: hook.name is not None and hook.name in self._disabled_hooks,
            sandbox_config=self._sandbox_config,
            sandbox_backend=self._sandbox_backend,
            # #2095: route a not-yet-allowlisted shell-hook's consent prompt
            # through this session's RequestBus, but ONLY when a live
            # intervention listener is attached (TUI / web / A2A-override) —
            # i.e. a surface that will actually answer. ``has_active_listener``
            # is checked per-dispatch (listeners attach/detach after this
            # construction: TUI mount, A2A request windows). Plain mcp-serve and
            # headless (no listener) → the dispatcher passes consent_bus=None →
            # the runner's REYN_ACCEPT_HOOKS / fail-closed path, and ``reyn run``
            # on a TTY (no listener) → the runner's stdin prompt — both
            # byte-identical to pre-#2095.
            consent_bus=self.as_request_bus(),
            # Lambda defers the lookup: ``self._interventions`` is constructed
            # later in ``__init__`` (after this builder returns), and the gate is
            # only called at dispatch time.
            consent_gate=lambda: self._interventions.has_active_listener(),
            # #2095 P3: P6-event sink so an auto-run (allowlisted) shell hook
            # surfaces in the events tab instead of being a silent side-effect.
            # Lambda defers ``self._chat_events`` resolution to dispatch time.
            emit_event=lambda et, **d: self._chat_events.emit(et, **d),
            # Phase 4a: broadcast every dispatched HookEvent to this session's
            # own bus, independently of the Sync hooks_for() loop above.
            bus=hook_bus,
        )
        # #2608 H4: the session-owned filesystem watcher (see
        # reyn.runtime.fs_watcher's module docstring for the thread->async
        # bridge design). Constructed unconditionally (cheap — no OS thread
        # spun up here, only inside FsWatcher.start()); ``hook_trigger`` is the
        # SAME deferred-lambda-over-``self._hook_dispatcher`` pattern H1 uses
        # (the dispatcher is unpacked onto ``self`` after this builder returns,
        # but this lambda is never CALLED until FsWatcher.start() is awaited from
        # ``run()``, long after __init__ has finished). ``paths``/
        # ``debounce_seconds`` default to empty/0.2 when no ``fs_watch:``
        # config block was resolved (mirrors ``hooks_config`` defaulting to []).
        fs_watcher = FsWatcher(
            paths=fs_watch_cfg.paths,
            debounce_seconds=fs_watch_cfg.debounce_seconds,
            hook_trigger=lambda point, template_vars: self._hook_dispatcher.dispatch(point, template_vars),
        )
        # Hook-Event Redesign Phase 4b/5 (#2880/#2881): the Composer definitions
        # (``composer_defs``, built above — ahead of the hook registry, #2889) +
        # the composed:*->Sync consumer bridge. Neither is STARTED here (starting
        # means spawning background asyncio tasks, which belongs in ``run()``,
        # this session's async entry point — construction here is synchronous);
        # `run()` calls `self._composer_registry.start()` / `self.
        # _composed_consumer.start()` once. `stop()`ed in `run()`'s shutdown
        # `finally` (mirrors the FsWatcher start/stop shape).
        composer_registry = ComposerRegistry(
            composers=[
                Composer(
                    d, bus=hook_bus,
                    emit_event=lambda et, **kw: self._chat_events.emit(et, **kw),
                )
                for d in composer_defs
            ],
        )
        composed_consumer = ComposedEventConsumer(
            bus=hook_bus, dispatcher=hook_dispatcher,
        )
        # #2073 S1: the config hot-reloader. Reads ONLY the IN-set (.reyn/*.yaml);
        # the OUT-set (reyn.yaml) is restart-only. Applies at the turn_end safe-point
        # (apply_pending below). Per-component reapply seams are registered in S2.
        # Reads ``chat_events`` EAGERLY (this is why the family is built after the
        # Family 1 bundle) and ``registry`` for its project_root.
        hot_reloader = HotReloader(
            project_root=getattr(registry, "_project_root", None) or Path.cwd(),
            events=chat_events,
        )
        return _HookEventBundle(
            hook_bus=hook_bus,
            hook_dispatcher=hook_dispatcher,
            fs_watcher=fs_watcher,
            composer_registry=composer_registry,
            composed_consumer=composed_consumer,
            hot_reloader=hot_reloader,
        )

    # ── #3082 Family 4: cost/budget bundle builder ──

    def _build_budget(
        self,
        budget_tracker: "BudgetTracker | None",
        chat_events: "EventLog",
        agent_name: str,
        router_cap: int,
    ) -> "BudgetGateway":
        """#3082 Family 4: build the cost/budget gateway — ``budget``
        (``BudgetGateway``, the per-session budget adapter). The simplest
        family: a single unconditional component, no intra-family DAG, no
        reordering — this builder is invoked at its ORIGINAL inline call
        site, unmoved.

        Byte-identical extraction of the construction that used to run
        inline in ``__init__`` — same object, same args. Takes
        ``budget_tracker`` / ``chat_events`` / ``agent_name`` / ``router_cap``
        explicitly rather than reaching into ``self`` mid-construction:
        ``budget_tracker`` is the LOCAL ``__init__`` parameter (NOT
        ``self._budget_tracker``, which is a separate tracking assignment
        made earlier in ``__init__`` for callers that receive the tracker by
        value, and is out of scope for this extraction — same shape as
        Family 2's ``state_log``); ``chat_events`` is Family 1's
        ``EventLog``, read EAGERLY here (``events=chat_events``), which is
        why this builder is invoked after the Family 1 bundle is unpacked
        (same eager-sibling-dependency shape as Family 3's ``hot_reloader``);
        ``agent_name`` is the property value already resolvable at the
        original call site; ``router_cap`` is the local ``_router_cap``
        resolved from ``safety.loop.max_router_calls_per_turn`` immediately
        before the original inline construction.

        PR-refactor-session-1 wave 3 PR1: per-session budget adapter.
        Absorbs total_usage / total_cost_usd / router-cap state that
        previously lived as scattered attributes on Session. (#3121 step4:
        returns the ``BudgetGateway`` directly — the prior single-field
        wrapper dataclass was ceremony, see #3082 anti-pattern #2.)"""
        return BudgetGateway(
            budget_tracker=budget_tracker,
            events=chat_events,
            agent_name=agent_name,
            default_router_cap=router_cap,
        )

    def _build_retrieval_bundle(
        self,
        action_retrieval: "ActionRetrievalConfig",
        embedding_config: "EmbeddingConfig | None",
        agent_name: str,
    ) -> "_RetrievalBundle":
        """#3082 Family 5: build the retrieval spine — the embedding block
        (four attrs, one conditional construction guarded by
        ``universal_wrappers_enabled and embedding_class`` with a try/except
        None-fallback) and ``action_usage_tracker`` (a SEPARATE conditional
        guarded by ``universal_wrappers_enabled and hot_list_n > 0``, also
        with a try/except None-fallback). ``action_usage_tracker`` is
        regrouped here per the Family 4 spec's DAG correction — it has no
        dependency on ``BudgetGateway`` and is co-located with
        ``action_embedding_index`` under the shared ``action_retrieval``
        config. ``render_bounds`` (never existed in this codebase) and
        ``subscription_writer`` (WAL-derived task-subscription state, not
        retrieval) are excluded per this same spec's DAG corrections.

        Byte-identical extraction of the construction sequence that used to
        run inline in ``__init__`` at its ORIGINAL position (line ~1152,
        BEFORE Family 1 / ``_build_audit_event_bundle`` runs) — same
        objects, same order, same conditionals, same try/except
        None-fallbacks, same args. ``action_retrieval`` / ``embedding_config``
        / ``agent_name`` are the ``self._action_retrieval`` value / the
        ``embedding_config`` __init__ parameter / the LOCAL ``agent_name``
        __init__ parameter (NOT ``self.agent_name``, mirroring the original
        inline reference at the ``action_usage_tracker`` persist path) — all
        resolvable before this builder's original call site.

        ``chat_events`` is deliberately NOT a builder input, unlike Family
        3's ``hot_reloader`` or Family 4's ``budget``: both closures below
        (``_embedding_event_sink`` / ``_on_hot_list_changed``) resolve
        ``self._chat_events`` at CALL time, not construction time — the
        EventLog is built later in ``__init__`` (Family 1, ~line 1560+).
        Eager-izing that reference (the Family 3/4 pattern) would raise
        ``AttributeError`` here, since this builder runs BEFORE Family 1.
        This builder is an instance method precisely so the closures can
        keep capturing ``self``.

        FP-0034 Phase 2 steps 1 + 5 / FP-0057 #2856 Part A / Issue #192:
        see the four embedding attrs' and ``action_usage_tracker``'s
        original inline comments, reproduced verbatim below."""
        # FP-0034 Phase 2 step 1: build the ActionEmbeddingIndex +
        # EmbeddingProvider once per session when the operator has
        # configured ``action_retrieval.embedding_class``.  Both stay
        # None when embedding is not configured, in which case the
        # ``search_actions`` wrapper is hidden by ``build_tools`` and
        # the handler degrades to an empty-result response.
        action_embedding_index: Any = None
        embedding_provider: Any = None
        embedding_model_class: str | None = None
        # FP-0057 #2856 Part A: the TUI model-download status sink CALLABLE
        # (set below, alongside ``embedding_provider``), threaded onto every
        # router OpContext as ``ctx.embedding_event_sink`` so the `embed` op
        # (which ``ActionEmbeddingIndex`` now routes through instead of
        # calling ``provider.embed()`` directly) can forward it into the
        # FRESH per-call provider it resolves — preserving the download-status
        # rows without the caller holding a long-lived provider instance.
        embedding_event_sink: Any = None
        if (
            action_retrieval.universal_wrappers_enabled
            and action_retrieval.embedding_class
            and embedding_config is not None
        ):
            try:
                from reyn.data.embedding import get_provider as _get_provider
                from reyn.tools.action_index import ActionEmbeddingIndex

                # FP-0043 Component C.3: surface the embedding provider's
                # lazy model-load lifecycle (= downloading / loaded /
                # error) via the session's events bus so the TUI
                # surface can render a sticky status row + a
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
                # exist on Session), which silently raised
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

                embedding_provider = _get_provider(
                    "litellm",
                    embedding_config,
                    event_sink=_embedding_event_sink,
                )
                # FP-0057 #2856 Part A: keep the sink CALLABLE addressable on
                # its own so it can be threaded onto router OpContexts
                # (ctx.embedding_event_sink) independently of
                # ``embedding_provider`` (which stays for non-tool-use /
                # legacy callers until they migrate).
                embedding_event_sink = _embedding_event_sink
                embedding_model_class = action_retrieval.embedding_class
                # FP-0057 Phase 0: unified onto IndexBackend's cache
                # convention (.reyn/cache/index/<source>/); the old
                # .reyn/cache/action_index/ path is no longer read or
                # written (clean-break — cache is regenerable).
                action_embedding_index = ActionEmbeddingIndex(
                    workspace_root=Path.cwd(),
                )
            except Exception:
                # If provider construction fails for any reason (= missing
                # dependency / malformed config), fall through to "no index"
                # so the rest of the session continues without
                # search_actions rather than refusing to start.
                embedding_provider = None
                action_embedding_index = None
                embedding_model_class = None
                embedding_event_sink = None
        # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list freq+recency.
        # Created when universal_wrappers_enabled=True and hot_list_n > 0.
        # Per-agent compacted table at
        # ``.reyn/agents/<agent_name>/action_usage.json``. The table is fed
        # by the chat-compactor sink (see ``CompactionController`` wiring
        # below); uncompacted turns are scanned at hot-list-build time.
        action_usage_tracker: Any = None
        if (
            action_retrieval.universal_wrappers_enabled
            and action_retrieval.hot_list_n > 0
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
                action_usage_tracker = ActionUsageTracker(
                    persist_path=(
                        Path(".reyn") / "agents" / agent_name
                        / "action_usage.json"
                    ),
                    on_ranking_changed=_on_hot_list_changed,
                )
            except Exception:
                action_usage_tracker = None
        return _RetrievalBundle(
            embedding_provider=embedding_provider,
            embedding_event_sink=embedding_event_sink,
            embedding_model_class=embedding_model_class,
            action_embedding_index=action_embedding_index,
            action_usage_tracker=action_usage_tracker,
        )

    def _build_router_waist(self, *, contextual_permission: "object | None" = None) -> "RouterHostAdapter":
        """#3082 Family 6a: build the router-host WAIST — ``router_host``
        (``RouterHostAdapter``, the concrete ``RouterLoopHost`` implementation
        that aggregates ~40 already-constructed Session sub-components).

        Byte-identical extraction of the construction sequence that used to
        run inline in ``__init__`` at its ORIGINAL position (line ~1726,
        no-move — every dep below is already set on ``self`` by this point)
        — same object, same construction order, same ~40 args. Almost every
        dependency is ALREADY an attribute on ``self`` (or a bound method /
        property) by the time this builder runs — so, following the Family
        3/5 instance-method precedent for eager sibling reads, this builder
        reads every OTHER dependency as ``self._X`` / ``self.X`` directly,
        exactly as the inline construction did. ``contextual_permission`` is
        the one EXPLICIT param (#3121 step3 Extract Class): it is the RAW
        constructor-supplied initial value (RouterHostAdapter freezes its own
        copy at construction, same as the pre-extraction code — later toggles
        via ``CapabilityVisibility`` do not retroactively update it, matching
        byte-identical pre-#3121 behavior), threaded explicitly because
        ``CapabilityVisibility`` (which owns the LIVE composed value everywhere
        else) needs ``router_host`` — THIS call's output — so it cannot exist
        yet when this builder runs. Defaults to ``None`` (= no narrowing) so a
        bare ``_build_router_waist()`` (e.g. a builder-contract test) stays
        constructible; the sole production caller, ``__init__``, always passes
        the real constructor value.

        ★ Three args are DEFERRED lambdas, NOT eager values —
        ``live_session_id_fn`` / ``current_task_id_fn`` / ``turn_origin_fn``
        keep resolving ``self._session_id`` / ``self._current_task_id`` /
        ``self._current_turn_origin`` at CALL time, not here: both already
        carry a pre-turn DEFAULT at construction (``_current_task_id`` is
        ``None``, set at :1074; ``_current_turn_origin`` is
        ``"auto_improvement"``, set at :1083 — both BEFORE this builder
        runs), but both are then REASSIGNED per turn inside
        ``run_one_iteration`` (far after ``__init__`` returns) — an
        eager-captured value here would freeze the pre-turn default forever,
        never seeing a real turn's task id / origin; ``live_session_id_fn``
        is deferred because a spawned session's live session id can change
        AFTER this constructor runs (the cached ``self._session_id`` read
        here is stale for that case — see the inline comment above
        ``record_spawned_task`` below). Eager-izing any of the three would
        freeze a per-turn value at
        construction time — the Family 3/5 deferred/eager pitfall repeated
        here for a third and heavier family. ``record_spawned_task`` (a bound
        method) and the two tracker lambdas ``delegation_tracker`` /
        ``agent_replies_tracker`` are likewise kept verbatim, still closing
        over ``self``.

        PR-refactor-session-1 wave 3 PR3: RouterHostAdapter — concrete
        RouterLoopHost implementation extracted from Session. Constructed
        last in __init__ because it receives callbacks that reference self
        (all of which are bound methods, resolved at call time not here)."""
        # #1092 PR-F1 (chat activation): build the chat axis's turn_budget engine
        # off the RESOLVED model (#1172-safe — resolve self.model exactly as the
        # CompactionEngine does; never hand the cosmetic class to the budget).
        # try_build_* returns None (NOT raise) when the model's context is too
        # small to satisfy the by-construction force-close floor (output_reserve +
        # offload_cap < threshold) — a small-context model is a legitimate chat
        # session that simply cannot support force-close, so it degrades to the
        # pre-force-close path (no cap, no handoff) rather than failing __init__.
        # ADDITIVE: the engine's sole consumer is
        # RouterHostAdapter.wrap_up_output_reserve, inert until the F2 handoff
        # calls _force_close_call — chat stays REACTIVE-only (see the property's
        # docstring for the deliberate per-axis choice).
        from reyn.services.turn_budget import try_build_default_turn_budget_engine
        _chat_turn_budget_engine = try_build_default_turn_budget_engine(
            self._resolver.resolve(self.model).model,
            use_chars4=getattr(self._compaction, "use_chars4_estimate", False),
        )

        router_host = RouterHostAdapter(
            # #2175: the safety.on_limit checkpoint + the shared per-run extension dict —
            # so the spawn SEAM (agent_spawn / topology_create) routes spawn-limit exceeds
            # through the same mode-driven framework as inter_agent_messaging's max_agent_hops.
            handle_chat_limit_checkpoint=self._handle_chat_limit_checkpoint,
            safety_extensions=self._safety_extensions,
            # #1092 PR-F1: the chat turn_budget engine (resolved-model, asserted).
            turn_budget_engine=_chat_turn_budget_engine,
            # FP-0050 / #1822 S2: content-threat scan + fence config.
            threat_scan=self._safety.threat_scan,
            contextual_permission=contextual_permission,  # #1827 S3 → control-IR OpContext (raw initial value, see docstring)
            hot_reloader=self._hot_reloader,  # #2073 S3 → per-session reload route (tool ctx)
            # FP-0063 PC: the router-dispatched `embed` TOOL builds its OpContext from
            # THIS host (RouterCallerState.op_context_factory = host.make_router_op_context),
            # so this is the live interactive path's embedding-cost wiring. The gateway is
            # the single recording entry point (session scope on itself; agent/project via
            # the shared tracker it holds). Session's own _make_router_op_context serves
            # file/MCP ops, which no embed op reaches.
            budget_gateway=self._budget,
            state_log=self._state_log,  # #2259 PR-1 → config generation emit from config ops
            # #1953 dynamic-wire: thread the REAL session id + Task backend so
            # router-dispatched task.* ops hit the assignee/requester CAS gate.
            session_id=self._session_id,
            task_backend=self._task_backend,
            task_waker=self._task_waker,  # #2107: thread the TaskWaker into the router op-ctx
            task_subscription_writer=self._task_subscription_writer,  # #2187 backend-master: the Task subscription WAL writer
            hook_dispatcher=self._hook_dispatcher,  # #1800 slice 5c: task_start/end (router path)
            hook_bus=self._hook_bus,  # Hook-Event Redesign Phase 5 part 2: emit_hook_event's publish target
            agent_name=self.agent_name,
            agent_role=self._agent_role,
            output_language=self.output_language,
            allowed_mcp=self._allowed_mcp,
            permission_resolver=self._perm,
            mcp_servers=self._mcp_servers,
            project_context=self._project_context,
            events=self._chat_events,
            resolver=self._resolver,
            memory=self._memory,
            journal=self._journal,
            agent_registry=self._registry,
            # IS-5: the session's real (initially empty) PipelineRegistry —
            # mirrors agent_registry above. Exposed via
            # RouterHostAdapter.get_pipeline_registry() and read onto
            # RouterCallerState.pipeline_registry by
            # RouterLoop._build_router_caller_state.
            pipeline_registry=self._pipeline_registry,
            # FP-0054 PR-C: the session's PresentationRegistry — mirrors
            # pipeline_registry above; the adapter threads its CURRENT snapshot into
            # each router OpContext, and _reapply_presentations swaps both copies.
            presentation_registry=self._presentation_registry,
            # #2103 S1bc-exec: record a spawned session's sid→task (the trusted result
            # header source) + read this session's LIVE sid (the cached session_id above
            # is stale for spawned sessions, stamped post-construction) for the non-main
            # spawn guard.
            record_spawned_task=self.record_spawned_task,
            live_session_id_fn=lambda: self._session_id,
            # #1953 §16: the per-turn execution context (set in run_one_iteration),
            # read at op-ctx-build time so a router task.create derives ownership.
            current_task_id_fn=lambda: self._current_task_id,
            # proposal 0060 Phase 1 (A7): mirrors current_task_id_fn exactly — a live
            # callback (not a fixed init value) because turn_origin varies per turn.
            turn_origin_fn=lambda: self._current_turn_origin,
            agent_workspace_dir=self.workspace_dir,
            file_read=self._file_read,
            file_write=self._file_write,
            file_delete=self._file_delete,
            file_list_directory=self._file_list_directory,
            file_regenerate_index=self._file_regenerate_index,
            mcp_list_servers=self._mcp_list_servers,
            mcp_list_tools=self._mcp_list_tools,
            mcp_call_tool=self._mcp_call_tool,
            # #2597 slice ②a: resources consumption (list/read/templates).
            mcp_list_resources=self._mcp_list_resources,
            mcp_list_resource_templates=self._mcp_list_resource_templates,
            mcp_read_resource=self._mcp_read_resource,
            # #2597 slice ②b: resource subscriptions.
            mcp_subscribe_resource=self._mcp_subscribe_resource,
            mcp_unsubscribe_resource=self._mcp_unsubscribe_resource,
            # #2597 slice ②c: prompts consumption (list/get).
            mcp_list_prompts=self._mcp_list_prompts,
            mcp_get_prompt=self._mcp_get_prompt,
            send_to_agent=self._send_to_agent,
            put_outbox=self._put_outbox,
            append_history=self._append_history,
            delegation_tracker=lambda: self._router_loop_delegations,
            agent_replies_tracker=lambda: self._router_loop_agent_replies,
            universal_wrappers_enabled=self._action_retrieval.universal_wrappers_enabled,
            action_embedding_index=self._action_embedding_index,
            embedding_provider=self._embedding_provider,
            embedding_model_class=self._embedding_model_class,
            embedding_event_sink=self._embedding_event_sink,  # FP-0057 #2856 Part A: forwarded to make_router_op_context
            # FP-0034 Phase 2 step 5: ActionUsageTracker for hot list.
            action_usage_tracker=self._action_usage_tracker,
            uncompacted_tool_call_records_fn=(
                self._uncompacted_tool_call_records
            ),
            action_retrieval_config=self._action_retrieval,
            available_skills=self._available_skills,  # #2548 PR-A
            # FP-0034 Phase 2: sandbox backend for exec D14 visibility gate.
            # #1417: gate on the INJECTED backend's real capability, not the
            # reyn.yaml config STRING. The exec capability comes from the
            # injected ``self._sandbox_backend`` instance (the SAME object used
            # for actual exec at line 1847 / sandboxed_exec.py: ``ctx.sandbox_
            # backend or get_default_backend(...)``); both injected types expose
            # ``.name`` (DockerEnvironmentBackend.name="docker" / SandboxBackend
            # .name). Without this, ``sandbox.backend=noop`` config + an injected
            # exec backend (``--env-backend=docker``) would HIDE exec from
            # discovery even though sandboxed_exec is functionally available
            # (the construction-forwarding-gap: config string ≠ live instance).
            # No injected instance → fall back to the config string (auto /
            # host-default behaviour unchanged).
            sandbox_backend=_exec_gate_backend_name(
                self._sandbox_backend, self._sandbox_config
            ),
            # #187: the FS env-backend instance + container repo root + host-side
            # state dir for the LIVE router OpContext Workspace (the registry
            # file-dispatch factory). Same source as the chat OpContext (#1410).
            environment_backend=self._environment_backend,
            workspace_base_dir=self._workspace_base_dir,
            workspace_state_dir=self._workspace_state_dir,
            # #187 exec-seam (10th defect): the exec backend INSTANCE so the LIVE
            # router's sandboxed_exec runs in the container repo, not the host
            # seatbelt fallback. Same instance the legacy _make_router_op_context
            # passes (4824); chat.py injects the SAME docker backend at both FS +
            # exec seams (#1289 single-shared-sandbox). Distinct from the D14
            # STRING above.
            sandbox_backend_instance=self._sandbox_backend,
            # #1339 / sandbox-model completion: thread the operator sandbox
            # policy so make_router_op_context resolves a concrete agent-level
            # policy onto the router OpContext (closes the chat-factory wiring gap).
            sandbox_policy=(
                self._sandbox_config.policy if self._sandbox_config is not None
                else None
            ),
            # Issue #364 multi-modal cluster: media-size gate config.
            multimodal_config=self._multimodal_config,
            # FP-0055 / #2679: operator render_template output bounds → the router
            # OpContext (the render_template op reads ctx.render_template_bounds).
            render_template_bounds=self._render_template_bounds,
            # #1652: reasoning config (display/continuity/recent_turns gates) +
            # the bounded prior-reasoning section renderer (reads this session's
            # history). The host exposes reasoning_display_enabled() /
            # reasoning_continuity_enabled() / reasoning_continuity_section() to
            # the router loop for emit-gating, persist-gating, and SP replay.
            reasoning_config=self._reasoning,
            reasoning_continuity_section_fn=self.reasoning_continuity_section,
            # Issue #383 PR-C: shared MediaStore for image + tool-result storage.
            media_store=self._media_store,
            # #1128 size axis: per-turn tool-result cap/offload (dead-end #1).
            # Late-bound method — the engine budgets it reads are computed by
            # the time a tool result flows through router_loop at runtime.
            cap_tool_result=self._cap_tool_result,
            # #272 media axis: per-turn media budget (= cap − tool text tokens)
            # so router_loop bounds the media follow-up (overflow media → ref).
            media_followup_budget=self._media_followup_budget,
            # tool-result-schema-redesign §5: gates build_offload_body's structured
            # inline-size gate (STRUCTURED_INLINE_MAX_CHARS). Static per-session config,
            # not a callable (unlike the two budgets above, which read live engine state).
            offload_enabled=self._offload_config.enabled,
            # #272/#1128 compact op: voluntary-compaction callback so the LLM-
            # emittable `compact` control_ir op can compact chat history.
            compact_now=self._compact_now_for_op,
            # #272/#1128 context-size signal: live exact-token budget so the
            # router SP can show the LLM the free window (header).
            context_window_status=self.context_window_status,
            # B25-S5-1: thread eager-build flag so RouterLoop awaits build
            # before computing _search_visible on the first turn.
            eager_embedding_build=self._eager_embedding_build,
            # FP-0022 fix (#53): give the router OpContext a real
            # InterventionBus so web_fetch / mcp install / mcp drop
            # handlers can run their interactive (Layer 4) approval
            # flow. The bus is built per make_router_op_context() call
            # — short-lived, scoped to the chat_router turn, identical
            # to what session._mcp_call_tool wires manually today.
            # #2708 P3.2a: when this session is an ATTACHED pipeline driver (it carries a
            # SpawnBridgeInterventionListener), the router intervention bus dispatches on the
            # PARENT session's live-operator listener instead of the driver's own
            # listener-less registry — so a pipeline-step ``ask_user`` reaches the operator
            # blocked on the parent by construction (#2721), instead of silently auto-
            # refusing. Non-driver / detached / ephemeral sessions (bridge is None) keep the
            # self-bound bus, byte-identical. Mirror of the presentation_renderer_factory
            # spawn-bridge below.
            # #3049: single-sourced with the MCP op callers via
            # ``_make_router_intervention_bus`` (bridge-aware — driver → parent, else
            # self-bound) so router-op interventions resolve to the SAME surface
            # regardless of which seam builds the OpContext.
            intervention_bus_factory=self._make_router_intervention_bus,
            # FP-0054 PR-B / #2708 P1: give the router OpContext a real PresentationRenderer
            # so a `present` op reaches the surface's sink instead of PR-A's null surface.
            # Built per make_router_op_context() call, mirroring the intervention_bus_factory
            # above. The sink is obtained from the surface's declared PresentationConsumer
            # (orphan-impossible: OutboxPresentationRenderer is constructible ONLY inside
            # OutboxPresentationConsumer.sink) — bound to THIS Session via sink(self).
            presentation_renderer_factory=lambda: self._presentation_consumer.sink(self),
            # FP-0037 S2: yaml mtime watch needs the project root to resolve
            # the 3 yaml scope tier paths. None falls back to user-global only.
            project_root=getattr(self._registry, "_project_root", None),
            # #1468: cooperative turn-cancel forwarding. The adapter's
            # _is_turn_cancel_requested() forwards to RouterLoopDriver; run_loop
            # checks it via getattr at each iteration boundary.
            turn_cancel_fn=self._is_turn_cancel_requested,
        )
        return router_host

    def _build_history_compaction_bundle(
        self, merge_action_usage: "Callable[[list[ChatMessage]], None]",
    ) -> "_HistoryCompactionBundle":
        """#3082 Family 6b: build the history-compaction chain —
        ``history_buffer`` / ``compaction_controller`` (incl. the
        None-then-patch forward-reference) / ``budget_advisor``. Byte-identical
        extraction of the construction sequence that used to run inline in
        ``__init__`` at its ORIGINAL position (line ~1797, no-move — every
        cross-family dep, including Family 6a's ``router_host``, is already
        set on ``self`` by this point).

        ★ The None-then-patch circular-dependency break is reproduced with
        LOCAL variables end to end: ``history_buffer`` is built with
        ``compaction_controller=None`` first; ``compaction_controller`` (and
        its inner ``CompactionEngine``) is then built reading the LOCAL
        ``history_buffer.build_system_prompt`` (NOT ``self._history_buffer``
        — that attribute does not exist yet, since ``__init__`` only assigns
        it AFTER this builder returns; reading ``self._history_buffer`` here
        would raise ``AttributeError``); then the LOCAL patch
        ``history_buffer._compaction_controller = compaction_controller``
        closes the cycle; ``budget_advisor`` is built last, also reading the
        LOCAL ``compaction_controller`` / ``history_buffer.build_history``.
        See :class:`_HistoryCompactionBundle`'s docstring for the full
        per-arg local-vs-deferred-self-vs-cross-family-self classification.

        ``merge_action_usage`` is the ``_merge_action_usage_from_candidates``
        closure defined in ``__init__`` immediately before this builder is
        called (unmoved, at its original position) — it is not one of this
        family's three components, so it is threaded through as an explicit
        param (mirroring Family 2/4's LOCAL-param pattern) rather than
        redefined inside the builder.

        ★ ``budget_advisor`` UP-move: this builder constructs it right after
        the forward-patch, BEFORE ``InterAgentMessaging`` (Family 8), which
        used to sit between them and is now constructed AFTER this builder
        returns (unmoved itself). Safe: every ``budget_advisor`` dep resolves
        here (LOCAL ``compaction_controller`` / ``history_buffer``,
        cross-family ``self._media_store`` / ``self._offload_config``), and
        ``InterAgentMessaging`` does not read any of this family's three
        components."""
        from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
        history_buffer = RouterHistoryBuffer(
            history_fn=self._active_branch_history,
            compaction=self._compaction,
            compaction_controller=None,  # patched below after CompactionController
            # #1752: live resolved model — a /model override changes the context
            # window, so resolve the active class → litellm string each call
            # instead of caching the construction-time model.
            model_fn=lambda: self._resolver.resolve(self.model).model,
            events=self._chat_events,
            media_store=self._media_store,
            router_host=self._router_host,
            action_retrieval=self._action_retrieval,
            non_interactive=self._non_interactive,
            reasoning=self._reasoning,  # #1652/② native reasoning re-attach + bound
        )

        compaction_controller = CompactionController(
            event_log=self._chat_events,
            config=self._compaction,
            # FP-0050/#1822 S3 (#1820): secret-redact turn text before summary.
            threat_scan=self._safety.threat_scan,
            history_access=lambda: self.history,
            latest_summary=self._latest_summary,
            compaction_engine=CompactionEngine(
                # #1172: pass a model CLASS (like "standard") plus the resolver —
                # CompactionEngine resolves to a litellm string by construction.
                # Without resolution the engine would hand "standard" straight to
                # litellm (BadRequestError) and every compaction trigger would
                # fail (dead-end-critical).
                # #1679: honor a documented model_class_by_purpose.compaction
                # override when set; otherwise keep self.model (byte-identical to
                # the former hardcode, incl. a per-run model override).
                model=self._resolver.purpose_class_or("compaction", self.model),
                events=self._chat_events,
                system_prompt_provider=history_buffer.build_system_prompt,
                resolver=self._resolver,
                # #1190 stage (ii): record chat compaction LLM spend (purpose=compaction).
                recorder=self._budget_tracker,
                # #1190 stage (iii) Part 4: attribute chat compaction to this session's agent.
                recorder_agent=self.agent_name,
            ),
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
            merge_action_usage=merge_action_usage,
        )
        # Wire compaction_controller now that it exists (the patch that closes
        # the circular dependency — LOCAL history_buffer, not self._history_buffer).
        history_buffer._compaction_controller = compaction_controller

        # session.py refactor PR-1: ContextBudgetAdvisor owns the five
        # per-turn budget-arithmetic methods. Session keeps forwarding
        # properties so RouterHostAdapter callbacks are unchanged.
        from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
        budget_advisor = ContextBudgetAdvisor(
            compaction=self._compaction,
            compaction_controller=compaction_controller,
            media_store=self._media_store,
            # #1752: live resolved model (see RouterHistoryBuffer above).
            model_fn=lambda: self._resolver.resolve(self.model).model,
            events=self._chat_events,
            history_fn=history_buffer.build_history,
            offload_config=self._offload_config,
        )

        return _HistoryCompactionBundle(
            history_buffer=history_buffer,
            compaction_controller=compaction_controller,
            budget_advisor=budget_advisor,
        )

    def _build_intervention_bundle(self) -> "_InterventionBundle":
        """#3082 Family 7: build ``chains`` / ``interventions`` /
        ``intervention_handler`` / ``intervention_coordinator`` /
        ``chain_timeout_glue``. Byte-identical extraction of the
        construction sequence that used to run inline in ``__init__`` —
        four of the five components stay at their ORIGINAL position (line
        ~1784, ``chains``'s original spot); only ``chain_timeout_glue``
        moves UP from its original position (~160 lines below, AFTER
        Family 8's ``InterAgentMessaging``) into this same contiguous
        builder call.

        ★ NO forward-patch / circular dependency: unlike Family 6b's
        history_buffer ↔ compaction_controller cycle, this family's
        chains ↔ chain_timeout_glue relationship is ASYMMETRIC —
        ``chain_timeout_glue`` reads ``chains`` EAGERLY at construction
        time, while ``chains`` only reaches ``chain_timeout_glue``
        INDIRECTLY through the bound method ``_on_chain_timeout_fire``
        (wired into Family 8's ``InterAgentMessaging``, unmoved), which
        forwards to ``self._chain_timeout_glue.on_chain_timeout_fire``
        only when CALLED — long after both exist. So construction is
        strictly LINEAR: chains → interventions → intervention_handler →
        intervention_coordinator → chain_timeout_glue. No None-then-patch
        needed.

        ★★ Family-8 cross-dep preserved: Family 8's ``InterAgentMessaging``
        (unmoved, constructed directly in ``__init__`` right after this
        builder returns) reads ``chain_manager=self._chains`` — this
        builder's call site sits at ``chains``'s ORIGINAL position, so
        ``self._chains`` is assigned by ``__init__`` well before
        ``InterAgentMessaging`` is constructed. The F8→F7 cross-family
        dependency resolves exactly as before.

        ★ intra-Family-7 local-vs-self: ``self._interventions`` /
        ``self._intervention_handler`` / ``self._chains`` are all assigned
        by ``__init__`` only AFTER this builder RETURNS — reading them as
        ``self._X`` from INSIDE the builder would raise ``AttributeError``.
        Every eager reference among this family's OWN five components is
        threaded through LOCAL variables (``chains`` / ``interventions`` /
        ``intervention_handler``), never ``self._X``. Deferred bound
        methods that resolve at CALL time (by which point the attributes
        ARE set) are kept as ``self.*`` — ``self._announce_intervention``.
        Cross-family / config dependencies (already set on ``self`` before
        this builder runs) are kept as ``self._X``. See
        :class:`_InterventionBundle`'s docstring for the full per-arg
        classification."""
        chains = ChainManager(
            journal=self._journal,
            events=self._chat_events,
            chain_timeout_seconds=self._chain_timeout_seconds,
            max_hop_depth=self._max_hop_depth,
        )
        interventions = InterventionRegistry(
            on_announce=self._announce_intervention,
            # issue #254 Phase 1: fail-closed when no listener is wired
            # (= no TUI mounted, no A2A override, no test fixture
            # registered). Without this, ``handle_limit_exceeded`` with
            # ``ask_timeout_seconds=0`` would await an unresolvable future
            # in test / headless contexts.
            enforce_listener_presence=True,
        )

        # FP-0019 Wave 2 part 1: InterventionHandler — ask_user dispatch service.
        # Extracted from Session.  Session keeps thin wrappers on
        # _dispatch_intervention / _maybe_answer_oldest_intervention /
        # _announce_intervention / _deliver_answer_to so the existing test
        # surface (and ChatInterventionBus) remain stable.
        intervention_handler = InterventionHandler(
            intervention_registry=interventions,
            journal=self._journal,
            event_log=self._chat_events,
            put_outbox=self._put_outbox,
            append_history=self._append_history_for_handler,
            # FP-0050 / #1862 (EP7): fences external peer-answer copies
            # bound for conversation context (history sink only).
            threat_scan=self._safety.threat_scan,
        )
        # Owns the chain-override state + the per-intervention dispatch
        # orchestration.
        intervention_coordinator = InterventionCoordinator(
            registry=interventions,
            handler=intervention_handler,
            events=self._chat_events,
        )

        # session.py refactor PR-4 (FP-0019 series final): ChainTimeoutGlue owns
        # chain timeout lifecycle.
        from reyn.runtime.services.chain_timeout_glue import ChainTimeoutGlue
        chain_timeout_glue = ChainTimeoutGlue(
            append_history_fn=self._append_history,
            events=self._chat_events,
            reset_turn_counter_fn=self._reset_router_turn_counter,
            run_router_loop_fn=self._run_router_loop,
            emit_cap_exhausted_fn=self._emit_router_cap_exhausted_user,
            put_outbox_fn=self._put_outbox,
            inbox=self.inbox,
            journal=self._journal,
            on_limit=self._on_limit,
            chains=chains,
            limit_checkpoint_fn=self._handle_chat_limit_checkpoint,
            chain_timeout_seconds=self._chain_timeout_seconds,
            send_agent_response_fn=self._send_agent_response,
            put_inbox_fn=self._put_inbox,
        )

        return _InterventionBundle(
            chains=chains,
            interventions=interventions,
            intervention_handler=intervention_handler,
            intervention_coordinator=intervention_coordinator,
            chain_timeout_glue=chain_timeout_glue,
        )

    def _build_inter_agent_messaging(self) -> "InterAgentMessaging":
        """#3082 Family 8a: build ``inter_agent_messaging``. Byte-identical
        extraction of the construction that used to run inline in
        ``__init__`` — same object, same 22 keyword args, same construction
        order, same (unmoved) position (right after Family 7's
        ``_build_intervention_bundle`` returns).

        This is a single independent leaf component (unlike Family 6b/7's
        multi-component families) — every arg is either an eager
        ``self._X`` (cross-family / config, already set on ``self`` by this
        point: Family 7's ``self._chains``, Family 1's
        ``self._chat_events``, plus early params/properties) or a deferred
        bound method / ``lambda`` closing over ``self`` (kept verbatim,
        NEVER eager-ized — ``run_router_loop`` /
        ``get_router_loop_delegations`` / ``set_router_loop_delegations`` /
        ``get_router_loop_agent_replies`` / ``set_router_loop_agent_replies``
        / ``session_id_fn`` all resolve per-turn / post-construction state at
        CALL time, not at builder-call time). No intra-family local-vs-self
        split applies — there is nothing else in this family to be local
        against. Returns the ``InterAgentMessaging`` instance directly
        (#3121 step4 removed the prior single-field wrapper dataclass)."""
        inter_agent_messaging = InterAgentMessaging(
            event_log=self._chat_events,
            chain_manager=self._chains,
            agent_name=self.agent_name,
            max_hop_depth=self._max_hop_depth,
            safety_extensions=self._safety_extensions,
            output_language=self.output_language,
            # FP-0050/#1822 S4b (EP5): fence untrusted inbound peer text.
            threat_scan=self._safety.threat_scan,
            append_history=self._append_history_for_inter_agent_messaging,
            put_outbox=self._put_outbox,
            handle_chat_limit_checkpoint=self._handle_chat_limit_checkpoint,
            run_router_loop=lambda text, cid: self._run_router_loop(text, cid),
            reset_router_turn_counter=self._reset_router_turn_counter,
            send_request_callback=self._a2a_send_request,
            send_response_callback=self._a2a_send_response,
            on_chain_timeout_fire=self._on_chain_timeout_fire,
            emit_router_cap_exhausted_fn=self._emit_router_cap_exhausted_user,
            get_router_loop_delegations=lambda: self._router_loop_delegations,
            set_router_loop_delegations=lambda v: setattr(self, "_router_loop_delegations", v),
            get_router_loop_agent_replies=lambda: self._router_loop_agent_replies,
            set_router_loop_agent_replies=lambda v: setattr(self, "_router_loop_agent_replies", v),
            # #2103 S1bc-exec: read this session's LIVE sid (spawned sessions are stamped
            # post-construction, so a cached value would be stale) for the responder_sid
            # tag; + the trusted spawned-task lookup for rendering a returning result.
            session_id_fn=lambda: self._session_id,
            lookup_spawned_task=self.lookup_and_evict_spawned_task,
        )
        return inter_agent_messaging

    def _build_memory(self) -> "MemoryService":
        """#3082 Family 8b: build ``memory``. Byte-identical extraction of the
        construction that used to run inline in ``__init__`` — same object,
        same keyword args, same (unmoved) position.

        This is a single independent leaf component (like Family 8a's
        ``inter_agent_messaging``) — every arg is an eager ``self._X``
        (Family 1's ``self._chat_events``, already set on ``self`` by this
        point) or a bound method / property already available at
        construction time (``self._file_write`` / ``self._file_read`` /
        ``self._file_delete`` / ``self._file_regenerate_index`` /
        ``self.workspace_dir``). No deferred lambda, no intra-family
        local-vs-self split.

        ★ PRE-WAIST placement: this builder's call site (in ``__init__``)
        MUST stay before ``_build_router_waist`` runs (Family 6a), which
        reads ``self._memory`` eagerly when constructing
        ``RouterHostAdapter``. Moving this call after the waist builder
        call would leave ``self._memory`` unassigned when the waist builder
        reads it, raising ``AttributeError``. Returns the ``MemoryService``
        instance directly (#3121 step4 removed the prior single-field
        wrapper dataclass)."""
        memory = MemoryService(
            agent_workspace_dir=self.workspace_dir,
            events=self._chat_events,
            file_write=self._file_write,
            file_read=self._file_read,
            file_delete=self._file_delete,
            file_regenerate_index=self._file_regenerate_index,
        )
        return memory

    def _build_mcp_connection_service(self) -> "MCPConnectionService":
        """#3082 Family 8c (mcp_connection_service, the FINAL family): build
        the session-owned held-open MCP connection service. Byte-identical
        extraction of the construction that used to run inline in
        ``__init__`` — same object, same 6 keyword args, same (unmoved)
        position (its original inline position, ~:1511, BEFORE Family 1 /
        ``_build_audit_event_bundle``, Family 3 / ``_build_hook_event_bundle``,
        Family 6a / ``_build_router_waist``, and Family 7 /
        ``_build_intervention_bundle`` all run).

        ★★ This family's crux (the sharpest deferred-resolution case in all
        of F8 — 4 refs, vs Family 5's 2): FOUR of the six keyword args below
        are ``lambda`` closures that resolve ``self._chat_events`` /
        ``self._router_host`` / ``self._hook_dispatcher`` /
        ``self._interventions`` at CALL time — none of those four
        attributes exist yet at this builder's call site. Eager-izing ANY
        of them (the Family 3/4 pattern, wrong HERE) would raise
        ``AttributeError`` the moment this builder runs, since it runs
        before all four are constructed. This builder is an instance
        method precisely so the four lambdas keep capturing ``self`` —
        kept verbatim, never eager-ized. Only ``elicitation_bus``/
        ``agent_name`` are eager (both already resolvable at this position
        — see their inline comments below, reproduced verbatim from the
        original construction). Returns the ``MCPConnectionService``
        instance directly (#3121 step4 removed the prior single-field
        wrapper dataclass)."""
        # #2597 S2a: the session-owned held-open MCP connection service (Option C —
        # one persistent MCPClient per server, reused across chat turns/tasks for
        # this session's whole lifetime). Constructed unconditionally (cheap — an
        # empty dict until first ``get()``); ONLY the non-ephemeral MCP call sites
        # (_mcp_call_tool / _mcp_list_tools) route through it — an ephemeral session
        # (``self._ephemeral`` set post-construction by the registry) keeps using the
        # per-call MCPClientPool so a sub-second-lived session never holds a
        # connection open. Closed at session teardown via aclose_mcp_connections
        # (registry.remove_session / archive_agent's main-session path).
        # #2597 S2b: emit_sink / tools_cache_invalidate are lambdas that defer
        # resolution of ``self._chat_events`` / ``self._router_host`` to CALL time
        # (mirrors the ``emit_event=lambda et, **d: self._chat_events.emit(et, **d)``
        # pattern used a few lines below) — both attributes are assigned LATER in this
        # __init__ (``_chat_events`` at construction of the EventLog; ``_router_host``
        # when the RouterHostAdapter is built), but neither lambda is ever CALLED until
        # a held MCP connection actually receives a server-pushed notification, long
        # after __init__ has finished.
        # #2608 H1: ``hook_trigger`` is the SAME deferred-lambda pattern, over
        # ``self._hook_dispatcher`` — constructed further below in this __init__ (the
        # HookDispatcher itself needs ``self._put_inbox`` / ``self._stage_next_turn_context``
        # / etc, already bound methods, so it's built after this point) — but, like
        # ``emit_sink``/``tools_cache_invalidate`` above, never CALLED until a held MCP
        # connection's receive loop enqueues an external event, long after __init__ has
        # finished. ``self.agent_name`` IS already resolvable here (``self._agent`` is
        # set earlier in this __init__), so it's passed eagerly (not deferred).
        # #2597 slice ③ (elicitation): SAME consent_bus/consent_gate split
        # #2095's shell-hook consent already uses (see the HookDispatcher
        # construction above) — a server->client elicitation is routed
        # through THIS session's RequestBus ONLY when a live intervention
        # listener is attached; headless (no listener) auto-declines inside
        # the handler (reyn.mcp.elicitation), never here. ``as_request_bus()``
        # is safe to call eagerly here (unlike the deferred lambdas above) —
        # it just wraps ``self`` in an adapter, no attribute it reads is
        # constructed later in this __init__.
        from reyn.mcp.connection_service import MCPConnectionService
        mcp_connection_service = MCPConnectionService(
            emit_sink=lambda et, **d: self._chat_events.emit(et, **d),
            tools_cache_invalidate=lambda server: self._router_host.invalidate_mcp_tools_cache(server),
            hook_trigger=lambda point, template_vars: self._hook_dispatcher.dispatch(point, template_vars),
            elicitation_bus=self.as_request_bus(),
            elicitation_gate=lambda: self._interventions.has_active_listener(),
            agent_name=self.agent_name,
        )
        return mcp_connection_service

    # ── #2073 S2: config hot-reload reapply seams (registered on the HotReloader) ──

    def _register_hot_reload_seams(self) -> None:
        """Register the per-component reapply seams + validate-before-apply on the
        HotReloader (#2073 S2). Called once at construction after router_host and
        other sub-components exist. Each seam reapplies one IN-set component live at
        the turn boundary; the Session orchestrates them (it owns the sub-components).
        Hooks = S2b (global .reyn/hooks.yaml); per-agent-hooks add-on = a separate
        decision."""
        hr = self._hot_reloader
        # validate-before-apply is the HotReloader's built-in structural check
        # (hot_reload.validate_in_set) — no per-Session override needed.
        hr.register_seam("cron", self._reapply_cron)
        hr.register_seam("mcp", self._reapply_mcp)
        hr.register_seam("per_agent_capability", self._reapply_per_agent_capability)
        hr.register_seam("new_agent", self._reapply_new_agent)
        hr.register_seam("hooks", self._reapply_hooks)  # #2073 S2b (global hooks)
        hr.register_seam("skills", self._reapply_skills)  # #2548 PR-B: skills hot-reload
        hr.register_seam("pipelines", self._reapply_pipelines)  # #2581: pipeline hot-reload
        hr.register_seam("presentations", self._reapply_presentations)  # FP-0054 PR-C
        # #3097: the security-core envelope re-resolve (see the wrapper's own docstring
        # for why it needs its own seam — its data source, resolved_profile_for, is
        # independent of every other registered seam's IN-set/cascade re-read).
        hr.register_seam("visibility_override", self._reapply_visibility_override_seam)

    def _build_hook_registry(self, in_set: "dict | None" = None) -> "object":
        """Build the LAYERED hook registry — the three-layer COMBINE (#2073 S2b + the
        per-agent-hooks add-on), ADDITIVE in order startup → runtime → per-agent:

        - **startup** — the reyn.yaml hooks (``self._startup_hooks_raw``, captured once
          at boot, the restart-only OUT-set, never re-read on a reload);
        - **runtime** — the global ``.reyn/hooks.yaml`` (from the IN-set);
        - **per-agent** — ``.reyn/agents/<name>/hooks.yaml`` (read directly here, same
          IN-set grain but scoped per agent).

        Rebuilding from scratch each call means a removed hook (runtime or per-agent)
        simply isn't in the new registry — removal handled by construction.

        Threads ``self._composed_schemas`` (#2889 — computed once in
        ``__init__`` from ``self._composer_defs``, BEFORE this is first
        called; composers are startup-only, so the map never changes) into
        every ``load_hooks`` call below, so a ``composed:*`` hook's
        ``matcher`` is schema-validated exactly like a builtin point's,
        closing the Phase-3 open-set gap ``composed:*`` was left in.

        **Per-LAYER boot resilience (the add-on refinement):** ``load_hooks`` raises
        ``HookConfigError`` on a malformed layer, and BOTH boot AND the reload path call
        this — a malformed persisted ``.reyn/hooks.yaml`` or per-agent file must NOT
        crash boot, NOR may one bad UNTRUSTED layer drop a good sibling. So the trusted
        startup layer (reyn.yaml — the operator's) must load (a failure propagates =
        fail loud), then each untrusted layer is try-added INDEPENDENTLY: a bad runtime
        keeps startup ∪ per-agent; a bad per-agent keeps startup ∪ runtime; each bad
        layer is dropped + warned. (On the reload path validate-before-apply also rejects
        a bad runtime layer up front; this is the boot + defence-in-depth guard.)"""
        from reyn.hooks.loader import HookConfigError, load_hooks
        runtime = (in_set or {}).get("hooks") or []
        runtime_list = list(runtime) if isinstance(runtime, list) else []
        per_agent_list = self._read_per_agent_hooks()
        per_session_list = self._read_per_session_hooks()  # #2285: the 4th, most-specific layer
        combined = list(self._startup_hooks_raw)
        composed_schemas = getattr(self, "_composed_schemas", None)
        registry = load_hooks(combined, composed_schemas)  # trusted startup must load — else fail loud
        for label, layer in (
            ("runtime", runtime_list),
            ("per-agent", per_agent_list),
            ("per-session", per_session_list),  # #2285: session-defined hooks (try-add like untrusted)
        ):
            if not layer:
                continue
            try:
                registry = load_hooks(combined + layer, composed_schemas)  # validate the cumulative add
                combined = combined + layer
            except HookConfigError as exc:
                logger.warning(
                    "config hot-reload: malformed %s hooks layer — skipped, keeping "
                    "the valid hook layers: %s", label, exc,
                )
        return registry

    def _read_per_agent_hooks(self) -> list:
        """Read the per-agent runtime hooks layer for the COMBINE (#2073 per-agent
        add-on) — ``.reyn/agents/<name>/hooks.yaml``, read directly (like the per-agent
        profile.yaml, not via the top-level IN-set loader). ``[]`` when absent."""
        from reyn.config.loader import load_per_agent_hooks
        return load_per_agent_hooks(self._hot_reload_project_root(), self.agent_name)

    def _read_per_session_hooks(self) -> list:
        """#2285: read the per-SESSION hooks layer — ``<per-session state dir>/hooks.yaml`` (the 4th,
        most-specific COMBINE layer). The per-session dir is the parent of this session's snapshot
        path (set per (name, sid) by spawn_session). A hook defined here is visible ONLY to this
        session. ``[]`` when absent (or the file is malformed — the loader's per-layer resilience
        also guards)."""
        import yaml
        path = Path(self._snapshot_path).parent / "hooks.yaml"
        if not path.is_file():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — malformed → treat as absent
            return []
        hooks = data.get("hooks") if isinstance(data, dict) else None
        return list(hooks) if isinstance(hooks, list) else []

    def _build_composer_defs(self, in_set: "dict | None" = None) -> list:
        """Build the LAYERED ``ComposerDef`` list (Hook-Event Redesign Phase 4b/5,
        proposal 0059 §5/§9, #2880/#2881) — the SAME 4-layer additive COMBINE
        shape as :meth:`_build_hook_registry` (startup -> runtime -> per-agent
        -> per-session), applied to ``composers:`` instead of ``hooks:``.

        Unlike hooks, composers are v1 **startup-only** — this is called ONCE
        from ``__init__`` (seeded with the boot IN-set) and the result is
        started once in ``run()``; there is no reapply/hot-reload seam yet (a
        live Composer's ``PendingStore`` correlating in-flight state makes
        restarting mid-session a materially different, not-yet-designed
        concern from a hook-registry swap, which has no analogous in-flight
        state to lose).

        Per-layer resilience mirrors ``_build_hook_registry`` exactly: the
        trusted startup (reyn.yaml) layer must parse+cycle-check cleanly or
        this fails loud (an operator config error); each of the 3 untrusted
        layers (runtime/per-agent/per-session) is try-added independently — a
        malformed layer is warned + dropped, keeping its valid siblings."""
        from reyn.hooks.composer import ComposerConfigError, load_composers
        runtime = (in_set or {}).get("composers") or []
        runtime_list = list(runtime) if isinstance(runtime, list) else []
        per_agent_list = self._read_per_agent_composers()
        per_session_list = self._read_per_session_composers()
        combined = list(self._startup_composers_raw)
        definitions = load_composers(combined)  # trusted startup must load — else fail loud
        for label, layer in (
            ("runtime", runtime_list),
            ("per-agent", per_agent_list),
            ("per-session", per_session_list),
        ):
            if not layer:
                continue
            try:
                definitions = load_composers(combined + layer)  # validate the cumulative add
                combined = combined + layer
            except ComposerConfigError as exc:
                logger.warning(
                    "config hot-reload: malformed %s composers layer — skipped, keeping "
                    "the valid composer layers: %s", label, exc,
                )
        return definitions

    def _read_per_agent_composers(self) -> list:
        """Read the per-agent COMPOSER layer (Hook-Event Redesign Phase 4b/5,
        #2880/#2881) — the ``composers:`` key of the SAME
        ``.reyn/agents/<name>/hooks.yaml`` file :meth:`_read_per_agent_hooks`
        reads its ``hooks:`` key from (same IN-set grain, scoped per agent).
        ``[]`` when the file or key is absent."""
        import yaml
        path = self._hot_reload_project_root() / ".reyn" / "agents" / self.agent_name / "hooks.yaml"
        if not path.is_file():
            return []
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — malformed → treat as absent
            return []
        if not isinstance(raw, dict):
            return []
        from reyn.security.secrets.interpolation import expand_env
        data = expand_env(raw)
        composers = data.get("composers") if isinstance(data, dict) else None
        return list(composers) if isinstance(composers, list) else []

    def _read_per_session_composers(self) -> list:
        """Read the per-SESSION composer layer (Hook-Event Redesign Phase 4b/5,
        #2880/#2881) — the ``composers:`` key of the SAME per-session
        ``hooks.yaml`` file :meth:`_read_per_session_hooks` reads its
        ``hooks:`` key from (#2285's 4th, most-specific layer). ``[]`` when
        the file or key is absent (or the file is malformed)."""
        import yaml
        path = Path(self._snapshot_path).parent / "hooks.yaml"
        if not path.is_file():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — malformed → treat as absent
            return []
        composers = data.get("composers") if isinstance(data, dict) else None
        return list(composers) if isinstance(composers, list) else []

    async def _reapply_hooks(self, in_set: dict) -> bool:
        """Reapply the hook layers (#2073 S2b + per-agent add-on) — re-read the global
        .reyn/hooks.yaml (IN-set) AND the per-agent .reyn/agents/<name>/hooks.yaml,
        re-combine with the FIXED reyn.yaml startup layer, and swap the dispatcher's
        registry. The dispatcher reads its registry fresh per dispatch, so the swap
        propagates to every holder. The startup layer is never re-read (safety
        boundary). Always rebuilds (handles add / change / remove of either layer)."""
        self._hook_dispatcher.replace_registry(self._build_hook_registry(in_set))
        return True

    def _hot_reload_project_root(self) -> "Path":
        """The project root for IN-set re-reads (same source the HotReloader uses)."""
        return getattr(self._registry, "_project_root", None) or Path.cwd()

    async def _reapply_cron(self, in_set: dict) -> bool:
        """Reapply .reyn/cron.yaml jobs to the live scheduler (#2073 S2/S4). Adds /
        replaces present jobs (add_job is idempotent by name) AND unschedules RUNTIME
        jobs removed from the file since the last reapply (#2073 S4 removal-diff). Only
        runtime (.reyn/cron.yaml) jobs are removable — startup (reyn.yaml) jobs are
        never in ``self._runtime_cron_names`` so they are never unscheduled. No active
        scheduler → no-op."""
        from reyn.runtime.cron import CronJob, get_active_scheduler
        sched = get_active_scheduler()
        if sched is None:
            return False
        jobs = [
            j for j in ((in_set.get("cron") or {}).get("jobs") or [])
            if isinstance(j, dict) and j.get("name")
        ]
        new_names = {j["name"] for j in jobs}
        changed = False
        # S4 removal-diff: unschedule runtime jobs deleted from .reyn/cron.yaml.
        for removed in self._runtime_cron_names - new_names:
            if await sched.remove_job(removed):
                changed = True
        # Add / replace the present runtime jobs (idempotent).
        for jd in jobs:
            await sched.add_job(CronJob(
                name=jd["name"], schedule=jd["schedule"], to=jd.get("to"),
                message=jd.get("message"), enabled=jd.get("enabled", True),
            ))
            changed = True
        self._runtime_cron_names = new_names  # track for the next reload's diff
        return changed

    async def _reapply_mcp(self, in_set: dict) -> bool:
        """Reapply MCP servers (#2073 S2) — re-probe via the existing turn-boundary
        refresh chain (which reads the re-read .reyn/mcp.yaml). Returns whether the
        in-memory tool cache changed."""
        result = await self.refresh_mcp_servers()
        return bool(result.get("refreshed"))

    async def _reapply_skills(self, in_set: dict) -> bool:
        """Reapply the skill registry (#2548 PR-B) — re-read the full config cascade
        (OUT-set reyn.yaml ∪ IN-set .reyn/config/skills.yaml) to rebuild the merged skill
        list, then update the LIVE available_skills on BOTH holders the Session owns
        (self._available_skills = base registered set; self._router_host._available_skills =
        filtered view after the per-session visibility override).

        The OUT-set (reyn.yaml-declared skills) survives because the full cascade merge
        in load_config() always includes it — the hot-reload never drops OUT-set entries.

        ``in_set`` is ignored; the full cascade re-read is the correct source (same pattern
        as refresh_mcp_servers roster re-read for the MCP roster gap fix). Returns True
        iff the base registered set actually changed."""
        from reyn.config.loader import load_config
        from reyn.data.skills.registry import build_skill_registry
        try:
            fresh_cfg = load_config(self._hot_reload_project_root())
            new_skills = build_skill_registry(fresh_cfg.skills)
        except Exception as exc:  # noqa: BLE001 — skills re-read is best-effort
            logger.warning("_reapply_skills: config re-read failed: %r", exc)
            return False
        old_names = {s.name for s in (self._available_skills or [])}
        new_names = {s.name for s in new_skills}
        if old_names == new_names:
            # Check if any entry fields changed (description / path / enabled / visibility).
            old_map = {s.name: s for s in (self._available_skills or [])}
            if all(
                new_s.description == old_map[new_s.name].description
                and new_s.path == old_map[new_s.name].path
                and new_s.enabled == old_map[new_s.name].enabled
                and new_s.visibility == old_map[new_s.name].visibility
                for new_s in new_skills
            ):
                return False  # no change
        # Update the base registered set (Session) + the filtered view (router_host).
        self._available_skills = new_skills or None
        self._capability_visibility.reapply_skill_visibility()  # re-derives router_host._available_skills from new base
        return True

    async def _reapply_pipelines(self, in_set: dict) -> bool:
        """Reapply the pipeline registry (#2581) — re-read the full config cascade
        (``load_config(project_root).pipelines``) and rebuild the ``pipelines/`` dir
        scan via :func:`~reyn.data.pipelines.registry.build_pipeline_registry`, mirroring
        ``_reapply_skills`` exactly (same disk-loader shape, same dual-write need).

        ``PipelineRegistry`` is append-only by design (no clear/unregister — a
        shadowing-prevention invariant), so an added/changed/removed ``pipelines/*.yaml``
        can only be picked up by building a FRESH registry and SWAPPING the reference —
        never by mutating the old one in place.

        The swap is a dual-write, exactly like ``_available_skills`` /
        ``_router_host._available_skills``: ``RouterHostAdapter`` holds its OWN
        ``_pipeline_registry`` attribute captured at construction and never re-reads
        Session, so both holders must be reassigned or the adapter's copy (the one
        ``run_pipeline`` actually resolves against, via ``get_pipeline_registry()``)
        would silently keep serving the stale registry.

        Fail-loud-but-non-fatal: ``build_pipeline_registry(..., strict=True)`` raises
        ``PipelineLoadError`` (malformed DSL / duplicate declared name / missing path /
        name mismatch) on the FIRST broken on-disk entry — ``strict=True`` is passed
        explicitly here to opt back INTO that atomic fail-loud posture (the default,
        used by session-FACTORY construction, is lenient/per-entry-isolated instead —
        see ``build_pipeline_registry``'s own docstring for why the two call sites
        need opposite postures: a brand-new session has no "old registry" to protect,
        a live hot-reload does). The raise is caught here (alongside any other
        unexpected error) — the reload seam logs + returns False, leaving the OLD
        registry (on both holders) fully intact. The new registry object is only ever
        assigned after a fully successful build, so a malformed file at reload-time
        can never half-apply or clear the live registry (atomic-by-construction, same
        guarantee as skills).

        Note (R7): a pipeline run already in flight resolves its OWN definition from
        the snapshotted work order (``invocation.json``), never the live registry, so a
        reload never changes an in-flight run's own steps/schema. A not-yet-executed
        ``call`` step inside that run DOES resolve its target against the LIVE registry
        at the time that step executes (call-by-name is a live lookup by design) — so a
        mid-run reload can still change a pending call's target. Existing design, not a
        gap introduced here."""
        from reyn.config.loader import load_config
        from reyn.data.pipelines.registry import build_pipeline_registry
        try:
            fresh_cfg = load_config(self._hot_reload_project_root())
            new_registry = build_pipeline_registry(
                fresh_cfg.pipelines, self._hot_reload_project_root(),
                strict=True,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, last-good on failure (incl. PipelineLoadError)
            logger.warning("_reapply_pipelines: registry rebuild failed: %r", exc)
            return False
        # Dual-write swap (Session + the adapter's own captured copy) — only reached
        # after a fully successful build, so a failure above never half-applies.
        self.set_pipeline_registry(new_registry)
        return True

    async def _reapply_presentations(self, in_set: dict) -> bool:
        """Reapply the named-presentation-template registry (FP-0054 PR-C) — re-read
        the full config cascade (``load_config(project_root).presentations``) and
        rebuild via :func:`~reyn.data.presentations.registry.build_presentation_registry`,
        mirroring ``_reapply_pipelines`` exactly (same disk-loader shape, same
        dual-write need).

        The registry is rebuilt fresh + the reference SWAPPED (never mutated), so an
        added / changed / removed template is picked up. The swap is a dual-write:
        ``self._presentation_registry`` AND ``self._router_host._presentation_registry``
        (the adapter holds its own captured copy that ``make_router_op_context`` reads
        into each OpContext) — both must be reassigned or the adapter keeps serving the
        stale registry.

        Fail-loud-but-non-fatal: ``build_presentation_registry(..., strict=True)`` raises
        ``PresentationLoadError`` on the FIRST malformed template — ``strict=True``
        opts INTO the atomic last-good posture (a live session keeps its old registry
        rather than half-applying a broken reload). The raise is caught here (alongside
        any other error) — the seam logs + returns False, leaving the OLD registry (on
        both holders) intact. The new registry is only assigned after a fully successful
        build, so a malformed file at reload-time can never half-apply.

        ``in_set`` is ignored; the full cascade re-read is the correct source (same
        pattern as ``_reapply_skills`` / ``_reapply_pipelines``). Returns True iff a new
        registry was successfully built + swapped."""
        from reyn.config.loader import load_config
        from reyn.data.presentations.registry import build_presentation_registry
        try:
            fresh_cfg = load_config(self._hot_reload_project_root())
            new_registry = build_presentation_registry(fresh_cfg.presentations, strict=True)
        except Exception as exc:  # noqa: BLE001 — best-effort, last-good on failure (incl. PresentationLoadError)
            logger.warning("_reapply_presentations: registry rebuild failed: %r", exc)
            return False
        # Dual-write swap (Session + the adapter's captured copy) — only reached after
        # a fully successful build, so a failure above never half-applies.
        self._presentation_registry = new_registry
        self._router_host._presentation_registry = new_registry
        return True

    async def _reapply_per_agent_capability(self, in_set: dict) -> bool:
        """Reapply the per-agent capability (#2073 S2) — Session-orchestrated. Re-read
        .reyn/agents/<name>/profile.yaml and update the per-agent allowlists on the
        holders the Session owns (itself / router_host) from the new
        AgentProfile (the #2074 unified per-agent spec). No profile / no change →
        no-op. (Single-source-of-truth is a beauty-follow-up, out of hot-reload scope.)"""
        from reyn.runtime.profile import AgentProfile
        agent_dir = self._hot_reload_project_root() / ".reyn" / "agents" / self.agent_name
        try:
            prof = AgentProfile.load(agent_dir)
        except (FileNotFoundError, OSError):
            return False  # single-agent / no profile → nothing per-agent to reapply
        if prof.allowed_mcp == self._allowed_mcp:
            return False  # unchanged
        # Session orchestrates the multi-holder swap (the holders it owns).
        self._allowed_mcp = prof.allowed_mcp
        self._router_host._allowed_mcp = prof.allowed_mcp          # MCP gate (decl source)
        return True

    async def _reapply_new_agent(self, in_set: dict) -> bool:
        """New-agent reapply (#2073 S2) — a confirming no-op: agent discovery is
        filesystem-live (AgentRegistry.list_names / get_or_load walk .reyn/agents/
        per call), so a newly-added agent is already visible without a reload step.
        Kept as an explicit seam so the IN-set component is accounted for, and a
        future cached-roster would slot its refresh here."""
        return False

    # ── PR21: state persistence helpers (WAL + snapshot) ─────────────────────
    # PR-refactor-session-1 wave 2: WAL/snapshot ownership moved to
    # SnapshotJournal; pending_chains lifecycle moved to ChainManager.
    # The methods below are thin delegators kept for the session-internal
    # call sites (inbox enqueue + dequeue, restoration orchestration).

    async def _cross_session_hook_put(
        self, target_session_id: str, kind: str, payload: dict, *, wake: bool
    ) -> None:
        """#2072: deliver a hook push to ANOTHER session of this agent (cross-session push).

        The canonical wake-triple (``resolve_session`` / ``get_session`` → ``_put_inbox`` →
        ``ensure_session_running``) — the same pattern TaskWaker / webhook_routing use. A
        ``transport:native`` target resolves via ``resolve_session``; a bare sid via
        ``get_session``. A target naming no live session is logged + dropped (the push is
        best-effort — a cross-session push to an absent peer must never crash the source run).
        Only a ``wake`` push boots the target's run-loop; a passive ride-along waits for the
        target's next turn."""
        reg = self._registry
        if ":" in target_session_id:
            transport, _, native = target_session_id.partition(":")
            target = reg.resolve_session(self.agent_name, transport, native)
        else:
            target = reg.get_session(self.agent_name, target_session_id)
        if target is None:
            logger.warning(
                "cross-session hook push: no live session %r for agent %r — dropped",
                target_session_id, self.agent_name)
            return
        await target._put_inbox(kind, payload)
        if wake:
            reg.ensure_session_running(self.agent_name, target_session_id)

    @property
    def halted_reason(self) -> "str | None":
        """#2259 PR-3: the fail-stop reason (e.g. ``"durability_failure"``) once the session has
        halted; ``None`` while running. The operator-visible in-memory state paired with the
        ``DurabilityHaltError`` raise (durability is dead → the reason cannot be a durable event)."""
        return self._halted_reason

    def _fail_stop_if_durability_dead(self) -> None:
        """#2259 PR-3: the fail-stop ACCEPT-edge guard. Raise ``DurabilityHaltError`` (recording the
        halt reason first, so it surfaces consistently with the process-edge) when durability has
        FAILED persistently — the agent stops accepting operations rather than accept one whose
        durable record will never land."""
        if self._state_log is not None and self._state_log.durability_failed:
            self._halted_reason = "durability_failure"
            raise DurabilityHaltError(
                f"agent '{self.agent_name}' halted: persistent durability failure — the agent "
                "stopped accepting operations to avoid silent unbounded loss (in-memory state must "
                "not race ahead of a dead disk)"
            )

    async def _put_inbox(self, kind: str, payload: dict) -> str:
        """Append `inbox_put` to WAL via journal, then queue on the async
        inbox. Returns the assigned message id (also stamped into payload
        as `_msg_id` so the consumer can look it up).

        **Internal API — plugin authors should NOT call directly**
        (FP-0041 plugins-api). Use ``reyn.gateway.api.push_to_agent``
        instead; this signature may change between Reyn versions.
        Other internal Reyn modules (= InterAgentMessaging, MCP handler,
        InterventionHandler, ChatLifecycleForwarder) keep calling
        this directly because they manage their own additional state
        machines (= chain_id / request_id / etc.) on top.
        """
        # #2259 PR-3: fail-stop ACCEPT-edge. If durability has failed persistently, REJECT the op
        # rather than accept one whose durable record will never land — the raise is the operator's
        # synchronous signal (more than the CRITICAL log). Pairs with the run-loop process-edge halt.
        self._fail_stop_if_durability_dead()
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

    async def _stage_next_turn_context(self, kind: str, payload: dict) -> None:
        """Stage a wake=false ride-along (C) into next-turn context, durably
        (B=persist): append to the in-memory buffer + record the WAL/snapshot
        entry. Shared by ``_drain_to_wake`` (inbox ride-alongs) and the
        ``HookDispatcher`` (#1800 slice 5b — direct C-staging that bypasses the
        inbox). Byte-behavior-identical extraction of the prior inline pair."""
        self._next_turn_context.append({"kind": kind, "payload": payload})
        await self._journal.record_next_turn_context_staged(kind=kind, payload=payload)

    async def _launch_pipeline_from_hook(self, name: str, input_data: "dict | None") -> None:
        """#2608 H3: launch a registered Pipeline from a hook's
        ``pipeline_launch`` action — the ``HookDispatcher``'s injected
        ``launch_pipeline`` seam.

        Async/detached (``start_pipeline_run``, same call the
        ``run_pipeline_async`` tool verb makes): fire-and-continue — the
        pipeline runs in its own recoverable driver-session, spawned under
        THIS session's own (agent, sid) identity (permission-bounded ⊆ this
        session's own capability), and the result arrives later on THIS
        session's inbox as a ``pipeline_result`` message.

        Fail-fast-but-non-crashing: a missing collaborator (no AgentRegistry /
        no WAL) or an unregistered ``name`` logs a decision-enabling WARNING
        naming exactly what's missing and returns — never raises. The
        dispatcher's own per-hook ``try/except`` isolation is a second line of
        defense; resolving the failure HERE (rather than letting
        ``PipelineRegistry.get`` raise a bare ``PipelineNotFoundError`` up
        through the dispatcher's generic catch) gives the operator a clearer,
        more specific message.
        """
        if self._registry is None:
            logger.warning(
                "hook pipeline_launch %r: this session has no AgentRegistry — "
                "cannot launch a pipeline from a hook. Skipping.", name,
            )
            return
        if self._state_log is None:
            logger.warning(
                "hook pipeline_launch %r: this session has no WAL (state_log) "
                "— an async pipeline launch requires persistence. Skipping.",
                name,
            )
            return
        try:
            pipeline = self._pipeline_registry.get(name)
            schema_registry = self._pipeline_registry.get_schema_registry(name)
        except PipelineNotFoundError:
            logger.warning(
                "hook pipeline_launch: pipeline %r is not registered on this "
                "session's PipelineRegistry — register it before referencing "
                "it from a hook. Skipping launch.", name,
            )
            return

        from reyn.runtime.session_api import start_pipeline_run
        # #3097: no explicit pipeline_registry hand-off needed — the spawned
        # driver-session's own _reapply_pipelines seam fires uniformly at ITS
        # spawn (AgentRegistry.spawn_session_recorded -> refresh_config_projections,
        # inside start_pipeline_run -> _spawn_pipeline_driver_session), rebuilding
        # from the current on-disk cascade directly. Folds out #3094's spawn-local
        # override (which forwarded THIS session's live registry object) — the
        # family gate achieves the same effect without depending on the caller
        # having a fresh in-memory copy to hand off.
        await start_pipeline_run(
            self._registry,
            pipeline=pipeline,
            pipeline_name=name,
            input=input_data,
            reply_to_agent=self.agent_name,
            reply_to_sid=self._session_id,
            state_log=self._state_log,
            schema_registry=schema_registry,
        )

    async def _drain_to_wake(
        self,
    ) -> tuple[list[tuple[str, dict]], tuple[str, dict]] | tuple[None, None]:
        """Drain the inbox up to and including the first ``wake=true`` message.

        Each inbox payload carries an optional ``wake`` bool (default ``True``
        when absent).  Existing producers (user / task_ready
        / etc.) never set ``wake``; the absent-means-True default makes them
        all behaviorally identical to wake=true, so the common/back-compat
        path returns immediately after the first blocking get with no
        ride-alongs.

        Returns ``(ride_alongs, trigger)`` where:

        - ``ride_alongs``  — list of ``(kind, payload)`` tuples for every
          ``wake=false`` message drained before the trigger.  Staged for the
          next turn as context (slice 4b — see TODO below).  Empty in the
          common case.
        - ``trigger``      — the first ``wake=true`` (or absent-wake) message;
          this drives the turn.

        Special case: if the first blocking get yields ``shutdown``, returns
        ``(None, None)`` so the caller can signal loop exit.

        Decision A (RESOLVED, issuecomment-4773744053): if the queue empties
        while holding only ``wake=false`` ride-alongs (no trigger yet),
        re-enter the blocking wait.  Ride-alongs NEVER trigger a turn alone.

        Per-message ``inbox_consume`` is recorded via ``_consume_inbox`` for
        EACH drained message (ride-alongs and the trigger alike), so the
        snapshot stays correct on crash+restore.

        #1800 slice 4a.  ``wake=false`` ride-along staging (slice 4b) is the
        next step; no ``wake=false`` producers exist yet, so the collected
        list is returned but not consumed here.

        #1800 slice 4b: each ``wake=false`` ride-along is staged durably
        (B=persist) **here**, immediately after its ``inbox_consume``.
        Staging in ``_drain_to_wake`` rather than in ``run_one_iteration``
        closes the crash window: a crash while blocking-waiting for the
        trigger (Decision A) leaves the C's already in the WAL + snapshot,
        so restore_state recovers them.  ``run_one_iteration`` still
        receives ``ride_alongs`` (4a contract) but no longer re-stages them.
        The common path (no wake=false) never calls
        ``record_next_turn_context_staged``, preserving 4a equivalence.
        """
        ride_alongs: list[tuple[str, dict]] = []

        while True:
            # (a) Blocking wait — preserves the idle-sleep property exactly
            # as the previous single-get path did.  Also records
            # inbox_consume via _consume_inbox (journaled, P6-clean).
            kind0, p0 = await self._consume_inbox()

            # (b) Shutdown sentinel: propagate immediately regardless of any
            # already-accumulated ride-alongs.
            if kind0 == "shutdown":
                return None, None

            # (c) wake=true (or absent → default True): this is the trigger.
            # Common/back-compat path — returns after the first blocking get
            # with no ride-alongs.
            if p0.get("wake", True):
                return ride_alongs, (kind0, p0)

            # (d) wake=false ride-along: stage durably (B=persist) the
            # moment it is consumed, BEFORE re-entering the blocking wait
            # for the trigger.  This closes the gap: without this, a crash
            # in the blocking wait would lose the consumed-but-not-persisted
            # ride-along.
            await self._stage_next_turn_context(kind0, p0)
            ride_alongs.append((kind0, p0))

            # Non-blocking drain: collect additional wake=false messages until
            # either a wake=true trigger arrives or the queue is momentarily
            # empty (Decision A: re-enter the blocking wait in that case).
            while True:
                try:
                    kind_nb, p_nb = self.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    # Queue empty, no trigger yet — re-enter outer blocking
                    # wait (Decision A).
                    break

                # Record inbox_consume for each non-blocking dequeue.
                msg_id_nb = (
                    p_nb.get("_msg_id") if isinstance(p_nb, dict) else None
                )
                if kind_nb != "shutdown":
                    await self._journal.consume_inbox(msg_id=msg_id_nb)

                if kind_nb == "shutdown":
                    return None, None

                if p_nb.get("wake", True):
                    return ride_alongs, (kind_nb, p_nb)

                # wake=false via non-blocking path: stage durably before
                # accumulating (same B=persist guarantee as the outer path).
                await self._stage_next_turn_context(kind_nb, p_nb)
                ride_alongs.append((kind_nb, p_nb))

    def restore_state(self, snapshot: AgentSnapshot) -> None:
        """Adopt a recovered snapshot: install in journal, repopulate the
        async inbox, restore pending chains via ChainManager (which re-arms
        timeout watchdogs), and re-enqueue outstanding interventions
        (PR-intervention-link L5) so the user can clear them after restart.

        Callable from async context only — restoration schedules asyncio
        tasks."""
        self._journal.install(snapshot)
        # #2884: restore the loop-valve counter from its snapshot-backed durable
        # form — otherwise a crash+restart silently resets it to 0, handing a
        # near-cap hook self-continuation chain a free fresh budget window.
        self._hook_driven_turns = snapshot.hook_driven_turns
        for msg in snapshot.inbox:
            self.inbox.put_nowait((msg["kind"], msg["payload"]))
        self._chains.restore(on_fire=self._on_chain_timeout_fire)
        # R-D12: rehydrate the durable buffered intervention answers from
        # the snapshot. If a previous restart had buffered an answer (user
        # answered a restored intervention) and a SECOND crash hit before
        # the resuming run_id consumed it, we still have the answer here.
        for run_id, ans in snapshot.buffered_intervention_answers.items():
            if not isinstance(ans, dict):
                continue
            self._buffered_intervention_answers[run_id] = InterventionAnswer(
                text=ans.get("text", ""),
                choice_id=ans.get("choice_id"),
            )
        # #1800 slice 4b: restore the staged next-turn-context buffer. If the
        # session crashed while holding staged C messages (waiting for a trigger),
        # they are recovered from the snapshot so the trigger's next turn
        # still sees the accumulated context.
        self._next_turn_context = [
            entry for entry in snapshot.next_turn_context
            if isinstance(entry, dict)
        ]
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
                #      resuming run's first ask_user picks it up (L6).
                #      R-D12: buffer is also durably persisted via
                #      ``record_intervention_answer_buffered`` so the
                #      answer survives a second crash before the run
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

        #1800 slice 4a: uses ``_drain_to_wake`` instead of ``_consume_inbox``
        directly.  With no ``wake=false`` messages ever enqueued (the current
        state — no wake=false producers exist yet), ``_drain_to_wake`` reduces
        to a single blocking get and the behaviour is identical to before.

        #1800 slice 4b: ``_drain_to_wake`` now stages each wake=false
        ride-along durably (B=persist) as it is consumed — see that method.
        ``run_one_iteration`` receives ``ride_alongs`` for 4a contract
        compatibility but no longer re-stages them.
        """
        # #2259 PR-3: fail-stop PROCESS-edge. If durability has failed persistently, HALT before
        # processing the next op — in-memory state must not advance into a dead disk (the owner's
        # "no silent unbounded loss"). Returns False = run()'s while-loop exits. Pairs with the
        # _put_inbox accept-edge raise (both read the latched health-signal).
        if self._state_log is not None and self._state_log.durability_failed:
            self._halted_reason = "durability_failure"
            return False
        # #1800 slice 4a/4b: drain up to the first wake=true trigger.
        # ride_alongs holds wake=false C messages accumulated before the
        # trigger.  They are already staged durably by _drain_to_wake (4b);
        # no further persist needed here.
        ride_alongs, trigger = await self._drain_to_wake()
        if trigger is None:
            # shutdown sentinel
            return False
        kind, payload = trigger
        # #1953 §16 (recursive-request): stamp this session's per-turn execution
        # context from the trigger (the SOURCE of OpContext.current_task_id).
        self._stamp_execution_context(kind, payload)
        # #1800 slice 7: the loop valve. Bound hook self-continuation at the
        # SINGLE seam — before any per-turn work (sender attribution / turn_started
        # emit / turn_start dispatch / kind dispatch). A human user turn re-arms
        # the budget; each hook-originated (kind="hook") turn increments it. When
        # the count exceeds the effective cap, the on_limit checkpoint fires
        # (warn → ask_user → abort); if it does not extend, the over-limit hook
        # turn is SUPPRESSED ENTIRELY (no turn_started, no turn_start E-hook
        # re-trigger that would circumvent the bound, no _handle_hook_message) and
        # the run-loop returns — the session stays alive + idle for the next real
        # trigger. Monotonic counter + finite cap + reset-on-user-turn ⇒ finite.
        if kind == "user":
            self._hook_driven_turns = 0
            # #2884: snapshot-back the counter so a crash+restart does not hand a
            # near-cap self-wake loop a free fresh budget window (see restore_state).
            await self._journal.record_hook_driven_turns(count=0)
        elif kind == "hook":
            self._hook_driven_turns += 1
            await self._journal.record_hook_driven_turns(count=self._hook_driven_turns)
            _base_cap = self._safety.loop.max_hook_driven_turns
            if _base_cap > 0:
                _cap = _base_cap + int(self._safety_extensions.get("hook_driven_turns", 0.0))
                if self._hook_driven_turns > _cap:
                    decision = await self._handle_chat_limit_checkpoint(
                        kind="hook_driven_turns",
                        prompt=(
                            f"Hook self-continuation reached the cap of {_cap} "
                            f"consecutive hook-driven turns. Allow more?"
                        ),
                        detail=f"count={self._hook_driven_turns} cap={_cap}",
                        extension_amount=float(_base_cap),
                    )
                    if not decision.allow_continue:
                        # Bound reached + not extended → suppress this hook turn.
                        # The chain stops here; the session survives (idle).
                        return True
                    # allow_continue: the checkpoint accumulated the extension into
                    # self._safety_extensions["hook_driven_turns"], raising the
                    # effective cap so this + subsequent turns proceed until the
                    # new bound (no re-prompt every turn).
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
        # #1800 slice 5a: turn lifecycle audit event (P6). Emitted after the
        # trigger is consumed and before dispatch, so slice 5b can attach the
        # turn_start hook here. chain_id from the payload (may be absent for
        # non-user triggers — that is fine, kind alone identifies the turn type).
        self._chat_events.emit(
            "turn_started",
            kind=kind,
            chain_id=payload.get("chain_id"),
        )
        # #1800 slice 5b: turn_start lifecycle hooks.
        await self._hook_dispatcher.dispatch(
            "turn_start",
            build_hook_payload(
                "turn_start", agent_name=self.agent_name,
                kind=kind, chain_id=payload.get("chain_id"),
            ),
        )
        # ADR-0038 Stage 1c: busy until this turn settles (its WAL appends done).
        self._turn_idle.clear()
        # #2242: the turn body now runs as its OWN per-turn sub-task (not inline
        # on this driver task) — this is what makes hard-cancel possible.
        # cancel_inflight() can call `_turn_owner_task.cancel()` directly, which
        # injects CancelledError at whatever await point the sub-task is
        # currently suspended on (mid-generation: the litellm.acompletion await
        # inside RouterLoop), aborting the in-flight HTTP request immediately —
        # unlike the pre-#2242 inline design, where the turn body ran on THIS
        # (the driver's own) task and only the cooperative flag (checked at the
        # top of each router-loop iteration, never during an LLM call) could ask
        # it to stop.
        self._turn_owner_task = asyncio.create_task(self._run_turn_body(kind, payload))
        _cancelled = False
        try:
            try:
                try:
                    await self._turn_owner_task
                except asyncio.CancelledError:
                    if self._turn_cancel_self_initiated:
                        # #2242 WAL-invariant 1: CancelledError unwound the
                        # turn-body task straight out of whatever await it was
                        # suspended on (mid-generation: the LLM await) — every
                        # statement AFTER that await (parsing the response,
                        # appending it to history, any further tool iteration)
                        # never executes, so the cancelled turn's result is
                        # never appended. Swallow here (do NOT re-raise) so the
                        # driver task — and thus the agent — survives to serve
                        # the next turn; only the per-turn sub-task was cancelled.
                        _cancelled = True
                    else:
                        # Not our own cancel_inflight() call — this driver task
                        # itself was cancelled from outside (e.g. an anyio scope
                        # teardown for the MCP/A2A request-handler task pumping
                        # run_one_iteration directly, FP-0013 §ADR-A). Preserve
                        # the pre-#2242 behaviour: let it propagate.
                        raise
            finally:
                # #2242 Finding 1: reset the self-initiated flag UNCONDITIONALLY
                # here (not only on the swallow branch). If cancel_inflight() set
                # it True but the CancelledError was never actually delivered to
                # this turn — e.g. the turn body completed (or caught+suppressed
                # the cancel) in the same tick before it landed, so `await
                # self._turn_owner_task` returned normally and the `except` above
                # never ran — a per-branch reset would leave the flag stuck True.
                # It would then mis-classify the NEXT turn's EXTERNAL cancel as
                # self-initiated and wrongly swallow it, violating the FP-0013
                # external-cancel-re-raise contract. A finally clears it on every
                # path (normal return, swallowed cancel, re-raised external
                # cancel) so the flag never outlives the turn that set it.
                self._turn_cancel_self_initiated = False
                self._turn_owner_task = None
                self._turn_idle.set()
                # Symmetric turn-end lifecycle event. turn_completed fires only on
                # the router path; turn_settled fires for EVERY turn kind (including
                # slash / intervention short-circuits that return before the router),
                # giving UI working-indicators driven by turn_started a reliable
                # clear signal regardless of how the turn ended.
                self._chat_events.emit(
                    "turn_settled", kind=kind, chain_id=payload.get("chain_id"),
                )
            if _cancelled:
                # #2242 WAL-invariant 2: a hard-cancel does not touch any
                # ALREADY-SPAWNED fire-and-forget WAL-append task (e.g. an
                # intervention-dispatch task tracked via `_track_wal_task` before
                # the cancelled turn's LLM await was reached) — cancelling
                # `_turn_owner_task` cancels only that one sub-task, never a
                # sibling task. `_turn_idle` is already `.set()` above (this turn
                # is done), so `await_quiescent()`'s re-entrancy check
                # (`current_task() is not self._turn_owner_task`, and
                # `_turn_owner_task` is already None) takes the `_turn_idle.wait()`
                # branch and returns immediately (already set) — it does not
                # self-deadlock; it exists here purely to JOIN any such stragglers
                # before this method returns, so they cannot land after the
                # session is reported idle.
                await self.await_quiescent()
        finally:
            # 0062: an outer finally so an ephemeral agent-step session still gets
            # scheduled to vanish even when the turn body raises past the inner
            # finally (e.g. a StructuredOutputError re-raised by
            # ``_handle_user_message`` — see session.py's ``except
            # StructuredOutputError: raise``). Previously ``_maybe_schedule_
            # ephemeral_vanish()`` sat AFTER this whole try block and was skipped
            # on any propagating exception, leaking the ephemeral session; this
            # feature is the first production path that raises a typed exception
            # out of a NORMAL (non-cap) turn, so the pre-existing gap is closed
            # here rather than shipped as a new leak.
            self._maybe_schedule_ephemeral_vanish()
        return True

    async def _run_turn_body(self, kind: str, payload: dict) -> None:
        """#2242: the per-kind turn dispatch, run as ``run_one_iteration``'s
        per-turn cancellable sub-task (``self._turn_owner_task``).

        Byte-identical dispatch to the pre-#2242 inline body (extracted, not
        rewritten) — a NORMAL (non-cancelled) turn behaves exactly as before;
        the only change is WHICH task executes it, so ``cancel_inflight()`` can
        target this task directly with ``asyncio.Task.cancel()`` instead of
        relying solely on the cooperative flag ``RouterLoopDriver`` polls at
        each iteration boundary (too coarse to interrupt a mid-flight LLM
        call — see ``cancel_inflight``'s docstring)."""
        if kind == "user":
            await self._handle_user_message(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or _new_chain_id(),
            )
        elif kind == "agent_request":
            await self._handle_agent_request(payload)
        elif kind == "agent_response":
            await self._handle_agent_response(payload)
        elif kind == "pipeline_result":
            # IS-2: an async pipeline driver-session posted its terminal
            # result here (the agent_response mirror — but chainless: the
            # launch returned immediately, so this is a fresh turn, routed
            # exactly like a task wake).
            await self._handle_pipeline_result(payload)
        elif kind in ("task_ready", "task_dependency_aborted"):
            # #1953 slice 7: the TaskWaker delivered a dep-graph disposition
            # (a dependent became ready, or a parent must decide recovery). Both
            # surface as an OS-originated message so the LLM acts via ordinary
            # task ops (P7 — no decision vocabulary).
            await self._handle_task_wake(payload)
        elif kind == "hook":  # HOOK_INBOX_KIND (#1800 slice 5b)
            # E (wake=true) lifecycle-hook push delivered as a turn trigger:
            # a system-role [hook:name] message + one router turn (self-
            # continuation). The attribution + wake binding ride in the
            # payload (race-free; the slice-7 valve can count hook-driven
            # turns, and the audit trail attributes the turn to the hook).
            await self._handle_hook_message(payload)

    def _maybe_schedule_ephemeral_vanish(self) -> None:
        """#2103: schedule the ephemeral auto-vanish teardown once this session's turn
        is idle-done. Thin forwarder — see ``SpawnTracker._maybe_schedule_ephemeral_vanish``
        for the full rationale (#3133 P3 Extract Class). ``current_task_id`` is per-turn
        mutable (reassigned every turn by ``_stamp_execution_context``), so it is threaded
        as a call-time argument rather than baked into the tracker's construction."""
        self._spawn_tracker._maybe_schedule_ephemeral_vanish(
            current_task_id=self._current_task_id,
        )

    def _stamp_execution_context(self, kind: str, payload: dict) -> None:
        """#1953 §16 (recursive-request): set the per-turn execution context read by
        the router op-ctx builders (→ ``OpContext.current_task_id``) so a
        ``task.create`` during this turn derives ownership (requester=<this task>,
        requester_kind=task). Per-turn + interleaving-precise: the context is exactly
        the task the THIS turn's wake is about, so a session juggling T1/T2 never
        mis-owns a create.

        - ``task_ready`` (``WAKE_READY_KIND``, execute-wake): stamp the task to execute
          (``meta.task_id``). Continuations are re-wakes (a completed sub-task promotes
          T → ``wake_ready_dependent`` re-stamps current=T on resume), so multi-turn
          execution is covered.
        - ``task_dependency_aborted`` (``WAKE_REQUESTER_KIND``, recovery-wake): stamp the
          MANAGING task-as-request (``meta.managing_task_id`` = T, set by
          ``notify_requester_decide`` only when the requester is a TASK) — so a
          REPLACEMENT the managing session creates this turn is owned by T (§16 B1,
          closes hole (i) recovery-create). None for a session-requester recovery (a
          top-level request's recovery stays session-owned). NOTE: NOT ``meta.task_id``
          here — that names the FAILED dependent, not the manager.

        Every trigger kind ``run_one_iteration`` can dispatch is classified
        explicitly (complete-by-construction — §16 B1.5, the iteration-cap orphan
        tui found: a hook self-continuation while executing T hit the old else→reset
        and orphaned a post-cap sub-task). The three bands:
        - SET (a task wake introduces the task context): the two above.
        - PRESERVE (a self-continuation / a response to the agent's OWN prior action —
          NOT a new context, so the current execution context must survive): ``hook``
          (the #1800 self-continuation), ``agent_response`` (a reply to a request THIS
          agent sent). These never switch tasks, so preserving is interleaving-safe.
        - RESET→None (a genuinely NEW external context = session-owned creates):
          ``user``, ``agent_request`` (an INCOMING peer ask), ``pipeline_result``
          (IS-2: an async pipeline's terminal result — a fresh external context,
          not a continuation of a task the receiver was executing). An unknown
          future kind falls here — fail-safe to session-owned (a mis-own/leak is
          worse than an orphan); a new self-continuation kind must be added to
          the PRESERVE set.

        Linger note (B1.5, considered + accepted): a post-completion hook can preserve
        current=T past T's completion. Functionally harmless — §16 B2's
        ownership-cascade fires on ABORT (a COMPLETED T is skipped), and a
        linger-owned sub-task's own recovery still routes to T's assignee. The
        clear-on-terminal alternative adds op→session coupling for no functional gain,
        so it is intentionally NOT done.

        proposal 0060 Phase 1 Layer A (A7): also derives ``self._current_turn_origin``
        — the OS-authoritative provenance classification of this turn, threaded into
        ``OpContext.turn_origin`` at both ctx-build sites exactly like
        ``current_task_id`` above. Only an explicit ``kind == "user"`` turn grants
        ``"user_directed"``; EVERY other kind — hook, pipeline_result, the wake family,
        sub-agent ``agent_request``/``agent_response``, or any future kind this method
        does not yet know about — resolves to the strictER ``"auto_improvement"``. This
        is an if/else fail-safe, not a lookup table: there is no path by which an
        unmapped kind can silently fall through to ``"user_directed"`` (0060 §2.7 —
        that would let an autonomous turn bypass the Phase-4 auto-improvement gate).
        Sub-agent turns are deliberately `"auto_improvement"` (lead-adjudicated,
        Addendum B A7): a human directed the PARENT task, not necessarily this
        install action."""
        meta = payload.get("meta", {})
        if kind == WAKE_READY_KIND:
            self._current_task_id = meta.get("task_id")
        elif kind == WAKE_REQUESTER_KIND:
            self._current_task_id = meta.get("managing_task_id")
        elif kind in (HOOK_INBOX_KIND, "agent_response"):
            pass  # PRESERVE: self-continuation / response to the agent's own action
        else:
            self._current_task_id = None  # user / agent_request / unknown → new context
        # A7: fail-safe if/else — "user" is the ONLY kind granting user_directed.
        self._current_turn_origin = "user_directed" if kind == "user" else "auto_improvement"

    async def _handle_task_wake(self, payload: dict) -> None:
        """#1953 slice 7: surface a Task dep-graph wake (``task_ready`` /
        ``task_dependency_aborted``) to the LLM as one router turn, so it resumes /
        recovers the work via ordinary task ops."""
        await self._handle_user_message(
            payload.get("text", ""),
            chain_id=payload.get("chain_id") or _new_chain_id(),
        )

    async def _handle_pipeline_result(self, payload: dict) -> None:
        """IS-2: surface an async pipeline's terminal result (``pipeline_result``)
        to the LLM as one router turn — same shape as ``_handle_task_wake``: the
        driver already formatted the OS-framed ``text``."""
        await self._handle_user_message(
            payload.get("text", ""),
            chain_id=payload.get("chain_id") or _new_chain_id(),
        )

    async def _handle_hook_message(self, payload: dict) -> None:
        """#1800 slice 5b: surface an E (wake=true) lifecycle-hook push as one
        router turn (self-continuation). The push is appended as an attributed
        system-role ``[hook:name]`` message — a NEW message (fidelity: never a
        silent mutation of an existing one) using the shared
        ``_format_hook_attribution`` helper so C and E cannot drift — then a
        single router turn runs."""
        name = payload.get("name", "hook")
        text = payload.get("text", "")
        chain_id = payload.get("chain_id") or _new_chain_id()
        self._append_history(ChatMessage(
            role="system",
            content=_format_hook_attribution(name, text),
            ts=_now_iso(),
            meta={"chain_id": chain_id},
        ))
        try:
            await self._run_router_loop(text, chain_id)
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(
                exc, chain_id=chain_id, user_text=text,
            )

    async def run(self) -> None:
        self._chat_events.emit("chat_started", agent_name=self.agent_name, model=self.model)
        # #1800 slice 5a: session lifecycle audit event (P6). Emitted alongside
        # chat_started; marks the boundary of the session's resource scope so
        # slice 5b can attach the session_start hook here.
        self._chat_events.emit("session_started", agent_name=self.agent_name)
        # #1800 slice 5b: session_start lifecycle hooks.
        await self._hook_dispatcher.dispatch(
            "session_start",
            build_hook_payload("session_start", agent_name=self.agent_name),
        )
        # #2608 H4: start the filesystem watcher (no-op if no fs_watch.paths
        # configured or 'watchdog' isn't installed — see FsWatcher.start).
        await self._fs_watcher.start()
        # Hook-Event Redesign Phase 5 part 1 (proposal 0059 §9 item 3 / #2881):
        # start every configured Composer (no-op — an empty list, the default
        # — spawns zero background tasks, byte-identical to pre-Composer-
        # wiring) and the composed:*->Sync consumer bridge (subscribes to this
        # session's own bus; a no-op happy path if no hook is registered
        # ``on: composed:*``, mirroring the no-hooks HookDispatcher equivalence).
        self._composer_registry.start()
        self._composed_consumer.start()

        # #1830 / FP-0052: warn if the startup model is above the cost threshold.
        # Fires once per session per model class (de-duped in maybe_emit_model_cost_warn).
        from reyn.runtime.model_cost_warn import maybe_emit_model_cost_warn
        maybe_emit_model_cost_warn(self, self.model, action="session_start")

        try:
            while await self.run_one_iteration():
                pass
        finally:
            try:
                await self._drain_on_shutdown()
            finally:
                # #2608 H4: stop the filesystem watcher (join the observer
                # thread). Nested finally so a raising ``_drain_on_shutdown``
                # can never skip this — the watcher must be torn down whenever
                # ``run()`` exits, cleanly or not. FsWatcher.aclose() itself is
                # idempotent + CancelledError-safe (see its own finally).
                try:
                    await self._fs_watcher.aclose()
                except Exception:  # noqa: BLE001 — teardown fault isolation, never blocks shutdown
                    logger.warning("FsWatcher.aclose() raised during session teardown", exc_info=True)
                # Hook-Event Redesign Phase 5 part 1 (#2881): stop the
                # composed-consumer bridge + every Composer's background task
                # (both cancel-safe/idempotent even if never started — see
                # ComposedEventConsumer.stop / ComposerRegistry.stop). Teardown
                # fault isolation mirrors the FsWatcher.aclose() guard above.
                try:
                    await self._composed_consumer.stop()
                    await self._composer_registry.stop()
                except Exception:  # noqa: BLE001 — teardown fault isolation, never blocks shutdown
                    logger.warning(
                        "Composer/ComposedEventConsumer teardown raised during session "
                        "teardown", exc_info=True,
                    )
                self._chat_events.emit("chat_stopped", agent_name=self.agent_name)
                # #1800 slice 5a: session lifecycle audit event (P6). Emitted alongside
                # chat_stopped; marks the end of the session's resource scope.
                self._chat_events.emit("session_completed", agent_name=self.agent_name)
                # #1800 slice 5b: session_end lifecycle hooks (F's natural resource
                # scope). The run-loop has exited, so an E push here is not drained
                # (harmless); session_end is the C/F point in practice.
                await self._hook_dispatcher.dispatch(
                    "session_end",
                    build_hook_payload("session_end", agent_name=self.agent_name),
                )
                await self._put_outbox(OutboxMessage(kind="__end__", text=""))

    async def _drain_on_shutdown(self) -> None:
        """Cancel any in-flight background work, then tear down on shutdown.

        Memory writes happen inline during each router turn, so there is no
        background extraction to drain — shutdown is teardown of whatever the
        user explicitly launched, plus a final await on the compaction task
        (if any) so the summary entry gets persisted before the process exits.

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
        # Stage-1 decouple: SkillRunner removed; no background skills to drain.

        # PR18: cancel any pending chain-timeout watchdogs so they don't keep
        # the loop alive past shutdown. Late-firing timers swallow their work
        # (the pending entry is gone) but cancellation is cleaner.
        # PR-refactor-session-1 wave 2: cancellation delegated to ChainManager.
        await self._chains.shutdown()

        # #1128 PR-a: the background compaction task was removed; compaction
        # now runs synchronously inside the router handler, so there is no
        # in-flight task to drain at shutdown.

    async def _handle_user_message(self, text: str, *, chain_id: str) -> None:
        # Slash commands (`/list`, `/tasks kill <id>`, `/answer <id> <text>`)
        # take precedence over both the active-intervention router and a
        # fresh router turn.
        if text.startswith("/"):
            if await self._maybe_handle_slash(text):
                return
        # If a spawned run is waiting on a user intervention (ask_user or
        # permission prompt), route this input to that intervention instead of
        # starting a fresh router turn.
        if await self._maybe_answer_oldest_intervention(text):
            return

        # #1800 slice 4b: apply any staged wake=false ride-along (C) context
        # to this turn.  Injected AFTER the slash/intervention short-circuits
        # so C messages only attach to an actually-running router turn (flow-
        # trace §3 risk note: a slash-command short-circuit must NOT consume
        # the staged C's — they wait for the real turn).
        if self._next_turn_context:
            for entry in self._next_turn_context:
                hook_kind = entry.get("kind", "hook")
                payload_data = entry.get("payload", {})
                hook_name = payload_data.get("name", hook_kind)
                hook_text = payload_data.get("text", "")
                self._append_history(ChatMessage(
                    role="system",
                    content=_format_hook_attribution(hook_name, hook_text),
                    ts=_now_iso(),
                ))
            self._next_turn_context.clear()
            await self._journal.record_next_turn_context_cleared()

        # R-D4: chat turn boundary — opportunistically check WAL size and
        # truncate if it has grown past the safety-net threshold. Long-idle
        # multi-agent / multi-chain idle sessions don't fire completion events,
        # so without this
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
        # NOTE: no "thinking…" status is emitted here. The turn-in-progress signal
        # is the event-driven working indicator (turn_started → turn_settled, via
        # ChatRenderer.on_chat_event), so a separate "thinking…" status line is a
        # redundant double-display (the inline CUI showed both "· thinking…" and
        # the "Working…" spinner). It was also the source of an orphaned blank line
        # before each reply (a cleared transient leaving its separator behind).

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
        except StructuredOutputError:
            # 0062: a schema-bearing agent-step turn's typed structured-output
            # failure (unsupported model / provider-rejected schema / exhausted
            # re-prompt budget) must reach the programmatic driver
            # (``run_agent_step`` via ``MessageBus.request``) as the ORIGINAL
            # typed exception — re-raise instead of falling through to the
            # generic handler below, which would collapse it into an opaque
            # classified "error" outbox string and lose the failure-mode
            # distinction the caller needs (§2.1's 3 distinct modes).
            raise
        except RouterCapExceeded as exc:
            await self._emit_router_cap_exhausted_user(exc, chain_id=chain_id, user_text=text)
            return
        except Exception as exc:
            # #187 B1 instrument: a mid-work router-loop exception (e.g. the final
            # call_llm raising after litellm's internal retries) was swallowed into a
            # classified outbox summary, silently terminating the turn — for an
            # autonomous run-once this ends the agent mid-edit with no diagnosable
            # trace (req=resp+1, no logged response). Surface the FULL exception
            # (stderr traceback + a P6 event) so the root error is primary-evidence
            # for the fix; the classified summary still goes to the outbox unchanged.
            logger.exception(
                "router loop terminated by unhandled exception (chain_id=%s)",
                chain_id,
            )
            try:
                self._chat_events.emit(
                    "router_loop_terminated_by_exception",
                    chain_id=chain_id,
                    error_type=type(exc).__name__,
                    error=repr(exc)[:500],
                )
            except Exception:  # noqa: BLE001 — instrumentation must never break the path
                pass
            await self._put_outbox(OutboxMessage(
                kind="error", text=classify_router_error(exc),
                meta={"chain_id": chain_id},
            ))
            return

        # #1128 PR-a: the post-reply fire-and-forget compaction check
        # (spawn_maybe → _maybe_compact, 30K-absolute trigger) was removed.
        # Auto-compaction is driven synchronously by the pre-frame guard
        # (ContextBudgetAdvisor.maybe_force_compact → force_compact_now, window-relative
        # effective_trigger) before each router call, plus on-demand (/compact,
        # compact op) and the retry_loop overflow backstop.

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        """Drop transient kinds while detached; durable kinds are queued.

        While `is_attached=False` (PR10 multi-agent: agent running in the
        background), `status`/`trace` carry no value to a detached display
        and would just accumulate in the queue. `agent`/`intervention`/
        `error`/`__end__` are kept so they reach the user
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
            from reyn.runtime.transport import ExternalRef
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
        self, exc: "RouterCapExceeded", *, chain_id: str, user_text: str = "",
        _llm_caller: "Any | None" = None,  # Tier 2 test seam: scripted-fake injection
    ) -> None:
        """User-facing fallback when the per-turn router cap is reached.

        #1496 (site C): attempt a force-close wrap-up so the LLM can
        summarize what was accomplished before the turn ends. Uses the
        session's accumulated history (not run_loop's local messages —
        router_cap fires BEFORE run_loop starts). Falls back to the
        original canned error + hardcoded reply if wrap-up fails or
        produces no text.
        """
        # #1496: emit audit event + attempt LLM wrap-up
        self._chat_events.emit(
            "limit_denied",
            kind="router_cap",
            count=exc.count,
            cap=exc.cap,
            chain_id=chain_id,
        )
        try:
            from reyn.runtime.router_loop import RouterLoop
            history = self._history_buffer.build_history()
            messages: list[dict] = [
                *history,
                *(
                    [{"role": "user", "content": user_text}]
                    if user_text else []
                ),
            ]
            _temp_loop = RouterLoop(
                host=self._router_host, chain_id=chain_id, llm_caller=_llm_caller,
            )
            _resolved = self._router_host.resolve_model(self.model)
            _reason = (
                f"router cap exhausted ({exc.count}/{exc.cap})"
                f"{'; last reason: ' + exc.last_reason if exc.last_reason else ''}"
            )
            _wrapup = await _temp_loop._force_close_call_with_retry(
                messages, resolved_model=_resolved, reason=_reason,
            )
            if _wrapup.content:
                await self._put_outbox(OutboxMessage(
                    kind="agent",
                    text=_wrapup.content,
                    meta={
                        "chain_id": chain_id,
                        "limit_stopped": True,
                        "limit_kind": "router_cap",
                    },
                ))
                self._append_history(ChatMessage(
                    role="assistant",
                    content=_wrapup.content,
                    ts=_now_iso(),
                    meta={"chain_id": chain_id, "source": "router_cap_exhausted_wrap_up"},
                ))
                return
        except Exception:  # noqa: BLE001 — wrap-up failed; degrade to canned reply
            pass

        # Fallback: original canned error + hardcoded reply
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

        Mirrors the phase-side limit checkpoint but uses the
        Session's intervention dispatcher (= ``_dispatch_intervention``,
        which records the WAL ``intervention_dispatched`` event before
        delivering the prompt) + on_limit + a session-stable run_id
        (= the agent name when no narrower scope applies, or the
        current chain_id for chain-scoped checkpoints). Emits a
        ``safety_limit_checkpoint`` audit event so the decision is
        visible alongside the existing chat events.

        #3053: the bus resolves BRIDGE-AWARE via ``_make_router_intervention_bus``
        (the SAME seam #3052 gave every MCP router-op, and #3053 gave the
        per-LLM-call ``_ChatBudgetBus``) — this checkpoint (``router_cap`` /
        ``max_agent_hops`` / ``phase_seconds`` / ``chain_seconds``) is reachable on
        an ATTACHED pipeline driver session exactly like the per-call budget gate,
        so freezing a self-bound ``_dispatch_intervention`` here would auto-refuse
        on the driver's own listener-less registry instead of reaching the
        pipeline originator — the identical anti-pattern #3053's structural guard
        also caught here.
        """
        # Adapter that conforms to the InterventionBus Protocol by resolving the
        # bridge-aware bus fresh on each call (never a frozen self-bound
        # dispatcher — #3053). ``_dispatch_intervention`` (reached transitively
        # through the resolved bus) records the intervention_dispatched /
        # intervention_resolved WAL events automatically, so per-site callers
        # don't need to.
        make_router_bus = self._make_router_intervention_bus

        class _ChatLimitBus:
            async def request(self, iv):  # type: ignore[no-untyped-def]
                return await make_router_bus().request(iv)

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
        """Forwarding → RouterLoopDriver._check_cap (PR-3)."""
        await self._loop_driver._check_cap(user_text)

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

    async def answer_oldest_intervention_choice(self, choice_id: str) -> bool:
        """Deliver a chosen choice id to the oldest pending intervention.

        The inline region selector calls this when the user picks a choice: the
        id is authoritative (bypasses text/hotkey ``match_choice``), reusing the
        same ``choice_id_override`` path A2A peer answers use. Returns False when
        nothing is pending. A public UI seam for closed-set typed-input.
        """
        iv = self._interventions.head()
        if iv is None:
            return False
        return await self._deliver_answer_to(iv, "", choice_id_override=choice_id)

    async def answer_oldest_intervention_text(self, text: str) -> bool:
        """Deliver a free-text answer to the oldest pending intervention.

        The inline CUI calls this when the user submits text via the normal
        input bar while an ask_user intervention is pending. Uses match_choice
        so hotkeys (``"1"`` → first option) and option names resolve correctly.
        Returns False when nothing is pending.
        """
        iv = self._interventions.head()
        if iv is None:
            return False
        return await self._deliver_answer_to(iv, text)

    async def _deliver_answer_to(
        self,
        iv: UserIntervention,
        text: str,
        *,
        choice_id_override: str | None = None,
        external_source: bool = False,
        attribution: "dict | None" = None,
    ) -> bool:
        """Thin wrapper → InterventionHandler.deliver_answer_to.

        ``choice_id_override`` is forwarded so peer-side callers (= A2A
        POST answer with explicit choice_id per PR #285 Gap 4) can bypass
        the TUI's text-based match_choice. issue #292 (α).

        ``external_source`` (FP-0050 / #1862, EP7) marks an untrusted peer
        answer so its history-bound copy is fenced. Set only by
        ``answer_pending_intervention`` (the A2A / webhook entry); the
        default ``False`` keeps all local UI callers (TUI / slash)
        unfenced.

        ``attribution`` (ADR-0039 P3) stamps *who granted* — the
        authenticated ``auth_user_id`` + connection id — onto the
        ``user_answered_intervention`` audit event, so a 2-on-1 grant is
        attributable to the identity AND the terminal. Local UI callers pass
        ``None`` (the operator's own process needs no wire attribution).
        """
        return await self._intervention_handler.deliver_answer_to(
            iv, text,
            choice_id_override=choice_id_override,
            external_source=external_source,
            attribution=attribution,
        )

    async def answer_pending_intervention(
        self,
        run_id: str,
        answer: "InterventionAnswer",
        *,
        attribution: "dict | None" = None,
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
        the peer-answer case there's typically one iv per run. Delegates to the handler so history +
        ``user_answered_intervention`` event + outbox cleanup all fire
        the same way as TUI answers — observers on the audit trail
        see a consistent shape regardless of answer origin.

        ``attribution`` (ADR-0039 multi-client input-broadcast fix, symmetric
        with ``answer_intervention_by_id``'s AG-UI path) threads through to the
        ``kind="user"`` broadcast frame so a peer-answer is attributable, same
        shape as ``user_answered_intervention``. A2A currently has no
        per-request identity to plumb here (unlike the AG-UI auth gate), so
        today's only caller passes ``None`` — the answer still broadcasts
        (unattributed, rendered as the bare operator line), leaving this a
        structurally-ready seam rather than a fabricated identity.

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
                # FP-0050 / #1862 (EP7): this is the single authoritative
                # peer-answer entry (A2A POST / webhook). Mark the answer
                # external so its history-bound copy is fenced before it
                # reaches conversation context.
                external_source=True,
                attribution=attribution,
            )
        return False

    async def answer_intervention_by_id(
        self,
        intervention_id: str,
        text: str = "",
        *,
        choice_id_override: str | None = None,
        external_source: bool = False,
        attribution: "dict | None" = None,
    ) -> bool:
        """Deliver an answer to the intervention identified BY ID (ADR-0039 P3, R1).

        The AG-UI HITL round-trip correlates a ``TOOL_CALL_RESULT`` to its
        intervention by the ``toolCallId`` (= this id), so the grant lands on the
        EXACT intervention the operator was shown — never the head-of-queue,
        which a second queued prompt could have displaced between display and
        answer (the answer-oldest race). An unknown or already-resolved id is a
        typed reject (returns ``False``) with **no** head fallback: the caller
        surfaces it as a rejected grant, never silently redirects it.

        The lookup + ``choice_id`` validation are server-side against this
        session's own registry entry — the client echoes only ``(id, text |
        choice_id)`` and its copy of the prompt/choices is not trusted (R6).
        """
        iv = self._interventions.get(intervention_id)
        if iv is None or iv.future.done():
            return False
        await self._deliver_answer_to(
            iv, text,
            choice_id_override=choice_id_override,
            external_source=external_source,
            attribution=attribution,
        )
        # Report whether the intervention was actually RESOLVED (the grant
        # landed), not merely whether the input was consumed: an unrecognized
        # choice emits a re-prompt hint (consumed) but leaves the future pending,
        # which the wire caller must see as a rejected answer — the operator's
        # terminal keeps the frontend-tool pending and the hint frame explains why.
        return iv.future.done()

    async def fail_close_interventions(self, reason: str) -> list[str]:
        """Typed-DENY every pending intervention whose answerable surface is gone.

        The load-bearing safety terminal (ADR-0039 D5(b)/P3): when the last
        operator surface for this session is lost and the grace window elapses,
        a pending ``ask_user`` / permission / safety-limit prompt must resolve to
        a typed refusal — never park unbounded. Per-intervention scope (R2): an
        intervention still answerable by a live listener (an A2A origin-pin peer)
        is left pending. Reuses the #2773 DENY shape (``refused=True`` + reason)
        and emits a P6 audit event per denied intervention (R5). Returns the
        denied intervention ids.
        """
        denied = self._interventions.deny_unanswerable_active(reason)
        for iv in denied:
            self._chat_events.emit(
                "intervention_denied",
                intervention_id=iv.id,
                kind=iv.kind,
                run_id=iv.run_id,
                actor=iv.actor,
                reason=reason,
            )
        return [iv.id for iv in denied]

    def emit_audit_event(self, event_type: str, **data) -> None:
        """Emit a P6 audit event on this session's event log (ADR-0039 P3).

        Narrow public seam for the AG-UI transport to record surface-lifecycle
        attribution — ``client_attached`` / ``client_seized`` / ``client_detached``
        — onto the durable ``.reyn/events`` audit trail (who attached which
        terminal, when authority moved). These types are not in the renderer
        forward-set, so they are audit-only (never a render frame).
        """
        self._chat_events.emit(event_type, **data)

    async def _announce_intervention(self, iv: UserIntervention) -> None:
        """Thin wrapper → InterventionHandler.announce."""
        await self._intervention_handler.announce(iv)

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
        caller) sees the change. For existing in-flight ivs that lose
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
        self._track_wal_task(asyncio.ensure_future(self._dispatch_intervention(iv)))
        return PendingOpView.from_intervention(iv)

    async def _dispatch_intervention(self, iv: UserIntervention) -> InterventionAnswer:
        """Dispatch one intervention via the InterventionCoordinator.

        Kept as a Session-level entry so existing call sites
        (ChatInterventionBus, _handle_chat_limit_checkpoint, tests) stay
        stable; the override-observe / origin-pin-stall / handler-dispatch
        orchestration lives in ``InterventionCoordinator.dispatch``.
        """
        return await self._intervention_coordinator.dispatch(iv)

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
             Phase 5+ adds the chain-walk to find the originating
             agent via an agent-lookup factory.
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
        ``Session.as_request_bus()`` (which returns an
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
            from reyn.runtime.services.intervention_handler import (
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
        # so it fires uniformly for the bus-emit path too (= an op
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
    ) -> "Session | None":
        """Hook for parent-agent delegation routing (issue #254 Phase 4).

        Return a Session to forward the request to a chain-upstream
        agent; return ``None`` to fall through to user_channel delivery.

        Default implementation returns ``None`` (= no parent resolution).
        Phase 5+ will walk the chain to find the originating agent and
        look it up via an agent-registry factory; Phase 4 only
        establishes the routing branch.
        """
        return None

    def as_request_bus(self) -> "AgentRequestBus":
        """Return a ``RequestBus``-typed adapter for this Session.

        OS-layer callers (= ``handle_limit_exceeded``, permission gates,
        ``ask_user`` op) can hold an ``AgentRequestBus`` without
        importing Session or knowing about the Agent's downstream
        routing choices. The adapter forwards ``request(iv)`` to
        ``handle_intervention(iv)``.

        issue #254 Phase 3 — the type-level realisation of the [A]
        contract from Phase 2: OS owns a ``RequestBus``, the bus is
        backed by an Agent (= Session), the Agent owns the routing
        decision and the downstream ``UserChannel`` selection.
        """
        return AgentRequestBus(self)

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
            # consumed by the resumed run.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._track_wal_task(
                    loop.create_task(
                        self._journal.record_intervention_answer_consumed(
                            run_id=run_id,
                        ),
                        name=f"buffered-answer-consumed-{run_id}",
                    )
                )
        return answer

    # ── agent-to-agent messaging (PR11 / PR14) ──────────────────────────────────
    # FP-0019 Wave 2 part 2: business logic extracted to InterAgentMessaging service.
    # Session keeps thin delegators here so existing internal call sites
    # (_on_chain_timeout_fire, _on_chain_peer_discarded, RouterHostAdapter
    # send_to_agent callback) continue to resolve without changes.

    async def _send_to_agent(
        self, *, to: str, request: str, depth: int, chain_id: str,
    ) -> None:
        """Thin delegator — business logic lives in InterAgentMessaging.send_to_agent."""
        await self._inter_agent_messaging.send_to_agent(
            to=to, request=request, depth=depth, chain_id=chain_id,
        )

    async def _send_agent_response(
        self, *, to: str, response: str, depth: int, chain_id: str,
        to_sid: "str | None" = None,
    ) -> None:
        """Thin delegator — business logic lives in InterAgentMessaging.send_agent_response."""
        await self._inter_agent_messaging.send_agent_response(
            to=to, response=response, depth=depth, chain_id=chain_id, to_sid=to_sid,
        )

    async def _handle_agent_request(self, payload: dict) -> None:
        """Thin delegator — business logic lives in InterAgentMessaging.handle_agent_request."""
        await self._inter_agent_messaging.handle_agent_request(payload)

    async def _handle_agent_response(self, payload: dict) -> None:
        """Thin delegator — business logic lives in InterAgentMessaging.handle_agent_response."""
        await self._inter_agent_messaging.handle_agent_response(payload)

    # ── chain timeout (PR18) ───────────────────────────────────────────────────
    # PR-refactor-session-1 wave 2: timer arm/cancel + sleep-and-fire loop are
    # now owned by ChainManager. The session keeps the on-fire callback below
    # so the upstream-error UX (synthesised response + chain_timeout event)
    # stays out of the service layer.

    async def _on_chain_timeout_fire(self, chain_id: str) -> None:
        """Forwarding → ChainTimeoutGlue.on_chain_timeout_fire (PR-4)."""
        await self._chain_timeout_glue.on_chain_timeout_fire(chain_id)
    async def _on_chain_peer_discarded(
        self, *, chain_id: str, peer: str, reason: str,
    ) -> None:
        """R-D14: AgentRegistry calls this when a peer agent's
        run for ``chain_id`` was discarded by the user.

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
            f"run ({reason}); waiting_on={waiting}"
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
                to_sid=pending.origin_sid,  # #2130
            )
        except Exception as exc:  # noqa: BLE001 — never wedge the loop
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain peer discarded: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    # ── slash command dispatch ──────────────────────────────────────────────────

    def _resolve_intervention_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Resolve a unique intervention id by prefix in the intervention registry."""
        return self._interventions.resolve_id_prefix(prefix)

    async def _maybe_handle_slash(self, text: str) -> bool:
        """Dispatch `/command args...` lines. Returns True when consumed.

        Delegates to the SlashRegistry in `reyn.interfaces.slash` so new commands
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
        from reyn.interfaces.slash import REGISTRY

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
            # 20+ command catalog into the error line (the previous list
            # truncated mid-name at ``try: /agent, /agents, /answer, /attach,``
            # hiding the actionable tail). ``suggest_for_unknown`` is a pure
            # helper in ``reyn.interfaces.slash`` so the suggestion contract is
            # directly testable without the surrounding session machinery.
            from reyn.interfaces.slash import suggest_for_unknown
            suggestions = suggest_for_unknown(cmd)
            known = ", ".join(f"/{n}" for n in suggestions)
            # ``kind="error"`` so the TUI renders an inline error (✗ glyph,
            # severity colour, scroll-away). The previous ``kind="system"``
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
        try:
            await slash_cmd.handler(self, args)
        except Exception as e:
            # A slash handler raising must not kill the session run loop: run()'s
            # `while await run_one_iteration()` has no `except`, so an uncaught
            # error here ends session.run() and silently drops every later inbox
            # message (the front-end keeps accepting input but never replies).
            # Surface a clean error and treat the command as consumed so the loop
            # continues. (CancelledError is BaseException → shutdown still cancels.)
            logger.exception("slash handler /%s failed", cmd)
            detail = f"{type(e).__name__}: {e}"
            if len(detail) > 72:
                detail = detail[:69] + "…"
            await self._put_outbox(OutboxMessage(
                kind="error", text=f"/{cmd} failed: {detail}",
            ))
            return True
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

    # NOTE: the slash handlers (list / answer / agents / attach / cost / budget)
    # live in ``src/reyn/runtime/slash/`` per the cli-redesign plan.
    # ``_resolve_intervention_id`` / ``_deliver_answer_to`` stay here as
    # session-state helpers the slash modules call back into.

    # ── RouterLoop helper methods (Wave 3 F1, kept for session callbacks) ──────────
    # _make_router_op_context + 3 helpers remain on Session because the
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

        Session receives the wrapped form from CLI bootstrap (config.mcp).
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

        Uses the session's events log and permission resolver. The actor
        "chat_router" is used for permission key lookups — it matches what the
        PermissionResolver uses to gate paths. All .reyn/ paths are in the
        default write zone so memory ops pass without additional approval.

        PermissionDecl is populated from the agent's effective permissions
        (file_read / file_write from config, mcp from configured servers) so
        that op_runtime layer permission checks actually gate access rather than
        silently allowing everything through an empty decl.
        """
        from reyn.runtime.router_op_context import build_router_op_context

        # #1412: single-sourced via build_router_op_context (shared with
        # RouterHostAdapter). Session wires intervention_bus POST-HOC on the
        # returned ctx (the MCP-op caller), so it is None at construction here;
        # media/multimodal/compact ops go via the registry / RouterHostAdapter
        # path (None here) — behavior-preserving.
        return build_router_op_context(
            events=self._chat_events,
            permission_resolver=self._perm,
            file_permissions=self._get_file_permissions_for_router(),
            mcp_servers=self._get_mcp_servers_for_router(),
            mcp_servers_flat=self._mcp_servers_flat(),
            allowed_mcp=self._allowed_mcp,
            workspace_base_dir=self._workspace_base_dir,
            workspace_state_dir=self._workspace_state_dir,
            environment_backend=self._environment_backend,
            sandbox_backend=self._sandbox_backend,
            sandbox_policy=(
                self._sandbox_config.policy
                if getattr(self, "_sandbox_config", None) is not None
                else None
            ),
            agent_id=self._agent_id,
            # #2708 P1: this builder serves file/MCP ops (_file_op / MCP), which no
            # `present` op reaches — so the present sink is EXPLICITLY None (the required
            # kwarg forces the decision, no silent omission). The visible present path is
            # RouterHostAdapter.make_router_op_context, which wires the surface consumer's sink.
            presentation_renderer=None,
            # #2409: forward the media store (the public twin RouterHostAdapter.make_router_op_context
            # already does — session.py:1826). Without it the chat-router MCP path got media_store=None
            # → MCP ImageContent couldn't be saved as a path-ref → a large image was inlined as
            # base64 to the LLM instead of a small path-ref (the clean-payload/offload gate needs the
            # image out of the inline body).
            media_store=self._media_store,
            contextual_permission=self._capability_visibility.contextual_permission,  # #1827 S3 → control-IR OpContext
            hook_dispatcher=self._hook_dispatcher,  # #1800 slice 5c: complete-by-construction (both router callers)
            hook_bus=self._hook_bus,  # Hook-Event Redesign Phase 5 part 2: emit_hook_event's publish target (both router op-ctx builders complete-by-construction)
            current_task_id=self._current_task_id,  # #1953 §16: ownership-derivation for task.create (enumerate ALL op-ctx builders)
            turn_origin=self._current_turn_origin,  # proposal 0060 Phase 1 (A7): OS-authoritative provenance source (enumerate ALL op-ctx builders)
            hot_reloader=self._hot_reloader,  # #2761 PR-2: per-session reloader (both router op-ctx builders complete-by-construction)
            render_template_bounds=self._render_template_bounds,  # #2679: operator bounds (both router op-ctx builders complete-by-construction)
            embedding_event_sink=self._embedding_event_sink,  # FP-0057 #2856 Part A: TUI model-download status sink for the embed op
            budget_gateway=self._budget,  # FP-0063 PC: embedding-cost recording entry point (enumerate ALL op-ctx builders; the load-bearing one for `embed` is RouterHostAdapter's)
        )

    def _make_router_intervention_bus(self):
        """The chat-router intervention bus for a router-initiated op, resolved
        BRIDGE-AWARE.

        SINGLE SOURCE for "which surface answers a router op's intervention",
        shared with the ``RouterHostAdapter`` ``intervention_bus_factory``
        (constructed above, ~session.py:1963): when this session is an ATTACHED
        pipeline DRIVER (it carries a ``SpawnBridgeInterventionListener`` — see
        ``_spawn_pipeline_driver_session``), the bus dispatches on the PARENT
        session's live-operator listener (compositionally resolved toward the
        outermost attached originator); otherwise a self-bound
        ``ChatInterventionBus`` on this session's own registry (a root chat, or a
        detached/headless run whose bridge is ``AuditOnlyInterventionBridge`` and
        thus fail-closes).

        #3049: the MCP op callers below (``_mcp_call_tool`` and its resource /
        prompt siblings) previously HARDCODED the self-bound branch, so a driver
        session's ``call_mcp_tool`` permission prompt (e.g. the ``rag_ingest``
        X1 pre-flight probes) orphaned on the driver's own listener-less
        registry — dispatched, stalled, and awaited forever (the confirmed hang).
        Routing them through this helper makes every IV-raising router leaf reach
        the pipeline originator uniformly, exactly as ``ask_user`` / ``present``
        already do via the bridge-aware ``RouterHostAdapter.make_router_op_context``.

        #3053-fix2: the no-bridge (root-session) branch resolves the SAME two
        terminals ``SpawnBridgeInterventionListener.bus`` uses for its parent —
        a LIVE listener on this session's own channel → deliver there (an
        interactive TUI/CUI/AGUI chat; every front-end registers on
        ``DEFAULT_CHAT_CHANNEL_ID``); NO listener → a typed, reason'd REFUSAL
        (``AuditOnlyInterventionBridge``), NEVER a stamped bus that origin-pin
        PARKS the iv forever. The park was a latent hang the MCP callers never
        witnessed (a no-listener root chat doesn't exercise a permission prompt),
        but the ``safety.limit`` budget/cap buses newly route here and hit it
        directly: the pre-#3053 direct ``_dispatch_intervention`` path auto-refused
        via ``enforce_listener_presence`` (no channel-id stamp → no origin-pin
        stall). Failing close by construction here restores that, and hardens the
        MCP leaf against the same no-operator hang — one uniform terminal, per the
        delivery rule's "no attached originator → close and answer" clause."""
        if self._intervention_bridge is not None:
            return self._intervention_bridge.bus(run_id=None, actor="chat_router")
        if self.interventions.has_listener(DEFAULT_CHAT_CHANNEL_ID):
            return ChatInterventionBus(
                self, run_id=None, actor="chat_router",
                channel_id=DEFAULT_CHAT_CHANNEL_ID,
            )
        # No bridge AND no live listener → no reachable operator → fail-close.
        return AuditOnlyInterventionBridge().bus(run_id=None, actor="chat_router")

    async def _file_op(self, op_dict: dict) -> dict:
        """Dispatch a file op via op_runtime. Returns result dict."""
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import FileIROp

        op = FileIROp(**op_dict)
        ctx = self._make_router_op_context()
        return await execute_op(op, ctx)

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

    async def _mcp_list_servers(self) -> list[dict]:
        """Returns the configured MCP server list with descriptions."""
        return self._get_mcp_servers_for_router()

    async def _mcp_list_tools(self, server: str) -> list[dict]:
        """Query the MCP server for its tools list."""
        from reyn.core.cancellable import Cancelled
        from reyn.mcp.client import expand_env
        from reyn.mcp.gateway import MCPFault, MCPGateway

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

        # #2421: route through the single MCPGateway seam — it owns the whole crash-safe lifecycle
        # (open + list + teardown inside the contain-all boundary, a task-affine pool, a per-call
        # timeout) and raises ONLY MCPFault, never a bare BaseExceptionGroup. This is the owner's
        # Windows crash path: a server that dies mid-list can no longer escape as an uncontained
        # group.
        # #2597 S2a: a non-ephemeral session routes through its held-open connection service (Option
        # C — no re-handshake on every list call); an ephemeral session keeps the pre-existing
        # one-shot pool (no injected pool — list is not batched across ops), since holding a
        # connection open for a sub-second-lived session is pure churn.
        gateway = (
            MCPGateway(
                pool=self._mcp_connection_service, agent_id=self._agent_id,
                cancel_event=self._loop_driver.cancel_event,
            )
            if not self._ephemeral
            else MCPGateway(agent_id=self._agent_id, cancel_event=self._loop_driver.cancel_event)
        )
        try:
            return await gateway.list_tools(server, expanded)
        except Cancelled:
            return [{"error": "cancelled"}]
        except MCPFault as exc:
            return [{"error": str(exc)}]

    async def _mcp_list_resources(self, server: str) -> list[dict]:
        """Query the MCP server for its resources list.

        #2597 slice ②a: mirrors ``_mcp_list_tools`` exactly — discovery-only,
        NOT permission-gated (no op-kind), routed through the same
        ``MCPGateway`` seam (held connection service on a non-ephemeral
        session, one-shot pool otherwise). Emits ``mcp_resources_listed`` for
        observability (list_tools has no analogous event; this ask is
        explicit per the #2597 ②a slice spec).
        """
        from reyn.core.cancellable import Cancelled
        from reyn.mcp.client import expand_env
        from reyn.mcp.gateway import MCPFault, MCPGateway

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

        gateway = (
            MCPGateway(
                pool=self._mcp_connection_service, agent_id=self._agent_id,
                cancel_event=self._loop_driver.cancel_event,
            )
            if not self._ephemeral
            else MCPGateway(agent_id=self._agent_id, cancel_event=self._loop_driver.cancel_event)
        )
        try:
            resources = await gateway.list_resources(server, expanded)
        except Cancelled:
            return [{"error": "cancelled"}]
        except MCPFault as exc:
            return [{"error": str(exc)}]
        self._chat_events.emit(
            "mcp_resources_listed", server=server, count=len(resources),
        )
        return resources

    async def _mcp_list_resource_templates(self, server: str) -> list[dict]:
        """Query the MCP server for its resource templates list.

        #2597 slice ②a: mirrors ``_mcp_list_resources`` (discovery-only, not
        permission-gated). Empty list is a normal result for a server that
        registers no templates.
        """
        from reyn.core.cancellable import Cancelled
        from reyn.mcp.client import expand_env
        from reyn.mcp.gateway import MCPFault, MCPGateway

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

        gateway = (
            MCPGateway(
                pool=self._mcp_connection_service, agent_id=self._agent_id,
                cancel_event=self._loop_driver.cancel_event,
            )
            if not self._ephemeral
            else MCPGateway(agent_id=self._agent_id, cancel_event=self._loop_driver.cancel_event)
        )
        try:
            return await gateway.list_resource_templates(server, expanded)
        except Cancelled:
            return [{"error": "cancelled"}]
        except MCPFault as exc:
            return [{"error": str(exc)}]

    async def _mcp_read_resource(self, server: str, uri: str) -> dict:
        """Read one MCP resource by URI and return its contents.

        #2597 slice ②a: mirrors ``_mcp_call_tool`` exactly — permission-gated
        (``require_mcp``, same server-scoped axis a tool call uses) + routed
        through ``execute_op`` on the ``mcp_read_resource`` op kind, so the
        SAME connection-service-vs-per-call-pool split ``_mcp_call_tool``
        documents applies here too.
        """
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import MCPReadResourceIROp
        from reyn.security.permissions.permissions import PermissionDecl

        op = MCPReadResourceIROp(kind="mcp_read_resource", server=server, uri=uri)
        ctx = self._make_router_op_context()
        # #3049: bridge-aware — a driver session's MCP permission prompt reaches the
        # pipeline originator instead of orphaning on the driver's own registry.
        ctx.intervention_bus = self._make_router_intervention_bus()
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        if not self._ephemeral:
            ctx.mcp_connection_service = self._mcp_connection_service
            return await execute_op(op, ctx)
        from reyn.mcp.pool import MCPClientPool
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await execute_op(op, ctx)

    async def _mcp_subscribe_resource(self, server: str, uri: str) -> dict:
        """Subscribe to server-pushed ``resources/updated`` for ``uri`` on
        ``server``. #2597 slice ②b: mirrors ``_mcp_read_resource`` — permission-
        gated (``require_mcp``, same server-scoped axis) + routed through
        ``execute_op`` on the ``mcp_subscribe_resource`` op kind.

        Unlike ``_mcp_read_resource``, a subscription is only meaningful on a
        PERSISTENT connection — the subscribed-URI set lives on
        ``MCPConnectionService`` (runtime-only, Q4) and the push notification
        arrives asynchronously, sometime after this call returns. An ephemeral
        session's per-call ``MCPClientPool`` closes the connection before this
        method even returns, so a "successful" subscribe there could never
        actually observe a push — refuse fast with a clear error instead of a
        silently-useless no-op subscription.
        """
        if self._ephemeral:
            return {
                "kind": "mcp_subscribe_resource", "status": "error", "server": server,
                "uri": uri,
                "error": "MCP resource subscriptions require a persistent connection "
                         "(not available in an ephemeral session).",
            }
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import MCPSubscribeResourceIROp
        from reyn.security.permissions.permissions import PermissionDecl

        op = MCPSubscribeResourceIROp(kind="mcp_subscribe_resource", server=server, uri=uri)
        ctx = self._make_router_op_context()
        # #3049: bridge-aware — a driver session's MCP permission prompt reaches the
        # pipeline originator instead of orphaning on the driver's own registry.
        ctx.intervention_bus = self._make_router_intervention_bus()
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        ctx.mcp_connection_service = self._mcp_connection_service
        return await execute_op(op, ctx)

    async def _mcp_unsubscribe_resource(self, server: str, uri: str) -> dict:
        """Unsubscribe from server-pushed updates for ``uri`` on ``server``.
        Mirrors :meth:`_mcp_subscribe_resource` — same persistent-connection
        requirement, same permission gate."""
        if self._ephemeral:
            return {
                "kind": "mcp_unsubscribe_resource", "status": "error", "server": server,
                "uri": uri,
                "error": "MCP resource subscriptions require a persistent connection "
                         "(not available in an ephemeral session).",
            }
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import MCPUnsubscribeResourceIROp
        from reyn.security.permissions.permissions import PermissionDecl

        op = MCPUnsubscribeResourceIROp(kind="mcp_unsubscribe_resource", server=server, uri=uri)
        ctx = self._make_router_op_context()
        # #3049: bridge-aware — a driver session's MCP permission prompt reaches the
        # pipeline originator instead of orphaning on the driver's own registry.
        ctx.intervention_bus = self._make_router_intervention_bus()
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        ctx.mcp_connection_service = self._mcp_connection_service
        return await execute_op(op, ctx)

    async def _mcp_list_prompts(self, server: str) -> list[dict]:
        """Query the MCP server for its prompts list.

        #2597 slice ②c: mirrors ``_mcp_list_resources`` exactly — discovery-only,
        NOT permission-gated (no op-kind), routed through the same
        ``MCPGateway`` seam (held connection service on a non-ephemeral
        session, one-shot pool otherwise). Emits ``mcp_prompts_listed`` for
        observability, same rationale as ``mcp_resources_listed``.
        """
        from reyn.core.cancellable import Cancelled
        from reyn.mcp.client import expand_env
        from reyn.mcp.gateway import MCPFault, MCPGateway

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

        gateway = (
            MCPGateway(
                pool=self._mcp_connection_service, agent_id=self._agent_id,
                cancel_event=self._loop_driver.cancel_event,
            )
            if not self._ephemeral
            else MCPGateway(agent_id=self._agent_id, cancel_event=self._loop_driver.cancel_event)
        )
        try:
            prompts = await gateway.list_prompts(server, expanded)
        except Cancelled:
            return [{"error": "cancelled"}]
        except MCPFault as exc:
            return [{"error": str(exc)}]
        self._chat_events.emit(
            "mcp_prompts_listed", server=server, count=len(prompts),
        )
        return prompts

    async def _mcp_get_prompt(self, server: str, name: str, arguments: "dict | None" = None) -> dict:
        """Fetch one rendered MCP prompt by name and return its messages.

        #2597 slice ②c: mirrors ``_mcp_read_resource`` exactly — permission-gated
        (``require_mcp``, same server-scoped axis a tool call / resource read
        uses) + routed through ``execute_op`` on the ``mcp_get_prompt`` op kind,
        so the SAME connection-service-vs-per-call-pool split ``_mcp_call_tool``
        documents applies here too.
        """
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import MCPGetPromptIROp
        from reyn.security.permissions.permissions import PermissionDecl

        op = MCPGetPromptIROp(
            kind="mcp_get_prompt", server=server, name=name, arguments=dict(arguments or {}),
        )
        ctx = self._make_router_op_context()
        # #3049: bridge-aware — a driver session's MCP permission prompt reaches the
        # pipeline originator instead of orphaning on the driver's own registry.
        ctx.intervention_bus = self._make_router_intervention_bus()
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        if not self._ephemeral:
            ctx.mcp_connection_service = self._mcp_connection_service
            return await execute_op(op, ctx)
        from reyn.mcp.pool import MCPClientPool
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await execute_op(op, ctx)

    async def _mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        """Invoke an MCP tool and return its result.

        #2597 S2a: a non-ephemeral session routes through its session-owned
        ``MCPConnectionService`` (Option C) — the connection is opened ONCE and held
        open for the rest of the session's lifetime (reused across chat turns/tasks;
        the S2-pre spike proved this is cross-task-safe for a FastMCP client), closed
        only at session teardown (``aclose_mcp_connections``, wired from
        ``registry.remove_session`` / the main-session archive path).

        An ephemeral session (``self._ephemeral``, set post-construction by the
        registry) keeps the PRE-#2597 per-call ``MCPClientPool`` path below: close
        the per-call MCP clients in the same task that opened them — the MCP SDK's
        ``stdio_client`` uses anyio cancel scopes that are task-affine, and leaving
        them open until asyncio loop teardown produces a "cancel scope crossed task
        boundary" RuntimeError (= recurring crash on every chat session end observed
        during the 2026-05-20 8-server smoke round). Holding a connection open for a
        sub-second-lived ephemeral session is pure churn (F4 decision), so it keeps
        opening + closing fresh per call.
        """
        from reyn.core.op_runtime import execute_op
        from reyn.schemas.models import MCPIROp
        from reyn.security.permissions.permissions import PermissionDecl

        op = MCPIROp(kind="mcp", server=server, tool=tool, args=args)
        ctx = self._make_router_op_context()
        # MCP handler requires intervention_bus; wire the session's bus
        # #3049: bridge-aware — a driver session's MCP permission prompt reaches the
        # pipeline originator instead of orphaning on the driver's own registry.
        ctx.intervention_bus = self._make_router_intervention_bus()
        # Narrow mcp scope to just this server while preserving file perms from the
        # populated decl. PermissionDecl.mcp must include the server for require_mcp to pass.
        ctx.permission_decl = PermissionDecl(
            file_read=ctx.permission_decl.file_read,
            file_write=ctx.permission_decl.file_write,
            mcp=[server],
        )
        if not self._ephemeral:
            ctx.mcp_connection_service = self._mcp_connection_service
            return await execute_op(op, ctx)
        # #a359 P2: a per-call structured pool — the client opens (pool.get in the op handler) AND
        # closes (pool __aexit__) in THIS task, and teardown faults (incl. BaseExceptionGroup) are
        # contained. Replaces the manual finally-close over ``ctx.mcp_clients`` (which closed a
        # client whose SDK task-group scope could have been entered lazily elsewhere).
        from reyn.mcp.pool import MCPClientPool
        async with MCPClientPool() as pool:
            ctx.mcp_pool = pool
            return await execute_op(op, ctx)

    def fs_watcher_is_started(self) -> bool:
        """Read-only introspection: whether this session's filesystem watcher
        (#2608 H4) is currently running (``False`` when no ``fs_watch.paths``
        were configured, or ``watchdog`` isn't installed, or ``start()``
        hasn't run yet). Public surface for callers/tests to observe lifecycle
        state without reaching into ``_fs_watcher`` directly."""
        return self._fs_watcher.is_started()

    async def aclose_fs_watcher(self) -> None:
        """#2608 H4 teardown: stop this session's filesystem watcher (join the
        observer thread). Idempotent (``FsWatcher.aclose`` is idempotent).
        ``run()`` already calls this in its own ``finally`` (session_end
        scope); exposed publicly for a caller/test that tears a session down
        without going through ``run()``."""
        await self._fs_watcher.aclose()

    def mcp_held_servers(self) -> list[str]:
        """Read-only introspection: names of MCP servers with a currently held-open
        connection (#2597 S2a). Always ``[]`` for an ephemeral session (never
        populates the connection service — see ``_mcp_call_tool``). Public surface
        for callers/tests to observe connection-reuse/teardown without reaching into
        ``_mcp_connection_service`` directly."""
        return self._mcp_connection_service.held_servers()

    async def aclose_mcp_connections(self) -> None:
        """#2597 S2a teardown: close every held MCP connection this session opened.

        Idempotent (``MCPConnectionService.aclose`` is idempotent). Called from the
        registry's session-teardown seams (``remove_session`` for a spawned session;
        ``archive_agent`` for the main session) — no new lifecycle owner, rides the
        existing quiesce-then-teardown seam. Ephemeral sessions never populate the
        service (they route MCP calls through the one-shot pool instead), so this is
        a no-op for them.
        """
        await self._mcp_connection_service.aclose()

    async def aclose_event_store(self) -> None:
        """#2783 teardown: drain this session's EventStore before the process exits.

        Idempotent (``EventStore.aclose`` is idempotent — see #2780). Without this,
        a normal ``/quit`` can drop the trailing audit events (e.g. the very
        ``session_completed``/``turn_completed`` records describing the graceful
        exit) because ``asyncio.run`` cancels outstanding tasks at loop teardown
        and ``EventStore.write`` enqueues via ``submit_nowait`` (fire-and-forget).
        Called from the registry's session-teardown seams alongside
        ``aclose_mcp_connections``/``aclose_fs_watcher`` — same pattern, same
        call sites.
        """
        await self._event_store.aclose()

    # --- RouterLoop orchestration ---

    def _cap_tool_result(self, content_str: str, *, content_type: "str | None" = None) -> str:
        """Forwarding → ContextBudgetAdvisor.cap_tool_result (PR-1).

        #2425 案B: the router chokepoint caps the canonical ``text`` body (already the clean payload),
        so the capper takes a single string — no clean-payload kwargs. ``content_type`` (#2663) is the
        canonical's renderer-only sidecar, forwarded so an offloaded ref's on-disk extension carries it
        for present's stage-3 default viewer — never read into any LLM-visible field here."""
        return self._budget_advisor.cap_tool_result(content_str, content_type=content_type)

    def _media_followup_budget(self, tool_content: str) -> "int | None":
        """Forwarding → ContextBudgetAdvisor.media_followup_budget (PR-1)."""
        return self._budget_advisor.media_followup_budget(tool_content)

    def context_window_status(self) -> dict:
        """Forwarding → ContextBudgetAdvisor.context_window_status (PR-1).

        Public — read by both the RouterHostAdapter SP context-size signal
        (via the callback wired at __init__) and the inline UI's ctx chip
        dropdown (status bar reads only public accessors, see
        interfaces/inline/app.py's module docstring).

        Cost is proportional to the CONVERSATION, not to the WAL. Three layers
        got it there, and each is load-bearing: #2951 caches the advisor's own
        json.dumps + token-estimate of the router-view history (re-paid only on
        a miss — history shrink, model/use_chars4 change, changed cached
        prefix); #2939 made ``build_history`` materialise its producer
        (``_active_branch_history``) ONCE instead of 2x (3x on the elide path,
        via each ``_latest_summary``); and #2939 made that producer's
        ``build_active_predicate`` derivation incremental, so it decodes only
        WAL entries appended since the previous turn rather than re-scanning
        every line. Measured (N=2000 msgs, warm token cache, Darwin/arm64):
        ~2.5ms per call, flat from M=5k to M=100k WAL entries — where before
        #2939 the same open cost ~20ms at M=5k rising to ~341ms at M=100k
        (#2940 measured ~445ms at M=100k on the same shape, ~99.7% of it in
        that scan).

        Still not free, and still not a per-render-frame call: it walks and
        serialises the whole conversation, so it scales with history length
        (N). The ctx chip's own denominator should use ``raw_context_window``
        below, which is O(1)."""
        return self._budget_advisor.context_window_status()

    def raw_context_window(self) -> dict:
        """Forwarding → ContextBudgetAdvisor.raw_context_window (status-bar ctx
        chip's real "distance to the model's hard limit" denominator). Public,
        and cheap (a dict lookup) — safe to call every render frame, unlike
        ``context_window_status`` above."""
        return self._budget_advisor.raw_context_window()

    async def _compact_now_for_op(self) -> dict:
        """#272/#1128/#191: voluntary-compaction callback (compact op + /compact).

        Runs the existing synchronous compaction and reports what it did.

        Axis note (#191, traced): the CHAT router prompt is head+tail TURN-COUNT
        bounded (``_build_history_for_router``), so the router-view
        ``freed_tokens`` is structurally ~0 even when compaction fires — chat
        compaction COMPRESSES the already-elided middle into a summary bridge
        rather than shrinking the bounded view. So the meaningful chat metric is
        ``summarized_turns`` + ``compressed_tokens`` (raw middle) → ``bridge_tokens``
        (the summary). ``freed_tokens`` is kept for the op contract shared with
        the phase axis (where it IS the real control_ir shrink), but is ~0 for
        chat — callers front the compression numbers, not freed, for chat.
        """
        import json as _json

        from reyn.services.compaction.engine import estimate_tokens

        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)

        def _cover() -> int:
            s = self._latest_summary()
            return int((s.meta or {}).get("covers_through_seq", 0)) if s is not None else 0

        def _est(text: str) -> int:
            try:
                return estimate_tokens(text, self.model, use_chars4=use_chars4)
            except Exception:  # noqa: BLE001 — estimation best-effort
                return 0

        effective_trigger, before = self._budget_advisor._free_window_now()
        prev_cover = _cover()
        await self._compaction_controller.force_compact_now()
        _, after = self._budget_advisor._free_window_now()
        new_cover = _cover()

        # Chat middle-compression: the conversational turns newly covered by the
        # summary bridge (prev_cover < seq <= new_cover) and their raw vs bridge
        # token cost. Empty when nothing was compacted (new_cover == prev_cover).
        conv = [m for m in self.history if m.role in ("user", "assistant", "tool", "agent")]
        middle = [m for m in conv if prev_cover < int(getattr(m, "seq", 0) or 0) <= new_cover]
        summary = self._latest_summary()
        bridge_text = summary.text if summary is not None else ""
        if not isinstance(bridge_text, str):
            bridge_text = _json.dumps(bridge_text, ensure_ascii=False)
        return {
            "freed_tokens": max(0, before - after),
            "free_window_after": max(0, effective_trigger - after),
            "free_window_before": max(0, effective_trigger - before),
            # #191 chat-axis compression metric (the meaningful chat signal):
            "summarized_turns": len(middle),
            "compressed_tokens": sum(_est(m.text) for m in middle),
            "bridge_tokens": _est(bridge_text) if summary is not None else 0,
        }

    def reasoning_continuity_section(self) -> str:
        """#1652/②: RETIRED — always ``""``.

        Cross-turn reasoning continuity now rides the wire assistant messages
        natively (RouterHistoryBuffer re-attaches the captured reasoning bundle
        — reasoning_content / thinking_blocks — bounded to ``recent_turns``),
        instead of a re-rendered text section at the router system-prompt tail.
        Moving it off the SP makes the SP byte-stable turn-to-turn → the long
        SP+tools prefix stays cacheable (the #1652/② cache win on capable-model
        tiers). Returning ``""`` keeps the SP omit-when-empty shape unchanged.
        """
        return ""

    async def _run_router_loop(
        self,
        user_text: str,
        chain_id: str,
    ) -> None:
        """Forwarding → RouterLoopDriver.run_turn (PR-3)."""
        await self._loop_driver.run_turn(user_text, chain_id)
        # #1800 slice 5a: turn lifecycle audit event (P6). Emitted immediately
        # after RouterLoopDriver.run_turn() returns — the router loop has
        # reached a terminal stop_reason and the turn's response is complete.
        # This is the hook point for the turn_end lifecycle hook (slice 5b).
        # Emitted here (not inside RouterLoop) so it fires exactly once per
        # turn independent of which terminal path the loop took, and so the
        # chain_id (known to _run_router_loop) is in scope.
        self._chat_events.emit("turn_completed", chain_id=chain_id)
        # #1800 slice 5b: turn_end lifecycle hooks. E (wake=true) self-continuation
        # fires here — the hook pushes a wake=true trigger that the next
        # run_one_iteration drains as a new turn (bounded by the slice-7 valve).
        # C (stage) / F (shell) also fire.
        await self._hook_dispatcher.dispatch(
            "turn_end",
            build_hook_payload(
                "turn_end", agent_name=self.agent_name,
                chain_id=chain_id, user_text=user_text,
            ),
        )
        # #2073 S1: config hot-reload safe-point (Timing-B). A reload scheduled
        # during/before this turn applies HERE — at the turn boundary
        # (finish-reason=stop), never mid-turn — so the next turn runs under the new
        # IN-set config (1 turn = 1 config snapshot). No-op (returns None) when no
        # reload is pending → zero overhead on the happy path.
        await self._hot_reloader.apply_pending()
        # ADR-0038 Stage 1a: turn boundary = a user-facing checkpoint. #1547: the
        # user message is this checkpoint's anchor for the rewind-timeline preview.
        # #1533 2c: the FULL message is persisted alongside (edit-prefill source).
        await self._journal.cut_generation(
            anchor=_truncate_anchor(user_text), full_message=user_text,
        )
