"""Exposure — the transport-neutral half of the tool-use presentation seam.

An **Exposure** answers *what* a scheme shows the LLM: a set of typed
descriptors, the raw facts a transport needs to shape the tool-use system
prompt, and an explicit declaration of where this cell deviates from the other
cells of the same scheme. It answers nothing about *how* any of that is
written down — that is the ``reyn.tools.encoders`` half, one implementation per
``Transport``.

**The boundary test**: if the answer changes when the transport changes, it
belongs to the Encoder; if it does not, it belongs to the Exposure. So the
excluded-name set and the permission narrowing are Exposure (they decide *what*
is shown, and are the same answer under either transport), while the
``tool_use_sp`` *value* and the identifier-collision map are Encoder — the
identifier map in particular has to agree with the one ``CodeActScheme.execute``
computes for the sandbox stubs, which makes it an encoding concern shared with
the executor rather than a property of the exposed set.

A descriptor is a ``{kind: ...}`` discriminated union. The ``function`` arm
carries a tool that round-trips losslessly through an OpenAI ``tools=`` entry.
Anything that does not — an Anthropic ``tool_search_tool_20251101`` meta-tool,
which ``build_tools`` emits when it is given a non-default
``mcp_search_threshold`` and which has no ``function`` key at all — becomes a
``provider_native`` arm carrying the entry verbatim. Normalising such an entry
into a function shape would silently drop it on a non-default configuration, so
the union says in the type that it cannot be re-derived.

The classification is **conservative**: an entry counts as ``function`` only
when its keys are exactly one of the two wire-forms below over exactly
``{name, description, parameters}``. Any additional or missing key makes it a
passthrough, so a shape this module does not model cannot be lossily rebuilt.
The wire-form a function arrived in is remembered, so re-encoding reproduces the
entry it came from rather than a normalised cousin of it — the flat form is
tolerated on the way in (an old defensive allowance) and must therefore also be
honoured on the way out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The two descriptor arms. An encoder declares which of these it can encode
#: (``reyn.tools.encoders``); an undeclared kind is refused, never dropped.
DESCRIPTOR_KIND_FUNCTION = "function"
DESCRIPTOR_KIND_PROVIDER_NATIVE = "provider_native"

_FUNCTION_ENTRY_KEYS = frozenset({"type", "function"})
_FUNCTION_BODY_KEYS = frozenset({"name", "description", "parameters"})

#: The two wire-forms a function entry is written in. ``nested`` is the
#: canonical OpenAI shape every production producer emits; ``flat`` is the
#: bare ``{name, description, parameters}`` the catalog-shape projection has
#: always tolerated, kept here so a flat entry re-encodes as a flat entry.
WIRE_FORM_NESTED = "nested"
WIRE_FORM_FLAT = "flat"


@dataclass(frozen=True)
class FunctionDescriptor:
    """A callable the LLM may invoke, in the transport-neutral form every
    encoder understands: a name, one line of documentation, and a JSON-schema
    parameter object. Round-trips losslessly to the ``tools=`` entry it came
    from, ``wire_form`` being the part of that entry which is not content."""

    name: str
    description: str
    parameters: dict
    wire_form: str = WIRE_FORM_NESTED
    kind: str = DESCRIPTOR_KIND_FUNCTION

    def as_tool_calls_entry(self) -> dict:
        """The ``tools=`` entry this descriptor came from, wire-form included."""
        body = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.wire_form == WIRE_FORM_FLAT:
            return body
        return {"type": "function", "function": body}


@dataclass(frozen=True)
class ProviderNativeDescriptor:
    """A provider-native tool entry carried **verbatim**.

    Reached when an entry cannot be described by ``FunctionDescriptor`` without
    loss (Anthropic's ``tool_search_tool_20251101`` meta-tool has a ``tools``
    array and no ``function`` key). Only a transport whose encoder declares this
    kind can emit it; the others refuse rather than drop it, which is the whole
    reason the arm is typed instead of being normalised away."""

    payload: dict
    kind: str = DESCRIPTOR_KIND_PROVIDER_NATIVE

    def as_tool_calls_entry(self) -> dict:
        return self.payload


ToolDescriptor = FunctionDescriptor | ProviderNativeDescriptor


def descriptor_name(descriptor: ToolDescriptor) -> str:
    """The exposed name of a descriptor, whichever arm it is."""
    if isinstance(descriptor, FunctionDescriptor):
        return descriptor.name
    return str(descriptor.payload.get("name", ""))


def descriptor_from_entry(entry: dict) -> ToolDescriptor:
    """Classify one ``tools=``-shaped entry into the descriptor union."""
    body = entry.get("function")
    if (
        entry.get("type") == "function"
        and isinstance(body, dict)
        and set(entry) == _FUNCTION_ENTRY_KEYS
        and set(body) == _FUNCTION_BODY_KEYS
    ):
        return FunctionDescriptor(
            name=body["name"],
            description=body["description"],
            parameters=body["parameters"],
            wire_form=WIRE_FORM_NESTED,
        )
    if set(entry) == _FUNCTION_BODY_KEYS:
        return FunctionDescriptor(
            name=entry["name"],
            description=entry["description"],
            parameters=entry["parameters"],
            wire_form=WIRE_FORM_FLAT,
        )
    return ProviderNativeDescriptor(payload=entry)


def descriptors_from_entries(entries: "list[dict]") -> "tuple[ToolDescriptor, ...]":
    """Classify a whole ``tools=`` payload, order preserved."""
    return tuple(descriptor_from_entry(entry) for entry in entries)


def _exposed_entry_name(entry: dict) -> str:
    body = entry.get("function")
    return str((body if isinstance(body, dict) else entry).get("name", ""))


def without_duplicate_names(entries: "list[dict]") -> "list[dict]":
    """Keep the FIRST entry for each tool name; drop later repeats of that name.
    Order preserved; nothing else is touched.

    A cell that composes ``base_tools`` with the flat catalog offers the same
    operation twice — the base tools and the catalog both carry ``read_file``,
    ``call_mcp_tool``, and so on. Measured on #3428: 12 such pairs under a
    default host config, 18 under a maximal one, i.e. up to 18 declarations the
    model is shown twice on **every** turn.

    **The base tools' entry is the one kept**, because callers compose
    ``base_tools`` first, and that ordering is load-bearing rather than
    incidental: an MCP tool's base schema carries the live ``enum`` of server/
    tool names (``tools/mcp.py``'s ``_enrich_router_schema``) while the
    catalog projection of the same tool does not, so keeping the catalog's row
    instead would let a model name a server/tool that does not exist.

    #3429 is why this is a plain name dedup. It used to be an *alias* dedup:
    the two rows carried two DIFFERENT names for one operation (``read_file``
    and ``file__read``), so removing one meant resolving the second spelling
    through ``invoke_action``'s alias table and choosing which spelling to keep —
    a choice with three measured behavioural consequences (result normalisation,
    canonicalization, and the peer ``enum`` above), because the two spellings
    were not interchangeable at dispatch. With one name per operation the two
    rows are literally the same name, the choice collapses into "keep the first",
    and the alias table is gone.

    ★ **This narrows the ADVERTISEMENT, not the executor's universe.** Callers
    apply it to the exposed list only — ``Exposure.dispatchable_names`` is
    composed before it — so the dropped row changes nothing about what can be
    dispatched.

    Two DIFFERENT registered definitions offering the same capability are
    invisible here and must stay hand-declared: ``mcp_call_tool`` and
    ``call_mcp_tool`` are separate ToolDefinitions with separate names, so no
    name comparison can pair them (that pair is the ``excluded_names`` entry in
    ``schemes._enumerate_exposure``)."""
    seen: "set[str]" = set()
    kept: "list[dict]" = []
    for entry in entries:
        name = _exposed_entry_name(entry)
        if name and name in seen:
            continue
        if name:
            seen.add(name)
        kept.append(entry)
    return kept


@dataclass(frozen=True)
class ExposureDeviation:
    """How one cell's exposed set differs from its scheme's other cells.

    Every difference between the cells of one scheme is **declared** here rather
    than being an unexplained divergence between two code paths, which is what
    makes a difference reviewable: a cell that deviates has to say so and say
    why, and a cell that does not deviate cannot drift into deviating quietly.

    It earned that job on #3381. The ``enumerate-all`` scheme's two cells did not
    share an exposure set — ``tool_calls`` composed the base tools with the
    catalog, ``content_fence`` rendered the catalog alone — and no line anywhere
    stated it as a decision. Declaring it here (#3376 P1) turned the question
    into "is this value right?", and settling it was then a change of these
    values, not of a code path. Today both cells declare
    ``includes_base_tools=True`` with the same exclusion, and differ only in
    ``applies_contextual_narrowing``, whose reason is a property of the
    transport (see ``schemes._enumerate_exposure``)."""

    includes_base_tools: bool
    excluded_names: frozenset = frozenset()
    applies_contextual_narrowing: bool = False
    rationale: str = ""


@dataclass(frozen=True)
class Exposure:
    """What a scheme shows, before any transport has had an opinion about it.

    ``sp_facts`` are the raw booleans and registry snapshots a transport needs
    to build tool-use system-prompt text — facts, never rendered text.
    ``sp_slot_overrides`` is scheme-owned prompt text destined for a named
    positional slot (retrieval's search guidance); an encoder that has no slot
    positions refuses a non-empty override rather than dropping it.

    ``dispatchable_names`` is the full set of names the executor can dispatch
    for this cell, *before* exclusion and permission narrowing. It is a fact,
    which is why it lives here — but the identifier map derived from it is an
    encoding, which is why that lives in the encoder, where it stays identical
    to the map ``CodeActScheme.execute`` builds for the sandbox stubs."""

    descriptors: "tuple[ToolDescriptor, ...]"
    sp_facts: "dict[str, Any]" = field(default_factory=dict)
    sp_slot_overrides: "dict[str, str]" = field(default_factory=dict)
    dispatchable_names: "tuple[str, ...]" = ()
    deviation: "ExposureDeviation | None" = None


__all__ = [
    "DESCRIPTOR_KIND_FUNCTION",
    "DESCRIPTOR_KIND_PROVIDER_NATIVE",
    "WIRE_FORM_FLAT",
    "WIRE_FORM_NESTED",
    "Exposure",
    "ExposureDeviation",
    "FunctionDescriptor",
    "ProviderNativeDescriptor",
    "ToolDescriptor",
    "descriptor_from_entry",
    "descriptor_name",
    "descriptors_from_entries",
    "without_duplicate_names",
]
