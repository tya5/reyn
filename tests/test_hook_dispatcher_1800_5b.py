"""Tier 2: #1800 slice 5b — the awaited HookDispatcher.

The dispatcher routes a lifecycle hook by type: push wake=true (E) → inbox
trigger; push wake=false (C) → next-turn staging; shell (F) → run_shell. Per-hook
isolation (a raising hook is skipped, siblings proceed) and the no-hooks no-op
equivalence are the load-bearing safety properties.

Policy (docs/deep-dives/contributing/testing.md):
- Real Session / HookRegistry / HookDef / EventLog / StateLog. The dispatcher's
  three Session seams are injected; the routing units use plain recording async
  callables (real instances, NOT MagicMock/AsyncMock) — exactly the DI surface.
- The Session-level tests use a real Session; only the LLM boundary
  (_loop_driver.run_turn) is replaced with a plain async noop where a turn runs.
- No private-state assertions; no format/shape pinning.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig

# ---------------------------------------------------------------------------
# Recording async callables — real instances (not mocks) for the injected seams
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable. Captures (args, kwargs) per call; an
    optional ``raises`` makes it throw (to exercise per-hook isolation)."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises

    @property
    def kinds(self) -> list:
        """The first positional arg of each call (the inbox/stage kind, or the
        shell command) — content for assertions instead of a len() pin."""
        return [a[0] for (a, _k) in self.calls]


def _dispatcher(hooks: list[HookDef], **seams) -> tuple[HookDispatcher, dict]:
    """Build a HookDispatcher over ``hooks`` with recording seams (overridable)."""
    seams.setdefault("put_inbox", _Recorder())
    seams.setdefault("stage_next_turn_context", _Recorder())
    seams.setdefault("run_shell", _Recorder())
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
    )
    return disp, seams


# ---------------------------------------------------------------------------
# Routing units (recording seams)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_wake_true_routes_to_inbox_E():
    """Tier 2: a push wake=true hook (E) routes to put_inbox as a turn trigger,
    carrying the [hook:name] attribution + wake=True; C-staging is NOT used."""
    hook = HookDef(on="turn_end", template_push=PushBlock(message="continue", wake=True))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].kinds == ["hook"]
    (args, _k), = seams["put_inbox"].calls
    _kind, payload = args
    assert payload["wake"] is True
    assert payload["name"] == "turn_end"      # attribution = the lifecycle point
    assert payload["text"] == "continue"
    assert seams["stage_next_turn_context"].calls == []   # E never stages


@pytest.mark.asyncio
async def test_push_wake_false_routes_to_staging_C():
    """Tier 2: a push wake=false hook (C) stages next-turn context directly (the
    4b staging seam), NOT the inbox (a passive ride-along never triggers)."""
    hook = HookDef(on="turn_start", template_push=PushBlock(message="ctx note", wake=False))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_start", {})

    assert seams["stage_next_turn_context"].kinds == ["hook"]
    (args, _k), = seams["stage_next_turn_context"].calls
    _kind, payload = args
    assert payload["name"] == "turn_start"
    assert payload["text"] == "ctx note"
    assert seams["put_inbox"].calls == []                 # C never triggers


@pytest.mark.asyncio
async def test_shell_routes_to_run_shell_F():
    """Tier 2: a shell hook (F) invokes run_shell with the command + the event
    context (the observable side-effect); no push paths are taken."""
    hook = HookDef(on="session_start", shell_exec="echo hi")
    disp, seams = _dispatcher([hook])

    await disp.dispatch("session_start", {"point": "session_start"})

    assert seams["run_shell"].kinds == ["echo hi"]
    (args, kwargs), = seams["run_shell"].calls
    assert args[1] == {"point": "session_start"}          # event context forwarded
    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_push_when_false_skips_the_push():
    """Tier 2: push_when rendering to false skips the push entirely — neither the
    inbox nor staging is touched (the conditional-push guard)."""
    hook = HookDef(on="turn_end", template_push=PushBlock(message="x", push_when="false"))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_end", {})

    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []


@pytest.mark.asyncio
async def test_throwing_hook_isolated_siblings_proceed():
    """Tier 2: a raising hook is logged + skipped; its siblings still run and
    dispatch() never propagates the exception (the per-hook isolation property)."""
    raising = _Recorder(raises=RuntimeError("boom"))
    hooks = [
        HookDef(on="turn_end", shell_exec="first"),    # this one raises
        HookDef(on="turn_end", shell_exec="second"),   # must still run
    ]
    disp, _seams = _dispatcher(hooks, run_shell=raising)

    # must NOT raise out of dispatch()
    await disp.dispatch("turn_end", {})

    assert raising.kinds == ["first", "second"]   # sibling ran after the raise


@pytest.mark.asyncio
async def test_empty_registry_dispatch_is_noop():
    """Tier 2: an empty registry makes dispatch() a pure no-op — none of the three
    seams is touched (the no-hooks equivalence property, dispatcher level)."""
    disp, seams = _dispatcher([])

    for point in ("session_start", "turn_start", "turn_end", "session_end"):
        await disp.dispatch(point, {})

    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []
    assert seams["run_shell"].calls == []


# ---------------------------------------------------------------------------
# Real-Session integration: no-hooks equivalence + config round-trip
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return Session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


@pytest.mark.asyncio
async def test_real_session_no_hooks_dispatch_is_noop(tmp_path):
    """Tier 2: a real Session with no hooks_config → its dispatcher fires at every
    lifecycle point as a no-op: nothing is pushed to the inbox and nothing is
    staged (run-loop byte-identical to a hooks-free build)."""
    session = _make_session(tmp_path, hooks_config=None)

    for point in ("session_start", "turn_start", "turn_end", "session_end"):
        await session._hook_dispatcher.dispatch(point, {})

    # No E trigger pushed to the (public) inbox at any point — the no-op signal.
    assert session.inbox.empty()


@pytest.mark.asyncio
async def test_real_session_config_roundtrip_E_reaches_inbox(tmp_path):
    """Tier 2: a NON-DEFAULT hooks_config (a real wake=true hook) threads through
    the production Session seam → load_hooks → the dispatcher → dispatch() pushes
    the attributed [hook:name] message to the real Session inbox. Proves the
    config→dispatcher→inbox path end-to-end on a real Session."""
    hooks_config = [
        {"on": "turn_end", "template_push": {"message": "self-continue", "wake": True}},
    ]
    session = _make_session(tmp_path, hooks_config=hooks_config)

    await session._hook_dispatcher.dispatch("turn_end", {})

    # The hook reaching the inbox proves the config parsed + loaded + dispatched
    # (an unloaded hook would leave the inbox empty → get_nowait would raise).
    kind, payload = session.inbox.get_nowait()
    assert kind == "hook"
    assert payload["wake"] is True
    assert payload["text"] == "self-continue"
    assert payload["name"] == "turn_end"
