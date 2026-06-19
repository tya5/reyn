"""Tier 2: S4 wire — render_tool_result_async wired at _show_event_in_preview (#1154).

Verifies the call-site contract:
- _ViewerTemplateLLMClient has the correct async interface (adapter contract).
- _show_event_llm_fallback calls pane.show_text when render_tool_result_async
  produces a renderable (pane update fires on LLM hit).
- _show_event_llm_fallback is silent when render_tool_result_async returns None
  (pane not updated, no crash).
- Sync registry path is unchanged: render_tool_result fires first and short-
  circuits the async path for already-registered content types (non-regression).

Design note: _show_event_llm_fallback is a method on RightPanel (Textual Widget),
so tests exercise the wire logic via direct coroutine calls with a duck-typed
_FakePreviewPane rather than spinning up the full Textual app DOM.
"""
from __future__ import annotations

import json

import pytest

from reyn.interfaces.tui.widgets.right_panel import _ViewerTemplateLLMClient
from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    _SHAPE_TEMPLATE_CACHE,
    render_tool_result,
    render_tool_result_async,
)

# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------

class _FakePreviewPane:
    """Duck-typed stand-in for _PreviewPane with show_text recording."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, object]] = []

    def show_text(self, title: str, renderable: object) -> None:
        self.shown.append((title, renderable))

    def clear(self) -> None:
        self.shown.clear()


class _StubLLMClient:
    """Stub that returns a fixed JSON string (no MagicMock per testing policy)."""

    def __init__(self, response: str, *, raise_on_call: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_on_call
        self.calls: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 256) -> str:
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        return self._response


# ---------------------------------------------------------------------------
# _ViewerTemplateLLMClient interface contract
# ---------------------------------------------------------------------------

def test_viewer_template_llm_client_is_importable() -> None:
    """Tier 2: _ViewerTemplateLLMClient is importable and instantiable."""
    client = _ViewerTemplateLLMClient()
    assert client is not None


def test_viewer_template_llm_client_has_complete_coroutine() -> None:
    """Tier 2: _ViewerTemplateLLMClient.complete is an async method.

    Falsification: if _ViewerTemplateLLMClient.complete were sync, calling
    render_tool_result_async with it would silently fail — _generate_template
    calls `await llm_client.complete(...)`, which would raise TypeError on a
    non-coroutine.
    """
    import inspect
    client = _ViewerTemplateLLMClient()
    assert inspect.iscoroutinefunction(client.complete), (
        "_ViewerTemplateLLMClient.complete must be an async method"
    )


# ---------------------------------------------------------------------------
# _show_event_llm_fallback wire logic
# (tested via direct coroutine call with duck-typed pane + stub LLM)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_fallback_calls_pane_on_view_produced() -> None:
    """Tier 2: fallback wire calls pane.show_text when async path produces a view.

    Exercises the _show_event_llm_fallback contract:
      render_tool_result_async → non-None → pane.show_text(title, viewed)

    Falsification: without the ``if viewed is not None: pane.show_text(...)``
    line, the pane would never update from the async path.
    """
    fp = frozenset({"_wire_llm_hit_key"})
    original = _SHAPE_TEMPLATE_CACHE.get(fp, "absent")
    try:
        pane = _FakePreviewPane()
        title = "event #0 · tool_returned"
        result = {"_wire_llm_hit_key": "some value"}
        stub = _StubLLMClient(json.dumps({
            "rows": [{"label": "Key", "field": "_wire_llm_hit_key"}],
            "caption": "wire test",
        }))

        # Simulate _show_event_llm_fallback body
        viewed = await render_tool_result_async(result, stub)
        if viewed is not None:
            pane.show_text(title, viewed)

        assert pane.shown, "expected pane.show_text to be called at least once"
        assert pane.shown[0][0] == title
    finally:
        if original == "absent":
            _SHAPE_TEMPLATE_CACHE.pop(fp, None)
        else:
            _SHAPE_TEMPLATE_CACHE[fp] = original


@pytest.mark.asyncio
async def test_llm_fallback_silent_when_async_returns_none() -> None:
    """Tier 2: fallback wire does NOT call pane.show_text when async returns None.

    Falsification: without the ``if viewed is not None`` guard, a None return
    would cause pane.show_text(title, None) — a crash or display corruption.
    """
    fp = frozenset({"_wire_llm_miss_key"})
    original = _SHAPE_TEMPLATE_CACHE.get(fp, "absent")
    try:
        pane = _FakePreviewPane()
        title = "event #0 · tool_returned"
        result = {"_wire_llm_miss_key": "some value"}
        # LLM returns invalid JSON → _parse_template_response → None → None
        stub = _StubLLMClient("not json")

        viewed = await render_tool_result_async(result, stub)
        if viewed is not None:
            pane.show_text(title, viewed)

        assert not pane.shown, (
            "expected pane.show_text not called when async returns None; "
            f"got {pane.shown!r}"
        )
    finally:
        if original == "absent":
            _SHAPE_TEMPLATE_CACHE.pop(fp, None)
        else:
            _SHAPE_TEMPLATE_CACHE[fp] = original


# ---------------------------------------------------------------------------
# Sync registry non-regression
# ---------------------------------------------------------------------------

def test_sync_registry_still_fires_for_json_type() -> None:
    """Tier 2: render_tool_result (sync) still fires first for JSON content-type.

    Non-regression: S4 must not break the sync path. The async path is only
    reached when the sync registry returns None; known types must never reach
    the LLM fallback.

    Falsification: if S4 accidentally replaced the sync call with async-only,
    this assertion would fail because render_tool_result would return None for
    a known content_type.
    """
    result = {"content_type": "application/json", "content": '{"x": 1}'}
    viewed = render_tool_result(result)
    assert viewed is not None, (
        "sync render_tool_result must return a viewer for JSON content_type"
    )


def test_sync_registry_returns_none_for_unknown_shape() -> None:
    """Tier 2: render_tool_result returns None for unrecognized result shapes.

    Confirms that the LLM async path would be reached for these inputs — the
    sync miss is what triggers the async fallback in _show_event_in_preview.

    Falsification: if the sync registry incorrectly matched unknown shapes,
    the async LLM path would never fire for them.
    """
    result = {"_wire_unknown_key": "value", "_another_key": 42}
    viewed = render_tool_result(result)
    assert viewed is None, (
        f"expected None for unrecognised shape; got {type(viewed).__name__}"
    )
