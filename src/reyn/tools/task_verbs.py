"""describe_task / list_tasks / cancel_task — proposal 0067 P4 (#3978).

Read/act against the CALLING session's own ``ChainManager`` (the
settle-path handle substrate — ``reyn.runtime.services.chain_manager``),
threaded via ``RouterCallerState.chains`` (mirrors ``pipeline_registry``'s
own threading, ``RouterHostAdapter.get_chains()``).

Per ADR-0040 D4 ("push-at-settle with immediate deletion") a settled
task's handle is gone — so all three ops answer only about RUNNING tasks;
there is no ``read_task_result``. A handle with ``kind=None`` (a legacy
delegate-relay chain, not yet a typed task — proposal 0067 P6 assigns one
when ``delegate_to_agent``'s completion folds into this substrate) is
excluded from every read here: it is not describable as a task today.

``cancel_task``'s one hard requirement (architect, #3978): a handle whose
``cancel`` hook is ``None`` (a crash-recovered chain — the live callable
belonged to the dead process) MUST NOT be reported as a success. Reporting
"cancelled" while the task keeps running is the same silent lie as
displaying an unenforced ``ttl_seconds`` would be.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES
from reyn.core.offload.canonical import (
    cancel_task_to_canonical,
    describe_task_to_canonical,
    list_tasks_to_canonical,
)
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult


def _chains(ctx: ToolContext) -> Any:
    rs = getattr(ctx, "router_state", None)
    return getattr(rs, "chains", None) if rs is not None else None


def _inbox_depth(ctx: ToolContext) -> "int | None":
    """THIS session's current inbox depth — proposal 0067 P9 (#3978).

    ``chains`` is scoped to THE CALLING session's own ChainManager (module
    docstring), so every task these tools can even see was registered with
    ``requester.session_id`` equal to this same calling session — the depth
    is therefore homogeneous across every task in a given response; no
    per-task resolution is needed."""
    rs = getattr(ctx, "router_state", None)
    return getattr(rs, "session_inbox_depth", None) if rs is not None else None


def _derive_exec_output_path(ctx: ToolContext, chain: Any) -> "str | None":
    """#4733 §3-a (BLOCKING fix, lead-coder review 2026-09-02): the exec
    output file's path is DERIVED here, never stored on the chain. The
    first cut of this feature persisted ``output_path`` as a volatile
    ``_PendingChain`` field (mirroring ``cancel``'s own non-persistence) —
    lead-coder's own diff-read caught the real defect that shape hides: a
    volatile POINTER to a DURABLE file means the durability #3's own
    placement choice bought for free (GC/pin/cap survive a restart; the
    ref naming the file did not) becomes cosmetic the moment a session
    restarts, and the loss is SILENT (``describe_task`` just omits
    ``"output"``, read as "this command produced nothing" rather than
    "the pointer to it was never durable").

    Every input this needs is ALREADY durable: ``chain.chain_id`` (=
    task_id, WAL-persisted at register time) and ``chain.requester``
    (also persisted) name which file; THIS session's own media_store-or-
    fallback choice is a static, config-driven fact restart cannot
    change (the reconstructed session reads the same config). So the
    exact path ``session_api.run_exec_async`` built the first time is
    reproducible here from data that was never at risk, closing the gap
    by construction rather than by remembering to persist one more
    field (#5673's own "don't write a derivable fact twice" precedent,
    cited by lead-coder for this exact review).

    Returns ``None`` only when this host cannot build an OpContext at all
    (``op_context_factory`` absent — a narrow test double, never a real
    session) — the one case with no data to derive from."""
    rs = getattr(ctx, "router_state", None)
    factory = getattr(rs, "op_context_factory", None) if rs is not None else None
    if factory is None:
        return None
    op_ctx = factory()
    media_store = getattr(op_ctx, "media_store", None)
    if media_store is not None:
        output_dir = media_store.history_content_dir
    else:
        output_dir = op_ctx.workspace.base_dir / ".reyn" / "tool-results"
    return str(output_dir / f"exec-{chain.chain_id}.log")


def _exec_output_preview(output_path: str) -> "dict[str, Any]":
    """#4733 §3-a: read the tee'd output file FRESH (no cursor — see
    ``session_api.run_exec_async``'s own docstring for why) and return a
    bounded head/tail preview + the path ref, the SAME shape (preview
    text + path + content_hash) the general tool-result-cap offload path
    already uses (``tool_result_cap.py``), so a reader already familiar
    with that shape recognizes this one.

    Never returns silently-empty for a missing file (BLOCKING fix,
    lead-coder review 2026-09-02: "say WHY it doesn't reach, not that
    it's absent" — the same posture ``LostReason``/
    ``offloaded_content_unavailable`` (#5651) already take for a spilled
    chat entry's own missing backing file, applied here to a DIFFERENT
    kind of missing-file since this one is not the history-content spill
    mechanism those types are scoped to): a read failure returns a typed
    ``{"lost": True, "reason": ...}`` block instead of ``None``/omission.
    ``"not_yet_written_or_evicted"`` is honest about NOT distinguishing
    "the runner hasn't opened it yet" from "cross-session eviction
    already removed it" — this module keeps no separate record that
    would tell the two apart, the same disclosed imprecision
    ``LostReason.GC``'s own docstring already accepts for its file."""
    import hashlib
    from pathlib import Path

    path = Path(output_path)
    try:
        data = path.read_bytes()
    except OSError:
        return {"lost": True, "reason": "not_yet_written_or_evicted", "path": output_path}
    content_hash = hashlib.sha256(data).hexdigest()
    if len(data) <= MAX_CONTROL_IR_RESULT_INLINE_BYTES:
        preview = data.decode("utf-8", errors="replace")
    else:
        half = MAX_CONTROL_IR_RESULT_INLINE_BYTES // 2
        head = data[:half].decode("utf-8", errors="replace")
        tail = data[-half:].decode("utf-8", errors="replace")
        preview = (
            f"{head}\n...[truncated: {len(data)} bytes total — "
            f'full output: read_file(path="{output_path}")]...\n{tail}'
        )
    return {"preview": preview, "path": output_path, "content_hash": content_hash}


def _describe(chain: Any, *, inbox_depth: "int | None" = None) -> dict[str, Any]:
    requester = chain.requester
    return {
        "task_id": chain.chain_id,
        "kind": chain.kind,
        "status": chain.status.value,
        "session": chain.requester.session_id,
        # Proposal 0067 P9 (#3978), architect ruling 2026-08-10: an
        # INSTANTANEOUS read of the task's own (= this calling session's)
        # inbox queue depth — by the time this reply reaches the LLM the
        # real value may already differ (see the field description surfaced
        # to the model, task_verbs.py's tool-description constants below).
        # None when unresolvable (e.g. no live registry in this context) —
        # never a silent 0, which would misread as "definitely empty".
        "session_inbox_depth": inbox_depth,
        "requester": {
            "agent_name": requester.agent_name,
            "session_id": requester.session_id,
        },
    }


# ── describe_task ────────────────────────────────────────────────────────────

_DESCRIBE_TASK_DESCRIPTION = (
    "Describe one currently RUNNING task by its task_id (the handle a "
    "run_prompt/run_pipeline async launch returned). A settled task's "
    "handle no longer exists — use this for progress checking or anomaly "
    "detection, not to retrieve a finished result (results arrive via a "
    "task_settled push, not a poll). session_inbox_depth is a snapshot of "
    "this task's own session's inbox queue depth at the moment of this "
    "call — it may already be stale by the time you read it, and null "
    "means it could not be resolved."
)

_DESCRIBE_TASK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task's handle, as returned by its async launch.",
        },
    },
    "required": ["task_id"],
}


