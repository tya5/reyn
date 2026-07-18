#!/usr/bin/env python3
"""Fail when the environment would run a different ``reyn`` than the tree under test.

An observation does not name its own referent. ``import reyn`` succeeding says
nothing about *which checkout* answered; ``execvp() failed`` and
``McpError: Connection closed`` do not distinguish "the feature is broken" from
"this venv has no such console script". #3024 collected eight measurement
failures in one day that all reduce to one sentence: **the thing measured was
not the thing that runs.** Two of them reached a co-vet verdict — the merge gate.

This module answers, mechanically, the question a reader of those errors has to
answer by hand today: *does this environment resolve the checkout I mean?* It
reports each way the answer can be "no" as a **separately named** finding,
because collapsing distinct referents into one word is the failure being closed
(the #3023 post-mortem, verbatim: "three different things — flake / main-red /
venv-console-script-absent — collapsed into one word").

Two properties are load-bearing:

**It never imports reyn.** ``reyn``'s own resolution is the subject under test;
an importer would be asserting with the very mechanism whose trustworthiness is
in question. Checks run reyn out-of-process and compare *paths*, and the module
stays stdlib-only (tomllib / subprocess / shutil), mirroring
``scripts/test_tier_audit.py`` and ``scripts/verify_module_docstrings.py``.

**It measures rather than infers.** Import resolution is not re-implemented from
``.pth`` files and ``sys.path`` rules — a subprocess is spawned and asked where
``reyn`` actually came from. ``CHECKS`` below is the enumeration; each entry
carries the remedy for its own finding, so the gate that fires is the thing that
explains itself.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# What a `mcp.client.stdio`-spawned server inherits. The MCP SDK passes only
# `DEFAULT_INHERITED_ENV_VARS` to a stdio server — six keys on POSIX, and
# PYTHONPATH is not among them. That whitelist is the reason a server subprocess
# resolves the *ambient* install even when the parent was pinned to a checkout
# with PYTHONPATH: the pin does not survive the spawn. Mirrored here (rather
# than imported) to keep this module dependency-free and to keep it measuring
# the shape that bit #3006 / #3008 / #3010 even if the SDK is absent.
MCP_INHERITED_ENV_VARS = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")

# Ask a subprocess where `reyn` resolves, without importing it. `find_spec` locates
# the package without executing its `__init__`, so a heavy import chain (or an
# import-time failure in a half-synced env) cannot turn a *location* question
# into a *health* question — precisely the conflation this gate exists to end.
_ORIGIN_PROBE = (
    "import importlib.util as u;"
    "s = u.find_spec('reyn');"
    "print(s.origin if s and s.origin else '')"
)


@dataclass(frozen=True)
class Finding:
    """One named way the environment disagrees with the tree under test."""

    check: str
    detail: str
    remedy: str

    def render(self) -> str:
        return f"  [{self.check}]\n    {self.detail}\n    remedy: {self.remedy}"


def _probe_origin(env: dict[str, str]) -> tuple[str | None, str]:
    """Return (origin, stderr) for `reyn` as resolved by a subprocess under ``env``."""
    proc = subprocess.run(
        [sys.executable, "-c", _ORIGIN_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    origin = proc.stdout.strip()
    return (origin or None), proc.stderr.strip()


def _mcp_shaped_env() -> dict[str, str]:
    """The env an MCP stdio server is actually handed."""
    return {k: os.environ[k] for k in MCP_INHERITED_ENV_VARS if k in os.environ}


def _declared_scripts(root: Path) -> dict[str, int]:
    """Map each ``[project.scripts]`` name to its pyproject.toml line number.

    The line number is half the point: a finding that says "declared at
    pyproject.toml:185" sends the reader to the declaration, which is what
    distinguishes "this venv is stale" from "this feature is broken".
    """
    pyproject = root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    names = list(data.get("project", {}).get("scripts", {}))
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    located: dict[str, int] = {}
    for name in names:
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
        located[name] = next(
            (i for i, line in enumerate(lines, 1) if pattern.match(line)), 0
        )
    return located


def _tree_of(origin: str) -> str:
    """Render a resolved origin as the checkout that owns it, for a diagnosis."""
    path = Path(origin)
    # <tree>/src/reyn/__init__.py -> <tree>; site-packages installs render as-is.
    if len(path.parents) >= 3 and path.parents[1].name == "src":
        return str(path.parents[2])
    return str(path.parent)


def check_tree_identity(root: Path) -> list[Finding]:
    """A subprocess must resolve `reyn` to the checkout under test.

    `pytest` puts ``<rootdir>/src`` on `sys.path` (``[tool.pytest.ini_options]
    pythonpath``), so an *in-process* test always reads the checkout it was
    started from. A subprocess has no such favour: it re-resolves `reyn` from
    the venv, and in a git worktree with no venv of its own that answer is
    whatever checkout the ambient venv's editable ``.pth`` points at. Both
    halves are then green and disagree, silently.
    """
    expected = root / "src" / "reyn"
    findings: list[Finding] = []

    for check, env, note in (
        (
            "tree-identity/inherited",
            dict(os.environ),
            "a plain subprocess (a test spawning sys.executable, a shell-out)",
        ),
        (
            "tree-identity/mcp-env",
            _mcp_shaped_env(),
            "an MCP stdio server (PYTHONPATH is not in the SDK's inherited-env whitelist)",
        ),
    ):
        origin, stderr = _probe_origin(env)
        if origin is None:
            findings.append(
                Finding(
                    check=check,
                    detail=(
                        f"{note} cannot import reyn at all.\n"
                        f"    This is NOT evidence that reyn is broken — it is an "
                        f"un-installed environment.\n"
                        f"    interpreter: {sys.executable}\n"
                        f"    stderr: {stderr[-200:] or '(none)'}"
                    ),
                    remedy=(
                        f"install reyn into {sys.prefix} (`pip install -e '.[dev]'` "
                        f"run FROM {root}) — see the warning in `check_console_scripts`."
                    ),
                )
            )
            continue
        if Path(origin).parent != expected:
            findings.append(
                Finding(
                    check=check,
                    detail=(
                        f"{note} resolves a DIFFERENT checkout than the tree under test.\n"
                        f"    tree under test: {root}\n"
                        f"    subprocess reads: {_tree_of(origin)}\n"
                        f"    (origin: {origin})\n"
                        f"    In-process tests here read {expected}; anything spawned "
                        f"reads the tree above. Both can be green while disagreeing."
                    ),
                    remedy=(
                        f"give this checkout its own venv, or pin PYTHONPATH={root / 'src'} "
                        f"for spawns (note: PYTHONPATH does NOT survive an MCP stdio spawn)."
                    ),
                )
            )
    return findings


def check_pinned_tree(root: Path) -> list[Finding]:
    """A subprocess pinned to this checkout's src must read this checkout.

    This is the question a test asks when it spawns something that imports reyn
    and threads ``PYTHONPATH`` to keep it honest. It is deliberately separate
    from `check_tree_identity`: that check asks what an *unpinned* spawn reads
    (and in a worktree the answer is another checkout, permanently, which no
    session can fix), whereas this one asks whether the pin — the thing a test
    can actually control — delivers. A pin that silently fails to win over an
    editable ``.pth`` would put a test back to measuring another tree while
    looking careful.
    """
    src = root / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    origin, stderr = _probe_origin(env)
    if origin is None:
        return [
            Finding(
                check="pinned-tree",
                detail=(
                    f"a subprocess pinned to PYTHONPATH={src} still cannot import reyn.\n"
                    f"    interpreter: {sys.executable}\n"
                    f"    stderr: {stderr[-200:] or '(none)'}"
                ),
                remedy=f"check that {src / 'reyn'} exists and this interpreter can read it.",
            )
        ]
    if Path(origin).parent != src / "reyn":
        return [
            Finding(
                check="pinned-tree",
                detail=(
                    f"a subprocess pinned to PYTHONPATH={src} reads a DIFFERENT checkout.\n"
                    f"    pinned to: {src}\n"
                    f"    reads:     {origin}\n"
                    f"    The pin did not win — spawned tests would measure the tree above."
                ),
                remedy=(
                    f"{src / 'reyn'} does not exist — {root} is not a checkout with a src "
                    f"layout, so the pin had nothing to point at and the venv answered "
                    f"instead."
                    if not (src / "reyn").exists()
                    else (
                        "something ahead of PYTHONPATH on sys.path resolves reyn first; "
                        f"inspect {sys.prefix}'s .pth files."
                    )
                ),
            )
        ]
    return []


def check_console_scripts(root: Path) -> list[Finding]:
    """Every declared console script must exist in this venv and read this tree.

    A `[project.scripts]` entry in pyproject.toml is a *declaration*; the console
    script is a file `pip` writes into a venv's `bin/`. Adding an entry does not
    reach into a venv that was installed before it (e.g. the top-level ``reyn``
    CLI entry; the builtin RAG ``reyn-rag-*`` scripts were retired under ADR
    0064 P5, so ``reyn`` is now the primary declared script — every venv
    installed before an entry existed has never heard of it). The absent script
    then surfaces as ``execvp() failed`` or, through a stdio client,
    ``McpError: Connection closed`` — neither of which says "absent", so both
    read as a broken feature. That misread reached a co-vet verdict twice in one
    day.
    """
    bin_dir = Path(sys.executable).parent
    findings: list[Finding] = []

    for name, line in _declared_scripts(root).items():
        declared_at = f"pyproject.toml:{line}" if line else "pyproject.toml"
        script = bin_dir / name
        if not script.exists():
            findings.append(
                Finding(
                    check="console-scripts/present",
                    detail=(
                        f"console script `{name}` is declared at {declared_at} but is "
                        f"ABSENT from {bin_dir}.\n"
                        f"    This venv is stale — it predates the declaration.\n"
                        f"    It is NOT evidence that `{name}` is broken, and NOT a flake: "
                        f"running it fails deterministically, as `execvp() failed` or "
                        f"`McpError: Connection closed`."
                    ),
                    remedy=(
                        f"re-install reyn into {sys.prefix} by running "
                        f"`pip install -e '.[dev]'` FROM the checkout this venv is "
                        f"meant to serve. Do NOT run it from a throwaway worktree: that "
                        f"repoints this venv's editable .pth at that worktree and every "
                        f"other consumer of the venv silently starts reading it (#3024)."
                    ),
                )
            )
            continue

        # The console script that will actually run is the one PATH finds, not the
        # one adjacent to sys.executable — an earlier venv on PATH shadows it.
        found = shutil.which(name)
        if found and Path(found).resolve() != script.resolve():
            findings.append(
                Finding(
                    check="console-scripts/present",
                    detail=(
                        f"console script `{name}` on PATH is NOT this venv's copy.\n"
                        f"    PATH resolves: {found}\n"
                        f"    this venv has: {script}\n"
                        f"    A spawn by name runs the PATH copy, which belongs to "
                        f"another environment."
                    ),
                    remedy=f"put {bin_dir} ahead of {Path(found).parent} on PATH.",
                )
            )
            continue

        # `pip` stamps a console script's shebang with the ABSOLUTE interpreter it
        # was installed into. A script left behind by an interpreter that no longer
        # matches this venv runs someone else's reyn while looking local.
        shebang = script.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if shebang and shebang[0].startswith("#!"):
            interpreter = shebang[0][2:].strip().split()[0]
            if Path(interpreter).resolve() != Path(sys.executable).resolve():
                findings.append(
                    Finding(
                        check="console-scripts/tree",
                        detail=(
                            f"console script `{name}` is stamped with a DIFFERENT "
                            f"interpreter than this venv's.\n"
                            f"    `{name}` runs: {interpreter}\n"
                            f"    this venv is: {sys.executable}\n"
                            f"    Whatever that interpreter resolves for `reyn` is what "
                            f"`{name}` executes — not necessarily {root}."
                        ),
                        remedy=(
                            f"re-install reyn into {sys.prefix} (see the "
                            f"console-scripts/present remedy for the .pth warning)."
                        ),
                    )
                )
    return findings


# The enumeration. A check is registered here or it does not run — `main` and the
# `tests/conftest.py` fixtures both derive their work from this map rather than
# from a hand-kept call list, so adding a check cannot leave a caller behind.
CHECKS = {
    "tree-identity": check_tree_identity,
    "pinned-tree": check_pinned_tree,
    "console-scripts": check_console_scripts,
}


def verify(root: Path, only: tuple[str, ...] = ()) -> list[Finding]:
    """Run the registered checks against ``root`` and return every finding."""
    selected = only or tuple(CHECKS)
    findings: list[Finding] = []
    for name in selected:
        findings.extend(CHECKS[name](root))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the checkout under test (default: the one containing this script)",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(CHECKS),
        default=[],
        help="run only the named check group (repeatable)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    findings = verify(root, tuple(args.only))
    if not findings:
        print(f"env-identity OK: {root} is the checkout this environment runs.")
        return 0

    print(
        f"env-identity FAILED for {root}\n"
        f"The environment does not run the checkout you are measuring. "
        f"Findings below are distinct — read each one's referent:\n",
        file=sys.stderr,
    )
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
