"""Tier 2: Docs filter clears on Esc (A-F4).

Before this fix, the only path to clear an active docs filter was
running ``/docs-filter`` with no argument — a 4-step ritual (press
``/``, see pre-filled ``/docs-filter ...``, delete the pre-fill,
submit empty). Esc was a silent no-op when the docs tab had a
filter active.

Now Esc on the docs tab with an active filter clears in place. The
hint text in the rendered docs output also updates from "(clear via
/docs-filter)" to "(Esc to clear)" so the affordance is discoverable.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_render_docs_filter_hint_advertises_esc_to_clear(tmp_path) -> None:
    """Tier 2: rendered docs output advertises Esc clear path."""
    from reyn.chat.tui.widgets.right_panel.docs_tab import render_docs

    # Build a docs/ tree so the renderer has something to enumerate.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("# alpha\n", encoding="utf-8")
    (docs / "beta.md").write_text("# beta\n", encoding="utf-8")

    from reyn.chat.tui.widgets.right_panel.docs_tab import build_docs_index
    groups, _flat = build_docs_index(tmp_path, docs_filter="alpha")
    rendered = render_docs(tmp_path, 0, groups, docs_filter="alpha")
    # New hint is present.
    assert "Esc to clear" in rendered
    # Old wording must not linger.
    assert "/docs-filter" not in rendered


def test_render_docs_no_filter_omits_clear_hint(tmp_path) -> None:
    """Tier 2: no filter active → no clear hint rendered."""
    from reyn.chat.tui.widgets.right_panel.docs_tab import render_docs

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("# alpha\n", encoding="utf-8")

    from reyn.chat.tui.widgets.right_panel.docs_tab import build_docs_index
    groups, _flat = build_docs_index(tmp_path, docs_filter="")
    rendered = render_docs(tmp_path, 0, groups, docs_filter="")
    assert "Esc to clear" not in rendered
    assert "filter:" not in rendered


def test_right_panel_on_key_esc_clears_docs_filter() -> None:
    """Tier 2: Esc on docs tab with active filter clears in place.

    Constructs a minimal RightPanel via ``__new__`` since
    ``_docs_filter`` mutation + ``_invalidate`` are all that the Esc
    branch touches; full Textual app context isn't needed to verify
    the state mutation.
    """
    from reyn.chat.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "docs"
    panel._docs_filter = "alpha"
    invalidated = []
    # Stub _invalidate so the call doesn't need a mounted widget.
    panel._invalidate = lambda: invalidated.append(True)

    # Stub event object matching the .key API the handler reads.
    class _Event:
        key = "escape"
        prevented = False
        stopped = False

        def prevent_default(self):
            self.prevented = True

        def stop(self):
            self.stopped = True

    event = _Event()
    panel.on_key(event)

    assert panel.docs_filter == ""
    assert invalidated == [True]
    assert event.prevented is True
    assert event.stopped is True
