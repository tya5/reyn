#!/usr/bin/env python3
"""#5499 — flag an issue with `needs-axis` the moment it carries no
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

## What counts as an "axis" label — measured, not assumed

The obvious design ("derive the vocabulary from each label's own
`description`, no hand list at all") was tried first and REJECTED by
direct measurement: of the 8 axis labels
`docs/deep-dives/contributing/issue-management.md` §4/§5 names (`band`,
`owner-hit`, `silent`, `blocks-others`, `ours-only`, `thin:retrieval`,
`thin:evaluation`, `priority:next`) plus the `no-axis` opt-out, only 3
(`no-axis`, `ours-only`, `priority:next`) even contain the word "軸"
anywhere in their description — `band`/`owner-hit`/`silent`/
`blocks-others`/`thin:retrieval`/`thin:evaluation` do not, so no
description-text scan can recover the set. Filed as #5519: a common
description marker (e.g. a leading `axis:`) would close this gap for
real, but editing repo labels is owner/lead territory, out of scope here.

The fallback (architect ruling, #5499, 2026-08-29): a small PATTERN lives
in this script, in ONE named constant (:data:`AXIS_LABEL_VOCABULARY` —
lead-coder's own follow-up correction: "語彙は script 内に散らさず1つの
名前付き定数に", the same landing shape #5517's own
``QUERIED_CAPABILITY_FIELDS_BY_MODALITY`` took) — not a hand-enumerated
list of every current axis label value, but the handful of PREFIX
FAMILIES (`priority:`, `roi:`, `thin:`) plus standalone names that
#5497's own investigation already measured (`git grep "priority:next|
roi:|owner-hit|ours-only|no-axis|thin:retrieval"`). A new `roi:*` /
`thin:*` / `priority:*` value is picked up automatically; the exact-name
set only grows when a genuinely new, unprefixed axis is created (rare,
and each such PR already touches this file's own docstring by
construction). Label COLOR was also tried and rejected (lead-coder's own
measurement): `thin:retrieval` and `thin:evaluation` share the identical
color `5319E7`, so color carries no more structure than description does.

**The vocabulary actually used every run is the INTERSECTION of this
pattern against the LIVE repo label list** (:func:`resolve_axis_vocabulary`)
— never trusted blind. BOTH directions of that intersection are checked
(lead-coder's #5499 correction to an earlier one-directional draft — "誰
かがラベルを1つ消した日に gate が黙って狭まり…緑のままになります", the
same "silently narrows, stays green" shape #5517 was just fixed for):

  1. live label matched a prefix → included in ``matched`` by
     construction (every live label is scanned against the pattern, so
     there is no live-side gap to separately detect).
  2. pattern-declared exact name → must still exist live. If ANY exact
     name has vanished (a rename/delete), :attr:`AxisVocabulary.ok` is
     False and the workflow fails RED — not a silent note, since a
     silent note is exactly the hole this correction closes.

If the LIVE-matched intersection is additionally EMPTY (every named axis
label vanished from the repo — #5482's own "a scanned population must
never legitimately reach 0" shape), that is also RED. A separate test
(`test_5499_axis_label_gate.py`) asserts :data:`AXIS_LABEL_VOCABULARY`
itself is non-empty independent of any live reconciliation — lead-coder's
#5499 condition ②, PR-body vacuity disclosure "merge 後 誰にも届きません".

## Both directions, by design (accept criterion, architect + lead-coder)

A gate that only ever ADDS `needs-axis` and never removes it is the same
shape as tonight's #5517 incident (a predicate nobody could falsify in
the direction that matters) — "always flag, never unflag" passes every
smoke test while doing nothing useful once an issue IS triaged. This
module's :func:`compute_label_action` is symmetric: MISSING → add,
PRESENT → remove, and a `needs-axis`-carrying issue that already has no
other axis label is a no-op (idempotent — the workflow may fire on
`opened`, `labeled`, AND `unlabeled`, and must not toggle-loop).

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


@dataclass(frozen=True)
class _AxisLabelPattern:
    """Single named constant carrying the whole vocabulary definition
    (lead-coder ruling, #5499, 2026-08-29: "語彙は script 内に散らさず1つ
    の名前付き定数に" — the same landing shape #5517's
    ``QUERIED_CAPABILITY_FIELDS_BY_MODALITY`` took). Neither field is a
    structural derivation — both description text (only 3/9 axis labels
    even contain "軸") and label color (``thin:retrieval`` and
    ``thin:evaluation`` share the identical color ``5319E7``, measured by
    lead-coder) were tried and rejected. The hand-written ``exact`` set is
    a disclosed limitation, not a design choice — filed as #5519."""

    # Prefix families: any live label starting with one of these counts
    # as an axis label, whatever value follows the colon (`roi:high`,
    # `thin:retrieval`, a future `priority:blocked`, ...) — no hand-
    # enumeration of every value in a family.
    prefixes: "tuple[str, ...]"
    # Exact, unprefixed axis-label names — #5497's own measured set
    # (`git grep "priority:next|roi:|owner-hit|ours-only|no-axis|thin:
    # retrieval"`), minus the ones already covered by a prefix above.
    # `no-axis` is included: it is the explicit "judged, none applies"
    # marker — an issue carrying it has been triaged and must NOT be
    # flagged.
    exact: "frozenset[str]"

    def matches(self, label_name: str) -> bool:
        if label_name in self.exact:
            return True
        return any(label_name.startswith(prefix) for prefix in self.prefixes)


