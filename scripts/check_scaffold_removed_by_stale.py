#!/usr/bin/env python3
"""#6209 — a `tests/scaffold/` file whose own `removed_by` condition is
already satisfied must be caught by CI, not found by accident.

## The incident this closes

`test_6184_2b1_compose_truncate_split.py` (it lived under
`tests/scaffold/`; #6207/#6210 removed it — not a path this docstring
names literally, so `check_tests_path_literal_reference.py`'s own
ratchet does not treat a gone file's mention as a fresh dangling
reference) had a header comment (real, `origin/main` `2256bd972`,
before that removal) naming its own removal condition
machine-readably:

    # scaffold: removed_by="#6184 段2b-2/2b-3 lands (the actual
    #     producer/consumer move) -- ..."

段2b-2/2b-3 had already landed (#6184 itself later closed too) by the
time anyone noticed — the file sat there, its own condition satisfied,
until a REVIEW OF AN UNRELATED ISSUE (#6205) happened to surface it.
CLAUDE.md's own rule: "If CI can catch the violation, write the gate,
not a rule here" — this gate is that.

## The rule — closed-number references ONLY, never `triggered_by`

`triggered_by` is NEVER inspected. `tests/scaffold/test_5620_litellm_
proxy_defects.py` is the reason: its own `triggered_by="#5620 -- ..."`
names issue #5620, which IS closed — but that file's own `removed_by`
condition is an UPSTREAM fact ("the litellm 1.95.0 proxy defects this
file reproduces are fixed upstream"), unrelated to #5620's own open/
closed state. A "triggered_by closed -> red" rule would false-positive
on this file the moment it landed; this gate would report a defect
that does not exist.

**Rule**: a `removed_by` string that NAMES one or more issue/PR numbers
(`#NNNN`), where EVERY named number is closed or merged, is stale —
red. A `removed_by` string that names ZERO numbers (an upstream/
external-event condition, in prose) is NEVER flagged, regardless of
its content — this gate cannot judge natural-language conditions, only
number-shaped ones (see this module's own "Scope" section below).

Population today (`origin/main`, #6207/#6210 already landed — the file
above is GONE): exactly 1 scaffold file, `test_5620_litellm_proxy_
defects.py`, whose `removed_by` names no numbers -> 0 offenders. The
`test_6184_2b1_...` shape is proven ONLY via a `tmp_path` fixture in
this gate's own test file (real-tree dependence here would go quietly
vacuous the moment that file was removed — which already happened).

## Scope — what this gate does NOT close

This gate closes ONLY the number-referencing shape. A `removed_by`
string phrased entirely in prose with no `#NNNN` (an upstream fact, an
external event, a design decision not tracked by an issue number) is
OUT OF SCOPE — this script cannot judge whether such a condition is
now true; only a human re-reading the file's own docstring can. Do not
read a green run here as "scaffold cleanup is automated" — it is not.

CI: gate
"""
from __future__ import annotations

from verify_env_identity import guard_bare_script_or_exit

guard_bare_script_or_exit()

import re
import subprocess
import sys
from pathlib import Path

_REMOVED_BY_RE = re.compile(r'#\s*scaffold:\s*removed_by\s*=\s*"([^"]*)"')
_NUMBER_RE = re.compile(r"#(\d+)")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAFFOLD_DIR = _REPO_ROOT / "tests" / "scaffold"


def extract_removed_by(file_text: str) -> "str | None":
    """The `removed_by` string from a scaffold file's own header comment
    (first match only — one declaration per file, matching the README's
    own documented convention), or ``None`` if the file carries none."""
    match = _REMOVED_BY_RE.search(file_text)
    return match.group(1) if match else None


def referenced_numbers(removed_by: str) -> "list[int]":
    """Every `#NNNN`-shaped issue/PR number the `removed_by` string
    names, deduplicated and sorted — empty for a pure-prose condition
    (the `test_5620_...` shape, never flagged by this gate)."""
    return sorted({int(n) for n in _NUMBER_RE.findall(removed_by)})


def find_stale_scaffold_files(
    scaffold_dir: Path,
    is_closed,
) -> "list[tuple[Path, str, list[int]]]":
    """``(file, removed_by, numbers)`` for every scaffold file whose
    ``removed_by`` names >=1 issue/PR number and ALL of them are
    closed/merged. ``is_closed`` is injectable (a real one calls `gh`;
    a test passes a small in-memory lookup) so this function itself
    never touches the network — see this module's own docstring for
    why the real population is proven via a fixture, not the real
    tree."""
    offenders: "list[tuple[Path, str, list[int]]]" = []
    for path in sorted(scaffold_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        removed_by = extract_removed_by(text)
        if removed_by is None:
            continue
        numbers = referenced_numbers(removed_by)
        if not numbers:
            continue
        if all(is_closed(n) for n in numbers):
            offenders.append((path, removed_by, numbers))
    return offenders


def _real_is_closed(number: int) -> bool:
    """Real GitHub state for one issue/PR number — the unified `issues`
    REST endpoint answers for both (a merged PR reads `state: closed`
    the same as a closed-without-merge one; `removed_by` never
    distinguishes the two, so neither does this). `:owner/:repo`
    resolves from the checkout's own git remote (same `gh` convenience
    `gh pr view`'s own bare form relies on, see
    `check_pr_closing_intent.py`'s own `fetch_pr_data`) — no hardcoded
    repo slug to go stale on a fork. `cwd=_REPO_ROOT` explicit: this
    module's own test file is exercised under `tests/conftest.py`'s
    autouse per-test `chdir(tmp_path)` isolation, where `:owner/:repo`
    cannot resolve a git remote at all — pinning the repo root here
    makes the SAME call correct in both that harness and a real CI
    invocation (which already runs from the repo root, so this is a
    no-op there, not a behavior change)."""
    result = subprocess.run(
        ["gh", "api", f"repos/:owner/:repo/issues/{number}", "--jq", ".state"],
        capture_output=True, text=True, check=True, cwd=_REPO_ROOT,
    )
    return result.stdout.strip() == "closed"


def main(argv: "list[str] | None" = None) -> int:
    del argv
    offenders = find_stale_scaffold_files(_SCAFFOLD_DIR, _real_is_closed)

    if not offenders:
        print(
            "OK: no tests/scaffold/ file's removed_by condition (issue/PR "
            "numbers it names) is fully satisfied."
        )
        return 0

    print("scaffold-removed-by-stale gate FAILED:\n", file=sys.stderr)
    for path, removed_by, numbers in offenders:
        # Relative to the repo root when the file genuinely lives there;
        # printed as-is otherwise (a test's own tmp_path fixture is
        # outside _REPO_ROOT, and relative_to() would raise — the
        # message's job is to name the file, not to assume its home).
        try:
            shown = path.relative_to(_REPO_ROOT)
        except ValueError:
            shown = path
        print(
            f"  {shown}: removed_by names {numbers} (ALL closed/merged) "
            f"-- {removed_by!r}",
            file=sys.stderr,
        )
    print(
        "\nEach file above named its own removal condition and that "
        "condition is now met — remove the file (CLAUDE.md testing "
        "policy Annex: a scaffold test's whole point is bounded life). "
        "This gate closes ONLY number-referencing removed_by strings; "
        "a prose/upstream condition is never flagged here (see this "
        "script's own module docstring, 'Scope').",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
