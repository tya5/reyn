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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from reyn.interfaces.slash import SlashContext

logger = logging.getLogger(__name__)
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reyn.config import (  # noqa: F401
    ActionRetrievalConfig,
    AuditEventsConfig,
    CostWarnConfig,
    EmbeddingConfig,
    HistoryResidentConfig,
    MultimodalConfig,
    OffloadConfig,
    OnLimitConfig,
    ReadCapConfig,
    RenderTemplateConfig,
    RouterConfig,
    SafetyConfig,
    WebFetchConfig,
)
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import truncate_anchor as _truncate_anchor
from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.status_classify import classify_op_status
from reyn.core.pipeline.registry import PipelineNotFoundError, PipelineRegistry
from reyn.core.turn_scope import active_turn
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
from reyn.runtime.errors import AgentStepError, RouterCapExceeded, StructuredOutputError
from reyn.runtime.inbox_arbiter import InboxArbiter
from reyn.runtime.limits.limit_handler import (
    LimitDecision,
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import OutboxHub
from reyn.runtime.pending_op_view import PendingOpView
from reyn.runtime.presentation_consumer import OutboxPresentationConsumer
from reyn.runtime.router_op_context import RouterOpContextSource
from reyn.runtime.services import (
    BudgetGateway,
    ChainManager,
    CompactionController,
    InterventionCoordinator,
    InterventionHandler,
    InterventionRegistry,
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryKnowledgeSync,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
    SnapshotJournal,
)
from reyn.runtime.services.chain_manager import _PendingChain
from reyn.runtime.services.execution_driver import ExecutionDriver
from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
from reyn.runtime.services.recovery import default_snapshot_path
from reyn.runtime.session_buses import (
    AgentRequestBus,
    AuditOnlyInterventionBridge,
    ChatInterventionBus,
)
from reyn.runtime.session_params import (
    CapabilityScope,
    PresentationWiring,
    ReactivityConfig,
)
from reyn.runtime.session_pure import (
    merge_memory_indexes,
    new_chain_id,
    render_summary_for_storage,
)
from reyn.runtime.spawn_tracker import SpawnTracker
from reyn.runtime.task_types import CurrentTask
from reyn.runtime.turn_origin import TurnOrigin
from reyn.security.permissions.permissions import PermissionResolver
from reyn.services.compaction.engine import CompactionEngine
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

# #2103 S1bc-exec: spawned-task correlation cap now lives in spawn_tracker.py's
# _MAX_SPAWNED_TASKS — see docs/reference/runtime/session-construction.md#capability-permission-visibility

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


# #268: canonical chat-channel id every interactive front-end registers on —
# see docs/concepts/runtime/intervention-delivery.md#the-single-construction-seam
DEFAULT_CHAT_CHANNEL_ID = "tui"

# #4387 Phase B ①: the minimum number of lines ``load_history``'s bounded
# path reads even when the latest compaction watermark is very close to
# EOF — a reasonable startup scrollback floor so a freshly-compacted
# session doesn't hydrate to a near-empty view. Mirrors #3476④'s own
# ``_HYDRATE_PAGE_FRAMES=200`` (the TUI's view-layer paging window).
_HISTORY_HYDRATE_MIN_LINES = 200


# EMPTY_STOP_RETRY_DIRECTIVE (router_loop.py) is imported function-locally at the
# construction site below: router_loop imports from session, so a module-level
# import here would create a circular import (#187 B43-NF-W6-1).


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


def _deepest_cause(exc: BaseException) -> "BaseException | None":
    """#4381 stage 1 (B): the DEEPEST ``__cause__`` in *exc*'s chain, or
    ``None`` if *exc* has no ``__cause__`` at all (it IS the root — the
    common case for most exceptions, which never wrap another).

    A pure helper (no session state) so the "reyn's own wrapper type is
    not what actually happened" gap can be tested directly, without
    driving a full turn through ``_run_router_loop``'s except block. See
    that call site's own comment for the concrete motivating case
    (``ContextOverflowError`` wrapping the real ``APIError`` that never
    reached ``reyn.log``)."""
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    return root if root is not exc else None


def _extract_tool_call_records(
    messages: "list[ChatMessage]",
) -> list[tuple[str, float]]:
    """Extract ``(action_name, ts_epoch)`` tuples from a list of
    ``ChatMessage`` instances.

    Recognises two emission shapes (mirrors ``router_loop`` recording
    semantics pre-refactor):

      - ``invoke_action`` tool call → ``args["action_name"]`` is the
        action name; the ``args`` payload may be a JSON string per
        the OpenAI wire shape.
      - Any other tool call → ``function.name`` itself (a hot-list
        alias or a universal wrapper). Non-action names like
        ``list_actions`` are caught by the tracker's
        ``_is_tracked_action_name`` filter and dropped.

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


# FP-0041 (#489) PR-A: humanic dispatch attribution helper — moved to
# ``reyn.runtime.inbox_arbiter`` (proposal 0067 P1, #3978), the only caller
# (``InboxArbiter.handle_sender_attribution``, née
# ``Session._handle_sender_attribution``).


def _format_config_reloaded(data: dict) -> str:
    """#3636: ``config_reloaded``'s summary formatter — a callable (not a plain
    ``str.format`` template) because ``detail`` is OPTIONAL (present only when the
    triggering install call supplied a single-entity qualifier; see
    ``hot_reload.HotReloader.apply_now``'s docstring). Two DIFFERENT installs of the
    same ``source`` kind (e.g. a plugin bundling two pipelines) each emit their own
    correct ``config_reloaded``; without this qualifier both render as the byte-
    identical "Reyn configuration was hot-reloaded (source: pipeline_install)." —
    an adjacent-duplicate-shaped artifact of lost resolution, not an actual
    double-write (#3636's investigation traced the owner's real events log: two
    distinct ``pipeline_installed`` events, ``rag_ingest`` then ``rag_query``, each
    with its own ``config_reloaded``)."""
    source = data.get("source", "unknown")
    detail = data.get("detail")
    if detail:
        return f"Reyn configuration was hot-reloaded (source: {source}: {detail})."
    return f"Reyn configuration was hot-reloaded (source: {source})."


# #398 v4 emitter family: op-emitted-event → state_change dispatch table.
# See docs/reference/runtime/session-construction.md#misc-lifecycle-wiring
# (mechanism + sister-mechanism note); per-entry rationale below. The template slot
# is normally a ``str.format``-compatible string (receives ``event.data`` as
# kwargs); ``config_reloaded`` uses a callable instead because its ``detail`` field
# is optional (#3636) — a plain template would ``KeyError`` (silently skipped) on
# any emit site that didn't supply it.
_STATE_CHANGE_EVENT_MAPPINGS: dict[str, "tuple[str, str | Callable[[dict], str]]"] = {
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
    # Config hot-reload (#2073) — see docs/concepts/runtime/config-hot-reload.md#p6-event.
    # Formatter is a callable, not a template string — see _format_config_reloaded (#3636).
    "config_reloaded": (
        "config_watcher",
        _format_config_reloaded,
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


def _format_ride_along_attribution(kind: str, name: str, text: str) -> str:
    """Render an attributed system-role push message: ``[<kind>:<name>] <text>``.

    #1800 slice 5b originally: the single source for the ``[hook:<name>]``
    prefix, shared by the staged-context consumer (C — wake=false
    ride-along) and ``_handle_hook_message`` (E — wake=true trigger) so the
    two paths can never drift. ``_handle_hook_message`` still always passes
    ``kind="hook"`` (a hook push, by construction, at that call site) —
    byte-identical output for that path.

    Proposal 0067 P5 (#3978, architect + lead-coder co-vet): generalized to
    take ``kind`` as an explicit parameter rather than hardcoding
    ``"hook"``. The staged-context flush (``_run_router_loop``) used to call
    this with only ``name`` and ``text``, discarding the entry's own
    ``kind`` as a mere fallback DEFAULT for ``name`` — so ANY staged
    producer (a future ``send_to_session`` wake=false ride-along included)
    rendered as ``[hook:...]`` regardless of what it actually was: a false
    attribution the LLM reads as fact. The fix follows the same TRUSTED-
    framing discipline ``InterAgentMessaging.handle_agent_response`` already
    uses for its ``[task_completed] kind=...`` header — the LABEL is
    OS-assigned from the entry's own recorded ``kind`` (trusted: staged by
    ``InboxArbiter.stage_next_turn_context`` from the inbox item's own
    ``TurnOrigin``, never echoed from producer-supplied content), never
    downgraded to a producer-supplied default. Only the ``text`` itself
    stays whatever content the producer supplied — the label doesn't.
    """
    return f"[{kind}:{name}] {text}"


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



class DurabilityHaltError(RuntimeError):
    """#2259 PR-3: raised when an operation is submitted to an agent whose durability has FAILED
    persistently (a §4-retry-exhausted fire-and-forget durable write — disk full / dead). The agent
    has FAIL-STOPPED: it no longer accepts operations, because in-memory state must not race ahead
    of a dead disk (the owner's "no silent unbounded loss"). The raise IS the operator-surface — the
    caller sees it synchronously on their next op, not only a CRITICAL log they would scroll past."""


@dataclass(frozen=True)
class _AuditEventBundle:
    """#3082 Family 1: the audit-event spine (P6) — ``event_store`` (disk-backed
    log) → ``audit_events`` (the ``EventLog`` nearly every other Session
    sub-component consumes) → ``outbox_hub`` (the outbox fan-out), plus the
    opt-in OTEL subscriber attached to ``audit_events``. Pure output→input
    value object: :meth:`Session._build_audit_event_bundle` is a byte-identical
    extraction of the construction sequence that used to run inline in
    ``Session.__init__`` — same objects, same order, same args. This is the
    FIRST family built (the spine); later families' builders take its fields
    as explicit inputs instead of reaching into ``self`` mid-construction."""

    event_store: EventStore
    audit_events: EventLog
    outbox_hub: OutboxHub
    otel_exporter: "object | None"


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
    ``self._hook_dispatcher`` / ``self._audit_events`` at call time, exactly as
    before). This family CONSUMES Family 1's ``audit_events`` (the
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
    (``embedding_provider`` / ``embedding_model_class`` /
    ``action_embedding_index``, three attrs, one conditional construction
    guarded by ``universal_wrappers_enabled and embedding.enabled`` (FP-0066
    §7) with a
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

    Pure output→input value object. #3408 moved the call site from its
    original pre-Family-1 position to run right AFTER Family 1
    (``_build_audit_event_bundle``), so this family's closure
    (``_on_hot_list_changed``) now takes ``audit_events`` as an eager builder
    input — like ``hot_reloader`` (Family 3) and ``budget`` (Family 4) — and
    binds it by IDENTITY instead of resolving ``self._audit_events`` by NAME
    at call time. Before #3408, the builder ran BEFORE Family 1 (so
    ``self._audit_events`` did not exist yet at its old call site) and the
    closure deferred the name lookup to call time to avoid an
    ``AttributeError``; #3408 measured that nothing between the old and new
    call sites reads or writes this family's own attrs, moved the call site
    instead, and closed the deferral. See
    :meth:`Session._build_retrieval_bundle`'s docstring for the full
    identity-vs-name rationale and the AST guard that keeps it true.

    #3438: the fourth attr, ``embedding_event_sink`` (a TUI model-download
    status sink, FP-0057 #2856 Part A), was removed along with its whole
    seven-hop wire (Session → OpContext → the `embed` op → provider). It had
    no producer: ``get_provider`` only forwards an ``event_sink`` kwarg to a
    provider class whose signature accepts it, and the sole implementation
    (``LiteLLMEmbeddingProvider``) never did. Its original reason for being —
    reporting a local in-process embedding model's lazy-load lifecycle
    (FP-0043 Component C.3) — stopped applying when #3128 removed that
    in-process backend; local/offline embedding now goes through an
    operator-run litellm proxy (see
    docs/concepts/data-retrieval/rag.md#local-and-offline-embedding-models),
    which reyn does not manage a download lifecycle for. No comment/ADR/issue
    recorded an intent to keep the wire for a future provider, so this was
    dead wiring, not a deliberate placeholder."""

    embedding_provider: "object | None"
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
        builder runs): kept as ``self._X`` — ``self._audit_events``
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
    ``self._audit_events`` [Family 1], plus a handful of already-set bound
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
    ``self._journal`` (Family 2), ``self._audit_events`` (Family 1),
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


# #3193: signal fields a file op result may carry ALONGSIDE its content when
# the op is incomplete-but-not-a-failure (a truncated read, a max_results-
# capped glob). Every `Session._file_*` wrapper forwards whichever of these
# are present, untouched, instead of the pre-#3193 behavior of collapsing
# any non-"ok" status into a content-discarding `{"error": ...}`.
_FILE_OP_SIGNAL_KEYS = (
    "truncated", "note", "next_offset", "next_char_offset",
    "shown_lines", "total_lines", "total_chars",
    "total_count", "returned_count",
)


def _forward_file_signal_fields(dest: dict, result: dict, *, outcome: str) -> None:
    """Copy any #3193 signal fields present on *result* onto *dest*, and — for
    ``outcome == "unknown"`` (a status `classify_op_status` does not
    recognize) — tag the output so the unrecognized status is OBSERVABLE
    rather than silently treated as either a clean success or a failure.
    See `reyn.core.op_runtime.status_classify` module docstring for the
    full policy rationale.

    ``outcome == "partial"`` always sets ``dest["truncated"] = True``
    regardless of which internal key the op_runtime handler used to signal
    it — the ``read`` op marks it via ``status == "truncated"`` (+ a
    private ``_truncated`` key), while ``glob`` keeps ``status == "ok"``
    and adds a sibling public ``truncated`` key. Wrapper callers get ONE
    consistent externally-visible field either way.
    """
    if outcome == "partial":
        dest["truncated"] = True
    for key in _FILE_OP_SIGNAL_KEYS:
        if key in result:
            dest[key] = result[key]
    if outcome == "unknown":
        status = result.get("status")
        dest["_unknown_op_status"] = status
        logger.warning(
            "file op result carried unrecognized status %r (kind=%r op=%r) — "
            "forwarding content best-effort; add this status to "
            "reyn.core.op_runtime.status_classify (#3193)",
            status, result.get("kind"), result.get("op"),
        )


# #3410: the four ``_mcp_list_*`` discovery methods all emit. The asymmetry
# #3082 recorded here — ``list_tools`` / ``list_resource_templates``
# suppressed, ``list_resources`` / ``list_prompts`` emitting — was settled by
# removing it, not by writing down a reason for it:
#
#   - The #3082 registry it replaces stated plainly that NO record existed of
#     why the two silent paths were built silent. Code cannot tell "forgotten"
#     from "decided"; the registry made the absence enumerable but could not
#     turn it into a decision. #3410 closes the audit-event kind vocabulary,
#     which makes every kind a public-API member — and "two of four sibling
#     discovery calls are invisible to an external consumer, for no recorded
#     reason" is not a vocabulary a consumer can reason about.
#   - The seam's own rule already argued this way: emitting is the recoverable
#     direction, because a missed event is invisible after the fact and a
#     spurious one is not.
#
# The event kind is now passed as a LITERAL by each of the four call sites
# rather than built as ``f"mcp_{noun}_listed"``. That is not cosmetic: an
# f-string kind is invisible to the #3410 vocabulary gate (which censuses
# string-constant emit arguments by AST), so the seam was the one production
# site that could mint an undeclared kind without any gate seeing it.


