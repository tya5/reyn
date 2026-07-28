"""Tier 2: the #3376 P1 Exposure/Encoder seam under the tool-use cells.

Two claims are kept apart everywhere below, because they are different claims:
**the mechanism is correct**, and **production reaches the mechanism**. A guard
that is right but unwired passes the first and fails the second, and only the
second is what protects a running session.

Byte-identity of the four cells is not asserted here — it is the scaffolded
oracle's job (``tests/scaffold/test_tool_use_oracle_3376.py``), which compares a
fresh-process capture from a real ``SchemeOps`` against a recorded artifact.
This file pins the invariants the seam adds on top of that.

``SchemeOps`` is a Protocol the ``RouterLoop`` implements over a live host; the
per-cell Fakes below are the idiom the existing scheme tests already use, and
they are used only where the input under test (a provider-native catalog entry,
a colliding identifier set) is one a real router cannot be steered into
producing. The scheme, exposure, encoder and identifier map are all real.
"""
from __future__ import annotations

import dataclasses

import pytest

from reyn.runtime.router_tools import build_mcp_search_tool
from reyn.tools.encoders import (
    ContentFenceEncoder,
    ToolCallsEncoder,
    UnencodableExposure,
    build_actions_map,
    encoder_for_transport,
)
from reyn.tools.exposure import (
    DESCRIPTOR_KIND_FUNCTION,
    DESCRIPTOR_KIND_PROVIDER_NATIVE,
    Exposure,
    FunctionDescriptor,
    ProviderNativeDescriptor,
    descriptor_from_entry,
    descriptors_from_entries,
)
from reyn.tools.scheme import Presentation
from reyn.tools.schemes._enumerate_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    TOOL_CALLS_EXPOSURE_DEVIATION,
    build_enumerate_all_exposure,
)
from reyn.tools.schemes.codeact import CodeActScheme
from reyn.tools.schemes.enumerate_all import EnumerateAllScheme
from reyn.tools.transport import (
    Transport,
    resolve_scheme_for_transport,
    valid_scheme_transport_pairs,
)


