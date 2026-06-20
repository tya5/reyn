"""Tier 2: Ctrl+\\ screenshots land in .reyn/, never the cwd.

Real-terminal dogfood finding (2026-06-20): the screenshot action saved
``reyn_<ts>.svg`` into the current working directory — i.e. into whatever repo
the operator launched ``reyn chat`` from — and it was not gitignored, so it
showed as an untracked file (git-status clutter / accidental ``git add .``). A
tool must not pollute the user's working tree.

The screenshot now routes into the gitignored ``.reyn/screenshots/`` artifact
dir (project root when known, else ``~/.reyn/screenshots/``).

Falsification: if the action reverts to ``save_screenshot()`` with no path, a
new ``.svg`` appears in the cwd and ``test_action_screenshot_does_not_touch_cwd``
fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── pure helper: the directory contract ─────────────────────────────────────

def test_resolve_screenshot_dir_under_project_reyn() -> None:
    """Tier 2: a known project root → <root>/.reyn/screenshots."""
    from reyn.interfaces.tui.app import _resolve_screenshot_dir

    d = _resolve_screenshot_dir(Path("/tmp/some-project"))
    assert d == Path("/tmp/some-project/.reyn/screenshots")
    assert ".reyn" in d.parts and "screenshots" in d.parts


def test_resolve_screenshot_dir_falls_back_to_home_reyn() -> None:
    """Tier 2: no project root → ~/.reyn/screenshots (never the cwd)."""
    from reyn.interfaces.tui.app import _resolve_screenshot_dir

    d = _resolve_screenshot_dir(None)
    assert d == Path.home() / ".reyn" / "screenshots"
    assert ".reyn" in d.parts


def test_resolve_screenshot_dir_is_never_bare_cwd() -> None:
    """Tier 2: the resolved dir is always under a ``.reyn`` segment.

    Falsification: a cwd-relative save (the bug) would have no ``.reyn`` segment.
    """
    from reyn.interfaces.tui.app import _resolve_screenshot_dir

    for root in (Path("/tmp/p"), None):
        d = _resolve_screenshot_dir(root)
        assert ".reyn" in d.parts, f"screenshot dir must be under .reyn, got {d}"


# ── e2e: the action does not pollute the cwd ────────────────────────────────

@pytest.mark.asyncio
async def test_action_screenshot_does_not_touch_cwd(tmp_path, monkeypatch) -> None:
    """Tier 2: action_screenshot writes into <project>/.reyn/screenshots, not cwd.

    Falsification: pre-fix this dropped a ``reyn_*.svg`` into the cwd — the
    cwd-svg snapshot below would grow.
    """
    # The open() subprocess is an OS side-effect (Preview / xdg-open); stub it so
    # the test doesn't spawn a viewer. Not a collaborator — just an OS effect.
    import subprocess

    from reyn.interfaces.tui.app import ReynTUIApp
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)

    cwd_svgs_before = set(Path.cwd().glob("*.svg"))

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        # Pin the project root to tmp so the screenshot lands under tmp/.reyn.
        monkeypatch.setattr(app, "_project_root_path", lambda: tmp_path)
        app.action_screenshot()
        await pilot.pause()

    shots = list((tmp_path / ".reyn" / "screenshots").glob("*.svg"))
    assert shots, "expected an SVG under <project>/.reyn/screenshots"

    cwd_svgs_after = set(Path.cwd().glob("*.svg"))
    assert cwd_svgs_after == cwd_svgs_before, (
        f"screenshot polluted the cwd: {cwd_svgs_after - cwd_svgs_before}"
    )
