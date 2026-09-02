#!/usr/bin/env python3
"""#5499/#5519 — flag an issue with `needs-axis` the moment it carries no
priority-axis label, and un-flag it the moment it gains one.

## Why this exists (owner + lead-coder measurement, #5497/#5499)

`priority:next` has 0 open issues; `roi:` last moved on #5197 (2026-08-23);
every issue filed on 2026-08-29 carries zero axis labels. The only inline
label CLAUDE.md carries (`blocked:external`) was used correctly all night —
`docs/deep-dives/contributing/issue-management.md`'s own diagnosis: **the
rule's location decides whether it is followed, not the reader's
discipline** ("軸ラベルの存在を忘れているとき、私はそれについて疑っていま
せん" — lead-coder). A "read this when in doubt" rule cannot fire when the
failure mode is forgetting the rule exists at all. This closes the gap the
way CLAUDE.md's own hard rule prescribes: "If CI can catch the violation,
write the gate, not a rule here."

## What counts as an "axis" label — a description marker, not a hand list

#5499's own first landing tried deriving the vocabulary straight from each
label's `description` and rejected it by direct measurement: of the 9 axis
labels (`band`, `owner-hit`, `silent`, `blocks-others`, `ours-only`,
`thin:retrieval`, `thin:evaluation`, `priority:next`, `no-axis`), only 3
even contained the word "軸" anywhere in their description — no scan of the
EXISTING text could recover the set, so #5499 fell back to a small
hand-written pattern in this script instead (prefix families plus a
hand-enumerated exact-name set), which #5519 itself named as the disclosed
limitation: "a new, unprefixed axis is created" still requires editing this
file, and a renamed/deleted exact-name label silently narrows the gate's
coverage until someone notices the RED.

#5519 (architect ruling via lead-coder, 2026-08-30) closes that gap for
real: every axis label's `description` on GitHub now ENDS with the literal
marker :data:`AXIS_DESCRIPTION_MARKER` (`"[axis]"`) — a small, one-time
repo-label edit (owner/lead territory, done alongside this PR). The label
DECLARES its own axis-hood; this script only has to recognise the marker
(:func:`label_declares_axis`, a pure string check with no vocabulary of
label names at all), then intersect that against the live repo label list
(:func:`resolve_axis_vocabulary`) exactly the way #5499 always did. A new
axis label picked up automatically the moment its own description carries
the marker — no script edit, ever, for any future axis (prefixed or not).
`no-axis` (the explicit "judged, none applies" opt-out) carries the same
marker: an issue carrying it has been triaged and must NOT be flagged,
the same as any other axis label.

If the LIVE-matched set is EMPTY (every marked axis label vanished from the
repo, or the marker itself was stripped from every description — #5482's
own "a scanned population must never legitimately reach 0" shape), that is
RED, not silently "nothing to check". A separate test
(`test_5499_axis_label_gate.py`) pins :func:`label_declares_axis` itself
against marked/unmarked description strings, purely (no network) — the
live reconciliation (:func:`resolve_axis_vocabulary` against a REAL `gh
api` label list) is exercised only by this script at gate-run time, per
lead-coder's own caution that a label-description-dependent test would be
network-flaky in CI if it tried to hit GitHub directly.

## Both directions, by design (accept criterion, architect + lead-coder)

A gate that only ever ADDS `needs-axis` and never removes it is the same
shape as #5517's own incident (a predicate nobody could falsify in the
direction that matters) — "always flag, never unflag" passes every smoke
test while doing nothing useful once an issue IS triaged. This module's
:func:`compute_label_action` is symmetric: MISSING → add, PRESENT →
remove, and a `needs-axis`-carrying issue that already has no other axis
label is a no-op (idempotent — the workflow may fire on `opened`,
`labeled`, AND `unlabeled`, and must not toggle-loop).

## Scope: never closes, never blocks

`docs/deep-dives/contributing/issue-management.md`'s own explicit design
constraint, reused here: an issue is a discussion vessel, not something
this gate is allowed to stop. `needs-axis` only ever makes an existing gap
VISIBLE (the same reason `blocked:external` works — "見えるから" — being
seen is the entire mechanism), never closes or blocks the issue itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

# #5519: the ONE marker every axis label's description ends with — see
# module docstring. Kept as a single named constant, same landing shape
# #5499's own AXIS_LABEL_VOCABULARY took (lead-coder: "語彙は script 内に
# 散らさず1つの名前付き定数に"), even though this constant is now a
# single string, not a name list — the label descriptions on GitHub are
# the vocabulary now, not this file.
AXIS_DESCRIPTION_MARKER = "[axis]"

NEEDS_AXIS_LABEL = "needs-axis"


def label_declares_axis(description: "str | None") -> bool:
    """Pure — no I/O. True iff *description* ends with
    :data:`AXIS_DESCRIPTION_MARKER` (trailing whitespace ignored, so a
    stray trailing space on GitHub's own side does not silently un-mark a
    label). A label whose description is missing/blank, or that mentions
    "軸"/"axis" in prose without the structured marker, does NOT count —
    this is the one thing that makes the vocabulary machine-checkable
    instead of prose-checkable (#5519's own reason for existing)."""
    return (description or "").rstrip().endswith(AXIS_DESCRIPTION_MARKER)


@dataclass(frozen=True)
class AxisVocabulary:
    """The live-reconciled result of scanning the repo's actual labels for
    :func:`label_declares_axis` — see module docstring. ``ok`` is false
    when the matched set is empty: #5482's own "a scanned population must
    never legitimately reach 0" shape, now covering both "every marked
    label was deleted" and "the marker was stripped from every
    description" in one check, since both look identical from here (a
    label declares axis-hood or it does not; there is no separate
    hand-list side to go stale)."""

    matched: "frozenset[str]"  # live label names whose description is marked

    @property
    def ok(self) -> bool:
        return bool(self.matched)


def resolve_axis_vocabulary(live_labels: "list[dict]") -> AxisVocabulary:
    """Pure — no I/O. *live_labels* is the repo's label list, each entry
    carrying at least ``name`` and ``description`` (the shape ``gh api
    repos/<repo>/labels`` returns). No hand-written name list is
    consulted at all — every live label is scanned on its own
    description."""
    matched = frozenset(
        entry["name"] for entry in live_labels if label_declares_axis(entry.get("description"))
    )
    return AxisVocabulary(matched=matched)


def compute_label_action(
    issue_label_names: "list[str]", vocabulary: "frozenset[str]"
) -> "str | None":
    """Symmetric decision — see module docstring for why both directions
    are required, not just "add". Returns ``"add"``, ``"remove"``, or
    ``None`` (no-op, already correct)."""
    has_axis = any(name in vocabulary for name in issue_label_names)
    has_needs_axis = NEEDS_AXIS_LABEL in issue_label_names
    if has_axis:
        return "remove" if has_needs_axis else None
    return None if has_needs_axis else "add"


def format_needs_axis_comment(vocabulary: "frozenset[str]") -> str:
    """The vocabulary actually consulted THIS run, named in the comment
    (architect condition ②) — so a false positive from a label whose
    description is not (yet) marked reads as self-explaining, not silent
    ("貼られた人がその場で一覧の古さに気づける")."""
    listed = ", ".join(f"`{name}`" for name in sorted(vocabulary))
    return (
        f"No priority-axis label found on this issue. Add one of the "
        f"axis labels below (or `no-axis` if none applies — "
        f"`docs/deep-dives/contributing/issue-management.md` §5), and "
        f"this `{NEEDS_AXIS_LABEL}` label will be removed automatically.\n\n"
        f"Vocabulary checked this run: {listed}\n\n"
        f"(A label counts as an axis label when its own GitHub description "
        f"ends with `{AXIS_DESCRIPTION_MARKER}` — if yours should be here "
        f"and isn't, its description is missing that marker; see #5519.)"
    )


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _fetch_repo_labels(repo: str) -> "list[dict]":
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/labels", "--paginate"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _fetch_issue_label_names(repo: str, issue_number: str) -> "list[str]":
    result = subprocess.run(
        ["gh", "issue", "view", issue_number, "--repo", repo, "--json", "labels"],
        capture_output=True, text=True, check=True,
    )
    return [entry["name"] for entry in json.loads(result.stdout)["labels"]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add/remove needs-axis on an issue based on its current labels.",
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--issue", required=True, metavar="NUMBER")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    live_labels = _fetch_repo_labels(args.repo)
    vocabulary = resolve_axis_vocabulary(live_labels)
    if not vocabulary.ok:
        print(
            "RED: no live repo label's description carries the "
            f"{AXIS_DESCRIPTION_MARKER!r} marker — every axis label has "
            "either been deleted or had its marker stripped. Refusing to "
            "silently treat every issue as axis-labeled. See #5519.",
            file=sys.stderr,
        )
        return 1

    issue_labels = _fetch_issue_label_names(args.repo, args.issue)
    action = compute_label_action(issue_labels, vocabulary.matched)

    if action == "add":
        subprocess.run(
            ["gh", "issue", "edit", args.issue, "--repo", args.repo,
             "--add-label", NEEDS_AXIS_LABEL],
            check=True,
        )
        subprocess.run(
            ["gh", "issue", "comment", args.issue, "--repo", args.repo,
             "--body", format_needs_axis_comment(vocabulary.matched)],
            check=True,
        )
        print(f"Added {NEEDS_AXIS_LABEL} to #{args.issue}.")
    elif action == "remove":
        subprocess.run(
            ["gh", "issue", "edit", args.issue, "--repo", args.repo,
             "--remove-label", NEEDS_AXIS_LABEL],
            check=True,
        )
        print(f"Removed {NEEDS_AXIS_LABEL} from #{args.issue}.")
    else:
        print(f"No action needed for #{args.issue}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
