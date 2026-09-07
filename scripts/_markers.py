#!/usr/bin/env python3
"""Shared marker-detection primitives for declaration/exemption/closing-note
gates (#5919 stage 1, extended by stage 2 and stage 3).

## The class this closes

Five `scripts/` gates each read prose (a PR comment, a PR body, a source
comment) looking for a human's *declaration* — "TESTS-READ", "RE-READ",
"BLOCKING-CLEARED", "this file is exempt", "this PR closes #N". Four of
them (the ones stage 1 served; the fifth, `check_open_blocking_
checkboxes.py`'s `_resolves_via_body` / marker anchoring, landed in
stage 2, also via this module) used to detect a declaration by
`.search()`-ing free text for a keyword, unanchored. #5919's census found
the shared
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

`undecorated_after_role_prefix` (stage 2) is a companion to
`role_prefixed_marker`, not a fourth shape: once a gate strips its own
markdown decoration (backtick/`*`) before matching, a blanket strip over
the WHOLE line would erase a real role prefix's own literal `**[...]**`
syntax right along with it — this function strips only AFTER a detected
role prefix, so `role_prefixed_marker`'s anchor still has something to
match. Used by `check_open_blocking_checkboxes.py` and (reusing THAT
gate's own marker regex + decoration pattern, not a second copy)
`check_blocking_has_reread_note.py`.

## `MARKER_BLOCKING`/`MARKER_CLEARED`/`NOTE_MARKER`/`DECORATION`/`undecorated`

A name read across a module boundary is either RECOGNITION (belongs
HERE — a syntax question every consumer answers identically) or
DECISION (belongs on its OWNING gate, as a PUBLIC name — never a
private one, regardless of who else needs it). `check_open_blocking_
checkboxes.py`'s BLOCKING/CLEARED marker shapes and decoration
handling, and `check_tests_read_names_its_tree.py`'s TESTS-READ marker
shape, are all recognition — every gate that checks for a BLOCKING/
CLEARED comment or a TESTS-READ note means the EXACT SAME syntax by it,
so those five live here, as the ONE place each is defined. By contrast,
`check_tests_read_names_its_tree.py`'s `note_names_head` (does a note
name a specific head) is a decision, not a syntax question — it stays
on that module, as a public name, never here.

A gate that needs a name defined on a DIFFERENT gate for its own
recognition step — rather than here, or as that gate's own public
decision name — is reaching across a boundary this module and its
sibling gates exist to keep from opening: nothing in this repo should
ever read a leading-underscore name off a sibling module.

`MARKER_BLOCKING`/`MARKER_CLEARED`/`NOTE_MARKER` are compiled,
ready-to-use `re.Pattern` objects (not builders like
`role_prefixed_marker` above) — every consumer of the BLOCKING/CLEARED
or TESTS-READ shape means the exact same thing by it (RE-READ is the
one keyword that stays genuinely gate-owned: only `check_blocking_has_
reread_note.py` ever means it), so reusing the identical compiled
pattern (not reconstructing a second copy from the same regex text) is
what keeps every consumer from drifting apart the moment one of them
changes — the same reasoning `check_blocking_has_reread_note.py` already
relied on when it first borrowed these as private instances.

`DECORATION`/`undecorated` are the UNCONDITIONAL sibling of
`undecorated_after_role_prefix` — for text that is not itself a
role-prefix candidate (near-miss detection's own bare-word check, which
never anchors to a role prefix at all).

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


def undecorated_after_role_prefix(line: str, decoration: "re.Pattern[str]") -> str:
    """*line*, with *decoration* stripped EVERYWHERE EXCEPT a leading
    CLAUDE.md role prefix (:data:`ROLE_PREFIX`), if one opens the line.

    #5919 stage 2 (`check_open_blocking_checkboxes.py`'s own real
    incident, then reused by `check_blocking_has_reread_note.py` when its
    own consumption of that gate's rename surfaced the same shape): a role
    prefix is ITSELF written with literal ``**...**`` — the same character
    class a gate's own decoration-tolerance (backtick/`*`, #5522) strips.
    A blanket, unconditional strip run over the WHOLE line would erase the
    role prefix's own asterisks right along with any decoration AROUND a
    marker that follows it, so a real, decorated comment like
    ``**[lead-coder]** — **BLOCKING (head `abc1234`)**`` would never match
    :func:`role_prefixed_marker`'s own anchored pattern at all post-strip
    (its `ROLE_PREFIX` requires the literal ``**[...]** — `` a blanket
    strip would have just erased). Stripping only AFTER the detected role
    prefix keeps that pattern intact while still tolerating decoration
    around the marker itself.

    *decoration* is caller-supplied (never a single frozen pattern here)
    because :mod:`_markers` does not own any one gate's decoration
    vocabulary — the same "shape here, vocabulary there" boundary
    :func:`role_prefixed_marker` already draws for the keyword itself."""
    prefix_match = re.match(ROLE_PREFIX, line)
    if prefix_match is None:
        return decoration.sub("", line)
    return line[:prefix_match.end()] + decoration.sub("", line[prefix_match.end():])


def first_nonempty_line(text: str) -> str:
    """*text*'s first NON-EMPTY line, stripped — the single line every
    marker in this module is matched against. A document's line 2
    onward (grounds, discussion, prior history) is never handed to a
    marker pattern; the exclusion is syntactic, not a matter of where a
    keyword happens to be more or less likely.

    #5919 stage 3: replaces a bare ``text.split("\\n", 1)[0]`` (this
    function's pre-stage-3 shape, and — independently —
    ``check_tests_read_names_its_tree.py``'s own pre-stage-3 ``_first_
    line``) — that shape returns an EMPTY string for a comment/body that
    opens with a blank line before its real content, which then fails
    every marker regex trivially, not because no marker was posted but
    because the marker's own line was never even looked at. Verified
    directly (not merely inferred from the two implementations'
    difference — architect's own explicit request): a real
    ``TESTS-READ (head <sha>)`` comment prefixed with one blank line
    matches under this rule and was silently missed under the old
    bare-split one — a genuine, previously-uncounted 5th fail-open
    instance in #5919's own census (architect: the 2026-09-07 census
    counted 4 silent-miss gates; this is the 5th, house rule 8/#5453,
    found only once this function actually ran against a real
    constructed input). See ``tests/scripts/test_markers_5919.py``'s own
    witness for that exact input. House rule 7's own "識別行 = marker
    直後の最初の非空行" definition already reads a marker's OWN position
    by this same rule, so this is not a NEW convention — it is the
    recognition side finally matching the decision side's own
    definition."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


