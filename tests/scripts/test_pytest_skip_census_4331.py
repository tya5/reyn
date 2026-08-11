"""Tier 1: scripts/pytest_skip_census.py's parsing/rendering contract.

Pure-function tests over fixture log text — no subprocess, no real pytest
run required, same discipline as the other `tests/scripts/` gate tests.
"""
from __future__ import annotations

from scripts.pytest_skip_census import main, parse_census, render_markdown

_SAMPLE_LOG = """\
tests/foo.py::test_a SKIPPED [ 50%]
tests/foo.py::test_b SKIPPED [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/foo.py:10: optional extra not installed
SKIPPED [1] tests/foo.py:20: optional extra not installed
========================= 3 passed, 2 skipped in 0.10s =========================
"""


def test_skip_lines_are_summed_and_grouped_by_reason() -> None:
    """Tier 1: two SKIPPED lines sharing the same reason text aggregate
    into one census row, counts summed — the whole point of a census
    (not a raw dump of every location)."""
    census = parse_census(_SAMPLE_LOG)
    assert census["skip_reason_counts"] == {"optional extra not installed": 2}
    assert census["summed_skips"] == 2


def test_summed_skips_cross_checked_against_pytests_own_total() -> None:
    """Tier 1: the summed per-reason count must match pytest's own final
    summary line — this is the self-check that makes a parsing bug in
    THIS script visible rather than a silently wrong number."""
    census = parse_census(_SAMPLE_LOG)
    assert census["reported_total_skipped"] == census["summed_skips"] == 2


def test_a_reason_mismatch_shows_up_as_a_warning_in_the_rendered_markdown() -> None:
    """Tier 1: the load-bearing self-check — if `reported_total_skipped`
    diverges from the summed count, the rendered markdown must say so."""
    census = parse_census(_SAMPLE_LOG)
    census["reported_total_skipped"] = 999  # simulate an undercounting bug
    md = render_markdown(census)
    assert "parse mismatch" in md
    assert "999" in md


def test_collection_errors_are_separate_from_skip_count() -> None:
    """Tier 1: #4331 condition 2 — a file that fails to collect
    contributes ZERO to the skipped tally (it never emits a SKIPPED
    line), so it must be counted and shown on its own, not folded into
    the skip total."""
    log = _SAMPLE_LOG + "ERROR tests/broken.py\n"
    census = parse_census(log)
    assert census["collection_errors"] == ["tests/broken.py"]
    assert census["summed_skips"] == 2  # unaffected by the collection error


def test_zero_collection_errors_is_stated_explicitly_not_silently_absent() -> None:
    """Tier 1: #4331's own motivating case — 0 collection errors must be
    a VISIBLE, stated fact in the rendered output, not the absence of a
    line (an absent line reads the same whether nobody checked or 0 was
    confirmed — #4331's entire complaint about the pre-existing state)."""
    census = parse_census(_SAMPLE_LOG)
    md = render_markdown(census)
    assert "Collection errors: 0" in md


def test_a_reason_containing_a_pipe_character_does_not_break_the_markdown_table() -> None:
    """Tier 1: a skip reason is free text and can legitimately contain
    `|` (e.g. a type union in a docstring-derived reason) — must not
    corrupt the markdown table's column structure."""
    log = (
        "SKIPPED [1] tests/foo.py:5: needs str | None support\n"
        "1 skipped in 0.01s\n"
    )
    census = parse_census(log)
    md = render_markdown(census)
    assert "needs str \\| None support" in md


def test_no_skips_at_all_renders_without_a_table() -> None:
    """Tier 1: a run with 0 skips must not render an empty/broken
    markdown table — no reason entries means no table section at all."""
    census = parse_census("5 passed in 0.05s\n")
    assert census["skip_reason_counts"] == {}
    md = render_markdown(census)
    assert "| count | reason |" not in md
    assert "0 test(s) skipped" in md


# ── never a gate — main() always returns 0 ──────────────────────────────────


def test_main_returns_zero_even_with_no_args() -> None:
    """Tier 1: #4331 condition 3 — this must never fail the CI job it
    runs in. Missing the log-path argument is a usage error, not a
    reason to exit nonzero."""
    assert main([]) == 0


def test_main_returns_zero_for_a_nonexistent_log_path(tmp_path) -> None:
    """Tier 1: a missing/unreadable log file degrades to an apologetic
    summary line, never a nonzero exit — same "not a gate" contract as
    the no-args case."""
    missing = tmp_path / "does-not-exist.log"
    assert main([str(missing)]) == 0


def test_main_writes_a_real_census_for_a_real_log_file(tmp_path, capsys) -> None:
    """Tier 1: the CLI's own load-bearing path — given a real log file,
    stdout must contain the rendered census, not just an empty success."""
    log_path = tmp_path / "pytest-output.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    assert main([str(log_path)]) == 0
    out = capsys.readouterr().out
    assert "2 test(s) skipped" in out
    assert "optional extra not installed" in out
