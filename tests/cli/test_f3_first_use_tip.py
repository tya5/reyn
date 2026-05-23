"""Tier 2: F3 first-use onboarding tip (T2-4, Wave-12 Topic B #2).

On the first F3 press per project, a one-time conv-pane status message
is shown explaining drill-down. Gated by ``tip_f3_seen`` in
``.reyn/tui_prefs.json`` so it never re-fires.

Pinned:
  - Default tui_prefs.json (absent / empty) treats tip_f3_seen as False.
  - Round-trip: save tip_f3_seen=True, reload → flag is True.
  - _maybe_emit_f3_tip with unseen flag → emits message containing
    "drill-down" and returns True; prefs file updated.
  - _maybe_emit_f3_tip with seen flag → no emission, returns False.
  - (App-level) first F3 press with tip unseen → status contains
    "drill-down"; subsequent press → no drill-down tip.
  - (App-level) tip fires even when no in-flight skill rows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Tier 2 — prefs-level tests (no Pilot / app required)
# ---------------------------------------------------------------------------


def test_tip_f3_seen_absent_treated_as_false(tmp_path: Path) -> None:
    """Tier 2: missing tui_prefs.json → tip_f3_seen defaults to False."""
    from reyn.chat.tui.prefs import load_tui_prefs

    prefs = load_tui_prefs(tmp_path)
    assert prefs.get("tip_f3_seen", False) is False


def test_tip_f3_seen_round_trip(tmp_path: Path) -> None:
    """Tier 2: save tip_f3_seen=True, reload → flag is True."""
    from reyn.chat.tui.prefs import load_tui_prefs, save_tui_prefs

    prefs: dict[str, Any] = {}
    prefs["tip_f3_seen"] = True
    save_tui_prefs(tmp_path, prefs)

    restored = load_tui_prefs(tmp_path)
    assert restored.get("tip_f3_seen") is True


# ---------------------------------------------------------------------------
# Tier 2 — helper-level tests (_maybe_emit_f3_tip extracted function)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_emit_f3_tip_emits_when_unseen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: _maybe_emit_f3_tip with unseen flag emits and returns True."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        emitted: list[str] = []
        result = app._maybe_emit_f3_tip(emitted.append)

        assert result is True
        assert len(emitted) == 1
        assert "drill-down" in emitted[0]

        # Flag persisted to disk.
        prefs_path = tmp_path / ".reyn" / "tui_prefs.json"
        assert prefs_path.exists()
        data = json.loads(prefs_path.read_text())
        assert data.get("tip_f3_seen") is True


@pytest.mark.asyncio
async def test_maybe_emit_f3_tip_silent_when_already_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: _maybe_emit_f3_tip with seen flag does not emit, returns False."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.prefs import save_tui_prefs

    # Pre-seed the flag.
    save_tui_prefs(tmp_path, {"tip_f3_seen": True})

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        emitted: list[str] = []
        result = app._maybe_emit_f3_tip(emitted.append)

        assert result is False
        assert emitted == []


# ---------------------------------------------------------------------------
# Tier 2 — app-level F3 integration via Pilot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f3_first_press_shows_drill_down_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: first F3 press with unseen tip → status contains "drill-down"."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # No rows in flight — the tip should still fire on first press.
        assert conv.in_flight_skill_rows() == []
        app.action_skill_expand_toggle()
        await pilot.pause()

        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "drill-down" in snap["body"]

        # Flag now persisted.
        prefs_path = tmp_path / ".reyn" / "tui_prefs.json"
        data = json.loads(prefs_path.read_text())
        assert data.get("tip_f3_seen") is True


@pytest.mark.asyncio
async def test_f3_second_press_no_drill_down_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: F3 with tip already seen → status shows "no active skill", not tip."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.prefs import save_tui_prefs
    from reyn.chat.tui.widgets import ConversationView

    # Pre-seed the seen flag.
    save_tui_prefs(tmp_path, {"tip_f3_seen": True})

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        assert conv.in_flight_skill_rows() == []
        app.action_skill_expand_toggle()
        await pilot.pause()

        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        # "drill-down" tip must NOT appear; the standard hint should.
        assert "drill-down" not in snap["body"]
        assert "no active skill" in snap["body"]