#: Markdown decoration this repo's own marker conventions tolerate around
#: a keyword or SHA — backtick code-spans and `**`/`*` emphasis. Neither a
#: SHA nor a marker keyword itself ever contains a backtick or asterisk,
#: so stripping this cannot turn a non-marker line into a false-positive
#: match (architect ruling, #5522: "許容側が自然" — people decorate SHAs
#: and keywords by hand; stripping once is cheaper than asking every
#: writer never to).
DECORATION = re.compile(r"[`*]")


def undecorated(line: str) -> str:
    """*line* with every :data:`DECORATION` character stripped,
    unconditionally.

    For text that is NOT itself a role-prefix candidate — a near-miss
    watcher's own bare-word check (which must never anchor to a role
    prefix at all, see this module's own "What this module deliberately
    does NOT do" section on why a watcher stays unanchored). A line that
    DOES open with a real role prefix needs :func:`undecorated_after_
    role_prefix` instead — a blanket strip here would erase the role
    prefix's own literal ``**[...]**`` syntax right along with any
    decoration around it."""
    return DECORATION.sub("", line)


#: The BLOCKING / BLOCKING-CLEARED marker recognition shapes (#5919 stage
#: 3, moved from ``check_open_blocking_checkboxes.py`` — see this
#: module's own docstring section for why these are SHARED compiled
#: instances, not a builder each consumer calls independently). Requires
#: the marker keyword co-located with ``(head <sha>)`` on the same
#: anchored line — not the bare word found anywhere on it. `IGNORECASE`
#: is dropped (``flags=0``) for the same reason the pre-#5919 regex
#: already gave: prose is far more likely to write "blocking" lowercase
#: than a deliberate marker is.
MARKER_BLOCKING = role_prefixed_marker(
    r"BLOCKING(?!-CLEARED)\s*\(\s*head\s+([0-9a-fA-F]{7,40})\s*\)", flags=0,
)
MARKER_CLEARED = role_prefixed_marker(
    r"BLOCKING-CLEARED\s*\(\s*head\s+([0-9a-fA-F]{7,40})\s*\)", flags=0,
)

#: House rule 8's TESTS-READ/TESTS-READY marker (#5919 stage 3, moved from
#: ``check_tests_read_names_its_tree.py``, which now reads THIS name —
#: never a locally-redefined copy — so a future widening of the keyword
#: (e.g. tolerating a further typo) cannot land in one gate and silently
#: not the other). Tolerates the ``TESTS-READY`` typo several sessions
#: produce, because the gate must not turn a typo into "no note landed."
NOTE_MARKER = role_prefixed_marker(r"TESTS-READ(?:Y)?\b")


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
