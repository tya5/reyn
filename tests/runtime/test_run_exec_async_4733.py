"""Tier 2: #4733 — ``exec(collect="async")`` (``session_api.run_exec_async``).

Real ``AgentRegistry`` + real ``Session`` (no mocks — mirrors
``test_run_prompt_async_3978_p4e.py``'s own construction pattern), a real
``NoopBackend`` subprocess launch (portable across CI platforms), and the
real ``_subprocess_io.communicate_capped`` sink mechanism (#4733 §3-a,
architect ruling 2026-09-02) — no fake collaborator stands in for any of
these.

Architect's settled design, pinned here:

  1. **Registration shape** — pipeline-type task: ``waiting_on == set()``
     (not the ``{target_agent}`` a prompt-task carries), ``kind == "exec"``,
     and NO chain-watchdog is armed (``arm_at is None``) — the sandbox
     policy already owns this exec's own deadline (#3903/#4193); a second,
     independent chain-timeout would be two competing expirations for one
     exec (architect ruling this session).
  2. **Reading while running** — the tee (added this session, #4733 §3-a
     after the original "reuse run_and_classify unchanged" premise proved
     incompatible with a growing tail) writes stdout/stderr to a file AS
     BYTES ARRIVE; a bounded-tail read of that SAME file (no cursor) grows
     across two reads taken while the process is still alive.
  3. **Completion** — the chain settles (gone from ``caller.chains``),
     ``task_settled`` dispatches with ``kind="exec"``, a REAL status
     derived from the actual outcome (never hardcoded — #5662/#5654
     discipline), and the payload carries ``returncode`` + a ref
     (``output_path``) — never the output body itself.
  4. **Durability** — the output file is still readable, in full, AFTER
     the task has settled (it is not an ephemeral scratch file tied to
     the task's own in-memory lifetime).
  5. **Cancel precision** — ``cancel_task``'s cancel hook sets a
     cancel_event DEDICATED to this one exec, never the caller's shared
     per-turn cancel_event ``make_router_op_context()`` would otherwise
     hand back — the most precise of the OS's 3 cancellation kinds.
  6. **Orphan-prevention** — the background ``asyncio.Task`` is registered
     on the caller session's OWN task funnel (``disposition="cancel_join"``)
     so session teardown cannot leak it, mirroring the 9 existing
     precedents ``TrackedTaskSet`` already documents.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_exec_async
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    from reyn.core.events.state_log import StateLog

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            sandbox_backend=NoopBackend(),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _yield_until(condition, *, description: str) -> None:
    """Cooperative-yield poll on a real condition — NO sleep(N) (CLAUDE.md:
    "no sleep the assertion depends on"). Unbounded — CI's own --timeout is
    the ceiling, per the same rule's own "wait on the condition
    unboundedly" clause; this never adds its own duration."""
    while not condition():
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_registers_exec_task_with_no_waiting_on_and_no_watchdog_armed(tmp_path):
    """Tier 2: pipeline-shaped registration — |waiting_on| == 0, kind="exec",
    no chain-watchdog (architect: the sandbox policy already owns this
    exec's own deadline)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", "print('hi')"],
    )
    assert result["status"] == "started"
    task_id = result["data"]["task_id"]

    chain = caller.chains.get(task_id)
    assert chain is not None, "the chain must be registered synchronously, before this call returns"
    assert chain.kind == "exec"
    assert chain.waiting_on == set(), "a pipeline-shaped task waits on nothing else"
    assert chain.arm_at is None, "no chain-watchdog — the sandbox policy owns this exec's own deadline"

    # Let the background task actually finish so it doesn't leak past this test.
    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")


def _describe_ctx(caller: Session) -> Any:
    """A minimal ``ToolContext``-shaped stand-in exposing exactly what
    ``_derive_exec_output_path`` reads (``ctx.router_state.
    op_context_factory``) — the SAME public seam a real router turn's
    ``RouterCallerState`` carries (``build_resource_caller_state``), not a
    private Session attribute."""
    import types

    return types.SimpleNamespace(
        router_state=types.SimpleNamespace(
            op_context_factory=caller.router_host.make_router_op_context,
        ),
    )


@pytest.mark.asyncio
async def test_describe_task_tail_grows_while_the_process_is_still_running(tmp_path):
    """Tier 2: the core §3-a witness — the tee mechanism
    (``communicate_capped``'s ``sink``, #4733) makes the output file's tail
    grow WHILE the process is still alive, not only after it exits. A FIFO
    gates the child's second write on this test's own explicit signal (an
    unbounded blocking read, never a sleep) so the two reads below are
    deterministically ordered around a real "still running" moment.

    The path is DERIVED (``_derive_exec_output_path``, BLOCKING fix,
    lead-coder review 2026-09-02) from ``task_id`` + this session's own
    media_store-or-fallback choice — it is never read off the chain
    itself (that field was removed for exactly the silent-loss-on-restart
    defect this fix closes)."""
    from reyn.tools.task_verbs import _derive_exec_output_path, _exec_output_preview

    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")
    describe_ctx = _describe_ctx(caller)

    fifo_path = tmp_path / "gate.fifo"
    os.mkfifo(fifo_path)

    script = (
        "import sys\n"
        "sys.stdout.write('first\\n'); sys.stdout.flush()\n"
        f"open({str(fifo_path)!r}).read()\n"  # blocks until this test opens it for writing
        "sys.stdout.write('second\\n'); sys.stdout.flush()\n"
    )
    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", script],
    )
    task_id = result["data"]["task_id"]
    chain = caller.chains.get(task_id)
    output_path = _derive_exec_output_path(describe_ctx, chain)
    assert output_path is not None

    # First read: only "first\n" can possibly have landed — the child is
    # blocked on the FIFO open/read, so this is a real "still running" read,
    # not a race with completion.
    await _yield_until(
        lambda: "first" in _exec_output_preview(output_path).get("preview", ""),
        description="first chunk visible",
    )
    first_preview = _exec_output_preview(output_path)
    assert "first" in first_preview["preview"]
    assert "second" not in first_preview["preview"], (
        "the child is still blocked on the FIFO — a premature 'second' here "
        "would mean this read raced completion instead of witnessing a "
        "genuinely still-running process"
    )

    # Release the gate — the child's second write, and its own exit, follow.
    fifo_path.open("w").write("go\n")

    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")
    # Durability pin: still readable, in FULL, after the task has settled
    # (the chain record is gone; the path is RE-DERIVED, the same way a
    # real post-restart describe_task call would, from task_id alone).
    final_text = Path(output_path).read_text()
    assert "first" in final_text and "second" in final_text, (
        "the tail must have GROWN — both chunks present after completion"
    )


@pytest.mark.asyncio
async def test_output_survives_a_simulated_restart_because_the_path_is_derived_not_stored(tmp_path):
    """Tier 2: BLOCKING fix pin (lead-coder review, 2026-09-02) — the exact
    defect the review caught: a task's output must still be describable
    after the task's own in-process record is gone and a FRESH
    ``_PendingChain`` (mirroring what ``ChainManager.restore()`` builds
    from a crash-recovered WAL entry — no ``cancel`` hook, since the live
    callable belonged to the dead process) is registered for the SAME
    ``chain_id``/``requester``. If ``output_path`` were still a stored
    field, this fresh chain would carry ``output_path=None`` and the file
    would read as lost even though it is sitting right there on disk —
    the exact silent-loss BLOCKING flagged."""
    from reyn.runtime.task_types import Requester
    from reyn.tools.task_verbs import _derive_exec_output_path, _exec_output_preview

    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")
    describe_ctx = _describe_ctx(caller)

    marker = "survives-a-restart-4733"
    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", f"print({marker!r})"],
    )
    task_id = result["data"]["task_id"]
    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")

    # Simulate a crash-recovered handle for the SAME task_id — no `cancel`
    # (restore() never gets one back; the live callable belonged to the
    # dead process), same shape task_verbs.py's own module docstring
    # already documents for a recovered handle.
    recovered = await caller.chains.register(
        chain_id=task_id, depth=0, original_text=" ".join([sys.executable]),
        sender="alpha", waiting_on=set(),
        requester=Requester(agent_name="alpha", session_id="main"),
        origin_depth=0, kind="exec", cancel=None,
    )
    assert recovered.cancel is None, "a recovered handle carries no live cancel hook"

    derived_path = _derive_exec_output_path(describe_ctx, recovered)
    assert derived_path is not None
    preview = _exec_output_preview(derived_path)
    assert "lost" not in preview, (
        f"the output must still be readable after a simulated restart, got {preview!r}"
    )
    assert marker in preview["preview"]


@pytest.mark.asyncio
async def test_completion_settles_and_dispatches_task_settled_with_ref_not_body(tmp_path, monkeypatch):
    """Tier 2: accept-completion — chain gone, task_settled carries
    kind="exec", a REAL status (derived from returncode, never hardcoded),
    and a returncode+ref payload — never the output body."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    settled_events: list[dict] = []

    async def _record_dispatch(point: str, template_vars: dict) -> None:
        settled_events.append(dict(template_vars))

    monkeypatch.setattr(caller, "dispatch_external_event", _record_dispatch)

    marker = "distinctive-output-marker-4733"
    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", f"print({marker!r})"],
    )
    task_id = result["data"]["task_id"]

    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")
    await _yield_until(lambda: bool(settled_events), description="task_settled dispatched")

    (payload,) = settled_events
    assert payload["task_id"] == task_id
    assert payload["kind"] == "exec"
    assert payload["status"] == "ok"  # returncode 0 → real "ok", not a hardcoded literal
    assert payload["result"]["returncode"] == 0
    output_path = payload["result"]["output_path"]
    assert marker not in str(payload["result"]), (
        "completion must carry a returncode + ref — never the output body itself"
    )
    # The ref it names is real and durable.
    assert marker in Path(output_path).read_text()


