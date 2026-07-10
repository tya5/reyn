"""Tier 2: the stream-consuming client is single-writer by construction (P1).

ADR-0039 P1's single-writer contract: the client (renderer + input handling)
touches the world ONLY through its ``ClientTransport`` — never ``Session`` /
``Workspace`` / tool / op-execution surface directly. This import-boundary AST
guard (the scoped-factory / op-context single-source precedent) pins that the
client modules import NONE of that writer surface, so the future remote client
(P2) is single-writer-safe for free: it is the same client, a different
transport.

- ``stream_client`` is the fully-migrated reference client — it must import the
  transport seam AND none of the writer surface.
- ``inline/app`` is the interactive input driver — its send side routes through
  the transport; it likewise imports none of the writer surface (its status-bar
  READS reach the registry duck-typed, a P3 read-model concern, not an import).
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

# The writer surface the client must not import directly. ``runtime.outbox`` (the
# display DTO) and ``interfaces.repl.renderer`` are display types, NOT this set.
_FORBIDDEN_PREFIXES = (
    "reyn.runtime.session",
    "reyn.runtime.workspace",
    "reyn.tools",
    "reyn.core.op_runtime",
)

_CLIENT_MODULES = (
    "interfaces/repl/stream_client.py",
    "interfaces/inline/app.py",
)


def _imported_modules(rel: str) -> set[str]:
    """Every module imported anywhere in the file (top-level or function-local)."""
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module)
    return mods


def _forbidden_hits(rel: str) -> list[str]:
    return sorted(
        mod
        for mod in _imported_modules(rel)
        if any(mod == p or mod.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)
    )


def test_client_modules_do_not_import_writer_surface() -> None:
    """Tier 2: neither client module imports Session / Workspace / tools /
    op-execution — the single-writer import boundary. A new direct import of any
    writer module fails here, naming module:file."""
    offenders = {rel: _forbidden_hits(rel) for rel in _CLIENT_MODULES}
    offenders = {rel: hits for rel, hits in offenders.items() if hits}
    assert not offenders, (
        "client module(s) import the writer surface directly — breaks the P1 "
        f"single-writer boundary (route through the ClientTransport): {offenders}"
    )


def test_stream_client_uses_the_transport_seam() -> None:
    """Tier 2: positive guard — the reference client imports the ClientTransport
    seam, so the chokepoint is used, not bypassed."""
    mods = _imported_modules("interfaces/repl/stream_client.py")
    assert any(m.startswith("reyn.interfaces.transport") for m in mods), (
        "stream_client must consume its session through reyn.interfaces.transport"
    )