async def _handle_describe_task(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    task_id = args["task_id"]
    chain = chains.get(task_id) if chains is not None else None
    if chain is None or chain.kind is None:
        return {"ok": False, "error": f"no running task {task_id!r}"}
    described = _describe(chain, inbox_depth=_inbox_depth(ctx))
    # #4733 §3-a: kind="exec" ONLY — bounded tail + path ref of the tee'd
    # output file, DERIVED fresh every call (never stored on the chain —
    # see _derive_exec_output_path's own docstring for why). describe_task
    # is the ONE place this enrichment happens — list_tasks stays lean
    # (see _handle_list_tasks's own comment).
    if chain.kind == "exec":
        output_path = _derive_exec_output_path(ctx, chain)
        described["output"] = (
            _exec_output_preview(output_path) if output_path is not None
            else {"lost": True, "reason": "no_op_context_factory_on_this_host"}
        )
    return {"ok": True, **described}


DESCRIBE_TASK = ToolDefinition(
    canonical=describe_task_to_canonical,
    name="describe_task",
    router_dispatched=True,
    description=_DESCRIBE_TASK_DESCRIPTION,
    parameters=_DESCRIBE_TASK_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_describe_task,
    category="discovery",
    purity="read_only",
)


# ── list_tasks ───────────────────────────────────────────────────────────────

_LIST_TASKS_DESCRIPTION = (
    "List currently RUNNING tasks (async run_prompt/run_pipeline launches "
    "not yet settled), optionally filtered by kind. Settled tasks are not "
    "listed — their handle no longer exists (ADR-0040 D4). Each entry's "
    "session_inbox_depth is a snapshot of this session's own inbox queue "
    "depth at the moment of this call (may already be stale by the time "
    "you read it; null means it could not be resolved) — the SAME value "
    "for every entry, since list_tasks only ever sees tasks registered on "
    "this same calling session."
)

_LIST_TASKS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "Optional — restrict to one task kind (e.g. 'prompt', "
                "'pipeline'). Omit to list every running task."
            ),
        },
    },
}


