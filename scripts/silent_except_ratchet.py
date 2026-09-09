#!/usr/bin/env python3
"""#5990 (follow-up gate) — a ratchet over `except Exception`/`except
BaseException` blocks that report NOTHING an operator would ever see.

## Why this exists

#5990's own census (backlog-watcher, Q2) found 98 of 221 such blocks under
`src/reyn/interfaces/` with no visible-report marker at all — the SAME
class as the issue's own headline defect (a worker dying, a transport
layer swallowing an error, a queue entry silently discarded), scattered
across the tree by DIFFERENT authors each independently deciding "this one
doesn't need to say anything" — never a single reviewed decision. lead-
coder's own ruling: fixing the 98 is out of THIS gate's scope (a
follow-up issue, not a mechanical PR); what's in scope is making sure a
NEW site of the same shape never lands unnoticed again.

## What counts as "visible" (deliberately narrow, not exhaustive)

A block counts as REPORTED (not flagged) if its body contains, anywhere
(``ast.walk``, so nested `if`/`with`/`for` inside the handler still
counts):

- a call whose attribute name is one of ``info`` / ``warning`` / ``warn``
  / ``error`` / ``exception`` / ``critical`` (``logger.debug`` does
  **not** count — #5990's own finding: a debug-only site cost 61 commits
  of blind bisection, "logged, just not visibly" is the SAME failure as
  not logged at all)
- a call to one of a short, explicit set of known visible-report
  functions this codebase already uses (`reply_error`, `_ingest_frame`,
  `OutboxMessage`, `JSONResponse`, `print`)
- a bare ``raise`` (re-raising lets a caller, or Textual's own
  `exit_on_error` default, take it from there)

Anything else — `pass`, a bare `return`, reassigning a variable and
falling through, `logger.debug(...)` alone — is SILENT.

## The false-reject / false-accept split (BOTH measured, not just one)

A narrow, explicit marker-name set (never a wildcard match on "any
`logger.*` call" or "any function whose name contains 'log'") trades false
accept for false reject, deliberately:

- **False reject** (flags a genuinely-fine site): a visible-report
  mechanism this codebase adds in the FUTURE, under a name not in the set
  above, reads as silent until this script's own set is updated. This is
  the KNOWN cost of a narrow, explicit set — accepted on purpose, because
  the alternative (a broad heuristic match) trades it for false accept,
  which #5990's own finding says costs more (a real silent failure
  missed, not a false alarm on a fine one).
- **False accept** (misses a genuinely silent site): a marker call PRESENT
  in the handler body that does not actually fire on the path that
  matters — inside one branch of an `if` that is never taken, dead code
  after an unconditional `return` above it, or a call that is syntactically
  a marker but carries nothing (`logger.info("")`, an f-string that
  evaluates empty). `ast.walk` only ever answers "does a call to this name
  exist in this body" — it cannot answer "does it actually run" or "does
  it actually communicate." **This is written here as a disclosed limit,
  not solved** — the same "suspected, not confirmed" posture
  `suspected_time_dependence_ratchet.py` (#4846) already established for
  a syntax-only AST gate.

## Deliberate divergence from #4846: fail-CLOSED on parse failure

`suspected_time_dependence_ratchet.py` treats a file it cannot parse as
contributing 0 to either count, silently — reasoned there as "a parse
failure is a DIFFERENT gate's problem" (ruff/CI would already be red).
**This gate does the opposite**: a file under scope that fails to parse
makes THIS SCRIPT exit non-zero, loudly, rather than silently undercounting
the population. Reason, spelled out so a future reader does not "fix" this
into matching #4846's own shape: this gate exists ONLY to stop a silent
failure from landing unnoticed, so its own population-derivation step
silently under-reporting on a parse failure would be the exact same defect
recurring one layer up, inside the gate meant to prevent it.

## Baseline semantics — this is NOT "the 98"

The committed baseline below is whatever THIS script's own AST logic
measures against the current tree, TODAY. It does not attempt to equal,
reconcile with, or explain any difference from #5990's own manually-read
census (98 of 221) — the manual read and this AST walk use structurally
different methods (a human reading intent vs. a name-match over syntax)
and are expected to disagree at the margins. **This baseline REPLACES the
census's 98 as this repo's own tracked number** — a ratchet needs one
committed, machine-derived number to grow against, not a hand-counted one
frozen at read time.

## Scope: `src/reyn/interfaces/` only (v1)

Matches #5990's own Q2 census scope exactly. Whether to widen this to
`core/`/`runtime/`/elsewhere is a separate decision this gate does not
make on its own — no code here assumes the scope will ever grow.

## What this ratchet does NOT attempt (explicitly out of scope, #6009-shaped)

An existing site that already has a marker can, in a LATER refactor, have
that marker call deleted while the bare `except Exception:` stays —
this ratchet's own per-file COUNT would only rise (and get caught) if the
file's total silent-count net increases; a refactor that removes one
marked site's marker while ALSO fixing an unrelated silent site in the
SAME file could theoretically cancel out in the file-level count. No
evidence this has ever actually happened is on record (the same
"population measured as zero, so not built" reasoning architect gave for
#6009's directory-sweep candidate) — flagged here as a disclosed gap,
not engineered around.

CI: gate
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "silent_except_ratchet_baseline.json"
_SCOPE = "src/reyn/interfaces"

# Deliberately narrow and explicit — see module docstring's false-reject /
# false-accept discussion for why this is not a wildcard match.
_VISIBLE_LOG_LEVELS = frozenset({"info", "warning", "warn", "error", "exception", "critical"})
_VISIBLE_MARKER_NAMES = frozenset({
    "reply_error", "_ingest_frame", "OutboxMessage", "JSONResponse", "print",
})


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `src/reyn/interfaces/` — `git
    ls-files`, the same population source `suspected_time_dependence_
    ratchet.py`/`check_tests_path_literal_reference.py` use and for the
    same reason: it already excludes `.venv/`, `__pycache__/`, and
    anything gitignored, with no hand-maintained exclusion list."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _call_func_name(node: ast.Call) -> "str | None":
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _handler_type_names(node: ast.ExceptHandler) -> "set[str]":
    """The exception type name(s) this handler catches — `Exception` /
    `BaseException`, singly or inside a tuple (`except (Exception, X):`).
    Empty for a bare `except:` (not this gate's population — a bare
    except is `except BaseException` in effect, but a DIFFERENT,
    pre-existing lint surface already governs bare excepts; this gate
    only widens what "Exception"/"BaseException" themselves catch)."""
    t = node.type
    if t is None:
        return set()
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, ast.Tuple):
        return {elt.id for elt in t.elts if isinstance(elt, ast.Name)}
    return set()


def _handler_is_silent(node: ast.ExceptHandler) -> bool:
    """True iff nothing in this handler's body (walked recursively) is a
    visible-report call or a re-raise. See module docstring for exactly
    what counts."""
    for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[])):
        if isinstance(stmt, ast.Raise):
            return False
        if isinstance(stmt, ast.Call):
            name = _call_func_name(stmt)
            if name is None:
                continue
            if name in _VISIBLE_LOG_LEVELS or name in _VISIBLE_MARKER_NAMES:
                return False
    return True


def silent_except_count(path: Path) -> int:
    """The number of silent `except Exception`/`except BaseException`
    blocks in *path*.

    Deliberately does NOT return 0 on a parse failure — see module
    docstring's fail-closed rationale. Raises `SyntaxError` (or whatever
    `ast.parse` itself raises) straight through to the caller; `measured`
    lets it propagate to `main`, which is where this gate turns it into a
    loud, non-zero exit."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _handler_type_names(node) & {"Exception", "BaseException"}:
            if _handler_is_silent(node):
                count += 1
    return count


