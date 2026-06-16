"""Tier 2: cost tab column widths adapt to narrow panel — token total always visible.

Pins the invariant that ``render_cost`` degrades gracefully as ``content_width``
shrinks:

1. At NARROW width (< _MEDIUM_THRESHOLD = 42) the agent-row token total
   (e.g. "3,500 tok") is present in the rendered output and is not clipped —
   even though cost and call-count fields ARE suppressed at that width.
2. At WIDE width (≥ _WIDE_THRESHOLD = 54) the full layout is unchanged:
   token total, cost, and call-count are all present.

These are checked on the PLAIN text returned by ``render_cost`` (= Rich markup
stripped), which is the public-surface contract of the function. No private
fields or internal state are asserted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text

from reyn.interfaces.tui.widgets.right_panel.cost_tab import (
    _MEDIUM_THRESHOLD,
    _WIDE_THRESHOLD,
    render_cost,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_markup(markup: str) -> str:
    """Strip Rich markup tags and return plain text for assertion."""
    try:
        return Text.from_markup(markup).plain
    except Exception:
        # If markup is broken, fall back to naive tag stripping.
        import re
        return re.sub(r"\[[^\]]*\]", "", markup)


def _render_plain(project_root: Path, content_width: int) -> str:
    """Return plain text of render_cost at the given content width."""
    markup = render_cost(project_root, budget_tracker=None,
                         content_width=content_width)
    # markup is a newline-joined string; strip each line individually.
    return "\n".join(_strip_markup(line) for line in markup.split("\n"))


def _make_events(project_root: Path, *, agent: str = "test-agent",
                 prompt_tokens: int = 3000, completion_tokens: int = 500,
                 cost_usd: float | None = 0.0123) -> None:
    """Write a minimal synthetic events dir with one llm_called + one
    llm_response_received event under agents/<agent>/skill_runs/.
    """
    skill_dir = (
        project_root / ".reyn" / "events" / "agents" / agent / "skill_runs"
        / "2026-05"
    )
    skill_dir.mkdir(parents=True, exist_ok=True)
    events_file = skill_dir / "2026-05-30T120000_test_skill.jsonl"
    called = json.dumps({
        "type": "llm_called",
        "timestamp": "2026-05-30T12:00:00+00:00",
        "data": {"model": "claude-sonnet"},
    })
    resp_data: dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if cost_usd is not None:
        resp_data["cost_usd"] = cost_usd
    received = json.dumps({
        "type": "llm_response_received",
        "timestamp": "2026-05-30T12:00:01+00:00",
        "data": resp_data,
    })
    events_file.write_text(called + "\n" + received + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_narrow_width_token_total_visible(tmp_path: Path) -> None:
    """Tier 2: at narrow content width the token total row is always present.

    The token total (prompt + completion) is the load-bearing number in the
    cost tab — it must survive even when cost + call-count fields are
    suppressed to avoid clipping. ``content_width`` below ``_MEDIUM_THRESHOLD``
    (42) is the minimum panel content area.
    """
    _make_events(tmp_path, prompt_tokens=3000, completion_tokens=500)
    # Use a width safely below _MEDIUM_THRESHOLD but above 0.
    narrow_width = _MEDIUM_THRESHOLD - 4  # = 38

    plain = _render_plain(tmp_path, narrow_width)

    # Token total "3,500" must be present in the rendered output.
    total = 3000 + 500  # 3500
    assert f"{total:,}" in plain, (
        f"Token total {total:,} not found in narrow-width render. "
        f"Rendered:\n{plain}"
    )
    # At narrow width the agent-row token label "tok" must also be visible.
    assert "tok" in plain, (
        f"'tok' label missing from narrow-width render. Rendered:\n{plain}"
    )


def test_wide_width_full_layout(tmp_path: Path) -> None:
    """Tier 2: at wide content width the full layout is unchanged.

    At ≥ _WIDE_THRESHOLD the token total, cost, and call-count columns are
    all present — no regression from the adaptive-width changes.
    """
    _make_events(tmp_path, prompt_tokens=3000, completion_tokens=500,
                 cost_usd=0.0123)
    wide_width = _WIDE_THRESHOLD + 10  # = 64, safely above full-layout threshold

    plain = _render_plain(tmp_path, wide_width)

    total = 3000 + 500
    # Token total
    assert f"{total:,}" in plain, (
        f"Token total {total:,} missing from wide render. Rendered:\n{plain}"
    )
    # Cost field
    assert "0.0123" in plain, (
        f"Cost field missing from wide render. Rendered:\n{plain}"
    )
    # Call count
    assert "1c" in plain, (
        f"Call-count '1c' missing from wide render. Rendered:\n{plain}"
    )


def test_narrow_width_suppresses_cost_calls(tmp_path: Path) -> None:
    """Tier 2: at narrow content width cost and call-count are suppressed.

    The BY AGENT / SKILL and BY MODEL agent-row cost + call-count fields must
    not appear at narrow width so they don't generate invisible clipped noise.
    The TODAY / ALL TIME sections always show cost (they're fixed-width label
    rows, not the columnar layout), so we assert only on the section with
    adaptive column widths.
    """
    _make_events(tmp_path, prompt_tokens=3000, completion_tokens=500,
                 cost_usd=0.0123, agent="my-agent")
    narrow_width = _MEDIUM_THRESHOLD - 4  # = 38

    plain = _render_plain(tmp_path, narrow_width)

    # The BY AGENT row must not contain "1c" (call-count) or "$0.0123"
    # (cost). Find the "BY AGENT / SKILL" section and check the agent row.
    lines = plain.split("\n")
    in_agent_section = False
    agent_row = ""
    for line in lines:
        if "BY AGENT" in line:
            in_agent_section = True
            continue
        if in_agent_section and "BY " in line and "AGENT" not in line:
            break  # moved past the section
        if in_agent_section and "my-agent" in line:
            agent_row = line
            break

    assert agent_row, (
        f"agent row for 'my-agent' not found in narrow render. "
        f"Full output:\n{plain}"
    )
    # Token total must be present.
    assert "3,500" in agent_row, (
        f"Token total missing from agent row: {agent_row!r}"
    )
    # Cost and call-count must be absent from the agent row at narrow width.
    assert "0.0123" not in agent_row, (
        f"Cost unexpectedly present in narrow agent row: {agent_row!r}"
    )
    assert "1c" not in agent_row, (
        f"Call-count unexpectedly present in narrow agent row: {agent_row!r}"
    )


def test_zero_width_does_not_crash(tmp_path: Path) -> None:
    """Tier 2: content_width=0 (unknown / pre-layout) falls back to wide layout.

    This is the default value when _PanelContent hasn't received its first
    layout pass yet. It must not crash and must produce readable output.
    """
    _make_events(tmp_path, prompt_tokens=100, completion_tokens=50)
    # Should not raise.
    plain = _render_plain(tmp_path, content_width=0)
    assert "150" in plain, (
        f"Token total 150 missing from zero-width (default) render. "
        f"Rendered:\n{plain}"
    )
