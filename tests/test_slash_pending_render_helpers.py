"""Tier 2: /pending slash — _render_list + _render_needs_attention pure helper contracts.

Both are pure formatting functions used by /pending list output.  They accept both
dataclass-style objects (via getattr) and dict-shaped values, and the singular/plural
and truncation behaviours need independent pinning.
"""
from __future__ import annotations

from types import SimpleNamespace

from reyn.interfaces.slash.pending import _render_list, _render_needs_attention

# ── helpers ────────────────────────────────────────────────────────────────


def _op(kind: str, iv_id: str, origin: str = "ch1", summary: str = "") -> SimpleNamespace:
    return SimpleNamespace(kind=kind, id=iv_id, origin_channel_id=origin, summary=summary)


# ── _render_list ───────────────────────────────────────────────────────────


def test_render_list_empty_returns_no_pending() -> None:
    """Tier 2: empty list → 'no pending operations' (no crash, no blank lines)."""
    out = _render_list([])
    assert out == "no pending operations"


def test_render_list_singular_header() -> None:
    """Tier 2: exactly one item → singular 'pending operation:' header."""
    out = _render_list([_op("ask_user", "iv-aaa111")])
    assert "1 pending operation:" in out
    assert "operations" not in out


def test_render_list_plural_header() -> None:
    """Tier 2: multiple items → plural 'pending operations:' header."""
    ops = [_op("ask_user", "iv-aaa111"), _op("input", "iv-bbb222")]
    out = _render_list(ops)
    assert "2 pending operations:" in out


def test_render_list_id_truncated_to_8_chars() -> None:
    """Tier 2: intervention id is displayed as first 8 chars only."""
    out = _render_list([_op("ask_user", "iv-longid123")])
    assert "iv-longi" in out
    assert "iv-longid123" not in out


def test_render_list_kind_and_origin_present() -> None:
    """Tier 2: each entry shows its kind and origin_channel_id."""
    out = _render_list([_op("ask_user", "iv-aaa111", origin="tui:alpha")])
    assert "ask_user" in out
    assert "tui:alpha" in out


def test_render_list_summary_shown_when_present() -> None:
    """Tier 2: summary is shown on a second line (↳ prefix) when non-empty."""
    out = _render_list([_op("ask_user", "iv-aaa111", summary="What file to use?")])
    assert "↳" in out
    assert "What file to use?" in out


def test_render_list_no_summary_omits_arrow_line() -> None:
    """Tier 2: empty summary omits the ↳ line entirely."""
    out = _render_list([_op("ask_user", "iv-aaa111", summary="")])
    assert "↳" not in out


def test_render_list_summary_truncated_at_60_chars() -> None:
    """Tier 2: summary > 60 chars is truncated in the display."""
    long_summary = "A" * 80
    out = _render_list([_op("ask_user", "iv-aaa111", summary=long_summary)])
    assert "A" * 61 not in out  # truncated — more than 60 'A' in a row not present
    assert "A" * 60 in out      # but first 60 chars are shown


def test_render_list_accepts_dict_shaped_ops() -> None:
    """Tier 2: dict-shaped ops (test / mock path) are rendered the same way."""
    op_dict = {
        "kind": "input",
        "id": "iv-dict001",
        "origin_channel_id": "ws:session",
        "summary": "dict op",
    }
    out = _render_list([op_dict])
    assert "input" in out
    assert "iv-dict0" in out
    assert "ws:session" in out
    assert "dict op" in out


# ── _render_needs_attention ────────────────────────────────────────────────


def test_render_needs_attention_empty_summary_returns_empty() -> None:
    """Tier 2: summary with no stuck_skills → '' (caller skips append)."""
    assert _render_needs_attention({}) == ""
    assert _render_needs_attention({"stuck_skills": []}) == ""


def test_render_needs_attention_shows_stuck_skill() -> None:
    """Tier 2: stuck skill entry surfaces skill_name, stuck_at, and run_id."""
    summary = {
        "stuck_skills": [
            {"skill_name": "planner", "run_id": "run-abc", "stuck_at": "phase_a"},
        ]
    }
    out = _render_needs_attention(summary)
    assert "needs attention" in out
    assert "planner" in out
    assert "phase_a" in out
    assert "run-abc" in out


def test_render_needs_attention_multiple_stuck_skills() -> None:
    """Tier 2: multiple stuck skills all appear in output."""
    summary = {
        "stuck_skills": [
            {"skill_name": "skill_a", "run_id": "r1", "stuck_at": "p1"},
            {"skill_name": "skill_b", "run_id": "r2", "stuck_at": "p2"},
        ]
    }
    out = _render_needs_attention(summary)
    assert "skill_a" in out
    assert "skill_b" in out
