"""Tier 1 + Tier 2: header trims model-id date suffix and dims the field.

Visual UX audit (MED severity Finding F4): the model field
(e.g. ``claude-opus-4-5-20251101``) is a long machine-readable identifier
that rarely changes within a session, but it sat between ``agent_name``
and the metrics that DO change every turn — eating ~25 horizontal cells
and pushing the clock canary off the right edge on narrow terminals.

The fix:

  1. Strip the universally redundant trailing date suffix
     (``-YYYYMMDD`` 8 digits or ``-YYYY-MM-DD``) and ``-latest``.
  2. Render the model field DIM so the user's eye gravitates to the
     per-turn metrics (tokens / cost / clock).

Tier 1: pure-function test of ``_shorten_model_id`` across the common
provider id shapes. Tier 2: header status integration — model variants
appear in the status text in the expected shortened form.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ReynHeader
from reyn.chat.tui.widgets.header import _shorten_model_id

# ── Tier 1: pure-function bucket coverage ────────────────────────────────────


@pytest.mark.parametrize(
    "model, expected",
    [
        # Anthropic 8-digit-date form
        ("claude-opus-4-5-20251101",       "claude-opus-4-5"),
        ("claude-3-7-sonnet-20250219",     "claude-3-7-sonnet"),
        ("claude-haiku-4-5-20251001",      "claude-haiku-4-5"),
        # OpenAI YYYY-MM-DD form
        ("gpt-4o-2024-08-06",              "gpt-4o"),
        ("gpt-4-turbo-2024-04-09",         "gpt-4-turbo"),
        # Google -latest suffix
        ("gemini-1.5-flash-latest",        "gemini-1.5-flash"),
        ("gemini-2.0-pro-latest",          "gemini-2.0-pro"),
        # No-suffix cases — passthrough
        ("claude-sonnet-4-6",              "claude-sonnet-4-6"),
        ("gpt-3.5-turbo",                  "gpt-3.5-turbo"),
        ("ollama/llama3",                  "ollama/llama3"),
        # Empty
        ("",                                ""),
        # Pathological — version-looking tail that is NOT a date
        ("model-2024",                     "model-2024"),  # 4 digits, not 8
        ("claude-3-5",                     "claude-3-5"),  # version, not date
    ],
)
def test_shorten_model_id_buckets(model: str, expected: str) -> None:
    """Tier 1: each provider-shape maps to the expected shortened form."""
    assert _shorten_model_id(model) == expected, (
        f"_shorten_model_id({model!r}) wrong: got {_shorten_model_id(model)!r}, "
        f"expected {expected!r}"
    )


def test_shorten_never_returns_empty_for_non_empty_input() -> None:
    """Tier 1: paranoia — if a future pattern would strip everything, fall back to raw."""
    # Any single-token model id with no separator should pass through untouched.
    assert _shorten_model_id("opus") == "opus"
    assert _shorten_model_id("a") == "a"


# ── Tier 2: header integration ───────────────────────────────────────────────


def _make_app(model: str) -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="aria",
        model=model,
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_header_status_shortens_anthropic_date_suffix() -> None:
    """Tier 2: ``claude-opus-4-5-20251101`` renders as ``claude-opus-4-5`` in the header."""
    app = _make_app("claude-opus-4-5-20251101")
    async with app.run_test(headless=True, size=(180, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        text = str(header._format_status())
        assert "claude-opus-4-5" in text
        # The date suffix must NOT appear
        assert "20251101" not in text, (
            f"date suffix leaked into header: {text!r}"
        )


@pytest.mark.asyncio
async def test_header_status_shortens_openai_dashed_date() -> None:
    """Tier 2: ``gpt-4o-2024-08-06`` renders as ``gpt-4o``."""
    app = _make_app("gpt-4o-2024-08-06")
    async with app.run_test(headless=True, size=(180, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        text = str(header._format_status())
        assert "gpt-4o" in text
        assert "2024-08-06" not in text, text


@pytest.mark.asyncio
async def test_header_status_strips_latest_suffix() -> None:
    """Tier 2: ``gemini-1.5-flash-latest`` renders as ``gemini-1.5-flash``."""
    app = _make_app("gemini-1.5-flash-latest")
    async with app.run_test(headless=True, size=(180, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        text = str(header._format_status())
        assert "gemini-1.5-flash" in text
        assert "-latest" not in text, text


@pytest.mark.asyncio
async def test_header_status_passthrough_for_undated_models() -> None:
    """Tier 2: models without a date suffix render unchanged."""
    app = _make_app("claude-sonnet-4-6")
    async with app.run_test(headless=True, size=(180, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        text = str(header._format_status())
        assert "claude-sonnet-4-6" in text


@pytest.mark.asyncio
async def test_header_status_still_contains_agent_and_metrics() -> None:
    """Tier 2: shortening doesn't drop other fields (agent, tokens, cost, clock).

    Regression check that the refactor of ``_format_status`` from a flat
    list to a list-of-tuples didn't accidentally swallow a part.
    """
    app = _make_app("claude-opus-4-5-20251101")
    async with app.run_test(headless=True, size=(180, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        text = str(header._format_status())
        assert "aria" in text                  # agent_name
        assert "tok" in text                   # tokens
        assert "$" in text                     # cost
        assert ":" in text                     # clock (HH:MM:SS)
