"""ToolRegistry — collection of ToolDefinition by name.

Per ADR-0026 §2. The registry is the canonical source for both
router-style (build_tools) and phase-style (control_ir_executor)
dispatchers. M1 establishes the registry shape; M2/M3 migrate
capabilities into it; M4 sunsets the legacy dual-source structures.
"""
from __future__ import annotations

from typing import Iterator

from reyn.tools.types import ToolDefinition


class ToolRegistry:
    """Collection of ToolDefinitions keyed by canonical name.

    Read-mostly; ToolDefinitions are added at startup via register()
    and looked up at dispatch time via lookup() / by name iteration.
    No mutation after startup is expected; registry can be frozen
    via finalize() (future amendment) but M1 keeps it mutable for
    test ergonomics.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Add a tool to the registry. Re-registration with the same
        name raises (= prevents accidental shadowing)."""
        if tool.name in self._tools:
            raise ValueError(
                f"ToolDefinition with name {tool.name!r} already registered. "
                f"Re-registration is not allowed; remove the prior registration first."
            )
        self._tools[tool.name] = tool

    def lookup(self, name: str) -> ToolDefinition | None:
        """Find a tool by name. Returns None if not registered."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """List of all registered tool names."""
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    # Filtered iteration for dispatchers
    def for_router(self) -> list[ToolDefinition]:
        """Tools where gates.router == "allow"."""
        return [t for t in self._tools.values() if t.gates.router == "allow"]

    def for_phase(self) -> list[ToolDefinition]:
        """Tools where gates.phase == "allow"."""
        return [t for t in self._tools.values() if t.gates.phase == "allow"]