class Session:
    def __init__(
        self,
        # Identity value object (single source of truth) — see
        # docs/reference/runtime/session-construction.md#identity-the-agent-value-object-fp-0043-stage-2
        agent: "Agent",
        # WAL-event/recovery pair (generation_store -> journal), built by the
        # caller via ``reyn.runtime.services.recovery.build_recovery`` instead
        # of by Session itself (recovery-bundle-out-of-Session refactor,
        # follow-up to #3082 Family 2's inline extraction). REQUIRED: no
        # default construction happens here.
        # See docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal
        generation_store: "SnapshotGenerationStore",
        journal: "SnapshotJournal",
        resolver: ModelResolver | None = None,
        safety: "SafetyConfig | None" = None,
        mcp_servers: dict | None = None,
        output_language: str | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        # #3787: the resolved AGENTS.md/REYN.md path this session's ``project_context``
        # was read from (``None`` when disabled/absent) — read-only turn-boundary edit
        # detection ONLY; see ``ProjectContextWatcher``'s module docstring for why this
        # is not wired through the #2073 hot-reload IN-set.
        project_context_path: "Path | None" = None,
        compaction_config: "CompactionConfig | None" = None,
        reasoning_config: "ReasoningConfig | None" = None,  # #1652 chat.reasoning
        registry: "AgentRegistry | None" = None,
        allowed_mcp: list[str] | None = None,
        events_config: AuditEventsConfig | None = None,
        # cost_warn config (#2230) — see docs/reference/runtime/session-construction.md#family-4-cost-budget
        cost_warn_config: CostWarnConfig | None = None,
        # Debug lever disabling tool-result size gates (see session-construction.md#family-4-cost-budget)
        offload_config: OffloadConfig | None = None,
        # render_template output bounds (FP-0055 / #2679) — see docs/reference/runtime/session-construction.md#family-4-cost-budget
        render_template_config: RenderTemplateConfig | None = None,
        # #4381 PR-5: the resource-bound per-result inline cap (file.py read op +
        # load_skill.py). None → context_builder's own model-independent default.
        read_cap_config: "ReadCapConfig | None" = None,
        # #4387 Phase B ③: the resource-bound cap on self.history's resident
        # footprint (bytes). None → HistoryResidentConfig's own default (256 MiB).
        history_resident_config: "HistoryResidentConfig | None" = None,
        state_log: StateLog | None = None,
        budget_tracker: BudgetTracker | None = None,
        snapshot_path: "Path | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        # #4274: reyn.yaml web_fetch.* → the chat-router OpContext's web_fetch_config
        # (verify_ssl / allow_private_ips / max_download_bytes). Plain value, same
        # shape as multimodal_config — not a per-turn supplier.
        web_fetch_config: "WebFetchConfig | None" = None,
        action_retrieval_config: "ActionRetrievalConfig | None" = None,
        # Chat-layer tool-use scheme name, threaded to RouterLoop (#1593 PR-2, default per #1657)
        chat_tool_use_scheme: str = "enumerate-all",
        embedding_config: "EmbeddingConfig | None" = None,
        eager_embedding_build: bool = False,
        # Resolved observability config -> opt-in OTLP export gate (P5 ADR-0039, see docs/reference/runtime/session-construction.md#family-1-audit-event-spine-p6)
        observability_config: "object | None" = None,
        # reyn.yaml llm.router.* ambient ContextVar (#1829 S3b)
        router_config: "RouterConfig | None" = None,
        retry_config: "object | None" = None,  # #1835: reyn.yaml llm.retry.* timing config
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
        skill_collisions = capability_scope.skill_collisions
        presentation_registry = presentation_wiring.presentation_registry
        presentation_consumer = presentation_wiring.presentation_consumer
        intervention_bridge = presentation_wiring.intervention_bridge
        # Identity cluster owned by Agent — single source of truth, no fallback
        # construction (#3133 Priority-0 step-2, see docs/reference/runtime/session-construction.md#identity-the-agent-value-object-fp-0043-stage-2).
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
        # Present-sink consumer; production always supplies one, direct/test construction falls back to outbox-backed default (#2708 P1, see session-construction.md#misc-lifecycle-wiring)
        self._presentation_consumer = (
            presentation_consumer
            if presentation_consumer is not None
            else OutboxPresentationConsumer()
        )
        # Spawn-time intervention bridge; binds an attached driver's ask_user to the parent's listener (#2708 P3.2a, see session-construction.md#misc-lifecycle-wiring)
        self._intervention_bridge = intervention_bridge
        # OS-authoritative provenance classification of the current turn, stamps entry["provenance"] (proposal 0060 Phase1 A7, see session-construction.md#safety-limits-interactive-mode)
        self._current_turn_origin: str = "auto_improvement"
        # Spawned EPHEMERAL flag, set post-construction by the registry (#2103, see session-construction.md#safety-limits-interactive-mode).
        # The vanish-scheduling state (_vanish_scheduled / _vanish_task) is owned by
        # SpawnTracker, constructed below (see #3133 P3 Extract Class, spawn_tracker.py).
        self._ephemeral: bool = False
        # #4193 ①: whether the caller that spawned this session is itself waiting
        # on it — a SEPARATE axis from ``_ephemeral`` above, set post-construction
        # by ``AgentRegistry.spawn_session_recorded`` from an explicit CALLER
        # decision, never derived from ``mode``. True by default: the common case
        # (an interactive chat session, direct/test construction) has someone
        # waiting. See ``OpContext.attended``'s own docstring for the full
        # 3-state table this feeds.
        self._attended: bool = True
        # Lazily-resolved minimal _untrusted profile cache (#1827 S4b, see docs/reference/runtime/session-construction.md#capability-permission-visibility)
        self._untrusted_contextual_cache = None
        # #4381 PR-2 stage ③: per-turn in-flight taint latch — set the moment
        # router_loop.py stamps `external_source` on a tool-result's meta
        # (RouterHostAdapter.mark_untrusted_in_flight, same update point, no
        # second signal to drift), reset at each turn's start
        # (`_run_router_loop`). Closes the gap `_ephemeral_contextual_for_turn`'s
        # `self.history` scan alone cannot see: a same-turn tool result whose
        # history entry has not yet landed (relevant once #4381's later PR
        # defers that commit to after the turn's response). Monotonic for the
        # turn's own remaining iterations, matching the existing
        # `_untrusted_latched` per-iteration latch's own "narrows, never
        # un-narrows mid-turn" discipline.
        self._in_flight_untrusted_this_turn: bool = False
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
        # #4274: stored so RouterOpContextSource can thread it into OpContext.web_fetch_config.
        self._web_fetch_config = web_fetch_config
        # #4381 PR-5: threaded into RouterHistoryBuffer (read_cap=) for the
        # resource/budget invariant check, and into OpContext.read_cap_config
        # for file.py's/load_skill.py's own read op (via RouterOpContextSource).
        self._read_cap_config = read_cap_config
        # #4387 Phase B ③: bounds self.history's resident footprint —
        # consulted by _append_history's eviction hook (below).
        self._history_resident_config = history_resident_config or HistoryResidentConfig()
        # #4468 security block (lead-coder review): the highest seq of any
        # EVICTED entry that carried the untrusted-content marker
        # (security.permissions.capability_profile.UNTRUSTED_META_KEY) —
        # an OR-latch mirroring #4381 PR-2's in-flight latch, so
        # _ephemeral_contextual_for_turn's narrowing scan (which only sees
        # RESIDENT entries) doesn't silently lose an untrusted-taint signal
        # to eviction. Monotone: only ever increases via
        # _evict_oldest_resident_entries; self-clears the same way the
        # resident scan does (the OR term drops out once the compaction
        # watermark advances past this seq — see _ephemeral_contextual_for_
        # turn's own comment on this field).
        self._max_evicted_untrusted_seq = 0
        # Single MediaStore instance per Session (#383 PR-C, see session-construction.md#multimodal-media)
        from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
        if multimodal_config is not None:
            self._media_store: "MediaStore | None" = MediaStore(
                MediaStoreConfig(
                    media_dir=multimodal_config.media_dir,
                    tool_results_dir=multimodal_config.tool_results_dir,
                ),
                project_root=Path.cwd(),
                # path-refs carry resource_uri/source_agent for cross-host dispatch (#385, see docs/reference/runtime/session-construction.md#multimodal-media)
                agent_name=self.agent_name,
                # path-refs carry a url when this instance is HTTP-reachable (#385, see docs/reference/runtime/session-construction.md#multimodal-media)
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
        # #3100 Axis 4: same-name-across-config-tiers collision map, consulted
        # by ``:skill`` invocation (reyn.interfaces.skill_invoke) to fire a
        # LOUD audit-event + warning instead of a silent shadow.
        self._skill_collisions: dict = skill_collisions or {}
        self._chat_tool_use_scheme = chat_tool_use_scheme  # #1593 PR-2, passed to RouterLoopDriver below
        # RouterLoop awaits the embedding index build synchronously on turn 1 when True (B25-S5-1 fix, see session-construction.md#family-5-retrieval)
        self._eager_embedding_build = eager_embedding_build
        # agent_id is owned by Agent (identity SSoT); the field + its prior
        # None-fallback default were removed (#3133 P0-follow-up, see docs/reference/runtime/session-construction.md#identity-the-agent-value-object-fp-0043-stage-2).
        # Proposal 0067 P0 (#3978): typed home for the present-tense task
        # state. See reyn.runtime.task_types.CurrentTask's own docstring for
        # why each field exists and where it comes from.
        self.current_task: "CurrentTask | None" = None
        # Outbox interceptor for external transport (e.g. Slack via MCP); None skips interception (FP-0041 #489 PR-D2)
        self._outbox_interceptor: Any = None
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
        self._max_hop_depth = _safety.loop.max_agent_hops  # PR11, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode
        self._chain_timeout_seconds = _safety.timeout.chain_seconds  # PR18, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode
        self._on_limit = _safety.on_limit  # FP-0005, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode
        self._safety_extensions: dict[str, float] = {}  # FP-0005, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode
        self._hook_driven_turns: int = 0  # #1800 slice 7, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode
        # Optional MCP server allowlist from agent profile (PR37, see docs/reference/runtime/session-construction.md#misc-lifecycle-wiring)
        self._allowed_mcp: list[str] | None = (
            list(allowed_mcp) if allowed_mcp is not None else None
        )

        self._events_config = events_config or AuditEventsConfig()  # PR20: per-chat rotation policy
        self._cost_warn_config = cost_warn_config or CostWarnConfig()  # #2230, see session-construction.md#family-4-cost-budget
        self._offload_config = offload_config or OffloadConfig()  # see docs/reference/runtime/session-construction.md#family-4-cost-budget
        # Resolve operator render_template bounds once, threaded to every router OpContext builder (FP-0055 / #2679, see session-construction.md#family-4-cost-budget)
        _rt_cfg = render_template_config or RenderTemplateConfig()
        from reyn.core.op_runtime.render_template import RenderTemplateBounds
        self._render_template_bounds = RenderTemplateBounds(
            max_output_chars=_rt_cfg.max_output_chars,
            wall_clock_seconds=_rt_cfg.wall_clock_seconds,
        )

        # WAL + per-agent snapshot for crash recovery via SnapshotJournal; snapshot_path kept only for diagnostics (PR21 / PR-refactor-session-1, see session-construction.md#family-2-recovery-wal-journal)
        self._session_id = session_id
        # #3705: pass the resolved state root through so an explicitly-
        # supplied workspace_state_dir isn't silently ignored (only used
        # when the caller didn't already override snapshot_path itself).
        self._snapshot_path = snapshot_path or default_snapshot_path(
            self.agent_name, root=self._reyn_state_root,
        )
        # generation_store / journal are now built by the CALLER (see
        # reyn.runtime.services.recovery.build_recovery) and received as
        # required params — Session no longer constructs its own recovery
        # pair (recovery-bundle-out-of-Session refactor, follow-up to #3082
        # Family 2's inline extraction; see
        # session-construction.md#family-2-recovery-wal-journal).
        self._generation_store = generation_store
        self._journal = journal
        # Turn-idle event for quiescence; lets a global rewind await_quiescent before the reset-record append (ADR-0038 Stage 1c, see session-construction.md#family-2-recovery-wal-journal)
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        # #3300 P2a: monotonic sent-queue-mutation counter — an order-race gate
        # for a client merging the granular `user_submitted`/`turn_started`
        # queue deltas (see `_bump_queue_seq` for the full rationale). In-memory
        # only, deliberately not WAL-durable: it is a client read-model
        # liveness aid (resolves snapshot/delta interleaving), not recovery
        # state — a restart safely resumes from 0 because a fresh connection's
        # STATE_SNAPSHOT always seeds the client's last-applied seq before any
        # delta is merged.
        self._queue_seq: int = 0
        # #3300 P3 (Y-server): msg_ids cancelled via `cancel_queued` while still
        # sitting in the (durable) `asyncio.Queue`, whose entry cannot be removed
        # in place (no such API) — skip-at-consume set. `_consume_inbox`/
        # `_drain_to_wake` discard a dequeued item whose msg_id is here instead
        # of dispatching it. The item's snapshot.inbox entry + WAL `inbox_cancel`
        # tombstone are recorded SYNCHRONOUSLY at cancel time (independent of
        # this deferred physical dequeue) — see `cancel_queued` /
        # `SnapshotJournal.cancel_inbox`. In-memory only: a crash before the
        # dequeue leaves no stale Queue entry to skip (a fresh process starts
        # with an empty asyncio.Queue and repopulates it from the recovered
        # snapshot, which already excludes the cancelled item — see
        # `restore_state`). Owned by ``self._inbox_arbiter`` (proposal 0067
        # P1, #3978 — InboxArbiter extraction), constructed below once
        # ``self.inbox`` exists.
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
        # Kept directly (not only via journal), see docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal
        self._state_log = state_log
        self._halted_reason: "str | None" = None  # #2259 PR-3: set on FAIL-STOP, see session-construction.md#family-2-recovery-wal-journal
        # In-memory buffer of restored-then-resolved intervention answers (PR-intervention-link L6, see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode)
        self._buffered_intervention_answers: dict[str, "InterventionAnswer"] = {}
        # In-memory staging for wake=false ride-along messages, durably
        # persisted in the snapshot (#1800 slice 4b). Owned by
        # ``self._inbox_arbiter`` (proposal 0067 P1, #3978).

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
        # #3339: chain_id of the most recent turn this session ran (set at the
        # _run_router_loop seam, kept after the turn ends so an idle status
        # surface can still report what the last turn cost). None before the
        # session's first router turn. The ambient scope carrying the same
        # value is per-TASK, so a render/status caller on another task cannot
        # read it — this attribute is how the turn key leaves the turn task.
        self._last_turn_chain_id: str | None = None

        _router_cap: int = _safety.loop.max_router_calls_per_turn  # per-turn router cap from safety config

        from reyn.config import CompactionConfig, ReasoningConfig
        self._compaction = compaction_config or CompactionConfig()
        self._reasoning = reasoning_config or ReasoningConfig()  # #1652: reasoning capture/continuity/display, on-by-default
        self._next_seq = 1

        # agents/<name>/ is state-only (PR20); Agent-derived workspace_dir, ensure it exists (FP-0043 Stage 2)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.workspace_dir / "history.jsonl"
        self.events_dir = (  # PR20: audit events dir, created lazily by EventStore on first write
            # #3705: anchored on the same root as workspace_dir — was a bare
            # relative `Path(".reyn")`, silently ignoring workspace_state_dir.
            self._reyn_state_root / "events" / "agents" / self.agent_name / "chat"
        )

        self.history: list[ChatMessage] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        # Proposal 0067 P1 (#3978): InboxArbiter owns the inbox-drain +
        # dispatch-attribution state cluster (pending peek buffer,
        # cancelled-msg-id skip-set, staged wake=false ride-alongs,
        # last_sender / last_reply_to) — see reyn.runtime.inbox_arbiter's
        # module docstring for what moved and what stayed here (and why).
        # ``notify_state_change`` is passed as a bound method reference
        # (resolved at call time, not at this line — the method is defined
        # later in this class, same shape callback-injection already uses
        # elsewhere in Session, e.g. InterAgentMessaging's construction).
        self._inbox_arbiter = InboxArbiter(
            inbox=self.inbox,
            journal=self._journal,
            notify_state_change=self.notify_state_change,
        )
        self.outbox: asyncio.Queue = asyncio.Queue()
        # event_store -> audit_events -> outbox_hub (+ opt-in OTEL), byte-identical extraction (#3082 Family 1, see session-construction.md#family-1-audit-event-spine-p6)
        _audit_bundle = self._build_audit_event_bundle(observability_config)
        self.outbox_hub = _audit_bundle.outbox_hub
        self._event_store = _audit_bundle.event_store
        self._audit_events = _audit_bundle.audit_events
        self._otel_exporter = _audit_bundle.otel_exporter
        # Embedding block + action_usage_tracker (#3082 Family 5). MOVED here,
        # right after Family 1, so the hot-list sink can bind audit_events by
        # IDENTITY (the _audit_bundle.audit_events OBJECT, passed as a builder
        # arg) instead of by NAME (a `self._audit_events` lookup deferred to
        # CALL time). #3408: the name-lookup form is the #2856 accident's
        # class -- a name reference can resolve to a DIFFERENT EventLog than
        # the one that was live at bind time (the #2856 bug bound `self.events`
        # by name and silently picked up the wrong log); an identity-bound
        # reference cannot, because it never re-resolves the name. Safe to
        # move here because Family 5 has no eager dependency on anything
        # Family 3/6a/etc. build later (see session-construction.md#family-5-retrieval).
        _retrieval_bundle = self._build_retrieval_bundle(
            self._action_retrieval, embedding_config, self.agent_name, self._audit_events,
        )
        self._action_embedding_index = _retrieval_bundle.action_embedding_index
        self._embedding_provider = _retrieval_bundle.embedding_provider
        self._embedding_model_class = _retrieval_bundle.embedding_model_class
        self._action_usage_tracker = _retrieval_bundle.action_usage_tracker
        # hook_bus -> hook_dispatcher -> fs_watcher -> composer_registry -> composed_consumer -> hot_reloader; runs right after Family 1 since hot_reloader reads audit_events eagerly (#3082 Family 3, see session-construction.md#family-3-hook-event-reactivity)
        _hook_bundle = self._build_hook_event_bundle(
            _boot_in_set,
            self._composer_defs,
            _fs_watch_cfg,
            self._audit_events,
            self._registry,
            self._session_id,
        )
        self._hook_bus = _hook_bundle.hook_bus
        self._hook_dispatcher = _hook_bundle.hook_dispatcher
        self._fs_watcher = _hook_bundle.fs_watcher
        self._composer_registry = _hook_bundle.composer_registry
        self._composed_consumer = _hook_bundle.composed_consumer
        # #4215 ②: holds the background task bridging THIS session's own
        # hook-bus events to a PARENT session's bus, when one is spawned
        # (session_api._spawn_pipeline_driver_session, attached path only).
        # None for every session that is not a bridged child (the default —
        # main sessions, detached spawns). Held here (not a local variable at
        # the spawn site) so it survives past the spawn call and so
        # AgentRegistry.remove_session has something to cancel at teardown —
        # see HookBus's own module docstring for why this is a narrow, opt-in
        # exception to "no cross-session Bus", not a removal of that claim.
        self._hook_bus_bridge_task: "asyncio.Task | None" = None
        self._hot_reloader = _hook_bundle.hot_reloader
        # Publish as the process-wide active reloader so the hooks-write LLM-op can request_reload (#2073 S3, see session-construction.md#family-3-hook-event-reactivity)
        from reyn.runtime.hot_reload import set_active_hot_reloader
        set_active_hot_reloader(self._hot_reloader)
        # #3787: project-context (AGENTS.md/REYN.md) read-only edit-detection watcher.
        # Deliberately NOT the HotReloader above — see ProjectContextWatcher's module
        # docstring for why this file cannot go through the LLM-writable IN-set.
        from reyn.runtime.project_context_watch import ProjectContextWatcher
        self._project_context_watcher = ProjectContextWatcher(
            path=project_context_path, events=self._audit_events,
        )
        # Publish this session's EventLog as the ambient LLM-chokepoint sink (#1669, see docs/reference/runtime/session-construction.md#family-1-audit-event-spine-p6)
        from reyn.core.events.events import set_llm_request_event_log
        set_llm_request_event_log(self._audit_events)
        # Publish reyn.yaml llm.router.* as the ambient router config (#1829 S3b, see docs/reference/runtime/session-construction.md#misc-lifecycle-wiring)
        if router_config is not None:
            from reyn.llm.llm import set_router_config
            set_router_config(router_config)
        if retry_config is not None:  # #1835, see docs/reference/runtime/session-construction.md#misc-lifecycle-wiring
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
        self._audit_events.add_subscriber(
            ChatLifecycleForwarder(
                self.outbox, registry=self._registry, events=self._audit_events
            )
        )
        # Generic events-log subscriber converting op-emitted events to state_change history entries (#398 v4 emitter family, see session-construction.md#misc-lifecycle-wiring)
        self._audit_events.add_subscriber(
            self._on_audit_event_for_state_change,
        )

        # Budget adapter, byte-identical extraction, simplest of the #3082 families (Family 4, see session-construction.md#family-4-cost-budget)
        self._budget = self._build_budget(
            budget_tracker, self._audit_events, self.agent_name, _router_cap,
        )

        # Memory persistence adapter, byte-identical extraction, pre-waist position (#3082 Family 8b, see session-construction.md#family-8b-memory)
        self._memory = self._build_memory()

        # One-shot command-UI request, see docs/reference/runtime/session-construction.md#family-7-intervention
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
            chat_tool_use_scheme=self._chat_tool_use_scheme,  # #3220: tool census matches the active scheme's composed payload
        )

        # owns + orchestrates them in one method (#2073 S2, see session-construction.md#family-3-hook-event-reactivity)
        self._register_hot_reload_seams()

        # Synchronous head/body/tail compaction callback (FP-0019 Wave 1 / #1128 PR-a, see docs/reference/runtime/session-construction.md#family-6b-history-compaction)
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

        # Adaptive per-user token-estimation learner (PR-N6, see docs/reference/runtime/session-construction.md#family-6b-history-compaction)
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

        # InterAgentMessaging: agent-to-agent messaging service (FP-0019 Wave 2 part 2, #3082 Family 8a, see docs/reference/runtime/session-construction.md#family-8a-inter-agent-messaging)
        self._inter_agent_messaging = self._build_inter_agent_messaging()

        # RouterLoopDriver owns the per-turn loop orchestration (PR-3, see docs/reference/runtime/session-construction.md#misc-lifecycle-wiring)
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
                events=self._audit_events,
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

    def subscribe_audit_events(self, cb: "Callable[..., None]") -> None:
        """Register ``cb`` for this session's audit events (narrow public API).

        Encapsulates the internal EventLog so UI callers (e.g. the inline CUI
        working indicator) subscribe without reaching into ``_audit_events``.
        ``cb`` receives an ``Event`` (``.type`` / ``.data``) synchronously on the
        session loop. Pair with :meth:`unsubscribe_audit_events`.
        """
        self._audit_events.add_subscriber(cb)

    def unsubscribe_audit_events(self, cb: "Callable[..., None]") -> bool:
        """Remove a callback registered via :meth:`subscribe_audit_events`."""
        return self._audit_events.remove_subscriber(cb)

    def set_events_dir(self, events_dir: Path) -> None:
        """#2348: re-point this session's chat EventStore to a per-session directory.

        Spawned sessions share the agent identity (and thus the name-only
        ``events_dir`` built in ``__init__``), so the chat audit events of all of an
        agent's sessions bled into one ``events/agents/<name>/chat`` tree. The
        registry's ``spawn_session`` fixup calls this — parallel to the snapshot/WAL
        re-key — before the run-loop goes live, so no event lands in the shared tree.

        Swaps ONLY the ``EventStore`` subscriber on ``_audit_events`` (remove old, add
        new); every OTHER subscriber (the ``ChatLifecycleForwarder`` outbox bridge, the
        state-change converter, any attach-time focus listener) is preserved. A rebuild
        of the subscriber list would silently drop them and audit events would stop
        reaching the outbox / TUI — so the swap is surgical, not a reconstruction.
        """
        new_store = EventStore(
            events_dir,
            max_bytes=self._events_config.max_bytes,
            max_age_seconds=self._events_config.max_age_seconds,
        )
        self._audit_events.remove_subscriber(self._event_store)
        self._audit_events.add_subscriber(new_store)
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
    def last_turn_usage(self) -> dict:
        """#3339: tokens + USD cost of this session's MOST RECENT turn —
        ``{"chain_id", "tokens", "prompt_tokens", "completion_tokens",
        "cost_usd", "usage_source"}`` (the same shape
        ``BudgetTracker.turn_usage`` returns).

        ``usage_source`` (#3351) is the provenance of the token figures —
        ``UsageSource.PROVIDER`` / ``ESTIMATED`` / ``UNKNOWN``, or ``None`` in
        the no-figure case below. It rides the same dict as the numbers so a
        consumer cannot read a figure while missing what kind of figure it is.

        A real per-turn aggregate: the durable tracker summed the actual
        per-call figures of every LLM call made under this turn's chain_id
        (tool-loop iterations included). It is NEVER derived by differencing
        cumulative counters, so it cannot drift into a fabricated number —
        the cost of a call the OS could not attribute to a turn is in no
        turn's total at all.

        When there is no figure, EVERY value is ``None`` — never a placeholder
        ``0``. A zero is indistinguishable from a turn that genuinely used
        nothing / cost nothing, and a renderer that forgot to check would print
        it as fact; ``None`` makes the same mistake loud (drawn as "None", or a
        ``TypeError`` the moment anything does arithmetic on it). No convention
        for a consumer to remember, and nothing to get wrong silently.

        The KEY SET is identical in both cases, so a consumer can read any
        field without first branching on which case it got — only the VALUES
        differ. A no-figure dict missing the keys the success dict carries
        would turn "no figure" into a ``KeyError`` at a different call site
        than the one that forgot to check.

        "No figure" occurs before this session's first router turn, with no
        tracker wired (unlimited mode), and when this session's last turn has
        been EVICTED from the tracker's bounded per-turn buckets
        (``TURN_BUCKET_CAP``).

        Read via the KEYED ``BudgetTracker.turn_usage(chain_id)`` (#3283 ④),
        NOT ``latest_turn_usage()``: the tracker is process-shared, so "the
        latest turn process-wide" is routinely some OTHER session's turn while
        this session's own last turn is sitting right there in the buckets.
        Asking for this session's own key answers the question actually being
        asked, and cannot return another session's number."""
        _none = {
            "chain_id": None,
            "tokens": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost_usd": None,
            # #3351: present in the no-figure dict too, so the key set stays
            # identical (see the docstring). ``None`` = there is no figure whose
            # provenance could be stated — distinct from
            # ``UsageSource.UNKNOWN``, which is a real figure of unstated origin.
            "usage_source": None,
        }
        chain_id = self._last_turn_chain_id
        tracker = self._budget_tracker
        if chain_id is None or tracker is None:
            return _none
        return tracker.turn_usage(chain_id) or _none

    def turn_usage(self, chain_id: str) -> "dict | None":
        """#3283 ④: tokens + USD cost of the turn ``chain_id`` —
        ``{"chain_id", "tokens", "prompt_tokens", "completion_tokens",
        "cost_usd", "usage_source"}``, or ``None`` when there is no figure for
        that turn (never recorded, evicted from the tracker's bounded buckets,
        or no tracker wired at all). ``usage_source`` (#3351) is the token
        counts' provenance, carried alongside the counts themselves.

        The KEYED sibling of :attr:`last_turn_usage`, for a caller that already
        knows which turn it is asking about — the TUI's right gutter, which
        renders one turn's ``↑prompt ↓completion`` split per conversation row
        and shows ``—`` on ``None``. (It does not display ``cost_usd``; the
        field is still returned for every other caller.) Same never-fabricate
        contract as the tracker's own
        ``turn_usage``: no ``0`` stands in for "unknown", and no figure is ever
        derived by differencing cumulative counters."""
        tracker = self._budget_tracker
        if tracker is None:
            return None
        return tracker.turn_usage(chain_id)

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

    # FP-0043 Stage 2: identity-field delegations to self._agent, read-only
    # (identity is immutable for the session lifetime). See docs/reference/runtime/session-construction.md#identity-the-agent-value-object-fp-0043-stage-2.
    @property
    def agent_name(self) -> str:
        return self._agent.agent_name

    @property
    def agent_id(self) -> str:
        return self._agent.agent_id

    @property
    def session_id(self) -> str:
        """This session's LIVE sid — the second half of the ``(agent, sid)`` key
        every per-session workspace surface is keyed by (``config.yaml`` narrowing,
        snapshot, state dir).

        A live read, not the construction-time value: ``spawn_session_recorded``
        re-keys a spawned session AFTER constructing it (``session._session_id =
        new_sid``), so anything caching the constructor's ``session_id`` argument is
        stale for exactly the sessions that were spawned programmatically. #3553
        added it because ``run_agent_step`` holds its invoker as a whole ``Session``
        and needs that key to look the invoker's own narrowing back up."""
        return self._session_id

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

    def available_skills(self) -> "list":
        """The skills registered for this session — the LIVE list, so a
        ``skills:`` hot-reload (``_reapply_skills``) is reflected on the next
        read rather than at the next restart.

        Public because a UI needs the SAME list the ``:name`` invocation path
        resolves against (``_maybe_handle_skill_invoke`` →
        ``invocable_skill_names``): the TUI's ``:`` completion (#3354) filters
        it through the shared ``skill_invoke_completions``, so a ``hidden`` or
        disabled skill can never be SUGGESTED by a surface that would then
        refuse to invoke it. Returns a copy — the caller must not be able to
        mutate the session's registry by editing what it was handed.
        """
        return list(self._available_skills or [])

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

    def _rebuild_derived_model_engines_for_model(self) -> None:
        """#1752 / #3785: rebuild the per-model-derived chat engines after a
        ``/model`` switch — turn_budget AND compaction, ONE private-Session
        entry point, not two.

        #3785 review (lead-coder): a second private-Session accessor
        (``_rebuild_compaction_engine_for_model``) was rejected — the
        ``_SESSION_RESIDUE`` ratchet in
        ``test_3595_s4_slash_handler_seam.py`` tracks *reachable* private
        access, and adding a second entry with "mirrors the existing one"
        as its own justification would have meant the precedent excused
        the debt rather than bounding it. Both rebuilds are equally "part
        of what /model MEANS" (the existing entry's own reasoning), so
        they fold into the SAME private call instead of two.

        turn_budget rebuilds EAGERLY: the engine bakes derived headroom
        (max_input + wrap-up-SP token cost) for one resolved (model,
        config) at construction (a deliberate compute-once invariant,
        mirroring CompactionEngine); ``try_build_*`` returns ``None`` for
        a small-context model (force-close stays inert, matching the
        original construction at ``__init__``) — matters immediately for
        a per-turn cap check, so it is worth having correct right away.

        compaction rebuilds LAZILY: the factory ``_build_history_compaction
        _bundle`` gave ``CompactionController`` already reads ``self.model``
        fresh each call, so a switch only needs the CACHE invalidated —
        the SAME lazy-build-on-first-real-use discipline #3671 established
        for construction applies to rebuilds too: a switch that never
        triggers compaction again should not pay to rebuild it. This is
        why compaction's own rebuild is NOT eager here, unlike turn_budget's
        — a deliberate difference, not an inconsistency (see
        ``docs/reference/runtime/session-construction.md``'s compaction
        section for the fuller argument).
        """
        from reyn.services.turn_budget import try_build_default_turn_budget_engine
        engine = try_build_default_turn_budget_engine(
            self._resolver.resolve(self.model).model,
            use_chars4=getattr(self._compaction, "use_chars4_estimate", False),
            # #3580: operator-tunable offload ceiling feeds the layer-1 reserve.
            max_inline_bytes=self._offload_config.max_inline_bytes,
        )
        self._router_host.set_turn_budget_engine(engine)
        self._compaction_controller.rebuild_engine()

    @property
    def workspace_dir(self) -> "Path":
        return self._agent.workspace_dir

    @property
    def _perm(self) -> "PermissionResolver | None":
        return self._agent.permission_resolver

    def _read_base_dir_override(self, path: "Path") -> "Path | None":
        """#4200: read a ``base_dir:`` override from *path* — either this
        session's own per-session config or the calling agent's own
        capability_profile — or ``None`` when absent/unset/malformed.

        Deliberately NOT routed through
        :func:`reyn.security.permissions.capability_profile.load_capability_profile`:
        that loader's own docstring says unknown keys are ignored
        (forward-compat), which for THIS key means it would silently DROP
        ``base_dir:`` rather than surface it — a raw read is required, same
        shape ``AgentRegistry.per_session_narrowing`` already uses for the
        SAME file (a key ``CapabilityProfile``'s dataclass doesn't model).
        A malformed file is surfaced (stderr-adjacent log, not a crash) and
        skipped — a typo must not crash session construction, and
        (restrict-only) skipping it only WIDENS toward the next fallback
        layer, never past the effective floor."""
        if not path.is_file():
            return None
        import yaml
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — hand/LLM-written yaml, surface not crash
            logger.warning(
                "#4200: skipping malformed base_dir override config %s: %s", path, e,
            )
            return None
        if not isinstance(raw, dict):
            return None
        value = raw.get("base_dir")
        if not value:
            return None
        return Path(str(value))

    @property
    def _workspace_base_dir(self) -> "Path | None":
        """#4200: this session's EFFECTIVE base_dir.

        A session-layer override (this session's own
        ``<session_state_dir>/config.yaml``) sits IN FRONT OF an agent-layer
        default (``.reyn/capability_profiles/<name>.yaml``), which sits IN
        FRONT OF the Agent's own value (``self._agent.workspace_base_dir`` —
        the pre-#4200 default, unchanged for every caller that sets
        neither override) — the SAME "layer in front of the shared Agent
        identity" shape #2103-S1a capability narrowing already uses. A
        spawned session's identity stays a SHARED ``Agent`` object; only
        the RESOLVED VALUE differs per session, never the object — #4200's
        own issue measured that duplicating the Agent per session is not
        the design.

        Both override reads are direct file reads, no ``AgentRegistry``
        access needed: the session config lives at the same directory
        :meth:`_read_per_session_hooks` already reads (a sibling of
        ``self._snapshot_path``), and the agent config lives at the same
        ``.reyn/capability_profiles/<name>.yaml`` the topology-binding read
        path already uses (``self._reyn_state_root`` is the SAME anchor).

        This is a live re-read on every access (a plain ``@property``, not
        cached) — a CALLER holding this value across a spawn-time fixup
        must re-read it, not cache it; see ``RouterOpContextSource``'s own
        ``workspace_base_dir_fn`` for why a frozen capture of this value is
        wrong for a spawned child."""
        session_override = self._read_base_dir_override(
            Path(self._snapshot_path).parent / "config.yaml"
        )
        if session_override is not None:
            return session_override
        agent_override = self._read_base_dir_override(
            self._reyn_state_root / "capability_profiles" / f"{self.agent_name}.yaml"
        )
        if agent_override is not None:
            return agent_override
        return self._agent.workspace_base_dir

    @property
    def _workspace_state_dir(self) -> "Path | None":
        return self._agent.workspace_state_dir

    @property
    def _reyn_state_root(self) -> "Path":
        """#3705: the SAME anchor `Agent.workspace_dir` resolves against
        (`workspace_state_dir` when the caller supplied one, else
        `Path.cwd() / ".reyn"`) — for the few Session-owned paths that sit
        ALONGSIDE `agents/<name>/`, not under it (`events/agents/<name>/...`),
        so they can't just be derived from `self.workspace_dir` directly."""
        return (
            self._workspace_state_dir
            if self._workspace_state_dir is not None
            else Path.cwd() / ".reyn"
        )

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
        # Internal backing-name for agent_role; delegating property, see docs/reference/runtime/session-construction.md#identity-the-agent-value-object-fp-0043-stage-2
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
    def turn_active(self) -> bool:
        """Read-only accessor: True while a turn is dispatched and in flight,
        False when idle (#3300 P2a).

        Exposes ``_turn_idle`` (ADR-0038 Stage 1c, cleared at turn-dispatch
        :meth:`run_one_iteration`, set in that turn's ``finally``) as a
        public read — this is surfacing EXISTING state, not a new authority.
        A client (in-process or remote/agui) uses this alongside
        :meth:`queued_user_messages` to render whether the next queued
        message will dispatch immediately (idle) or wait (busy).
        """
        return not self._turn_idle.is_set()

    def _bump_queue_seq(self) -> int:
        """Advance + return the sent-queue mutation seq counter (#3300 P2a).

        Called once per queue-affecting mutation — a ``user`` item entering
        the inbox (``submit_user_text``) or leaving it (dispatch,
        ``turn_started``) — and the returned value is stamped onto that
        mutation's audit-event (``seq=``). A client merging the granular
        ``user_submitted``/``turn_started`` deltas keeps the highest ``seq``
        it has applied (seeded from ``STATE_SNAPSHOT``'s ``queue_seq``) and
        discards any delta whose ``seq`` is not strictly greater — this is
        the order-race gate: a stale/duplicate ``user_submitted`` for an
        item ALREADY dispatched (whose ``turn_started`` carries a higher
        seq, itself ≤ a snapshot taken after that dispatch) can never
        resurrect the item in the client's queue model, regardless of the
        arrival order of the snapshot read vs. the delta delivery. Every
        turn (not only ``kind=="user"``) bumps this counter at
        ``turn_started`` — harmless for non-queue turns (nothing in the
        client's queue model matches their ``chain_id``), and keeps the
        counter a single, simple, strictly-monotonic sequence.
        """
        self._queue_seq += 1
        return self._queue_seq

    @property
    def queue_seq(self) -> int:
        """Read-only accessor for the current queue-mutation seq counter
        (#3300 P2a) — see :meth:`_bump_queue_seq`."""
        return self._queue_seq

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
        + default chain through this surface. Proposal 0067 P1 (#3978):
        the value itself lives on ``self._inbox_arbiter.last_reply_to``.
        """
        return self._inbox_arbiter.last_reply_to

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
        turn's run_loop breaks at the next tool-iteration boundary, AND sets
        the per-turn ``cancel_event`` (``RouterLoopDriver.cancel_event``,
        threaded onto the router's OpContext via
        ``RouterHostAdapter._set_cancel_event`` — #1470) that a currently-
        running sandboxed subprocess tool races against. Any spawned tasks
        are cancelled immediately via asyncio task cancellation (existing
        behaviour, preserved here).

        #4166 correction (this docstring previously said "subprocess kill is
        a follow-up scope" — that was stale even when written: the regular
        ``sandboxed_exec`` op's non-CodeAct launches have raced
        ``cancel_event`` and killed the process group since #1470;
        ``CodeActRunner`` was the one launch route that reinvented its own
        ``Popen`` instead of going through ``SandboxBackend.run()`` and so
        never got it — #4166 closed that gap). A tool NOT wired to
        ``cancel_event`` at all (this Session's set is #1470's
        ``sandboxed_exec``/CodeAct plus whatever else threads
        ``OpContext.cancel_event`` through — MCP calls, embed, plugin
        install; grep ``cancel_event=ctx.cancel_event`` for the current
        list) still only observes the cooperative flag at the next
        iteration boundary, same as before.

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

        **Re-entrancy guard (self-cancel)**: the hard ``Task.cancel()`` is
        skipped when the CALLER IS the turn-owner task — the mirror of the
        guard ``await_quiescent`` already carries for the same call shape (a
        slash handler invoking ``AgentRegistry.checkout`` mid-turn, which
        all-cancels every loaded session *including its own*). Without it the
        turn cancels itself: ``_must_cancel`` is armed and ``CancelledError``
        lands at checkout's first real suspension — the reset-record's
        durability await — so the ``rewind`` WAL record is written but step 5
        (``_materialize_rewind``) never runs. The WAL then claims the world was
        reset while every live session keeps running the pre-rewind lineage,
        and the user gets neither the ``⏪ checked out`` reply nor an error
        (``CancelledError`` is a ``BaseException``, so ``rewind_cmd``'s
        ``except Exception`` does not see it). "Cancel all in-flight work" can
        only ever mean "everything except the caller asking for it": a caller
        cannot want the request it is currently making to be destroyed. Every
        other caller (Ctrl-C via the transport, the AG-UI endpoint,
        ``remove_session``) runs on a different task and is unaffected.
        """
        # #3903: was anything actually running, BEFORE the cancel attempt —
        # the return value used to say "✗ cancelled turn" unconditionally,
        # even when there was nothing in flight (the same shape #4166 found
        # live in cancel_task's own reply: an accepted request that reports
        # success regardless of whether anything was actually stopped).
        running_turn = (
            self._turn_owner_task is not None
            and asyncio.current_task() is not self._turn_owner_task
            and not self._turn_owner_task.done()
        )
        self._loop_driver.request_cancel()
        if running_turn and self._turn_owner_task.cancel():
            self._turn_cancel_self_initiated = True
        forwards = list(self._cancel_forward_targets)
        for forward in forwards:
            forward()
        if running_turn or forwards:
            return "✗ cancelled turn"
        return "nothing was running"

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
        # 1. wait for the current turn to go idle -- re-entrancy-safe (see
        # docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal
        # for the _turn_idle / _turn_owner_task rationale).
        if asyncio.current_task() is not self._turn_owner_task:
            await self._turn_idle.wait()
        # 2. cancel + join chain-timeout watchdogs. A cancelled timer cannot fire
        #    (no chain_timeout_fired append); join settles any callback already
        #    in-progress before this returns. On reconstruct, restore() re-arms a
        #    watchdog from the recovered snapshot (proposal 0067 P8, #3978:
        #    against the REMAINING time on a persisted arm_at deadline, not
        #    necessarily a fresh window), so cancelling is reversible.
        await self._chains.cancel_and_join_timers()
        # 3-4. RE-DRAIN LOOP (#2115): cancel the tracked fire-and-forget WAL tasks
        #    (cancel — not join-only — is required: the intervention-dispatch task
        #    awaits the user-answer future indefinitely; the tasks are drop-safe so
        #    cancelling is correct) + join the append-capable tasks
        #    (_inflight_wal_tasks), then RE-CHECK. A joined task may schedule a NEW
        #    tracked append (or re-spawn) DURING the gather — which the prior
        #    one-shot snapshot would miss (#2115). Loop to a fixpoint (both sets fully
        #    drained) so no append can land after this returns. On reconstruct,
        #    restore() re-arms timers from the recovered snapshot (proposal 0067
        #    P8, #3978: against the REMAINING time on a persisted arm_at deadline,
        #    not necessarily a fresh window), so cancelling is reversible.
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
        rationale (#3121 step3 Extract Class).

        #3380: the ephemeral per-turn narrowing is passed IN rather than resolved
        there — its input is the live conversation (``self.history``), which
        ``CapabilityVisibility`` deliberately does not hold (it owns the envelope +
        the ``/visibility`` override, not the context). ``Session`` is the only
        object that can see both, so the composition happens at this seam."""
        return self._capability_visibility.capability_visibility_state(
            ephemeral_contextual=self._ephemeral_contextual_for_turn(),
        )

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
            self._capability_visibility.reapply_visibility_override()
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
        """#2608 H5: public entry point for an OUT-OF-SESSION source to fire
        a hook on THIS session's dispatcher.

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

        proposal 0067 P3: ``reyn.runtime.services.pipeline_executor_driver.
        PipelineExecutorDriver._deliver`` calls this on the REPLY session
        (the issuer waiting on the task, not the driver-session that ran
        it) to fire ``task_settled`` right after ``pipeline_result`` is
        delivered — the same "resolve a Session it doesn't own, long after
        its own ``__init__``" shape H5/H4 already needed a public seam for.
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
            next_turn_context              → self._inbox_arbiter.next_turn_context
            hook_driven_turns              → self._hook_driven_turns (#2884; restore_state's
                                               plain assignment is unconditional, so no
                                               separate clear step is needed here)

        The _inflight_wal_tasks task handles are already settled by
        await_quiescent; this drops the (now-done) handles so the rewound
        session starts clean.
        """
        while True:
            try:
                self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._chains.reset()
        self._interventions.clear()
        restore_tasks = getattr(self, "_restore_intervention_tasks", None)
        if restore_tasks:
            for t in restore_tasks:
                if not t.done():
                    t.cancel()
            self._restore_intervention_tasks = []
        self._buffered_intervention_answers.clear()
        # next_turn_context (#1800-4b)
        self._inbox_arbiter.next_turn_context.clear()
        # hook_driven_turns (#2884): reset the loop-valve counter mirror. restore_state
        # re-assigns it wholesale from the reconstructed snapshot, but clearing here keeps
        # the zero-residue guarantee robust independent of that assignment.
        self._hook_driven_turns = 0
        self._inflight_wal_tasks.clear()
        # pending_inbox_item / cancelled_msg_ids / last_sender / last_reply_to
        # (proposal 0067 P1, #3978 InboxArbiter extraction): the same latent
        # gap current_task's own comment below names — NONE of these four
        # were part of AgentSnapshot before this extraction either, and
        # reset_for_rewind never cleared them (only next_turn_context and
        # hook_driven_turns were). A genuine process crash was already safe
        # (fresh Session() defaults win, restore_state never sets them), but
        # a REWIND reuses the SAME live Session — without this, a peeked
        # mid-turn item, a stale cancel-skip entry, or a stale sender/
        # reply_to attribution from BEFORE the rewound point would silently
        # outlive it. Discovered while moving this state into one holder for
        # this extraction; fixed here rather than deferred, matching the
        # current_task precedent immediately below.
        self._inbox_arbiter.pending_inbox_item = None
        self._inbox_arbiter.cancelled_msg_ids.clear()
        self._inbox_arbiter.last_sender = None
        self._inbox_arbiter.last_reply_to = None
        # current_task (proposal 0067 P1', #3978): NOT part of AgentSnapshot
        # (deliberately volatile, same framing ADR-0040 gives reply_to — "None
        # after crash"), so a genuine process crash is naturally safe: a
        # fresh Session() defaults current_task to None and restore_state
        # never sets it. A REWIND is a different recovery path — the SAME
        # live Session object survives, so without this explicit clear a
        # mid-delegation current_task would outlive the rewind and
        # MessageBus._is_quiescent would report non-quiescent forever for a
        # delegation the rewound timeline no longer contains (lead-coder
        # review, #3978: "委譲したまま二度と返らないセッション" — worse than
        # the bug P1' exists to close). See the paired truncate/rewind-style
        # falsify test.
        self.current_task = None

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
        from reyn.security.permissions.capability_profile import compose_resolved

        ephemeral = self._ephemeral_contextual_for_turn()
        if ephemeral is None:
            return self._capability_visibility.contextual_permission
        resolved = [(ephemeral, frozenset())]
        if self._capability_visibility.contextual_permission is not None:
            resolved.insert(0, (self._capability_visibility.contextual_permission, frozenset()))
        return compose_resolved(resolved)[0]

    def _ephemeral_contextual_for_turn(self) -> "object | None":
        """#3380: the EPHEMERAL half of the per-turn narrowing on its own — the
        resolved ``_untrusted`` profile while the live active context is tainted,
        ``None`` otherwise.

        Split out of :meth:`_effective_contextual_for_turn` (which composes it with
        the static envelope) because the Tool tab has to tell the two apart: an
        envelope denial is durable and the operator cannot lift it, whereas this one
        lifts itself when the untrusted entry compacts out. Rendering both under one
        reason would answer a different question than the operator's ("why can I not
        use this, and what would change it") — the #3378 failure mode.

        NOT a second notion of "what is narrowed": this IS the term
        ``_effective_contextual_for_turn`` composes, so the tab and the live gate
        cannot drift. The taint is re-derived from ``self.history`` on every call
        (never latched at turn start), so a status-bar read is as of NOW; only the
        loaded profile is cached, and a profile file does not change mid-session.

        #4387 Phase A: the scan this re-derivation runs is bounded to the
        UNCOMPACTED tail (``seq > self._compaction_watermark()``, the same
        bound ``_uncompacted_tool_call_records`` already uses), not all of
        ``self.history``. ``metas_have_untrusted``'s own docstring already
        promises "the until-compaction scope for free" from being handed
        only "the live, un-compacted entries" — this call site was actually
        violating that contract (it passed the FULL unfiltered history,
        so real deployments never got the promised self-clear-on-compaction
        at all, only an O(session-length) scan on every turn once
        narrowing was opted into, #3501). Bounding to the watermark fixes
        both: the freshness/self-clearing property this method's own tests
        pin (``test_turn_context_denial_self_clears_when_the_taint_leaves_
        the_context``) becomes what actually happens in a compacting
        session, not just an unenforced docstring claim, and the re-scan on
        every turn is now proportional to the uncompacted tail rather than
        total session length. Entries with ``seq == 0`` (pre-#3704 legacy
        rows with no assigned coordinate) are always included — never
        excluded from a taint check just because they predate seq
        tracking.

        #3501: OPT-IN. This returns ``None`` — no narrowing at all — unless the
        operator set ``safety.threat_scan.capability_narrowing`` to something other
        than ``off``. It is the SINGLE place the opt-in is read, so the live gate,
        the advertisement filter and the Tool tab are all engaged or all disengaged
        by one check and cannot disagree about whether the mechanism is on.
        """
        from reyn.security.permissions.capability_profile import (
            UNTRUSTED_NARROWING_ORIGIN,
            load_untrusted_profile,
            metas_have_untrusted,
            resolve_profile,
        )

        threat_scan = getattr(self._safety, "threat_scan", None)
        if threat_scan is None or not threat_scan.narrowing_engaged():
            return None
        watermark = self._compaction_watermark()
        active = (m for m in self.history if m.seq == 0 or m.seq > watermark)
        # #4381 PR-2 stage ③: history scan OR the in-flight latch — the
        # latch covers a same-turn tool result whose history entry has not
        # landed yet (see the flag's own docstring in __init__ for why).
        # #4468 (lead-coder security review): OR a THIRD term —
        # self._max_evicted_untrusted_seq's own OR-latch, covering an
        # untrusted entry that WAS active (seq > watermark) but has since
        # been evicted from self.history by #4387's resident-byte cap (the
        # scan above only ever sees RESIDENT entries, so eviction would
        # otherwise silently drop this signal — CLAUDE.md's own "removing
        # one layer regrants a denied capability" shape). Self-clears the
        # same way the resident scan does: once compaction advances the
        # watermark past that seq, this term drops out on its own — no
        # separate clearing logic needed, same monotone comparison either
        # way.
        if not (
            metas_have_untrusted(m.meta for m in active)
            or self._in_flight_untrusted_this_turn
            or self._max_evicted_untrusted_seq > watermark
        ):
            return None
        if self._untrusted_contextual_cache is None:
            root = self._perm.project_root if self._perm is not None else Path.cwd()
            self._untrusted_contextual_cache = resolve_profile(
                load_untrusted_profile(root),
                origin=UNTRUSTED_NARROWING_ORIGIN,
            )[0]
        return self._untrusted_contextual_cache

    def _mark_untrusted_in_flight(self) -> None:
        """#4381 PR-2 stage ③: set the per-turn in-flight taint latch.

        Wired into ``RouterHostAdapter`` as ``mark_untrusted_in_flight`` and
        called by ``router_loop.py`` at the SAME point it stamps
        ``external_source`` onto a tool-result's persisted meta — see that
        call site's own comment for why this is not a second, independently-
        maintained signal. Reset to ``False`` at the top of
        ``_run_router_loop`` (each new turn); never cleared mid-turn (the
        same "narrows, never un-narrows before the turn boundary" property
        ``_untrusted_latched`` already has for the per-iteration rung).
        """
        self._in_flight_untrusted_this_turn = True

    # ── persistence ─────────────────────────────────────────────────────────────

    def _append_history(self, msg: ChatMessage) -> None:
        # #3704: assign a monotonic seq to every persisted entry regardless
        # of role. Previously gated on ``msg.role in ("user", "agent")`` —
        # ``"agent"`` was never a real role (``ChatMessage``'s role Literal
        # has always been "user"/"assistant"/"tool"/"system"/"summary"; no
        # commit ever introduced an "agent" role), so this condition only
        # ever matched "user". assistant/tool entries persisted with
        # seq==0 permanently, and CompactionController._select_candidates's
        # ``t.seq > prev_cover`` filter (services/compaction_controller.py)
        # reads seq==0 as "already covered" — so assistant/tool turns were
        # silently EXCLUDED from every compaction candidate set, unfixably,
        # since seq is set once at persist time. Owner-ratified fix
        # (2026-08-08): drop the role gate rather than fix the spelling —
        # ``seq == 0`` used to mean two different things ("not yet
        # assigned" and "covered by an already-compacted summary"), and
        # ``t.seq > prev_cover`` cannot tell them apart; removing the gate
        # collapses it to ONE meaning ("no coordinate assigned" — true only
        # for old, pre-fix history entries now).
        if msg.seq == 0:
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
        self._evict_oldest_resident_entries()

    def _evict_oldest_resident_entries(self) -> int:
        """#4387 Phase B ③: cap ``self.history``'s in-memory footprint at
        ``self._history_resident_config.max_bytes``, evicting from the FRONT
        (oldest first) — the symmetric, opposite-direction operation of
        :meth:`_load_older_entries`'s prepend (architect's #4387 derivation:
        "追い出しは既に在って動いている操作の逆" — eviction is not
        information loss, since ``history.jsonl`` is untouched and anything
        evicted reloads on demand via the already-shipped backward-hydrate
        path, #4400/#4411).

        Recomputes the total resident size from scratch each call rather
        than maintaining a running counter deliberately: ``self.history`` has
        multiple direct-mutation call sites elsewhere (assignment, slicing —
        e.g. ``_load_older_entries``'s own ``self.history[0:0] = parsed``,
        and tests that reassign ``s.history`` directly to simulate a bounded
        load), so an incrementally-maintained counter would silently drift
        from the true resident set the first time any of those paths ran
        without also updating it. Recomputing is O(n) per append, but n is
        bounded by construction (that is the entire point of this cap), so
        the cost stays small — and the invariant "resident size <= cap after
        every append" holds by construction, with no cached state that could
        desync from reality.

        ONLY called from the tail-growth path (:meth:`_append_history`, a
        normal turn appending the newest entry) — deliberately NOT called
        after :meth:`_load_older_entries`'s backward-prepend. A caller that
        explicitly asks to page further back (TUI scrollback / search /
        ``_active_branch_history``'s rewind-visibility extension) wants those
        entries resident; evicting them again immediately would silently
        defeat the very feature #4400/#4411 built, thrashing between
        "extend backward" and "evict the same entries straight back out."
        A deep page-back can therefore transiently exceed the cap — that is
        an explicit, bounded (by ``min_lines``) request the caller made, not
        the unbounded tail-growth this cap exists to bound.

        Returns the count evicted (0 = already within budget)."""
        cap = self._history_resident_config.max_bytes
        sizes = [
            len(json.dumps(asdict(m), ensure_ascii=False).encode("utf-8"))
            for m in self.history
        ]
        total = sum(sizes)
        evict_count = 0
        # Never evict the newest (last) entry, even if it alone exceeds the
        # cap on its own — the entry just appended must stay resident so the
        # turn that produced it remains immediately usable; an oversized
        # single entry is a cap-sizing question for the operator, not
        # something this method silently drops.
        while total > cap and evict_count < len(sizes) - 1:
            total -= sizes[evict_count]
            evict_count += 1
        if evict_count:
            # #4468 security block (lead-coder review): before dropping
            # them, latch the highest seq among the evicted entries that
            # carried the untrusted-content marker — see
            # self._max_evicted_untrusted_seq's own __init__ comment and
            # _ephemeral_contextual_for_turn's OR-term for why. Checked
            # here (not gated behind whether narrowing is enabled) since
            # the cost is one dict-get per evicted entry regardless, and
            # gating it would mean narrowing couldn't be turned on
            # mid-session without a gap for whatever already evicted
            # before the flag flipped.
            from reyn.security.permissions.capability_profile import (
                UNTRUSTED_META_KEY,
            )
            for m in self.history[:evict_count]:
                if isinstance(m.meta, dict) and m.meta.get(UNTRUSTED_META_KEY):
                    self._max_evicted_untrusted_seq = max(
                        self._max_evicted_untrusted_seq, m.seq,
                    )
            del self.history[:evict_count]
        return evict_count

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
        always visible (backward-compatible, no migration).

        #4387 Phase B ②: since ``load_history()`` no longer necessarily
        loads the whole file, this extends ``self.history`` BACKWARD first
        (:meth:`_load_older_entries`) whenever the active branch's own
        abandoned-interval bounds reach further back than what's currently
        in memory — see ``earliest_relevant_wal_seq``'s own docstring for
        why its return value is the exact threshold that makes the filter
        below correct. A session that has never rewound
        (``earliest_relevant_wal_seq`` returns ``None``) never triggers
        this — the common case pays nothing extra.
        """
        if self._state_log is None:
            return self.history
        from reyn.core.events.snapshot_generations import (
            build_active_predicate,
            earliest_relevant_wal_seq,
        )

        threshold = earliest_relevant_wal_seq(self._state_log)
        if threshold is not None:
            while True:
                loaded_wal_seqs: "list[int]" = [
                    m.meta["wal_seq"] for m in self.history
                    if isinstance(m.meta, dict) and isinstance(m.meta.get("wal_seq"), int)
                ]
                earliest_loaded = min(loaded_wal_seqs) if loaded_wal_seqs else None
                if earliest_loaded is not None and earliest_loaded <= threshold:
                    break
                oldest_seq = self.history[0].seq if self.history else 0
                extended = self._load_older_entries(before_seq=oldest_seq)
                if extended == 0:
                    break  # BOF reached — nothing older exists on disk

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

    def last_sender(self) -> str | None:
        """Return the most-recently-attributed sender label or None if no
        message has been routed yet. Read-only accessor for
        ``InboxArbiter.last_sender`` — write side stays internal to the
        dispatch attribution path (proposal 0067 P1, #3978: moved onto
        ``self._inbox_arbiter``, see ``InboxArbiter.handle_sender_attribution``)."""
        return self._inbox_arbiter.last_sender

    def _on_audit_event_for_state_change(self, event) -> None:
        """Generic events-log subscriber that converts known emitter events
        to ``state_change`` history entries (= #398 v4 emitter family).

        The chat router's ``OpContext.events`` is bound to this session's
        ``_audit_events`` (= session.py make_router_op_context). When the
        LLM invokes an op like ``mcp_install`` and the op emits its
        success event, this subscriber sees it and mints the
        corresponding state_change so the LLM's next turn sees the
        world-state change without a separate plumbing path per
        emitter.

        Extension shape (= one dict entry per new emitter):
          ``_STATE_CHANGE_EVENT_MAPPINGS[event_type] = (source, template)``
        where ``template`` is EITHER a ``str.format``-compatible string
        (receives the event's ``data`` dict as kwargs) OR a callable
        ``(data: dict) -> str`` for formatters that need optional-field
        handling a plain template can't express (#3636 — see
        ``_format_config_reloaded``). New emitters only need to (a) emit
        a known event type on the audit events log and (b) register
        their (source, template) in the mapping.

        Defensive: malformed event payloads (= missing template keys,
        wrong types) are silently skipped — observability must not
        crash the events bus or downstream subscribers.
        """
        mapping = _STATE_CHANGE_EVENT_MAPPINGS.get(getattr(event, "type", ""))
        if mapping is None:
            return
        source, template = mapping
        try:
            if callable(template):
                summary = template(event.data or {})
            else:
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
            self._audit_events.emit(
                "state_change_notified",
                summary=summary,
                source=source or "",
            )
        except Exception:
            # Defensive: observability must not crash the API.
            pass

    def notify_turn_cancelled(self, chain_id: "str | None") -> None:
        """Persist a genuinely-cancelled turn's outcome as a first-class
        history entry (#3694).

        Storage shape mirrors :meth:`notify_state_change` exactly (same
        precedent, same owner ruling — "むやみに増やすべきでない、system
        あるならそれで": no new role, reuse ``system``):
          - ``role="system"`` — excluded from every LLM-facing turn list
            (``RouterHistoryBuffer.build_history``'s allowlist is
            ``role in ("user","assistant","tool","agent")``) and from
            compaction candidates (``force_compact_now``'s own turns
            filter is the same allowlist) — structurally, not by a new
            check either of those already-existing filters would need.
          - ``meta.kind="turn_cancelled"`` — distinguishes this from a
            ``state_change`` system entry; a reader (TUI restore
            projection) dispatches on this key, never on the rendered
            ``content`` string.
          - ``meta.chain_id`` — correlates back to the turn this outcome
            belongs to (the same chain_id the cancelled turn's own
            ``user_message_received`` / ``turn_started`` events carry).

        Append-only, NOT an in-place edit: ``history.jsonl`` has no
        rewrite path (only ``"a"``/``"r"`` opens exist — grepped), so a
        cancelled outcome discovered at turn-end cannot be durably
        recorded by mutating the (already-persisted) user turn's own
        ``meta`` — that mutation would only live in memory and vanish on
        restart. A NEW entry is the only way to add durable information
        to an append-only log.

        Called from exactly the places that OBSERVE a turn ending
        because it was cancelled (not merely requested — a cancel racing
        turn completion must never call this): ``RouterLoop``'s
        cooperative-cancel terminal (``_loop_cancelled`` true at the
        outer-loop exit) when reached, and ``Session.run_one_iteration``'s
        hard-cancel ``CancelledError`` catch as the receiver for when a
        ``cancel_inflight()`` hard ``Task.cancel()`` (the common
        mid-LLM-call Ctrl+C case) injects ``CancelledError`` at whatever
        await the turn was suspended on — which unwinds straight past
        ``RouterLoop``'s own terminal check (measured: zero
        ``CancelledError`` handling anywhere in ``router_loop.py``), so
        that check never runs for a hard cancel. The two call sites are
        mutually exclusive per actual cancelled turn (one always reaches
        its stamp point, the other doesn't, for a given cancellation) —
        this is a primary path plus the receiver for when it's skipped,
        not two independent recorders that could double-append.
        """
        self._append_history(ChatMessage(
            role="system",
            content="Turn interrupted by user.",
            ts=_now_iso(),
            meta={"kind": "turn_cancelled", "chain_id": chain_id},
        ))

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
        # #2081: every A2A REQUEST-path load marks the target is_delegate=True
        # (recursive, response path does not) — see delegation-policy.md.
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

    def _parse_history_line(self, line: str) -> "ChatMessage | None":
        """Parse one ``history.jsonl`` line into a ``ChatMessage``, or
        ``None`` if malformed (skipped, never raised — byte-identical to
        the pre-#4387 behavior). Pure: does not touch ``self.history``."""
        try:
            raw = json.loads(line)
            # Read-time migration for pre-#383 entries (legacy text + media
            # shape → new content shape).
            raw = _migrate_legacy_chat_message(raw)
            return ChatMessage(**raw)
        except Exception:
            return None

    def _append_parsed_history_line(self, line: str) -> None:
        """Parse one ``history.jsonl`` line and append it to ``self.history``
        — the per-line body shared by both of :meth:`load_history`'s paths."""
        msg = self._parse_history_line(line)
        if msg is not None:
            self.history.append(msg)

    def _load_older_entries(self, *, before_seq: int, min_lines: int = _HISTORY_HYDRATE_MIN_LINES) -> int:
        """#4387 Phase B ②: extend ``self.history`` BACKWARD from
        ``history.jsonl``, prepending up to ``min_lines`` entries older
        than ``before_seq`` (typically ``self.history[0].seq`` — "give me
        more of what I don't have yet"). Returns the count actually
        prepended (0 if none qualify, e.g. already at the file's start).

        Called by consumers that need to look further back than what a
        bounded :meth:`load_history` loaded at startup — currently
        :meth:`_active_branch_history` when a rewind/branch-switch cut
        references a ``wal_seq`` older than anything in memory. Prepending
        never evicts anything already loaded (Phase B is "load lazily,"
        not "unload") — see the module docstring on why that is currently
        UNBOUNDED (a deep rewind can pull most of a huge file back into
        memory) and left that way deliberately: bounding it caps how far
        back a rewind can reach, which is an owner-facing capability
        question, not one this stage answers.
        """
        from reyn.runtime.history_tail_reader import read_history_before

        lines = read_history_before(
            self.history_path, before_seq=before_seq, min_lines=min_lines,
        )
        if not lines:
            return 0
        parsed = [m for line in lines if (m := self._parse_history_line(line)) is not None]
        self.history[0:0] = parsed
        return len(parsed)

    def extend_history_backward(self, *, min_lines: int = _HISTORY_HYDRATE_MIN_LINES) -> int:
        """#4387 Phase B ② (remaining consumers): the PUBLIC paging primitive
        for callers outside ``Session`` itself — TUI scrollback paging and
        in-conversation search, via
        :meth:`reyn.interfaces.repl.read_model.RegistryReadModel.load_older_conversation_history`.

        Thin wrapper over :meth:`_load_older_entries`, deriving ``before_seq``
        from ``self.history[0].seq`` — "give me more of what's already oldest
        in memory," the generic paging shape. This differs from
        :meth:`_active_branch_history`'s own internal use of the private
        primitive, which derives ``before_seq`` from the WAL rewind
        threshold instead (a specific correctness bound, not "one more
        page") — that call stays on the private method directly since it is
        still ``Session``'s own internal correctness mechanism, not a
        paging request from outside.

        Returns the count of entries prepended (0 = the file's start was
        already reached — the sole "nothing more to load" signal a paging
        caller needs).
        """
        oldest_seq = self.history[0].seq if self.history else 0
        return self._load_older_entries(before_seq=oldest_seq, min_lines=min_lines)

    def load_history(self) -> None:
        """#4387 Phase B ①: hydrate ``self.history`` at startup WITHOUT
        necessarily reading the whole (potentially huge — owner's real
        environment measured 500MB) file. Two paths:

        FAST (the common case, post-#3704): peek the file's last complete
        line cheaply (:func:`read_last_line`, bounded by one line's length).
        If it carries a real assigned ``seq`` (every append does, post-#3704
        — see #3704's own history), a bounded backward read
        (:func:`read_history_tail`) is safe: it is GUARANTEED to include
        everything since the latest compaction (the same
        ``_compaction_watermark()`` bound Phase A's narrowing-taint fix
        already assumes) plus a minimum scrollback floor, and ``_next_seq``
        derives from that one peeked line directly — O(1), no scan.

        FALLBACK (rare — a file whose last write predates #3704, or one
        that fails to parse): the ORIGINAL full forward read + full
        ``max(seq)`` scan, byte-identical to the pre-#4387 behavior. This is
        the path ``test_3704_seq_assigned_to_every_role.py``'s interleaved
        ``seq == 0`` fixture exercises — that test needs no change.

        Entries this doesn't load are NOT lost: ``history.jsonl`` is
        append-only, so anything left unread here stays on disk, reachable
        later via the on-demand extend path (#4387 Phase B ②, not yet
        implemented).
        """
        if not self.history_path.exists():
            return
        from reyn.runtime.history_tail_reader import read_history_tail, read_last_line

        last_line = read_last_line(self.history_path)
        last_seq = 0
        if last_line is not None:
            try:
                last_seq = int(json.loads(last_line).get("seq", 0) or 0)
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                last_seq = 0

        if last_seq > 0:
            for line in read_history_tail(
                self.history_path, min_lines=_HISTORY_HYDRATE_MIN_LINES,
            ):
                self._append_parsed_history_line(line)
            self._next_seq = last_seq + 1
            return

        # Fallback: full forward read, full scan — see docstring above.
        with self.history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                self._append_parsed_history_line(line)
        # #3704: entries persisted before the role-gate removal (assistant/
        # tool turns from the old buggy path) have seq==0 and stay that
        # way forever — nothing re-derives or backfills a coordinate for
        # them at read time. ``if m.seq`` below simply ignores those
        # zeros when finding the max, which is correct: a 0 never
        # legitimately outranks a real assigned seq.
        max_seen = max((m.seq for m in self.history if m.seq), default=0)
        self._next_seq = max_seen + 1

    # ── inbox API ───────────────────────────────────────────────────────────────

    async def submit_user_text(
        self, text: str, *, attribution: "dict | None" = None,
    ) -> str:
        # PR14: every top-level user submission starts a fresh chain_id that
        # propagates through any agent_request / agent_response generated in
        # response. Logged in history meta + events.jsonl for cross-agent trace.
        chain_id = new_chain_id()
        # #3300 P2b: meta computed once, stored additively on the inbox payload too
        # (not just emitted on the event below) — a late-joiner seeding from
        # STATE_SNAPSHOT/queued_user_messages() needs it too. See agui-transport.md.
        meta = _user_frame_meta(attribution)
        msg_id = await self._put_inbox(
            TurnOrigin.CLIENT_INPUT,
            {"text": text, "chain_id": chain_id, "meta": meta},
        )
        # #3300 P1(C)/P2a(E): user_submitted is the single source of truth for the
        # echo (no parallel outbox write); msg_id is a PUBLIC wire key (unlike the
        # internal `_put_inbox` key); display neutralization happens downstream.
        # See agui-transport.md.
        self._audit_events.emit(
            "user_submitted",
            text=text,
            chain_id=chain_id,
            msg_id=msg_id,
            seq=self._bump_queue_seq(),
            meta=meta,
        )
        # #3287: return msg_id so a submitting client can recognise its own echo by
        # id, not text (avoids same-text collision). See agui-transport.md.
        return msg_id

    async def submit_agent_request(
        self, *, from_agent: str, request: str, depth: int, chain_id: str,
        from_sid: "str | None" = None,
    ) -> None:
        await self._put_inbox(TurnOrigin.AGENT_REQUEST, {
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
        await self._put_inbox(TurnOrigin.AGENT_RESPONSE, {
            "from_agent": from_agent, "response": response, "depth": depth,
            "chain_id": chain_id,
            # #2103 S1bc-exec: responder_sid correlates to the spawner's _spawned_tasks
            # record. See session-construction.md.
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
        await self._put_inbox(TurnOrigin.PIPELINE_RESULT, {
            "run_id": run_id, "pipeline_name": pipeline_name, "status": status,
            "text": text, "chain_id": chain_id or new_chain_id(),
            "sender": "pipeline:os",
        })

    # ── #2103 S1bc-exec: spawned-task correlation (SpawnTracker). See
    # session-construction.md. ──

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
        # #398 v4 emitter wiring: unregister the permission-persist subscriber so
        # dead-session refs don't accumulate on the shared PermissionResolver.
        # See session-construction.md.
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
             — probes every configured server that (1) and (2) left without a
             cached ANSWER (#3520; this includes a server whose earlier probe
             timed out, which is stored nowhere rather than as an empty list).

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
            await self._router_host.maybe_refresh_mcp_tools_from_yaml()
            self._router_host.maybe_reload_mcp_tools_cache_from_disk()
            await self._router_host.ensure_mcp_tools_cached(
                per_server_timeout=self._safety.timeout.mcp_probe_seconds,
            )
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

        # Compare content, not id() — the adapter always returns a FRESH copy on
        # every read regardless of whether the cache changed, so id() would report
        # a swap on every call and `refreshed` would be meaningless (rejected: id()
        # comparison; #2372 area).
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
        worker, pipeline driver-session, and ``spawn_session``/``delegate_to_agent``
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

    # ── #3082 Family 1: audit-event spine builder. See session-construction.md. ──

    def _build_audit_event_bundle(
        self, observability_config: "object | None"
    ) -> "_AuditEventBundle":
        """#3082 Family 1: build the audit-event (P6) spine — ``event_store``
        (disk-backed) -> ``audit_events`` (the ``EventLog`` nearly every other
        Session sub-component consumes) -> ``outbox_hub`` (the outbox
        fan-out), plus the opt-in OTEL subscriber attached to ``audit_events``.

        Byte-identical extraction of the sequence that used to run inline in
        ``__init__`` — same objects, same construction order, same args.
        Reads only attributes ``__init__`` has already set by this point
        (``self.outbox`` / ``self.agent_name`` / ``self.events_dir`` /
        ``self._events_config`` / ``self._agent.agent_id``); takes
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
        audit_events = EventLog(
            subscribers=[event_store],
            agent_id=self._agent.agent_id,  # FP-0016 E: auto-inject agent_id into every event
        )
        otel_exporter = None
        try:
            from reyn.observability.otel_exporter import build_otel_exporter
            otel_exporter = build_otel_exporter(observability_config)
            if otel_exporter is not None:
                audit_events.add_subscriber(otel_exporter)
        except Exception:  # noqa: BLE001 — OTEL attach must never break session init
            otel_exporter = None
        return _AuditEventBundle(
            event_store=event_store,
            audit_events=audit_events,
            outbox_hub=outbox_hub,
            otel_exporter=otel_exporter,
        )

    # ── #3082 Family 3: hook-event/reactivity bundle builder. See session-construction.md. ──

    def _build_hook_event_bundle(
        self,
        boot_in_set: "dict",
        composer_defs: list,
        fs_watch_cfg: "object",
        audit_events: "EventLog",
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
        ``self._audit_events`` unchanged.

        Placement (call-site in ``__init__``): this family is built AFTER the
        Family 1 audit-event bundle because it CONSUMES ``audit_events`` —
        ``hot_reloader`` reads it EAGERLY (``events=audit_events``). That is the
        #3082 pipeline's output→input order (Family 1 → Family 3), and it is
        also byte-identical to the original inline code, where the
        hot_reloader was likewise constructed after the ``audit_events`` EventLog.

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
        from reyn.hooks.composer import ComposerRegistry, build_composers
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
        # HookDispatcher/Composer below — the lambda resolves ``self._audit_events``
        # only at first-drop time, never at construction — so a subscriber-queue
        # drop is fail-visible via a metadata-only bus_subscriber_dropped P6
        # audit-event.
        hook_bus = HookBus(emit_event=lambda et, **d: self._audit_events.emit(et, **d))
        # #1800 slice 5b: the awaited HookDispatcher. Hooks load from the resolved
        # ``hooks:`` block; None/absent → empty registry → every dispatch() is a
        # no-op (run-loop byte-identical to a hooks-free build). Constructed
        # unconditionally so the 4 lifecycle dispatch() sites are uniform.
        hook_dispatcher = HookDispatcher(
            self._build_hook_registry(boot_in_set),
            put_inbox=self._put_inbox,
            stage_next_turn_context=self._inbox_arbiter.stage_next_turn_context,
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
            # Lambda defers ``self._audit_events`` resolution to dispatch time.
            emit_event=lambda et, **d: self._audit_events.emit(et, **d),
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
        # Composer/consumer registry: build != start — starting here has no
        # async context to run in; run() starts/stops them (#2880/#2881;
        # session-construction.md#composer-registry-consumer-construction-vs-start-28802881).
        composer_registry = ComposerRegistry(
            composers=build_composers(
                composer_defs,
                bus=hook_bus,
                durable_store=self._build_composer_pending_store(composer_defs),
                emit_event=lambda et, **kw: self._audit_events.emit(et, **kw),
            ),
        )
        composed_consumer = ComposedEventConsumer(
            bus=hook_bus, dispatcher=hook_dispatcher,
        )
# #2073 S1: the config hot-reloader reads ONLY the IN-set (.reyn/*.yaml); the
# OUT-set (reyn.yaml) is restart-only and never picked up here. Applies at the
# turn_end safe-point (apply_pending below); reads audit_events eagerly.
        hot_reloader = HotReloader(
            project_root=getattr(registry, "_project_root", None) or Path.cwd(),
            events=audit_events,
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
        audit_events: "EventLog",
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
        ``budget_tracker`` / ``audit_events`` / ``agent_name`` / ``router_cap``
        explicitly rather than reaching into ``self`` mid-construction:
        ``budget_tracker`` is the LOCAL ``__init__`` parameter (NOT
        ``self._budget_tracker``, which is a separate tracking assignment
        made earlier in ``__init__`` for callers that receive the tracker by
        value, and is out of scope for this extraction — same shape as
        Family 2's ``state_log``); ``audit_events`` is Family 1's
        ``EventLog``, read EAGERLY here (``events=audit_events``), which is
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
            events=audit_events,
            agent_name=agent_name,
            default_router_cap=router_cap,
        )

    def _build_retrieval_bundle(
        self,
        action_retrieval: "ActionRetrievalConfig",
        embedding_config: "EmbeddingConfig | None",
        agent_name: str,
        audit_events: "EventLog",
    ) -> "_RetrievalBundle":
        """#3082 Family 5: build the retrieval spine — the embedding block
        (three attrs, one conditional construction guarded by
        ``universal_wrappers_enabled and embedding.enabled`` (FP-0066 §7,
        clean-break replacement for the retired ``embedding_class`` truthy
        gate) with a try/except
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
        run inline in ``__init__``, MODULO one reordering (#3408): the
        call site moved from its ORIGINAL position (line ~1152, BEFORE
        Family 1 / ``_build_audit_event_bundle`` ran) to run right AFTER
        Family 1 instead. ``action_retrieval`` / ``embedding_config`` /
        ``agent_name`` are the ``self._action_retrieval`` value / the
        ``embedding_config`` __init__ parameter / the LOCAL ``agent_name``
        __init__ parameter (NOT ``self.agent_name``, mirroring the original
        inline reference at the ``action_usage_tracker`` persist path) —
        all resolvable at the new call site exactly as they were at the old
        one, since nothing between the two positions reads or writes them.

        ``audit_events`` IS a builder input (#3408), unlike its pre-#3408
        shape where it was deliberately excluded so the closure below
        (``_on_hot_list_changed``) could defer ``self._audit_events``
        resolution to CALL time — the EventLog did not exist yet at the
        builder's old (pre-Family-1) call site, so an eager reference there
        would have raised ``AttributeError``. #3408 moved the call site
        instead of keeping the deferral: ``git grep '_audit_events ='`` across
        ``src`` finds exactly ONE assignment, ``Session.__init__``'s single
        ``self._audit_events = _audit_bundle.audit_events`` — no restore/attach
        path re-binds it — so a NAME lookup deferred to call time and an
        IDENTITY reference captured once at construction resolve to the same
        object today. The identity form is preferred structurally: a name
        lookup re-resolves ``self._audit_events`` on every call, so a future
        rebinding of that name (however unlikely) would retarget every
        deferred closure that names it silently — the #2856 accident's
        class (a name reference resolved to a DIFFERENT EventLog than the
        one live when the reference was written). An identity reference
        captured once cannot retarget: it either keeps pointing at the
        EventLog it was given, or (if that assumption ever breaks) the
        AST single-assignment guard in
        ``tests/repo/test_audit_events_single_assignment_3408.py`` goes RED first,
        naming the new call site and saying "route this through the builder
        arg, not a rediscovered eager ``self._audit_events``."

        FP-0034 Phase 2 steps 1 + 5 / Issue #192:
        see the three embedding attrs' and ``action_usage_tracker``'s
        original inline comments, reproduced verbatim below."""
        # FP-0034 Phase 2 step 1: build the ActionEmbeddingIndex +
        # EmbeddingProvider once per session when the operator has set
        # ``embedding.enabled: true`` (FP-0066 §7 — clean-break
        # replacement for the retired ``action_retrieval.embedding_class``
        # on/off gate; the model CLASS is ``embedding.default_class``).
        # Both stay None when embedding is not enabled, in which case the
        # ``search_actions`` wrapper is hidden by ``build_tools`` and
        # the handler degrades to an empty-result response.
        action_embedding_index: Any = None
        embedding_provider: Any = None
        embedding_model_class: str | None = None
        if (
            action_retrieval.universal_wrappers_enabled
            and embedding_config is not None
            and embedding_config.enabled
        ):
            try:
                from reyn.data.embedding import get_provider as _get_provider
                from reyn.tools.action_index import ActionEmbeddingIndex

                embedding_provider = _get_provider("litellm", embedding_config)
                embedding_model_class = embedding_config.default_class
                # FP-0057 Phase 0: unified onto IndexBackend's cache convention — clean-break, no migration. docs/reference/runtime/reyn-dir-layout.md#canonical-layout
                # #3705: anchored on workspace_base_dir (the OpContext FS
                # root) when the caller supplied one — was a bare
                # `Path.cwd()`, silently ignoring it. `None` (Agent's own
                # documented "→ host cwd" default) preserves prior behavior
                # for callers that never set it.
                # #4200 KNOWN GAP (same eager-capture class as
                # RouterOpContextSource's own workspace_base_dir_fn fix,
                # not applied here): ActionEmbeddingIndex takes a plain
                # `Path`, not a supplier — its constructor has no lazy
                # variant, so it CANNOT read a spawned session's real
                # per-session base_dir override (fixed up by the registry
                # AFTER this line runs). A spawned child's search_actions
                # index is rooted at the PARENT's base_dir until
                # ActionEmbeddingIndex itself is restructured to defer its
                # workspace_root read — out of scope here (not in #4200's
                # own "auto-follows" list; this is the action-embedding
                # cache location, not the permission/exec surface #4200
                # targets). Tracked, not silently dropped.
                action_embedding_index = ActionEmbeddingIndex(
                    workspace_root=self._workspace_base_dir or Path.cwd(),
                )
            except Exception:
                # If provider construction fails for any reason (= missing
                # dependency / malformed config), fall through to "no index"
                # so the rest of the session continues without
                # search_actions rather than refusing to start.
                embedding_provider = None
                action_embedding_index = None
                embedding_model_class = None
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
                # Issue #192 / #3408: wire a callback that emits
                # ``hot_list_updated`` on every reorder of the compacted
                # ranking. Binds the ``audit_events`` builder ARG by IDENTITY
                # (not a ``self._audit_events`` name lookup) — see this
                # method's docstring for why identity binding is safe today
                # and what keeps it safe (the AST single-assignment guard).
                def _on_hot_list_changed(ranking: list[dict]) -> None:
                    try:
                        audit_events.emit(
                            "hot_list_updated", ranking=ranking,
                        )
                    except Exception:
                        pass
                action_usage_tracker = ActionUsageTracker(
                    # #3705: anchored on the same root as workspace_dir —
                    # was a bare relative `Path(".reyn")`.
                    persist_path=(
                        self._reyn_state_root / "agents" / agent_name
                        / "action_usage.json"
                    ),
                    on_ranking_changed=_on_hot_list_changed,
                )
            except Exception:
                action_usage_tracker = None
        return _RetrievalBundle(
            embedding_provider=embedding_provider,
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
        constructor-supplied initial value, threaded explicitly because
        ``CapabilityVisibility`` (which owns the LIVE composed value everywhere
        else) needs ``router_host`` — THIS call's output — so it cannot exist
        yet when this builder runs. It is the FALLBACK the op-context
        supplier's ``contextual_permission_fn`` uses until
        ``CapabilityVisibility`` exists, after which the supplier reads the
        live composed value (#3607 — the adapter used to freeze this raw value
        for the whole session, so an operator's later narrowing never reached a
        registry-dispatched op). Defaults to ``None`` (= no narrowing) so a
        bare ``_build_router_waist()`` (e.g. a builder-contract test) stays
        constructible; the sole production caller, ``__init__``, always passes
        the real constructor value.

        ★ Several of the values threaded in are DEFERRED, NOT eager — the
        ``*_fn`` fields of ``RouterOpContextSource`` (#3607) and
        ``live_session_id_inputs.live_session_id_fn`` (#3482) keep resolving
        ``self._<attr>`` at CALL time, not here. ``_current_turn_origin``
        already carries a pre-turn DEFAULT at construction
        (``"auto_improvement"``, set at :1083 — BEFORE this builder runs), but
        it is then REASSIGNED per turn inside ``run_one_iteration`` (far after
        ``__init__`` returns) — an eager-captured value here would freeze the
        pre-turn default forever, never seeing a real turn's origin;
        ``live_session_id_fn`` is deferred because a spawned session's live
        session id can change AFTER this constructor runs (the cached
        ``self._session_id`` read here is stale for that case — see the inline
        comment above ``record_spawned_task`` below). Eager-izing any of them
        would freeze a per-turn value at construction time — the Family 3/5
        deferred/eager pitfall repeated here for a third and heavier family.
        ``record_spawned_task`` (a bound method) and
        ``put_outbox_inputs.agent_replies_tracker`` (a tracker lambda) are
        likewise kept verbatim, still closing over ``self``.

        PR-refactor-session-1 wave 3 PR3: RouterHostAdapter — concrete
        RouterLoopHost implementation extracted from Session. Constructed
        last in __init__ because it receives callbacks that reference self
        (all of which are bound methods, resolved at call time not here)."""
        # #1092 PR-F1: turn_budget engine off the RESOLVED model. Making
        # try_build_* raise instead of return None crashes __init__ for
        # small-context models instead of degrading (session-construction.md#chat-turn_budget-engine-none-on-small-context-never-raise-1092-pr-f1).
        #
        # #3671 follow-up: a DEFERRED closure (the same "*_fn kept verbatim,
        # closing over self, resolved at call time" family this docstring
        # already names above for RouterOpContextSource/live_session_id_fn),
        # not an eager call. try_build_default_turn_budget_engine touches
        # litellm's model catalog (get_max_input_tokens) — calling it here
        # unconditionally put that cost on EVERY session construction, i.e.
        # the TUI startup path, for a value nothing reads until the first
        # force-close check mid-turn (owner real-machine measurement: #3671,
        # `tui-boot` inflated 2.27s -> 6.49s once #3780 stopped prepaying the
        # import earlier). `RouterHostAdapter._ensure_turn_budget_engine`
        # calls this at most once, on first reference — try_build_*'s own
        # contract (None for a small-context model, never a raise) is
        # unchanged, just realized lazily instead of at construction.
        def _build_chat_turn_budget_engine() -> Any:
            from reyn.services.turn_budget import try_build_default_turn_budget_engine
            return try_build_default_turn_budget_engine(
                self._resolver.resolve(self.model).model,
                use_chars4=getattr(self._compaction, "use_chars4_estimate", False),
                # #3580: operator-tunable offload ceiling feeds the layer-1 reserve.
                max_inline_bytes=self._offload_config.max_inline_bytes,
            )

        # #3607: the ONE chat-router op-context supplier for this Session.
        # Both entry points that hand an OpContext to a chat-router op —
        # ``Session._make_router_op_context`` (the _file_op / MCP callbacks)
        # and ``RouterHostAdapter.make_router_op_context`` (the registry
        # dispatch's ``op_context_factory``) — are one-line delegations to
        # ``.build()`` on THIS object, so they cannot hand out different
        # capabilities. Before, each assembled its own, and twelve fields had
        # already diverged. Every ``*_fn`` here is read at build time: see the
        # class docstring for why a snapshot of any of them is right on turn 1
        # and wrong afterwards.
        self._router_op_context_source = RouterOpContextSource(
            events=self._audit_events,
            permission_resolver=self._perm,
            file_permissions_fn=self._get_file_permissions_for_router,
            mcp_servers_fn=self._get_mcp_servers_for_router,
            mcp_servers_flat_fn=self._mcp_servers_flat,
            # #1827: live — ``_reapply_per_agent_capability`` REPLACES the
            # allowlist mid-session, and a narrowing that only reached one of
            # the two op-context doors is not a narrowing.
            allowed_mcp_fn=lambda: self._allowed_mcp,
            # #4200: LIVE — was a frozen value (`self._workspace_base_dir`),
            # captured at construction time, before a spawned session's real
            # base_dir override (#4200's own session-config-layer read,
            # which depends on `self._snapshot_path`) is fixed up by the
            # registry's post-construction spawn fixup. Same staleness
            # class `session_id_fn`/`turn_origin_fn` above already guard
            # against — a plain value here would silently freeze the
            # PARENT's base_dir onto every child forever.
            workspace_base_dir_fn=lambda: self._workspace_base_dir,
            workspace_state_dir=self._workspace_state_dir,
            environment_backend=self._environment_backend,
            sandbox_backend=self._sandbox_backend,
            # #1339: live — ``_sandbox_config`` is the resolved operator policy
            # and a reload can replace it.
            sandbox_policy_fn=lambda: (
                self._sandbox_config.policy if self._sandbox_config is not None
                else None
            ),
            # FP-0016: this agent's identity → the MCP client's X-Reyn-Agent-Id.
            agent_id=self._agent.agent_id,
            # FP-0022 fix (#53) / #3049: bridge-aware — resolved per call so an
            # attached pipeline driver's op reaches the ORIGINATOR's operator.
            intervention_bus_factory=self._make_router_intervention_bus,
            # FP-0054 PR-B / #2708 P1: a real PresentationRenderer so a `present`
            # op reaches the surface's sink instead of PR-A's null surface. The
            # sink comes from the surface's declared PresentationConsumer
            # (orphan-impossible: OutboxPresentationRenderer is constructible
            # ONLY inside OutboxPresentationConsumer.sink) — bound to THIS
            # Session via sink(self).
            presentation_renderer_factory=lambda: self._presentation_consumer.sink(self),
            # FP-0054 PR-C: live — ``_reapply_presentations`` swaps the registry
            # so a newly-registered template is visible on the next op.
            presentation_registry_fn=lambda: self._presentation_registry,
            multimodal_config=self._multimodal_config,
            web_fetch_config=self._web_fetch_config,  # #4274
            read_cap_config=self._read_cap_config,  # #4381 PR-5
            media_store_fn=lambda: self._media_store,  # #383/#2409
            compact_now=self._compact_now_for_op,  # #272/#1128
            threat_scan=self._safety.threat_scan,  # FP-0050/#1822
            # #1827 S3: live — ``CapabilityVisibility`` owns the composed value
            # but needs ``router_host``, i.e. THIS builder's output, so it does
            # not exist yet. Until it does, the constructor's raw value is the
            # narrowing in force (byte-identical to the pre-#3607 adapter,
            # which froze that value forever).
            contextual_permission_fn=lambda: (
                self._capability_visibility.contextual_permission
                if getattr(self, "_capability_visibility", None) is not None
                else contextual_permission
            ),
            # #1953: live — a spawned session's real id is assigned after this
            # constructor runs, and ops must namespace under the real one.
            session_id_fn=lambda: self._session_id,
            hook_dispatcher=self._hook_dispatcher,  # #1800 slice 5c
            hook_bus=self._hook_bus,  # Hook-Event Redesign Phase 5 part 2
            # proposal 0060 Phase 1 (A7): live — ``_current_turn_origin`` carries
            # a pre-turn default here and is REASSIGNED per turn in
            # ``run_one_iteration``; an eager read would freeze the default.
            turn_origin_fn=lambda: self._current_turn_origin,
            hot_reloader=self._hot_reloader,  # #2073 S3 / #2761 PR-2
            render_template_bounds=self._render_template_bounds,  # #2679
            budget_gateway=self._budget,  # FP-0063 PC
            # #3196 co-vet round 2: the BASE registered set, deliberately NOT the
            # visibility-filtered view — the `file` op's skill-load provenance
            # gate is a TRUST decision and must not depend on whether the
            # operator hid the skill from the menu.
            available_skills_fn=lambda: self._available_skills,
            # #3903 a-2 ③: live — ``_ephemeral`` is reassigned post-construction
            # (``spawn_ephemeral_session``), same reasoning as ``session_id_fn``
            # above and the existing ``ephemeral_fn`` this Session already
            # threads to the MCP gateway (``_mcp_list_via_gateway``, below).
            ephemeral_fn=lambda: self._ephemeral,
            # #4193 ①: live — ``_attended`` is reassigned post-construction the
            # same way ``_ephemeral`` is (``AgentRegistry.spawn_session_recorded``,
            # not mode-derived — see ``Session._attended``'s own docstring).
            attended_fn=lambda: self._attended,
        )
        # #3482/#3447: the 3-param mcp-gateway cluster (sole reader:
        # RouterHostAdapter._mcp_list_via_gateway) — a real consumer-set
        # cluster, all three arrived together in #3447's Path A fold.
        _mcp_gateway_inputs = McpGatewayInputs(
            mcp_connection_service=self._mcp_connection_service,
            mcp_agent_id=self._agent.agent_id,
            ephemeral_fn=lambda: self._ephemeral,
        )
        # #3482: two more measured consumer-set clusters (sole readers:
        # RouterHostAdapter.put_outbox / .live_session_id) — #4150 retired
        # the third (send_to_agent_inputs): the adapter's own send_to_agent
        # method it fed had zero callers after P6 (#3978) removed the sole
        # producer of the closure that used to reach it. self._send_to_agent
        # (the callback it wrapped) stays live — it's P4e's own transport,
        # reached directly via Session._send_to_agent, not through this
        # bundle or this adapter. Same values, same call-time semantics as
        # the flat kwargs they replace — the trackers stay lambdas over the
        # live Session fields and live_session_id_fn stays a deferred read
        # (the constructor's cached session_id is stale for a spawned
        # session).
        _put_outbox_inputs = PutOutboxInputs(
            put_outbox=self._put_outbox,
            agent_replies_tracker=lambda: self._router_loop_agent_replies,
        )
        _live_session_id_inputs = LiveSessionIdInputs(
            session_id=self._session_id,
            live_session_id_fn=lambda: self._session_id,
        )

        router_host = RouterHostAdapter(
            # #2175: the safety.on_limit checkpoint + the shared per-run extension dict —
            # so the spawn SEAM (spawn_agent / create_topology) routes spawn-limit exceeds
            # through the same mode-driven framework as inter_agent_messaging's max_agent_hops.
            handle_chat_limit_checkpoint=self._handle_chat_limit_checkpoint,
            safety_extensions=self._safety_extensions,
            # #1092 PR-F1: the chat turn_budget engine (resolved-model, asserted).
            # #3671 follow-up: a factory, not the built engine — see the
            # closure above.
            turn_budget_engine_factory=_build_chat_turn_budget_engine,
            # FP-0050 / #1822 S2: content-threat scan + fence config.
            threat_scan=self._safety.threat_scan,
            op_context_source=self._router_op_context_source,  # #3607
            state_log=self._state_log,  # #2259 PR-1 → config generation emit from config ops
            live_session_id_inputs=_live_session_id_inputs,  # #3482
            # #4215①: LAZY — a spawned session's real ``_snapshot_path`` is
            # assigned by the registry's spawn-time fixup, AFTER this
            # constructor runs (same reason ``session_id_fn`` above is a
            # callable, not a value).
            session_state_dir_fn=lambda: Path(self._snapshot_path).parent,
            agent_name=self.agent_name,
            agent_role=self._agent_role,
            output_language=self.output_language,
            permission_resolver=self._perm,
            mcp_servers=self._mcp_servers,
            project_context=self._project_context,
            events=self._audit_events,
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
            # proposal 0067 P4 (#3978): describe_task/list_tasks/cancel_task
            # act against THIS session's own pending_chains — mirrors
            # pipeline_registry above.
            chains=self.chains,
            # FP-0054 PR-C: the session's PresentationRegistry — mirrors
            # pipeline_registry above; the adapter threads its CURRENT snapshot into
            # each router OpContext, and _reapply_presentations swaps both copies.
            presentation_registry=self._presentation_registry,
            # #2103 S1bc-exec: record a spawned session's sid→task (the trusted result
            # header source) + read this session's LIVE sid (the cached session_id above
            # is stale for spawned sessions, stamped post-construction) for the non-main
            # spawn guard.
            record_spawned_task=self.record_spawned_task,
            agent_workspace_dir=self.workspace_dir,
            # #3705: was never passed — the adapter fell back to its own
            # cwd-relative default (`Path.cwd() / ".reyn" / "state"`) even
            # when this Session had an explicit workspace_state_dir.
            state_dir=self._reyn_state_root / "state",
            mcp_call_tool=self._mcp_call_tool,
            # #2597 slice ②a: resources consumption (read/templates).
            mcp_read_resource=self._mcp_read_resource,
            # #2597 slice ②b: resource subscriptions.
            mcp_subscribe_resource=self._mcp_subscribe_resource,
            mcp_unsubscribe_resource=self._mcp_unsubscribe_resource,
            # #2597 slice ②c: prompt fetch (get).
            mcp_get_prompt=self._mcp_get_prompt,
            # #3447/#3482: the 5 mcp_list_* callbacks (servers/tools/resources/
            # resource_templates/prompts) were folded onto the adapter itself
            # (RouterHostAdapter.mcp_list_*) — it already duplicated
            # _mcp_servers_flat/_get_mcp_servers_for_router, so only the raw
            # gateway-identity inputs need threading through (mcp_gateway_inputs
            # bundle, built above), not a callback per listing method.
            mcp_gateway_inputs=_mcp_gateway_inputs,
            put_outbox_inputs=_put_outbox_inputs,  # #3482
            append_history=self._append_history,
            # #3792: mid-turn CLIENT_INPUT injection.
            peek_mid_turn_injection=self._inbox_arbiter.peek_mid_turn_injection,
            commit_mid_turn_injection=self._commit_mid_turn_injection,
            # #4381 PR-2 stage ③: in-flight taint latch.
            mark_untrusted_in_flight=self._mark_untrusted_in_flight,
            # Proposal 0067 P1' (#3978)
            mark_task_pending=lambda: setattr(self, "current_task", CurrentTask()),
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
            available_skills=self._available_skills,  # #2548 PR-A
            # Read the injected sandbox_backend INSTANCE's .name, not the
            # config string — a mismatch silently HIDES a working exec
            # capability from discovery (#1417/FP-0034; session-construction.md#sandbox_backend-gate-reads-the-injected-instance-not-the-config-string-1417-fp-0034).
            sandbox_backend=_exec_gate_backend_name(
                self._sandbox_backend, self._sandbox_config
            ),
            # #187: the FS env-backend instance for the LIVE router OpContext
            # Workspace (the registry file-dispatch factory). Same source as
            # the chat OpContext (#1410) — which now reads it off the shared
            # op-context supplier; the adapter keeps its own reference because
            # its container-repo helpers read it directly.
            environment_backend=self._environment_backend,
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
            # #3580: the structured gate's two sizes are operator-tunable too;
            # threaded beside the flag through the same static-config seam.
            offload_structured_inline_max_chars=self._offload_config.structured_inline_max_chars,
            offload_structured_preview_chars=self._offload_config.structured_preview_chars,
            # #272/#1128 context-size signal: live exact-token budget so the
            # router SP can show the LLM the free window (header).
            context_window_status=self.context_window_status,
            # B25-S5-1: thread eager-build flag so RouterLoop awaits build
            # before computing _search_visible on the first turn.
            eager_embedding_build=self._eager_embedding_build,
            # #3049/#2708 P3.2a: bridge-aware intervention bus (attached driver ->
            # parent's operator; root/detached -> self-bound), single-sourced via
            # _make_router_intervention_bus. docs/concepts/runtime/intervention-delivery.md#the-single-construction-seam
            intervention_bus_factory=self._make_router_intervention_bus,
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
            events=self._audit_events,
            media_store=self._media_store,
            router_host=self._router_host,
            action_retrieval=self._action_retrieval,
            non_interactive=self._non_interactive,
            reasoning=self._reasoning,  # #1652/② native reasoning re-attach + bound
            # #3629: live workspace root, resolved at wire-serialise time — a
            # callable (never the resolved value) so a rewind/checkout swap
            # between turns is reflected. Mirrors ``Workspace.__init__``'s own
            # ``base_dir or Path.cwd()`` default (``self._workspace_base_dir``
            # is the agent-level override, ``None`` when unset — the SAME
            # value ``ctx.workspace.base_dir`` resolves to for the ops this
            # buffer's history entries came from).
            project_dir_fn=lambda: self._workspace_base_dir or Path.cwd(),
            read_cap=self._read_cap_config,  # #4381 PR-5
        )

        # #3671 follow-up: a DEFERRED closure, not an eager construction —
        # same reasoning and same family as _build_chat_turn_budget_engine
        # above. CompactionEngine.__init__ touches litellm's model catalog
        # (estimate_tokens/get_max_input_tokens) to measure its budgets;
        # building it here unconditionally put that cost on EVERY session
        # construction (the TUI startup path) for a value nothing reads
        # until compaction actually triggers, mid-turn.
        # CompactionController._engine (a lazy property) calls this at most
        # once, on first reference.
        def _build_chat_compaction_engine() -> CompactionEngine:
            return CompactionEngine(
                # #1172: pass a model CLASS, not a pre-resolved literal —
                # CompactionEngine.__init__ resolves it itself via `resolver`
                # below (unresolved-vs-resolved was the #1172 hazard, not
                # class-vs-literal; passing an unresolved class here is
                # correct — see CompactionEngine's own docstring).
                # #3785: compaction always follows the conversation's active
                # model now — no per-purpose override
                # (model_class_by_purpose.compaction was removed; a config
                # that still sets it fails to load, config/root.py). Reading
                # `self.model` FRESH here (not baked in) is what makes
                # `CompactionController.rebuild_engine` (called on every
                # `/model` switch) actually pick up the new model — this
                # closure is called again lazily, at most once per rebuild.
                model=self.model,
                events=self._audit_events,
                system_prompt_provider=history_buffer.build_system_prompt,
                resolver=self._resolver,
                # #1190 stage (ii): record chat compaction LLM spend (purpose=compaction).
                recorder=self._budget_tracker,
                # #1190 stage (iii) Part 4: attribute chat compaction to this session's agent.
                recorder_agent=self.agent_name,
            )

        compaction_controller = CompactionController(
            event_log=self._audit_events,
            config=self._compaction,
            # FP-0050/#1822 S3 (#1820): secret-redact turn text before summary.
            threat_scan=self._safety.threat_scan,
            history_access=lambda: self.history,
            latest_summary=self._latest_summary,
            compaction_engine_factory=_build_chat_compaction_engine,
            history_appender=self._append_history,
            make_summary_message=lambda rendered, structured, covers: ChatMessage(
                role="summary",
                content=rendered,
                ts=_now_iso(),
                meta={"structured": structured, "covers_through_seq": covers},
            ),
            render_summary=render_summary_for_storage,
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
            events=self._audit_events,
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
            events=self._audit_events,
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
            event_log=self._audit_events,
            put_outbox=self._put_outbox,
            append_history=self._append_history_for_handler,
            # FP-0050 / #1862 (EP7): fences external peer-answer copies
            # bound for conversation context (history sink only).
            threat_scan=self._safety.threat_scan,
        )
        intervention_coordinator = InterventionCoordinator(
            registry=interventions,
            handler=intervention_handler,
            events=self._audit_events,
        )

        # session.py refactor PR-4 (FP-0019 series final): ChainTimeoutGlue owns
        # chain timeout lifecycle.
        from reyn.runtime.services.chain_timeout_glue import ChainTimeoutGlue
        chain_timeout_glue = ChainTimeoutGlue(
            append_history_fn=self._append_history,
            events=self._audit_events,
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
        ``self._audit_events``, plus early params/properties) or a deferred
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
            event_log=self._audit_events,
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
            # Proposal 0067 P1' (#3978): same mutable-ref-owned-by-Session
            # pattern as the two pairs above.
            get_current_task=lambda: self.current_task,
            set_current_task=lambda v: setattr(self, "current_task", v),
            # #2103 S1bc-exec: read this session's LIVE sid (spawned sessions are stamped
            # post-construction, so a cached value would be stale) for the responder_sid
            # tag; + the trusted spawned-task lookup for rendering a returning result.
            session_id_fn=lambda: self._session_id,
            lookup_spawned_task=self.lookup_and_evict_spawned_task,
            # Proposal 0067 P4e (#3978): task_settled dispatch for a settled
            # kind="prompt" chain — this module's own "no direct reference
            # to Session" design constraint means InterAgentMessaging can't
            # call dispatch_external_event itself, so it's injected here.
            dispatch_task_settled=self.dispatch_external_event,
        )
        return inter_agent_messaging

    def _build_memory(self) -> "MemoryService":
        """#3082 Family 8b: build ``memory``. Byte-identical extraction of the
        construction that used to run inline in ``__init__`` — same object,
        same keyword args, same (unmoved) position.

        Most args are an eager ``self._X`` (Family 1's ``self._audit_events``,
        already set on ``self`` by this point) or a bound method / property
        already available at construction time (``self._file_write`` /
        ``self._file_read`` / ``self._file_delete`` /
        ``self._file_regenerate_index`` / ``self.workspace_dir``).

        ★ ONE deferred lambda, and it is required: ``knowledge_sync``'s
        ``op_context_fn`` resolves ``self._router_host`` at CALL time. The
        waist (Family 6a) has not run yet at this builder's call site — see
        the PRE-WAIST note below — so an eager read would raise
        ``AttributeError`` here; and even after the waist exists, an OpContext
        is per-turn state that must not be snapshotted (#3607: this is the
        SAME context factory ``RouterLoop._remember`` used to reach through
        ``self.host.make_router_op_context()``, unchanged).

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
            events=self._audit_events,
            file_write=self._file_write,
            file_read=self._file_read,
            file_delete=self._file_delete,
            file_regenerate_index=self._file_regenerate_index,
            # FP-0050 / #1822 S4a: the memory-write threat scan config — the
            # same ``self._safety.threat_scan`` the adapter gets for the
            # tool-result legs of the same guard.
            threat_scan=self._safety.threat_scan,
            knowledge_sync=MemoryKnowledgeSync(
                op_context_fn=lambda: self._router_host.make_router_op_context(),
                events=self._audit_events,
            ),
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
        are ``lambda`` closures that resolve ``self._audit_events`` /
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
        # #2597 S2a/S2b/H1: held-open MCP connection; emit_sink /
        # tools_cache_invalidate / hook_trigger stay deferred lambdas — eager
        # binding raises AttributeError here (session-construction.md#mcpconnectionservice-four-deferred-lambdas-over-not-yet-built-siblings-2597).
        from reyn.mcp.connection_service import MCPConnectionService
        mcp_connection_service = MCPConnectionService(
            emit_sink=lambda et, **d: self._audit_events.emit(et, **d),
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
                registry = load_hooks(combined + layer, composed_schemas)
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

    def _build_composer_pending_store(self, composer_defs: list):
        """#3180: the crash-durable ``PendingStore`` shared by every ``durable``
        composer of this session (``op=deadline`` by default) — ``None`` when no
        definition asks for durability, so a durability-free session pays no
        file at all.

        It lives in the PER-SESSION state dir (the same dir
        :meth:`_toggle_store_dir` / ``_read_per_session_hooks`` use), not the
        shared ``.reyn/state/``: composers are per-session, and two sessions of
        the same agent arming the same composer name would otherwise overwrite
        each other's armed set. ``retain_composers`` drops restored records
        whose composer no longer exists in config, so a renamed deadline cannot
        leave an arm nothing will ever disarm."""
        from reyn.hooks.durable_pending_store import STORE_FILENAME, DurablePendingStore
        durable_names = {d.name for d in composer_defs if d.durable}
        if not durable_names:
            return None
        store = DurablePendingStore(Path(self._snapshot_path).parent / STORE_FILENAME)
        store.retain_composers(durable_names)
        return store

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
        for removed in self._runtime_cron_names - new_names:
            if await sched.remove_job(removed):
                changed = True
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
            old_map = {s.name: s for s in (self._available_skills or [])}
            if all(
                new_s.description == old_map[new_s.name].description
                and new_s.path == old_map[new_s.name].path
                and new_s.enabled == old_map[new_s.name].enabled
                and new_s.visibility == old_map[new_s.name].visibility
                for new_s in new_skills
            ):
                return False
        self._available_skills = new_skills or None
        self._capability_visibility.reapply_skill_visibility()
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
            return False
        # #3607: ONE holder. The router OpContext's ``allowed_mcp`` is read off
        # this attribute at build time, so assigning it IS reapplying the gate.
        # There used to be a second assignment here, onto the adapter — dead
        # since #3482 moved the adapter's ``allowed_mcp`` into a FROZEN bundle:
        # it created an attribute nothing read, so the narrowing reached the
        # Session door and silently not the registry-dispatch one.
        self._allowed_mcp = prof.allowed_mcp
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
        self, target_session_id: str, kind: "TurnOrigin", payload: dict, *, wake: bool
    ) -> None:
        """#2072: deliver a hook push to ANOTHER session of this agent (cross-session push).

        The canonical wake-triple (``resolve_session`` / ``get_session`` → ``_put_inbox`` →
        ``ensure_session_running``) — the same pattern webhook_routing uses. A
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

    async def _deliver_cross_session_message(
        self, *, target_agent: str, target_session_id: str,
        kind: "TurnOrigin", payload: dict, wake: bool,
    ) -> bool:
        """Proposal 0067 P5 (#3978): deliver a message to a LIVE session of ANY
        agent (not just this one) — the substrate ``send_to_session`` drives.

        Same canonical wake-triple as ``_cross_session_hook_put`` (resolve →
        ``_put_inbox`` → ``ensure_session_running``), generalized to an
        explicit ``target_agent`` instead of always ``self.agent_name``
        (``AgentRegistry.get_session``/``resolve_session`` already take an
        agent name — the hook-push method just never needed to pass a
        different one).

        Unlike the hook push, this is NOT fire-and-forget: it returns
        ``True``/``False`` so the calling tool handler can report failure to
        the LLM instead of a silent drop (delegate_to_agent's B33 W5 F2
        precedent — a success-shaped envelope for a message that never
        arrived invites the LLM to fabricate a reply on the peer's behalf).

        Delivery-only, deliberately: a target naming no LIVE session returns
        ``False`` rather than loading/spawning one — ``send_to_session`` pairs
        with an already-running peer (ADR-0040 D5's "tap the shoulder"), it is
        not a spawn primitive.
        """
        reg = self._registry
        if ":" in target_session_id:
            transport, _, native = target_session_id.partition(":")
            target = reg.resolve_session(target_agent, transport, native)
        else:
            target = reg.get_session(target_agent, target_session_id)
        if target is None:
            return False
        await target._put_inbox(kind, payload)
        if wake:
            reg.ensure_session_running(target_agent, target_session_id)
        return True

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
        durable record will never land.

        #2280: the FIRST time this latches, also emit a ``session_halted`` audit-event (guarded by
        ``self._halted_reason is None`` so a durability-dead session that keeps rejecting further
        ops does not re-emit on every subsequent submit) — the observability half of the halt. The
        raise above is unconditional and IS the safety mechanism (synchronous, on every call, no
        gating); this emit is purely so an operator surface (TUI status line / plain bottom
        toolbar) can proactively show the reason instead of only learning it from the exception
        text on the operator's own next interaction."""
        if self._state_log is not None and self._state_log.durability_failed:
            if self._halted_reason is None:
                self._halted_reason = "durability_failure"
                self._audit_events.emit("session_halted", reason=self._halted_reason)
            raise DurabilityHaltError(
                f"agent '{self.agent_name}' halted: persistent durability failure — the agent "
                "stopped accepting operations to avoid silent unbounded loss (in-memory state must "
                "not race ahead of a dead disk)"
            )

    async def _put_inbox(self, kind: "TurnOrigin", payload: dict) -> str:
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
        # #2259 PR-3: fail-stop ACCEPT-edge — see docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal (`_halted_reason`).
        self._fail_stop_if_durability_dead()
        msg_id = await self._journal.append_inbox(kind=kind, payload=payload)
        full_payload = {**payload, "_msg_id": msg_id}
        await self.inbox.put((kind, full_payload))
        return msg_id

    def queued_user_messages(self) -> "list[dict]":
        """Read-only accessor: the current UNDISPATCHED ``kind=="user"`` inbox
        queue — the server-authoritative sent-queue state a client renders
        (#3300 P2a; rendering itself is P2b).

        Reads ``self._journal.snapshot.inbox`` — the SAME snapshot-backed,
        WAL-durable list ``append_inbox``/``consume_inbox``
        (``runtime/services/snapshot_journal.py``) keep current, so this is
        exposure of existing server-authoritative state, not a new one. Only
        ``kind=="user"`` items are surfaced (the sent-queue concept covers
        top-level user submissions; ``agent_request``/``agent_response``/
        ``pipeline_result`` inbox items are internal wake triggers, never
        rendered as a queued user message).

        #3595 step 1b narrowed what reaches this filter rather than changing the
        filter: text pushed by an external transport (``external_message``) or a
        cron fire (``cron``) used to claim ``kind="user"`` and therefore appeared
        here. It no longer does, and that is the same sentence as before — the
        sent-queue renders what THIS operator submitted from a client, and a
        Slack peer's message was never that.

        Each item: ``{"msg_id": str, "chain_id": str | None, "text": str | None,
        "meta": dict}`` — ``msg_id``/``chain_id`` are the correlation ids a
        client matches against the ``user_submitted`` (enqueue) /
        ``turn_started`` (dispatch) audit-event deltas to keep its queue model
        in sync. ``meta`` (#3300 P2b co-vet fix) is the SAME ADR-0039
        attribution ``submit_user_text`` stamps on the ``user_submitted``
        event (``_user_frame_meta`` — now ALSO stored on the inbox payload,
        see ``submit_user_text``) — carrying it here is what lets a client
        that seeds its queue view from THIS snapshot (rather than only the
        live delta) still render the correct ``[actor]`` prefix once the
        item promotes to a flow entry.
        """
        return [
            {
                "msg_id": item.get("id"),
                "chain_id": item.get("payload", {}).get("chain_id"),
                "text": item.get("payload", {}).get("text"),
                "meta": item.get("payload", {}).get("meta") or {},
            }
            for item in self.journal.snapshot.inbox
            if item.get("kind") == TurnOrigin.CLIENT_INPUT
        ]

    async def cancel_queued(self, msg_id: str) -> bool:
        """#3300 P3 (Y-server): cancel-by-id for an UNDISPATCHED (queued) user
        message — server-authoritative, WAL-durable. A DIFFERENT intent from
        :meth:`cancel_inflight` (which stops the currently RUNNING turn) —
        never escalated between the two.

        Three owner-ratified semantics (issue #3300 architect design pass):

        - **queued (undispatched) → removed.** If ``msg_id`` is still in the
          inbox (``SnapshotJournal.cancel_inbox`` finds it in
          ``snapshot.inbox``), it is synchronously pruned from the snapshot
          AND a WAL ``inbox_cancel`` tombstone is recorded (★§1 below); its
          msg_id is recorded for skip-at-consume (the physical
          ``asyncio.Queue`` entry cannot be removed in place — no such API —
          so it is discarded, never dispatched, whenever it is eventually
          dequeued: see ``_consume_inbox``/``_drain_to_wake``); and an
          ``inbox_cancel`` audit-event delta is emitted, seq-stamped like
          ``user_submitted``/``turn_started`` (the sent-queue order-race-gate
          token, ``_bump_queue_seq``) — the server-authoritative removal
          signal every attached client (local + remote/agui) applies to its
          sent-queue view.
        - **already DISPATCHED → no-op.** If ``msg_id`` is absent from
          ``snapshot.inbox`` (``consume_inbox`` already pruned it when the
          turn dispatched), this returns ``False`` — a no-op. Cancelling an
          already-running (or completed) turn is deliberately NOT escalated
          to :meth:`cancel_inflight` — that is a distinct user intent this
          method never invokes.
        - **idempotent.** A second cancel of the same ``msg_id`` finds it
          already absent (pruned by the first call) → no-op — safe for an
          at-most-once reconnect retry (a client that is unsure whether its
          first cancel POST landed can safely resend).

        ★§1 (CLAUDE.md recovery-feature PR gate / architect design-pass
        contract correction — the load-bearing point): the inbox is
        snapshot-backed, not purely WAL-event-derived (``restore_all`` loads
        ``snapshot.json`` and replays only the WAL tail above its
        ``applied_seq``). A WAL ``inbox_cancel`` tombstone ALONE would not
        survive truncation below this item's ``inbox_put`` event — the
        snapshot would still hold it, resurrecting it on restore. Pruning the
        snapshot HERE, synchronously, at cancel-record time (via
        ``SnapshotJournal.cancel_inbox``, mirroring ``consume_inbox``'s
        shape) is what makes cancellation survive that truncation — see
        ``tests/interfaces/test_3300_p3_cancel_by_id.py``'s truncate-falsify gate (and
        its strip-falsify: skipping the snapshot-prune resurrects the
        "cancelled" item post-truncation).

        ★F (no-await critical section, design-pass pin F): the queued/
        dispatched judgement, the cancelled-set record, the snapshot
        mutation + WAL tombstone (``cancel_inbox``), and the delta emit
        (``EventLog.emit`` — a plain synchronous call) all happen with NO
        real ``await`` suspension in between — ``cancel_inbox`` is
        ``async def`` for call-site symmetry with ``append_inbox``/
        ``consume_inbox`` but is internally fully synchronous (the same
        ``_wal_append_nowait``/``save_nowait`` fire-and-forget pair those
        use), so awaiting it never yields control back to the event loop.
        This is what makes the "queued XOR dispatched" exit exclusive: no
        other task — in particular the dispatcher's own dequeue-then-promote
        sequence in ``_drain_to_wake``/``run_one_iteration`` (which, for a
        ``kind=="user"`` trigger, likewise has no suspension between its own
        dequeue and its ``turn_started`` emit) — can interleave between this
        method's "still queued?" check and its commit. See
        ``tests/interfaces/test_3300_p3_cancel_by_id.py``'s cancel-during-dequeue race
        test.
        """
        cancelled = await self._journal.cancel_inbox(msg_id=msg_id)
        if not cancelled:
            return False
        self._inbox_arbiter.cancelled_msg_ids.add(msg_id)
        self._audit_events.emit(
            "inbox_cancel", msg_id=msg_id, seq=self._bump_queue_seq(),
        )
        return True

    async def _commit_mid_turn_injection(self, msg_id: str) -> None:
        """#3792: commit (pop) the item the most recent successful
        ``peek_mid_turn_injection()`` call returned.

        The atomic unit architect's design calls unbreakable — history
        append, journal consume (SSoT prune + WAL tombstone), and the
        sent-queue "1 delta" promote — all three, or none. All three are
        synchronous in practice (``_append_history`` is a plain file write;
        ``SnapshotJournal.consume_inbox`` is ``async def`` for call-site
        symmetry but never actually suspends — see its own docstring;
        ``EventLog.emit`` is a plain call), so — mirroring
        ``cancel_queued``'s own "no-await critical section" pin (★F there)
        — there is no real ``await`` suspension between them for another
        task to interleave into.

        Reuses the SAME ``turn_started`` audit-event shape the ordinary
        turn-boundary promote uses (``run_one_iteration``) rather than a new
        kind — the sent-queue's ``_handle_turn_started_event`` already
        matches by ``chain_id`` and does not care whether this is the FIRST
        ``turn_started`` for that chain_id or an extra one fired mid-turn
        (architect: "new state does not grow, only the trigger count does").
        Deliberately does NOT touch ``_hook_driven_turns`` or dispatch
        ``turn_start`` hooks — a mid-turn injection rides inside the
        ALREADY-running turn's budget/lifecycle, it does not start a new one
        (architect's point 4: the loop valve must not reset).

        No-op (raises nothing, commits nothing) if ``msg_id`` does not match
        the currently held ``self._inbox_arbiter.pending_inbox_item`` —
        defensive: this would only happen if the caller calls commit
        without a matching prior peek, which is a caller bug, not a runtime
        condition to paper over silently as a success.
        """
        if self._inbox_arbiter.pending_inbox_item is None:
            return
        kind, payload = self._inbox_arbiter.pending_inbox_item
        held_msg_id = payload.get("_msg_id") if isinstance(payload, dict) else None
        if held_msg_id != msg_id:
            return
        self._inbox_arbiter.pending_inbox_item = None
        self._append_history(ChatMessage(
            role="user", content=payload.get("text") or "", ts=_now_iso(),
            meta=payload.get("meta") or {},
        ))
        await self._journal.consume_inbox(msg_id=msg_id)
        self._audit_events.emit(
            "turn_started",
            kind=kind,
            chain_id=payload.get("chain_id"),
            seq=self._bump_queue_seq(),
        )

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
        # #3097: no explicit pipeline_registry hand-off — the spawned
        # driver rebuilds via its own _reapply_pipelines seam; re-adding a
        # hand-off reintroduces the #3094 stale-copy risk (pipeline-registration.md#spawned-pipeline-driver-registry-no-explicit-hand-off-3097).
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
        # #1800 slice 4b: restore the staged next-turn-context buffer — see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode (`InboxArbiter.next_turn_context`).
        self._inbox_arbiter.next_turn_context = [
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
                # Restored interventions do NOT re-emit intervention_dispatched —
                # re-adding it duplicates the WAL record from the original run
                # (R-D12; 0016-durable-answer-buffer.md#restore-path-re-enqueue-does-not-re-emit-intervention_dispatched-r-d12).
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
        self._audit_events.emit(
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
        # #2259 PR-3 / #2280: fail-stop PROCESS-edge — see docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal (`_halted_reason`).
        if self._state_log is not None and self._state_log.durability_failed:
            if self._halted_reason is None:
                self._halted_reason = "durability_failure"
                self._audit_events.emit("session_halted", reason=self._halted_reason)
            return False
        # #1800 slice 4a/4b: drain up to the first wake=true trigger.
        # ride_alongs holds wake=false C messages accumulated before the
        # trigger.  They are already staged durably by _drain_to_wake (4b);
        # no further persist needed here.
        ride_alongs, trigger = await self._inbox_arbiter.drain_to_wake()
        if trigger is None:
            # shutdown sentinel
            return False
        kind, payload = trigger
        # proposal 0060 Phase 1 (A7): stamp per-turn provenance — see docs/reference/runtime/session-construction.md#safety-limits-interactive-mode (`_current_turn_origin`).
        self._stamp_execution_context(kind, payload)
        # #1800 slice 7: the loop valve (bound hook self-continuation) — see docs/concepts/runtime/hooks.md#loop-valve.
        if kind == TurnOrigin.CLIENT_INPUT:
            self._hook_driven_turns = 0
            # #2884: snapshot-back the counter so a crash+restart does not hand a
            # near-cap self-wake loop a free fresh budget window (see restore_state).
            await self._journal.record_hook_driven_turns(count=0)
        elif kind == TurnOrigin.HOOK:
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
        # FP-0041 (#489) PR-A: surface a sender change as a state_change
        # entry — removing this call collapses multi-consumer attribution
        # into one undifferentiated feed (authoring-guide.md#sender-attribution-as-state_change-fp-0041-489).
        self._inbox_arbiter.handle_sender_attribution(payload)
        # #1800 slice 5a: turn lifecycle audit event (P6). Emitted after the
        # trigger is consumed and before dispatch, so slice 5b can attach the
        # turn_start hook here. chain_id from the payload (may be absent for
        # non-user triggers — that is fine, kind alone identifies the turn type).
        # #3300 P2a: `seq` is the sent-queue mutation/order-race-gate token — see docs/reference/runtime/agui-transport.md#reynevent (`turn_started` / `user_submitted`).
        self._audit_events.emit(
            "turn_started",
            kind=kind,
            chain_id=payload.get("chain_id"),
            seq=self._bump_queue_seq(),
        )
        # #1800 slice 5b: turn_start lifecycle hooks.
        await self._hook_dispatcher.dispatch(
            "turn_start",
            build_hook_payload(
                "turn_start", agent_name=self.agent_name,
                kind=kind, chain_id=payload.get("chain_id"),
            ),
        )
        # ADR-0038 Stage 1c: busy until this turn settles — see docs/reference/runtime/session-construction.md#family-2-recovery-wal-journal (`_turn_idle`).
        self._turn_idle.clear()
        # #2242: turn body runs as its OWN sub-task so cancel_inflight() can
        # abort a mid-generation await — confusing a self-issued hard-cancel
        # with an external one makes the WAL and the runtime diverge
        # (0038-user-facing-time-travel-rewind.md#turn-body-sub-task-enables-hard-cancel-2242).
        self._turn_owner_task = asyncio.create_task(self._run_turn_body(kind, payload))
        _cancelled = False
        try:
            try:
                try:
                    await self._turn_owner_task
                except asyncio.CancelledError:
                    # #3377: the question here is WHOSE cancellation this is,
                    # and the authoritative answer is not a flag — it is
                    # whether THIS (the driver) task is itself being
                    # cancelled. ``Task.cancelling() > 0`` is true only when
                    # ``cancel()`` was called on the current task, which is
                    # exactly the FP-0013 §ADR-A case (an anyio scope teardown
                    # cancelling the MCP/A2A request-handler task that pumps
                    # ``run_one_iteration``). The same discriminator is already
                    # used by ``reyn.mcp.pool.is_real_control_flow`` and
                    # ``reyn.core.cancellable.race_cancellable``.
                    #
                    # Before #3377 this branched on ``_turn_cancel_self_initiated``
                    # alone, which conflates two very different things: a
                    # cancel aimed at THIS task, and a cancel aimed at the
                    # per-turn SUB-task by anyone who did not go through
                    # ``cancel_inflight()`` (Ctrl-C plumbed to the turn, a
                    # timeout, a stop-world operation, whatever is added
                    # next — #3369 fixed one such source, not the class).
                    # The second re-raised, which ended the while-loop in
                    # ``run()``: the inbox kept being PUT to and was never
                    # CONSUMED again, with no error log, indistinguishable
                    # from a permanent hang.
                    _driver = asyncio.current_task()
                    if _driver is not None and _driver.cancelling() > 0:
                        # A cancel genuinely directed at this task = a real
                        # shutdown. It must still stop the loop; structured
                        # concurrency requires the cancelled task to end.
                        raise
                    # Only the per-turn sub-task was cancelled. #2242
                    # WAL-invariant 1 holds either way: CancelledError unwound
                    # the turn-body task straight out of whatever await it was
                    # suspended on (mid-generation: the LLM await), so every
                    # statement after that await never executes and the
                    # cancelled turn's result is never appended. Swallow (do
                    # NOT re-raise) so the driver task — and thus the agent —
                    # survives to serve the next turn.
                    if not self._turn_cancel_self_initiated:
                        # #3377: a turn-scoped cancel we did not initiate is
                        # survivable but NOT expected — record it, so this
                        # never again presents as an unexplained silent stall.
                        logger.warning(
                            "turn sub-task for kind=%s chain_id=%s was cancelled by "
                            "something other than cancel_inflight(); the turn is "
                            "abandoned but the run-loop survives to serve the next "
                            "message.",
                            kind, payload.get("chain_id"),
                        )
                    else:
                        # #3694: the receiver for when a hard cancel
                        # (cancel_inflight()'s Task.cancel(), the common
                        # mid-LLM-call Ctrl+C case) injects CancelledError
                        # at whatever await the turn was suspended on —
                        # this unwinds straight past RouterLoop's own
                        # cooperative-cancel terminal (router_loop.py's
                        # `if _loop_cancelled:` block, which never runs for
                        # a hard cancel: zero CancelledError handling
                        # anywhere in that module). Gated on
                        # ``_turn_cancel_self_initiated`` — a cancel from
                        # something OTHER than cancel_inflight() (the `if`
                        # branch above) is NOT a user cancel and must not
                        # be recorded as one.
                        self.notify_turn_cancelled(payload.get("chain_id"))
                    _cancelled = True
            finally:
                # #2242 Finding 1: reset UNCONDITIONALLY here, not per-branch above — if the
                # CancelledError never actually landed this tick, a per-branch reset leaves the
                # flag stuck True, so the NEXT turn's real external cancel gets misclassified as
                # self-initiated and silently swallowed instead of re-raised.
                self._turn_cancel_self_initiated = False
                self._turn_owner_task = None
                self._turn_idle.set()
                # Symmetric turn-end lifecycle event. turn_completed fires only on
                # the router path; turn_settled fires for EVERY turn kind (including
                # slash / intervention short-circuits that return before the router),
                # giving UI working-indicators driven by turn_started a reliable
                # clear signal regardless of how the turn ended.
                self._audit_events.emit(
                    "turn_settled", kind=kind, chain_id=payload.get("chain_id"),
                )
            if _cancelled:
                # #2242 WAL-invariant 2: joins sibling fire-and-forget WAL-append tasks before
                # returning idle — safe here (no self-deadlock) because `_turn_idle` is already
                # `.set()` and `current_task()` isn't `_turn_owner_task`, so `await_quiescent()`
                # takes the already-set branch and returns immediately.
                await self.await_quiescent()
        finally:
            # 0062: outer `finally` (not after the try block) — a StructuredOutputError
            # re-raised past the inner finally used to skip this and leak the ephemeral
            # session forever; must run on every exit path, not only the normal return.
            self._maybe_schedule_ephemeral_vanish()
        return True

    async def _run_turn_body(self, kind: str, payload: dict) -> None:
        """#2242: the per-kind turn dispatch, run as ``run_one_iteration``'s
        per-turn cancellable sub-task (``self._turn_owner_task``).

        ``kind`` is annotated ``str``, not ``TurnOrigin``, and that asymmetry with
        the PRODUCER side (``_put_inbox``) is deliberate. What arrives here came
        off ``self.inbox``, and ``restore_state`` repopulates that queue from the
        snapshot's JSON — plain strings, including one a build older than a member
        wrote. Annotating a ``TurnOrigin`` would be a claim this method cannot
        keep. The comparisons still read as members because ``TurnOrigin`` is a
        ``StrEnum``: a restored ``"user"`` and ``TurnOrigin.CLIENT_INPUT`` are the
        same value, so recovery needs no conversion step and an unknown kind falls
        through to no branch exactly as before.

        Byte-identical dispatch to the pre-#2242 inline body (extracted, not
        rewritten) — a NORMAL (non-cancelled) turn behaves exactly as before;
        the only change is WHICH task executes it, so ``cancel_inflight()`` can
        target this task directly with ``asyncio.Task.cancel()`` instead of
        relying solely on the cooperative flag ``RouterLoopDriver`` polls at
        each iteration boundary (too coarse to interrupt a mid-flight LLM
        call — see ``cancel_inflight``'s docstring)."""
        if kind == TurnOrigin.CLIENT_INPUT:
            # #3595 S5: the same arm every other text-bearing member takes.
            # ``_handle_user_message`` used to sit here and short-circuit a
            # ``/``-prefixed line into slash dispatch before the turn; with the
            # interpretation moved client-side there is nothing left for a
            # CLIENT_INPUT-specific entry to do, so it is gone rather than kept
            # as a forwarder.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )
        elif kind == TurnOrigin.AGENT_REQUEST:
            await self._handle_agent_request(payload)
        elif kind == TurnOrigin.AGENT_RESPONSE:
            await self._handle_agent_response(payload)
        elif kind == TurnOrigin.PIPELINE_RESULT:
            # IS-2: an async pipeline driver-session posted its terminal
            # result here (the agent_response mirror — but chainless: the
            # launch returned immediately, so this is a fresh turn, routed
            # exactly like a task wake).
            await self._handle_pipeline_result(payload)
        elif kind == TurnOrigin.AGENT_STEP:
            # A pipeline ``agent`` step's prompt: text a MODEL will read as this
            # ephemeral worker's one turn, never an operator's typed line. It used
            # to arrive as ``kind="user"`` (``session_api.run_agent_step``), which
            # is what put model-authored text through ``_handle_user_message``'s
            # ``startswith("/")`` slash dispatch — every registered slash command
            # executable from model output. Its own kind took it off that entry
            # in step 1; S5 then deleted the entry itself, so the separation no
            # longer carries the protection — no inbox member reaches a slash
            # dispatch, because ``Session`` has none (#3595 / owner: "inbox に
            # つまれたものはスラッシュコマンドとして解釈されない"). The member
            # stays because "who wrote this" is still a real distinction the
            # turn-origin stamp below reads.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )
        elif kind == TurnOrigin.EXTERNAL_MESSAGE:
            # Text that arrived over an EXTERNAL transport: a chat webhook
            # (``gateway.api.push_to_agent`` — Slack / LINE / any ``reyn.webhooks``
            # plugin) or an out-of-process request handler
            # (``mcp.server.send_to_agent_impl``, reached by the MCP
            # ``send_to_agent`` tool and the A2A JSON-RPC router). Both used to
            # arrive as ``kind="user"``, which is the claim
            # ``_handle_user_message`` acted on by handing a ``/``-prefixed line
            # to slash dispatch — so a Slack message reading ``/reset`` executed
            # the command, and anyone able to post to the webhook could run any
            # of the registered slash commands. Step 1b took these producers off
            # that entry; S5 deleted the entry itself, so the hole is closed
            # twice over and by construction (#3595 / owner: "inbox につまれた
            # ものはスラッシュコマンドとして解釈されない"). Slash stays
            # unexposed to these producers by product decision, not by omission
            # (owner, 2026-08-01: 「現時点では slash に公開不要」) — exposing it
            # later means routing them through the shared CLIENT-side slash
            # layer, never re-testing ``startswith("/")`` at a transport.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )
        elif kind == TurnOrigin.CRON:
            # A fired message-based cron job's text. Operator-authored (job
            # config), but authored as the AGENT'S PROMPT and delivered to an
            # unattended session with no client attached — not a line typed at a
            # composer, so it does not claim to be one. Same routing as
            # ``external_message`` above, a separate union member because the two
            # answer "who wrote this" differently; see
            # ``TurnOrigin.CRON``.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )
        elif kind == TurnOrigin.HOOK:
            # E (wake=true) lifecycle-hook push delivered as a turn trigger:
            # a system-role [hook:name] message + one router turn (self-
            # continuation). The attribution + wake binding ride in the
            # payload (race-free; the slice-7 valve can count hook-driven
            # turns, and the audit trail attributes the turn to the hook).
            await self._handle_hook_message(payload)
        elif kind == TurnOrigin.PIPELINE_NUDGE:
            # The empty-text pump that starts an ATTACHED pipeline run
            # (``session_api.run_pipeline_attached``). It claimed CLIENT_INPUT
            # until #3595 S2 — a statement about which member the table above
            # runs a turn for, not about who wrote the message; nobody did.
            # Routing it here rather than through ``_handle_user_message`` was
            # what the rename BOUGHT, and it was behaviour-neutral for this
            # producer specifically: the only step in between was
            # ``text.startswith("/")``, and this producer's text is always
            # ``""``. That is why the slash defect could never surface here,
            # and why the fix was not "close a hole" but "stop the one honest
            # member from having two meanings". S5 has since deleted that step
            # entirely, so every text-bearing member now takes this same call.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )
        elif kind == TurnOrigin.PEER_SESSION:
            # Proposal 0067 P5 (#3978): a peer session's text, delivered by
            # send_to_session(wake=True) or run_prompt(collect="attached") —
            # see TurnOrigin.PEER_SESSION's own docstring for why the two
            # share this member. Same routing as EXTERNAL_MESSAGE/CRON above:
            # the sender-attribution path (_handle_sender_attribution, run
            # unconditionally before dispatch) already surfaces the
            # "[context shift] ..." framing generically from the payload's
            # own sender/reply_to fields — this branch does not need its own.
            await self._handle_inbox_text(
                payload.get("text", ""),
                chain_id=payload.get("chain_id") or new_chain_id(),
            )

    def _maybe_schedule_ephemeral_vanish(self) -> None:
        """#2103: schedule the ephemeral auto-vanish teardown once this session's turn
        is idle-done. Thin forwarder — see ``SpawnTracker._maybe_schedule_ephemeral_vanish``
        for the full rationale (#3133 P3 Extract Class)."""
        self._spawn_tracker._maybe_schedule_ephemeral_vanish()

    def _stamp_execution_context(self, kind: str, payload: dict) -> None:
        """proposal 0060 Phase 1 Layer A (A7): derive ``self._current_turn_origin``
        — the OS-authoritative provenance classification of this turn, threaded into
        ``OpContext.turn_origin`` at both ctx-build sites. Only an explicit
        ``kind == "user"`` turn grants ``"user_directed"``; EVERY other kind —
        hook, pipeline_result, ``agent_step`` (#3595), ``external_message`` /
        ``cron`` (#3595 step 1b), sub-agent
        ``agent_request``/``agent_response``, or
        any future kind this method does not yet know about — resolves to the
        strictER ``"auto_improvement"``. This is an if/else fail-safe, not a
        lookup table: there is no path by which an unmapped kind can silently
        fall through to ``"user_directed"`` (0060 §2.7 — that would let an
        autonomous turn bypass the Phase-4 auto-improvement gate). Sub-agent
        turns are deliberately `"auto_improvement"` (lead-adjudicated, Addendum
        B A7): a human directed the PARENT task, not necessarily this install
        action. #3595 made a pipeline agent step's prompt turn carry its own
        kind instead of impersonating ``"user"``, so it now lands on that same
        stricter side — by the rule above, not by a new branch, and matching the
        sub-agent reasoning verbatim (the human directed the pipeline, not this
        step's install)."""
        self._current_turn_origin = (
            "user_directed" if kind == TurnOrigin.CLIENT_INPUT else "auto_improvement"
        )

    async def _handle_pipeline_result(self, payload: dict) -> None:
        """IS-2: surface an async pipeline's terminal result (``pipeline_result``)
        to the LLM as one router turn — the driver already formatted the
        OS-framed ``text``.

        #3595 S5 closed a finding recorded here: this path used to go through
        ``_handle_user_message``, so a pipeline's result text was slash
        -interpreted. Nothing protected it but the formatter's ``[pipeline] run
        …`` framing never starting with ``/`` — a property of the formatter, not
        a gate. With no interpretation left in ``Session`` the property is no
        longer load-bearing."""
        await self._handle_inbox_text(
            payload.get("text", ""),
            chain_id=payload.get("chain_id") or new_chain_id(),
        )

    async def _handle_hook_message(self, payload: dict) -> None:
        """#1800 slice 5b: surface an E (wake=true) lifecycle-hook push as one
        router turn (self-continuation). The push is appended as an attributed
        system-role ``[hook:name]`` message — a NEW message (fidelity: never a
        silent mutation of an existing one) using the shared
        ``_format_ride_along_attribution`` helper (``kind="hook"`` at this call
        site, always — a hook push by construction) so C and E cannot drift —
        then a single router turn runs."""
        name = payload.get("name", "hook")
        text = payload.get("text", "")
        chain_id = payload.get("chain_id") or new_chain_id()
        self._append_history(ChatMessage(
            role="system",
            content=_format_ride_along_attribution(TurnOrigin.HOOK, name, text),
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
        self._audit_events.emit("chat_started", agent_name=self.agent_name, model=self.model)
        # #1800 slice 5a: session lifecycle audit event (P6). Emitted alongside
        # chat_started; marks the boundary of the session's resource scope so
        # slice 5b can attach the session_start hook here.
        self._audit_events.emit("session_started", agent_name=self.agent_name)
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
        except asyncio.CancelledError:
            # #3377: the run-loop may legitimately be stopped by a cancel
            # (a real shutdown), but it must never stop SILENTLY. Without
            # this, a cancelled loop emitted exactly the same
            # ``chat_stopped`` / ``session_completed`` pair as a clean
            # shutdown, so "the agent stopped consuming its inbox" was
            # indistinguishable from "the agent finished" — while the
            # inbox kept accepting puts nobody would ever read. Reuses the
            # #2280 ``session_halted`` surface (already on the transport
            # forward-allowlist and rendered by both the TUI status line
            # and the plain-CUI bottom toolbar) rather than inventing a
            # parallel signal. Guarded on ``_halted_reason is None`` for
            # the same reason #2280's own emits are: at most one halt
            # notice per session.
            if self._halted_reason is None:
                self._halted_reason = "cancelled"
                self._audit_events.emit("session_halted", reason=self._halted_reason)
            logger.warning(
                "Session.run() for agent '%s' is ending because it was cancelled — "
                "the run-loop stops here and its inbox will not be consumed again.",
                self.agent_name,
            )
            raise
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
                self._audit_events.emit("chat_stopped", agent_name=self.agent_name)
                # #1800 slice 5a: session lifecycle audit event (P6). Emitted alongside
                # chat_stopped; marks the end of the session's resource scope.
                self._audit_events.emit("session_completed", agent_name=self.agent_name)
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

        # #1128 PR-a: compaction runs synchronously now, not as a background
        # task — see docs/concepts/data-retrieval/chat-compaction.md#compaction-paths

    async def _handle_inbox_text(self, text: str, *, chain_id: str) -> None:
        """Run ONE turn on ``text`` — the shared body under every text-bearing
        inbox kind, and the ONLY thing ``Session`` does with inbox text (#3595).

        ★ There is no operator-command surface above this method any more. S5
        deleted ``_handle_user_message``, whose entire remaining content was a
        ``text.startswith("/")`` short-circuit into slash dispatch, and with it
        the ``/answer`` pre-queue fast path that was the second entry to the same
        dispatch. Interpreting a string as a command is CLIENT work
        (:mod:`reyn.interfaces.slash.dispatch`), so ``TurnOrigin.CLIENT_INPUT``
        now arrives here exactly like every other text-bearing member — the
        owner's ruling, in its final form: "inbox につまれたものはスラッシュ
        コマンドとして解釈されない。されるんだとするとそれが不具合".

        S1–S3 made a producer unable to CLAIM ``CLIENT_INPUT`` without being a
        declared client-input seam; S5 makes the claim carry no command
        privilege at all, so the two gates are independent rather than stacked:
        even a producer that legitimately claims ``CLIENT_INPUT`` (a webhook
        never can, but ``run-once``'s stdin does) cannot execute a command by
        writing one.

        This body carries the ``:skill`` invocation and the pending-intervention
        answer route. Neither is a registered command (a ``:`` invocation
        composes skill text and falls THROUGH into the turn below; an
        intervention answer is a typed reply to a question this session itself
        asked), which is why they stay here while slash left.
        """
        # #3100: operator-explicit `:skill [:skill2 ...] [trailing]` skill
        # invocation — a namespace separate from `/` (Axis 4: syntactic
        # closed-type distinction, not a runtime precedence lookup). Unlike
        # slash (an OS-executed handler), a successful `:` invocation
        # REPLACES `text` with the composed skill body(ies) + trailing args
        # and falls through into the ordinary router turn below — one turn,
        # one LLM wake, however many skills were stacked (Axis 2/3).
        if text.lstrip().startswith(":"):
            skill_consumed, skill_text = await self._maybe_handle_skill_invoke(text)
            if skill_consumed is True:
                return
            if skill_consumed is False:
                text = skill_text or text
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
        if self._inbox_arbiter.next_turn_context:
            for entry in self._inbox_arbiter.next_turn_context:
                # Proposal 0067 P5 (#3978, architect + lead-coder co-vet):
                # entry_kind is the entry's OWN, OS-recorded TurnOrigin (staged
                # by InboxArbiter.stage_next_turn_context straight from the
                # inbox item's kind — trusted, never producer-echoed). It used
                # to be discarded as a mere fallback DEFAULT for entry_name and
                # never reach the formatter's bracket, so every staged
                # producer rendered "[hook:...]" regardless of what it truly
                # was (a real bug this arc's own extraction surfaced: the
                # first non-hook staged producer, send_to_session wake=false,
                # would have carried a false "[hook:...]" label to the LLM).
                # Same TRUSTED-framing discipline InterAgentMessaging.
                # handle_agent_response already applies to its own
                # OS-assigned "[task_completed] kind=..." header — the LABEL
                # is OS state, not producer content.
                entry_kind = entry.get("kind", "hook")
                payload_data = entry.get("payload", {})
                entry_name = payload_data.get("name", entry_kind)
                entry_text = payload_data.get("text", "")
                self._append_history(ChatMessage(
                    role="system",
                    content=_format_ride_along_attribution(
                        entry_kind, entry_name, entry_text,
                    ),
                    ts=_now_iso(),
                ))
            self._inbox_arbiter.next_turn_context.clear()
            await self._journal.record_next_turn_context_cleared()

        # R-D4: WAL size safety-net check at the chat turn boundary — see
        # docs/deep-dives/decisions/0014-wal-size-safety-net.md#decision
        if self._registry is not None:
            asyncio.create_task(
                self._registry.maybe_truncate_for_size(),
                name="wal-size-safety-net",
            )

        # Issue #366 → #383: drain queued /image media blocks onto this turn —
        # see docs/reference/runtime/session-construction.md#multimodal-media
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
        self._audit_events.emit(
            "user_message_received", text=text, chain_id=chain_id,
            media_block_count=len(attached_media),
        )
        # NOTE: no "thinking…" status is emitted here. The turn-in-progress signal
        # is the event-driven working indicator (turn_started → turn_settled, via
        # ChatRenderer.on_audit_event), so a separate "thinking…" status line is a
        # redundant double-display (the inline CUI showed both "· thinking…" and
        # the "Working…" spinner). It was also the source of an orphaned blank line
        # before each reply (a cleared transient leaving its separator behind).

        # Reset the per-turn router cap counter at the top of each fresh
        # user turn. Subsequent in-chain re-invocations (agent_response on
        # this chain, _resolve_pending_chain) accumulate against the same
        # budget without resetting.
        self._reset_router_turn_counter()

        # #3475: the FP-0037 MCP-tools-cache priming chain (yaml refresh / disk
        # reload / lazy probe) used to live HERE — the "user" turn kind only.
        # `_handle_hook_message` and `InterAgentMessaging.handle_agent_request`
        # call `_run_router_loop` directly and never ran this chain, so a
        # session whose FIRST turn arrives as `hook` or `agent_request` (e.g. a
        # freshly `spawn_ephemeral_session`-ed worker driven by an inbound
        # `agent_request`) built its first `tools=` payload against an
        # unprimed (`None`) cache — no `mcp_tool_name` enum, silently, for the
        # rest of that session's life (the populated-guard is one-shot). The
        # chain now lives in `_run_router_loop` itself instead — see that
        # method's docstring for why it is the one seam every turn kind
        # funnels through — so the ordering guarantee is structural (every
        # first LLM call goes through this priming, not just the common case).
        try:
            await self._run_router_loop(text, chain_id)
        except StructuredOutputError:
            # 0062: re-raise, don't fall through — the generic handler below collapses
            # this into an opaque "error" outbox string, losing the 3 distinct failure
            # modes (0062 proposal §2.1) the caller needs to tell apart.
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
            #
            # #4381 stage 1 (B): reyn's own retry_loop re-wraps a real failure into
            # ContextOverflowError/UnrecoveredError at each escalation (router_loop_
            # driver.py's `raise X(...) from original_exc` chain) — so this except's
            # own `exc` is often reyn's OWN wrapper type, not what actually happened
            # (owner's real-environment observation: reyn.log showed only
            # `ContextOverflowError`, while the audit trail's `compaction_shrink_
            # recovered.cause` — a SEPARATE event, from a SEPARATE code path — held
            # the real answer, `APIError`, the whole time). Walking `__cause__` to
            # the end of the chain (every wrap site in that file uses `from`, so the
            # chain is always intact) and naming it explicitly here means an operator
            # reading EITHER `reyn.log` or this ONE audit event's own `cause` field
            # gets the real answer without needing to know a second event exists.
            _root_cause = _deepest_cause(exc)
            _cause_name = type(_root_cause).__name__ if _root_cause is not None else None
            logger.exception(
                "router loop terminated by unhandled exception (chain_id=%s)%s",
                chain_id,
                f" — cause: {_cause_name}: {_root_cause}" if _root_cause is not None else "",
            )
            try:
                self._audit_events.emit(
                    "router_loop_terminated_by_exception",
                    chain_id=chain_id,
                    error_type=type(exc).__name__,
                    error=repr(exc)[:500],
                    cause=_cause_name,
                )
            except Exception:  # noqa: BLE001 — instrumentation must never break the path
                pass
            # #2732: this except is a CATCH-ALL (any LLM-call or router-loop
            # exception, not only cred errors) — the ORIGINAL swallow-to-empty-
            # reply bug. Interactive chat (self._ephemeral is False) keeps the
            # pre-existing render-not-raise behavior below: the classified
            # summary reaches the TUI/renderer as an OutboxMessage, which is
            # correct there. But an agent-step spawn's leaf session IS ALWAYS
            # ephemeral (spawn_ephemeral_session hardcodes mode="ephemeral"),
            # and its ``kind="agent"``-only join in
            # ``session_api.run_agent_step`` silently drops this ``kind="error"``
            # outbox message, returning "" with no exception — the pipeline
            # executor's ``except AgentStepError`` (executor.py) never fires and
            # the step looks like it succeeded with an empty answer. Gate the
            # re-raise on ``self._ephemeral`` (NOT unconditional — an
            # unconditional raise here would break the interactive chat loop,
            # which relies on this method returning normally after queuing the
            # error reply) so only the agent-step-spawn leg gets a typed
            # failure the caller can observe. Re-raise the BASE
            # ``AgentStepError`` (not an LLM-specific subclass) because this
            # catch-all also covers non-LLM exceptions; `from exc` preserves
            # the original exception via chaining (already retained above by
            # the audit event + traceback log for both branches).
            if self._ephemeral:
                raise AgentStepError(classify_router_error(exc)) from exc
            await self._put_outbox(OutboxMessage(
                kind="error", text=classify_router_error(exc),
                meta={"chain_id": chain_id},
            ))
            return

        # #1128 PR-a: post-reply fire-and-forget compaction was removed; the 3
        # compaction paths (pre-frame guard / voluntary op / retry backstop) are
        # documented at docs/concepts/data-retrieval/chat-compaction.md#compaction-paths

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        """Drop transient kinds while nobody is subscribed; durable kinds are queued.

        While ``self.outbox_hub`` has no live subscribers (#3793 stage 2: no
        forwarder attached, no AG-UI/other surface subscribed), `status`/
        `trace` carry no value to a display nobody is reading and would just
        accumulate in the queue. `agent`/`intervention`/`error`/`__end__` are
        kept so they reach the user once a surface subscribes or remain in
        history (history append happens independently in callers).

        FP-0041 #489 PR-D2: outbox reply_to + external transport interceptor.
          - When ``msg.reply_to`` is unset and the session has a recent
            inbox-captured ``self._inbox_arbiter.last_reply_to`` (proposal
            0067 P1, #3978), the outbox message
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
        if msg.reply_to is None and self._inbox_arbiter.last_reply_to is not None:
            from dataclasses import replace
            msg = replace(msg, reply_to=self._inbox_arbiter.last_reply_to)
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
        self._put_outbox_nowait(msg)

    def _put_outbox_nowait(self, msg: OutboxMessage) -> None:
        """The synchronous tail of :meth:`_put_outbox` — everything except the
        external-transport interceptor, which is the method's only await.

        Exists because ``ClientTransport.put_display`` is synchronous by
        contract (``InProcessTransport`` implements it with a bare
        ``put_nowait``), and #3595 S4 routes slash-command replies through that
        seam. ``SessionBoundTransport`` is handed this method as its display
        sink, so a slash reply keeps landing on ``self.outbox`` with the same
        reply-to defaulting and the same detached-drop rule it had when the
        handler called ``_put_outbox`` directly — the S4 seam change moves who
        the handler depends on, not where its output goes.

        ``put_nowait`` rather than ``await put()`` is not a behaviour change:
        ``self.outbox`` is an UNBOUNDED ``asyncio.Queue``, whose ``put()``
        never suspends and delegates straight to ``put_nowait``. Bounding the
        outbox later would break that equivalence — it would make this path
        drop-or-raise where the async one blocks — so a maxsize on
        ``self.outbox`` has to reckon with this method.

        ⚠️ The interceptor leg is deliberately NOT reachable from the client
        seam. It dispatches a message to an external transport (Slack / LINE
        via the web layer) INSTEAD of queueing it for display, keyed on a
        ``reply_to`` inherited from whatever last arrived on the inbox. A slash
        reply is client-authored output for the operator who typed the command,
        and #3595 step 1b already ruled that slash is not exposed to those
        transports at all; letting a sticky external ``reply_to`` divert a
        ``/cost`` line typed in the TUI away from the TUI would contradict that
        ruling rather than preserve behaviour.
        """
        if msg.reply_to is None and self._inbox_arbiter.last_reply_to is not None:
            from dataclasses import replace
            msg = replace(msg, reply_to=self._inbox_arbiter.last_reply_to)
        # #3793 stage 2: the "nobody is watching, drop status/trace" gate
        # (formerly ``if not self.is_attached: return`` here) is now derived
        # from ``self.outbox_hub.has_subscribers()`` instead of a manually-
        # synced bool — ``self.is_attached`` could not express per-connection
        # focus once ADR-0039 D4's N:N model applies. This must be checked
        # HERE, at emission, not left to ``OutboxHub._fanout``'s per-message
        # no-op: ``_fanout`` only runs once ``_drain`` is consuming, and
        # ``_drain`` itself only starts on the FIRST ``subscribe()`` — a
        # session booted via ``ensure_session_running`` (no forwarder; e.g. a
        # persistent ``cron:``/``webhook:`` session, FP-0043) may never be
        # subscribed to at all, so without this earlier gate ``_drain`` never
        # starts and ``self.outbox`` grows unboundedly for the session's
        # whole lifetime (caught in #3813 review).
        if not self.outbox_hub.has_subscribers() and msg.kind in {"status", "trace"}:
            return
        self.outbox.put_nowait(msg)

    # ── compaction helpers (FP-0019 Wave 1) ────────────────────────────────────
    # Business logic lives in CompactionController.  Session keeps only the
    # helpers that are still needed as injected callbacks.

    def _latest_summary(self) -> ChatMessage | None:
        """Return the most recent summary message, or None."""
        for m in reversed(self.history):
            if m.role == "summary":
                return m
        return None

    def _compaction_watermark(self) -> int:
        """The latest summary's ``covers_through_seq`` (0 if none yet) — seqs
        at or below this are considered compacted out. #4387 Phase A:
        factored out of :meth:`_uncompacted_tool_call_records` so
        :meth:`_ephemeral_contextual_for_turn` can bound its own scan the
        same way, rather than duplicating the ``_latest_summary`` +
        ``covers_through_seq`` lookup inline."""
        latest = self._latest_summary()
        return int((latest.meta or {}).get("covers_through_seq", 0)) if latest is not None else 0

    def _uncompacted_tool_call_records(self) -> list[tuple[str, float]]:
        """Return ``(action_name, ts_epoch)`` records from the
        portion of history that has NOT yet been folded into a
        compactor summary.

        Used by RouterLoop to build the hot-list each turn without
        relying on a parallel per-call write log; the compacted table
        in :class:`~reyn.tools.action_usage_tracker.ActionUsageTracker`
        already covers the older portion.
        """
        watermark = self._compaction_watermark()
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

        #3382: falling back used to be *silent* — a single
        ``except Exception: pass`` collapsed "the wrap-up call failed",
        "no LLM is configured / reachable" and "the LLM returned no text"
        into one indistinguishable outcome. The reason is now named
        (``wrap_up_failed: <ExcType>: <msg>`` — which is also how the
        no-LLM-configured case identifies itself, via the provider's own
        exception type — or ``wrap_up_empty``) and logged at WARNING
        before the canned reply is emitted.

        The ``try`` is also narrowed: it no longer wraps the success-path
        emission, so an outbox/history failure *after* a successful
        wrap-up surfaces instead of silently producing a second, canned
        reply. ``BaseException`` (``asyncio.CancelledError`` — see #3377)
        is deliberately not caught: a cancel must propagate, not be
        degraded into a user-visible "budget exhausted" message.
        """
        # #1496: emit audit event + attempt LLM wrap-up
        self._audit_events.emit(
            "limit_denied",
            kind="router_cap",
            count=exc.count,
            cap=exc.cap,
            chain_id=chain_id,
        )
        from reyn.runtime.router_loop import RouterLoop

        _wrapup_text: str | None = None
        _reason_unavailable = ""
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
        _reason = (
            f"router cap exhausted ({exc.count}/{exc.cap})"
            f"{'; last reason: ' + exc.last_reason if exc.last_reason else ''}"
        )
        try:
            _resolved = self._router_host.resolve_model(self.model)
            _wrapup = await _temp_loop._force_close_call_with_retry(
                messages, resolved_model=_resolved, reason=_reason,
            )
        except Exception as _err:  # noqa: BLE001 — named below, then degraded
            _reason_unavailable = f"wrap_up_failed: {type(_err).__name__}: {_err}"
        else:
            _wrapup_text = _wrapup.content or None
            if _wrapup_text is None:
                _reason_unavailable = "wrap_up_empty: the LLM returned no text"

        if _wrapup_text is not None:
            await self._put_outbox(OutboxMessage(
                kind="agent",
                text=_wrapup_text,
                meta={
                    "chain_id": chain_id,
                    "limit_stopped": True,
                    "limit_kind": "router_cap",
                },
            ))
            self._append_history(ChatMessage(
                role="assistant",
                content=_wrapup_text,
                ts=_now_iso(),
                meta={"chain_id": chain_id, "source": "router_cap_exhausted_wrap_up"},
            ))
            return

        logger.warning(
            "router_cap force-close wrap-up unavailable (%s) — chain_id=%s; "
            "falling back to the canned retry-exhausted reply, so this turn's "
            "closing message is generic rather than a summary of what was done.",
            _reason_unavailable,
            chain_id,
        )

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
        of each fresh turn (`_handle_inbox_text`, `_handle_agent_request`).
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
        visible alongside the existing audit events.

        #3053: the bus resolves BRIDGE-AWARE via ``_make_router_intervention_bus``
        (the SAME seam #3052 gave every MCP router-op, and #3053 gave the
        per-LLM-call ``_ChatBudgetBus``) — this checkpoint (``router_cap`` /
        ``max_agent_hops`` / ``chain_seconds``) is reachable on
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
        self._audit_events.emit(
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
            self._audit_events.emit(
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
        self._audit_events.emit(event_type, **data)

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
            self._audit_events.emit(
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
        self._audit_events.emit(
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
             policies (e.g. "router_cap hit + N prior extensions
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
        self_ans = await self.try_self_answer(iv)
        if self_ans is not None:
            self._audit_events.emit(
                "intervention_routed",
                route="self_answer",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            return self_ans

        parent = self.resolve_parent_agent(iv)
        if parent is not None:
            self._audit_events.emit(
                "intervention_routed",
                route="parent_delegate",
                iv_kind=iv.kind,
                iv_id=iv.id,
            )
            # Issue #261: token-based set/reset (not a plain assignment) — a multi-hop
            # delegation chain overwrites `source_agent_var` on each hop and must restore the
            # OUTER caller's value on return, or the chain's own attribution corrupts silently.
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
        self._audit_events.emit(
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
          - "router_cap limit hit + we've already auto-extended
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

    # ── agent-to-agent messaging (PR11 / PR14) ──
    # FP-0019 Wave 2 part 2 extraction to InterAgentMessaging; Session keeps thin
    # delegators so existing call sites resolve unchanged: docs/reference/runtime/session-construction.md#family-8a-inter-agent-messaging

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
        self._audit_events.emit(
            "chain_peer_discarded",
            chain_id=chain_id,
            peer=peer,
            reason=reason,
            waiting_on=waiting,
            # proposal 0067 P4e (#3978): field KEY unchanged (this audit
            # event's payload shape is not part of the closed AUDIT_EVENT_KINDS
            # vocabulary, but .reyn/events has consumers outside reyn — see
            # CLAUDE.md — so only the VALUE's source moved to the
            # materialized requester field, not the emitted key name).
            origin_agent=pending.requester.agent_name,
        )
        try:
            await self._send_agent_response(
                to=pending.requester.agent_name,
                response=error_text,
                depth=pending.origin_depth,
                chain_id=chain_id,
                to_sid=pending.requester.session_id,  # #2130
            )
        except Exception as exc:  # noqa: BLE001 — never wedge the loop
            await self._put_outbox(OutboxMessage(
                kind="error",
                text=f"chain peer discarded: failed to notify upstream: {exc}",
                meta={"chain_id": chain_id},
            ))

    # ── slash command support (no dispatch: that is client-side, #3595 S5) ──────

    def _resolve_intervention_id(self, prefix: str) -> tuple[str | None, list[str]]:
        """Resolve a unique intervention id by prefix in the intervention registry."""
        return self._interventions.resolve_id_prefix(prefix)

    def _slash_context(self) -> "SlashContext":
        """Build what a slash handler is handed when it runs SERVER-side (#3595).

        A slash handler is CLIENT-layer code — the owner's design is that a
        client interprets ``/``-prefixed text and maps it onto published
        operations, and that ``Session`` never interprets a string — so what it
        depends on is ``ClientTransport``, the seam every reyn client already
        writes through. After S5 the interpretation is entirely the client's
        (:mod:`reyn.interfaces.slash.dispatch`), and a LOCAL client passes its
        own transport, never this.

        ★ This survives S5 for the REMOTE case only, and the reason is a
        property of the residue rather than of the dispatch: a ``--connect``
        client holds no ``Session``, so the eleven commands that still read
        session state cannot run on its side of the wire at all. The AG-UI
        endpoint's ``slash_command`` arm — which receives a command NAME the
        client already resolved, never a string to sniff — runs them here and
        builds their context with this method. It goes away when
        ``SlashContext.session`` does.

        ``session=self`` is the declared residue, not a design element — see
        :class:`~reyn.interfaces.slash.SlashContext`.
        """
        from reyn.interfaces.slash import SlashContext
        from reyn.interfaces.transport.session_bound import SessionBoundTransport

        return SlashContext(
            transport=SessionBoundTransport(
                self, display_sink=self._put_outbox_nowait,
            ),
            session=self,
        )

    # NOTE: the slash handlers live in ``src/reyn/interfaces/slash/`` and the
    # dispatch that reaches them is CLIENT-side (#3595 S5,
    # ``interfaces/slash/dispatch.py``). ``_resolve_intervention_id`` /
    # ``_deliver_answer_to`` stay here as session-state helpers the slash
    # modules call back into through ``SlashContext.session`` — the declared,
    # shrinking residue enumerated in ``tests/interfaces/test_3595_s4_slash_handler_seam.py``.

    async def _maybe_handle_skill_invoke(
        self, text: str,
    ) -> "tuple[bool | None, str | None]":
        """Dispatch ``:skill [:skill2 ...] [trailing]`` (#3100 operator-explicit
        skill invocation — see ``reyn.interfaces.skill_invoke`` module docstring
        for the full design). Returns ``(consumed, replacement_text)``:

        - ``(None, None)`` — *text* is not a `:` invocation at all; the caller
          proceeds with the ORIGINAL text unchanged (falls through to the
          intervention router / a fresh turn exactly as before #3100).
        - ``(True, None)`` — recognized as a `:` invocation but it failed
          (unknown skill name) or was a discovery request (bare ``:`` /
          ``:list``); the reply/error was already put on the outbox — the
          caller MUST stop, no router turn fires for this message.
        - ``(False, text)`` — success; the caller replaces its working
          ``text`` with the returned composed skill-body(ies) + trailing
          args and continues into the ordinary turn pipeline below, so
          however many skills were stacked, the model sees them in ONE
          turn (Axis 2: skills are always LLM-wake, never mechanical;
          Axis 3: stacking is "load N SKILL.md bodies into one wake").
        """
        from reyn.interfaces.skill_invoke import (
            invocable_skill_names,
            parse_skill_invocation,
            read_skill_frontmatter_meta,
            resolve_skill_body,
            substitute_arguments,
            suggest_unknown_skill,
        )

        stripped = text.strip()
        if stripped in (":", ":list"):
            known = invocable_skill_names(self._available_skills)
            listing = ", ".join(f":{n}" for n in known) if known else "(none registered)"
            await self._put_outbox(OutboxMessage(
                kind="system", text=f"installed skills: {listing}",
            ))
            return True, None

        parsed = parse_skill_invocation(text)
        if parsed is None:
            return None, None

        known_names = invocable_skill_names(self._available_skills)
        entries_by_name = {
            e.name: e for e in (self._available_skills or []) if e.name in known_names
        }

        resolved = []
        for name in parsed.names:
            entry = entries_by_name.get(name)
            if entry is None:
                # Axis 5 (explicit, actionable error): docs/concepts/tools-integrations/skills.md#operator-explicit-invocation-the-skill-namespace-3100
                suggestions = suggest_unknown_skill(name, known_names=known_names)
                hint = ", ".join(f":{n}" for n in suggestions) if suggestions else "(no skills registered)"
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f"no skill ':{name}' — try: {hint} / :list for every invocable skill",
                ))
                return True, None
            # Axis 4 (config-tier collision, LOUD not silent): docs/concepts/tools-integrations/skills.md#operator-explicit-invocation-the-skill-namespace-3100
            tiers = self._skill_collisions.get(name)
            if tiers:
                self._audit_events.emit(
                    "skill_invoke_collision", name=name, tiers=list(tiers),
                )
                await self._put_outbox(OutboxMessage(
                    kind="system",
                    text=(
                        f"note: ':{name}' is declared in multiple config sources "
                        f"({', '.join(tiers)}); using the most specific one. "
                        "Rename one of them to disambiguate."
                    ),
                ))
            resolved.append(entry)

        project_dir = self._hot_reload_project_root()
        # #3198: build the `${env:VAR}` allowlist decl from `self._perm._config` directly,
        # NOT the wildcarded router-op-context decl — that decl's http_get/secret_write
        # wildcards rely on a runtime prompt `env_expand` lacks; reusing it here silently
        # widens which env vars a `:` invocation can leak into the LLM's context.
        from reyn.security.permissions.permissions import PermissionDecl
        skill_env_decl = PermissionDecl.from_dict(
            self._perm._config if self._perm is not None else None,
        )
        parts: list[str] = []
        for entry in resolved:
            try:
                body = resolve_skill_body(
                    entry.path, project_dir=project_dir, permission_decl=skill_env_decl,
                )
            except (OSError, UnicodeDecodeError) as exc:
                await self._put_outbox(OutboxMessage(
                    kind="error",
                    text=f":{entry.name} failed to load ({entry.path}): {exc}",
                ))
                return True, None
            meta = read_skill_frontmatter_meta(body)
            substituted = substitute_arguments(
                body, trailing=parsed.trailing, arg_spec=meta.arguments,
            )
            parts.append(f"[:{entry.name}]\n{substituted}")
            # Audit trail: mirrors `skill_body_loaded` (the ordinary file-read
            # op's skill-load event, reyn.core.op_runtime.file), scoped to the
            # explicit `:` path specifically so a replay can tell "the model
            # read this on its own" apart from "the operator explicitly asked".
            self._audit_events.emit(
                "skill_invoke_body_loaded", name=entry.name, path=entry.path,
            )

        composed = "\n\n".join(parts)
        if parsed.trailing:
            composed = f"{composed}\n\n{parsed.trailing}"
        return False, composed

    # ── RouterLoop helper methods (Wave 3 F1, kept for session callbacks) ──────────
    # These 3 resolvers remain on Session because the session's internal
    # MCP/file callbacks (_mcp_list_tools, _mcp_call_tool, _file_op) use them,
    # and because the op-context supplier reads them as bound methods (so the
    # roster it advertises follows a mid-session mcp_install). The adapter has
    # its own copies for the surfaces it serves directly.

    def _get_file_permissions_for_router(self) -> dict | None:
        """Return file permissions in the form {read: [paths], write: [paths]}
        for the router's tool catalog. None if no PermissionResolver is wired,
        or when both axes resolve to the empty set.

        #3458: this is a pass-through to
        ``PermissionResolver.advertised_file_permissions()`` — the SAME
        resolution the runtime gate enforces. It used to parse
        ``_perm._config`` here, which knew nothing about the gate's internal
        default zone, so an operator who configured nothing got file tools
        withheld from the model while the gate would have permitted them
        (#3449).
        """
        if self._perm is None:
            return None
        return self._perm.advertised_file_permissions()

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
        """Ask this session's op-context supplier for a chat-router OpContext.

        The internal MCP / file callbacks below (``_file_op``,
        ``_mcp_call_tool`` and siblings) reach op_runtime through here. #3607:
        this used to assemble its own OpContext — the same object
        ``RouterHostAdapter.make_router_op_context`` assembled separately, and
        the two had diverged on twelve fields, so an op's capabilities depended
        on which door it came through. Both are now one call on one supplier.
        """
        return self._router_op_context_source.build()

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

        Returns ``{"path": path, "content": <text>}`` — plus, when the read
        was truncated, the signal fields (``truncated``, ``note``,
        ``next_offset``, ...) forwarded UNTOUCHED (#3193: a truncated read
        still has real content and must not be reported as a failure) — or
        ``{"error": ...}`` only for a genuine failure status.
        """
        result = await self._file_op({"kind": "file", "op": "read", "path": path})
        outcome = classify_op_status(result.get("status"))
        if outcome in ("success", "partial", "unknown"):
            out = {"path": path, "content": result.get("content", "")}
            _forward_file_signal_fields(out, result, outcome=outcome)
            return out
        if result.get("status") == "not_found":
            return {"error": f"file not found: {path}"}
        return {"error": result.get("error", "read failed")}

    async def _file_write(self, path: str, content: str) -> dict:
        """Write a file through op_runtime.

        Returns: {"path": path, "written": True} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "write", "path": path, "content": content})
        outcome = classify_op_status(result.get("status"))
        if outcome in ("success", "partial", "unknown"):
            out = {"path": path, "written": True}
            _forward_file_signal_fields(out, result, outcome=outcome)
            return out
        return {"error": result.get("error", "write failed")}

    async def _file_delete(self, path: str) -> dict:
        """Delete a file through op_runtime.

        Returns: {"path": path, "deleted": bool} or {"error": ...}.
        """
        result = await self._file_op({"kind": "file", "op": "delete", "path": path})
        outcome = classify_op_status(result.get("status"))
        if outcome in ("success", "partial", "unknown"):
            out = {"path": path, "deleted": result.get("deleted", True)}
            _forward_file_signal_fields(out, result, outcome=outcome)
            return out
        return {"error": result.get("error", "delete failed")}

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
        outcome = classify_op_status(result.get("status"))
        if outcome in ("success", "partial", "unknown"):
            out = {
                "path": path,
                "output_path": output_path,
                "entries": result.get("entries", 0),
            }
            _forward_file_signal_fields(out, result, outcome=outcome)
            return out
        return {"error": result.get("error", "regenerate_index failed")}

    # #3447: the five discovery-only mcp_list_* methods (servers / tools /
    # resources / resource_templates / prompts) FOLDED onto RouterHostAdapter —
    # see ``RouterHostAdapter._mcp_list_via_gateway`` /
    # ``RouterHostAdapter._mcp_resolve_server_config`` /
    # ``RouterHostAdapter.mcp_list_*``. Unlike the call-family methods below
    # (read/subscribe/unsubscribe/get_prompt/call_tool), the listing methods
    # never touched permission-gated ``execute_op`` state Session alone
    # holds — the adapter already duplicated the two inputs they needed
    # (``_mcp_servers_flat`` / ``_get_mcp_servers_for_router``), so moving the
    # gateway-calling logic there too let the corresponding 5 constructor
    # callbacks (``mcp_list_servers`` / ``mcp_list_tools`` /
    # ``mcp_list_resources`` / ``mcp_list_resource_templates`` /
    # ``mcp_list_prompts``) drop from ``RouterHostAdapter.__init__`` entirely
    # (#3409's "17 callbacks" constructor-width finding). The op layer now
    # RAISES ``Cancelled``/``MCPFault`` instead of catching them locally; the
    # catch moved to ``tools/mcp.py``'s ``_handle_list_mcp_*`` handlers, at
    # the same position the ``_mcp_list_error`` sentinel-check already sat —
    # architect firm (#3411, 2026-07-29): no context-manager / audit-emit /
    # pool-teardown sits between the raise site and either catch position, so
    # this is behavior-preserving, not a contract change.

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

    # #3447: _mcp_list_prompts folded onto RouterHostAdapter.mcp_list_prompts —
    # see the note above ``_mcp_read_resource``.

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
        dropdown (the status bar reads only public accessors, via
        interfaces/repl/status.py's ``_snapshot``).

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
        """Forwarding → RouterLoopDriver.run_turn (PR-3).

        #3339: this is the seam where the turn's ``chain_id`` becomes the
        AMBIENT turn identity for everything the turn does — every LLM call
        the router loop makes (including each tool-loop iteration and any
        compaction triggered inside the turn) reads it at the cost chokepoint
        and files its tokens/cost under this turn. The scope is bound here
        rather than at ``turn_started`` because this is the single point every
        turn kind (user / hook / agent_request / pipeline_result) funnels
        through WITH its chain_id in scope, and it closes exactly where the
        turn's work ends.

        A SUB-AGENT's turn arrives here as ``agent_request`` and REBINDS to
        its own chain_id, so it is billed to its own turn rather than the
        parent's — even though its task inherited the parent's chain_id at
        spawn. Work that never reaches this seam sees no turn and is recorded
        unattributed instead of charged to the latest turn; see
        ``reyn.core.turn_scope`` for the enumeration of those paths (the
        ``/compact`` slash short-circuit and the dev/dogfood surfaces).

        #3475: this is ALSO where the FP-0037 MCP-tools-cache priming chain
        (yaml refresh / disk reload / lazy probe) runs, for the same reason —
        it is the one place every turn kind funnels through BEFORE the first
        LLM call of the turn. Previously only the CLIENT_INPUT entry
        (``_handle_user_message``, deleted by #3595 S5) ran this
        chain, so a turn arriving as ``hook`` or ``agent_request`` (a freshly
        spawned worker's first inbound message, e.g.) built its `tools=`
        payload against a never-primed cache — no `mcp_tool_name` enum on
        `call_mcp_tool`/`describe_mcp_tool`, silently, for that session's
        entire life (`ensure_mcp_tools_cached`'s populated-guard was one-shot;
        #3520 made it per-server, so an UNANSWERED probe no longer freezes the
        enum either — but a chain that never runs still primes nothing).
        Running the chain here instead makes the ordering guarantee structural
        rather than kind-dependent.

        #4405: this whole body is the "one turn" a ``REYN_STALL_TRACE=N``
        stall-trace diagnostic brackets — armed on entry, disarmed in a
        ``finally`` so an exception path still cancels it. Off by default
        (env var unset); see ``reyn.runtime.stall_trace`` for why.
        """
        from reyn.runtime.stall_trace import (
            arm as _arm_stall_trace,
        )
        from reyn.runtime.stall_trace import (
            disarm as _disarm_stall_trace,
        )
        from reyn.runtime.stall_trace import (
            stall_trace_seconds_from_env,
        )

        _stall_seconds = stall_trace_seconds_from_env()
        if _stall_seconds is not None:
            _arm_stall_trace(_stall_seconds)
        try:
            self._last_turn_chain_id = chain_id
            # #4381 PR-2 stage ③: reset the in-flight taint latch for the
            # NEW turn — never cleared mid-turn (see the flag's own
            # docstring in __init__).
            self._in_flight_untrusted_this_turn = False
            await self._router_host.maybe_refresh_mcp_tools_from_yaml()
            self._router_host.maybe_reload_mcp_tools_cache_from_disk()
            await self._router_host.ensure_mcp_tools_cached(
                per_server_timeout=self._safety.timeout.mcp_probe_seconds,
            )
            with active_turn(chain_id):
                await self._loop_driver.run_turn(user_text, chain_id)
            # #1800 slice 5a: emit HERE (after `run_turn()` returns), not inside RouterLoop — the
            # only placement that fires exactly once per turn regardless of which terminal path the
            # loop took; moving it into RouterLoop risks double-emission or a missed terminal path.
            self._audit_events.emit("turn_completed", chain_id=chain_id)
            # #1800 slice 5b: turn_end lifecycle hooks — E self-continuation / C stage / F shell:
            # docs/concepts/runtime/hooks.md#e-self-continuation-a-push-with-wake-true
            await self._hook_dispatcher.dispatch(
                "turn_end",
                build_hook_payload(
                    "turn_end", agent_name=self.agent_name,
                    chain_id=chain_id, user_text=user_text,
                ),
            )
            # #2073 S1: config hot-reload turn-boundary safe-point (timing-B):
            # docs/concepts/runtime/config-hot-reload.md#turn-boundary-safe-point-timing-b
            await self._hot_reloader.apply_pending()
            # #3787: project-context edit detection — read-only, emits at most once per
            # edit; does NOT reload ``self._project_context`` (see ProjectContextWatcher).
            self._project_context_watcher.check()
            # ADR-0038 Stage 1a: turn boundary = a user-facing checkpoint. #1547: the
            # user message is this checkpoint's anchor for the rewind-timeline preview.
            # #1533 2c: the FULL message is persisted alongside (edit-prefill source).
            await self._journal.cut_generation(
                anchor=_truncate_anchor(user_text), full_message=user_text,
            )
        finally:
            if _stall_seconds is not None:
                _disarm_stall_trace()
