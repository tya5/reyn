#!/usr/bin/env python3
"""#5003 — flag a PR that removes an identifier from ``src/`` without
touching any ``docs/`` file that still names it.

## The question this asks, and the one it deliberately does NOT ask

The first form tried (lead-coder, #4997 doc-drift candidate) asked the DOC:
"are you a removal record?" — a natural-language judgment a machine cannot
make (this codebase deliberately writes phrases like "now-retired" / "#4951-B
で削除" — a naive "does this doc still mention a name that's gone from src"
scan flags every one of those *on-purpose* removal records as a violation).
Per CLAUDE.md's own rule, a mechanism that has to guess is a mechanism that
stays silent and gets removed — see `find_negated_closing_declarations` in
`check_pr_closing_intent.py` for the sibling case (negation-detection stayed
UNCONDITIONAL specifically because it never had to guess intent).

The question this script asks instead is put to the PR, not the doc:

    "Of the identifiers this diff removed from src/, is there any docs/
    file that still contains one of them, that this PR did NOT touch?"

Not touched -> flag. Touched -> pass, unconditionally, with no attempt to
read what the touch says. This is why the false-positive class collapses:
whoever writes a removal record ("X is now retired, see #N") touches that
doc IN THE SAME PR that removes X — the two edits are the same commit action
by construction, not two independent choices that might drift apart. And a
PRE-EXISTING removal record (some earlier PR's own "X, removed in #N" note)
never enters this PR's candidate set at all, because X was not removed BY
THIS diff — it was already gone from src/ before this diff started.

## Structural exclusions (syntax only, never semantic judgment)

1. **History-class docs are exempt directories, not a semantic read.**
   `docs/deep-dives/decisions/` and `docs/deep-dives/journal/` are the
   places CLAUDE.md's own rules say a name is deliberately preserved after
   removal (an ADR records what a design USED to be; a journal entry records
   what happened, and rewriting it changes the record). Exclusion is by
   DIRECTORY PREFIX — never by asking whether a given file "is" a record.
2. **Identifier salience floor** (the one tunable knob — see
   `_MIN_IDENTIFIER_LENGTH`). A short, common bare word ("run", "Session")
   removed from one src/ call site is not a distinctive enough token to
   search docs/ for without drowning in coincidental prose matches. An
   identifier passes the floor if it has ANY of: contains `_` (snake_case
   symbols are not English words), is dotted (`module.symbol` /
   `Class.method` shape), or is at least `_MIN_IDENTIFIER_LENGTH` characters
   long. All three tests are syntactic — none of them reads what the
   identifier means.

## Not yet blocking (architect ruling, #5003)

The false-positive rate of this discriminator is UNMEASURED beyond
lead-coder's own "caught it by hand twice" instances (true-positive side
only). Per the architect's explicit condition, this check ships in **warn
mode** (`main` always returns 0) until a calibration pass — `--calibrate`
below — has been run against a batch of recently-merged PRs and each red
hit inspected by hand. Flip to blocking only after that count exists; an
untallied "0 false positives" is indistinguishable from "flags nothing"
(CLAUDE.md's pre-conclusion checklist, item 5).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Structural exclusions
# ---------------------------------------------------------------------------

# Directory prefixes (relative to repo root) exempt from drift-flagging:
# a name preserved here on purpose is the documented PRODUCT of this
# directory, not drift. See module docstring, exclusion 1.
#
# docs/deep-dives/proposals/ added after #5010 calibration (PR #4454):
# every proposal in this directory carries its own **Status** field
# (README.md: "cut, landed" / etc.) — the directory's own README states
# the split explicitly: decisions/ records "why chosen", proposals/
# records "what should be implemented", and both are point-in-time
# design records, not living reference docs that track current src/
# identifier names. #4454's `_force_close_wrap_up` false-fire (named in
# two `Status: cut, landed`-flagged proposals neither touched by the PR
# that removed it) is the real incident this exclusion closes.
_HISTORY_CLASS_DOC_PREFIXES = (
    "docs/deep-dives/decisions/",
    "docs/deep-dives/journal/",
    "docs/deep-dives/proposals/",
)

# The one tunable knob (architect ruling, #5003) — see module docstring,
# exclusion 2. Not yet calibrated against a measured false-positive rate;
# revisit via `--calibrate` before this check goes blocking.
_MIN_IDENTIFIER_LENGTH = 8

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def is_salient_identifier(name: str) -> bool:
    """True iff *name* clears the identifier-salience floor — see module
    docstring, exclusion 2. Purely syntactic: never reads what the token
    means, only its shape."""
    if "_" in name:
        return True
    if "." in name:
        return True
    return len(name) >= _MIN_IDENTIFIER_LENGTH


def is_history_class_doc(path: str) -> bool:
    """True iff *path* (repo-relative, forward-slash) sits under a
    directory this repo's own rules designate as a preserved record — see
    module docstring, exclusion 1. Directory-prefix test only."""
    return any(path.startswith(prefix) for prefix in _HISTORY_CLASS_DOC_PREFIXES)


# ---------------------------------------------------------------------------
# Pure diff parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileDiff:
    path: str
    removed_lines: "tuple[str, ...]"
    added_lines: "tuple[str, ...]"


def _iter_file_diffs(diff_text: str) -> "list[_FileDiff]":
    """Split a unified ``git diff`` / ``gh pr diff`` text into per-file
    removed/added line buckets. Only the ``+``/``-`` content lines are kept
    (not the file-header ``+++``/``---`` lines, which this regex excludes
    by requiring the line not start with ``+++``/``---``)."""
    files: "list[_FileDiff]" = []
    current_path: "str | None" = None
    removed: "list[str]" = []
    added: "list[str]" = []

    def _flush() -> None:
        if current_path is not None:
            files.append(_FileDiff(current_path, tuple(removed), tuple(added)))

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            _flush()
            removed = []
            added = []
            # "diff --git a/path b/path" — take the b/ side (post-image path,
            # which is what a rename/new-file case still resolves to).
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            current_path = match.group(2) if match else None
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    _flush()
    return files


def find_touched_files(diff_text: str) -> "set[str]":
    """Every file path this diff touches at all (added, removed, or
    modified) — repo-relative, forward-slash. Used to test whether a docs/
    file the identifier appears in was edited by THIS PR."""
    return {fd.path for fd in _iter_file_diffs(diff_text)}


_TRIPLE_QUOTES = ('"""', "'''")


