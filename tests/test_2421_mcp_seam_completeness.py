"""Tier 2: #2421 — every MCP entry path routes through the MCPGateway seam; no direct MCPClient
construction leaks outside it.

The whack-a-mole root: fault-isolation + task-affine lifecycle + timeout were applied per entry path,
so the list/probe paths (which constructed ``MCPClient`` directly) were swept-missed and the crash
recurred. The gateway consolidates the guarantees; this structural gate makes the bypass IMPOSSIBLE
by construction — only the pool constructs a client (client.py defines it), so a new entry path
CANNOT reintroduce the crash class without tripping this test. This is the CI-enforced regression
guard the design calls for (not a one-shot grep).
"""
from __future__ import annotations

import re
from pathlib import Path

# The seam: the pool constructs clients; client.py defines the class (+ docstring examples).
_ALLOWED = {"mcp/pool.py", "mcp/client.py"}
# ``MCPClient(`` construction — the paren distinguishes it from the ``MCPClientPool`` /
# ``MCPClient`` type-reference (``Pool`` / ``]`` / import follows, not ``(``).
_CONSTRUCT = re.compile(r"\bMCPClient\s*\(")


def test_no_direct_mcpclient_construction_outside_seam():
    """Tier 2: MCP ops go through MCPGateway → MCPClientPool → MCPClient. A new entry path that
    constructs ``MCPClient(...)`` directly would bypass the contain-all boundary + task-affine
    lifecycle (the sibling-sweep-miss class that caused the list-path crash). RED if a direct
    construction reappears anywhere in src outside the pool/client seam."""
    src = Path(__file__).resolve().parents[1] / "src" / "reyn"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        rel = py.relative_to(src).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _CONSTRUCT.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "direct MCPClient(...) construction bypasses the MCPGateway seam — route it through the "
        "gateway instead:\n" + "\n".join(offenders)
    )
