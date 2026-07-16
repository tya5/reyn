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
     close N but GitHub's parser did not pick it up (wrong keyword form,
     wrong issue number, etc). Real examples: #2990→#2620, #3006→#2972.
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
as `` `Closes #N` `` — GitHub's real closing-keyword parser does not respect
backticks for auto-closing either (hence the #3003 near-miss risk), so this
script strips backtick characters from the body before matching rather than
skipping fenced code spans.

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


def _strip_backticks(text: str) -> str:
    """Remove backtick characters so fenced/defused keywords still match.

    GitHub's own closing-keyword parser does not respect backticks for
    auto-closing (a PR body containing `` `Closes #N` `` still auto-closes
    N on merge) — this script's own regex must therefore see through
    backticks the same way, rather than skipping fenced code spans.
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
    closing_refs_set = set(closing_refs)
    findings: list[Finding] = []

    # Check 1 (false negative): declared closing but parser did not close.
    for n in sorted(closing_declared - closing_refs_set):
        findings.append(
            Finding(
                check=1,
                issue=n,
                message=(
                    f"body declares closing intent for #{n} (Closes/Fixes/"
                    f"Resolves) but GitHub's parser did NOT resolve #{n} as "
                    "a closing reference — merge will NOT close it. Check the "
                    "keyword form and issue number are exactly what GitHub's "
                    "parser expects."
                ),
            )
        )

    # Check 2 (false positive): declared non-closing but parser will close.
    for n in sorted(nonclosing_declared & closing_refs_set):
        findings.append(
            Finding(
                check=2,
                issue=n,
                message=(
                    f"body declares non-closing intent for #{n} (part of/"
                    f"toward) but GitHub's parser WILL close #{n} on merge — "
                    "the declared intent and the parsed behavior contradict. "
                    f"Rewrite the reference to #{n} so it isn't a closing "
                    f"keyword, or if closing #{n} is actually intended, use "
                    "Closes/Fixes/Resolves instead."
                ),
            )
        )

    # Check 3 (undeclared): parser will close but body says nothing about N.
    declared_any = closing_declared | nonclosing_declared
    for n in sorted(closing_refs_set - declared_any):
        findings.append(
            Finding(
                check=3,
                issue=n,
                message=(
                    f"#{n} will be closed on merge (GitHub's parser resolved "
                    f"it via closingIssuesReferences) but the body contains "
                    f"no declaration at all about #{n} (no Closes/Fixes/"
                    f"Resolves, no part of/toward). GitHub's parser likely "
                    f"picked up a bare keyword in prose (e.g. 'auto-close "
                    f"#{n}'). If closing #{n} is intentional, write "
                    f"'Closes #{n}' explicitly; if not, rephrase so the "
                    f"reference to #{n} doesn't read as a closing keyword."
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
