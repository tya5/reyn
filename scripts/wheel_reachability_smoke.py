#!/usr/bin/env python3
"""CI parity gate: dev == wheel repo-self-access, exercised against a REAL wheel.

Proposal 0061 §3.4 (supersedes proposal 0060's narrower "F1 builtin
production-reachability" smoke — this script previously only exercised
``read_builtin_doc`` / ``read_builtin_body_bytes``; 0061 §3.5 retired
``read_builtin_doc`` and the build-time docs mirror it depended on, in favor
of the Hatchling ``force-include`` mechanism (``pyproject.toml``
``[tool.hatch.build.targets.wheel.force-include]``) + ``reyn.runtime.reyn_src``'s
dual-mode ``resolve_reyn_root()``). This script builds a REAL wheel, installs
ONLY that wheel (no ``-e``, no source tree on ``sys.path``) into a throwaway
venv, and probes it for:

1. **Config completeness** — the wheel contains ``py.typed``,
   ``builtin/**`` (skill/pipeline bodies, #2913, still LIVE), the
   ``environment/*.Dockerfile`` files, and the ``_bundled/`` tree
   (README/CHANGELOG/docs, 0061 force-include) — the omission risk 0061 §3.1
   calls out explicitly (drop any of these => a silently-broken wheel).
2. **POSITIVE reachability + byte-identity (0061 §3.4)** — ``reyn_src``'s
   ``resolve_reyn_root`` + ``safe_resolve_inside`` + ``read_text`` read
   README.md, a ``docs/`` file, and a ``src/reyn/`` source file THROUGH the
   installed wheel, and their content is byte-identical to the same logical
   path read directly off this dev checkout. Proves the dual-mode resolver's
   "one logical namespace, two physical layouts" invariant for real, not just
   in a monkeypatched test.
3. **NEGATIVE reachable-set refusal (0061 §3.3/§3.4 flip-witness)** — a
   non-declared path (``pyproject.toml`` — genuinely absent from the wheel)
   is refused via ``ValueError``, not silently returning nothing.
4. **#2913 builtin body reads (kept, LIVE, unaffected by 0061)** —
   ``read_builtin_body_bytes`` on a real skill/pipeline body, plus the
   least-privilege negative (an in-package ``.py`` module returns ``None``).

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
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Real dev-checkout content the wheel-side reads are compared against for
# byte-identity. Read here (in THIS process, off the source tree) — not
# inside the venv subprocess, so there's no ambiguity about which tree the
# "expected" bytes came from.
_EXPECTED_README = (REPO_ROOT / "README.md").read_bytes()
_EXPECTED_DOC = (REPO_ROOT / "docs" / "index.md").read_bytes()
_EXPECTED_SOURCE = (REPO_ROOT / "src" / "reyn" / "runtime" / "reyn_src.py").read_bytes()

# The probe runs INSIDE the venv's own interpreter (so imports resolve against
# the installed wheel, never this repo's src/ tree via sys.path/PYTHONPATH
# leaking in). It is handed to the venv python as a `-c` script rather than a
# file under REPO_ROOT so there is no chance of an accidental relative import
# resolving back into the source checkout. Expected bytes are passed in via
# repr() literals (README/docs/source content) baked into the script text.
_PROBE_SRC = r"""
import sys
from pathlib import Path

_CHECK_NAMES = [
    "dev-mask guard (reyn.__file__ under venv, not source tree)",
    "resolve_reyn_root: wheel mode detected (_bundled/ present)",
    "reyn_src read: README.md byte-identical to dev checkout",
    "reyn_src read: docs/index.md byte-identical to dev checkout",
    "reyn_src read: src/reyn/runtime/reyn_src.py byte-identical to dev checkout",
    "reyn_src read: non-declared path (pyproject.toml) refused",
    "read_builtin_body_bytes: reyn_cheat_sheet SKILL.md reachable",
    "read_builtin_body_bytes: flagship pipeline yaml reachable",
    "read_builtin_body_bytes: non-body .py path returns None (least-privilege)",
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

from reyn.runtime.reyn_src import read_text, resolve_reyn_root, safe_resolve_inside

def _check_wheel_mode_detected():
    resolve_reyn_root.cache_clear()
    root = resolve_reyn_root()
    assert (root / "_bundled").is_dir(), (
        f"resolve_reyn_root() returned {root}, expected a wheel package dir "
        "with an adjacent _bundled/ directory (0061 SS3.2 wheel-mode signal)"
    )

check(_CHECK_NAMES[1], _check_wheel_mode_detected)

def _read_logical(logical_path):
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, logical_path)
    result = read_text(target, logical_path)
    assert "content" in result, f"read_text({logical_path!r}) returned no content: {result}"
    return result["content"].encode("utf-8")

def _check_readme_byte_identical():
    got = _read_logical("README.md")
    assert got == __EXPECTED_README__, (
        f"README.md via wheel-mode reyn_src ({len(got)} bytes) does not match "
        f"the dev checkout ({len(__EXPECTED_README__)} bytes)"
    )

check(_CHECK_NAMES[2], _check_readme_byte_identical)

def _check_docs_byte_identical():
    got = _read_logical("docs/index.md")
    assert got == __EXPECTED_DOC__, (
        f"docs/index.md via wheel-mode reyn_src ({len(got)} bytes) does not match "
        f"the dev checkout ({len(__EXPECTED_DOC__)} bytes)"
    )

check(_CHECK_NAMES[3], _check_docs_byte_identical)

def _check_source_byte_identical():
    got = _read_logical("src/reyn/runtime/reyn_src.py")
    assert got == __EXPECTED_SOURCE__, (
        f"src/reyn/runtime/reyn_src.py via wheel-mode reyn_src ({len(got)} bytes) "
        f"does not match the dev checkout ({len(__EXPECTED_SOURCE__)} bytes)"
    )

