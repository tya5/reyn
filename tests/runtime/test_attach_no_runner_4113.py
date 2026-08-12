"""Tier 2: #4113, architect ruling 2026-08-10 — ``AgentRegistry.attach(...,
start_runner=False)`` skips ONLY the background ``session.run()`` task,
nothing else.

Context: ``reyn run-once`` (`interfaces/cli/commands/chat.py`'s
`_restore_and_attach`) used to call bare ``registry.attach(name)`` — which
unconditionally spawns a background ``session.run()`` loop on the SAME
Session object `_run_once` then drives inline via
``send_to_agent_impl``'s ``MessageBus.request`` pump. Both pumps
(``run_one_iteration()``) run concurrently on the identical Session, in
the identical process — the exact "self-running AND inline-driven"
violation `a2a.py`'s own invariant forbids ("never both"). Fully
demonstrated by reading the code (measured, #4113 issue comment), not
requiring a live reproduction to know it's real.

Fix (this PR): fix the VIOLATING SIDE (`attach()`'s own call site in
`run-once`), not a generic pump-layer gate — a generic "refuse if a
runner exists" gate at the pump layer would break every already-shipped
MCP/A2A caller that legitimately drives a self-running session inline
today (architect's own MCP double-pump warning, same shape, this time
concrete: "the fix becomes an outage").

Pins:
  1. ``start_runner=False`` — no `session.run()` task is created for that
     key, but the session IS still loaded (``get_or_load``) and the
     forwarder/focus-listener/connection-switch/announce side effects are
     UNCHANGED (only the runner is skipped).
  2. Default (``start_runner=True``, the un-named param — every existing
     caller) is byte-identical to before: a runner IS created. This is
     the accept-side sibling proving the new param doesn't silently
     change the default caller's behavior.
  3. Falsify pair: with `start_runner=False`, `MessageBus.request`
     (`_run_once`'s own pump) does NOT race a background task — confirmed
     by the ABSENCE of that task, not by trusting the flag alone.

All assertions go through ``AgentRegistry.is_session_running(name, sid)``
(a public read, not private-state access) — the SAME predicate
``attach``/``ensure_running``/``ensure_session_running`` already re-derive
inline before creating a new task.

The registry-level tests above pin the MECHANISM (does `start_runner=`
actually gate task creation); the module also includes a BEHAVIORAL
witness (below) — driving the REAL `reyn chat --once` entry point and
observing, from a public read, that no runner is live while the session
is driven inline — pinning that `chat.py`'s own once-path call site
actually USES the mechanism. The mechanism working correctly is not
evidence the call site uses it (same "declared vs. executed" trap this
whole issue's own thread hit twice already: lead-coder's premature
"substrate is live" claim, and this session's own re-measurement habit).
A first version of this witness read `chat.py`'s source and matched the
literal call string — lead-coder correctly blocked it (six-question ②:
transcribing the implementation back as the assertion only fails if both
sides are edited together, and false-positives on any refactor that
keeps behavior but changes call syntax); replaced with the behavioral
version below.
"""
from __future__ import annotations

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from tests._support.agent_session import make_session


def _registry(tmp_path):
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


@pytest.mark.asyncio
async def test_start_runner_false_loads_but_does_not_spawn_a_run_task(tmp_path):
    """Tier 2: the #4113 fix's core pin — attach(start_runner=False) loads
    the session (get_or_load) but starts NO background session.run() task
    for (name, "main")."""
    reg = _registry(tmp_path)

    session = await reg.attach("alpha", start_runner=False)

    assert session is not None
    assert not reg.is_session_running("alpha", _DEFAULT_SID), (
        "start_runner=False must not create a session.run() task"
    )

    await reg.shutdown()


@pytest.mark.asyncio
async def test_default_start_runner_still_spawns_a_run_task(tmp_path):
    """Tier 2: accept-side sibling — every EXISTING caller (bare
    `registry.attach(name)`, no kwarg) keeps byte-identical behavior. The
    new param's default must not silently change what already ships."""
    reg = _registry(tmp_path)

    session = await reg.attach("alpha")

    assert session is not None
    assert reg.is_session_running("alpha", _DEFAULT_SID), (
        "the default (start_runner=True) must still create a session.run() task"
    )

    await reg.shutdown()


