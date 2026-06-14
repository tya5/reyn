"""#1402: single-source scoped ChatSession construction.

Three frontends build a ``ChatSession`` with overlapping-but-divergent scoped
wiring:

- ``cli/commands/chat.py`` (chat-CLI / ``run-once``) — the full scoped set;
- ``web/deps.py`` (A2A) — partial (capabilities accreted one-by-one);
- ``cli/commands/mcp.py`` ``run_serve`` (stdio MCP) — partial.

A scoped capability hand-added to one factory silently leaked from the others —
the forwarding-gap class (sibling to base_dir #1410, permission-zone #1415,
exec-seam #1419, empty-stop #1424). This factory is the single chokepoint:

- the drift-prone **SCOPED** params are **required** keyword args (no default),
  so every frontend MUST pass them explicitly — ``None`` / off means "not used
  here" *documented*, never silently omitted. Adding a new scoped capability
  here forces all three factories to decide (completeness-by-construction);
- the common base params flow through ``**base`` so a non-scoped ``ChatSession``
  param can never drift between factories.

This is a **behavior-preserving** refactor (#1402 lead decision): each factory
passes its current explicit values, so runtime behaviour is unchanged. The
missing-capability gaps the divergence revealed (e.g. A2A lacks env-backend /
container-rooting) are an explicit-default-documented follow-up — a consumer
that needs one (e.g. an A2A SWE runner) flips that factory's default to a real
value in one line.

The multi-callsite invariant (no factory constructs ``ChatSession`` directly —
all route through here) is pinned by
``tests/test_scoped_session_factory_invariant_1402.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from reyn.chat.session import ChatSession

if TYPE_CHECKING:
    from pathlib import Path


def build_scoped_chat_session(
    *,
    # ── SCOPED capability surface (REQUIRED — no defaults) ──────────────────
    # The drift surface: every frontend MUST pass these explicitly. Add a field
    # here when a new scoped capability lands → all three factories are forced
    # to provide it (completeness-by-construction).
    environment_backend: Any,  # EnvironmentBackend | None — agent FS-seam backend instance
    sandbox_backend: Any,  # SandboxBackend | None — agent exec-seam backend instance
    workspace_base_dir: "Path | None",  # #187 chat OpContext FS root (container repo) / None=host cwd
    workspace_state_dir: "Path | None",  # #187 host-side OS state dir
    exclude_tools: "frozenset[str] | set[str] | None",  # #1400 tool names hidden + execution-blocked
    agent_id: str | None,  # FP-0016 agent-id-scoped memory
    router_max_iterations: int,  # #187 per-message tool-call budget
    non_interactive: bool,  # #1439 Fix #1: run-once (no TTY) → SP proceeds instead of asking a clarifying question. Per-frontend: chat-CLI = not isatty(); A2A/MCP/chainlit/dogfood = False (interactive byte-identical)
    eager_embedding_build: bool,  # build the action embedding index up-front
    allowed_mcp: list[str] | None,  # per-profile MCP allow-list
    # ── per-session config (required: should be UNIFORM across factories) ──
    # These are reyn.yaml-derived config every factory loads the same way; the
    # sandbox_config + multimodal_config drifts each previously needed a
    # dedicated uniformity point-test (now subsumed by the src/-wide invariant).
    sandbox_config: Any,  # SandboxConfig | None — exec-tool gating string
    multimodal_config: Any,  # MultimodalConfig | None
    action_retrieval_config: Any,  # ActionRetrievalConfig | None
    embedding_config: Any,  # EmbeddingConfig | None
    tool_calls_op_loop_skills: list[str] | None,  # #1212 op-loop gate
    chat_tool_use_scheme: str,  # #1593 PR-2 config.tool_use.chat — chat-layer ToolUseScheme name (UNIFORM: every frontend resolves the same reyn.yaml value)
    # ── common base (pass-through; session identity/infra, not a drift surface) ──
    **base: Any,
) -> ChatSession:
    """Construct a ``ChatSession`` with the scoped capability + per-session
    config surface passed explicitly. See module docstring for the drift-class
    rationale."""
    return ChatSession(
        environment_backend=environment_backend,
        sandbox_backend=sandbox_backend,
        workspace_base_dir=workspace_base_dir,
        workspace_state_dir=workspace_state_dir,
        exclude_tools=exclude_tools,
        agent_id=agent_id,
        router_max_iterations=router_max_iterations,
        non_interactive=non_interactive,
        eager_embedding_build=eager_embedding_build,
        allowed_mcp=allowed_mcp,
        sandbox_config=sandbox_config,
        multimodal_config=multimodal_config,
        action_retrieval_config=action_retrieval_config,
        embedding_config=embedding_config,
        tool_calls_op_loop_skills=tool_calls_op_loop_skills,
        chat_tool_use_scheme=chat_tool_use_scheme,
        **base,
    )