def _nested(name: str, *, description: str = "", properties: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


class _Ops:
    """A protocol-conforming ``SchemeOps`` Fake with real callables and explicit
    returns (the idiom ``test_enumerate_all_scheme_1593`` / ``test_codeact_scheme_1593``
    already use) — never a mock. Only ``base_tools`` / ``catalog_entries`` are
    exercised; presentation is the whole subject here."""

    def __init__(self, *, base: list[dict] | None = None, catalog: list[dict] | None = None):
        self._base = base if base is not None else []
        self._catalog = catalog if catalog is not None else []

    def base_tools(self, available, layer_ctx) -> list[dict]:
        return list(self._base)

    async def catalog_entries(self) -> list[dict]:
        return list(self._catalog)


# ── the pair table: still a capability declaration, still fail-closed ─────────


def test_the_pair_table_survives_as_a_capability_declaration() -> None:
    """Tier 2: every registered cell resolves, and its transport has an encoder
    that declares at least one descriptor kind.

    The seam did not replace the pair table with "whatever composes without
    raising" — the two declarations coexist and say different things: the pair
    table says which CELLS exist, an encoder says what a TRANSPORT can encode.
    Vacuity guard: an empty registry would make every clause below true."""
    pairs = valid_scheme_transport_pairs()
    assert pairs, "the (scheme, transport) registry is empty — this arm would be vacuous"

    for scheme, transport in pairs:
        assert resolve_scheme_for_transport(scheme, transport)
        encoder = encoder_for_transport(transport)
        assert encoder.encodable_descriptor_kinds, (
            f"{transport.value} declares no encodable descriptor kind, so its cells "
            "would be valid only by the absence of an exception"
        )


def test_an_unregistered_cell_is_still_refused_fail_closed() -> None:
    """Tier 2: after the seam, an unregistered cell is still REFUSED.

    "A table exists" and "what is not in the table is rejected" are two
    assertions, and only the second one is fail-closedness. The hazard the seam
    introduces is exactly that a general (Exposure x Encoder) composition makes
    every cell mechanically encodable, so the table could survive while meaning
    nothing.

    The unregistered set is DERIVED — the full product of the registered scheme
    names and every ``Transport``, minus the registered cells — so a cell added
    to the table moves itself out of this arm rather than leaving a hand-written
    list stale. Vacuity guard: both the registered and the unregistered set must
    be non-empty, otherwise the loop below asserts nothing."""
    registered = set(valid_scheme_transport_pairs())
    schemes = {scheme for scheme, _ in registered}
    product = {(scheme, transport) for scheme in schemes for transport in Transport}
    unregistered = product - registered

    assert registered, "no registered cell — the complement below would be everything"
    assert unregistered, (
        "every (scheme, transport) is registered, so this arm inspects nothing. If "
        "that is genuinely true, the fail-closed claim needs a different witness."
    )

    for scheme, transport in sorted(unregistered, key=lambda p: (p[0], p[1].value)):
        with pytest.raises(ValueError, match=r"no \(scheme, transport\) registration"):
            resolve_scheme_for_transport(scheme, transport)

    # An entirely unknown presentation name is refused for EVERY transport, not
    # only for the ones that happen to have no encoder.
    for transport in Transport:
        with pytest.raises(ValueError):
            resolve_scheme_for_transport("no-such-presentation", transport)


# ── the descriptor union ─────────────────────────────────────────────────────


def test_a_function_entry_round_trips_through_the_descriptor_union() -> None:
    """Tier 2: mechanism — classifying then re-encoding a function entry returns
    the entry it started from, in the wire-form it arrived in.

    Both forms matter: the canonical nested one every production producer emits,
    and the bare flat one the catalog-shape projection has always tolerated. A
    descriptor that normalised the second into the first would rewrite a payload
    the seam is supposed to carry unchanged."""
    nested = _nested("file__read", description="Read a file.", properties={"path": {}})
    flat = {"name": "file__read", "description": "Read a file.", "parameters": {"properties": {}}}

    for entry in (nested, flat):
        descriptor = descriptor_from_entry(entry)
        assert descriptor.kind == DESCRIPTOR_KIND_FUNCTION
        assert descriptor.as_tool_calls_entry() == entry


def test_a_provider_native_entry_is_carried_verbatim_not_normalised() -> None:
    """Tier 2: mechanism — an entry that cannot be described as a function is
    carried through as a ``provider_native`` arm, byte-for-byte.

    The subject is a REAL production artifact: ``build_mcp_search_tool`` is what
    ``build_tools`` emits for the deferred-MCP-loading mode, and its entry has
    ``type: "tool_search_tool_20251101"`` with no ``function`` key at all. Squeezing
    that into a function descriptor would drop the wrapped tool list, so the union
    states in the type that it cannot be re-derived and hands back the original."""
    entry = build_mcp_search_tool([_nested("srv", description="a server")])
    assert entry.get("function") is None and entry["type"] != "function", (
        "the production meta-tool no longer has the shape this arm is about"
    )

    descriptor = descriptor_from_entry(entry)
    assert descriptor.kind == DESCRIPTOR_KIND_PROVIDER_NATIVE
    assert descriptor.as_tool_calls_entry() == entry
    assert ToolCallsEncoder().encode_tools(Exposure(descriptors=(descriptor,))) == [entry]


@pytest.mark.asyncio
async def test_production_cells_reach_the_descriptor_classifier() -> None:
    """Tier 2: production-reaches — the cells' payloads are what the classifier
    plus the encoder produce, not something a scheme assembled beside them.

    Asserted on the real ``EnumerateAllScheme``: every entry its presentation
    emits is the re-encoding of the descriptor its own source entry classifies
    to. If a cell went back to appending raw entries, its payload could still
    look right while nothing here had run — so the comparison is against the
    seam's output, entry by entry, over a source deliberately containing all
    three shapes (nested, flat, provider-native)."""
    provider_native = build_mcp_search_tool([_nested("srv")])
    catalog = [
        _nested("git__commit"),
        {"name": "flat__tool", "description": "d", "parameters": {"properties": {}}},
    ]
    base = [_nested("file__read"), provider_native]

    ops = _Ops(base=base, catalog=catalog)
    pres = await EnumerateAllScheme().build_presentation(
        {"hot_list_aliases": []}, {"search_visible": True}, ops,
    )
    descriptors = descriptors_from_entries(base + catalog)
    assert pres.llm_tools_payload == [d.as_tool_calls_entry() for d in descriptors]
    # Vacuity guard: the equality above must have spanned every descriptor shape,
    # otherwise it only proves the seam carries the easy one.
    assert {d.kind for d in descriptors} == {
        DESCRIPTOR_KIND_FUNCTION, DESCRIPTOR_KIND_PROVIDER_NATIVE,
    }
    assert {getattr(d, "wire_form", None) for d in descriptors} >= {"nested", "flat"}


# ── the capability declaration ───────────────────────────────────────────────


def test_content_fence_refuses_what_it_did_not_declare() -> None:
    """Tier 2: mechanism — the ``content_fence`` encoder raises on a
    ``provider_native`` descriptor and on scheme-owned slot text, rather than
    quietly emitting a code-API without them.

    A provider's own meta-tool has no rendering as a Python signature, and this
    transport has no positional prompt slots. Both are capability losses if they
    pass silently: the operator configured something the model never sees, and
    nothing anywhere says so. The assertions drive the encoder's real ``encode_*``
    methods — the same ones the cell calls — so a guard removed from those call
    sites turns this arm RED."""
    encoder = ContentFenceEncoder()
    assert DESCRIPTOR_KIND_PROVIDER_NATIVE not in encoder.encodable_descriptor_kinds

    native = Exposure(descriptors=(ProviderNativeDescriptor(payload=build_mcp_search_tool([])),))
    with pytest.raises(UnencodableExposure, match="provider_native"):
        encoder.encode_tool_use_sp(native)
    with pytest.raises(UnencodableExposure, match="provider_native"):
        encoder.encode_tools(native)

    slotted = Exposure(
        descriptors=(FunctionDescriptor(name="a", description="", parameters={}),),
        sp_slot_overrides={"slot_post_catalog": "search first"},
    )
    with pytest.raises(UnencodableExposure, match="slot_post_catalog"):
        encoder.encode_tool_use_sp(slotted)


@pytest.mark.asyncio
async def test_the_content_fence_cell_reaches_that_refusal() -> None:
    """Tier 2: production-reaches — the refusal above is on the CodeAct cell's
    own path, not merely on a helper nobody calls.

    Driven through the real ``CodeActScheme.build_presentation`` over a catalog
    carrying the real provider-native meta-tool. A real ``RouterLoop`` cannot be
    steered into emitting one (``RouterLoop.present``/``base_tools`` call
    ``build_tools`` with the default threshold of 0, which never selects the
    deferred-loading mode), which is why the catalog is supplied directly here —
    the scheme, the exposure builder and the encoder are all the real ones."""
    ops = _Ops(catalog=[_nested("git__commit"), build_mcp_search_tool([_nested("srv")])])
    with pytest.raises(UnencodableExposure, match="content_fence"):
        await CodeActScheme().build_presentation({}, {}, ops)


# ── the declared deviation (#3381's receptacle) ──────────────────────────────


def test_the_two_enumerate_all_cells_declare_different_exposure_sets() -> None:
    """Tier 2: mechanism — the deviation parameter is what decides whether base
    tools are exposed and which names are dropped, so #3381 is settled by a value.

    The two constants keep today's values; this arm proves the values are load
    bearing by building the same catalog under each of them and comparing the
    exposed sets. Vacuity guard: the base tool and the excluded name must both be
    present in the source, otherwise "absent from the result" means nothing."""
    base = [_nested("delegate_to_agent")]
    catalog = [_nested("git__commit"), _nested("mcp__call_tool")]
    ops = _Ops(base=base, catalog=catalog)

    def names(deviation) -> set[str]:
        exposure = build_enumerate_all_exposure(
            catalog_entries=catalog, available={}, layer_ctx={}, ops=ops, deviation=deviation,
        )
        return {d.name for d in exposure.descriptors}

    tool_calls = names(TOOL_CALLS_EXPOSURE_DEVIATION)
    content_fence = names(CONTENT_FENCE_EXPOSURE_DEVIATION)

    assert "delegate_to_agent" in {e["function"]["name"] for e in base}
    assert "mcp__call_tool" in {e["function"]["name"] for e in catalog}

    assert "delegate_to_agent" in tool_calls and "delegate_to_agent" not in content_fence
    assert "mcp__call_tool" not in tool_calls and "mcp__call_tool" in content_fence
    assert "git__commit" in tool_calls and "git__commit" in content_fence


@pytest.mark.asyncio
async def test_the_production_cells_carry_those_declarations_unchanged() -> None:
    """Tier 2: production-reaches — the values above are the ones today's cells
    run on, and their effect shows in what the two real schemes present.

    Keeping today's values is deliberate: the ``content_fence`` cell gaining base
    tools would add callables to CodeAct's system prompt, which is a behaviour
    change #3376 P1 excludes and #3381 owns."""
    assert TOOL_CALLS_EXPOSURE_DEVIATION.includes_base_tools is True
    assert TOOL_CALLS_EXPOSURE_DEVIATION.excluded_names == frozenset({"mcp__call_tool"})
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.includes_base_tools is False
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.excluded_names == frozenset()

    base = [_nested("delegate_to_agent")]
    catalog = [_nested("git__commit"), _nested("mcp__call_tool")]
    ops = _Ops(base=base, catalog=catalog)

    flat_cell = await EnumerateAllScheme().build_presentation(
        {"hot_list_aliases": []}, {}, ops,
    )
    fence_cell = await CodeActScheme().build_presentation({}, {}, ops)

    advertised = {e["function"]["name"] for e in flat_cell.llm_tools_payload}
    assert "delegate_to_agent" in advertised
    assert "mcp__call_tool" not in advertised
    assert "def delegate_to_agent(" not in fence_cell.tool_use_sp
    assert "def git__commit(" in fence_cell.tool_use_sp


# ── the identifier map is the executor's map ─────────────────────────────────


def test_the_code_api_identifiers_come_from_the_full_dispatchable_universe() -> None:
    """Tier 2: the rendered identifier for an action does not change when
    narrowing hides one of its colliding siblings.

    ``CodeActScheme.execute`` injects sandbox stubs named by
    ``build_actions_map`` over the FULL dispatchable catalog. If the code-API
    derived its identifiers from the narrowed subset instead, a collision suffix
    would shift and the model would call an identifier no stub answers to. So the
    map is the encoder's, computed over the exposure's dispatchable universe, and
    the universe is the pre-narrowing set."""
    colliding = ["a-b__x", "a.b__x"]
    universe = build_actions_map(colliding)
    idents = sorted(universe)
    assert idents == ["a_b__x", "a_b__x_2"], "the collision fixture stopped colliding"

    exposure = Exposure(
        # Only the SECOND of the colliding pair is exposed; both are dispatchable.
        descriptors=(FunctionDescriptor(name=colliding[1], description="", parameters={}),),
        dispatchable_names=tuple(colliding),
    )
    rendered = ContentFenceEncoder().encode_tool_use_sp(exposure)

    surviving = next(ident for ident, qn in universe.items() if qn == colliding[1])
    assert f"def {surviving}(" in rendered, (
        "the exposed action was rendered under an identifier the executor's stub "
        "map does not contain — the map was derived from the narrowed subset"
    )


def test_presentation_no_longer_carries_the_dead_sp_channel() -> None:
    """Tier 2: the ``Presentation`` contract is the seam's two channels only.

    ``sp_params`` had no production reader and ``sp_fragment`` had no production
    writer; a field with neither is not a channel, and carrying it into a new
    structure would make the new structure born owing something. Asserted on the
    real dataclass's field set rather than on an attribute lookup, so a
    reintroduction by any route is caught."""
    fields = {f.name for f in dataclasses.fields(Presentation)}
    assert "sp_params" not in fields
    assert "sp_fragment" not in fields
    assert {"llm_tools_payload", "tool_use_sp"} <= fields
