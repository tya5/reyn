"""Tier 1: #4927 — ``check_claude_md_doc_overlap.py``'s tokenizer must strip
markdown decoration before splitting on whitespace, not just whitespace; and
``main()`` must fail loudly, not report "0 pairs", on an empty population.

The un-fixed tokenizer (``re.findall(r"\\S+", text)`` alone) let a decoration
character (backtick / ``*`` / ``#`` / ``-``) glued to a word at a DIFFERENT
line-wrap point in two files split the SAME text into a different token
sequence — undercounting real overlap. Real instance (architect, #4927): the
pre-push command list is duplicated between ``CLAUDE.md`` and
``docs/deep-dives/contributing/pr-workflow.md``, wrapped at different points
inside backtick-fenced inline code; the un-fixed tool measured 0 words of
overlap for this pair where real, current duplication exists.

Real files, not a synthetic fixture (lead-coder's explicit #4927
instruction, following #4858's own principle: the reproducing command
sequence itself is not a defect to paraphrase away — rewording it would
stop demonstrating the bug this test pins). If either file's wording
changes enough that this specific span no longer exists, that is new
information for whoever touches those files next, not a reason to weaken
this test's own assertion.

Second bug in the same PR (lead-coder's review, blocking point ①): a
mistyped ``--root`` producing zero discovered ``CLAUDE.md`` files printed
"Found 0 pairs ... 0 words total" and exited 0 — indistinguishable from a
real population that genuinely has no overlap. Six-questions ④: a green
over an EMPTY collection wears the same colour as a real green. ``main()``
now exits 1 on an empty population instead of measuring nothing.
"""
from __future__ import annotations

import importlib.util
import sys

from tests._support.paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "check_claude_md_doc_overlap.py"
_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
_PR_WORKFLOW = REPO_ROOT / "docs" / "deep-dives" / "contributing" / "pr-workflow.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("_claude_md_doc_overlap_4927", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pair_overlap_words(mod, a_text: str, b_text: str) -> int:
    a_words = mod._tokenize(a_text)
    b_words = mod._tokenize(b_text)
    blocks = mod.find_overlap_blocks(a_words, b_words)
    return sum(b.size for b in blocks)


def test_decoration_stripped_tokenizer_finds_the_real_command_list_overlap() -> None:
    """Tier 1: the fixed tokenizer finds >= 15 words of overlap between
    CLAUDE.md and pr-workflow.md's shared pre-push command list — asserting
    a VALUE (a floor), not an exact match against this test's own
    computation, so a future edit narrowing (but not eliminating) the
    overlap doesn't silently pass a test that no longer checks anything
    (the same "declared vs. actual" gap #4858 was about)."""
    mod = _load_module()
    claude_text = _CLAUDE_MD.read_text(encoding="utf-8")
    pr_workflow_text = _PR_WORKFLOW.read_text(encoding="utf-8")

    words = _pair_overlap_words(mod, claude_text, pr_workflow_text)
    assert words >= 15, (
        f"expected >= 15 words of overlap on the known command-list span "
        f"between CLAUDE.md and pr-workflow.md; got {words}. If this "
        f"legitimately dropped to 0, the shared command list itself "
        f"changed — that's real news, not a reason to lower this bound."
    )


def test_strip_falsify_undecorated_tokenizer_misses_the_same_pair() -> None:
    """Tier 1: strip-falsify — the PRE-#4927 tokenizer shape (whitespace
    split only, no decoration stripped) finds 0 words on the SAME pair,
    proving the fix in the test above is genuinely load-bearing and not
    something the old code already caught."""
    import re

    def _old_tokenize(text: str) -> list[str]:
        return re.findall(r"\S+", text)

    mod = _load_module()
    claude_words = _old_tokenize(_CLAUDE_MD.read_text(encoding="utf-8"))
    pr_words = _old_tokenize(_PR_WORKFLOW.read_text(encoding="utf-8"))
    blocks = mod.find_overlap_blocks(claude_words, pr_words)
    total = sum(b.size for b in blocks)
    assert total == 0, (
        f"expected the OLD (undecorated) tokenizer to miss this pair "
        f"entirely (0 words) — got {total}. If this is no longer 0, the "
        f"files changed in a way that makes this strip-falsify stale; it "
        f"no longer proves the fix is load-bearing and needs a fresh pair."
    )


def test_empty_population_fails_loudly_instead_of_reporting_zero_pairs(tmp_path, capsys) -> None:
    """Tier 1: an empty CLAUDE.md population (wrong --root, or a repo that
    genuinely has none) must exit non-zero, not print "Found 0 pairs, 0
    words total" and exit 0 — that output is indistinguishable from "the
    population is real and has zero overlap" (lead-coder #4927 blocking
    point ①; real repro: running a copy of this script from outside the
    repo silently reported Population: 0 / 0 words total / exit 0)."""
    mod = _load_module()

    exit_code = mod.main(["--root", str(tmp_path)])

    assert exit_code != 0, (
        "an empty population must not exit 0 — that reads as a clean "
        "measurement instead of a broken --root or a lost population"
    )
    captured = capsys.readouterr()
    assert "0 CLAUDE.md" not in captured.out, (
        "must not print a 'Population: 0' success-shaped line at all — "
        f"got stdout: {captured.out!r}"
    )


def test_strip_falsify_empty_population_check_is_load_bearing() -> None:
    """Tier 1: strip-falsify — with the loud-failure check removed,
    ``discover_claude_md_files`` + ``measure`` on an empty root DOES return
    an empty result silently (0 files, 0 pairs), proving the check in the
    test above is the thing doing the work, not something else already
    catching it."""
    import tempfile
    from pathlib import Path

    mod = _load_module()
    with tempfile.TemporaryDirectory() as empty_root:
        root = Path(empty_root)
        claude_files = mod.discover_claude_md_files(root)
        results = mod.measure(root)
        assert claude_files == [] and results == [], (
            "expected an empty root to silently produce 0 files / 0 results "
            "at the discover/measure layer — this is exactly the silent "
            "shape main() must refuse to report as if it were a measurement"
        )
