"""Tier 2: #6224 — two failures that used to leave a DIFFERENT (or ZERO)
on-screen trace than every other swallowed exception in this module, now
made equally visible through the SAME mechanism.

**Defect A** — an Artifacts-pane inline-row selection
(``TextualChatApp.on_option_list_option_selected``'s ``ref is None`` leg,
``app.py``) called ``_handle_open_inline_artifact_request`` with no
``try/except`` at all, OUTSIDE ``_pump_frames`` — an exception there left
ZERO trace: no ``_pump_swallow_stats`` bump, no ``frame pump: N swallowed``
status-line segment, no ``on_exception``/``_handle_exception`` app override
to catch it either. The fix wraps that call and reuses
``TextualChatApp._record_pump_swallow`` directly (architect ruling, #6224
review: recovering a design that method's own docstring already declared,
not inventing a new one) — this test drives a REAL Enter keypress on a
REAL, composed inline Artifacts row whose (real, dataclass) ``ArtifactRow``
carries a type-violating ``inline_content`` (an ``int``, not ``str`` —
nothing in this module enforces the dataclass's own type hints at
runtime), which makes ``fh.write(row.inline_content or "")`` inside
``_handle_open_inline_artifact_request`` raise a genuine, uncontrived
``TypeError`` — no ``MagicMock``/``AsyncMock``/``patch`` of any
collaborator. ``open_with_os_default`` itself is documented "never raises"
and ``mimetypes.guess_extension`` cannot be made to raise on ordinary
inputs, so a wrong-typed ``inline_content`` is the one legitimate way to
force this call site's OWN "something downstream raised" case without
faking a collaborator.

**Defect B** — ``RewindPicker.show_tree``/``show_points``
(``rewind_picker.py``) call ``self.query_one("#rewind-picker-options", ...)``
UNGUARDED, unlike this class's own ``_set_title``/``hide()`` siblings
(both ``try/except: pass``). Deliberately left unguarded (see the comment
now on both call sites) rather than made to match those siblings: this
method's whole job is to populate the picker, so swallowing a real
``NoMatches`` here would open (or fail to open) the picker with no rows
and no signal — the owner's OWN reported symptom, reproduced silently.
Left to raise, it propagates to ``_handle_rewind_request``'s caller,
``_pump_frames``'s own ``__rewind_list__`` leg, which is ALREADY wrapped
in ``except Exception`` -> ``_record_pump_swallow`` — so this defect
needed no code change, only the deliberate-and-documented decision this
test locks in. It is proven here by removing the REAL, composed
``#rewind-picker-options`` ``OptionList`` child (``Widget.remove()`` — a
real Textual API call on a real mounted widget, not a mock) before
triggering a real typed ``/rewind`` through the real slash dispatch, and
asserting the SAME counter the Defect-A test asserts on bumps, while the
picker itself never becomes visible.

Harness: reproduces the real ``AgentRegistry``/``Session``/
``InProcessTransport``/``TextualChatApp``/``Pilot`` pattern established by
``test_6224_real_transport_select_execute_roundtrip.py`` — read that
file's own module docstring first.

★★★ Same harness trap as that file: a real ``InProcessTransport`` needs an
explicit ``transport.start()`` before anything reaches it (``TextualChatApp``
itself never calls it — confirmed by grep, zero call sites). Skipping it is
SILENT: frames queue forever, nothing pumps them, and every symptom looks
identical to the defects themselves. ``transport.start()`` is called before
mounting the app in both tests below.

No ceilings: every wait below is an unbounded ``while`` polling loop —
CI's own ``--timeout=120`` is the kill switch (testing.md's ceiling rule),
not a self-imposed deadline.

Private-attribute reads with no public alternative: ``_ingest_frame``,
``_open_drawer``, ``_artifact_rows_cache``, ``registry._dir`` — their
absence from the public surface is the finding (testing.md's own
private-state rule), matching the precedent the reference file above
already established. Neither test asserts on ``_pump_swallow_stats``
directly (that IS private state, unlike the methods above which are
private DRIVERS — testing.md's rule targets assertions, not every
private-prefixed call): both read only the PUBLIC render of
``chrome.StatusLine``, the SAME idiom
``test_5732_pump_swallow_visibility.py`` already established
("count は公開の読みに載る" — architect ruling quoted there).

Strip-falsify (this file's own author, actually run against an edited
tree, both directions kept inside the editor — see the PR body for the
full account):

- Defect A: removing the new ``try/except`` around
  ``await self._handle_open_inline_artifact_request(row)`` in
  ``on_option_list_option_selected`` and running just this test: **hangs**
  (``timeout 60 pytest ... ::test_inline_artifact_row_failure...`` exits
  124). The ``TypeError`` propagates uncaught out of a Textual message
  handler; nothing in this app catches it or moves the counter, so the
  test's own unbounded poll on ``_pump_swallow_stats.count`` never
  observes a satisfying state and runs until CI's ``--timeout`` kills it
  — a hang, not a clean assertion failure, which is itself evidence FOR
  the defect (a crashed handler really does look like nothing happened).
  Reverting the edit (in the editor only, never via git) restores GREEN.
- Defect B: temporarily wrapping ``show_points``'s ``query_one`` call in
  the SAME ``try/except: pass`` shape its ``_set_title``/``hide()``
  siblings use (the actual call site this test's own scenario hits —
  a single-branch tree falls back to ``show_points``, so probing
  ``show_tree``'s OWN ``query_one`` first left this test still GREEN,
  which is itself a real finding about which method the single-branch
  path actually calls) and running just this test: **also hangs**, the
  same shape as Defect A and for the same reason — swallowing the
  ``NoMatches`` means ``_handle_rewind_request`` returns normally,
  ``_record_pump_swallow`` never runs, and the counter never reaches 1,
  so the unbounded poll never exits. Reverting (in the editor only)
  restores GREEN. Both defects strip-falsify to the SAME observed shape:
  a clean hang under CI's own timeout, never a crash and never a silent
  false-green.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from textual.widgets import OptionList

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
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

_REPLY = "acknowledged"


async def _scripted_provider(**_kwargs) -> LLMToolCallResult:
    """The provider boundary: a real async callable returning a real
    result (same idiom as the #6224 reference file)."""
    return LLMToolCallResult(
        content=_REPLY, tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def _build_registry(tmp_path) -> AgentRegistry:
    """A real registry + real WAL + real Session factory, entirely under
    tmp_path (reproduced from the #6224 reference file)."""
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


async def _await_reply(registry: AgentRegistry, *, contains: str) -> str:
    """Unbounded — CI's own ``--timeout=120`` is the kill switch."""
    while True:
        msg = await registry.repl_outbox.get()
        text = str(getattr(msg, "text", "") or "")
        if contains in text:
            return text


async def _run_one_turn(registry: AgentRegistry, session) -> None:
    """Waits on the WAL generation boundary UNBOUNDED — no self-imposed
    deadline, per testing.md's ceiling rule."""
    before = len(registry.list_rewind_points())
    await session.submit_user_text("turn 0")
    await _await_reply(registry, contains=_REPLY)
    while len(registry.list_rewind_points()) <= before:
        await asyncio.sleep(0.05)


def _presentation_frame(*, name: str, inline: str) -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{
            "component": "artifact",
            "media_type": "text/plain",
            "name": name,
            "body": {"inline": inline},
        }]},
    )


