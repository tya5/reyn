#!/usr/bin/env python3
"""Detect PR-body / GitHub-parser contradictions about issue-closing intent.

INVARIANT: the intent a PR body *declares* about issue #N must match the
closing behavior GitHub's own parser (``closingIssuesReferences``, readable
via ``gh pr view <N> --json closingIssuesReferences,body`` while the PR is
still open) actually resolved for #N. This script detects contradictions —
it never infers intent beyond what the body's declaring phrases literally
say.

Three checks, all facets of the same invariant:

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
     example: #3003→#2827.
  3. **undeclared** — N IS in ``closingIssuesReferences`` but the body
     contains NO declaration at all (neither closing nor non-closing) about
     N → GitHub's parser silently picked up a closing reference from prose
     the author never flagged as intentional (e.g. "auto-close #N" in a
     sentence). This is the hole a closing-keyword-only check (1+2) misses:
     both checks 1 and 2 presuppose the author wrote *some* declaring
     phrase; an author who writes neither slips through both.

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

(#3003 is *not* evidence about backticks: its body's backticked
`` `Closes` `` has no adjacent issue number and could not have closed
anything. What GitHub actually parsed there is the bare ``close #2827``
substring inside the prose "auto-close #2827" — i.e. #3003 is the
bare-prose-keyword case, which is what check 3 covers.)

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


def _strip_backticks(text: str) -> str:
    """Remove backtick characters so fenced/defused keywords still match.

    GitHub's parser **does** respect backticks — a body containing
    `` `Closes #N` `` does NOT auto-close N on merge (verified: #2990 and
    #3006 both fence their closing keyword and both have an empty
    ``closingIssuesReferences``; #2620 and #2972 consequently stayed open).

    This matcher is deliberately stricter: a fenced ``Closes #N`` is still
    the author declaring intent to close N, so we must see the declaration
    that GitHub's parser ignored — that mismatch IS the check-1 defect.
    Hence: strip the fence characters, don't skip fenced spans.
    """
    return text.replace("`", "")


def find_closing_declarations(body: str) -> set[int]:
    """Return the set of issue numbers the body declares closing-intent for."""
    text = _strip_backticks(body)
    return {int(m.group(1)) for m in _CLOSING_RE.finditer(text)}


def find_nonclosing_declarations(body: str) -> set[int]:
    """Return the set of issue numbers the body declares non-closing-intent for."""
    text = _strip_backticks(body)
    return {int(m.group(1)) for m in _NONCLOSING_RE.finditer(text)}


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


@dataclass
class Finding:
    check: int
    issue: int
    message: str


def check_contradictions(body: str, closing_refs: list[int]) -> list[Finding]:
    """Pure contradiction detector — no network, no inference of intent.

    ``body`` is the raw PR body text. ``closing_refs`` is the list of issue
    numbers GitHub's parser (``closingIssuesReferences``) actually resolved
    as closing targets for this PR.
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
    for n in sorted((nonclosing_declared | discussing_declared) & closing_refs_set):
        form = (
            "a closing-check 'discussing' marker"
            if n in discussing_declared and n not in nonclosing_declared
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

    return findings


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def fetch_pr_data(pr_number: int) -> tuple[str, list[int]]:
    """Fetch (body, closing_issue_numbers) for an open PR via ``gh pr view``."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "closingIssuesReferences,body",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    body = data.get("body") or ""
    closing_refs = [ref["number"] for ref in data.get("closingIssuesReferences") or []]
    return body, closing_refs


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
            "Path to a JSON fixture file with keys 'body' (str) and "
            "'closingIssuesReferences' (list of {'number': N} or plain ints) "
            "— same shape as `gh pr view --json closingIssuesReferences,body`. "
            "Lets this check run offline / in tests without hitting GitHub."
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pr is not None:
        try:
            body, closing_refs = fetch_pr_data(args.pr)
        except subprocess.CalledProcessError as exc:
            print(f"gh pr view failed: {exc.stderr}", file=sys.stderr)
            return 2
        source = f"PR #{args.pr}"
    else:
        from pathlib import Path

        raw = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        body = raw.get("body") or ""
        closing_refs = _closing_refs_from_fixture(raw.get("closingIssuesReferences"))
        source = args.fixture

    findings = check_contradictions(body, closing_refs)

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
