"""RenderableCacheMixin — shared "what did the widget last render?" surface.

Several Static-backed widgets (= SkillActivityRow, SlashPicker, and the
upcoming ReynHeader migration) need a stable test-side accessor for
the most-recent renderable. Textual's ``Static`` / ``Label`` do NOT
expose a portable ``.renderable`` getter across versions — sometimes
``_renderable`` (private), sometimes a versioned property, sometimes
neither. Each widget had ended up duplicating the same ~10-line cache
pattern:

  self._rendered_cache: Text | None = None
  ...
  def rendered_text(self) -> str:
      cache = self._rendered_cache
      return str(cache.plain) if cache is not None else ""

This mixin consolidates the pattern. Widgets call
``self._set_rendered_cache(text)`` immediately after every
``Static.update(text)`` / ``Label.update(text)`` call, and tests read
``widget.rendered_text()`` for assertion. Accepts both ``Text`` and
``str`` — the latter is wrapped before storage so ``.plain`` always
works downstream.

Trigger for extraction: 3 occurrences of the same pattern (see
``feedback_test_public_surface_not_private_state`` philosophy — the
mixin keeps tests reading public surfaces while the production code
stays decoupled from Textual's private attributes).
"""
from __future__ import annotations

from rich.text import Text


class RenderableCacheMixin:
    """Mix-in caching the last Text rendered into the underlying Static.

    Storage: ``Text`` (so ``.plain`` is always available). ``str`` input
    is wrapped at set-time so the read path stays uniform regardless
    of which call site produced the renderable.

    Subclasses MUST call :py:meth:`_set_rendered_cache` right after
    every ``Static.update`` / ``Label.update`` invocation; they're
    paired updates, not a single funnel — keeping them adjacent is
    the simplest way to ensure cache and widget stay in sync.

    Subclasses MUST NOT mutate ``_rendered_cache`` directly. The
    contract is "set via the helper, read via rendered_text".
    """

    # Class-level annotation so dataclass-style subclasses can still
    # type their fields; the per-instance value is set lazily on
    # first call to ``_set_rendered_cache``.
    _rendered_cache: Text | None = None

    def _set_rendered_cache(self, renderable: Text | str) -> None:
        """Record what was just rendered. Accepts ``Text`` or ``str``."""
        if isinstance(renderable, Text):
            self._rendered_cache = renderable
        else:
            # str input — wrap in Text so ``.plain`` access on read
            # is uniform with the Text case. Empty string maps to
            # ``Text("")`` (whose ``.plain`` is also ``""``).
            self._rendered_cache = Text(str(renderable))

    def rendered_text(self) -> str:
        """Plain-text rendering of the last frame sent to the widget.

        Returns ``""`` when nothing has been rendered yet (= pre-mount,
        or the widget was never updated). Stable across Textual
        versions because the value lives in our own cache, not in
        the framework widget's private renderable slot.
        """
        cache = self._rendered_cache
        if cache is None:
            return ""
        return str(cache.plain)


__all__ = ["RenderableCacheMixin"]