@pytest.mark.asyncio
async def test_inline_artifact_row_failure_reaches_the_same_visible_counter(
    tmp_path,
):
    """Tier 2: Defect A — a real Enter keypress on a real, composed
    pure-inline Artifacts row whose handler raises must surface on the
    SAME public status line a REF-backed row's failure already does via
    the pump — not leave zero trace. Reads only the PUBLIC render of
    :class:`~reyn.interfaces.inline.textual_chat.chrome.StatusLine`
    (same idiom ``test_5732_pump_swallow_visibility.py`` already
    established — "count は公開の読みに載る", never a private
    ``_pump_swallow_stats`` access)."""
    from reyn.interfaces.inline.textual_chat.chrome import StatusLine

    registry = _build_registry(tmp_path)
    try:
        transport = InProcessTransport(
            registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
        )
        transport.start()  # required — see module docstring's ★★★ note

        app = TextualChatApp(
            transport=transport,
            read_model=RegistryReadModel(registry, agent_name="default"),
            agent_name="default",
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app._ingest_frame(
                _presentation_frame(name="bad-row.txt", inline="hello")
            )
            await pilot.pause()
            app._open_drawer("artifacts")
            await pilot.pause()

            row = app._artifact_rows_cache[0]
            # test premise: a real pure-inline row (no ref, real str content)
            assert row.ref is None and row.inline_content == "hello"

            # Corrupt the REAL dataclass instance's own field with a
            # type-violating (but real, not faked) value — `dataclasses`
            # does not enforce type hints at runtime, so this is a
            # legitimate way to force `fh.write(row.inline_content or "")`
            # inside `_handle_open_inline_artifact_request` to raise a
            # genuine `TypeError`, without mocking any collaborator.
            app._artifact_rows_cache[0] = dataclasses.replace(
                row, inline_content=12345  # type: ignore[arg-type]
            )

            artifacts_pane = app.query_one("#artifacts", OptionList)
            artifacts_pane.focus()
            artifacts_pane.highlighted = 0
            await pilot.pause()

            status_text = str(app.query_one(StatusLine).render())
            assert "frame pump" not in status_text  # test premise

            await pilot.press("enter")

            # Unbounded — CI's own --timeout=120 is the kill switch. This
            # call site sits OUTSIDE the pump's own per-frame chrome
            # refresh, so nothing re-renders the status line on its own —
            # drive it (a private METHOD call, not a private-state
            # assertion: testing.md's rule targets assertions, and this
            # app's own reference harness already calls private methods
            # as drivers, e.g. `_ingest_frame`/`_open_drawer` above).
            while "frame pump" not in status_text:
                app._refresh_live_chrome()
                status_text = str(app.query_one(StatusLine).render())
                await pilot.pause()
                await asyncio.sleep(0.02)

            assert "frame pump: 1 swallowed" in status_text, (
                f"the failure must surface on the always-visible status "
                f"line, the same way a REF-backed row's failure already "
                f"does: {status_text!r}"
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_rewind_picker_missing_option_list_raises_into_the_pump_counter(
    tmp_path, monkeypatch,
):
    """Tier 2: Defect B — with the real ``#rewind-picker-options`` child
    widget removed (a real Textual ``remove()``, not a mock), a real typed
    ``/rewind`` through the real slash dispatch must NOT open an empty,
    silent picker — the unguarded ``query_one`` inside
    ``RewindPicker.show_tree``/``show_points`` must raise, and that raise
    must reach the SAME pump-swallow counter Defect A's fix now also
    feeds, because ``_handle_rewind_request`` already runs inside
    ``_pump_frames``'s own guarded ``__rewind_list__`` leg. Reads only the
    PUBLIC render of ``StatusLine`` (same idiom as the sibling test
    above and as ``test_5732_pump_swallow_visibility.py``), never a
    private ``_pump_swallow_stats`` access."""
    from reyn.interfaces.inline.textual_chat.chrome import StatusLine

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
        transport.start()  # required — see module docstring's ★★★ note

        app = TextualChatApp(
            transport=transport,
            read_model=RegistryReadModel(registry, agent_name="default"),
            agent_name="default",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            picker = app.query_one(RewindPicker)
            options_widget = picker.query_one(
                "#rewind-picker-options", OptionList
            )
            await options_widget.remove()  # real Textual API, real widget
            await pilot.pause()

            status_text = str(app.query_one(StatusLine).render())
            assert "frame pump" not in status_text  # test premise

            from reyn.interfaces.inline.textual_chat.chrome import Composer
            composer = app.query_one(Composer)
            composer.focus()
            await pilot.pause()
            for key in ["slash", "r", "e", "w", "i", "n", "d"]:
                await pilot.press(key)
            await pilot.press("enter")

            # Unbounded — CI's own --timeout=120 is the kill switch. This
            # call site runs INSIDE `_pump_frames`, whose own trailing
            # per-frame chrome refresh already re-renders the status line
            # on every iteration, so no manual refresh driver is needed
            # here (contrast the sibling Defect-A test above).
            while "frame pump" not in status_text:
                status_text = str(app.query_one(StatusLine).render())
                await pilot.pause()
                await asyncio.sleep(0.02)

            assert "frame pump: 1 swallowed" in status_text, (
                "the picker's own missing OptionList must be counted "
                "exactly once via the pump's existing __rewind_list__ guard"
            )
            assert picker.display is False, (
                "the picker must never claim to be showing rows it could "
                "not populate — a silently 'opened but empty' picker would "
                "be the deny case this design decision exists to avoid"
            )
    finally:
        await registry.shutdown()
