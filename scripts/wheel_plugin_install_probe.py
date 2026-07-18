#!/usr/bin/env python3
"""In-venv probe: the builtin ``rag`` PLUGIN installs and spawns from a real,
wheel-only reyn install (ADR 0064 P5 — supersedes the retired
``wheel_mcp_console_probe.py``, which exercised the now-removed
``reyn-rag-chunker`` console script).

Run by ``scripts/wheel_reachability_smoke.py`` INSIDE a throwaway venv that
has ONLY the built wheel installed (no ``-e``, no ``[builtin-rag]`` extra —
that extra is dev/test-only now, see ``pyproject.toml``). The two paths/env
vars this needs are passed at RUNTIME:

  * ``REYN_HOME`` — an isolated ``$HOME`` so ``plugin_install`` writes to a
    throwaway ``<REYN_HOME>/.reyn/plugins/`` rather than the real one.
  * ``REYN_CLEAN_BIN`` — a ``bin/`` dir of a venv WITHOUT reyn, prepended to
    the spawned server's ``PATH``, manufacturing "ambient python3 is not
    reyn's interpreter" (the pipx-shaped condition #2972 cared about)
    deterministically. This is what makes the registered mcp spawn command
    an ABSOLUTE path to the plugin's own materialised venv interpreter
    matter: if it were bare ``python``, this hostile PATH would resolve the
    wrong (or no) interpreter.

**What this witnesses.** ``plugin_install(source={"kind": "builtin", "name":
"rag"})`` run against a REAL wheel-only reyn install:
  1. resolves the builtin plugin dir the wheel ships
     (``reyn/builtin/plugins/rag/`` — the same ``force-include``/package-data
     glob 0061 already covers for ``builtin/**``),
  2. copies it to ``<REYN_HOME>/.reyn/plugins/rag/``,
  3. materialises its ``requirements.txt`` (chonkie/apsw/sqlite-vec/fastmcp)
     into a DEDICATED per-plugin venv via real ``uv venv`` + ``uv pip
     install`` (real network — the install-time fetch ADR 0064 §3.11
     describes),
  4. registers its mcp servers into ``.reyn/config/mcp.yaml`` with the
     ``command`` rewritten from ``python`` to that materialised venv's own
     interpreter (so spawn needs no network and does not depend on the
     ambient ``python3``),
  5. registers its two pipelines + its skill.

Then this probe spawns the registered chunker server DIRECTLY (the exact
command ``.reyn/config/mcp.yaml`` now carries) under the pipx-shaped PATH and
calls a real MCP tool on it — proving spawn really is network-free and
ambient-python3-independent, not just that install succeeded.

Exits 0 iff every check passes; exits 1 with a ``[PASS]``/``[FAIL]`` line per
check.
"""
from __future__ import annotations

import asyncio
import os
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
    resolver.session_approve_host("pypi.org", "probe")

    decl = PermissionDecl(
        file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
        http_get=[{"host": "pypi.org"}],
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
    clean_bin = os.environ.get("REYN_CLEAN_BIN", "")
    if not reyn_home or not clean_bin:
        print("[FAIL] REYN_HOME / REYN_CLEAN_BIN must both be set")
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
        if _FAILURES:
            print(f"\n{len(_FAILURES)} check(s) FAILED")
            return 1

    caps = set(result.get("capabilities") or [])
    _check(caps == {"mcp", "pipelines", "skills"}, "all three capabilities registered", repr(caps))

    from reyn.core.op_runtime.plugin_install import plugins_root

    plugin_root = plugins_root() / "rag"
    venv_python = plugin_root / ".venv" / "bin" / "python"
    _check(venv_python.exists(), "per-plugin venv interpreter materialised", str(venv_python))

    import yaml

    mcp_yaml = project_root / ".reyn" / "config" / "mcp.yaml"
    servers = {}
    if mcp_yaml.exists():
        data = yaml.safe_load(mcp_yaml.read_text(encoding="utf-8")) or {}
        servers = ((data.get("mcp") or {}).get("servers")) or {}
    chunker_entry = servers.get("reyn_chunker") or {}
    _check(
        chunker_entry.get("command") == str(venv_python),
        "registered reyn_chunker spawn command points at the materialised venv interpreter",
        repr(chunker_entry),
    )

    # ── the real spawn-under-hostile-PATH check ──────────────────────────
    env = dict(os.environ)
    env["PATH"] = f"{clean_bin}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)

    probe_ambient = __import__("subprocess").run(
        ["python3", "-c", "import reyn"], env=env, capture_output=True, text=True,
    )
    _check(
        probe_ambient.returncode != 0,
        "ambient python3 on the probe PATH cannot import reyn (pipx-shaped)",
        "a reyn-importing python3 would make the spawn check vacuous",
    )

    command = chunker_entry.get("command", "")
    args = [str(a) for a in (chunker_entry.get("args") or [])]
    try:
        chunks = asyncio.run(_call_chunk_tool(command, args, env))
    except Exception as exc:  # noqa: BLE001 — any failure is the finding
        _check(False, "registered chunker server serves a real tool call under a hostile PATH", repr(exc))
    else:
        _check(
            bool(chunks) and "content_hash" in chunks[0],
            "registered chunker server serves a real tool call under a hostile PATH",
            f"got {chunks!r:.200}",
        )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} check(s) FAILED")
        return 1
    print("\nall plugin-install checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
