#!/usr/bin/env python3
"""CI smoke: F1 builtin production-reachability, exercised against a REAL wheel.

Proposal 0060 F1: builtin doc/skill/pipeline bodies (``reyn.builtin.docs``,
``reyn.builtin.registry``) must be readable in a real ``pip install`` wheel,
where ``resolve_reyn_root()`` (``reyn.runtime.reyn_src``) raises — there is no
co-located ``pyproject.toml`` to anchor the repo-root walk — and the source
``docs/`` tree is absent entirely. This invariant depends on two build-time
mechanisms staying wired together: the ``[tool.setuptools.package-data]``
``"builtin/**/*"`` glob (``pyproject.toml``) and the custom ``build_py``
cmdclass (``setup.py``) that runs ``scripts/mirror_reference_docs.py`` to copy
``docs/reference/`` into ``src/reyn/builtin/reference/`` before that glob is
collected.

**Why this script exists (the debt it closes).** Before this script, the only
CI coverage of F1 reachability was ``tests/test_2913_builtin_body_wheel_reachable.py``
— importing ``reyn`` from the dev checkout (``pip install -e .``) and
monkeypatching a *simulated* wheel ``project_root``. That test imports
``reyn.builtin`` from the SOURCE TREE via ``importlib.resources`` — which reads
straight from ``src/reyn/builtin/...`` on disk, present regardless of whether
the packaging glob or the ``build_py`` mirror step still work. If either
silently broke (e.g. someone deleted the ``build_py`` cmdclass override, or
typo'd the package-data glob), the dev-checkout test would keep passing while
a real ``pip install reyn`` wheel silently shipped WITHOUT the builtin
content — a production break with zero CI signal. This script closes that gap
by building an actual wheel, installing ONLY the wheel (no ``-e``, no source
tree on ``sys.path``) into a throwaway venv, and reading the builtin content
through that installed package.

**What it checks, in order:**

1. Build a wheel via ``python -m build --wheel`` into a temp directory.
2. Create a fresh venv and ``pip install --no-deps`` ONLY that wheel (no
   dependencies needed — ``reyn.builtin.docs`` / ``reyn.builtin.registry`` are
   stdlib-only, and installing the full dependency set would make this smoke
   needlessly slow for a check that doesn't need it).
3. Dev-mask guard: assert ``reyn.__file__`` resolves under the venv's
   site-packages, NOT this repo's ``src/`` tree — if this assertion doesn't
   hold, every check below would false-pass against the source checkout
   instead of the wheel, exactly the failure mode this script exists to close.
4. Positive reachability: ``read_builtin_doc`` on a real ``docs/reference/``
   path, and ``read_builtin_body_bytes`` on the ``reyn_cheat_sheet`` skill,
   the ``flagship`` pipeline, and the ``draft_judge_revise`` skill — each
   asserted non-empty with recognizable content (not just "no exception").
5. Negative least-privilege scoping (#2914 co-vet Ruling 1):
   ``read_builtin_body_bytes`` on an in-package ``.py`` module and on a
   ``reference/`` doc path must return ``None`` (NOT bytes) — proving the
   bypass stays scoped to ``skills/``/``pipelines/`` bodies only.

Exits 0 iff every check passes; exits non-zero (with a PASS/FAIL line per
check) on the first structural failure or any assertion failure. Cleans up
the temp wheel directory and venv in a ``finally`` block, on success or
failure alike.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The probe runs INSIDE the venv's own interpreter (so imports resolve against
# the installed wheel, never this repo's src/ tree via sys.path/PYTHONPATH
# leaking in). It is handed to the venv python as a `-c` script rather than a
# file under REPO_ROOT so there is no chance of an accidental relative import
# resolving back into the source checkout.
_PROBE_SRC = r"""
import sys
from pathlib import Path

_CHECK_NAMES = [
    "dev-mask guard (reyn.__file__ under venv, not source tree)",
    "read_builtin_doc: reference doc reachable",
    "read_builtin_body_bytes: reyn_cheat_sheet SKILL.md reachable",
    "read_builtin_body_bytes: flagship pipeline yaml reachable",
    "read_builtin_body_bytes: draft_judge_revise SKILL.md reachable",
    "read_builtin_body_bytes: non-body .py path returns None (least-privilege)",
    "read_builtin_body_bytes: reference/ doc path returns None (least-privilege)",
]

results = []

def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
    except Exception as exc:  # noqa: BLE001 - report every failure, don't stop early
        results.append((name, False, f"{type(exc).__name__}: {exc}"))

import reyn

def _dev_mask_guard():
    reyn_file = Path(reyn.__file__).resolve()
    venv_root = Path(sys.prefix).resolve()
    assert str(reyn_file).startswith(str(venv_root)), (
        f"reyn.__file__ ({reyn_file}) is NOT under the venv ({venv_root}) -- "
        "this probe is reading the source checkout, not the installed wheel; "
        "every check below would be meaningless"
    )
    repo_root_marker = Path(r"__REPO_ROOT__").resolve()
    assert not str(reyn_file).startswith(str(repo_root_marker)), (
        f"reyn.__file__ ({reyn_file}) resolves under the repo checkout "
        f"({repo_root_marker}) -- dev-tree leakage into the venv"
    )

