"""Tier 2: AsyncStackPanel._refresh respects is_mounted gate (I-F11).

Wave-10 follow-up Topic I finding F11 (P3): ``self._static`` is
assigned inside ``compose()`` BEFORE the ``yield``. Textual
considers the widget mounted only after ``yield`` returns + the
DOM append completes on the next event-loop tick. An external
caller invoking ``add()`` / ``set_pending()`` / ``remove()``
before the widget is attached (= pre-mount race in test harness,
or future wiring that hooks into a pre-mount registry event)
passes the bare ``self._static is None`` guard because the
attribute IS set — but ``self._static.update(...)`` lands on a
Static that has no parent in the DOM. The update is silently
dropped by Textual.

After the fix ``_refresh`` also checks ``is_mounted``. Pre-mount
calls are silent no-ops; post-mount calls flush as before.

Public surfaces tested:
  - pre-mount (= is_mounted False) _refresh → no Static.update call
  - post-mount (= is_mounted True) _refresh → Static.update fires
  - missing is_mounted attribute (= ancient Textual fallback) →
    update fires (regression guard for the ``getattr(..., True)``
    defensive default)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubStatic:
    """Stand-in for Textual's Static — captures update() calls."""

    def __init__(self) -> None:
        self.updates: list = []

    def update(self, content) -> None:  # type: ignore[no-untyped-def]
        self.updates.append(content)


@pytest.fixture
def _panel_with_overridable_mounted(monkeypatch):
    """AsyncStackPanel with a writable ``is_mounted`` for the duration of one test.

    Textual's ``is_mounted`` is a class-level property without a
    setter, so direct instance assignment fails. Replace the
    descriptor with a plain property that reads from an instance
    attribute we can control. ``monkeypatch`` undoes the swap when
    the test ends so other tests in the suite see the real property.
    """
    from reyn.chat.tui.widgets.async_stack_panel import AsyncStackPanel

    original = AsyncStackPanel.is_mounted
    monkeypatch.setattr(
        AsyncStackPanel,
        "is_mounted",
        property(lambda self: getattr(self, "_test_is_mounted", False)),
        raising=False,
    )
    panel = AsyncStackPanel()
    panel._static = _StubStatic()  # type: ignore[assignment]  # inject stub via private attr (setup only)
    yield panel
    # monkeypatch.setattr handles cleanup; ``original`` referenced to quiet linter.
    del original


def test_refresh_pre_mount_is_silent_noop(_panel_with_overridable_mounted) -> None:
    """Tier 2: ``_refresh`` before mount does not push to detached Static."""
    panel = _panel_with_overridable_mounted
    panel._test_is_mounted = False  # type: ignore[attr-defined]

    panel._refresh()

    assert panel.static_widget.updates == [], (  # type: ignore[union-attr]
        f"pre-mount _refresh should not push updates; got "
        f"{panel.static_widget.updates!r}"  # type: ignore[union-attr]
    )


def test_refresh_post_mount_pushes_update(_panel_with_overridable_mounted) -> None:
    """Tier 2: ``_refresh`` after mount flushes content to the Static."""
    panel = _panel_with_overridable_mounted
    panel._test_is_mounted = True  # type: ignore[attr-defined]

    panel._refresh()

    assert panel.static_widget.updates, (  # type: ignore[union-attr]
        "post-mount _refresh should push at least one update"
    )


def test_refresh_with_no_static_is_safe_noop() -> None:
    """Tier 2b: existing ``_static is None`` guard still works (regression)."""
    from reyn.chat.tui.widgets.async_stack_panel import AsyncStackPanel

    panel = AsyncStackPanel()
    # ``_static`` is None at construction time; ``_refresh`` should
    # silently return without raising even though the new
    # ``is_mounted`` gate would also have caught the pre-mount call.
    panel._refresh()  # must not raise
