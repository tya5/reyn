"""Tier 2: fork-picker edit-mode bindings + banner (ADR-0038 2c, tui-coder half).

The picker stays mounted during edit (can_focus=False → InputBar always focused),
so edit-mode must (a) suspend the picker's ↑/↓/Enter/`e` nav so those reach the
InputBar, (b) give Esc a priority-3.5 "exit edit, keep picker" branch, and
(c) show a non-clobberable "✎ editing checkpoint #N" banner. This is the binding
half; the data-flow (full-message pre-fill + submit→checkout(N-1)+fork) lands
separately on the same co-authored branch.

Pins (real instances + run_test pilot — no mocks):
- enter/exit_edit_mode toggle edit_mode_active + the banner (public surface).
- exit_edit_mode resets the Esc-Esc first tap (#1554 every-exit-must-reset).
- check_action suspends rewind_prev/next/confirm + edit_checkpoint while editing.
- Esc during edit exits edit-mode and KEEPS the picker (priority-3.5).
- `e` on a selected checkpoint enters edit-mode on that seq.
- the "mode" banner beats a general breadcrumb but yields to a terminal error.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import reyn.interfaces.tui.app as app_mod
from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView, InputBar
from reyn.interfaces.tui.widgets.sticky_status import StickyStatus


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


async def _registry_with_checkpoints(tmp_path: Path) -> AgentRegistry:
    """Real registry with one active branch + 2 checkpoints (no fork needed)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
    )
    AgentProfile.new("default", role="").save(tmp_path / ".reyn" / "agents" / "default")
    log = reg.state_log
    for mid in ("m1", "m2"):
        s = await log.append("inbox_consume", target="default", msg_id=mid)
        snap = AgentSnapshot.empty("default")
        snap.applied_seq = s
        reg._store_for("default").record(snap)
    return reg


def _sticky(app: ReynTUIApp) -> StickyStatus:
    conv = app.query_one("#conversation", ConversationView)
    return conv.query_one("#sticky-status", StickyStatus)


def _make_app(registry=None) -> ReynTUIApp:
    return ReynTUIApp(
        registry=registry, agent_name="default", model="test-model",
        budget_tracker=None,
    )


# ── edit-mode lifecycle (no picker needed) ──────────────────────────────────

@pytest.mark.asyncio
async def test_enter_exit_edit_mode_toggles_flag_and_banner() -> None:
    """Tier 2: enter_edit_mode sets edit_mode_active + shows the banner;
    exit_edit_mode clears both (public surface, no private-state assert)."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        assert app.edit_mode_active is False

        app.enter_edit_mode(7)
        await pilot.pause()
        assert app.edit_mode_active is True
        snap = _sticky(app).snapshot()
        assert "editing checkpoint #7" in snap.get("body", "")
        assert snap.get("kind") == "mode"

        app.exit_edit_mode()
        await pilot.pause()
        assert app.edit_mode_active is False
        # hide() deactivates the sticky (body text persists by design — the
        # #1546 hint clear keys on body too); assert the banner is no longer active.
        assert _sticky(app).snapshot().get("active") is False


@pytest.mark.asyncio
async def test_exit_edit_mode_resets_esc_esc_pending(monkeypatch) -> None:
    """Tier 2: exit_edit_mode resets the pending Esc-Esc first tap, so
    "exit-edit Esc then clean Esc" can't false-fire the picker (#1554 discipline).

    #1587: the window is widened so the real auto-clear ``set_timer`` can't fire
    mid-test (a slow 3.11 run zeroed the pending state before the first assert);
    this test pins the *reset-on-exit* path, not the timer-driven lapse."""
    monkeypatch.setattr(app_mod, "_ESC_ESC_WINDOW_S", 1_000_000.0)
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")          # arm a clean-Esc first tap
        assert app.esc_esc_pending is True
        app.enter_edit_mode(3)
        await pilot.pause()
        app.exit_edit_mode()                 # must disarm the first tap
        await pilot.pause()
        assert app.esc_esc_pending is False


# ── banner priority (lead-required render test) ─────────────────────────────

@pytest.mark.asyncio
async def test_mode_banner_beats_general_breadcrumb() -> None:
    """Tier 2: a routine general breadcrumb showing → entering edit-mode
    overwrites it with the banner (mode 85 > general 50)."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app.query_one("#conversation", ConversationView).show_status(
            "↑ turn 2 / 5", kind="general",
        )
        await pilot.pause()
        app.enter_edit_mode(4)
        await pilot.pause()
        assert "editing checkpoint #4" in _sticky(app).snapshot().get("body", "")