check(_CHECK_NAMES[4], _check_source_byte_identical)

def _check_non_declared_path_refused():
    root = resolve_reyn_root()
    try:
        safe_resolve_inside(root, "pyproject.toml")
    except ValueError as exc:
        assert "reachable set" in str(exc), f"wrong refusal reason: {exc}"
        return
    raise AssertionError("pyproject.toml resolved instead of being refused")

check(_CHECK_NAMES[5], _check_non_declared_path_refused)

from reyn.builtin.docs import read_builtin_body_bytes
from reyn.builtin.registry import BUILTIN_SKILLS, BUILTIN_PIPELINES

def _check_cheat_sheet():
    path = BUILTIN_SKILLS["reyn_cheat_sheet"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "reyn_cheat_sheet SKILL.md body is empty"
    assert b"reyn_cheat_sheet" in body, "reyn_cheat_sheet SKILL.md missing its own name marker"

check(_CHECK_NAMES[6], _check_cheat_sheet)

def _check_flagship_pipeline():
    path = BUILTIN_PIPELINES["flagship"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "flagship pipeline yaml body is empty"

check(_CHECK_NAMES[7], _check_flagship_pipeline)

def _check_negative_non_body_py():
    import reyn.builtin.registry as registry_mod
    non_body_path = registry_mod.__file__
    result = read_builtin_body_bytes(non_body_path)
    assert result is None, (
        f"read_builtin_body_bytes({non_body_path!r}) returned {len(result) if result else 0} "
        "bytes -- expected None (in-package .py is NOT a legitimate body read)"
    )

check(_CHECK_NAMES[8], _check_negative_non_body_py)

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
    """Remove ``build/``, ``dist/`` and ``src/reyn.egg-info/`` before building.

    All are gitignored, regenerated-every-build artifact directories. A stale
    leftover from an EARLIER (working) invocation could mask a packaging
    regression, so always start from a clean slate.
    """
    for stale in (
        REPO_ROOT / "build",
        REPO_ROOT / "dist",
        REPO_ROOT / "src" / "reyn.egg-info",
    ):
        if stale.exists():
            shutil.rmtree(stale)


def _check_wheel_config_completeness(wheel_path: Path) -> list[str]:
    """0061 SS3.1 config completeness — the three package-data entries
    (py.typed, builtin/**/*, environment/*.Dockerfile) plus the force-include
    _bundled/ tree (README/CHANGELOG/docs). Returns a list of failure
    messages (empty = all present)."""
    failures: list[str] = []
    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())

    def _any_match(predicate) -> bool:
        return any(predicate(n) for n in names)

    if "reyn/py.typed" not in names:
        failures.append("py.typed missing from wheel")
    if not _any_match(lambda n: n.startswith("reyn/builtin/skills/") and n.endswith(".md")):
        failures.append("builtin/skills/**/*.md missing from wheel")
    if not _any_match(lambda n: n.startswith("reyn/builtin/pipelines/") and n.endswith(".yaml")):
        failures.append("builtin/pipelines/**/*.yaml missing from wheel")
    if not _any_match(lambda n: n.startswith("reyn/environment/") and n.endswith(".Dockerfile")):
        failures.append("environment/*.Dockerfile missing from wheel")
    if "reyn/_bundled/README.md" not in names:
        failures.append("_bundled/README.md (force-include) missing from wheel")
    if "reyn/_bundled/CHANGELOG.md" not in names:
        failures.append("_bundled/CHANGELOG.md (force-include) missing from wheel")
    if not _any_match(lambda n: n.startswith("reyn/_bundled/docs/") and n.endswith(".md")):
        failures.append("_bundled/docs/**/*.md (force-include) missing from wheel")
    return failures


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

        # 1b. Config completeness (0061 SS3.1) -- inspect the wheel archive
        # directly before even installing it.
        completeness_failures = _check_wheel_config_completeness(wheel_path)
        if completeness_failures:
            for msg in completeness_failures:
                print(f"[FAIL] config completeness: {msg}")
            return 1
        print("[PASS] config completeness: py.typed + builtin/** + environment/*.Dockerfile + _bundled/ all present")

        # 2. Fresh venv, wheel-only install (no -e, no deps -- the probed
        #    modules are stdlib-only so this stays fast).
        venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))
        venv_python = venv_dir / "bin" / "python"
        if not venv_python.exists():  # pragma: no cover - Windows layout
            venv_python = venv_dir / "Scripts" / "python.exe"

        _run([str(venv_python), "-m", "pip", "install", "--no-deps", "--quiet", str(wheel_path)])

        # 3-6. Run the probe INSIDE the venv interpreter.
        probe_src = (
            _PROBE_SRC.replace("__REPO_ROOT__", str(REPO_ROOT))
            .replace("__EXPECTED_README__", repr(_EXPECTED_README))
            .replace("__EXPECTED_DOC__", repr(_EXPECTED_DOC))
            .replace("__EXPECTED_SOURCE__", repr(_EXPECTED_SOURCE))
        )
        result = subprocess.run(
            [str(venv_python), "-c", probe_src],
            capture_output=True,
            text=True,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")

        if result.returncode != 0:
            print(f"[FAIL] wheel reachability parity gate FAILED (exit {result.returncode})")
            return 1

        print("[PASS] wheel reachability parity gate: all checks green")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Leave the repo checkout as clean as we found it (both dirs are
        # gitignored build artifacts we generated above).
        _clean_stale_build_artifacts()


if __name__ == "__main__":
    sys.exit(main())