AXIS_LABEL_VOCABULARY = _AxisLabelPattern(
    prefixes=("priority:", "roi:", "thin:"),
    exact=frozenset(
        {"band", "owner-hit", "silent", "blocks-others", "ours-only", "no-axis"}
    ),
)

NEEDS_AXIS_LABEL = "needs-axis"


@dataclass(frozen=True)
class AxisVocabulary:
    """The live-reconciled result of matching :data:`AXIS_LABEL_VOCABULARY`
    against the repo's actual label list — see module docstring. ``ok`` is
    false whenever EITHER direction of the reconciliation found a gap —
    lead-coder's #5499 correction: reporting a vanished exact name without
    failing the job is the exact "gate silently narrows, stays green"
    shape #5517 was just fixed for. Both directions are checked:
    live-label-matched-a-prefix → always included in ``matched`` (by
    construction — every live label is scanned against the pattern, so
    there is no live-side gap to separately detect); pattern-exact-name
    → must still exist live (``vanished_exact_names``, checked here)."""

    matched: "frozenset[str]"  # live labels that satisfy the pattern
    vanished_exact_names: "tuple[str, ...]"  # exact names with no live match

    @property
    def ok(self) -> bool:
        return bool(self.matched) and not self.vanished_exact_names


def resolve_axis_vocabulary(live_label_names: "list[str]") -> AxisVocabulary:
    """Pure — no I/O. Intersects :data:`AXIS_LABEL_VOCABULARY` against a
    live label name list."""
    live = set(live_label_names)
    matched = frozenset(name for name in live if AXIS_LABEL_VOCABULARY.matches(name))
    vanished = tuple(
        sorted(name for name in AXIS_LABEL_VOCABULARY.exact if name not in live)
    )
    return AxisVocabulary(matched=matched, vanished_exact_names=vanished)


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
    (architect condition ②) — so a false positive from a not-yet-
    pattern-matched new axis label reads as self-explaining, not silent
    ("貼られた人がその場で一覧の古さに気づける")."""
    listed = ", ".join(f"`{name}`" for name in sorted(vocabulary))
    return (
        f"No priority-axis label found on this issue. Add one of the "
        f"axis labels below (or `no-axis` if none applies — "
        f"`docs/deep-dives/contributing/issue-management.md` §5), and "
        f"this `{NEEDS_AXIS_LABEL}` label will be removed automatically.\n\n"
        f"Vocabulary checked this run: {listed}\n\n"
        f"(If your label isn't in that list, the vocabulary may be "
        f"stale — see #5519.)"
    )


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _fetch_repo_label_names(repo: str) -> "list[str]":
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/labels", "--paginate"],
        capture_output=True, text=True, check=True,
    )
    return [entry["name"] for entry in json.loads(result.stdout)]


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

    live_labels = _fetch_repo_label_names(args.repo)
    vocabulary = resolve_axis_vocabulary(live_labels)
    if vocabulary.vanished_exact_names:
        print(
            "RED: axis-label names in this script's own vocabulary no "
            f"longer exist in the repo's live label list: "
            f"{', '.join(vocabulary.vanished_exact_names)} — the gate's "
            "coverage silently narrowed. Update AXIS_LABEL_VOCABULARY in "
            "scripts/axis_label_gate.py (see #5519).",
            file=sys.stderr,
        )
    if not vocabulary.matched:
        print(
            "RED: the axis-label vocabulary matched ZERO live repo "
            "labels — every named axis label has vanished. Refusing to "
            "silently treat every issue as axis-labeled.",
            file=sys.stderr,
        )
    if not vocabulary.ok:
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
