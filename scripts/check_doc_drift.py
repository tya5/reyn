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
   `docs/deep-dives/decisions/`, `docs/deep-dives/journal/`, and
   `docs/deep-dives/proposals/` are the places CLAUDE.md's own rules (and,
   for proposals/, that directory's own README) say a name is deliberately
   preserved after removal (an ADR/proposal records what a design USED to
   be — every proposal carries its own `Status:` field — and a journal
   entry records what happened; rewriting any of them changes the record).
   Exclusion is by DIRECTORY PREFIX — never by asking whether a given file
   "is" a record.
2. **Identifier salience floor** (the one tunable knob — see
   `_MIN_IDENTIFIER_LENGTH`). A short, common bare word ("run", "Session")
   removed from one src/ call site is not a distinctive enough token to
   search docs/ for without drowning in coincidental prose matches. An
   identifier passes the floor if it has ANY of: contains `_` (snake_case
   symbols are not English words), is dotted (`module.symbol` /
   `Class.method` shape), or is at least `_MIN_IDENTIFIER_LENGTH` characters
   long. All three tests are syntactic — none of them reads what the
   identifier means.

## Extraction: precise (tokenizer) path vs line-heuristic fallback

Two ways to decide "what identifiers did this diff remove from a `.py`
file" exist in this module, and calibration (#5010) is why both do:

- **`find_removed_identifiers_precise`** (architect ruling, #5010 round
  2) — reads the REAL pre/post file content (`git show <sha>:<path>`)
  and tokenizes it with Python's own `tokenize` module. A NAME token
  structurally cannot come from inside a STRING (docstring) or COMMENT
  token, so this closes the false-positive class calibration actually
  measured (6 of 9 real-world candidates were docstring-prose words —
  see git history for the full writeup) with ZERO marker-guessing. Used
  whenever a PR number is available (`--pr`, or CI).
- **`find_removed_identifiers`** (the original #5007/#5010-round-1
  line-heuristic: comment/docstring-marker stripping + regex) — pure
  over diff TEXT alone, no repository access. Used for `--fixture` mode
  (no PR to resolve real file content from), and as the PER-FILE
  FALLBACK when the precise path can't run (a deleted/renamed file has
  no post-image; unparseable content) — every fallback is printed to
  stderr, never silently taken.

## Blocking (promoted #5010, architect ruling 2026-08-21)

