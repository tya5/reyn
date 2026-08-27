#!/usr/bin/env python3
"""Detect PR-body / GitHub-parser contradictions about issue-closing intent.

INVARIANT: the intent a PR body *declares* about issue #N must match the
closing behavior GitHub's own parser (``closingIssuesReferences``, readable
via ``gh pr view <N> --json closingIssuesReferences,body`` while the PR is
still open) actually resolved for #N. This script detects contradictions —
it never infers intent beyond what the body's declaring phrases literally
say.

Five checks, all facets of the same invariant except check 5 (see its own
entry below for why it is deliberately NOT an intent-comparison check):

  1. **false negative** — body declares closing intent (``Closes #N`` /
     ``Fixes #N`` / ``Resolves #N``, in any casing, even inside backticks)
     but N is NOT in ``closingIssuesReferences`` → the author *wanted* to
     close N but GitHub's parser did not pick it up. In both real examples
     the cause is backtick-fencing: #2990 wrote `` `Closes #2620` `` and
     #3006 wrote `` `Closes #2972` ``, GitHub honored neither, and both
     issues stayed open until a human closed them by hand.
  2. **false positive** — body declares non-closing intent (``part of #N`` /
     ``toward #N``) but N IS in ``closingIssuesReferences`` → the PR will
     auto-close N on merge despite the author saying it shouldn't. Real
     example: #3003→#2827. Subject to the use-vs-mention rule below: a
     ``part of #N`` occurrence in a body that also declares an unfenced,
     canonical ``Closes #N`` for the same N is read as a *mention*, not a
     scope declaration.
  3. **undeclared** — N IS in ``closingIssuesReferences`` but the body
     contains NO declaration at all (neither closing nor non-closing) about
     N → GitHub's parser silently picked up a closing reference from prose
     the author never flagged as intentional (e.g. "auto-close #N" in a
     sentence). This is the hole a closing-keyword-only check (1+2) misses:
     both checks 1 and 2 presuppose the author wrote *some* declaring
     phrase; an author who writes neither slips through both.
  4. **commit-message declaration** — a PR *commit message* (not the PR
     body) contains a closing declaration for #N, but the PR body does NOT
     also declare closing intent for #N. Real incident: PR #3187's body was
     correctly ``part of #1909`` and ``closingIssuesReferences`` was empty
     (checks 1-3 above all PASS) — but an intermediate commit's message
     still carried ``Closes #1909`` from an earlier draft. GitHub's default
     squash-merge commit message is the *concatenation* of the PR's commit
     messages, so that stray ``Closes #1909`` line survived into the squash
     commit body (merge commit ``d9b4c3a0``, line 19) and GitHub auto-closed
     #1909 on merge — despite the PR body being clean and
     ``closingIssuesReferences`` showing nothing.

     Why scanning individual commit messages (rather than trying to predict
     the eventual squash message) is sufficient: at CI time, while the PR is
     still open, the *final* squash-commit message does not exist yet — a
     human merging the PR can still hand-edit GitHub's proposed squash body.
     But GitHub's proposed default is deterministically the concatenation of
     the already-known commit messages, so if a closing keyword sits in ANY
     commit message, it WILL appear in the default squash body unless a
     human edits it out by hand. This check flags that "may still be
     silently carried forward" state; it does not (and structurally cannot,
     before merge) know whether a human will hand-edit it away. That is by
     design, not a gap: the check's job is to catch the state that CAN leak
     a close, not to certify that a human removed it.

     **Title source (#5321, corrected #5330)**: the PR's own TITLE is a
     third text that can carry the same leak — ``gh api repos/<owner>/
     <repo>`` measured this repo's own ``squash_merge_commit_title:
     COMMIT_OR_PR_TITLE`` setting, which means a 2+-commit PR's squash
     headline is built from the PR TITLE, not any one commit's own title.
     `` `fix #5299 regression in the cron runner` `` as a title on a
     2+-commit PR is exactly the #3187 leak class via this third text —
     unscanned before #5321 (no known merged PR has actually tripped it;
     the gap was found by inspection of the script's own field list, not
     a real incident). #5321's own first version gated this on
     ``len(commit_messages) >= 2`` — WRONG (#5330, architect + lead-coder):
     that reasoning is true ONLY for a squash merge. This repo ALSO
     allows a plain merge commit (``allow_merge_commit: true``,
     ``merge_commit_message: PR_TITLE``, the SAME ``gh api`` call, a field
     #5321 didn't read) — a merge commit's body is the PR TITLE
     regardless of commit count, and the merge METHOD a human will pick
     is not known at check time, so "1 commit → safe" cannot be asserted.
     The scan is UNCONDITIONAL. A 1-commit PR's title duplicating its own
     single commit-message finding is not a false positive in practice
     (this repo's own ``fix(#N):``-shaped commit titles never match
     ``_CLOSING_RE`` at all — the parenthesis right after the keyword
     breaks the match); where a title genuinely matches, a duplicate
     finding on the SAME real leak is still correct, just two reports of
     one true positive.
  5. **negated closing keyword** (#4992, real incident 2026-08-21) — a
     closing keyword with a negation word in the narrow window immediately
     before it (e.g. "does not close #N", "will never fix #N"), in the PR
     body, a commit message, OR the PR title (#5321, corrected #5330 —
     unconditional, same as check 4's title scan). UNCONDITIONAL — fires
     regardless of
     ``closingIssuesReferences``, never comparing declared intent against
     the parser's actual behavior the way checks 1-4 do. Real incidents
     #4834 and #4986 were BOTH auto-closed by exactly this shape ("it does
     not claim to close #4834 either" / "Does not claim to fix #4986") —
     GitHub's parser does not read the negation, only the keyword+#N
     substring, so the author's "not" never protected anything. Checks 1/2
     were SILENT on both: the parser's own reading ("there is a
     declaration to close #N") matched what checks 1/2 already expect from
     an honored ``Closes``-shaped declaration, so neither found a
     mismatch to report — negation doesn't just fail to protect, it
     manufactures the *appearance* of a consistent, correctly-declared
     close. See ``find_negated_closing_declarations``'s own docstring and
     the ``_NEGATION_TERMS_EN``/``_NEGATION_TERMS_JA`` comment for the full vocabulary and the
     backtick escape hatch.

Declaring-phrase vocabulary — three forms, all checked against the parser:

  * **closing** — ``Closes #N`` / ``Fixes #N`` / ``Resolves #N``
  * **non-closing (scope)** — ``part of #N`` / ``toward #N``
  * **mention-only** — ``<!-- closing-check: discussing #N -->``

The third form exists because a PR body that *talks about* closing keywords
rather than using them is a real and unavoidable false-positive class for
checks 1/2 — a doc PR, a CLAUDE.md rule-4 explanation, or this script's own
PR, which must quote ``Closes #N`` to explain what it detects. It is also
broader than quoting: #2989's ordinary prose "Order-dependency is resolved:
#2975" collides with the keyword+``#N`` shape and trips check 1 with no
keyword being discussed at all.

The marker is a *declaration*, not a mute — it says "I mention N, I do not
close N", and is checked against the parser exactly like the other two
forms (marker says discussing #N while ``closingIssuesReferences`` contains
N → check 2 FAILs). It is scoped per-issue, never body-wide, so a body that
both declares and discusses keeps check 1 live on its genuine declaration.
An HTML comment is the chosen form because it is invisible when rendered
but visible, greppable, and explicit in the source — as against an
invisible zero-width space, which is a disguise rather than a declaration:
undiscoverable by the next author, who would then have no way to tell a
true finding from a mystery red and would learn to ignore the gate.

Design constraint (ratified in issue #3007's discussion): check 3 must NOT
re-enumerate GitHub's own closing-keyword vocabulary (closes/fixes/resolves/
closed/fixed/resolved/close/fix/resolve and so on) — that would be a census
of GitHub's parser that silently breaks the moment GitHub changes its
keyword set. Check 3 only needs our own small declaring-phrase vocabulary
(closing: Closes/Fixes/Resolves variants; non-closing: part of/toward) to
decide whether the body says *anything at all* about N — GitHub's own
parser output (``closingIssuesReferences``) remains the sole source of
truth for what will actually close.

Backtick defusal: Check 1 must still match ``Closes #N`` even when written
as `` `Closes #N` ``. GitHub's parser **does** respect backticks and will
silently decline to close a fenced reference — that gap is precisely the
defect check 1 exists to catch, not something to mirror. Verified on the
motivating incidents: #2990's body carries a fully-backticked
`` `Closes #2620` `` and #3006's a `` `Closes #2972` ``, and both PRs'
``closingIssuesReferences`` are empty — which is *why* #2620 and #2972
stayed open after merge and a human had to close them by hand.

So this matcher is deliberately **stricter than** GitHub's, in the one
direction that surfaces the contradiction: a fenced ``Closes #N`` is still
the author *declaring* intent to close N, and GitHub not honoring it is the
mismatch worth failing on. The script therefore strips backtick characters
from the body before matching rather than skipping fenced code spans.

Use vs mention (#3559): the finders above assume every occurrence of a
declaring phrase is a declaration *about this PR's scope*. For check 2's
``part of #N`` / ``toward #N`` vocabulary that assumption has a systematic
counterexample, and it is one the repo's own rules manufacture. CLAUDE.md
rule 4 requires an author to enumerate every open ``part of #X`` PR before
writing ``Closes #X``; a Test-plan line recording that they did so
necessarily *quotes the phrase* — "enumerated part of #3553, none open" —
inside a body whose operative declaration is ``Closes #3553``. Check 2 read
the quotation as a declaration and failed the PR (real incident: #3558).
A PR that follows rule 4 **and records having followed it** went red, while
not recording it stayed green — a perverse incentive against the very
discipline the gate enforces.

The criterion is *use vs mention*, not "is it fenced". Backticks are one
symptom of mention; the same sentence written bare is equally a mention, so
excluding fenced spans would neither be sufficient here nor safe elsewhere
(it would blind check 1 — see ``_body_as_author_declared``). What settles it is
**whether the same body canonically declares ``Closes #N`` for that same
N**. A PR cannot coherently claim both "this closes #N" and "this is only
part of #N" about its own scope, so when both appear, one is not a scope
claim — and the canonical closing declaration is the one the author
actually made. The ``part of #N`` occurrence is then a mention
(:func:`find_canonical_closing_declarations`).

Everything rests on how *narrow* "canonically declares" is, and two wider
rules were written and measured before this one:

  * **Plain co-occurrence over ``_CLOSING_RE``.** Went GREEN on real
    #3003 — its body declares ``part of #2827`` and, forty lines down,
    explains that a keyword there "would auto-close #2827 with part 1
    undone". ``_CLOSING_RE`` matches that ``close`` (it must: GitHub
    honors it, which is check 1's and check 3's business), so the rule
    accepted keyword-shaped prose as the author's declaration and
    silenced check 2 on its own motivating incident.
  * **"The closing declaration must come FIRST."** Fixed #3003 but went
    RED when the Test-plan record sits *above* the ``Closes`` line —
    the verdict flipping on line order alone, with the red case being
    exactly the plain-prose record this rule exists to permit. That
    fails the acceptance condition (no marker, no fence, still green),
    since the only workarounds are reordering or fencing — both rituals.

:data:`_CANONICAL_CLOSING_RE` is therefore restricted to the declaring
vocabulary this module documents above — ``Closes``/``Fixes``/``Resolves``
— which is narrower than the keyword set GitHub honors. That is not a
census of GitHub's parser (the thing check 3 must not do); it is the
opposite, a deliberately small set of forms an author uses to *declare*.
``auto-close`` is not one of them, so #3003 stays red without any appeal
to position, which is what makes the rule order-independent.

Fenced spans are removed first (``_body_as_github_parses``) so a body that
merely *quotes* `` `Closes #N` `` — a doc PR, or this script's own PR —
cannot corroborate either; GitHub honors no fenced keyword, so an N in
``closingIssuesReferences`` alongside only a fenced ``Closes #N`` came from
a bare prose keyword, i.e. the #3003 shape again. That makes checks 1 and 2
read the same syntax in deliberately opposite directions, because their
failure costs are asymmetric: check 1's job is to surface a declaration
GitHub ignored (so it must be stricter than GitHub), while check 2's job is
to surface an unintended close (so a quoted keyword must not count as the
author's intent). ``_body_as_author_declared``'s docstring already established that
per-check divergence from GitHub's behavior is this gate's design, not a
deviation from it.

Note what this deliberately does NOT require: no marker, no token, no
fence. A solution that made the author annotate their Test-plan line would
only *reduce* the perverse incentive — any extra ritual attached to
recording the discipline keeps discouraging the record.

**The accepted trade: this rule prefers a false negative to a false
positive, and that is a decision, not a side effect.** The rule is
unconditional — given a canonical ``Closes #N``, *every* other
``part of #N`` in that body becomes a mention, including one the author
meant as a genuine scope declaration. So a body that declares both forms
canonically and unfenced for the same N is now SILENT where it used to
FAIL. That direction was chosen deliberately:

  * The false positive it removes is *systematic and self-reinforcing*.
    It fires on a body that followed rule 4 and recorded doing so, and the
    cheapest way for an author to clear it is to delete the record. A gate
    that trains people out of writing down the discipline it enforces
    corrupts the evidence it depends on, and it does so on every
    rule-4-compliant PR, forever.
  * The false negative it accepts is *self-contradictory input*. "Closes
    #N" and "only part of #N" cannot both be true of one PR, so the body
    is already wrong before this gate reads it. The author has stated an
    intent to close, GitHub agrees, and the resulting close is the one
    they asked for — there is no surprise closure, which is the harm
    check 2 exists to prevent. It is also not silent everywhere: a
    genuinely non-closing PR that carries a stray canonical ``Closes #N``
    still trips check 4 if that keyword reaches a commit message.

``test_check2_is_silent_when_both_forms_are_canonical`` pins this leg, so
the behavior is recorded rather than merely reachable. If the trade is ever
revisited, the honest lever is the ``<!-- closing-check: discussing #N -->``
marker, which is exempt from this rule precisely because it is unambiguous
syntax — not a re-narrowing of the ``part of`` reading, which is where the
perverse incentive lives.

Why check 2 is NOT replaced by performing rule 4's enumeration itself
(``gh pr list --state all --search "#X in:body"``), which would measure the
property (#3368: an open part-of PR still existed) rather than the body's
self-consistency, and would structurally dissolve the perverse incentive.
Three measured reasons, none of them cost — the query is one search per
declared closing issue and returns in ~0.75s:

  * **It is not a substitute.** The enumeration answers "do sibling
    part-of PRs remain open for #X?" (#3368). Check 2 answers "will THIS
    PR close #N against its own declaration?" (#3003→#2827). Disjoint
    incidents; dropping check 2 loses the #3003 class outright, and
    keeping it means the use-vs-mention fix is needed either way.
  * **It inherits the same use/mention ambiguity, one scope up.** GitHub's
    search is a text search over PR bodies — the same substrate as the
    regexes here, not a different kind of measurement. Measured on this
    repo: ``"part of #3300" in:body`` returns 6 PRs, and PR #3309 is among
    them because its prose says "#3301 (part of #3300 P1 C)" while its own
    declaration is "part of #3273. Closes #3287" — a mention, not a
    declaration. (Unquoted, it is far worse: search drops ``#``, so
    ``#3300 in:body`` and ``3300 in:body`` return the same 24 PRs,
    including several that merely contain the digits.) When the false hit
    is a *sibling*, the failing PR's author has no remedy — they cannot
    edit another PR's body — which is strictly worse than the incentive
    being replaced.
  * **It is not deterministic.** The verdict would depend on repo state at
    CI time rather than on the PR under test, so the same commit can go
    green and then red without being touched, and GitHub's search index is
    eventually consistent on top of that. A required merge gate that
    re-runs to a different answer teaches authors to re-run until green.

(#3003 is *not* evidence about backticks: its body's backticked
`` `Closes` `` has no adjacent issue number and could not have closed
anything. What GitHub actually parsed there is the bare ``close #2827``
substring inside the prose "auto-close #2827" — i.e. #3003 is the
bare-prose-keyword case, which is what check 3 covers.)

Check 4's escape hatch reuses the exact same ``<!-- closing-check:
discussing #N -->`` marker vocabulary — no new syntax — but scoped to the
SAME text blob as the existing per-issue scoping principle above, extended
one level: the PR body is one blob, and each commit message is its own
separate blob. A commit message that itself contains both a closing keyword
(e.g. explaining ``Closes #N`` as worked example text, exactly as this
script's own commit messages must avoid doing) AND the discussing marker
naming N *within that same commit message* is exempt for that commit. A
marker living in the PR body does NOT exempt a closing keyword sitting in a
commit message — they are different blobs — which is deliberate: it is
precisely what keeps check 4 catching the #3187 shape, where the body
carries an unrelated ``discussing #1909`` marker (added for its own body-
text reasons) while a commit message independently carries the real
``Closes #1909`` leak. Letting the body's marker blanket-exempt commit
content would silently defeat check 4 on exactly the incident it exists to
catch.

The parsing logic (``find_closing_declarations`` / ``find_nonclosing_declarations``
/ ``check_contradictions``) is pure — no network, no subprocess — so it is
fully unit-testable. ``fetch_pr_data`` is a thin ``gh`` wrapper kept
separate so the pure logic can be exercised without hitting GitHub.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Declaring-phrase vocabulary (OUR vocabulary — deliberately small and NOT a
# re-enumeration of GitHub's closing-keyword parser; see module docstring).
# ---------------------------------------------------------------------------

# Closing-intent declaration: Close(s|d)/Fix(es|ed)/Resolve(s|d) followed by
# "#N", optionally separated by a colon/whitespace. Case-insensitive so
# "closes", "Closes", "CLOSES" all match (as does GitHub's own parser).
_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)

# Non-closing-intent declaration: "part of #N" / "toward(s) #N".
_NONCLOSING_RE = re.compile(
    r"\b(?:part of|towards?)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Check 5 vocabulary (#4992, architect ruling, real incident #4834/#4986,
# 2026-08-21): negating a closing keyword does NOT protect against it —
# GitHub's own parser matches the bare keyword+#N substring and does not
# read the surrounding negation. "it does not claim to close #4834 either"
# and "Does not claim to fix #4986" both auto-closed their issue on merge,
# same-timestamp with the PR, reason=COMPLETED, no human involved.
#
# Deliberately a SYNTACTIC check, not an intent check: a negated closing
# keyword is ALWAYS wrong, independent of whether GitHub's parser agrees —
# GitHub closes regardless of the author's negation, so there is no
# "intent matched, no problem" case for this shape to fall into. This is
# check 5's whole reason for existing separately from checks 1-3, which DO
# compare declared intent against closingIssuesReferences: those checks
# would have been silent here (the parser's own reading — "declares
# closing intent for #4834" — MATCHES what the parser did; check 1 asks
# "declared but not resolved" and check 2 asks "declared non-closing but
# resolved", and this body's negation sentence reads to _CLOSING_RE as a
# closing declaration, which the parser then genuinely honored — no
# mismatch for checks 1/2 to catch). Architect's own words: 否定は保護で
# ないどころか、正しさの見た目を作った ── gate も GitHub も同じく「#4834
# を閉じる宣言が在る」と読み、check 1（宣言≠実際）が「矛盾なし」で沈黙
# した。(negation isn't just non-protective — it manufactures the
# APPEARANCE of correctness: both the gate and GitHub read "there is a
# declaration to close #4834" identically, so check 1's own "declared ≠
# actual" logic found no mismatch and stayed silent.)
#
# Window is deliberately NARROW (architect: "窓は狭く") — only the last
# few words immediately before the keyword are checked, not the whole
# body. A negation word far earlier in an unrelated sentence must not
# false-positive on an unrelated later closing keyword elsewhere in the
# body.
_NEGATION_WINDOW_WORDS = 6

# #4992 review (reviewer finding via lead-coder): the original vocabulary
# was matched via bare substring containment (`term in window`), which is
# wrong on its own terms — "not" is a substring of "notable", "note", and
# "annotate", so a genuine, correct declaration like "Note: closes #N"
# would have been WRONGLY flagged. English terms are matched on WORD
# boundaries instead (see `_NEGATION_PATTERN` below); Japanese terms stay
# substring-matched deliberately (they attach directly to a verb stem with
# no preceding space in real usage — "〜ない" — so a \b-anchored regex
# would not isolate them the way it does ASCII words).
#
# "cannot" is listed SEPARATELY from "not", not merged into it, precisely
# because word-boundary matching is now correct: \bnot\b does NOT match
# inside the single compound word "cannot" (no boundary between "can" and
# "not" there), even though it DOES match the "not" in "does not"/"did
# not" (both have a real space, hence a real word boundary, before
# "not"). Losing "cannot" coverage by switching to \b matching without
# adding it back explicitly would have silently narrowed the vocabulary
# architect specified.
_NEGATION_TERMS_EN = ("not", "cannot", "never", "without", "no longer")
_NEGATION_TERMS_JA = ("ない", "ません", "せず")
_NEGATION_PATTERN = re.compile(
    "|".join(rf"\b{re.escape(term)}\b" for term in _NEGATION_TERMS_EN),
    re.IGNORECASE,
)


def _window_has_negation(window: str) -> bool:
    """True if *window* (the last few words before a closing keyword,
    lowercased) contains a negation term — English terms on word
    boundaries (see :data:`_NEGATION_PATTERN`'s own comment for why
    ``cannot`` needs its own entry), Japanese terms by substring."""
    if _NEGATION_PATTERN.search(window):
        return True
    return any(term in window for term in _NEGATION_TERMS_JA)

# Mention-only declaration (the third declaration type):
#     <!-- closing-check: discussing #2620 #2972 -->
# An HTML comment, so it is invisible in the rendered PR body but visible,
# greppable, and explicit in the source. It names the specific issue numbers
# the body merely *talks about* (quoting a closing keyword to explain it,
# or prose that happens to collide with the keyword+#N shape) rather than
# declares intent to close.
#
# This is a declaration, not a mute: it says "I mention N, I do not close N",
# and it is checked against the parser exactly like the other two forms — if
# the parser closes N anyway, that is a contradiction and check 2 fails.
# Deliberately per-issue rather than body-wide: a body-wide switch would
# disable check 1 for a body's *genuine* declarations too (this script's own
# PR both declares `Closes #3007` and discusses #2620/#2972/#2827 as
# examples — a body-wide marker would silently drop check-1 protection from
# the real declaration, turning the escape hatch into a bypass).
_DISCUSSING_MARKER_RE = re.compile(
    r"<!--\s*closing-check:\s*discussing\s+((?:#\d+[\s,]*)+?)\s*-->",
    re.IGNORECASE,
)
_ISSUE_NUM_RE = re.compile(r"#(\d+)")


# ---------------------------------------------------------------------------
# The two readings of a PR body.
#
# This module deliberately reads the same body two ways, and the pair below is
# the ONLY place that difference lives. They are named for the question they
# answer, not for the string surgery they perform, because the surgeries look
# alike (both are about backticks) while the meanings are opposite — and a
# caller that picks the wrong one gets a plausible-looking wrong answer rather
# than an error. See the module docstring's "Use vs mention" section.
# ---------------------------------------------------------------------------

# Fenced code — a ``` block, then an inline `span`. Used only to build the
# GitHub-side reading below.
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# #4992 review (lead-coder, real measurement): a single-backtick span CAN
# contain a line ending — CommonMark/GFM's own spec ("line endings are
# converted to spaces" within a code span) — so a sentence a PR author
# wraps across two source lines while backtick-fencing it (an ordinary
# markdown-authoring habit, and exactly what a long negated-keyword
# sentence following check 5's own "wrap it in backticks" advice tends to
# produce) is STILL one fenced span to GitHub's real renderer/parser, not
# two unfenced fragments. The previous `[^`\n]*` pattern excluded
# newlines and so treated a two-line-wrapped span as unfenced — a real,
# measured false positive on this PR's own body (PR #4993:
# closingIssuesReferences confirmed empty for the wrapped issue numbers,
# i.e. GitHub genuinely did not parse them as closing references, but
# this gate's OWN check 5 still flagged them). ``re.DOTALL`` non-greedy so
# `.` matches a newline too, without needing to special-case it.
#
# #4992 post-merge review (architect): the discriminator this fix rests
# on is the OBSERVATION above (GitHub genuinely did not parse the wrapped
# span as closing), not "because CommonMark says so" — a spec citation
# alone would NOT have been accepted as sufficient grounds for widening a
# gate whose whole job is stopping risky bare phrasing; what makes this
# an acceptable widening is that it only recognizes spans GitHub itself
# already treats as safe, never loosens detection of anything GitHub
# would actually act on. Disclosed explicitly, per the same discipline
# this repo's own pre-conclusion checklist requires: that observation is
# n=1 (this PR's own body, one wrapped span) — supported by CommonMark's
# spec as the reason to expect it generalizes, but not independently
# re-measured against a second real body.
#
# Also flagged (not a defect, a scope note): `_body_as_github_parses`
# feeds check 2's own use-vs-mention reading too
# (`find_canonical_closing_declarations`), so this same widening — a
# span that spans a line break is now recognized as fenced — applies
# there as well, not only to check 5. This is the CORRECT, intended
# consequence of the two checks sharing one "what does GitHub actually
# see as fenced" primitive, not an accidental side effect: check 2 asks
# "did the author canonically declare a close GitHub will act on", and a
# multi-line-fenced `Closes #N` is exactly as GitHub-inert as a
# single-line-fenced one, so it must be excluded from check 2's
# corroboration the same way.
_INLINE_CODE_RE = re.compile(r"`[^`]*?`", re.DOTALL)


def _body_as_author_declared(text: str) -> str:
    """The body as the AUTHOR meant it — fences are defused, not honored.

    Removes backtick characters so a fenced keyword still matches. GitHub's
    parser **does** respect backticks — a body containing `` `Closes #N` ``
    does NOT auto-close N on merge (verified: #2990 and #3006 both fence
    their closing keyword and both have an empty ``closingIssuesReferences``;
    #2620 and #2972 consequently stayed open).

    This reading is deliberately stricter than GitHub's: a fenced
    ``Closes #N`` is still the author declaring intent to close N, so we must
    see the declaration that GitHub's parser ignored — that mismatch IS the
    check-1 defect. Hence: strip the fence characters, do not skip fenced
    spans.
    """
    return text.replace("`", "")


def _body_as_github_parses(text: str) -> str:
    """The body as GITHUB reads it — fenced spans are honored and dropped.

    The mirror image of :func:`_body_as_author_declared`: fenced blocks and
    inline code spans are removed (blocks first, then spans), so a merely
    quoted keyword contributes nothing. Used where the question is what the
    author *actually declared to the world* rather than what they typed —
    check 2's use-vs-mention corroboration.
    """
    return _INLINE_CODE_RE.sub(" ", _FENCED_BLOCK_RE.sub(" ", text))


# The CANONICAL declaring form only — ``Closes #N`` / ``Fixes #N`` /
# ``Resolves #N``, which is exactly the closing vocabulary the module
# docstring documents an author as *declaring* with. Deliberately NARROWER
# than _CLOSING_RE, which also matches the bare and past-tense shapes GitHub
# honors (``close``/``closed``/``fix``/``fixed``/...) because check 1 must
# catch anything GitHub would act on. Check 2 asks a different question — did
# the author *declare* a close? — and keyword-shaped prose is not a
# declaration. Real falsifier: #3003's body says a keyword there "would
# auto-close #2827", which _CLOSING_RE matches and this does not. The
# ``(?<![\w-])`` guard additionally refuses a hyphen- or word-glued keyword.
_CANONICAL_CLOSING_RE = re.compile(
    r"(?<![\w-])(?:closes|fixes|resolves)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)


def find_closing_declarations(body: str) -> set[int]:
    """Return the set of issue numbers the body declares closing-intent for."""
    text = _body_as_author_declared(body)
    return {int(m.group(1)) for m in _CLOSING_RE.finditer(text)}


def find_nonclosing_declarations(body: str) -> set[int]:
    """Return the set of issue numbers the body declares non-closing-intent for."""
    text = _body_as_author_declared(body)
    return {int(m.group(1)) for m in _NONCLOSING_RE.finditer(text)}


def find_canonical_closing_declarations(body: str) -> set[int]:
    """Return issue numbers the body declares closing intent for, canonically.

    "Canonically" = an unfenced ``Closes #N`` / ``Fixes #N`` / ``Resolves #N``
    (:data:`_CANONICAL_CLOSING_RE`), i.e. the author writing the declaring
    form — not merely prose that collides with a keyword GitHub honors, and
    not a quoted example. Deliberately reads fences the opposite way to
    :func:`find_closing_declarations`; see the module docstring's "Use vs
    mention" section for why the two checks diverge here.

    Check 2 uses this to tell use from mention: when the same body declares
    ``Closes #N`` canonically, a ``part of #N`` elsewhere in it is not a
    second, contradictory scope claim — it is a mention. Position-independent
    on purpose (see the docstring: an ordering rule reddened the very
    plain-prose record this rule exists to permit).
    """
    return {
        int(m.group(1)) for m in _CANONICAL_CLOSING_RE.finditer(_body_as_github_parses(body))
    }


def find_discussing_declarations(body: str) -> set[int]:
    """Return issue numbers declared mention-only via a ``closing-check`` marker.

    Reads ``<!-- closing-check: discussing #N #M -->`` markers (see
    ``_DISCUSSING_MARKER_RE``). Backticks are NOT stripped first: the marker
    is an exact, deliberate syntax an author types, so it should be matched
    as written rather than reconstructed out of fenced text.
    """
    out: set[int] = set()
    for marker in _DISCUSSING_MARKER_RE.finditer(body):
        out.update(int(n) for n in _ISSUE_NUM_RE.findall(marker.group(1)))
    return out


def find_negated_closing_declarations(body: str) -> set[int]:
    """Return issue numbers whose closing-keyword declaration is negated
    (#4992, architect ruling) — e.g. "does not claim to close #4834",
    "will never fix #4986". Always wrong: GitHub's parser does not read
    the negation, only the keyword+#N substring, so a negated closing
    keyword closes the issue exactly as if the negation were absent. See
    this module's ``_NEGATION_TERMS_EN``/``_NEGATION_TERMS_JA`` comment for the real incident and
    why this is a syntactic check, not an intent-matching one.

    Reads the body as GITHUB parses it (fenced spans honored/removed,
    :func:`_body_as_github_parses`) — a negated keyword wrapped in
    backticks poses no real auto-close risk (GitHub does not parse inside
    backticks either), so it is not flagged; that fencing is this check's
    own sanctioned escape hatch when the literal phrase must appear at
    all (see the error message below).

    Uses :data:`_CLOSING_RE` (the broad, GitHub-matching keyword set —
    bare and past-tense forms included), not the narrower
    ``_CANONICAL_CLOSING_RE`` — GitHub's real parser honors "close"/
    "closed"/"fix"/"fixed" etc. exactly as it honors "Closes", and both
    real incidents used the bare/past-tense form ("close" / "fix"), not
    the canonical declaring form.
    """
    text = _body_as_github_parses(body)
    out: set[int] = set()
    for m in _CLOSING_RE.finditer(text):
        preceding_words = text[:m.start()].split()[-_NEGATION_WINDOW_WORDS:]
        window = " ".join(preceding_words).lower()
        if _window_has_negation(window):
            out.add(int(m.group(1)))
    return out


@dataclass
class Finding:
    check: int
    issue: int
    message: str


def check_contradictions(
    body: str,
    closing_refs: list[int],
    commit_messages: list[str] | None = None,
    title: str | None = None,
) -> list[Finding]:
    """Pure contradiction detector — no network, no inference of intent.

    ``body`` is the raw PR body text. ``closing_refs`` is the list of issue
    numbers GitHub's parser (``closingIssuesReferences``) actually resolved
    as closing targets for this PR. ``commit_messages`` is the list of full
    commit-message texts (headline + body) for every commit on the PR — used
    only by check 4 (see module docstring); defaults to none, so callers
    that only have body/closing_refs (e.g. the existing test suite) are
    unaffected.

    ``title`` (#5321) — the PR's own title, scanned by checks 4 and 5
    ONLY when ``len(commit_messages) >= 2`` (this repo's own measured
    ``squash_merge_commit_title: COMMIT_OR_PR_TITLE`` setting: a 1-commit
    PR's squash headline is that ONE commit's own title, already covered
    by scanning ``commit_messages`` — the PR title never reaches the
    merge commit in that case, and flagging it there would be a false
    positive on the common 1-commit PR, not a genuine leak). A 2+-commit
    PR's squash headline IS the PR title, so a closing keyword there is
    the exact #3187/check-4 leak class, just via a THIRD text GitHub
    reads (body, commit messages, and now title) — unscanned before
    #5321 (real gap named in #5321, not yet incident-confirmed: no
    known merged PR has actually tripped this one).
    """
    closing_declared = find_closing_declarations(body)
    nonclosing_declared = find_nonclosing_declarations(body)
    discussing_declared = find_discussing_declarations(body)
    closing_refs_set = set(closing_refs)
    findings: list[Finding] = []

    # Check 1 (false negative): declared closing but parser did not close.
    #
    # Exempt only the issue numbers a `closing-check: discussing` marker
    # names. The exemption is per-issue, so a body that both declares
    # (`Closes #A`) and discusses (`#B`) keeps check 1 live on #A. And it
    # never reaches the parser's output: an N that IS in closing_refs while
    # marked "discussing" is caught by check 2 below, so the marker can
    # silence a *declaration* the author says they never made, but can
    # never silence an actual closure.
    for n in sorted(closing_declared - closing_refs_set - discussing_declared):
        findings.append(
            Finding(
                check=1,
                issue=n,
                message=(
                    f"body declares closing intent for #{n} (Closes/Fixes/"
                    f"Resolves) but GitHub's parser did NOT resolve #{n} as "
                    "a closing reference — merge will NOT close it. Note "
                    "GitHub does NOT honor a closing keyword inside backticks "
                    "(this is how #2620 and #2972 stayed open), so check the "
                    f"keyword for #{n} is unfenced and its form/number are "
                    "exactly what GitHub's parser expects. If the body only "
                    f"*discusses* #{n} (quoting a keyword, or prose that "
                    "collides with the keyword shape) rather than declaring "
                    f"intent, add: <!-- closing-check: discussing #{n} -->"
                ),
            )
        )

    # Check 2 (false positive): declared non-closing but parser will close.
    #
    # Both non-closing declaration forms land here — "part of/toward #N"
    # (scope) and a `discussing #N` marker (mention-only). They contradict
    # the parser identically: the author said they are not closing N, and
    # GitHub says it will.
    #
    # Use vs mention (#3559): "part of #N" is prose, so an occurrence is not
    # automatically a declaration about THIS PR's scope. When the same body
    # ALSO declares `Closes #N` canonically, the two cannot both be scope
    # claims about N, and the canonical declaration is the one the author
    # made — so the "part of #N" occurrence is a mention. That is the shape
    # CLAUDE.md rule 4's enumeration record produces (#3558), where without
    # this the gate penalises recording the discipline it enforces.
    #
    # What does the work is the NARROWNESS of the corroborating vocabulary,
    # not position. Two rules were measured and rejected first:
    #   * plain co-occurrence over _CLOSING_RE — went GREEN on real #3003,
    #     whose body says a keyword there "would auto-close #2827". That
    #     silenced check 2 on its own motivating incident.
    #   * "the closing declaration must come FIRST" — went RED when the
    #     Test-plan record was written above the `Closes` line, i.e. on
    #     exactly the plain-prose record this rule exists to permit, with
    #     the verdict flipping on line order alone.
    # _CANONICAL_CLOSING_RE excludes the first (auto-close is not `Closes`)
    # and needs no ordering, so it excludes the second too. Fences are
    # removed so a merely quoted `Closes #N` cannot corroborate either.
    #
    # The `discussing` marker is NOT subject to this: it is exact syntax an
    # author types for this single purpose, so it is never a mention, and a
    # body carrying both `Closes #N` and a `discussing #N` marker is genuine
    # author confusion worth reporting.
    nonclosing_operative = nonclosing_declared - find_canonical_closing_declarations(body)

    for n in sorted((nonclosing_operative | discussing_declared) & closing_refs_set):
        form = (
            "a closing-check 'discussing' marker"
            if n in discussing_declared and n not in nonclosing_operative
            else "part of/toward"
        )
        findings.append(
            Finding(
                check=2,
                issue=n,
                message=(
                    f"body declares non-closing intent for #{n} ({form}) but "
                    f"GitHub's parser WILL close #{n} on merge — the declared "
                    "intent and the parsed behavior contradict. Rewrite the "
                    f"reference to #{n} so it isn't a closing keyword (GitHub "
                    "parses bare keywords in prose, e.g. 'auto-close "
                    f"#{n}'), or if closing #{n} is actually intended, use "
                    "Closes/Fixes/Resolves instead."
                ),
            )
        )

    # Check 3 (undeclared): parser will close but body says nothing about N.
    #
    # A `discussing` marker counts as a declaration here so the same N is
    # not reported twice — the marker-vs-parser contradiction is already
    # reported by check 2 above, with a more precise message. No N in
    # closing_refs can escape: it is covered by check 2 or check 3.
    declared_any = closing_declared | nonclosing_declared | discussing_declared
    for n in sorted(closing_refs_set - declared_any):
        findings.append(
            Finding(
                check=3,
                issue=n,
                message=(
                    f"#{n} will be closed on merge (GitHub's parser resolved "
                    f"it via closingIssuesReferences) but the body contains "
                    f"no declaration at all about #{n} (no Closes/Fixes/"
                    f"Resolves, no part of/toward, no closing-check marker). "
                    "GitHub's parser likely picked up a bare keyword in prose "
                    f"(e.g. 'auto-close #{n}'). If closing #{n} is "
                    f"intentional, write 'Closes #{n}' explicitly; if not, "
                    f"rephrase so the reference to #{n} doesn't read as a "
                    "closing keyword."
                ),
            )
        )

    # Check 4 (commit-message declaration): a commit message declares
    # closing intent for #N that the PR body does NOT also declare. See
    # module docstring for why this is checked independently of
    # closing_refs (GitHub's default squash-merge body concatenates commit
    # messages, so this leaks even when closingIssuesReferences is empty).
    #
    # Exemption is scoped per commit message (its own text blob), not to
    # the PR body's discussing markers — see module docstring "Check 4's
    # escape hatch" section for why a body-wide exemption would silently
    # defeat this check on the #3187 shape it exists to catch.
    commit_closing_declared: set[int] = set()
    for msg in commit_messages or []:
        commit_closing_declared |= find_closing_declarations(msg) - find_discussing_declarations(msg)

    for n in sorted(commit_closing_declared - closing_declared):
        findings.append(
            Finding(
                check=4,
                issue=n,
                message=(
                    f"a commit message on this PR declares closing intent "
                    f"for #{n} (Closes/Fixes/Resolves) but the PR BODY does "
                    f"not also declare closing intent for #{n}. GitHub's "
                    "default squash-merge commit message concatenates all "
                    "commit messages, so this keyword will be carried into "
                    f"the merge commit and can auto-close #{n} on merge "
                    "regardless of what the PR body says or what "
                    "closingIssuesReferences currently shows (real "
                    "incident: PR #3187 / issue #1909, merge commit "
                    f"d9b4c3a0). If closing #{n} is intended, also write "
                    f"'Closes #{n}' in the PR body. If not, rewrite or "
                    f"squash away the offending commit message, or if the "
                    f"commit message only *discusses* #{n} rather than "
                    "declaring intent, add "
                    f"<!-- closing-check: discussing #{n} --> to that SAME "
                    "commit message."
                ),
            )
        )

    # Check 4, title source (#5321, corrected #5330 — architect + lead-coder
    # converged): the PR title, scanned UNCONDITIONALLY, not gated on commit
    # count. First version of this PR gated on len(commit_messages) >= 2,
    # reasoning "a 1-commit PR's title never becomes the merge commit" —
    # that reasoning is true ONLY for a SQUASH merge
    # (squash_merge_commit_title=COMMIT_OR_PR_TITLE, this repo's own
    # setting). This repo ALSO allows a plain merge commit
    # (allow_merge_commit=true, merge_commit_message=PR_TITLE, gh api
    # repos/tya5/reyn — lead-coder's own field, missed the first time): a
    # merge commit's body is the PR TITLE regardless of commit count. The
    # merge METHOD a human will pick is not known at check time, so
    # "1 commit → safe" cannot be asserted — the gate must scan the title
    # unconditionally. False-positive concern (a 1-commit PR's title
    # duplicating its own single commit-message finding) does not apply in
    # practice: this repo's own `fix(#N):`-shaped commit titles never match
    # _CLOSING_RE at all (its `\s*:?\s*` does not allow a `(` between the
    # keyword and `#N`) — verified directly (architect). Where a title DOES
    # genuinely match, a duplicate finding on the SAME real leak is not
    # incorrect, just two reports of one true positive.
    if title:
        title_closing_declared = (
            find_closing_declarations(title) - find_discussing_declarations(title)
        )
        for n in sorted(title_closing_declared - closing_declared):
            findings.append(
                Finding(
                    check=4,
                    issue=n,
                    message=(
                        f"this PR's TITLE declares closing intent for #{n} "
                        f"(Closes/Fixes/Resolves) but the PR BODY does not "
                        f"also declare closing intent for #{n}. If this PR "
                        "merges as a plain merge commit, the commit body IS "
                        "the PR TITLE (this repo's own "
                        "merge_commit_message=PR_TITLE setting) regardless "
                        "of commit count; if it squashes, the title becomes "
                        "the headline whenever this PR has 2+ commits "
                        "(squash_merge_commit_title=COMMIT_OR_PR_TITLE). "
                        "Either way this keyword can carry into the merge "
                        f"commit and auto-close #{n} on merge regardless of "
                        "what the PR body says (the #3187/check-4 leak "
                        "class, via a third text GitHub reads — #5321). If "
                        f"closing #{n} is intended, also write "
                        f"'Closes #{n}' in the PR body. If not, reword the "
                        f"title so it doesn't read as a closing keyword — a "
                        "discussing marker in the body cannot exempt a "
                        "title (this check scans the TITLE's own text, "
                        "same per-source scoping check 4 already uses for "
                        "commit messages, not a body-wide exemption)."
                    ),
                )
            )

    # Check 5 (negated closing keyword, #4992): a closing keyword with a
    # negation word in the narrow window immediately before it — see this
    # module's ``_NEGATION_TERMS_EN``/``_NEGATION_TERMS_JA`` comment and ``find_negated_closing_
    # declarations``'s own docstring for the real incident (#4834/#4986)
    # and why this fires unconditionally, never comparing against
    # ``closing_refs``. Also checked in the PR body's own commit messages
    # (same #3187-shaped leak class check 4 exists for — a negated keyword
    # in a commit message rides into the default squash-merge body exactly
    # like an unnegated one does).
    for n in sorted(find_negated_closing_declarations(body)):
        findings.append(
            Finding(
                check=5,
                issue=n,
                message=(
                    f"the body contains a NEGATED closing keyword for #{n} "
                    "(e.g. 'does not close', 'never fixes') — GitHub's "
                    "parser does not read the negation, only the "
                    f"keyword+#{n} substring, so this WILL close #{n} on "
                    "merge exactly as if the negation were absent (real "
                    "incident: #4834 and #4986 both auto-closed this way, "
                    "same timestamp as the merge, reason=COMPLETED, no "
                    f"human involved). Rewrite without the verb: '#{n} "
                    f"stays open' or '#{n} is out of scope' — or omit the "
                    "issue number entirely if the reader doesn't need to "
                    "follow it. If the literal phrase must appear, wrap it "
                    f"in backticks (`` `closes #{n}` `` — GitHub does not "
                    "parse inside backticks)."
                ),
            )
        )
    for msg in commit_messages or []:
        for n in sorted(find_negated_closing_declarations(msg)):
            findings.append(
                Finding(
                    check=5,
                    issue=n,
                    message=(
                        f"a commit message on this PR contains a NEGATED "
                        f"closing keyword for #{n} — same defect as above, "
                        "carried into the default squash-merge commit body "
                        f"(the #3187/check-4 leak class) even if the PR "
                        "body itself is clean. Rewrite without the verb, "
                        "or wrap in backticks."
                    ),
                )
            )

    # Check 5, title source (#5321, corrected #5330): same negated-keyword
    # shape, scanned UNCONDITIONALLY — same reasoning as check 4's title
    # scan above (the merge METHOD is not known at check time, and a plain
    # merge commit carries the PR TITLE regardless of commit count).
    if title:
        for n in sorted(find_negated_closing_declarations(title)):
            findings.append(
                Finding(
                    check=5,
                    issue=n,
                    message=(
                        f"this PR's TITLE contains a NEGATED closing "
                        f"keyword for #{n} — same defect as check 5's body/"
                        "commit-message findings, carried into the merge "
                        "commit either as a plain merge commit's body "
                        "(merge_commit_message=PR_TITLE) or a squash "
                        "headline (squash_merge_commit_title="
                        "COMMIT_OR_PR_TITLE). "
                        "Rewrite the title without the verb, or wrap the "
                        "phrase in backticks."
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _commit_message_text(commit: dict) -> str:
    """Reconstruct a full commit-message text from a ``gh``-shaped commit dict.

    ``gh pr view --json commits`` returns each commit as
    ``{"messageHeadline": ..., "messageBody": ..., ...}`` — the same split
    ``git log`` uses. Joined back with a blank line the same way ``git``
    itself displays/concatenates a commit message, so the reconstructed
    text matches what a human (or GitHub's squash-message builder) actually
    sees.
    """
    headline = commit.get("messageHeadline") or ""
    msg_body = commit.get("messageBody") or ""
    return f"{headline}\n\n{msg_body}" if msg_body else headline


def fetch_pr_data(pr_number: int) -> tuple[str, list[int], list[str], str]:
    """Fetch (body, closing_issue_numbers, commit_messages, title) for an
    open PR.

    Uses ``gh pr view``. ``commit_messages`` is one full message string
    (headline + body) per commit currently on the PR — see check 4 in the
    module docstring for why these are scanned independently of
    ``closingIssuesReferences``. ``title`` (#5321) is the PR's own title —
    see :func:`check_contradictions`'s own docstring for when it actually
    becomes the squash-merge headline.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "closingIssuesReferences,body,commits,title",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    body = data.get("body") or ""
    closing_refs = [ref["number"] for ref in data.get("closingIssuesReferences") or []]
    commit_messages = [_commit_message_text(c) for c in data.get("commits") or []]
    title = data.get("title") or ""
    return body, closing_refs, commit_messages, title


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect contradictions between a PR body's declared closing "
            "intent and GitHub's parsed closingIssuesReferences."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pr",
        type=int,
        metavar="N",
        help="Live PR number — fetched via `gh pr view N --json closingIssuesReferences,body`.",
    )
    group.add_argument(
        "--fixture",
        metavar="PATH",
        help=(
            "Path to a JSON fixture file with keys 'body' (str), "
            "'closingIssuesReferences' (list of {'number': N} or plain ints), "
            "optionally 'commits' (list of {'messageHeadline':, "
            "'messageBody':} or plain strings), and optionally 'title' "
            "(str, #5321) — same shape as `gh pr view --json "
            "closingIssuesReferences,body,commits,title`. Lets this check "
            "run offline / in tests without hitting GitHub."
        ),
    )
    return parser


def _closing_refs_from_fixture(raw: object) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for item in raw:  # type: ignore[union-attr]
        if isinstance(item, dict):
            out.append(int(item["number"]))
        else:
            out.append(int(item))
    return out


def _commit_messages_from_fixture(raw: object) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for item in raw:  # type: ignore[union-attr]
        if isinstance(item, dict):
            out.append(_commit_message_text(item))
        else:
            out.append(str(item))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pr is not None:
        try:
            body, closing_refs, commit_messages, title = fetch_pr_data(args.pr)
        except subprocess.CalledProcessError as exc:
            print(f"gh pr view failed: {exc.stderr}", file=sys.stderr)
            return 2
        source = f"PR #{args.pr}"
    else:
        from pathlib import Path

        raw = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        body = raw.get("body") or ""
        closing_refs = _closing_refs_from_fixture(raw.get("closingIssuesReferences"))
        commit_messages = _commit_messages_from_fixture(raw.get("commits"))
        title = raw.get("title") or ""
        source = args.fixture

    findings = check_contradictions(body, closing_refs, commit_messages, title)

    if not findings:
        print(f"OK — no closing-intent contradictions found ({source}).")
        return 0

    print(f"FAIL — closing-intent contradictions found ({source}):\n")
    for f in findings:
        print(f"  [check {f.check}] #{f.issue}: {f.message}\n")
    print(f"Total: {len(findings)} contradiction(s) across checks "
          f"{sorted({f.check for f in findings})}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
