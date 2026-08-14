"""Tool-use scheme abstraction (#1593) — pluggable tool presentation + dispatch.

The OS owns the **tool-use loop** (build presentation → call LLM → interpret →
execute → feed back → repeat). A **scheme** owns *how* tools are shown to the LLM
and *how* an LLM response becomes executed actions. Adding a competitor scheme
(enumerate-all, CodeAct, retrieval) = implement this protocol + register by name;
the OS never changes (P7 — the OS holds no scheme-specific concepts: no
``action_name``, no ``catalog``, no "code block").

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
    """The layer a router loop runs for. Scheme-selectable via ``tool_use.scheme``
    x ``tool_use.transport`` in reyn.yaml (FP-0066 P4b, #3247); the chat layer
    defaults to ``enumerate-all`` / ``tool_calls`` (#1657). #2768 removed the
    dead ``step`` / ``phase`` layers (phase-graph era, zero read sites —
    ``PhaseRouterLoopHost`` deleted #2438)."""

    CHAT = "chat"


# ── The ``tools=`` channel (#3421) ──────────────────────────────────────────
#
# A transport either HAS a ``tools=`` channel or it does not, and that is a
# different question from how many tools are in it. Expressing the second answer
# as an empty list made the two indistinguishable: ``[]`` read equally well as
# "this transport advertises nothing right now" (a real, reachable state — every
# tool narrowed away by permission) and as "this transport has no such channel at
# all" (``content_fence``, whose entire surface is the code-API in the system
# prompt). The distinction was stated in prose at three call sites and in the
# type at none, so no consumer could act on it.
#
# It is a ``{kind: ...}`` discriminated union for the same reason
# ``exposure.ToolDescriptor`` is: the arm a value is on is a fact the producer
# knows and the consumer must not re-derive by sniffing the value's shape.

TOOLS_CHANNEL_KIND_ADVERTISED = "advertised"
TOOLS_CHANNEL_KIND_ABSENT = "absent"


@dataclass(frozen=True)
class AdvertisedTools:
    """This transport HAS a ``tools=`` channel; ``entries`` is what is in it.

    ``entries == []`` is meaningful on this arm and does not mean the same thing
    as ``NoToolsChannel``: it says the channel exists and is currently empty (no
    tool survived exclusion / permission narrowing), which is a state the model
    can be shown and the operator can be told about."""

    entries: list[dict]
    kind: str = TOOLS_CHANNEL_KIND_ADVERTISED


@dataclass(frozen=True)
class NoToolsChannel:
    """This transport has NO ``tools=`` channel — the field does not apply.

    ``content_fence`` is the case: the model expresses a chosen action by writing
    a fenced snippet against the code-API in ``tool_use_sp``, and nothing is ever
    advertised through ``tools=``. A cell on this arm therefore cannot use
    advertisement as its dispatch gate, which is why ``Presentation`` requires it
    to carry an explicit ``dispatchable_catalog`` (#1618 root-1, now checked)."""

    kind: str = TOOLS_CHANNEL_KIND_ABSENT


ToolsChannel = AdvertisedTools | NoToolsChannel


def advertised_entries(channel: ToolsChannel) -> "list[dict]":
    """The entries that reach the provider's ``tools=`` argument — ``[]`` on the
    absent arm.

    This is the WIRE view, and the collapse is deliberate: an absent channel and
    an empty one send the same thing, so a consumer whose only job is to hand the
    payload to the provider (or to name what the model can see) is right to call
    this. A consumer that must tell the two apart branches on the arm instead —
    which is now possible, and is the whole point of the union."""
    if isinstance(channel, AdvertisedTools):
        return channel.entries
    return []


@dataclass
class Presentation:
    """What a scheme shows the LLM: the ``tools=`` channel + the tool-use SP.

    A scheme builds this by handing an ``Exposure`` (``reyn.tools.exposure`` —
    what is shown, transport-neutrally) to the encoder for its transport
    (``reyn.tools.encoders`` — how that transport writes it down). Both fields
    below are therefore encoder output, and the two channels differ by transport
    rather than by scheme: ``tool_calls`` fills ``tools_channel`` with
    ``AdvertisedTools`` and a positional slot-map; ``content_fence`` says
    ``NoToolsChannel`` and puts its whole surface in ``tool_use_sp``.
    """

    tools_channel: ToolsChannel
    # #1593 PR-4: the scheme's current candidate set (hashable ids) — the OS reads
    # this on the RePresent loop to detect convergence (``new = candidates - seen``;
    # empty ⇒ stop). Default empty: schemes that never RePresent (universal /
    # enumerate-all) leave it untouched, so it is inert for them.
    candidates: tuple = field(default_factory=tuple)
    # #1618 root-1: the scheme's DISPATCHABLE action set — the membership/resolution
    # gate for ANY call (JSON tool_call OR in-code ``tool()``), in the canonical
    # ``catalog_entries`` shape. Decoupled from ``tools_channel`` (what the LLM
    # is ADVERTISED): a scheme can advertise nothing yet dispatch everything (CodeAct
    # writes code, advertises ∅, dispatches the full catalog). Default ``None`` ⇒
    # dispatchable = advertised — byte-identical for universal / enumerate-all /
    # retrieval, whose three catalog notions coincide.
    dispatchable_catalog: "list[dict] | None" = None
    # #1618 root-2 / #1627 Stage 0 — positional slot-map for the tool-use SP regions.
    # ``None`` ⇒ the OS builds all three slots via build_universal_tool_use_slots
    # (universal / enumerate-all — BYTE-IDENTICAL).
    # ``str`` ⇒ back-compat shim: treated as ``{"slot_pre_environment": str}`` — the OS
    # injects it verbatim AT the R1 position and leaves R2/R3 absent (CodeAct path).
    # ``dict[str, str]`` ⇒ positional slot-map; keys are one or more of:
    #   - ``slot_pre_environment``  — R1: replaces ## Capabilities (routing guide).
    #   - ``slot_post_environment`` — R2: replaces ## Action categories + discovery mandate.
    #   - ``slot_in_behaviour``     — R3: replaces never-invent + ROUTING RULE inside ## Behaviour.
    # Absent keys → OS omits that region entirely. P7: the OS owns the region positions;
    # the content is scheme-owned. OS retains identity + errors-verbatim + non-tool routing.
    tool_use_sp: "dict[str, str] | str | None" = None

    def __post_init__(self) -> None:
        # #3421: a cell with no ``tools=`` channel cannot use advertisement as its
        # dispatch gate — there is nothing advertised to key on — so it MUST name
        # its dispatchable set explicitly. That was already true of every
        # ``content_fence`` cell by construction (#1618 root-1: "an empty
        # advertisement must not become an empty dispatch gate"); it is checkable
        # only now that "no channel" is a value rather than an empty list. Note
        # ``dispatchable_catalog=[]`` passes: a cell that genuinely dispatches
        # nothing says so, which is the same empty-vs-absent discipline one level
        # down.
        if isinstance(self.tools_channel, NoToolsChannel) and self.dispatchable_catalog is None:
            raise ValueError(
                "a Presentation on the NoToolsChannel arm must carry an explicit "
                "dispatchable_catalog: with no tools= channel there is no "
                "advertisement for the dispatch gate to fall back on, so "
                "dispatchable_catalog=None would silently gate every call against "
                "an empty set. Pass [] to mean 'dispatches nothing'."
            )


# ── Canonical catalog-shape projections (#1618 root-1) ──────────────────────
# ``catalog_entries`` (and an ``AdvertisedTools.entries`` payload) carry ONE canonical
# the OpenAI-nested ``{"type":"function","function":{"name","description",
# "parameters"}}``. The OS owns the projections every consumer needs, so no consumer
# hand-reads a nested dict at a guessed depth (the #1/#3 root: render + the exclude
# filter read ``entry["name"]`` top-level on a nested entry → empty / silent no-op).


def flat_catalog_entries(entries: "list[dict]") -> "list[dict]":
    """Project canonical (OpenAI-nested) entries → the FLAT ``{name, description,
    parameters}`` shape that the code-API render + the dispatch membership map read.
    Tolerates an already-flat entry (defensive). ``parameters`` is always a valid
    (possibly empty) JSON-schema object."""
    out: list[dict] = []
    for e in entries:
        fn = e.get("function") if isinstance(e.get("function"), dict) else e
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def dispatch_catalog_map(entries: "list[dict]") -> "dict[str, dict]":
    """Project canonical entries → the ``{name → canonical_entry}`` membership map the
    dispatch gate (``DispatchContext.tool_catalog``) checks. Keyed by the entry's
    function name."""
    out: dict[str, dict] = {}
    for e in entries:
        fn = e.get("function") if isinstance(e.get("function"), dict) else e
        name = fn.get("name")
        if name:
            out[name] = e
    return out


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
    scheme-agnostic op-specific handling (plan / op dispatch).

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

    - ``present``  → today's ``build_tools`` + SP params.
    - ``resolve``  → dedupe + salvage/unwrap → actions carrying **effective names**
      (so the OS can exclude-gate pre-dispatch).
    - ``dispatch`` → per-action ``dispatch_tool`` (DispatchContext / permission
      — the pure-OS substrate, P5).
    - ``feedback`` → the basic tool_result→message formatting (op-specific plan
      handling stays in the OS loop, around this).
    """

    def present(self, available: Any, layer_ctx: Any) -> Presentation: ...
    def resolve(self, llm_response: Any, tool_catalog: dict) -> list[dict]: ...
    async def dispatch(
        self, actions: list[dict], *, call_id: "str | None" = None,
    ) -> list[dict]: ...
    def feedback(self, result: "ExecutionResult") -> list[dict]: ...

    # Building blocks for SELF-CONTAINED schemes (#1593 PR-2) — a non-delegating
    # scheme composes its own presentation from these instead of calling the
    # whole-universal ``present``. The router (host-context holder) provides them
    # so schemes stay P7-clean. Additive → universal's delegation is unchanged
    # (no PR-1 regression).
    def base_tools(self, available: Any, layer_ctx: Any) -> list[dict]:
        """The prior-shape base tools (``build_tools`` with wrappers OFF): the
        common base every scheme starts from (agents/mcp/file/web)."""
        ...

    async def catalog_entries(self) -> list[dict]:
        """Every usable catalog action across all categories projected to a flat,
        directly-callable tool schema (qualified ``<category>__<entry>`` name) —
        what enumerate-all adds on top of ``base_tools`` instead of the wrappers.

        Async (#1593 PR-2 seam call): enumerating the live catalog requires the
        async-built router caller-state (resource categories — agents/mcp/rag
        — drop without it; the rag manifest fetch is the genuine await)."""
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
    holds no scheme-specific strings (P7). Schemes are selected by name
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


# The default scheme name — enumerate-all (#1657: owner default switch, the H1
# fix; enumerate-all flat-lists actions so the LLM invokes them directly instead
# of hallucinating invoke_action names → 30%→100% direct tool-use).
# universal-category remains available via config (tool_use.scheme) for many-tool /
# minimal-surface setups. The OS holds the *name* string (a config key), not
# scheme logic, so this stays P7-clean.
DEFAULT_SCHEME_NAME = "enumerate-all"


__all__ = [
    "TOOLS_CHANNEL_KIND_ABSENT", "TOOLS_CHANNEL_KIND_ADVERTISED",
    "AdvertisedTools", "NoToolsChannel", "ToolsChannel", "advertised_entries",
    "ToolUseLayer", "Presentation", "Execute", "RePresent", "CodeBlock",
    "Interpretation", "ExecutionResult", "ExecContext", "ToolUseScheme", "SchemeOps",
    "register_scheme", "get_scheme", "registered_scheme_names",
    "DEFAULT_SCHEME_NAME",
]
