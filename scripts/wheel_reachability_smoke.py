#!/usr/bin/env python3
"""CI parity gate: dev == wheel repo-self-access, exercised against a REAL wheel.

Proposal 0061 §3.4 (supersedes proposal 0060's narrower "F1 builtin
production-reachability" smoke — this script previously only exercised
``read_builtin_doc`` / ``read_builtin_body_bytes``; 0061 §3.5 retired
``read_builtin_doc`` and the build-time docs mirror it depended on, in favor
of the Hatchling ``force-include`` mechanism (``pyproject.toml``
``[tool.hatch.build.targets.wheel.force-include]``) + ``reyn.runtime.reyn_repo``'s
dual-mode ``resolve_reyn_root()``). This script builds a REAL wheel, installs
ONLY that wheel (no ``-e``, no source tree on ``sys.path``) into a throwaway
venv, and probes it for:

1. **Config completeness** — the wheel contains ``py.typed``,
   ``builtin/**`` (skill/pipeline bodies, #2913, still LIVE), the
   ``environment/*.Dockerfile`` files, and the ``_bundled/`` tree
   (README/CHANGELOG/docs, 0061 force-include) — the omission risk 0061 §3.1
   calls out explicitly (drop any of these => a silently-broken wheel).
2. **POSITIVE reachability + byte-identity (0061 §3.4)** — ``reyn_repo``'s
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
5. **#2972 ambient-``python3`` builtin shell-out (mode-2, XFAIL until
   fixed)** — the builtin RAG ingest pipeline
   (``src/reyn/builtin/pipelines/rag_ingest.yaml``) shells out to
   ``python3 -m reyn.builtin.rag_ingest_helpers`` via ``sandboxed_exec``,
   which forwards the ambient ``PATH`` through unmodified — never
   ``sys.executable``, never a venv path baked into the command. This check
   reproduces that EXACT shape: plain ``python3`` resolved from a PATH
   shaped like a ``pipx install reyn`` environment (a clean, reyn-less venv
   sits first on PATH). It is the only check in this file that CAN witness
   the #2972 regression — every other check here runs the wheel through an
   interpreter that resolves reyn (``venv_python`` / ``sys.executable``),
   and a normal pytest job's ambient ``python3`` trivially IS the job's own
   (reyn-having) interpreter, so "ambient python3 == reyn's interpreter" can
   never be falsified there. Deliberately EXPECTED TO FAIL (XFAIL) until
   #2972 lands a fix; an unexpected PASS (XPASS) fails the gate instead of
   silently going stale — see ``_check_ambient_python3_shellout`` and its
   caller in ``main()``.

Exits 0 iff every check passes (check 5 XFAILs rather than passing); exits
non-zero (with a PASS/FAIL line per check) on the first structural failure,
any assertion failure, or check 5 unexpectedly passing (XPASS, #2972 fixed
but this probe not yet promoted to a strict assertion). Cleans up the temp
wheel directory and venv in a ``finally`` block, on success or failure
alike.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The in-venv probe is a NORMAL committed .py file (not a generated / string-
# templated sub-script, not a `python -c` payload). It is run directly by the
# venv interpreter — `<venv>/bin/python scripts/wheel_parity_probe.py` — and
# reads the dev repo root at RUNTIME from the `REYN_DEV_REPO_ROOT` env var, so
# no file content is ever embedded in any .py source. See its module docstring.
_PROBE_SCRIPT = REPO_ROOT / "scripts" / "wheel_parity_probe.py"


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


def _check_ambient_python3_shellout(repo_root: Path, tmp_root: Path) -> tuple[bool, str]:
    """#2972 mode-2 probe: reproduce the builtin RAG ingest pipeline's
    ambient-``python3`` shell-out EXACTLY as ``sandboxed_exec`` performs it —
    resolving ``python3`` from ``PATH`` (never ``sys.executable``, never a
    venv path baked into the command line) — inside a PATH shaped like a
    ``pipx install reyn`` environment.

    A clean venv WITHOUT reyn installed (``with_pip=False`` — no wheel, no
    ``-e``, nothing) is built and ONLY its ``bin/`` dir is prepended to
    ``PATH``, so a bare ``python3`` command resolves to an interpreter that
    cannot import reyn — manufacturing the "ambient python3 differs from
    reyn's own interpreter" condition deterministically, on any platform or
    dev box, regardless of what the CALLING process's own ambient ``python3``
    happens to be (which is why every OTHER check in this file — running the
    wheel through ``venv_python`` / ``sys.executable`` — structurally cannot
    witness this bug; see the module docstring).

    Returns ``(ok, detail)`` — ``ok`` is whether the shell-out succeeded
    (expected ``False`` until #2972 is fixed); ``detail`` is the last
    stderr/stdout line for a decision-enabling failure message.
    """
    clean_venv = tmp_root / "venv-no-reyn"
    venv.EnvBuilder(with_pip=False, clear=True).create(str(clean_venv))
    clean_bin = clean_venv / "bin"
    if not clean_bin.exists():  # pragma: no cover - Windows layout
        clean_bin = clean_venv / "Scripts"

    env = dict(os.environ)
    env["PATH"] = f"{clean_bin}{os.pathsep}{env.get('PATH', '')}"
    # No PYTHONPATH -- a dev-tree leak here would false-pass the exact thing
    # this check exists to catch.
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        ["python3", "-m", "reyn.builtin.rag_ingest_helpers", "probe"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else f"exit {result.returncode}"
    return ok, detail


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

        # 3-6. Run the committed probe .py directly with the venv interpreter.
        # The dev repo root is passed at RUNTIME via the REYN_DEV_REPO_ROOT env
        # var (no code-generation, no `.replace()` templating, no file content
        # baked into any .py source). The probe reads BOTH sides of every
        # byte-identity comparison at runtime — see scripts/wheel_parity_probe.py.
        probe_env = {**os.environ, "REYN_DEV_REPO_ROOT": str(REPO_ROOT)}
        # Strip PYTHONPATH so an inherited `src`-on-path can't leak the dev
        # source tree into the venv interpreter (the dev-mask guard in the
        # probe would catch it, but preventing it is cleaner).
        probe_env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(venv_python), str(_PROBE_SCRIPT)],
            capture_output=True,
            text=True,
            env=probe_env,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")

        if result.returncode != 0:
            print(f"[FAIL] wheel reachability parity gate FAILED (exit {result.returncode})")
            return 1

        # 7. #2972 mode-2 probe (XFAIL until fixed) -- see module docstring
        # check 5 and _check_ambient_python3_shellout's docstring for why
        # THIS is the one check in the file that can witness the regression.
        mode2_ok, mode2_detail = _check_ambient_python3_shellout(REPO_ROOT, tmp_dir)
        if mode2_ok:
            print(
                "[FAIL] XPASS (unexpected): ambient-python3 builtin shell-out "
                "now succeeds -- #2972 appears fixed. Promote this probe from "
                "XFAIL to a strict assertion (drop the XFAIL wrap in "
                "_check_ambient_python3_shellout's caller) in the SAME PR that "
                f"closes #2972, so this gate does not go silently stale. detail: {mode2_detail}"
            )
            return 1
        print(
            "[XFAIL] #2972 ambient-python3 builtin shell-out (tracked, expected "
            f"until fixed): {mode2_detail}"
        )

        print("[PASS] wheel reachability parity gate: all checks green")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Leave the repo checkout as clean as we found it (both dirs are
        # gitignored build artifacts we generated above).
        _clean_stale_build_artifacts()


if __name__ == "__main__":
    sys.exit(main())
