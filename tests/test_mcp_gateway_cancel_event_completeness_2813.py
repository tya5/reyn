"""Tier 2: #2813 — every interactive-turn MCPGateway(...) construction must pass
``cancel_event=``, mirroring test_2421_mcp_seam_completeness.py's structural-gate
pattern (a grep-enforced completeness pin, not a one-shot audit).

Why this is needed (not just "nice to have"): #2813's own incident was exactly a
sibling-sweep-miss — the cancel_event race was proven for sandboxed_exec, but every
other bounded-call op handler silently missed it, including a brand-new MCP op
handler someone could add tomorrow. Per-call-site opt-in with no enforcement is the
whack-a-mole shape #2421's own docstring already names as the root cause it fixed for
MCPClient construction; this test closes the same class of gap for cancel_event.

Scope: only op_runtime/ and runtime/ (session.py) — the interactive-turn surfaces
where a cancel_event is actually available. interfaces/cli/commands/mcp.py's two
constructions are deliberately excluded: those are one-shot, non-interactive CLI
invocations (`reyn mcp probe` / `reyn mcp list-tools`) with no per-turn Session/
RouterLoopDriver to source a cancel_event from at all. tools/ (e.g.
tools/mcp_verbs.py) is OUT OF SCOPE for this scan by construction — every
MCPGateway(...) it needs is constructed inside op_runtime/mcp_install.py's
probe_mcp_server(), which this scan does cover; tools/ itself never constructs
a MCPGateway directly. If a future tools/ module starts constructing one
directly, this test's scoped_dirs list must be extended to include it, or the
gap goes unenforced.
"""
from __future__ import annotations

import re
from pathlib import Path

# One-shot, non-interactive CLI commands — no per-turn cancel_event exists to pass.
_ALLOWED = {"interfaces/cli/commands/mcp.py"}

_CONSTRUCT = re.compile(r"\bMCPGateway\s*\(")


def _find_call_spans(text: str) -> list[tuple[int, int, int]]:
    """Return (start_offset, end_offset, start_line) for each real MCPGateway(...)
    call in *text* — balances parens from the opening ``(`` to its matching close,
    so a multi-line call is captured as one span (unlike a per-line regex)."""
    spans: list[tuple[int, int, int]] = []
    for m in _CONSTRUCT.finditer(text):
        start = m.end() - 1  # position of the opening "("
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        start_line = text.count("\n", 0, m.start()) + 1
        spans.append((m.start(), i + 1, start_line))
    return spans


def test_every_mcpgateway_construction_passes_cancel_event():
    """Tier 2: RED if a new (or existing) MCPGateway(...) construction in
    op_runtime/ or runtime/ omits cancel_event= — the same structural-gate shape
    as test_2421_mcp_seam_completeness.py, applied to the #2813 cancel-race
    contract instead of the transport-construction contract."""
    root = Path(__file__).resolve().parents[1] / "src" / "reyn"
    scoped_dirs = [root / "core" / "op_runtime", root / "runtime"]
    offenders: list[str] = []
    for scoped in scoped_dirs:
        for py in scoped.rglob("*.py"):
            rel = py.relative_to(root).as_posix()
            if rel in _ALLOWED:
                continue
            text = py.read_text(encoding="utf-8")
            for start, end, line_no in _find_call_spans(text):
                call_text = text[start:end]
                if "cancel_event" not in call_text:
                    offenders.append(f"{rel}:{line_no}: {call_text.strip()[:80]}")
    assert not offenders, (
        "MCPGateway(...) constructed without cancel_event= — a Ctrl-C during this "
        "call will wait out the op's own internal timeout instead of interrupting "
        "immediately (#2813). Pass cancel_event=ctx.cancel_event (op_runtime) or "
        "cancel_event=self._loop_driver.cancel_event (session.py), or add this file "
        "to a documented allowlist if truly non-interactive:\n" + "\n".join(offenders)
    )