def measured(root: Path = _ROOT) -> "tuple[dict[str, int], int]":
    """`(count_by_file, scanned_file_count)` — only files with a nonzero
    count appear in the dict (the other ratchets' own "absence means
    zero" convention); `scanned_file_count` is used only for the vacuity
    guard, never the pass/fail comparison itself.

    A file that fails to parse is NOT caught here — it propagates up to
    `main`, which is the fail-closed behaviour this module's docstring
    commits to."""
    files = _iter_scan_files(root)
    by_file: "dict[str, int]" = {}
    for path in files:
        count = silent_except_count(path)
        if count:
            by_file[str(path.relative_to(root))] = count
    return (by_file, len(files))


def load_baseline(path: Path = _BASELINE_PATH) -> "dict[str, int]":
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(by_file: "dict[str, int]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(dict(sorted(by_file.items())), indent=2) + "\n", encoding="utf-8")


def grown_files(measured_by_file: "dict[str, int]", baseline_by_file: "dict[str, int]") -> "dict[str, tuple[int, int]]":
    """`{file: (baseline_count, measured_count)}` for every file whose
    measured count exceeds its baseline count — a file absent from the
    baseline is compared against 0. A file whose count DROPPED (or
    vanished) is never reported — the silent-shrink contract every
    ratchet in this repo shares."""
    grown: "dict[str, tuple[int, int]]" = {}
    for file, count in measured_by_file.items():
        base = baseline_by_file.get(file, 0)
        if count > base:
            grown[file] = (base, count)
    return grown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT measured counts "
            "instead of checking against them. Use for initial adoption, "
            "a real reduction you want to lock in, or a deliberate, "
            "reviewed new silent except — say why in the PR body. There "
            "is no other way to silence a new site (no comment-based "
            "exception escape hatch)."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        counts, scanned = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        # #5990 fail-closed divergence from #4846 — see module docstring.
        print(
            f"silent-except ratchet FAILED: could not scan the population "
            f"({exc}). This gate fails CLOSED on a scan error rather than "
            f"silently under-counting — fix the scan (or the file it "
            f"choked on), do not treat this as a pass.",
            file=sys.stderr,
        )
        return 1

    # Vacuity guard, checked BEFORE anything else and independent of
    # --write-baseline: a scan that found 0 files is a scanner failure,
    # not a clean population.
    if scanned == 0:
        print(
            "silent-except ratchet FAILED: the scan found 0 files under "
            f"{_SCOPE}/ — this is a scanner failure, not a clean "
            "population; `git ls-files` returned nothing.",
            file=sys.stderr,
        )
        return 1

    if args.write_baseline:
        write_baseline(counts, _BASELINE_PATH)
        print(
            f"Wrote silent-except counts for {len(counts)} file(s) to "
            f"{_BASELINE_PATH}, {scanned} file(s) scanned under {_SCOPE}/."
        )
        return 0

    baseline = load_baseline(_BASELINE_PATH)
    grown = grown_files(counts, baseline)

    if grown:
        print("silent-except ratchet FAILED:\n", file=sys.stderr)
        print(
            f"{len(grown)} file(s) grew their silent-except count versus "
            f"the baseline ({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for file, (base, count) in sorted(grown.items()):
            print(f"  {file}: {base} -> {count}", file=sys.stderr)
        print(
            "\nA `except Exception`/`except BaseException` block here has "
            "no visible report (no log above debug, no known marker call, "
            "no re-raise) — see scripts/silent_except_ratchet.py's own "
            "module docstring for exactly what counts and why. If this is "
            "a deliberate, reviewed addition, say so in the PR body and "
            "run --write-baseline. This is SUSPECTED, not confirmed — see "
            "the docstring's own false-accept/false-reject disclosure.",
            file=sys.stderr,
        )
        return 1

    total = sum(counts.values())
    print(
        f"silent-except ratchet OK: {total} silent site(s) across "
        f"{len(counts)} file(s), all baselined ({scanned} file(s) scanned "
        f"under {_SCOPE}/). Suspected, not confirmed — see module "
        "docstring for what this does and does not catch."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
