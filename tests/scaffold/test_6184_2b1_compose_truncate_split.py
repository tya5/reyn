# scaffold: triggered_by="#6184 段2b-1 -- _summarize_args/_summarize_result split into compose+truncate halves (src/reyn/interfaces/repl/renderer.py)"
# scaffold: removed_by="#6184 段2b-2/2b-3 lands (the actual producer/consumer move) -- at that point compose and truncate run in different processes/wire hops and a byte-identical-with-the-pre-split-single-function comparison stops being a meaningful witness"
"""Tier scaffold: #6184 段2b-1 -- byte-identical witness for the newly split
``_summarize_args`` (-> ``_compose_args`` + ``_truncate_args``) and
``_summarize_result`` (-> compose branches tagging a ``_Truncatable`` +
``_truncate_result_summary``), covering the declared 6-shape population
(lead-coder ruling, #6184 issuecomment) at the COMPOSED call boundary --
not merely via ``_short``'s own lower-level tests, which already cover
these shapes for ``_short`` itself and are unaffected by this split.

Population (declared, not measured from real args distributions --
architect ruling: "every branch of the function itself", #6184):
⑴ empty/None -> ""            -- existing test (test_repl_renderer_pure_helpers.py)
⑵ short dict (no cut fires)   -- existing test (same file)
⑶ a single value over the per-value cut width -- THIS FILE (missing before)
⑷ the whole joined line over the overall cut width -- THIS FILE (missing before)
⑸ a non-dict (bare value) branch -- existing test (same file)
⑹ a value with newlines/consecutive whitespace -- THIS FILE for
  ``_summarize_args`` (missing before) + one representative
  ``_summarize_result`` branch (``error``) alongside the existing
  fallback-path newline test (``test_oversized_result_is_truncated_one_line``,
  tests/interfaces/test_inline_tool_result_summary.py) -- not every
  truncatable branch individually, since normalization is the SAME shared
  ``_normalize_text`` primitive for all of them (already exhaustively
  covered by ``_short``'s own tests) and each branch's own ROUTING
  (prefix/content) is independently proven by existing long-value tests
  in that same file (answer/url/stderr/error/error_message/mcp_content/
  name_or_desc all already have a passing test reaching their own
  ``_Truncatable`` tag) -- the cartesian product of every branch times
  every shape is not required to prove the split itself preserves
  behavior, since no branch-specific normalize logic was introduced.

Byte-identical is proved against a REFERENCE implementation of the
pre-split algorithm, never against a second call to the SAME new code
(testing.md's own "same expression on both sides" hazard, six-questions
Q2)."""
from __future__ import annotations

from reyn.interfaces.repl.renderer import _summarize_args, summarize_tool_result


def _reference_short(v, n: int = 60) -> str:
    """The pre-#6184-段2b-1 ``_short``, verbatim -- an independent oracle,
    never imported from renderer.py, so a future accidental behavior
    change in the split code has something OUTSIDE itself to diverge
    from."""
    if v is None:
        return ""
    s = v if isinstance(v, str) else repr(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _reference_summarize_args(args) -> str:
    """The pre-#6184-段2b-1 ``_summarize_args``, verbatim."""
    if not args:
        return ""
    if isinstance(args, dict):
        return _reference_short(
            ", ".join(f"{k}={_reference_short(v, 24)}" for k, v in args.items())
        )
    return _reference_short(args)


# ---------------------------------------------------------------------------
# _summarize_args
# ---------------------------------------------------------------------------


def test_shape_3_single_value_over_per_value_width() -> None:
    """Tier 1: ⑶: a value alone exceeding the 24-char per-value cut is truncated
    exactly as the pre-split two-stage ``_short(_short(...))`` algorithm
    did."""
    args = {"path": "x" * 40}
    got = _summarize_args(args)
    assert got == _reference_summarize_args(args)
    # positive control: the per-value cut actually fired (testing.md Q4 --
    # a green over an untruncated value would prove nothing about this shape)
    assert "…" in got


def test_shape_4_whole_line_over_total_width() -> None:
    """Tier 1: ⑷: many short keys whose JOINED line exceeds the 60-char overall
    cut, none individually over the 24-char per-value cut -- exercises
    the OUTER cut specifically, not the inner one."""
    args = {f"k{i}": "v" for i in range(30)}
    got = _summarize_args(args)
    assert got == _reference_summarize_args(args)
    assert "…" in got  # positive control: the outer cut fired


def test_shape_6_value_with_newlines_and_consecutive_whitespace() -> None:
    """Tier 1: ⑹: a value containing newlines and runs of spaces exercises
    whitespace-collapse (normalization), which moved to compose
    (``_compose_args``) per the architect correction -- proves the moved
    half still produces the SAME final string as the pre-split
    single-function algorithm."""
    args = {"body": "line1\n\n  line2   line3"}
    got = _summarize_args(args)
    assert got == _reference_summarize_args(args)
    assert "\n" not in got  # positive control: normalization fired


# ---------------------------------------------------------------------------
# summarize_tool_result / _summarize_result -- one representative
# truncatable branch (error) for ⑹, alongside the existing fallback-path
# newline witness (test_oversized_result_is_truncated_one_line).
# ---------------------------------------------------------------------------


def test_error_branch_with_newlines_is_normalized() -> None:
    """Tier 2: ⑹, ``error`` branch (the FIRST/most commonly-hit ``_Truncatable``
    tag in practice): a multi-line error message collapses to one line,
    matching the pre-split ``f"✗ {_short(error, 78)}"`` computation."""
    error_text = "first line\n\n  second   line"
    got = summarize_tool_result("list_directory", {"error": error_text})
    reference = f"✗ {_reference_short(error_text, 78)}"
    assert got == reference
    assert "\n" not in got