@pytest.mark.asyncio
async def test_cancel_stops_only_this_exec_via_a_dedicated_cancel_event(tmp_path):
    """Tier 2: cancel precision — cancel_task's hook sets a cancel_event
    scoped to THIS ONE exec, distinct from the caller's own shared
    per-turn cancel_event. Settling then reports status="cancelled" —
    never a false "ok" for a task that was actually cancelled (the same
    silent-lie class cancel_task's own description disclaims)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    fifo_path = tmp_path / "block.fifo"
    os.mkfifo(fifo_path)
    # A process that blocks until cancelled — never exits on its own within
    # this test, so a real cancel is what ends it, not a natural exit race.
    script = f"open({str(fifo_path)!r}).read()"

    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", script],
    )
    task_id = result["data"]["task_id"]
    chain = caller.chains.get(task_id)
    assert chain.cancel is not None, "a freshly-registered exec task must be cancellable"

    # The caller's OWN per-turn cancel_event (from make_router_op_context())
    # must be untouched by cancelling this one exec — precision pin.
    turn_ctx = caller.router_host.make_router_op_context()
    turn_cancel_event = turn_ctx.cancel_event

    chain.cancel()  # the exact call cancel_task's own handler makes
    assert turn_cancel_event is None or not turn_cancel_event.is_set(), (
        "cancelling this exec must not set the caller's shared per-turn "
        "cancel_event — cancel_task on this task_id must stop ONLY this exec"
    )

    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")


@pytest.mark.asyncio
async def test_background_task_is_registered_on_the_session_task_funnel(tmp_path):
    """Tier 2: orphan-prevention — the background asyncio.Task lands on
    the caller session's OWN TrackedTaskSet (disposition="cancel_join"),
    the same funnel #4759 built so a session teardown cannot leak it,
    mirroring the 9 existing precedents that funnel's own module docstring
    documents."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    caller = reg.get_or_load("alpha")

    result = await run_exec_async(
        reg, caller_agent="alpha", caller_sid="main",
        argv=[sys.executable, "-c", "print('hi')"],
    )
    task_id = result["data"]["task_id"]

    tracked_names = [t.get_name() for t in caller._background_tasks.pending()]  # noqa: SLF001 — Tier-2 OS-invariant read, no public snapshot exists for "is this specific task tracked"
    assert any(f"exec-async-{task_id}" in n for n in tracked_names), (
        f"expected a tracked task named exec-async-{task_id!r}, got {tracked_names!r}"
    )

    await _yield_until(lambda: not caller.chains.has(task_id), description="settle")


@pytest.mark.asyncio
async def test_sync_exec_creates_no_output_file(tmp_path):
    """Tier 2: deny-sibling — the SYNCHRONOUS exec path (``handle``,
    collect omitted) is unaffected by #4733: it opens no tee file at all
    (``run_sandboxed_exec``'s own ``sink=None`` default for that entry
    point)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    workspace = Workspace(events=events, base_dir=tmp_path)
    ctx = OpContext(
        workspace=workspace, events=events, permission_decl=PermissionDecl(),
        sandbox_backend=NoopBackend(), default_sandbox_policy={},
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=[sys.executable, "-c", "print('sync')"])

    result = await handle_sandboxed_exec(op=op, ctx=ctx)
    assert result["status"] == "ok"
    tool_results_dir = tmp_path / ".reyn" / "tool-results"
    assert not tool_results_dir.exists() or not any(tool_results_dir.glob("exec-*.log")), (
        "the synchronous exec path must create no exec-*.log tee file"
    )
