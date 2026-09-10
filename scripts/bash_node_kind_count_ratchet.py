#!/usr/bin/env python3
"""#5987 stage 1 — ratchets the COUNT of ``tree-sitter-bash`` grammar
node-kind names :func:`reyn.security.bash_node_kinds.derive_bash_node_kind_names`
derives from the INSTALLED package.

This is a ratchet on a THIRD-PARTY dependency's compiled grammar, not an
algorithm-level pin on reyn's own code (CLAUDE.md's testing policy forbids
the latter, not the former) — a red here means either the ``tree-sitter-bash``
dependency changed (bump the committed baseline DELIBERATELY, in the same PR
that bumps the pin) or ``bash_node_kinds.py``'s own derivation logic broke
(do NOT update the baseline for that; fix the derivation instead). See
``src/reyn/security/bash_node_kinds.py``'s own module docstring for why this
population is machine-derived rather than hand-enumerated, and why it
currently measures 236 (not the 184 a pre-ruling research pass cited from a
file the installed package does not actually ship).

Same skeleton as ``flat_tests_ratchet.py``/``mypy_ratchet.py``: a committed
baseline (here, a single integer, not a name set — the population itself is
a `frozenset[str]` of real grammar node-kind names, but what this gate
ratchets is its SIZE, since the actual names are already witnessed directly
by ``tests/security/test_5987_bash_node_kinds_stage1.py``'s own spot-check
test, not duplicated here as a second, larger hand-copied list —
lead-coder's explicit instruction against hand-copying the population this
module derives). ``--write-baseline`` regenerates it from the CURRENTLY
measured count — use only for initial adoption or a deliberate dependency
bump, never to silence an unexplained drift.

CI: gate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "bash_node_kind_count_baseline.json"


def measured_count() -> int:
    """The REAL, current node-kind count, derived at call time from the
    installed ``tree-sitter-bash`` package via
    :func:`reyn.security.bash_node_kinds.derive_bash_node_kind_names` —
    never a hand-typed number. Imported lazily inside the function (not at
    module scope) so ``--help``/argument-parsing errors do not require the
    dependency to already be importable."""
    from reyn.security.bash_node_kinds import derive_bash_node_kind_names

    return len(derive_bash_node_kind_names())


def load_baseline(path: Path = _BASELINE_PATH) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    return int(data["node_kind_count"])


def write_baseline(count: int, path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps({"node_kind_count": count}, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENTLY measured node-kind "
            "count instead of checking against it. Use ONLY for initial "
            "adoption or a deliberate tree-sitter-bash dependency bump — "
            "regenerating to silence an unexplained drift defeats the "
            "ratchet (see module docstring)."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    measured = measured_count()

    if args.write_baseline:
        write_baseline(measured, _BASELINE_PATH)
        print(f"Wrote node_kind_count={measured} to {_BASELINE_PATH}")
        return 0

    baseline = load_baseline(_BASELINE_PATH)

    if measured != baseline:
        print("bash-node-kind-count ratchet FAILED:\n", file=sys.stderr)
        print(
            f"measured {measured} node kind(s) from the installed "
            f"tree-sitter-bash package, but the committed baseline "
            f"({_BASELINE_PATH.relative_to(_ROOT)}) says {baseline}.",
            file=sys.stderr,
        )
        print(
            "\nIf tree-sitter-bash was deliberately bumped and the grammar "
            "genuinely changed: re-run with --write-baseline and commit the "
            "new baseline IN THE SAME PR as the dependency bump.\n"
            "If nothing in pyproject.toml/the lockfile changed: this "
            "module's OWN derivation logic broke — do not update the "
            "baseline to paper over that, fix bash_node_kinds.py instead.",
            file=sys.stderr,
        )
        return 1

    print(f"bash-node-kind-count ratchet OK: {measured} node kind(s), matches baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