def _strip_comments_and_docstrings(lines: "tuple[str, ...]") -> "list[str]":
    """Best-effort, line-oriented removal of ``#``-comment text AND
    triple-quoted docstring bodies from a sequence of ``.py`` source
    lines — a syntactic rule (Python's own comment/string-literal
    markers), not a semantic read of what the text says.

    Real incidents this closes (#5010 calibration, backward scan past
    PR #5007's own 15-PR sample): a `#`-comment prose word
    ("scaffolding", PR #4981, fixed in #5007) and SIX docstring-prose
    words ("Operational" PR #4563, "affordances" PR #4560, "resumption"
    PR #4545, "surprised" PR #4504, "normalises" PR #4459, "Compares"
    PR #4458) were each extracted as if they were removed code
    identifiers and matched against docs/ using their ordinary-English
    sense — false positives with nothing to do with a removed src/
    symbol. The original ``#``-only stripper (PR #5007) caught the first
    class but not the second; this closes both with one state machine.

    Disclosed limitations (same shape as the original's): a ``#`` or
    triple-quote marker inside a single/double-quoted string literal is
    not distinguished from a real marker; and this operates only on the
    given (possibly non-contiguous — a diff hunk may omit context lines)
    line sequence, so a docstring opened on a line NOT included here
    (e.g. an unchanged line the hunk didn't carry) is not recognized as
    already-open when this sequence starts. A full Python tokenizer
    would close both gaps but needs the complete pre/post file content,
    not just a diff's changed-line text — a larger surface than this
    diff-line-only module takes on elsewhere; accepted as a known gap,
    not silently assumed away.
    """
    out: "list[str]" = []
    in_docstring = False
    quote = ""
    for raw in lines:
        line = raw
        if in_docstring:
            idx = line.find(quote)
            if idx == -1:
                out.append("")
                continue
            line = line[idx + 3:]
            in_docstring = False
        piece = ""
        while True:
            hash_idx = line.find("#")
            markers = [(hash_idx, "#")] if hash_idx != -1 else []
            for q in _TRIPLE_QUOTES:
                q_idx = line.find(q)
                if q_idx != -1:
                    markers.append((q_idx, q))
            if not markers:
                piece += line
                break
            idx, marker = min(markers, key=lambda pair: pair[0])
            if marker == "#":
                piece += line[:idx]
                break
            prefix = line[:idx]
            rest = line[idx + 3:]
            close_idx = rest.find(marker)
            if close_idx == -1:
                piece += prefix
                in_docstring = True
                quote = marker
                break
            line = prefix + rest[close_idx + 3:]
        out.append(piece)
    return out


def find_removed_identifiers(diff_text: str, *, src_prefix: str = "src/") -> "set[str]":
    """Identifiers this diff removed from ``src/``: tokens present on a
    removed (``-``) line of a ``src/``-prefixed file, salient (see
    :func:`is_salient_identifier`), and NOT also present on any added
    (``+``) line of the SAME file in this diff (a token that moved within
    the same file's diff was not removed, just relocated). ``#``-comment
    and docstring text is stripped first for ``.py`` files (see
    :func:`_strip_comments_and_docstrings`) — prose is not a code
    identifier."""
    removed_ids: "set[str]" = set()
    for fd in _iter_file_diffs(diff_text):
        if not fd.path.startswith(src_prefix):
            continue
        is_py = fd.path.endswith(".py")
        removed_lines = _strip_comments_and_docstrings(fd.removed_lines) if is_py else list(fd.removed_lines)
        added_lines = _strip_comments_and_docstrings(fd.added_lines) if is_py else list(fd.added_lines)
        removed_tokens = {tok for line in removed_lines for tok in _IDENTIFIER_RE.findall(line)}
        added_tokens = {tok for line in added_lines for tok in _IDENTIFIER_RE.findall(line)}
        for tok in removed_tokens - added_tokens:
            if is_salient_identifier(tok):
                removed_ids.add(tok)
    return removed_ids


