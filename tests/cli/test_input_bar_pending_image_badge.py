"""Tier 2: InputBar hint footer shows a pending-image count badge (B5).

When ``session.pending_user_images`` is non-empty, the ``#hints`` Label
appends a ``📎 N image(s)`` badge to the normal hint text so the user
can see how many images are queued while composing a long prompt.

Public surfaces tested:
  - ``refresh_hint()`` with N pending images → ``#hints`` Label text
    contains the count badge (singular and plural forms).
  - ``refresh_hint()`` with 0 pending images → no badge in hint.
  - The in-flight hint (``_HINT_IN_FLIGHT``) takes precedence over the
    image badge when in-flight — badge is suppressed.

The test drives a live Textual app via ``run_test`` and asserts on the
rendered ``#hints`` Label text (public surface), not private fields.
``refresh_hint()`` is the public entry-point we call to drive badge
updates (the same method the app's slash ``finally`` block calls).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _label_text(label) -> str:
    """Read the live rendered text from a Textual Label widget."""
    return str(label.content)


class _FakeSession:
    """Minimal session stub exposing ``pending_user_images``.

    The app's mount path calls a few session methods while wiring up the
    UI (e.g. ``register_intervention_listener``); they are stubbed as
    no-ops so ``app.run_test()`` mounts cleanly with this stub injected
    via ``_get_session``. The badge logic only reads ``pending_user_images``.
    """

    def __init__(self, pending: int = 0) -> None:
        self._queue: list[dict] = [{"type": "image"} for _ in range(pending)]

    @property
    def pending_user_images(self) -> list[dict]:
        return self._queue

    def __getattr__(self, name: str):
        # Any session method the app mount touches that the badge test
        # doesn't care about (register_intervention_listener, etc.) is a
        # harmless no-op — keeps the stub minimal without whack-a-mole.
        return lambda *a, **k: None


@pytest.mark.asyncio
async def test_badge_shown_when_images_pending() -> None:
    """Tier 2: ``refresh_hint()`` adds badge when pending count > 0.

    A session with 2 pending images is attached to the app via the
    ``_get_session`` hook.  After calling ``refresh_hint()`` the
    ``#hints`` Label text must contain the count.
    """
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=2)

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    # Monkeypatch _get_session to return our stub.
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.refresh_hint()
        await pilot.pause()

        text = _label_text(label)
        assert "2" in text, f"badge count missing from hint: {text!r}"
        assert "image" in text, f"badge word 'image' missing from hint: {text!r}"
        assert "📎" in text, f"paperclip icon missing from hint: {text!r}"


@pytest.mark.asyncio
async def test_badge_singular_form_for_one_image() -> None:
    """Tier 2: exactly 1 pending image → singular ``image`` (not ``images``)."""
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=1)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.refresh_hint()
        await pilot.pause()

        text = _label_text(label)
        assert "1 image" in text, (
            f"expected '1 image' (singular) in hint; got: {text!r}"
        )
        assert "images" not in text, (
            f"plural 'images' leaked for count=1; got: {text!r}"
        )


@pytest.mark.asyncio
async def test_badge_plural_form_for_multiple_images() -> None:
    """Tier 2: 3 pending images → plural ``images``."""
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=3)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.refresh_hint()
        await pilot.pause()

        text = _label_text(label)
        assert "3 images" in text, (
            f"expected '3 images' (plural) in hint; got: {text!r}"
        )


@pytest.mark.asyncio
async def test_no_badge_when_queue_empty() -> None:
    """Tier 2: empty queue → no badge in hint text."""
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=0)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.refresh_hint()
        await pilot.pause()

        text = _label_text(label)
        assert "📎" not in text, f"badge appeared with empty queue: {text!r}"
        # Normal hint should still be present.
        assert "Enter" in text, f"normal hint missing after empty-queue refresh: {text!r}"


@pytest.mark.asyncio
async def test_in_flight_suppresses_badge() -> None:
    """Tier 2: in-flight hint takes precedence — badge is suppressed while locked.

    Preserves the existing B3 invariant: ``_HINT_IN_FLIGHT`` is shown
    exclusively while in-flight, even if images are pending.
    """
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=2)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        # Put the bar in-flight: the badge must be suppressed.
        bar.set_in_flight(True)
        await pilot.pause()

        text = _label_text(label)
        assert "responding" in text, (
            f"in-flight hint missing when in-flight: {text!r}"
        )
        assert "📎" not in text, (
            f"badge should be suppressed while in-flight; got: {text!r}"
        )


@pytest.mark.asyncio
async def test_badge_appears_after_unlock() -> None:
    """Tier 2: badge becomes visible again after ``set_in_flight(False)``
    when images are still queued (i.e. the user sent a text msg that
    doesn't drain the queue — edge case, but the badge must recover).
    """
    from textual.widgets import Label

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    fake_session = _FakeSession(pending=1)
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    app._get_session = lambda: fake_session  # type: ignore[method-assign]

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.set_in_flight(True)
        await pilot.pause()
        # Confirm in-flight hint is showing.
        assert "responding" in _label_text(label)

        # Unlock — the session still has 1 image pending; badge should reappear.
        bar.set_in_flight(False)
        await pilot.pause()

        text = _label_text(label)
        assert "📎" in text, (
            f"badge did not reappear after unlock with images still pending: {text!r}"
        )
        assert "1 image" in text, (
            f"badge count incorrect after unlock: {text!r}"
        )
