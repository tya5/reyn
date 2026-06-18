"""Tier 2: cost tab cache-hit display — cached_tokens aggregated and rendered.

Pins the cache-hit display added to the cost tab:
- ``_tok(p, c, cached)`` renders a dim third line ``⚡N cached (X% hit)``
  when cached > 0, and omits it when cached == 0.
- ``render_cost`` reads ``cached_tokens`` from ``llm_response_received``
  events and surfaces it in the TODAY and ALL TIME token rows.

Falsification:
- Without the cached parameter, ``_tok`` always returns a 2-line string;
  the cache-hit line would never appear.
- Without the aggregation fix, a synthetic event with ``cached_tokens``
  would not affect the rendered output (cache count stays 0).
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text

from reyn.interfaces.tui.widgets.right_panel.cost_tab import _tok, render_cost


def _plain(markup: str) -> str:
    """Strip Rich markup to plain text."""
    try:
        return Text.from_markup(markup).plain
    except Exception:
        import re
        return re.sub(r"\[[^\]]*\]", "", markup)


def _render_plain(project_root: Path) -> str:
    markup = render_cost(project_root, budget_tracker=None, content_width=60)
    return "\n".join(_plain(line) for line in markup.split("\n"))


def _write_events(project_root: Path, *, prompt_tokens: int,
                  completion_tokens: int, cached_tokens: int = 0) -> None:
    skill_dir = (
        project_root / ".reyn" / "events" / "agents" / "test-agent"
        / "skill_runs" / "2026-05"
    )
    skill_dir.mkdir(parents=True, exist_ok=True)
    events_file = skill_dir / "2026-05-30T120000_test.jsonl"
    called = json.dumps({
        "type": "llm_called",
        "timestamp": "2026-05-30T12:00:00+00:00",
        "data": {"model": "claude-sonnet"},
    })
    resp_data: dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": 0.01,
    }
    if cached_tokens:
        resp_data["cached_tokens"] = cached_tokens
    received = json.dumps({
        "type": "llm_response_received",
        "timestamp": "2026-05-30T12:00:01+00:00",
        "data": resp_data,
    })
    events_file.write_text(called + "\n" + received + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# _tok unit tests
# ---------------------------------------------------------------------------

def test_tok_with_cached_renders_hit_line() -> None:
    """Tier 2: _tok with cached>0 includes cache count and hit rate in output.

    Falsification: without the cached parameter the function never emits
    cache info; none of these assertions would pass.
    """
    out = _plain(_tok(1000, 200, cached=800))
    assert "800" in out, f"expected cached count 800 in output: {out!r}"
    assert "80%" in out, f"expected 80% hit rate in output: {out!r}"
    assert "cached" in out.lower(), f"expected 'cached' in output: {out!r}"


def test_tok_zero_cached_no_third_line() -> None:
    """Tier 2: _tok with cached=0 (default) does not include cache info.

    Falsification: if the cached line were always emitted, "cached" would
    appear in the output even with cached=0 and the assertion would fail.
    """
    out = _plain(_tok(1000, 200, cached=0))
    assert "cached" not in out.lower(), f"expected no cache info: {out!r}"


def test_tok_cached_equals_p_shows_100_percent() -> None:
    """Tier 2: when all prompt tokens are cached, hit rate shows 100%.

    Falsification: if pct were rounded incorrectly (e.g. floor of
    800/800=1.0 gave 99%), this assertion would fail.
    """
    out = _plain(_tok(800, 100, cached=800))
    assert "100%" in out, f"expected 100% hit rate: {out!r}"


# ---------------------------------------------------------------------------
# render_cost integration tests
# ---------------------------------------------------------------------------

def test_render_cost_cached_tokens_appear_in_all_time(tmp_path: Path) -> None:
    """Tier 2: when events have cached_tokens, ALL TIME token row shows cache hit.

    Falsification: without the aggregation fix (bucket["cached"] += cached),
    the cached field stays 0 and the hit line is never rendered.
    """
    _write_events(tmp_path, prompt_tokens=1000, completion_tokens=200,
                  cached_tokens=600)
    plain = _render_plain(tmp_path)
    assert "600" in plain, f"expected cached count 600 in output:\n{plain}"
    assert "60%" in plain, f"expected 60% hit rate in output:\n{plain}"


def test_render_cost_no_cached_tokens_no_hit_line(tmp_path: Path) -> None:
    """Tier 2: events without cached_tokens produce no cache-hit line.

    Falsification: if cached_tokens defaulted to a non-zero value, a hit
    line would appear even without the field, and this assertion would fail.
    """
    _write_events(tmp_path, prompt_tokens=1000, completion_tokens=200,
                  cached_tokens=0)
    plain = _render_plain(tmp_path)
    assert "cached" not in plain.lower(), (
        f"expected no cache line when cached_tokens=0:\n{plain}"
    )
