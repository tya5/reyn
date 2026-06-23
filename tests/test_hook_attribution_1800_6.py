"""Tier 2: #1800 slice 6 — per-hook ``[hook:name]`` attribution + fidelity.

Slice 5b attributed every push as ``[hook:<point>]`` because the ``hooks:`` config
was a nameless list. Slice 6 adds an OPTIONAL ``name`` to a hook entry; the
dispatcher attributes with ``hook.name`` when set, else the lifecycle point
(back-compat). The ``[hook:name]`` prefix is rendered by the single shared
``_format_hook_attribution`` helper (centralized in 5b), so the E (inbox trigger)
and C (staged ride-along) paths cannot drift.

Fidelity: a push is an attributed NEW system-role message — never a silent
mutation of existing history.

Policy: real ``HookDef``/``HookRegistry``/``load_hooks`` + recording async seams
for the dispatcher units; a real Session (LLM boundary faked) for the fidelity
check. No MagicMock; Tier declared; unpack idiom.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock
from reyn.runtime.chat_message import ChatMessage, _now_iso
from reyn.runtime.session import Session, _format_hook_attribution


class _Recorder:
    """A real recording async callable (the DI seam) — captures (args, kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _dispatcher(hooks: list[HookDef]) -> tuple[HookDispatcher, dict]:
    seams = {
        "put_inbox": _Recorder(),
        "stage_next_turn_context": _Recorder(),
        "run_shell": _Recorder(),
    }
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
    )
    return disp, seams


# --- loader -----------------------------------------------------------------


def test_loader_captures_name_and_defaults_none():
    """Tier 2: the loader captures an explicit ``name``; absent → ``None`` (the
    dispatcher then defaults the attribution to the hook-point)."""
    named = load_hooks([{"name": "my_cont", "on": "turn_end", "push": {"message": "x"}}])
    assert named.hooks_for("turn_end")[0].name == "my_cont"
    unnamed = load_hooks([{"on": "turn_end", "push": {"message": "x"}}])
    assert unnamed.hooks_for("turn_end")[0].name is None


def test_loader_rejects_non_string_name():
    """Tier 2: a non-string ``name`` is a config error (mirrors the matcher
    validation), not a silent coercion."""
    from reyn.hooks.schema import HookConfigError

    with pytest.raises(HookConfigError):
        load_hooks([{"name": 123, "on": "turn_end", "push": {"message": "x"}}])


# --- dispatcher attribution (named → name; unnamed → point) -----------------


@pytest.mark.asyncio
async def test_dispatcher_uses_name_when_set():
    """Tier 2: a named push hook attributes with the hook's ``name``."""
    hook = HookDef(on="turn_end", name="my_cont", push=PushBlock(message="go", wake=True))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_end", {})

    (args, _k), = seams["put_inbox"].calls
    _kind, payload = args
    assert payload["name"] == "my_cont"


@pytest.mark.asyncio
async def test_dispatcher_defaults_to_point_when_unnamed():
    """Tier 2: an unnamed push hook defaults the attribution to the lifecycle
    point — preserving slice-5b ``[hook:<point>]`` (back-compat)."""
    hook = HookDef(on="turn_start", name=None, push=PushBlock(message="go", wake=False))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_start", {})

    (args, _k), = seams["stage_next_turn_context"].calls
    _kind, payload = args
    assert payload["name"] == "turn_start"


# --- shared renderer (E and C cannot drift) ---------------------------------


def test_format_hook_attribution_brackets_the_name():
    """Tier 2: the single shared renderer brackets the name as
    ``[hook:<name>] <text>`` — used by BOTH the E ``_handle_hook_message`` and the
    staged-C consumer, so the two paths render identically by construction."""
    assert _format_hook_attribution("my_cont", "do it") == "[hook:my_cont] do it"
    assert _format_hook_attribution("turn_end", "x") == "[hook:turn_end] x"


# --- fidelity (E path, real Session) ----------------------------------------


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="attr-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


@pytest.mark.asyncio
async def test_E_hook_appends_named_system_message_preserving_history(tmp_path):
    """Tier 2: fidelity — an E hook delivery appends a NEW system-role
    ``[hook:name]`` message and leaves existing history byte-unchanged — a hook
    can never silently mutate an existing message."""
    session = _make_session(tmp_path)

    async def _noop(user_text: str, chain_id: str) -> None:
        pass

    session._loop_driver.run_turn = _noop  # type: ignore[method-assign]

    prior = ChatMessage(role="user", content="prior message", ts=_now_iso())
    session._append_history(prior)
    before = list(session.history)

    await session._handle_hook_message(
        {"name": "my_cont", "text": "continue", "chain_id": "c"},
    )

    # the pre-existing message is the SAME object, byte-unchanged (no mutation)
    assert session.history[0] is before[0]
    assert session.history[0].role == "user"
    assert session.history[0].content == "prior message"
    # a NEW system-role [hook:name] message was appended (added, not replaced)
    appended = [m for m in session.history if m not in before]
    assert any(
        m.role == "system" and m.content == "[hook:my_cont] continue"
        for m in appended
    ), f"expected a [hook:my_cont] system message; got {[(m.role, m.content) for m in appended]}"