async def _handle_list_tasks(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    if chains is None:
        return {"tasks": []}
    kind_filter = args.get("kind")
    inbox_depth = _inbox_depth(ctx)
    tasks = []
    for chain_id in chains.all_chain_ids():
        chain = chains.get(chain_id)
        if chain is None or chain.kind is None:
            continue
        if kind_filter is not None and chain.kind != kind_filter:
            continue
        described = _describe(chain, inbox_depth=inbox_depth)
        del described["requester"]  # list view: {task_id, kind, status, session, session_inbox_depth}
        # #4733 §3-a: the bounded-tail "output" enrichment describe_task
        # adds for kind="exec" is describe_task-ONLY — _describe() itself
        # never adds it (see that function's own shape). A listing
        # enumerates possibly many tasks at once, and deriving + reading +
        # previewing every exec task's output file on each list_tasks call
        # would scale with task count, not with what the caller actually
        # asked to inspect. Use describe_task(task_id) for that one task's
        # output.
        tasks.append(described)
    return {"tasks": tasks}


LIST_TASKS = ToolDefinition(
    canonical=list_tasks_to_canonical,
    name="list_tasks",
    router_dispatched=True,
    description=_LIST_TASKS_DESCRIPTION,
    parameters=_LIST_TASKS_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_list_tasks,
    category="discovery",
    purity="read_only",
)


# ── cancel_task ──────────────────────────────────────────────────────────────

_CANCEL_TASK_DESCRIPTION = (
    "Request cooperative cancellation of a currently RUNNING task by its "
    "task_id. Returns immediately with status='cancel_requested' — the "
    "task stops at its next safe boundary, and its settle (task_settled) "
    "still fires with status='cancelled' once it does. A task recovered "
    "after a crash cannot be cancelled (no live process to signal); this "
    "returns an explicit error, never a false 'cancelled'."
)

_CANCEL_TASK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task's handle, as returned by its async launch.",
        },
    },
    "required": ["task_id"],
}


async def _handle_cancel_task(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    task_id = args["task_id"]
    chain = chains.get(task_id) if chains is not None else None
    if chain is None or chain.kind is None:
        return {"task_id": task_id, "status": "error", "error": f"no running task {task_id!r}"}
    if chain.cancel is None:
        # Architect's witness requirement (#3978): a recovered handle's
        # cancel hook belonged to the dead process — reporting success
        # here would be the exact silent lie an unenforced ttl_seconds is.
        return {
            "task_id": task_id, "status": "error",
            "error": (
                f"task {task_id!r} cannot be cancelled — no live process is "
                "reachable (likely a handle recovered after a crash)"
            ),
        }
    chain.cancel()
    # #5654: record the request itself, independent of the target's cancel
    # hook actually landing — the fact "an operator asked" survives even if
    # the target's turn takes a moment to actually stop (or, for a pipeline
    # kind, until the next step boundary).
    await chains.mark_cancel_requested(task_id)
    return {"task_id": task_id, "status": "cancel_requested"}


CANCEL_TASK = ToolDefinition(
    canonical=cancel_task_to_canonical,
    name="cancel_task",
    router_dispatched=True,
    description=_CANCEL_TASK_DESCRIPTION,
    parameters=_CANCEL_TASK_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_cancel_task,
    category="io",
    purity="side_effect",
)
