"""Tier 2: the #3376 P1 Exposure/Encoder seam under the tool-use cells.

Two claims are kept apart everywhere below, because they are different claims:
**the mechanism is correct**, and **production reaches the mechanism**. A guard
that is right but unwired passes the first and fails the second, and only the
second is what protects a running session.

Byte-identity of the pre-existing cells was never asserted here — it was the
scaffolded oracle's job, and that oracle was **deleted** when #3376 P3 registered
the last cell (a golden snapshot of current behaviour outlives its usefulness the
moment there is no migration left to measure, and then resists correct changes
instead of catching wrong ones). This file pins the invariants the seam adds,
which are the ones that outlive the arc.

``SchemeOps`` is a Protocol the ``RouterLoop`` implements over a live host; the
per-cell Fakes below are the idiom the existing scheme tests already use, and
they are used only where the input under test (a provider-native catalog entry,
a colliding identifier set) is one a real router cannot be steered into
producing. The scheme, exposure, encoder and identifier map are all real.
"""
from __future__ import annotations

import dataclasses
import re

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
    ExposureDeviation,
    FunctionDescriptor,
    ProviderNativeDescriptor,
    descriptor_from_entry,
    descriptors_from_entries,
)
from reyn.tools.scheme import (
    AdvertisedTools,
    Presentation,
    advertised_entries,
    get_scheme,
)
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
from tests._support.tool_use_negative_examples import NOT_A_PRESENTATION


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
    already use) — never a mock. Only the three composition ingredients are
    exercised; presentation is the whole subject here."""

    def __init__(self, *, base: list[dict] | None = None, catalog: list[dict] | None = None):
        self._base = base if base is not None else []
        self._catalog = catalog if catalog is not None else []

    def base_tools(self, available, layer_ctx) -> list[dict]:
        return list(self._base)

    async def catalog_entries(self) -> list[dict]:
        return list(self._catalog)

    def present(self, available, layer_ctx) -> Presentation:
        # The router's already-folded composition, which the ``category`` cells
        # build their exposure from. Its *content* is irrelevant to the arms that
        # use it here (they assert a relation between two derived sets, not the
        # set itself); what matters is that it is one list, composed once.
        return Presentation(tools_channel=AdvertisedTools(entries=list(self._base) + list(self._catalog)))


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

    ★ The witness comes from OUTSIDE the presentation axis, not from a gap
    inside it. Until #3376 P3 this arm derived its witness as the complement of
    the registered set, which read as drift-proof and was not: the arc's whole
    purpose was to register cells, so the complement was guaranteed to empty
    itself, and it did — P2 and P3 each falsified the witness the previous phase
    had chosen. A negative example must be something that cannot ENTER the set,
    not something that merely is not in it yet: ``NOT_A_PRESENTATION`` is not a
    name on the axis at all, so no future cell can register it.

    Vacuity guards, both needed: the witness must really be outside the
    namespace (otherwise this asserts that a registered cell is refused, which
    would be a bug in the assertion, not in the code), and ``Transport`` must be
    non-empty (otherwise the loop runs zero times). The derived complement is
    still exercised when it is non-empty — a new presentation name repopulates
    it — but nothing here depends on it any more."""
    registered = set(valid_scheme_transport_pairs())
    assert registered, "no registered cell — the pair table is empty"

    schemes = {scheme for scheme, _ in registered}
    assert NOT_A_PRESENTATION not in schemes, (
        f"{NOT_A_PRESENTATION!r} became a real presentation name, so it is no "
        "longer outside the namespace and cannot witness fail-closedness. Pick "
        "another name that is not on the axis — do not switch to an unregistered "
        "cell of a real presentation, which is what expired twice already."
    )
    assert list(Transport), "Transport is empty — the loop below would assert nothing"

    for transport in Transport:
        with pytest.raises(ValueError, match=r"no \(scheme, transport\) registration"):
            resolve_scheme_for_transport(NOT_A_PRESENTATION, transport)

    # Opportunistic, not load-bearing: today the presentation x transport product
    # is fully registered so this set is empty, and it refills the day either axis
    # grows. Deriving it keeps a new cell from having to be added by hand here.
    product = {(scheme, transport) for scheme in schemes for transport in Transport}
    for scheme, transport in sorted(product - registered, key=lambda p: (p[0], p[1].value)):
        with pytest.raises(ValueError, match=r"no \(scheme, transport\) registration"):
            resolve_scheme_for_transport(scheme, transport)


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
    assert advertised_entries(
        ToolCallsEncoder().encode_tools(Exposure(descriptors=(descriptor,)))
    ) == [entry]


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
    assert advertised_entries(pres.tools_channel) == [d.as_tool_calls_entry() for d in descriptors]
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


# ── the declared deviation (where #3381 was settled) ─────────────────────────


def test_the_two_enumerate_all_cells_expose_the_same_set() -> None:
    """Tier 2: mechanism — both ``enumerate-all`` cells resolve to one exposed
    set, and the deviation parameter is what decides it.

    #3381: the ``content_fence`` cell used to render the catalog alone, so no
    base tool was callable from the code-API while the same-named ``tool_calls``
    cell advertised them. The two deviations now agree on every field that
    selects the set. This arm proves the values are load bearing by building the
    same source under each of them and comparing. Vacuity guard: the base tool
    and the excluded name must both be present in the source, otherwise
    "absent from the result" means nothing."""
    base = [_nested("delegate_to_agent")]
    catalog = [_nested("git__commit"), _nested("mcp__call_tool")]
    ops = _Ops(base=base, catalog=catalog)

    def names(deviation) -> set[str]:
        exposure, _dispatchable = build_enumerate_all_exposure(
            catalog_entries=catalog, available={}, layer_ctx={}, ops=ops, deviation=deviation,
        )
        return {d.name for d in exposure.descriptors}

    assert "delegate_to_agent" in {e["function"]["name"] for e in base}
    assert "mcp__call_tool" in {e["function"]["name"] for e in catalog}

    tool_calls = names(TOOL_CALLS_EXPOSURE_DEVIATION)
    content_fence = names(CONTENT_FENCE_EXPOSURE_DEVIATION)

    assert tool_calls == content_fence == {"delegate_to_agent", "git__commit"}


