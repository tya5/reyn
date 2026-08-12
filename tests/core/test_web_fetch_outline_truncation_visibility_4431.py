"""Tier 2: _HtmlPreviewParser's outline cap must be visible when it cuts (#4431).

``_OUTLINE_MAX`` caps the outline preview `_HtmlPreviewParser` builds for a
web_fetch result. Before #4431 a page with more headings than the cap silently
dropped the rest — the outline read as "the whole document's heading
structure" when it was really "the first N". Per the owner's #4381-motivated
ruling (a silent cap with no config knob needs the loss to be visible instead —
this cap doesn't need a config knob, since the full page text is available
alongside the preview and a preview length is a display choice, not a
correctness bound), `result()` now reports `outline_truncated` +
`outline_heading_count` whenever the cap actually cut something.
"""
from __future__ import annotations

from reyn.core.op_runtime.web import _HtmlPreviewParser

_OUTLINE_MAX = _HtmlPreviewParser._OUTLINE_MAX


def _headings_html(n: int) -> str:
    return "".join(f"<h2>Heading {i}</h2><p>body</p>" for i in range(n))


def test_outline_signals_truncation_past_the_cap():
    """Tier 2: more headings than _OUTLINE_MAX → outline_truncated + the real total."""
    parser = _HtmlPreviewParser()
    parser.feed(_headings_html(_OUTLINE_MAX + 4))

    result = parser.result()

    assert len(result["outline"]) == _OUTLINE_MAX
    assert result["outline_truncated"] is True
    assert result["outline_heading_count"] == _OUTLINE_MAX + 4


def test_outline_no_truncation_signal_when_everything_fits():
    """Tier 2: accept-side twin — at or under the cap, nothing was cut, so the
    flag must be ABSENT (not present-and-False), matching op_runtime/file.py's
    own #4431 not_found suggestions convention (only outright absence says
    nothing was truncated)."""
    parser = _HtmlPreviewParser()
    under_cap = [f"Heading {i}" for i in range(_OUTLINE_MAX - 1)]
    parser.feed("".join(f"<h2>{h}</h2><p>body</p>" for h in under_cap))

    result = parser.result()

    assert [o.split(": ", 1)[1] for o in result["outline"]] == under_cap
    assert "outline_truncated" not in result
    assert "outline_heading_count" not in result


def test_outline_exactly_at_the_cap_is_not_truncated():
    """Tier 2: boundary — exactly _OUTLINE_MAX headings is the whole document,
    not a cut."""
    parser = _HtmlPreviewParser()
    parser.feed(_headings_html(_OUTLINE_MAX))

    result = parser.result()

    assert len(result["outline"]) == _OUTLINE_MAX
    assert "outline_truncated" not in result
