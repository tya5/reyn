#!/usr/bin/env python3
"""Measure verbatim-span overlap between every ``CLAUDE.md`` file and the
``docs/`` corpus — the re-runnable form of #4858/#4860's measurement.

#4858: 3/3 same-day mirror-drift misses (`#4841→#4843`, `#4851→#4853`,
`#4854`'s own bullet) all landed inside a ~10%-of-CLAUDE.md verbatim overlap
with ``tier1-rationale.md`` — a normative rule restated in a second place
that could (and did) go stale independently. #4860 fixed that ONE pair to
0 words of overlap (word-tokenized ``difflib.SequenceMatcher``, blocks
``>= 15`` words — the method this script keeps).

Deliberately NOT a hard CI gate (owner/lead-coder ruling still stands: gate
design has open questions — front-matter vs. naming-convention pairing,
false-positive handling, whether a restated table is ever legitimate). This
script is the reusable MEASUREMENT — "what is the current value" — kept so
re-measuring after the next ``CLAUDE.md`` edit is one command, not a fresh
investigation. Whoever eventually designs the gate reads this file for the
population definition, per lead-coder's own #4858 comment.

Population, derived from the filesystem, not hardcoded (#4858 follow-up,
2026-08-19 measurement): every ``CLAUDE.md`` found under the repo root,
excluding ``.venv``/``.claude/worktrees``/``node_modules`` — this is the
CANONICAL way to answer "how many surfaces are there right now" so this
script does not silently go stale the next time a module gains or loses
its own ``CLAUDE.md`` (as happened between #4860's 1-doc-pair design and
this file's own 7-surface reality, per #4867/#4869/#4918's split).

Known accepted exemptions (NOT flagged as findings, ruled acceptable —
#4858 2026-08-19): a short, stable, intentionally-repeated epigraph/tagline
(the Constitution one-liner in ``CLAUDE.md``, ``docs/index.md``, and
``docs/start.md``) is a citation, not a restated rule — #4860's own
precedent ("pointed to CLAUDE.md's own quote instead of repeating it") is
about restated PROSE, not a single stable headline meant to read the same
everywhere. This script does NOT auto-exempt such spans (no reliable way
to tell a legitimate short quote from an accidental one by shape alone);
it reports every span >=15 words and leaves severity judgment to the
reader, per #4858's own explicit warning that "always require both" is
the wrong rule for this population.
"""
from __future__ import annotations

import argparse
import re
from difflib import SequenceMatcher
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXCLUDED_DIR_PARTS = {".venv", ".venv311", "node_modules", "worktrees"}
_MIN_OVERLAP_WORDS = 15


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def discover_claude_md_files(root: Path) -> list[Path]:
    """Every ``CLAUDE.md`` under *root*, excluding vendored/worktree copies
    — the population this measurement checks. Filesystem-derived (not a
    hardcoded list) so it tracks the module-CLAUDE.md set as it evolves."""
    found = []
    for p in sorted(root.rglob("CLAUDE.md")):
        if any(part in _EXCLUDED_DIR_PARTS for part in p.parts):
            continue
        found.append(p)
    return found


def find_overlap_blocks(
    a_words: list[str], b_words: list[str], min_len: int = _MIN_OVERLAP_WORDS,
) -> list:
    sm = SequenceMatcher(None, a_words, b_words, autojunk=False)
    return [b for b in sm.get_matching_blocks() if b.size >= min_len]


def measure(root: Path) -> list[tuple[Path, Path, list, list[str]]]:
    """Every (claude_file, doc_file, blocks, claude_words) with >=1
    matching span of >= _MIN_OVERLAP_WORDS words."""
    claude_files = discover_claude_md_files(root)
    docs_files = sorted((root / "docs").glob("**/*.md"))

    results = []
    for cf in claude_files:
        a_words = _tokenize(cf.read_text(encoding="utf-8"))
        for df in docs_files:
            b_words = _tokenize(df.read_text(encoding="utf-8"))
            blocks = find_overlap_blocks(a_words, b_words)
            if blocks:
                results.append((cf, df, blocks, a_words))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "--root", default=str(_REPO_ROOT), help="Repo root (default: this script's own repo)"
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    claude_files = discover_claude_md_files(root)
    results = measure(root)

    print(f"Population: {len(claude_files)} CLAUDE.md file(s):")
    for cf in claude_files:
        print(f"  {cf.relative_to(root)}")
    print()

    total_words = sum(sum(b.size for b in blocks) for _, _, blocks, _ in results)
    print(
        f"Found {len(results)} (CLAUDE.md, docs-file) pair(s) with "
        f">= {_MIN_OVERLAP_WORDS}-word overlap, {total_words} words total\n"
    )
    for cf, df, blocks, a_words in results:
        span_words = sum(b.size for b in blocks)
        print(
            f"{cf.relative_to(root)}  <->  {df.relative_to(root)}: "
            f"{len(blocks)} span(s), {span_words} words"
        )
        for b in blocks:
            snippet = " ".join(a_words[b.a : b.a + min(b.size, 20)])
            print(f"    size={b.size}: {snippet!r}{'...' if b.size > 20 else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
