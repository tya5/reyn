"""Tier 2: #6224 — the select→execute round trip through a REAL
``InProcessTransport``, for both the Artifacts pane and bare ``/rewind``.

The owner reported "選択しても実行されない" (Artifacts) and "引数なしで実行
したけど何も起こらない" (``/rewind``). Neither symptom reproduced under
investigation — both of the investigator's own apparent reproductions traced
back to test-harness bugs, not production defects (see #6224 for the full
trail) — but that investigation surfaced a real, independent gap: nothing in
the existing suite drives EITHER round trip through the exact production
path an operator's keypress takes AND a real ``InProcessTransport``:

- ``test_artifact_list_and_open_4482.py``'s own end-to-end open test calls
  ``_handle_open_artifact_request`` directly (skipping the pane selection);
  its "inline artifact" sibling constructs an ``OptionList.OptionSelected``
  event by hand and calls the handler directly rather than driving a real
  keypress (its own docstring overclaims otherwise — see #6224).
- Every existing ``/rewind`` picker test pushes an already-built
  ``__rewind_list__`` sentinel straight onto a test-double transport
  (``ScriptedTransport``/local ``QueueTransport``, both fakes for THIS
  purpose) — none types ``/rewind`` and lets the real slash dispatch produce
  that sentinel.
- ``tests/_support/textual_chat_test_helpers.py``'s shared ``QueueTransport``
  cannot stand in for either: its ``put_display`` is a deliberate no-op (it
  exists to let a test push scripted frames via its own ``push()``, not to
  observe what the app itself sends outbound) — silently swallowing exactly
  the sentinel this round trip depends on. It is NOT modified here (other
  tests correctly depend on its current no-op shape); these two tests use
  the real ``InProcessTransport`` instead, closing the gap structurally
  rather than teaching the fake to lie less.

★★★ **The harness trap that produced this file's need to exist**: a real
``InProcessTransport`` requires an explicit ``transport.start()`` call before
anything reaches it — ``TextualChatApp`` itself never calls it (confirmed by
grep across ``app.py``: zero call sites), so it is the CALLER's/constructor's
own responsibility. Skipping it does not raise or warn: ``put_display``
still enqueues into ``registry.repl_outbox`` as usual, but nothing ever
pumps that queue into ``transport.frames()`` (the background
``_pump_outbox()`` task ``start()`` itself launches), so the frame sits
there forever and the app's ``_pump_frames()`` blocks silently on an empty
internal queue. Every symptom of the real bug (nothing visibly happens) and
every symptom of THIS trap are IDENTICAL from the outside — the author of
this file hit it twice, once per test, while investigating #6224 itself.
The next person adding a similar real-transport test will hit it too, and
because the failure is silent, they will not know they hit it. Call
``transport.start()`` before mounting the app.

Real ``AgentRegistry``/``Session`` (mirrors
``tests/runtime/test_slash_rewind_self_cancel_3362.py``'s own harness), real
``InProcessTransport``, real ``TextualChatApp``, real ``Pilot`` keypresses.
The only substituted thing is the OS opener binary itself (a fake script on
``PATH`` — the same technique ``test_artifact_list_and_open_4482.py`` and
``test_copy_mode_3507.py`` already use; launching a real external
application from a test is neither possible nor desirable).
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest
from textual.widgets import OptionList

from reyn.core.events.state_log import StateLog
from reyn.data.workspace.artifact_ref import mint_ref
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_WAIT_S = 20.0
_REPLY = "acknowledged"


async def _scripted_provider(**_kwargs) -> LLMToolCallResult:
    """The provider boundary: a real async callable returning a real result
    (same idiom as ``test_slash_rewind_self_cancel_3362.py``)."""
    return LLMToolCallResult(
        content=_REPLY, tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def _build_registry(tmp_path: Path) -> AgentRegistry:
    """A real registry + real WAL + real Session factory, entirely under
    tmp_path (reproduced from ``test_slash_rewind_self_cancel_3362.py``)."""
    agents_dir = tmp_path / ".reyn" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    cell: "list[AgentRegistry]" = []

    def factory(profile: AgentProfile) -> Session:
        agent_dir = agents_dir / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        session = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            registry=cell[0] if cell else None,
            workspace_base_dir=tmp_path / "ws",
            workspace_state_dir=agent_dir / "state",
        )
        session.load_history()
        return session

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=factory, state_log=state_log,
    )
    cell.append(registry)
    AgentProfile.new("default", role="test agent").save(registry._dir / "default")
    return registry


async def _await_reply(registry: AgentRegistry, *, contains: str) -> "str | None":
    async def _drain() -> str:
        while True:
            msg = await registry.repl_outbox.get()
            text = str(getattr(msg, "text", "") or "")
            if contains in text:
                return text

    try:
        return await asyncio.wait_for(_drain(), timeout=_WAIT_S)
    except asyncio.TimeoutError:
        return None


async def _run_one_turn(registry: AgentRegistry, session) -> None:
    """Waits on the WAL generation boundary UNBOUNDED — CI's own
    ``--timeout=120`` is the kill switch, per testing.md's ceiling rule; no
    self-imposed deadline here."""
    before = len(registry.list_rewind_points())
    await session.submit_user_text("turn 0")
    assert await _await_reply(registry, contains=_REPLY) is not None, (
        "the scripted turn never produced a reply"
    )
    while len(registry.list_rewind_points()) <= before:
        await asyncio.sleep(0.05)


def _presentation_frame(*, name: str, ref: str, media_type: str = "text/html") -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{
            "component": "artifact",
            "media_type": media_type,
            "name": name,
            "body": {"ref": ref, "size": 999},
        }]},
    )


@pytest.mark.asyncio
async def test_typed_bare_rewind_opens_the_picker_through_real_slash_dispatch(
    tmp_path, monkeypatch,
):
    """Tier 2: typing ``/rewind`` (bare) into the real Composer, pressing
    Enter, drives the real production path — ``Composer.Submitted`` ->
    ``on_composer_submitted`` -> ``_submit`` -> ``maybe_dispatch_slash`` ->
    the real ``rewind_cmd`` -> ``session.set_pending_command_ui`` + the real
    ``__rewind_list__`` sentinel over a real ``InProcessTransport`` -> the
    real ``RewindPicker`` opening. No step of this chain is a test double;
    every prior test either pushed a hand-built sentinel directly or ran the
    handler in isolation (see module docstring)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _scripted_provider,
    )
    registry = _build_registry(tmp_path)
    try:
        await registry.attach("default")
        session = registry.get_session("default")
        await _run_one_turn(registry, session)
        assert registry.list_rewind_points(), (
            "test setup: the scripted turn must have produced a rewind point"
        )

        transport = InProcessTransport(
            registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
        )
        transport.start()  # ★see module docstring — required, silent if skipped

        app = TextualChatApp(
            transport=transport,
            read_model=RegistryReadModel(registry, agent_name="default"),
            agent_name="default",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            from reyn.interfaces.inline.textual_chat.chrome import Composer
            composer = app.query_one(Composer)
            composer.focus()
            await pilot.pause()

            for key in ["slash", "r", "e", "w", "i", "n", "d"]:
                await pilot.press(key)
            await pilot.press("enter")

            from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
            picker = app.query_one(RewindPicker)
            # Unbounded — CI's own --timeout=120 is the kill switch (testing.md
            # ceiling rule), no self-imposed deadline.
            while not picker.display:
                await pilot.pause()
                await asyncio.sleep(0.02)

            assert picker.display is True, (
                "a real typed /rewind, through the real slash dispatch and a "
                "real InProcessTransport, must open the picker"
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_enter_keypress_on_a_ref_backed_artifact_row_launches_the_real_opener(
    tmp_path, monkeypatch,
):
    """Tier 2: pressing Enter on a REAL, focused, ref-backed Artifacts-pane
    OptionList row (not a hand-constructed ``OptionList.OptionSelected``)
    drives ``on_option_list_option_selected`` -> ``_submit("/open <ref>")``
    -> ``maybe_dispatch_slash`` -> the real ``open_cmd`` -> a real
    ``__open_artifact__`` sentinel over a real ``InProcessTransport`` ->
    ``_handle_open_artifact_request`` -> the real OS-opener subprocess launch
    (substituted only at the OS-binary boundary, per the module docstring).
    ``test_artifact_list_and_open_4482.py``'s own end-to-end test calls the
    handler directly, skipping every one of these steps; this is the
    keypress-driven sibling that was missing."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "report.pptx"
    target.write_text("fake pptx bytes")
    ref = mint_ref(tmp_path, "default", target)

    opener_name = "open" if sys.platform == "darwin" else "xdg-open"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "opened.txt"
    sink_tmp = tmp_path / "opened.txt.tmp"
    script = bindir / opener_name
    # Atomic write (write to sink_tmp, then mv into place) so a poller's
    # sink.exists() means "write complete", never "created, still being
    # written" (#4482 CI flake precedent, reused here).
    script.write_text(f'#!/bin/sh\necho "$1" > {sink_tmp}\nmv {sink_tmp} {sink}\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    agents_dir = tmp_path / ".reyn" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    def factory(profile: AgentProfile) -> Session:
        agent_dir = agents_dir / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name, agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    registry = AgentRegistry(project_root=tmp_path, session_factory=factory)
    try:
        transport = InProcessTransport(
            registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
        )
        transport.start()  # ★see module docstring — required, silent if skipped

        app = TextualChatApp(transport=transport)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app._ingest_frame(_presentation_frame(name="report.pptx", ref=ref))
            await pilot.pause()
            app._open_drawer("artifacts")
            await pilot.pause()

            row = app._artifact_rows_cache[0]
            assert row.ref == ref and row.is_inline is False  # test premise

            artifacts_pane = app.query_one("#artifacts", OptionList)
            artifacts_pane.focus()
            artifacts_pane.highlighted = 0
            await pilot.pause()

            await pilot.press("enter")

            # Unbounded — CI's own --timeout=120 is the kill switch (testing.md
            # ceiling rule), no self-imposed deadline.
            while not sink.exists():
                await pilot.pause()
                await asyncio.sleep(0.02)

            assert sink.exists(), (
                "a real Enter keypress on a real ref-backed Artifacts row, "
                "through the real transport, must reach the OS opener"
            )
            assert sink.read_text().strip() == str(target)
    finally:
        await registry.shutdown()
