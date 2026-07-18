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
5. **ADR 0064 P5 builtin ``rag`` PLUGIN install + spawn under a pipx-shaped
   PATH (STRICT)** — ``plugin_install(source={"kind": "builtin", "name":
   "rag"})`` against a wheel-only reyn install must copy the plugin,
   materialise its dependencies into a per-plugin venv (real ``uv``), and
   register its mcp servers with the spawn command rewritten to that venv's
   own interpreter; the registered chunker server must then actually start
   and serve a real tool call when the ambient ``python3`` is NOT reyn's
   interpreter. This supersedes the retired #2972
   ``reyn-rag-chunker``/``reyn-rag-vector-store`` console scripts (ADR 0064
   §4: "no console-scripts ... spawn is network-free by construction") — the
   plugin's absolute-path-to-materialised-venv spawn command is now what
   makes the arc work under ``pipx install reyn``, a non-activated venv, or
   any environment whose ambient ``python3`` differs from reyn's own, and
   this is the only check in this file that can witness it: every other
   check runs the wheel through an interpreter that already resolves reyn
   (``venv_python`` / ``sys.executable``), and a normal pytest job's ambient
   ``python3`` trivially IS the job's own (reyn-having) interpreter, so
   "ambient python3 == reyn's interpreter" can never be falsified there.

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
    """ADR 0064 P5 check: the builtin ``rag`` plugin installs (real
    ``plugin_install``) and spawns via its own materialised per-plugin venv,
    in a PATH shaped like a ``pipx install reyn`` environment (ambient
    ``python3`` is not reyn's interpreter).

    A clean venv WITHOUT reyn (``with_pip=False`` — no wheel, no ``-e``,
    nothing) is built and its ``bin/`` dir is prepended to the SPAWNED
    server's ``PATH``, manufacturing "ambient python3 differs from reyn's own
    interpreter" deterministically. That is why this is the only check here
    that can witness the property: every other one runs the wheel through an
    interpreter that already resolves reyn.

    Needs a SECOND venv with the wheel installed WITHOUT the ``builtin-rag``
    extra (that extra is dev/test-only now, ADR 0064 P5) — ``plugin_install``
    itself materialises chonkie/apsw/sqlite-vec/fastmcp into a THIRD,
    per-plugin venv via real ``uv venv``/``uv pip install`` (needs ``uv`` on
    PATH and real network — the install-time fetch ADR 0064 §3.11
    describes; measured ~60s, affordable against the job's 5-minute budget).

    The real ``plugin_install`` call + MCP client call live in the committed
    ``scripts/wheel_plugin_install_probe.py``, run BY THE WHEEL-ONLY venv's
    interpreter (this process — the CI job python — has neither reyn nor
    fastmcp). Returns True iff every check inside the probe passed.
    """
    rag_venv = tmp_root / "venv-rag-plugin"
    venv.EnvBuilder(with_pip=True, clear=True).create(str(rag_venv))
    rag_bin = rag_venv / "bin"
    if not rag_bin.exists():  # pragma: no cover - Windows layout
        rag_bin = rag_venv / "Scripts"
    rag_python = rag_bin / "python"
    if not rag_python.exists():  # pragma: no cover - Windows layout
        rag_python = rag_bin / "python.exe"

    # No [builtin-rag] extra here — the plugin materialises its OWN deps at
    # install time; this venv only needs reyn itself (+ yaml, a core dep) to
    # run the plugin_install op.
    _run([str(rag_python), "-m", "pip", "install", "--quiet", str(wheel_path)])

    clean_venv = tmp_root / "venv-no-reyn"
    venv.EnvBuilder(with_pip=False, clear=True).create(str(clean_venv))
    clean_bin = clean_venv / "bin"
    if not clean_bin.exists():  # pragma: no cover - Windows layout
        clean_bin = clean_venv / "Scripts"

    reyn_home = tmp_root / "reyn-home"
    reyn_home.mkdir(parents=True, exist_ok=True)

    probe_env = {
        **os.environ,
        "REYN_HOME": str(reyn_home),
        "REYN_CLEAN_BIN": str(clean_bin),
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

        # 7. ADR 0064 P5 check (STRICT). See the module docstring check 5 and
        # _check_builtin_rag_plugin_install for why THIS is the one check in
        # the file that can witness ambient-python3-independence.
        if not _check_builtin_rag_plugin_install(wheel_path, tmp_dir):
            print(
                "[FAIL] ADR 0064 P5 builtin rag plugin install: plugin_install "
                "did not install + spawn the rag plugin's chunker server under "
                "a pipx-shaped PATH -- the builtin RAG arc is broken for any "
                "install whose ambient python3 is not reyn's interpreter."
            )
            return 1
        print(
            "[PASS] ADR 0064 P5 builtin rag plugin install + spawn under a pipx-shaped PATH"
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