A backward scan of ~400 merged PRs found 9 real candidates (PRs where
this discriminator's "touched the doc?" branch actually ran). Hand-
inspection: 3 CONFIRMED TRUE POSITIVES (`_action_retrieval`, PRs
#4572/#4567/#4563 — PR #4582's own title: "sweep stale
action_retrieval.universal_wrappers_enabled refs #4572's own fix scope
missed", direct evidence a human had to notice and fix what this gate
would have flagged), 0 false positives after the round-2 (tokenizer)
rewrite (was 7/9 with the line-heuristic-only round 1).

**"0 FP among 9" is NOT why this is blocking** — with only 9 trials,
0/9 barely bounds the true rate at all (roughly "under ~30%", a loose
statistical read, not a safety argument). The promotion rests on three
STRUCTURAL points instead, none of them a count:

1. **The false-positive CLASS was eliminated, not merely unobserved.**
   Both real FP classes found in calibration (`#`-comment prose,
   docstring prose) tokenize as impossible-to-confuse with a NAME token
   once the extractor reads real pre-image file content instead of
   guessing from diff-line text — see `find_removed_identifiers_precise`.
   The disclosed line-heuristic limitation (a `#`/quote inside a string
   literal) disappears the same way, for the same reason.
2. **The fallback speaks up.** Every time the precise path can't run
   for a file (no pre/post image; unparseable content), it prints to
   stderr and falls back to the weaker line heuristic FOR THAT FILE
   ONLY — never silently. Verified against two real triggers
   (#4560/#4454, both files deleted entirely by their PR), with a test
   that asserts the fallback message itself, not just the result.
3. **The decisive point: the PASSING action is always the correct
   action.** This gate's pass condition is "touch the doc file in the
   SAME PR" — even in a false-positive world, the only thing an author
   does in response is open that doc and confirm it's still accurate.
   There is no dead end a false positive can walk someone into. Staying
   warn-only despite that would return to #5003's own founding problem
   ("the prescription exists, but firing depends on someone's memory").

**Revert condition** (required — a promotion with no way back is a
one-way door): if a genuine false positive is found in production use,
`.github/workflows/check-doc-drift.yml`'s job reverts to warn-only
(annotation, `main()`'s exit code ignored) until the new FP class is
closed the same structural way as the first two — never by loosening
`_MIN_IDENTIFIER_LENGTH` or adding another marker-guessing heuristic.
"""
from __future__ import annotations

import argparse
import json
import keyword
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from io import StringIO
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
    # 1-indexed line numbers in the PRE-image (removed) / POST-image
    # (added) file content — same length/order as removed_lines/
    # added_lines respectively. Added for #5010 round 2 (the precise,
    # tokenizer-backed extraction path needs to know which real file
    # line a removed/added token sits on; see find_removed_identifiers_precise).
    removed_line_nos: "tuple[int, ...]"
    added_line_nos: "tuple[int, ...]"


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _iter_file_diffs(diff_text: str) -> "list[_FileDiff]":
    """Split a unified ``git diff`` / ``gh pr diff`` text into per-file
    removed/added line buckets, tracking each line's real 1-indexed
    pre-/post-image line number from the hunk headers (``@@ -a,b +c,d @@``).
    Only the ``+``/``-`` content lines are kept (not the file-header
    ``+++``/``---`` lines, which this regex excludes by requiring the line
    not start with ``+++``/``---``)."""
    files: "list[_FileDiff]" = []
    current_path: "str | None" = None
    removed: "list[str]" = []
    added: "list[str]" = []
    removed_nos: "list[int]" = []
    added_nos: "list[int]" = []
    old_line = 0
    new_line = 0

    def _flush() -> None:
        if current_path is not None:
            files.append(_FileDiff(
                current_path, tuple(removed), tuple(added),
                tuple(removed_nos), tuple(added_nos),
            ))

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            _flush()
            removed, added, removed_nos, added_nos = [], [], [], []
            old_line = new_line = 0
            # "diff --git a/path b/path" — take the b/ side (post-image path,
            # which is what a rename/new-file case still resolves to).
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            current_path = match.group(2) if match else None
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file" — not a real line
        elif line.startswith("@@"):
            hunk = _HUNK_HEADER_RE.match(line)
            if hunk:
                old_line = int(hunk.group(1))
                new_line = int(hunk.group(2))
        elif line.startswith("+"):
            added.append(line[1:])
            added_nos.append(new_line)
            new_line += 1
        elif line.startswith("-"):
            removed.append(line[1:])
            removed_nos.append(old_line)
            old_line += 1
        else:
            # context line — present in both images, advance both counters.
            old_line += 1
            new_line += 1
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


def _removed_identifiers_in_file(fd: "_FileDiff") -> "set[str]":
    """The LINE-HEURISTIC path (comment/docstring line-stripping + regex)
    for one file — the #5007 / #5010-round-1 approach. Kept as the
    FALLBACK for when the precise, tokenizer-backed path
    (:func:`find_removed_identifiers_precise`) can't run for a file (no
    pre-image available, or the content doesn't tokenize as valid
    Python) — never silently dropped, see that function's docstring."""
    is_py = fd.path.endswith(".py")
    removed_lines = _strip_comments_and_docstrings(fd.removed_lines) if is_py else list(fd.removed_lines)
    added_lines = _strip_comments_and_docstrings(fd.added_lines) if is_py else list(fd.added_lines)
    removed_tokens = {tok for line in removed_lines for tok in _IDENTIFIER_RE.findall(line)}
    added_tokens = {tok for line in added_lines for tok in _IDENTIFIER_RE.findall(line)}
    return {tok for tok in removed_tokens - added_tokens if is_salient_identifier(tok)}


def find_removed_identifiers(diff_text: str, *, src_prefix: str = "src/") -> "set[str]":
    """Identifiers this diff removed from ``src/``, via the LINE-HEURISTIC
    path only (:func:`_removed_identifiers_in_file`) — pure over diff text,
    no repository access. This is what ``--fixture`` mode uses (no PR
    number to resolve real file content from), and the fallback
    :func:`find_removed_identifiers_precise` reaches for per-file when the
    precise path can't run. Prefer :func:`find_removed_identifiers_precise`
    when a PR number is available — it does not share this function's
    known false-positive class (docstring/comment prose)."""
    removed_ids: "set[str]" = set()
    for fd in _iter_file_diffs(diff_text):
        if not fd.path.startswith(src_prefix):
            continue
        removed_ids |= _removed_identifiers_in_file(fd)
    return removed_ids


def _name_tokens_by_line(source: str) -> "dict[str, set[int]]":
    """Pure: map each NAME token (identifier) in *source* to the set of
    1-indexed line numbers it starts on, via Python's own ``tokenize``
    module. A NAME token structurally cannot come from inside a STRING
    (docstring or otherwise) or COMMENT token — the tokenizer resolves
    quoting correctly by construction, so this closes BOTH gaps the
    line-heuristic path only approximated: docstring prose (never
    tokenizes as NAME) and the disclosed ``#``/quote-inside-a-string-
    literal limitation (no marker-guessing needed at all). Python
    keywords (``def``, ``class``, ``return``, ...) are excluded — never
    real removed/added identifiers. Returns ``{}`` if *source* does not
    tokenize as valid Python (a caller falls back to the line heuristic
    and logs it — see :func:`find_removed_identifiers_precise`)."""
    result: "dict[str, set[int]]" = {}
    try:
        for tok in tokenize.generate_tokens(StringIO(source).readline):
            if tok.type == tokenize.NAME and not keyword.iskeyword(tok.string):
                result.setdefault(tok.string, set()).add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError, OSError):
        return {}
    return result


def find_removed_identifiers_precise(
    diff_text: str, repo_root: Path, pre_sha: str, post_sha: str,
    *, src_prefix: str = "src/",
) -> "set[str]":
    """The precise identifier-removal extractor (architect ruling, #5010
    round 2): for each ``src/`` ``.py`` file in the diff, tokenizes the
    REAL pre-image and post-image file content (:func:`_file_content_at`,
    via ``git show``) instead of guessing from diff-line text — the diff
    only tells us WHICH LINES changed; the real file tells us what those
    lines actually ARE (code vs. comment vs. docstring), which is what
    was structurally missing from the line-heuristic path (#5010
    calibration: 6 of 9 real-world false positives were docstring prose,
    since #5007's ``#``-only stripper never covered triple-quoted text).

    Falls back to :func:`_removed_identifiers_in_file` (the line
    heuristic), restricted to one file at a time, when the precise path
    can't run for that file — no pre-image (e.g. a newly added file has
    no prior content to diff against) or content that fails to tokenize.
    Every fallback is printed to stderr; never silently dropped."""
    removed_ids: "set[str]" = set()
    for fd in _iter_file_diffs(diff_text):
        if not fd.path.startswith(src_prefix) or not fd.path.endswith(".py"):
            continue
        pre_content = _file_content_at(pre_sha, fd.path, repo_root)
        post_content = _file_content_at(post_sha, fd.path, repo_root)
        if pre_content is None or post_content is None:
            print(
                f"WARN check_doc_drift: falling back to line heuristic for "
                f"{fd.path!r} — pre/post file content unavailable at "
                f"{pre_sha}/{post_sha}",
                file=sys.stderr,
            )
            removed_ids |= _removed_identifiers_in_file(fd)
            continue
        pre_tokens = _name_tokens_by_line(pre_content)
        post_tokens = _name_tokens_by_line(post_content)
        if not pre_tokens and pre_content.strip():
            print(
                f"WARN check_doc_drift: falling back to line heuristic for "
                f"{fd.path!r} — pre-image did not tokenize as valid Python",
                file=sys.stderr,
            )
            removed_ids |= _removed_identifiers_in_file(fd)
            continue
        removed_line_nos = set(fd.removed_line_nos)
        added_line_nos = set(fd.added_line_nos)
        removed_here = {name for name, lines in pre_tokens.items() if lines & removed_line_nos}
        added_here = {name for name, lines in post_tokens.items() if lines & added_line_nos}
        for tok in removed_here - added_here:
            if is_salient_identifier(tok):
                removed_ids.add(tok)
    return removed_ids


def _file_content_at(sha: str, path: str, repo_root: Path) -> "str | None":
    """``git show <sha>:<path>`` — the real file content at a specific
    ref. Returns ``None`` if the ref/path doesn't resolve (most commonly:
    a newly-added file has no pre-image, so ``pre_sha`` never contained
    it) rather than raising — the caller treats ``None`` as "fall back to
    the line heuristic for this file", never as an error to propagate."""
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=repo_root, capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def resolve_pr_shas(pr_number: int) -> "tuple[str, str]":
    """(pre_sha, post_sha) — the two refs :func:`find_removed_identifiers_precise`
    reads real file content from. Prefers the merge commit
    (``gh pr view --json mergeCommit``): pre = the merge commit's first
    parent, post = the merge commit itself — the most robust choice for
    an already-merged PR, since both are guaranteed present in this
    repo's own local git history (no extra fetch needed). Falls back to
    ``baseRefOid``/``headRefOid`` for an OPEN PR (no ``mergeCommit`` yet —
    the live-CI case, where the PR branch is what's checked out)."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "mergeCommit,baseRefOid,headRefOid"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    merge = data.get("mergeCommit")
    if merge and merge.get("oid"):
        return f"{merge['oid']}^", merge["oid"]
    return data["baseRefOid"], data["headRefOid"]


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
    diff, via the LINE-HEURISTIC path (:func:`find_removed_identifiers`),
    that also do not survive anywhere else in the current ``src/`` tree
    (``git grep``). Used only when no PR number is available (``--fixture``
    mode) — prefer :func:`resolve_gone_identifiers_precise` otherwise."""
    return {
        identifier
        for identifier in find_removed_identifiers(diff_text)
        if not identifier_survives_in_src(identifier, repo_root)
    }


def resolve_gone_identifiers_precise(
    diff_text: str, repo_root: Path, pre_sha: str, post_sha: str,
) -> "set[str]":
    """Network/filesystem wrapper: identifiers removed from ``src/`` by
    this diff, via the PRECISE (tokenizer-backed) path
    (:func:`find_removed_identifiers_precise`), that also do not survive
    anywhere else in the current ``src/`` tree (``git grep``)."""
    return {
        identifier
        for identifier in find_removed_identifiers_precise(diff_text, repo_root, pre_sha, post_sha)
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
    pr_number: "int | None" = None,
) -> "list[Finding]":
    """End-to-end: wires the network/filesystem wrappers into
    :func:`check_doc_drift_pure`. This is the impure entry point — tests
    exercise :func:`check_doc_drift_pure` directly with fixture data.

    *pr_number*, when given, resolves real pre/post file content
    (:func:`resolve_pr_shas`) and uses the PRECISE, tokenizer-backed
    extraction path (:func:`resolve_gone_identifiers_precise`) — the
    #5010-round-2 architect ruling: guessing from diff-line text alone
    (comment/docstring line-stripping) has a real, measured
    false-positive class the tokenizer path structurally does not.
    ``None`` (``--fixture`` mode, no PR to resolve shas from) falls back
    to the line-heuristic path (:func:`resolve_gone_identifiers`)."""
    touched = find_touched_files(diff_text)
    if pr_number is not None:
        pre_sha, post_sha = resolve_pr_shas(pr_number)
        gone = resolve_gone_identifiers_precise(diff_text, repo_root, pre_sha, post_sha)
    else:
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


def _print_findings_and_exit_code(findings: "list[Finding]", source: str) -> int:
    """The blocking-gate contract, pulled out as its own small pure-ish
    function so the exit code — the actual thing a required CI check
    reads — is directly testable without needing real PR/repo access
    (see tests/scripts/test_check_doc_drift_5003.py's `test_main_*`
    cases). 0 clean, 1 on any finding — see module docstring,
    "Blocking" (promoted #5010)."""
    if not findings:
        print(f"OK — no doc-drift findings ({source}).")
        return 0

    print(f"FAIL — doc-drift findings ({source}):\n")
    for f in findings:
        print(f"  {f.identifier!r} removed from src/, still named in {f.doc_path} (untouched by this PR)")
    print(
        f"\nTotal: {len(findings)} finding(s). Fix: touch the doc file listed above in "
        "THIS PR (a removal note, or an update) — that is the correct action whether "
        "this is a real drift or a coincidental identifier match; see the module "
        "docstring's 'Blocking' section for why.",
    )
    return 1


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    pr_number = None
    if args.pr is not None:
        try:
            diff_text = fetch_pr_diff(args.pr)
        except subprocess.CalledProcessError as exc:
            print(f"gh pr diff failed: {exc.stderr}", file=sys.stderr)
            return 2
        source = f"PR #{args.pr}"
        pr_number = args.pr
    else:
        diff_text = Path(args.fixture).read_text(encoding="utf-8")
        source = args.fixture

    findings = check_doc_drift(diff_text, pr_number=pr_number)
    return _print_findings_and_exit_code(findings, source)


if __name__ == "__main__":
    sys.exit(main())
