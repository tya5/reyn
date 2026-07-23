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
5. **ADR 0064 P5 / #3209 builtin ``rag`` PLUGIN install = REGISTER-ONLY
   (STRICT)** — ``plugin_install(source={"kind": "builtin", "name":
   "rag"})`` against a wheel-only reyn install must copy + register the
   plugin ONLY: no per-plugin venv materialised, and the registered mcp
   servers' spawn ``command`` is exactly what the plugin's own ``.mcp.json``
   names (register-only, ADR 0064 §3.11b — the pre-#3209 design instead
   materialised a venv and rewrote the command to point at it; that
   turnkey-install guarantee was explicitly traded away for skill-driven,
   user-managed venvs, architect firm point 3). TWO further legs prove the
   #3060 by-construction trade actually holds under the new contract:
   - **fail-fast (negative witness)**: spawning the registered command
     against an interpreter with NONE of the plugin's own deps installed
     (the operator skipped the skill's venv-setup step) fails immediately
     with a clear ``ModuleNotFoundError`` — never a hang, never a runtime
     fetch attempt.
   - **deps-present (positive witness)**: spawning the SAME registered
     command against a venv that DOES have the plugin's ``requirements.txt``
     deps installed (pip-installed here, playing the operator/LLM's
     skill-driven setup role) actually serves a real tool call — proving the
     skill-driven path this redesign moved the turnkey guarantee onto really
     works, not just that install itself completes. This supersedes the
     retired #2972 ``reyn-rag-chunker``/``reyn-rag-vector-store`` console
     scripts (ADR 0064 §4: "no console-scripts") and is the only check in
     this file that spawns the rag plugin's own scripts at all — every other
     check runs the wheel through an interpreter that already resolves reyn,
     which the rag scripts (standalone, ``import reyn`` = 0) never needed
     anyway.

Exits 0 iff every check passes; exits non-zero (with a PASS/FAIL line per
check) on the first structural failure or any assertion failure. Cleans up
the temp wheel directory and venv in a ``finally`` block, on success or
failure alike.
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


_PLUGIN_INSTALL_PROBE_SCRIPT = REPO_ROOT / "scripts" / "wheel_plugin_install_probe.py"


def _check_builtin_rag_plugin_install(wheel_path: Path, tmp_root: Path) -> bool:
    """ADR 0064 P5 / #3209 check: the builtin ``rag`` plugin (a) installs
    REGISTER-ONLY (no venv materialised, spawn command registered AS-IS),
    (b) fails fast with a clear error — no network, no runtime fetch — when
    the spawn interpreter lacks the plugin's own deps, and (c) actually
    serves a real tool call once the operator/LLM has created a venv WITH
    those deps and pointed the registered command at it (the skill-driven
    setup path #3209 moved this responsibility onto).

    THREE throwaway venvs:
      * ``venv-rag-plugin`` — wheel-only reyn install; runs the committed
        probe (this process needs only reyn + yaml to call ``plugin_install``
        — fastmcp is a core reyn dep, so the MCP client call below works
        from here too).
      * ``venv-no-deps`` — a BARE venv (``with_pip=False``): no fastmcp, no
        chonkie, nothing. Its interpreter is what the fail-fast leg spawns
        the registered chunker command against — deterministically
        reproducing "the operator skipped the skill's venv-setup step".
      * ``venv-rag-deps`` — a REAL venv with the rag plugin's own
        ``requirements.txt`` (sqlite-vec/apsw/chonkie/fastmcp) pip-installed
        into it (real network — this IS the skill-driven setup step,
        performed here by the harness playing the operator/LLM's role) —
        what the deps-present leg points the registered command at.

    The real ``plugin_install`` call + both spawn legs live in the committed
    ``scripts/wheel_plugin_install_probe.py``, run BY THE WHEEL-ONLY venv's
    interpreter. Returns True iff every check inside the probe passed.
    """
    rag_venv = tmp_root / "venv-rag-plugin"
    venv.EnvBuilder(with_pip=True, clear=True).create(str(rag_venv))
    rag_bin = rag_venv / "bin"
    if not rag_bin.exists():  # pragma: no cover - Windows layout
        rag_bin = rag_venv / "Scripts"
    rag_python = rag_bin / "python"
    if not rag_python.exists():  # pragma: no cover - Windows layout
        rag_python = rag_bin / "python.exe"

    # No [builtin-rag] extra here — register-only install never provisions
    # the plugin's own deps (#3209); this venv only needs reyn itself
    # (+ yaml, a core dep) to run the plugin_install op.
    _run([str(rag_python), "-m", "pip", "install", "--quiet", str(wheel_path)])

    # A BARE venv — no third-party packages at all — for the fail-fast leg.
    no_deps_venv = tmp_root / "venv-no-deps"
    venv.EnvBuilder(with_pip=False, clear=True).create(str(no_deps_venv))
    no_deps_bin = no_deps_venv / "bin"
    if not no_deps_bin.exists():  # pragma: no cover - Windows layout
        no_deps_bin = no_deps_venv / "Scripts"

    # A REAL venv with the plugin's own requirements.txt installed — the
    # skill-driven setup step (#3209 moved this off plugin_install onto the
    # operator/LLM), done here by the harness so the deps-present leg has a
    # ready interpreter to point the registered command at.
    rag_deps_venv = tmp_root / "venv-rag-deps"
    venv.EnvBuilder(with_pip=True, clear=True).create(str(rag_deps_venv))
    rag_deps_bin = rag_deps_venv / "bin"
    if not rag_deps_bin.exists():  # pragma: no cover - Windows layout
        rag_deps_bin = rag_deps_venv / "Scripts"
    rag_deps_python = rag_deps_bin / "python"
    if not rag_deps_python.exists():  # pragma: no cover - Windows layout
        rag_deps_python = rag_deps_bin / "python.exe"
    rag_requirements = (
        REPO_ROOT / "src" / "reyn" / "builtin" / "plugins" / "rag" / "requirements.txt"
    )
    _run([
        str(rag_deps_python), "-m", "pip", "install", "--quiet",
        "-r", str(rag_requirements),
    ])

    reyn_home = tmp_root / "reyn-home"
    reyn_home.mkdir(parents=True, exist_ok=True)

    probe_env = {
        **os.environ,
        "REYN_HOME": str(reyn_home),
        "REYN_NO_DEPS_BIN": str(no_deps_bin),
        "REYN_RAG_DEPS_PYTHON": str(rag_deps_python),
    }
    probe_env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(rag_python), str(_PLUGIN_INSTALL_PROBE_SCRIPT)],
        capture_output=True,
        text=True,
        env=probe_env,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode == 0


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

        # 7. ADR 0064 P5 / #3209 check (STRICT). See the module docstring
        # check 5 and _check_builtin_rag_plugin_install's docstring for the
        # register-only + fail-fast + deps-present three-leg contract.
        if not _check_builtin_rag_plugin_install(wheel_path, tmp_dir):
            print(
                "[FAIL] ADR 0064 P5 / #3209 builtin rag plugin install: the "
                "register-only install contract, its fail-fast (no-deps) "
                "leg, or its deps-present (real tool call) leg did not hold "
                "-- the builtin RAG arc's #3209 redesign is broken."
            )
            return 1
        print(
            "[PASS] ADR 0064 P5 / #3209 builtin rag plugin install: "
            "register-only + fail-fast + deps-present all hold"
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
