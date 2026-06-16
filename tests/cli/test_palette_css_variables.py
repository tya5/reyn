"""Tier 2: the palette → CSS-variable bridge stays consistent + is injected.

`.tcss` files can't `import` the Python palette tokens, so they historically
mirrored the hex values as hand-synced literals (a drift hazard). The App now
injects `_palette.css_variables()` via `get_css_variables`, letting `theme.tcss`
reference `$reyn-*` instead of hardcoding hex — `_palette.py` becomes the single
source for CSS-side colours too.

These pin the bridge CONTRACT (not specific hex — not a format-pin):
  - each `reyn-*` CSS var equals its source palette token (so the bridge can't
    silently diverge from the tokens)
  - the App actually injects the `reyn-*` vars on top of Textual's own
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui import _palette
from reyn.interfaces.tui._palette import _BG_HEADER, _TEXT_BODY, css_variables


def test_css_variables_equal_their_source_tokens() -> None:
    """Tier 2: every reyn-* CSS var resolves to its palette token (no drift).

    Pins the single-source invariant: the CSS-var map is a view onto the
    tokens, not an independently-maintained copy. Maps each `reyn-<name>`
    back to the `_<NAME>` token and asserts equality.
    """
    cssv = css_variables()
    assert cssv, "css_variables() must not be empty"
    for name, value in cssv.items():
        token_attr = "_" + name.removeprefix("reyn-").upper().replace("-", "_")
        token_val = getattr(_palette, token_attr, None)
        assert token_val is not None, (
            f"CSS var ${name} has no matching palette token {token_attr}"
        )
        assert value == token_val, (
            f"CSS var ${name} ({value}) diverged from {token_attr} ({token_val}) "
            "— the bridge must mirror the token, not hold a stale copy."
        )


@pytest.mark.asyncio
async def test_app_injects_palette_css_variables() -> None:
    """Tier 2: the App's get_css_variables includes the reyn-* vars + Textual's."""
    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(
        registry=None, agent_name="t", model="m", budget_tracker=None,
    )
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        cssv = app.get_css_variables()
        # Custom palette vars present + correct (tokens imported by name so
        # the comparison reads a bare Name, not a private-attribute access).
        assert cssv.get("reyn-text-body") == _TEXT_BODY
        assert cssv.get("reyn-bg-header") == _BG_HEADER
        # Textual's own theme vars still present (merge didn't drop them).
        assert "primary" in cssv
