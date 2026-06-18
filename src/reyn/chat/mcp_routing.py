"""FP-0043 Stage 4b-6: MCP-server-transport session routing (registry-only, unit-testable).

When an external MCP client invokes a Reyn agent (Reyn-as-MCP-server: `reyn mcp
serve`), the invocation runs on a SHARED "mcp" session per agent — isolated from
the user's "main" conversation, while preserving the continuity the request-response
model relies on (a partial send_to_agent leaves running_skills that the NEXT call
pumps via MessageBus.request — a single stable session per connection).

MCP is stdio: one external client per `reyn mcp serve` process, and the call-tool
handler carries no connection id (McpRef is per-REQUEST, not per-connection), so
"per-connection" collapses to one shared mcp session per process. A future SSE
multi-connection transport could refine to per-connection if a conn-id surfaces.

Unlike the unattended transports (cron/webhook), the MCP path drives the turn INLINE
(MessageBus.request; the MCP stdio SDK starves a background task) and returns the
reply in the tool response — so NO ``ensure_session_running`` and no new output
mechanism. Mirrors the a2a shared-session helper, minus the escalation machinery
(MCP send_to_agent is pure request-response — no Task escalation).
"""
from __future__ import annotations

MCP_TRANSPORT = "mcp"
# Constant native-id → one shared mcp session per agent (stdio = single connection
# per process). Per-connection is moot until a multi-connection transport surfaces.
MCP_NATIVE_ID = "mcp"


def mcp_session_id() -> str:
    """The logical session-id (routing-key) of the shared mcp session: ``mcp:mcp``."""
    return f"{MCP_TRANSPORT}:{MCP_NATIVE_ID}"


def resolve_mcp_session(registry, agent_name: str):
    """Resolve (get-or-spawn) the agent's shared mcp session.

    Idempotent — both MCP tools that touch a session (send_to_agent /
    answer_intervention) route through here so they act on the SAME mcp session
    (not "main"), keeping the request-response continuity intact. No
    ``ensure_session_running`` — the MCP handler drives the turn inline."""
    return registry.resolve_session(agent_name, MCP_TRANSPORT, MCP_NATIVE_ID)