check(_CHECK_NAMES[0], _dev_mask_guard)

from reyn.builtin.docs import read_builtin_doc, read_builtin_body_bytes
from reyn.builtin.registry import BUILTIN_SKILLS, BUILTIN_PIPELINES

def _check_reference_doc():
    text = read_builtin_doc("glossary.md")
    assert text and len(text.strip()) > 0, "glossary.md read back empty"

check(_CHECK_NAMES[1], _check_reference_doc)

def _check_cheat_sheet():
    path = BUILTIN_SKILLS["reyn_cheat_sheet"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "reyn_cheat_sheet SKILL.md body is empty"
    assert b"reyn_cheat_sheet" in body, "reyn_cheat_sheet SKILL.md missing its own name marker"

check(_CHECK_NAMES[2], _check_cheat_sheet)

def _check_flagship_pipeline():
    path = BUILTIN_PIPELINES["flagship"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "flagship pipeline yaml body is empty"

check(_CHECK_NAMES[3], _check_flagship_pipeline)

def _check_draft_judge_revise():
    path = BUILTIN_SKILLS["draft_judge_revise"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "draft_judge_revise SKILL.md body is empty"
    assert b"draft_judge_revise" in body, "draft_judge_revise SKILL.md missing its own name marker"

check(_CHECK_NAMES[4], _check_draft_judge_revise)

def _check_negative_non_body_py():
    import reyn.builtin.registry as registry_mod
    non_body_path = registry_mod.__file__
    result = read_builtin_body_bytes(non_body_path)
    assert result is None, (
        f"read_builtin_body_bytes({non_body_path!r}) returned {len(result) if result else 0} "
        "bytes -- expected None (in-package .py is NOT a legitimate body read)"
    )

check(_CHECK_NAMES[5], _check_negative_non_body_py)

def _check_negative_reference_doc_path():
    import importlib.resources as resources
    reference_root = resources.files("reyn.builtin") / "reference"
    candidate = reference_root / "glossary.md"
    result = read_builtin_body_bytes(str(candidate))
    assert result is None, (
        f"read_builtin_body_bytes on a reference/ doc path returned "
        f"{len(result) if result else 0} bytes -- expected None (reference/ is read via "
        "read_builtin_doc, never through the body-read bypass)"
    )

check(_CHECK_NAMES[6], _check_negative_reference_doc_path)

any_fail = False
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    if not ok:
        any_fail = True
    line = f"[{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)

sys.exit(1 if any_fail else 0)
"""


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def _clean_stale_build_artifacts() -> None:
    """Remove ``build/`` and ``src/reyn.egg-info/`` before building.

    Both are gitignored, regenerated-every-build artifact directories
    (``.gitignore``: ``build/``, ``src/*.egg-info/``). ``setuptools``'
    ``build_py`` copies files INTO ``build/lib/...`` incrementally — it does
    not clean previously-copied files that no longer match the current
    ``package-data`` glob. A stale ``build/`` left over from an EARLIER
    (working) invocation can therefore mask a packaging regression: the
    wheel would keep shipping ``builtin/reference`` because it is still
    sitting in ``build/lib/`` from before, not because the (now-broken)
    glob still matches it — a false-allow this script exists specifically
    to prevent. Always start from a clean slate.
    """
    for stale in (REPO_ROOT / "build", REPO_ROOT / "src" / "reyn.egg-info"):
        if stale.exists():
            shutil.rmtree(stale)


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="reyn-wheel-smoke-"))
    wheel_dir = tmp_dir / "dist"
    venv_dir = tmp_dir / "venv"
    try:
        _clean_stale_build_artifacts()
        # 1. Build the wheel from the checkout into an isolated dist dir.
        try:
            _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheel_dir)], cwd=str(REPO_ROOT))
        except FileNotFoundError as exc:
            print(f"[FAIL] `python -m build` unavailable: {exc} -- install the `build` package first")
            return 1

        wheels = sorted(wheel_dir.glob("reyn-*.whl"))
        if not wheels:
            print(f"[FAIL] no wheel produced in {wheel_dir}")
            return 1
        wheel_path = wheels[-1]
        print(f"built wheel: {wheel_path}")

        # 2. Fresh venv, wheel-only install (no -e, no deps -- the probed
        #    modules are stdlib-only so this stays fast).
        venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))
        venv_python = venv_dir / "bin" / "python"
        if not venv_python.exists():  # pragma: no cover - Windows layout
            venv_python = venv_dir / "Scripts" / "python.exe"

        _run([str(venv_python), "-m", "pip", "install", "--no-deps", "--quiet", str(wheel_path)])

        # 3-5. Run the probe INSIDE the venv interpreter.
        probe_src = _PROBE_SRC.replace("__REPO_ROOT__", str(REPO_ROOT))
        result = subprocess.run(
            [str(venv_python), "-c", probe_src],
            capture_output=True,
            text=True,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")

        if result.returncode != 0:
            print(f"[FAIL] wheel reachability smoke FAILED (exit {result.returncode})")
            return 1

        print("[PASS] wheel reachability smoke: all checks green")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Leave the repo checkout as clean as we found it (both dirs are
        # gitignored build artifacts we generated above).
        _clean_stale_build_artifacts()


if __name__ == "__main__":
    sys.exit(main())