@pytest.mark.asyncio
async def test_falsify_start_runner_false_actually_prevents_the_race(tmp_path):
    """Tier 2c: falsify — confirms the fix by first calling attach() the
    OLD way (bare, no start_runner kwarg — exactly what reyn run-once
    used to do) and observing that a run() task DOES then exist for the
    once-path's key, before a fresh registry with the real
    start_runner=False call confirms it's absent. Proves the assertions
    above are meaningfully load-bearing, not trivially true regardless of
    the parameter."""
    old_reg = _registry(tmp_path / "old")
    await old_reg.attach("alpha")
    assert old_reg.is_session_running("alpha", _DEFAULT_SID), (
        "sanity check: the pre-fix call shape genuinely creates a runner "
        "(if this fails, the falsify below proves nothing)"
    )
    await old_reg.shutdown()

    fixed_reg = _registry(tmp_path / "fixed")
    await fixed_reg.attach("alpha", start_runner=False)
    assert not fixed_reg.is_session_running("alpha", _DEFAULT_SID), (
        "the fix must prevent the runner that the falsify step just proved "
        "the old call shape creates"
    )
    await fixed_reg.shutdown()


def _run_chat_once(tmp_path, monkeypatch, *, on_send=None):
    """Drive the REAL `reyn chat --once` entry point (`chat.run(args)`) —
    mirrors `test_startup_dedup_3671_p4a.py`'s `_run_chat_once` exactly
    (same substitutions: fake `send_to_agent_impl`, stdin). `on_send`, if
    given, is called with the LIVE `registry` from inside the fake send —
    the exact moment the real one would be pumping the session inline,
    so a caller can observe registry state precisely where the old bug's
    race would have been active."""
    import argparse
    import io

    from reyn.interfaces.cli.commands.chat import register as chat_register

    monkeypatch.chdir(tmp_path)
    # #4349: reyn ships no built-in model catalog — a minimal reyn.yaml is
    # needed for the real config-load path this helper drives (mirrors
    # test_chat_cli_flags.py's own copy of this fix).
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: openai/test-standard-model\n",
        encoding="utf-8",
    )
    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    chat_register(sub)
    args = top.parse_args(["chat"])
    args.once = True

    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None,
                          inbox_kind="user") -> dict:
        if on_send is not None:
            on_send(registry, agent_name)
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    from reyn.interfaces.cli.commands import chat as chat_mod
    chat_mod.run(args)


def test_run_once_path_has_no_live_runner_while_driving_inline(tmp_path, monkeypatch):
    """Tier 2: behavioral witness on the REAL once-path, replacing an
    earlier static-source version lead-coder correctly blocked (six-
    question ②: transcribing the implementation's own call-string back
    as the assertion only fails if BOTH sides are edited together, and
    false-positives on any refactor that keeps behavior but changes call
    syntax). This drives the ACTUAL `reyn chat --once` entry point
    (`chat.run(args)`, same harness `test_startup_dedup_3671_p4a.py`
    uses) and observes `AgentRegistry.is_session_running` (public) from
    INSIDE the faked `send_to_agent_impl` — the exact moment the real
    pump would be racing a background runner if the fix were absent."""
    observed = {}

    def _observe(registry, agent_name):
        observed["is_running"] = registry.is_session_running(agent_name, _DEFAULT_SID)

    _run_chat_once(tmp_path, monkeypatch, on_send=_observe)

    assert observed.get("is_running") is False, (
        "reyn chat --once must not have a live session.run() runner while "
        "send_to_agent_impl drives the session inline — a live runner here "
        "would race the inline pump on the identical Session object (#4113)"
    )


@pytest.mark.asyncio
async def test_interactive_path_still_starts_a_live_runner(tmp_path):
    """Tier 2: accept-side sibling — `_background_attach` (the REAL,
    independently-importable function `reyn chat`'s interactive path
    calls — module-level per its own docstring, not a nested closure)
    still starts a runner. Confirms the fix is scoped to the once-path
    only — an interactive `reyn chat` session IS the legitimate
    self-running case this whole invariant exists to protect."""
    from reyn.interfaces.cli.commands.chat import _background_attach

    reg = _registry(tmp_path)

    await _background_attach(reg, "alpha", skip_restore=True)

    assert reg.is_session_running("alpha", _DEFAULT_SID), (
        "_background_attach (reyn chat's interactive path) must still "
        "start a live runner — unaffected by the #4113 once-path fix"
    )

    await reg.shutdown()
