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

    Two channels shape the system prompt, deliberately separated:

    - ``sp_params`` — **named gates** the OS-owned ``build_system_prompt`` already
      understands (``universal_wrappers_enabled`` / ``search_actions_enabled`` …).
      universal-category and enumerate-all express their whole SP shape through
      these → their build is byte-identical (default ``sp_fragment=""``).
    - ``sp_fragment`` — **free-form, scheme-owned** SP text the OS appends verbatim
      without interpreting it (P7: the OS must not know "code-API" or "search-SP").
      A scheme reaches for this only when its tool-use instructions are genuinely
      new content that no named gate can express — CodeAct (rendered fn-signature
      code-API) is the first consumer; retrieval's search-tool SP shares the same
      single channel. Empty by default ⇒ the named-gate path is untouched.
    """

    llm_tools_payload: list[dict]
    sp_params: dict[str, Any] = field(default_factory=dict)
    sp_fragment: str = ""
    # #1593 PR-4: the scheme's current candidate set (hashable ids) — the OS reads
    # this on the RePresent loop to detect convergence (``new = candidates - seen``;
    # empty ⇒ stop). Default empty: schemes that never RePresent (universal /
    # enumerate-all) leave it untouched, so it is inert for them.
    candidates: tuple = field(default_factory=tuple)


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


@dataclass
class PlainText:
    """The LLM's response carries no actionable operation — no tool call, no code
    block, no refinement request. It is a plain natural-language reply (the model is
    done). The OS routes it to the terminal text-reply path: the tool-round loop
    exits and ``llm_response.content`` becomes the turn reply.

    Dataless by design: ``interpret`` is a pure classifier — it does NOT copy the
    text into the member. The OS already holds the authoritative ``llm_response``
    when it calls ``interpret``; duplicating ``content`` here would invite drift over
    which copy is canonical. (#1593 Issue-2 seam ruling.)

    All three schemes emit it: universal-category (a plain answer, = today's
    empty-``tool_calls`` → text-reply, byte-identical), CodeAct (final text after N
    code rounds), retrieval (the model answers without searching)."""


Interpretation = Execute | RePresent | CodeBlock | PlainText


@dataclass
class ExecutionResult:
    """The outcome of executing an ``Interpretation`` — per-action tool results
    (JSON-serialisable dicts), consumed by ``format_feedback`` and by the OS loop's
    scheme-agnostic op-specific handling (plan / invoke_skill).

    ``tool_calls`` + ``assistant_content`` (#1608) enrich the result so a scheme's
    ``format_feedback`` can build the **full** appendable message sequence (the
    assistant tool-call turn + the per-result ``{role:tool, tool_call_id}`` messages)
    — moving the OS loop's former inline zip into the scheme (P7). Both default empty
    so non-Execute schemes (CodeAct reads only ``tool_results``) are unaffected;
    ``tool_calls[i]`` aligns with ``tool_results[i]`` (un-reordered — #1406/#187)."""

    tool_results: list[dict]
    tool_calls: list[dict] = field(default_factory=list)
    assistant_content: str = ""


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
class SchemeOps(Protocol):
    """Router-provided tool-use operations a **delegating** scheme calls.

    PR-1's ``UniversalCategoryScheme`` delegates to these (the router binds its
    existing universal-category logic) so the seam lands **byte-identical** — zero
    logic is physically moved. PR-2 (enumerate-all) / PR-3 (CodeAct) implement their
    own scheme logic instead of delegating, which is what proves the abstraction.
    Each op is the OS-substrate side of one scheme method:

    - ``present``  → today's ``build_tools`` + SP params (or the phase op-catalog).
    - ``resolve``  → dedupe + salvage/unwrap → actions carrying **effective names**
      (so the OS can exclude-gate pre-dispatch).
    - ``dispatch`` → per-action ``dispatch_tool`` (DispatchContext / phase-memo /
      permission — the pure-OS substrate, P5).
    - ``feedback`` → the basic tool_result→message formatting (op-specific plan /
      invoke_skill handling stays in the OS loop, around this).
    """

    def present(self, available: Any, layer_ctx: Any) -> Presentation: ...
    def resolve(self, llm_response: Any, tool_catalog: dict) -> list[dict]: ...
    async def dispatch(self, actions: list[dict]) -> list[dict]: ...
    def feedback(self, result: "ExecutionResult") -> list[dict]: ...

    # Building blocks for SELF-CONTAINED schemes (#1593 PR-2) — a non-delegating
    # scheme composes its own presentation from these instead of calling the
    # whole-universal ``present``. The router (host-context holder) provides them
    # so schemes stay P7-clean. Additive → universal's delegation is unchanged
    # (no PR-1 regression).
    def base_tools(self, available: Any, layer_ctx: Any) -> list[dict]:
        """The prior-shape base tools (``build_tools`` with wrappers OFF): the
        common base every scheme starts from (skills/agents/mcp/file/web)."""
        ...

    async def catalog_entries(self) -> list[dict]:
        """Every usable catalog action across all categories projected to a flat,
        directly-callable tool schema (qualified ``<category>__<entry>`` name) —
        what enumerate-all adds on top of ``base_tools`` instead of the wrappers.

        Async (#1593 PR-2 seam call): enumerating the live catalog requires the
        async-built router caller-state (resource categories — skills/agents/mcp/
        rag — drop without it; the rag manifest fetch is the genuine await)."""
        ...

    async def search_actions(self, query: str, *, top_k: int = 10) -> list[str]:
        """Rank usable actions by semantic match to ``query`` → matched qualified
        action names (#1593 PR-4 retrieval). Reuses ``ActionEmbeddingIndex.query``
        (embeds the dynamic query — async, the reason presentation is async). Returns
        ``[]`` when the index/provider is unavailable (degrade). A generic search
        building block — the OS holds no "retrieval" concept (P7)."""
        ...


@runtime_checkable
class ToolUseScheme(Protocol):
    """The pluggable tool-use scheme contract. The OS calls only these four; it
    holds no scheme-specific strings (P7). Schemes are selected per-layer by name
    from the registry. ``ops`` is the OS-substrate binding — a delegating scheme
    (PR-1 universal) uses it; a self-contained scheme (enumerate-all/CodeAct) ignores
    it."""

    name: str

    async def build_presentation(self, available: Any, layer_ctx: Any, ops: "SchemeOps") -> Presentation:
        """Build the ``tools=`` payload + SP-shaping inputs for the layer.

        Async (#1593 PR-2 seam call): presentation is I/O for every non-trivial
        scheme — enumerate-all awaits the live catalog, and PR-4 retrieval runs a
        per-round embedding query — so the contract is async even though PR-1
        universal's body stays a sync delegation (it just isn't awaited)."""
        ...

    def interpret(self, llm_response: Any, *, tool_catalog: dict, ops: "SchemeOps") -> Interpretation:
        """Normalize the LLM output into a tagged ``Interpretation`` (resolution +
        de-dup happen here; for JSON schemes → ``Execute`` with resolved effective
        names)."""
        ...

    async def execute(self, interp: Interpretation, exec_ctx: ExecContext, ops: "SchemeOps") -> ExecutionResult:
        """Run the interpretation (permission-gated via ``exec_ctx`` / ``ops``)."""
        ...

    def format_feedback(self, result: ExecutionResult, ops: "SchemeOps") -> list[dict]:
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
    "Interpretation", "ExecutionResult", "ExecContext", "ToolUseScheme", "SchemeOps",
    "register_scheme", "get_scheme", "registered_scheme_names",
    "DEFAULT_SCHEME_NAME",
]