@pytest.mark.asyncio
async def test_mode_banner_yields_to_terminal_error() -> None:
    """Tier 2: a terminal error showing (priority 110) → entering edit-mode does
    NOT overwrite it (mode 85 < 110), so a critical failure stays visible."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app.query_one("#conversation", ConversationView).show_status(
            "✗ budget exceeded", kind="error", terminal=True,
        )
        await pilot.pause()
        app.enter_edit_mode(4)
        await pilot.pause()
        body = _sticky(app).snapshot().get("body", "")
        assert "budget exceeded" in body
        assert "editing checkpoint" not in body


# ── nav suspension + Esc priority-3.5 + `e` (picker needed) ──────────────────

@pytest.mark.asyncio
async def test_nav_and_edit_bindings_suspended_during_edit(tmp_path) -> None:
    """Tier 2: with the picker open, rewind_prev/next/confirm + edit_checkpoint
    are live; entering edit-mode suspends ALL of them (so ↑/↓/Enter reach the
    InputBar and `e` types) — the e2e flow-trace catch."""
    reg = await _registry_with_checkpoints(tmp_path)
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._open_rewind_menu()
        await pilot.pause()
        assert app.rewind_menu_open is True
        for act in ("rewind_prev", "rewind_next", "rewind_confirm", "edit_checkpoint"):
            assert app.check_action(act, ()) is True, act

        app.enter_edit_mode(1)
        await pilot.pause()
        for act in ("rewind_prev", "rewind_next", "rewind_confirm", "edit_checkpoint"):
            assert app.check_action(act, ()) is False, act


@pytest.mark.asyncio
async def test_esc_during_edit_exits_edit_keeps_picker(tmp_path) -> None:
    """Tier 2: Esc while editing exits edit-mode but KEEPS the picker mounted
    (priority-3.5, before the rewind-menu dismiss) and resets the first tap."""
    reg = await _registry_with_checkpoints(tmp_path)
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._open_rewind_menu()
        await pilot.pause()
        app.enter_edit_mode(1)
        await pilot.pause()
        assert app.edit_mode_active is True and app.rewind_menu_open is True

        await pilot.press("escape")
        await pilot.pause()
        assert app.edit_mode_active is False     # edit exited
        assert app.rewind_menu_open is True       # picker stays
        assert app.esc_esc_pending is False       # reset discipline


@pytest.mark.asyncio
async def test_ctrl_t_enters_edit_mode_on_selected_seq(tmp_path) -> None:
    """Tier 2: ``ctrl+t`` while the picker is open enters edit-mode on the
    highlighted checkpoint's seq (action_edit_checkpoint → enter_edit_mode).

    ``ctrl+t`` (not bare ``e``: the can_focus=False picker keeps the InputBar
    focused and swallows printable keys; and not ``ctrl+e``: the focused
    TextArea binds ctrl+e → cursor_line_end). ``ctrl+t`` is TextArea-unbound, so
    the app priority binding reaches it — verified in tmux (run_test alone gave
    a false positive for ctrl+e, hence the tmux gate)."""
    reg = await _registry_with_checkpoints(tmp_path)
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._open_rewind_menu()
        await pilot.pause()
        selected = app._rewind_menu.selected_point()
        assert selected is not None
        expected_seq = int(selected["seq"])

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.edit_mode_active is True
        assert f"editing checkpoint #{expected_seq}" in _sticky(app).snapshot().get("body", "")


@pytest.mark.asyncio
async def test_edit_checkpoint_noop_when_already_editing(tmp_path) -> None:
    """Tier 2: the edit binding is inert while already editing — check_action
    gates ``edit_checkpoint`` off so ``ctrl+t`` does not re-enter (and a direct
    action call is guarded)."""
    reg = await _registry_with_checkpoints(tmp_path)
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._open_rewind_menu()
        await pilot.pause()
        app.enter_edit_mode(1)
        await pilot.pause()
        # check_action gates `e` off while editing → action does not re-fire.
        assert app.check_action("edit_checkpoint", ()) is False
        app.action_edit_checkpoint()   # direct call is a no-op (guard)
        await pilot.pause()
        assert app.edit_mode_active is True   # still seq 1, not re-entered


@pytest.mark.asyncio
async def test_ctrl_t_gated_off_on_first_turn_checkpoint(tmp_path) -> None:
    """Tier 2: edit (ctrl+t) is inert on the FIRST-turn checkpoint — there is no
    prior turn to fork from (predecessor_turn_checkpoint None, #1567), so the
    affordance fails open elsewhere but is gated off here rather than entering
    edit-mode then bouncing on submit. The newer checkpoint (has a predecessor)
    stays editable."""
    reg = await _registry_with_checkpoints(tmp_path)   # 2 turn checkpoints, 1 branch
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._open_rewind_menu()
        await pilot.pause()
        # default selection = newest checkpoint → has a predecessor turn → editable
        newest = app._rewind_menu.selected_point()
        assert reg.predecessor_turn_checkpoint(int(newest["seq"])) is not None
        assert app.check_action("edit_checkpoint", ()) is True
        # navigate to the oldest (= first turn) → no predecessor → ctrl+t gated off
        app._rewind_menu.move_selection(+1)
        await pilot.pause()
        oldest = app._rewind_menu.selected_point()
        assert reg.predecessor_turn_checkpoint(int(oldest["seq"])) is None
        assert app.check_action("edit_checkpoint", ()) is False
        # nav bindings stay live regardless of predecessor (only edit is gated)
        assert app.check_action("rewind_prev", ()) is True


@pytest.mark.asyncio
async def test_esc_cancel_restores_pre_edit_draft(tmp_path) -> None:
    """Tier 2: Esc-cancel from edit-mode restores the pre-edit draft (full undo
    of the pre-fill) — the binding path (action_voice_cancel priority-3.5) calls
    _restore_pre_edit_input, the prod call-site for the data-flow's restore. #1571.

    Wire test (verify-live-dispatch): drives the real Esc key, not the restore
    method directly, so it pins that the binding actually reaches it."""
    reg = await _registry_with_checkpoints(tmp_path)
    app = _make_app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        bar.set_text("my unfinished draft")
        app._open_rewind_menu()
        await pilot.pause()
        seq = int(app._rewind_menu.selected_point()["seq"])
        reg.anchor_store.capture(seq, "anchor…", full="the loaded checkpoint message")
        app.action_edit_checkpoint()       # enter edit-mode + prefill (saves draft)
        await pilot.pause()
        assert app.edit_mode_active is True
        assert bar.current_text() == "the loaded checkpoint message"   # prefilled
        await pilot.press("escape")        # Esc-cancel via the binding path
        await pilot.pause()
        assert app.edit_mode_active is False                # edit exited
        assert app.rewind_menu_open is True                 # picker kept (priority-3.5)
        assert bar.current_text() == "my unfinished draft"  # draft restored (full undo)