def identifier_survives_in_src(identifier: str, src_root: Path) -> bool:
    """True iff *identifier* still appears anywhere in the CURRENT (post-
    diff) ``src/`` tree — i.e. this diff moved it rather than deleting it
    from the codebase. Uses `git grep` scoped to the working tree so this
    reads the checked-out post-diff state, not history."""
    result = subprocess.run(
        ["git", "grep", "-lF", "--", identifier, "--", "src"],
        cwd=src_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def find_doc_files_containing(identifier: str, docs_root: Path) -> "set[str]":
    """Every ``docs/`` file (repo-relative path) that contains *identifier*
    as a whole word, excluding history-class directories (exclusion 1)."""
    result = subprocess.run(
        ["git", "grep", "-lF", "--", identifier, "--", "docs"],
        cwd=docs_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return {
        path
        for path in result.stdout.splitlines()
        if path and not is_history_class_doc(path)
    }


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    identifier: str
    doc_path: str


def check_doc_drift_pure(
    gone_identifiers: "set[str]",
    doc_files_by_identifier: "dict[str, set[str]]",
    touched_files: "set[str]",
) -> "list[Finding]":
    """The discriminator itself (architect ruling, #5003), as a PURE
    function over already-resolved data — no subprocess, no filesystem —
    so this is the Tier 1 contract surface (mirrors
    ``check_pr_closing_intent.check_contradictions``).

    *gone_identifiers*: identifiers this diff removed from ``src/`` that do
    not survive anywhere else in the current ``src/`` tree (the network
    wrapper, :func:`resolve_gone_identifiers`, computes this).
    *doc_files_by_identifier*: identifier -> the non-history docs/ files
    that still name it (the network wrapper,
    :func:`resolve_doc_files_by_identifier`, computes this).
    *touched_files*: every file path this diff touched at all.

    For every gone identifier, for every doc file that still names it,
    flag it UNLESS this diff also touched that doc file.
    """
    findings: "list[Finding]" = []
    for identifier in sorted(gone_identifiers):
        for doc_path in sorted(doc_files_by_identifier.get(identifier, ())):
            if doc_path in touched_files:
                continue
            findings.append(Finding(identifier=identifier, doc_path=doc_path))
    return findings


def resolve_gone_identifiers(diff_text: str, repo_root: Path) -> "set[str]":
    """Network/filesystem wrapper: identifiers removed from ``src/`` by this
    diff (:func:`find_removed_identifiers`, pure) that also do not survive
    anywhere else in the current ``src/`` tree (``git grep``)."""
    return {
        identifier
        for identifier in find_removed_identifiers(diff_text)
        if not identifier_survives_in_src(identifier, repo_root)
    }


def resolve_doc_files_by_identifier(
    identifiers: "set[str]", repo_root: Path,
) -> "dict[str, set[str]]":
    """Network/filesystem wrapper: for each identifier, the non-history
    docs/ files that still name it (``git grep``, one call per identifier —
    see :func:`find_doc_files_containing`)."""
    return {
        identifier: find_doc_files_containing(identifier, repo_root)
        for identifier in identifiers
    }


def check_doc_drift(
    diff_text: str,
    *,
    repo_root: Path = _ROOT,
) -> "list[Finding]":
    """End-to-end: wires the network/filesystem wrappers into
    :func:`check_doc_drift_pure`. This is the impure entry point — tests
    exercise :func:`check_doc_drift_pure` directly with fixture data."""
    touched = find_touched_files(diff_text)
    gone = resolve_gone_identifiers(diff_text, repo_root)
    doc_files_by_identifier = resolve_doc_files_by_identifier(gone, repo_root)
    return check_doc_drift_pure(gone, doc_files_by_identifier, touched)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def fetch_pr_diff(pr_number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Flag a PR that removes an identifier from src/ without "
            "touching any docs/ file that still names it. Warn-only until "
            "calibrated — see module docstring."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, metavar="N", help="Live PR number (via `gh pr diff`).")
    group.add_argument("--fixture", metavar="PATH", help="Path to a unified-diff text file.")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pr is not None:
        try:
            diff_text = fetch_pr_diff(args.pr)
        except subprocess.CalledProcessError as exc:
            print(f"gh pr diff failed: {exc.stderr}", file=sys.stderr)
            return 2
        source = f"PR #{args.pr}"
    else:
        diff_text = Path(args.fixture).read_text(encoding="utf-8")
        source = args.fixture

    findings = check_doc_drift(diff_text)

    if not findings:
        print(f"OK — no doc-drift findings ({source}).")
        return 0

    print(f"WARN — doc-drift findings ({source}), NOT YET BLOCKING (see module docstring):\n")
    for f in findings:
        print(f"  {f.identifier!r} removed from src/, still named in {f.doc_path} (untouched by this PR)")
    print(f"\nTotal: {len(findings)} finding(s).")
    return 0  # warn-only — see module docstring "Not yet blocking"


if __name__ == "__main__":
    sys.exit(main())