def test_the_only_declared_difference_left_is_the_narrowing() -> None:
    """Tier 2: mechanism — the two deviations differ in exactly one field, and it
    is the one whose reason is a property of the transport.

    ``applies_contextual_narrowing`` is True only for ``content_fence`` because
    the OS's post-presentation ``apply_contextual_visibility`` acts on a
    ``tools=`` payload that transport does not have. Compared field by field over
    the real dataclass rather than by naming the two fields expected to match, so
    a *new* field that silently diverges is caught too; ``rationale`` is prose and
    is excluded by name."""
    differing = {
        f.name
        for f in dataclasses.fields(ExposureDeviation)
        if f.name != "rationale"
        and getattr(TOOL_CALLS_EXPOSURE_DEVIATION, f.name)
        != getattr(CONTENT_FENCE_EXPOSURE_DEVIATION, f.name)
    }
    assert differing == {"applies_contextual_narrowing"}
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.applies_contextual_narrowing is True


@pytest.mark.asyncio
async def test_the_production_cells_carry_those_declarations() -> None:
    """Tier 2: production-reaches — the values above are the ones today's cells
    run on, and their effect shows in what the two real schemes present.

    The ``content_fence`` assertions are the #3381 fix on its production path: a
    base tool is a declared function in the rendered code-API, and the excluded
    catalog wrapper is not."""
    assert TOOL_CALLS_EXPOSURE_DEVIATION.includes_base_tools is True
    assert TOOL_CALLS_EXPOSURE_DEVIATION.excluded_names == frozenset({"mcp__call_tool"})
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.includes_base_tools is True
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.excluded_names == frozenset({"mcp__call_tool"})

    base = [_nested("delegate_to_agent")]
    catalog = [_nested("git__commit"), _nested("mcp__call_tool")]
    ops = _Ops(base=base, catalog=catalog)

    flat_cell = await EnumerateAllScheme().build_presentation(
        {"hot_list_aliases": []}, {}, ops,
    )
    fence_cell = await CodeActScheme().build_presentation({}, {}, ops)

    advertised = {e["function"]["name"] for e in advertised_entries(flat_cell.tools_channel)}
    assert "delegate_to_agent" in advertised
    assert "mcp__call_tool" not in advertised
    assert "def delegate_to_agent(" in fence_cell.tool_use_sp
    assert "def git__commit(" in fence_cell.tool_use_sp
    assert "def mcp__call_tool(" not in fence_cell.tool_use_sp


@pytest.mark.asyncio
async def test_every_code_api_function_has_a_sandbox_stub_to_answer_it() -> None:
    """Tier 2: production-reaches — every function the code-API declares is a name
    ``execute`` builds a stub for, over the real registered ``content_fence`` cells.

    This is the invariant #3381's fix could most easily have broken: the encoder
    names the code-API's functions from ``Exposure.dispatchable_names`` while
    ``execute`` names the sandbox stubs from ``Presentation.dispatchable_catalog``,
    and the fix made the first of those wider than the cell's catalog. If the two
    were composed at two places, the model would read ``def delegate_to_agent(...)``
    and the OS gate would answer ``unknown_tool``. Driven per registered cell so a
    new ``content_fence`` cell is covered without being listed here; the excluded
    wrapper is asserted to be dispatchable-but-unrendered, which is the #1618
    root-1 contract (``tool_excluded``, not ``unknown_tool``)."""
    base = [_nested("delegate_to_agent")]
    catalog = [_nested("git__commit"), _nested("mcp__call_tool")]
    cells = [
        get_scheme(resolve_scheme_for_transport(scheme, transport))
        for scheme, transport in valid_scheme_transport_pairs()
        if transport is Transport.CONTENT_FENCE
    ]
    assert cells, "no content_fence cell is registered — this arm would be vacuous"

    for cell in cells:
        ops = _Ops(base=base, catalog=catalog)
        pres = await cell.build_presentation({"hot_list_aliases": []}, {}, ops)
        stub_names = set(
            build_actions_map(
                [e["function"]["name"] for e in pres.dispatchable_catalog],
            ).keys()
        )
        declared = set(re.findall(r"`def (\w+)\(", pres.tool_use_sp))
        assert declared, f"{type(cell).__name__} declared no callable — arm is vacuous"
        assert declared <= stub_names, (
            f"{type(cell).__name__} renders {sorted(declared - stub_names)} in its "
            "code-API, but execute() builds no sandbox stub of that name — the "
            "exposed set and the dispatchable set were composed separately"
        )

    fence_cell = await CodeActScheme().build_presentation(
        {"hot_list_aliases": []}, {}, _Ops(base=base, catalog=catalog),
    )
    dispatchable = {e["function"]["name"] for e in fence_cell.dispatchable_catalog}
    assert "mcp__call_tool" in dispatchable and "delegate_to_agent" in dispatchable


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
    assert {"tools_channel", "tool_use_sp"} <= fields
