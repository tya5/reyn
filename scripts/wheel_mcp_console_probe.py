#!/usr/bin/env python3
"""In-venv probe: a builtin RAG MCP server launches via its console script
when the ambient ``python3`` is NOT reyn's interpreter (#2972).

Run by ``scripts/wheel_reachability_smoke.py`` INSIDE a throwaway venv that has
the built wheel + its ``[builtin-rag]`` extra installed, via
``<venv>/bin/python scripts/wheel_mcp_console_probe.py``. It is a normal
committed ``.py`` file — NOT a generated / string-templated sub-script and NOT
handed to ``python -c`` (same convention as ``wheel_parity_probe.py``). The two
paths it needs are passed at RUNTIME via env vars:

  * ``REYN_CONSOLE_SCRIPT`` — absolute path to the installed
    ``reyn-rag-chunker`` console script (the artifact under test).
  * ``REYN_CLEAN_BIN`` — a ``bin/`` dir of a venv WITHOUT reyn, prepended to
    the child's ``PATH`` so a bare ``python3`` resolves to an interpreter that
    cannot import reyn.

**What this witnesses, and why it needs a hostile PATH.** reyn does not own the
operator's python runtime: it ships the builtin MCP servers' code and starts
whatever ``command`` the operator's config names, as-is. The console script is
what makes that offer work under ``pipx install reyn`` — pip stamps a console
script's shebang with the ABSOLUTE path of the interpreter it was installed
into, so the server runs under reyn's own python no matter what ``python3``
means on the caller's PATH. The claim is therefore only falsifiable in an
environment where ambient ``python3`` is NOT reyn's, which this probe
manufactures deterministically (rather than hoping the CI box happens to have
one). It asserts that condition really holds before trusting the result — a
clean venv that could somehow import reyn would make the whole check vacuous.

The server is driven through a REAL MCP stdio client (``fastmcp.Client``) and a
REAL tool call, not just "did the process stay up": a server that starts and
then fails every call would otherwise pass.

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


def _pipx_shaped_env(clean_bin: str) -> dict[str, str]:
    """The child's env: a reyn-less venv's ``bin/`` first on PATH, and no
    PYTHONPATH (an inherited one would re-add reyn to the child by another
    route and false-pass the very thing this probe exists to catch)."""
    env = dict(os.environ)
    env["PATH"] = f"{clean_bin}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)
    return env


async def _call_chunk_tool(script: str, env: dict[str, str]) -> list:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(command=script, args=[], env=env)
    async with Client(transport) as client:
        result = await client.call_tool(
            "chunk", {"text": "reyn console script probe", "size": 50, "overlap_ratio": 0.0},
        )
        return result.data


def main() -> int:
    script = os.environ.get("REYN_CONSOLE_SCRIPT", "")
    clean_bin = os.environ.get("REYN_CLEAN_BIN", "")
    if not script or not clean_bin:
        print("[FAIL] REYN_CONSOLE_SCRIPT / REYN_CLEAN_BIN must both be set")
        return 1

    # 0. The console script is a real shipped artifact, and its shebang names
    #    an absolute interpreter (the mechanism the whole check rests on).
    _check(Path(script).exists(), f"console script shipped by the wheel: {Path(script).name}")
    shebang = Path(script).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    _check(
        shebang.startswith("#!/") and "python" in shebang,
        "console script shebang pins an absolute interpreter",
        shebang,
    )

    env = _pipx_shaped_env(clean_bin)

    # 1. GUARD: the manufactured condition must actually hold. If the ambient
    #    python3 on this PATH CAN import reyn, check 2 proves nothing.
    probe = subprocess.run(
        ["python3", "-c", "import reyn"], env=env, capture_output=True, text=True,
    )
    _check(
        probe.returncode != 0,
        "ambient python3 on the probe PATH cannot import reyn (pipx-shaped)",
        "a reyn-importing python3 would make the launch check vacuous",
    )

    # 2. THE CHECK: the console script serves a real MCP tool call anyway.
    try:
        chunks = asyncio.run(_call_chunk_tool(script, env))
    except Exception as exc:  # noqa: BLE001 — any failure is the finding
        _check(False, "console-script MCP server serves a tool call under that PATH", repr(exc))
    else:
        _check(
            bool(chunks) and "content_hash" in chunks[0],
            "console-script MCP server serves a tool call under that PATH",
            f"got {chunks!r:.200}",
        )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} check(s) FAILED")
        return 1
    print("\nall console-script launch checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
