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


@dataclass(frozen=True)
class ExposureDeviation:
    """How one cell's exposed set differs from its scheme's other cells.

    The ``enumerate-all`` scheme spans two cells that do **not** share an
    exposure set: over ``tool_calls`` it composes the base tools with the
    catalog minus ``mcp__call_tool``; over ``content_fence`` it renders the
    catalog alone, so base tools such as ``delegate_to_agent`` are not callable
    from the code-API. Whether that is intended has never been written down
    anywhere, and it is tracked as #3381.

    This type is the receptacle: the difference is **declared** at each cell
    rather than being an unexplained divergence between two code paths, and the
    values stay exactly what they are today. Settling #3381 becomes a change of
    a value here, not a change of a code path."""

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
]
