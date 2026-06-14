"""Tool-use scheme abstraction (#1593) — pluggable tool presentation + dispatch.

The OS owns the **tool-use loop** (build presentation → call LLM → interpret →
execute → feed back → repeat). A **scheme** owns *how* tools are shown to the LLM
and *how* an LLM response becomes executed actions. Adding a competitor scheme
(enumerate-all, CodeAct, retrieval) = implement this protocol + register by name;
the OS never changes (P7 — the OS holds no scheme-specific concepts: no
``qualified_name``, no ``catalog``, no "code block").

A scheme provides four things, per the locked #1593 design:

1. ``build_presentation`` → the ``tools=`` payload + the SP-shaping inputs.
2. ``interpret``          → normalize the LLM output into a tagged ``Interpretation``
                            (``Execute`` / ``RePresent`` / ``CodeBlock``).
3. ``execute``            → run the interpretation (permission-gated).
4. ``format_feedback``    → turn results into the next round's LLM message(s).

PR-1 ships the full interface (all three ``Interpretation`` tags) but only the
``UniversalCategoryScheme`` (the current behaviour, moved behind the protocol),
which emits only ``Execute`` — so behaviour is byte-identical. enumerate-all (PR-2)
and CodeAct (PR-3) add ``RePresent`` / ``CodeBlock`` paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ToolUseLayer(str, Enum):
    """The layer a router loop runs for — each independently scheme-selectable
    (``tool_use: {chat, step, phase}``). The phase layer's op-catalog presentation
    is the phase scheme; chat/step default to universal-category."""

    CHAT = "chat"
    STEP = "step"
    PHASE = "phase"


@dataclass
class Presentation:
    """What a scheme shows the LLM: the ``tools=`` payload + SP-shaping inputs.

    PR-1: ``sp_params`` carries the parameters that drive the (still monolithic)
    ``build_system_prompt`` — byte-identical. PR-2 (enumerate-all) introduces a
    scheme-owned SP *fragment* once a scheme needs a divergent prompt.
    """

    llm_tools_payload: list[dict]
    sp_params: dict[str, Any] = field(default_factory=dict)


# ── Interpretation: the tagged union the OS loop dispatches on ──────────────


@dataclass
class Execute:
    """The LLM asked to run tool calls. ``actions`` carry **resolved effective
    names** (the scheme's ``interpret`` does salvage / unwrap), so the OS can
    apply its exclude policy *before* dispatch (pre-execute gate)."""

    actions: list[dict]


@dataclass
class RePresent:
    """The LLM's output is a refinement request (e.g. a retrieval search) — the OS
    re-calls ``build_presentation`` with the refinement and re-queries the LLM. Not
    emitted by universal-category (PR-1); used by a retrieval scheme (future)."""

    refinement: Any


@dataclass
class CodeBlock:
    """The LLM wrote a code snippet (CodeAct) — ``execute`` runs it in a sandbox
    exposing only permission-approved functions. Not emitted in PR-1; CodeAct is
    PR-3."""

    code: str


Interpretation = Execute | RePresent | CodeBlock


@dataclass
class ExecutionResult:
    """The outcome of executing an ``Interpretation`` — per-action tool results
    (JSON-serialisable dicts), consumed by ``format_feedback`` and by the OS loop's
    scheme-agnostic op-specific handling (plan / invoke_skill)."""

    tool_results: list[dict]


@dataclass
class ExecContext:
    """What ``execute`` needs from the OS to dispatch — the permission resolver +
    op handlers (so P5 governs every effect, unchanged), the OS-held tool-catalog
    projection (read by universal dispatch / salvage), and the sandbox (CodeAct).
    The OS assembles this from the running host; schemes never reach past it."""

    permission_resolver: Any = None
    op_handlers: Any = None
    tool_catalog: dict = field(default_factory=dict)
    sandbox: Any = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class ToolUseScheme(Protocol):
    """The pluggable tool-use scheme contract. The OS calls only these four; it
    holds no scheme-specific strings (P7). Schemes are selected per-layer by name
    from the registry."""

    name: str

    def build_presentation(self, available: Any, layer_ctx: Any) -> Presentation:
        """Build the ``tools=`` payload + SP-shaping inputs for the layer."""
        ...

    def interpret(self, llm_response: Any, *, tool_catalog: dict) -> Interpretation:
        """Normalize the LLM output into a tagged ``Interpretation`` (resolution +
        any de-dup happen here; for JSON schemes → ``Execute`` with resolved
        effective names)."""
        ...

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext) -> ExecutionResult:
        """Run the interpretation (permission-gated via ``exec_ctx``)."""
        ...

    def format_feedback(self, result: ExecutionResult) -> list[dict]:
        """Turn results into the next round's LLM message(s)."""
        ...


# ── registry (name → scheme) ────────────────────────────────────────────────

_SCHEMES: dict[str, ToolUseScheme] = {}


def register_scheme(scheme: ToolUseScheme) -> None:
    """Register a scheme by its ``name``. Idempotent (last wins)."""
    _SCHEMES[scheme.name] = scheme


def get_scheme(name: str) -> "ToolUseScheme | None":
    """Look up a registered scheme by name (None if absent)."""
    return _SCHEMES.get(name)


def registered_scheme_names() -> list[str]:
    """Sorted names of registered schemes (introspection / tests)."""
    return sorted(_SCHEMES)


# The default scheme name — universal-category, preserving today's behaviour when
# no per-layer config overrides it. The OS holds the *name* string (a config key),
# not scheme logic, so this stays P7-clean.
DEFAULT_SCHEME_NAME = "universal-category"


__all__ = [
    "ToolUseLayer", "Presentation", "Execute", "RePresent", "CodeBlock",
    "Interpretation", "ExecutionResult", "ExecContext", "ToolUseScheme",
    "register_scheme", "get_scheme", "registered_scheme_names",
    "DEFAULT_SCHEME_NAME",
]
