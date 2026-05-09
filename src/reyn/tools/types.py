"""Type definitions for the unified tool registry (ADR-0026 M1).

ToolDefinition is the single source of truth for a capability's
identity, metadata, gates, and handler. ToolGates encodes the
per-protocol allow/deny declaration. ToolContext is the
protocol-agnostic execution context handed to handlers; per-protocol
dispatchers build it before invocation. ToolHandler is the async
callable signature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol


# ToolGates: per-protocol allow/deny gate at the registry level.
# This is Layer 1 of the 3-layer gate model (= ADR-0026 §3):
#   Layer 1: role gate (this dataclass)
#   Layer 2: phase narrowing (Phase.allowed_ops)
#   Layer 3: permission resolver (per-call runtime)
@dataclass(frozen=True)
class ToolGates:
    router: Literal["allow", "deny"] = "allow"
    phase:  Literal["allow", "deny"] = "allow"


# ToolResult: canonical result shape returned by handlers. Each
# protocol-specific dispatcher adapts this shape to its own surface
# (= router serializes to JSON string for tool_result content;
# phase wraps in {kind, status} envelope for control_ir_results).
# Handler returns whatever Mapping[str, Any] makes semantic sense
# for the capability; dispatcher does the shape adaptation.
ToolResult = Mapping[str, Any]


# ToolContext: protocol-agnostic execution context. Built by the
# dispatcher (router or phase) before invoking the handler.
# Universal fields: events / permission_resolver / workspace.
# Per-protocol-specific state can be accessed via caller_kind branching;
# future iterations may introduce caller-specific sub-objects per
# ADR-0026 Open Question #3 recommendation.
@dataclass
class ToolContext:
    events: Any                                      # EventLog
    permission_resolver: Any | None                  # PermissionResolver
    workspace: Any                                   # Workspace
    caller_kind: Literal["router", "phase"]
    # Per-protocol-specific state (= ADR-0026 Open Question #3):
    # Today these are loose Any types; future amendment may
    # introduce typed sub-objects (e.g., RouterCallerState,
    # PhaseCallerState).
    router_state: Any = None                         # chain_id, etc. for router callers
    phase_state: Any = None                          # skill_run_id, run_visit_count, etc. for phase callers


# ToolHandler: async callable signature.
# Returns canonical ToolResult; raises on error (dispatcher wraps).
class ToolHandler(Protocol):
    async def __call__(
        self,
        args: Mapping[str, Any],
        ctx: ToolContext,
    ) -> ToolResult: ...


@dataclass(frozen=True)
class ToolDefinition:
    """Single source of truth for a capability exposed to both
    router-style and phase-style LLM invocations.

    Per ADR-0026 §2. Held in a ToolRegistry; rendered to OpenAI tools[]
    via render_for_router(); rendered to Control IR
    available_control_ops via render_for_phase().
    """
    # Identity
    name: str                                        # canonical name (= ADR-0026 Open Question #6)
    description: str                                 # LLM-facing description
    parameters: Mapping[str, Any]                    # JSON schema (object root)

    # Gating
    gates: ToolGates

    # Implementation
    handler: ToolHandler

    # Metadata
    category: str                                    # = "io" / "discovery" / "memory" / etc.
    purity: Literal["pure", "side_effect", "read_only", "world_pure"] = "side_effect"

    # Future metadata anchors (commented out; surface as needed):
    # cost_weight: float = 1.0
    # rate_limit_class: str | None = None
    # log_redaction: tuple[str, ...] = ()

    # Protocol-specific renders
    def render_for_router(self) -> dict:
        """Render to OpenAI tools[] entry shape used by call_llm_tools.

        Identical structure to the existing ToolSpec.to_openai_dict().
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }

    def render_for_phase(self) -> dict:
        """Render to a Control IR available_control_ops entry shape.

        Mirrors the structure that
        kernel/control_ir_executor.py::_build_phase_tool_catalog
        produces today. Phase-side dispatch uses this when constructing
        the phase context's available_control_ops list.
        """
        return {
            "kind": self.name,
            "description": self.description,
            "args_schema": dict(self.parameters),
            "purity": self.purity,
        }
