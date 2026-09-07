#!/usr/bin/env python3
"""Shared marker-detection primitives for declaration/exemption/closing-note
gates (#5919 stage 1).

## The class this closes

Five `scripts/` gates each read prose (a PR comment, a PR body, a source
comment) looking for a human's *declaration* — "TESTS-READ", "RE-READ",
"BLOCKING-CLEARED", "this file is exempt", "this PR closes #N". Four of
them (the ones this module serves; the fifth, `check_open_blocking_
checkboxes.py`'s `_resolves_via_body`, is a separate PR — house-rule
change, architect's call) used to detect a declaration by `.search()`-ing
free text for a keyword, unanchored. #5919's census found the shared
defect: **a sentence saying the declaration does NOT apply still contains
the keyword**, so "no TESTS-READ note yet" and "No RE-READ needed here"
both read as the note they deny. Each gate had grown its own regex to do
this, so each gate could independently reopen the same hole — a
per-gate discipline problem CLAUDE.md itself names ("A rule with no
trigger is not a rule"): the fix has to be a shared BOUNDARY a sixth gate
cannot avoid, not five separate reminders.

## Why a marker (not smarter prose-reading)

A human declaration and a human *discussion* of that declaration use the
same words — no keyword search, however placed, can tell them apart from
content alone (the discussing PR comment "This PR is not ready for a
TESTS-READ note yet" is not distinguishable from a real one by which
words it contains). What DOES distinguish them is a fixed, deliberate
syntax — something nobody produces while writing an ordinary sentence
about the same topic. Every primitive here compiles a pattern matched
with `re.Pattern.match()` against a single, pre-isolated line (never
`.search()` over a whole multi-line comment/body) so the marker's position
is part of the contract, not a coincidence of where a keyword happened to
land.

Three shapes cover the four gates:

* **line-start, optional role-prefix** (`role_prefixed_marker`) — the
  marker keyword must OPEN the comment's first line, either bare
  (``TESTS-READ (head <sha>)``, the form most existing notes in this repo
  already use) or immediately after the CLAUDE.md rule-2 role prefix
  (``**[role]** — TESTS-READ (head <sha>)``) — both anchored at column 0,
  never merely present somewhere on the line. A sentence that merely
  DISCUSSES the marker never opens a line this way: "This PR is not ready
  for a TESTS-READ note yet" does not start with the keyword (something
  else — "This") and does not start with a role prefix immediately
  followed by it either. The same anchor also excludes a backtick- or
  quote-wrapped marker (`` `TESTS-READ (head x)` ``, ``> TESTS-READ
  ...``) — those do not open the line with the literal keyword or role
  prefix either, a backtick or `>` character does. Used by TESTS-READ
  (`check_tests_read_names_its_tree.py`) and RE-READ
  (`check_blocking_has_reread_note.py`).
* **HTML comment** (`html_comment_marker`) — invisible when rendered,
  explicit and greppable in source; nobody writes `<!-- tag: ... -->`
  by accident mid-sentence. Used by the `closing-check: discussing #N`
  exemption in `check_pr_closing_intent.py`.
* **fixed-prefix source comment** (`fixed_comment_marker`) — a Python
  comment line whose first token, at column 0, is the exact directive
  word, e.g. `# EXEMPT: out_of_process_reyn`. A prose comment
  *mentioning* the same fixture name mid-sentence does not start there.
  Used by `check_subprocess_reyn_pin.py`'s exemption declaration.

## What this module deliberately does NOT do

It does not decide what counts as a declaration for any one gate — each
gate still owns its own vocabulary (which keyword, which marker body) and
still owns matching that vocabulary against whatever it reads (a first
line, a whole comment, a whole file). This module only supplies the
SHAPE that keeps that matching from being satisfiable by ordinary prose.
It is not itself a gate and has no `main`.

stdlib-only (`re`), so every importing gate stays dep-free.
"""
from __future__ import annotations

import re

#: CLAUDE.md rule 2's required opening of every PR comment/issue body:
#: ``**[role]** — ``. A marker anchored immediately after this prefix
#: cannot be produced by a sentence that merely discusses the marker's
#: keyword, because ordinary prose does not open a comment's first line
#: with a role prefix followed directly by that keyword.
ROLE_PREFIX = r"\*\*\[[^\]]+\]\*\*\s+—\s+"


def role_prefixed_marker(keyword_pattern: str, *, flags: int = re.IGNORECASE) -> "re.Pattern[str]":
    """Compile a marker regex matching only a line that OPENS with
    *keyword_pattern* — bare, or immediately after the role prefix
    (:data:`ROLE_PREFIX`). Both alternatives are anchored at column 0;
    neither matches the keyword appearing later on the line, inside
    backticks/a quote block, or merely discussed in a longer sentence.

    Match the result with ``.match()`` (or ``.search()`` — the leading
    ``^`` makes them equivalent here) against a single, already-isolated
    line — never a whole multi-line comment, or the anchor is
    meaningless."""
    return re.compile(rf"^(?:{ROLE_PREFIX})?{keyword_pattern}", flags)


def first_line(text: str) -> str:
    """*text*'s first line, newline-delimited, no trailing newline — the
    single line every marker in this module is matched against. A
    document's line 2 onward (grounds, discussion, prior history) is
    never handed to a marker pattern; the exclusion is syntactic, not a
    matter of where a keyword happens to be more or less likely."""
    return text.split("\n", 1)[0]


def html_comment_marker(tag: str, payload_pattern: str, *, flags: int = re.IGNORECASE) -> "re.Pattern[str]":
    """Compile an HTML-comment marker regex: ``<!-- tag: <payload> -->``.

    *payload_pattern* is inserted as-is (own capture groups included) so
    a caller that needs the payload back (e.g. the issue numbers a
    ``discussing`` marker names) gets the same group numbering it would
    from a hand-written pattern. Invisible when a PR body/comment is
    rendered, but explicit and greppable in the raw source — nobody
    writes an HTML comment by accident in the middle of an ordinary
    sentence, which is what makes this a declaration rather than a
    coincidence of wording."""
    return re.compile(rf"<!--\s*{re.escape(tag)}:\s*{payload_pattern}\s*-->", flags)


def fixed_comment_marker(directive: str, *, flags: int = 0) -> "re.Pattern[str]":
    """Compile a ``# DIRECTIVE: <name>`` marker anchored to a Python
    comment's OWN start.

    Match the result with ``.match()`` against ONE physical line at a
    time (e.g. ``for line in text.splitlines(): fixed_comment_marker(...)
    .match(line)``), never ``.search()`` over the whole file — a prose
    comment that merely *mentions* the directive word or the exempted
    name mid-sentence (``# this subprocess call never touches
    out_of_process_reyn``) does not start a line with ``# DIRECTIVE:``,
    so it does not match; only a line deliberately opening with the
    exact directive does."""
    return re.compile(rf"^#\s*{re.escape(directive)}:\s*(\S+)", flags)
