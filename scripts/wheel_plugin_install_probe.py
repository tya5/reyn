#!/usr/bin/env python3
"""In-venv probe: the builtin ``rag`` PLUGIN installs REGISTER-ONLY from a
real, wheel-only reyn install, and its registered mcp servers behave exactly
per the #3209 register-only redesign (ADR 0064 §3.11b — supersedes the
retired ``wheel_mcp_console_probe.py``, which exercised the now-removed
``reyn-rag-chunker`` console script).

Run by ``scripts/wheel_reachability_smoke.py`` INSIDE a throwaway venv that
has ONLY the built wheel installed (no ``-e``, no ``[builtin-rag]`` extra —
that extra is dev/test-only now, see ``pyproject.toml``). Three
paths/env vars this needs are passed at RUNTIME:

  * ``REYN_HOME`` — an isolated ``$HOME`` so ``plugin_install`` writes to a
    throwaway ``<REYN_HOME>/.reyn/plugins/`` rather than the real one.
  * ``REYN_NO_DEPS_BIN`` — a ``bin/`` dir of a BARE venv (no third-party
    packages at all — not even fastmcp), prepended to the spawned server's
    ``PATH`` for the fail-fast leg: deterministically reproducing "the
    operator skipped the skill's venv-setup step".
  * ``REYN_RAG_DEPS_PYTHON`` — the absolute interpreter path of a venv that
    DOES have the plugin's own ``requirements.txt`` deps installed (real
    ``pip install`` — the skill-driven setup step, performed by the smoke
    harness playing the operator/LLM's role), for the deps-present leg.

**What this witnesses.** ``plugin_install(source={"kind": "builtin", "name":
"rag"})`` run against a REAL wheel-only reyn install:
  1. resolves the builtin plugin dir the wheel ships
     (``reyn/builtin/plugins/rag/`` — the same ``force-include``/package-data
     glob 0061 already covers for ``builtin/**``),
  2. copies it to ``<REYN_HOME>/.reyn/plugins/rag/`` — and ONLY copies: no
     venv is materialised (#3209 — register-only; the pre-#3209 design
     instead ran a real ``uv venv``/``uv pip install`` here),
  3. registers its mcp servers into ``.reyn/config/mcp.yaml`` with the
     ``command`` UNCHANGED from the plugin's own ``.mcp.json`` (bare
     ``"python"``, register-only — no venv-interpreter rewrite of any kind),
  4. registers its two pipelines + its skill.

Then this probe spawns the registered chunker server DIRECTLY (the exact
command ``.reyn/config/mcp.yaml`` now carries) TWICE:
  - once against an interpreter with NONE of the plugin's own deps (the
    fail-fast/negative-witness leg — must fail immediately with a clear
    ``ModuleNotFoundError``, never a hang or a runtime fetch attempt), and
  - once against an interpreter WITH those deps installed (the deps-present/
    positive-witness leg — must actually serve a real MCP tool call),

proving the #3209 register-only contract holds on both sides: install
provisions nothing, and the skill-driven setup path it moved that
responsibility onto genuinely works.

Exits 0 iff every check passes; exits 1 with a ``[PASS]``/``[FAIL]`` line per
check.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

_FAILURES: list[str] = []


def _check(ok: bool, label: str, detail: str = "") -> None:
    if ok:
        print(f"[PASS] {label}")
    else:
        msg = f"{label}{f' — {detail}' if detail else ''}"
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


async def _run_install(project_root: Path):
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.plugin_install import handle as install_handle
    from reyn.core.op_runtime.plugin_install import plugins_root
    from reyn.schemas.models import PluginInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(plugins_root()), "probe", "file.write", recursive=True)
    for cfg in ("mcp.yaml", "pipelines.yaml", "skills.yaml"):
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), "probe", "file.write",
        )

    decl = PermissionDecl(
        file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
    )
    ctx = OpContext(
        workspace=_StubWorkspace(base_dir=project_root),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="probe",
    )
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "builtin", "name": "rag"})
    return await install_handle(op, ctx), project_root


async def _call_chunk_tool(command: str, args: list[str], env: dict[str, str]) -> list:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(command=command, args=args, env=env)
    async with Client(transport) as client:
        result = await client.call_tool(
            "chunk", {"text": "reyn plugin install probe", "size": 50, "overlap_ratio": 0.0},
        )
        return result.data


def main() -> int:
    reyn_home = os.environ.get("REYN_HOME", "")
    no_deps_bin = os.environ.get("REYN_NO_DEPS_BIN", "")
    rag_deps_python = os.environ.get("REYN_RAG_DEPS_PYTHON", "")
    if not reyn_home or not no_deps_bin or not rag_deps_python:
        print("[FAIL] REYN_HOME / REYN_NO_DEPS_BIN / REYN_RAG_DEPS_PYTHON must all be set")
        return 1

    os.environ["HOME"] = reyn_home
    project_root = Path(reyn_home) / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    result, project_root = asyncio.run(_run_install(project_root))

    _check(
        result.get("status") == "installed",
        "plugin_install(builtin, rag) reports installed",
        repr(result)[:500],
    )
    if result.get("status") != "installed":
        print(f"\n{len(_FAILURES)} check(s) FAILED")
        return 1

    caps = set(result.get("capabilities") or [])
    _check(caps == {"mcp", "pipelines", "skills"}, "all three capabilities registered", repr(caps))

    from reyn.core.op_runtime.plugin_install import plugins_root

    plugin_root = plugins_root() / "rag"

    # ── register-only: NO per-plugin venv materialised (#3209) ───────────
    venv_marker = plugin_root / ".venv"
    _check(
        not venv_marker.exists(),
        "register-only: no per-plugin venv materialised (#3209)",
        str(venv_marker),
    )

    import yaml

    mcp_yaml = project_root / ".reyn" / "config" / "mcp.yaml"
    servers = {}
    if mcp_yaml.exists():
        data = yaml.safe_load(mcp_yaml.read_text(encoding="utf-8")) or {}
        servers = ((data.get("mcp") or {}).get("servers")) or {}
    chunker_entry = servers.get("reyn_chunker") or {}

    expected_script = str(plugin_root / "scripts" / "chunker_server.py")
    _check(
        chunker_entry.get("command") == "python"
        and chunker_entry.get("args") == [expected_script],
        "register-only: reyn_chunker spawn command registered AS-IS (bare "
        "'python', the script's copy-time-baked absolute path — no "
        "venv-interpreter rewrite of any kind)",
        repr(chunker_entry),
    )

    command_args = [str(a) for a in (chunker_entry.get("args") or [expected_script])]

    # ── leg 1 (negative witness, fail-fast): the spawn interpreter has NONE
    # of the plugin's own deps -- the operator skipped the skill's venv-setup
    # step. Must fail immediately with a clear error, never a hang / a
    # runtime fetch attempt (#3060 preserved). ──────────────────────────────
    no_deps_env = dict(os.environ)
    no_deps_env["PATH"] = f"{no_deps_bin}{os.pathsep}{no_deps_env.get('PATH', '')}"
    no_deps_env.pop("PYTHONPATH", None)

    try:
        fail_fast = subprocess.run(
            ["python", *command_args],
            env=no_deps_env, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        _check(
            False,
            "fail-fast: chunker server startup fails fast (no-deps venv) "
            "-- never hangs / attempts a runtime fetch",
            f"timed out instead of failing fast: {exc}",
        )
    else:
        stderr_tail = fail_fast.stderr[-500:]
        _check(
            fail_fast.returncode != 0 and "ModuleNotFoundError" in fail_fast.stderr,
            "fail-fast: chunker server startup fails with a clear "
            "ModuleNotFoundError (no-deps venv) -- never a hang or a "
            "runtime fetch attempt (#3060 preserved)",
            stderr_tail,
        )

    # ── leg 2 (positive witness, deps-present): the SAME registered command
    # spawned against a venv that DOES have the plugin's own deps installed
    # (the operator/LLM's skill-driven setup, done here by the harness)
    # actually serves a real tool call. ─────────────────────────────────────
    deps_env = dict(os.environ)
    deps_env.pop("PYTHONPATH", None)
    # Mirror production spawn (MCPClient._open_stdio merges the registered
    # entry's OWN declared env on top) — the .mcp.json's
    # FASTMCP_SHOW_SERVER_BANNER=false / FASTMCP_CHECK_FOR_UPDATES=off apply
    # here too, not just in the real registration path.
    deps_env.update({str(k): str(v) for k, v in (chunker_entry.get("env") or {}).items()})
    try:
        chunks = asyncio.run(_call_chunk_tool(rag_deps_python, command_args, deps_env))
    except Exception as exc:  # noqa: BLE001 — any failure is the finding
        _check(
            False,
            "deps-present: registered chunker server serves a real tool "
            "call once pointed at a venv with the plugin's own deps "
            "installed",
            repr(exc),
        )
    else:
        _check(
            bool(chunks) and "content_hash" in chunks[0],
            "deps-present: registered chunker server serves a real tool "
            "call once pointed at a venv with the plugin's own deps "
            "installed",
            f"got {chunks!r:.200}",
        )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} check(s) FAILED")
        return 1
    print("\nall plugin-install checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
