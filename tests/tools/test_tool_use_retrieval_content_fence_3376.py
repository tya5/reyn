"""Tier 2: the #3376 P3 ``(retrieval, content_fence)`` cell — the last one.

The cell exists because of one property, and the arms are arranged around it:
**``retrieval`` discovers by searching, never by listing**, and that has to
survive the transport change. A test that only checked "the cell produces
output" would pass on a version that rendered the flat catalog into the code-API
— which is ``enumerate-all`` under another name, so that is the version these
arms are built to fail.

The second property is the one that makes the pair worth filling at all: over
``tool_calls`` the narrowing costs a ``RePresent`` round trip, because a
``tools=`` payload can only change *between* LLM calls. Over ``content_fence``
the search result is an ordinary value inside the snippet, so the same paradigm
runs without one — and *cannot* use one, because this transport's whole surface
is the system prompt and the prompt is built once per turn. That asymmetry is
asserted, not merely described, so a future "unify the two cells" change has to
confront it.

Two claims are kept apart throughout, as in the P1/P2 siblings: **the mechanism
is correct**, and **production reaches the mechanism**. A property that holds in
a helper nobody calls protects nothing.

The Fake ``SchemeOps`` below is the idiom the existing scheme tests use (real
callables, explicit returns, never a mock) and appears only where the input under
test — a catalog grown to 300 entries, a contextual narrowing that hides a
wrapper — is one a real router cannot be steered into producing. The production
arms drive the real object graph instead: a real ``Session`` -> its
``RouterHostAdapter`` -> a real ``RouterLoop`` (which *is* the ``SchemeOps``
Protocol implementation) -> the registered scheme instance.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.effective import ContextualPermission
from reyn.tools.exposure import Exposure, ExposureDeviation
from reyn.tools.scheme import (
    AdvertisedTools,
    NoToolsChannel,
    Presentation,
    get_scheme,
)
from reyn.tools.schemes._content_fence_cell import ContentFenceCellScheme
from reyn.tools.schemes._retrieval_exposure import retrieval_sp_facts
from reyn.tools.schemes.retrieval import RetrievalScheme
from reyn.tools.schemes.retrieval_content_fence import (
    DEGRADED_EXPOSURE_DEVIATION,
    SEARCH_FIRST_EXPOSURE_DEVIATION,
    RetrievalContentFenceScheme,
    build_retrieval_content_fence_exposure,
)
from reyn.tools.transport import Transport, resolve_scheme_for_transport
from tests._support.agent_session import make_session

#: The code-API declares each callable as ``- `def <name>(...)```. Match the
#: DECLARATION rather than a bare substring: an action name also occurs inside
#: other entries' prose descriptions, where it is a cross-reference and not an
#: exposed callable.
_DECLARED_RE = re.compile(r"`def (\w+)\(")


def _declared(code_api: str) -> "set[str]":
    return set(_DECLARED_RE.findall(code_api))


def _nested(name: str, *, description: str = "", properties: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}},
        },
    }


#: What ``build_tools`` composes when the universal wrappers and the D14 search
#: gate are both on: a base tool plus the four catalog wrappers.
_WRAPPERS = [
    _nested("read_file", properties={"path": {}}),
    _nested("list_actions", properties={"category": {}}),
    _nested("search_actions", properties={"query": {}}),
    _nested("describe_action", properties={"action_name": {}}),
    _nested("invoke_action", properties={"action_name": {}, "args": {}}),
]

_BASE = [_nested("read_file", properties={"path": {}})]


class _Ops:
    """A protocol-conforming ``SchemeOps`` Fake with real callables and explicit
    returns — never a mock.

    ``present`` mirrors production's defining property: it returns the wrapper
    composition and does NOT consult the catalog, exactly as
    ``RouterLoop.present`` calls ``build_tools`` rather than ``catalog_entries``.
    ``catalog_entries`` returns the flat M-action catalog, so an implementation
    that reached for it in the search-first branch is immediately visible in the
    numbers."""

    def __init__(self, *, wrappers: "list[dict]", catalog: "list[dict]"):
        self._wrappers = wrappers
        self._catalog = catalog

    def present(self, available, layer_ctx) -> Presentation:
        return Presentation(tools_channel=AdvertisedTools(entries=list(self._wrappers)))

    def base_tools(self, available, layer_ctx) -> "list[dict]":
        return list(_BASE)

    async def catalog_entries(self) -> "list[dict]":
        return list(self._catalog)


def _catalog(size: int) -> "list[dict]":
    return [_nested(f"cat{i}__verb", description=f"action {i}") for i in range(size)]


_SEARCH_ON = {"search_visible": True}
_SEARCH_OFF = {"search_visible": False}


# ── discovery is a search, not a listing ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_code_api_leads_with_search_and_withholds_the_listing() -> None:
    """Tier 2: mechanism — the rendered code-API declares ``search_actions`` and
    does NOT declare ``list_actions``.

    This is ``retrieval``'s reason to exist, restated as a measurement, and it is
    what separates this cell from ``(category, content_fence)``: both keep the
    surface small, but ``category`` lets the model browse the catalog by name
    while this one requires it to describe what it wants. ``describe_action``
    stays because the model learns action NAMES from the search and still needs
    their argument schemas before ``invoke_action`` can be called correctly —
    asserted, so a future "tighten the surface" change cannot quietly strand the
    model with names it cannot call."""
    pres = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_ON), _Ops(wrappers=_WRAPPERS, catalog=_catalog(3)),
    )
    declared = _declared(pres.tool_use_sp)

    assert "search_actions" in declared, "the search affordance is not in the code-API"
    assert "list_actions" not in declared, (
        "the enumeration affordance is in the code-API — discovery in this "
        "presentation is a search, and a browse verb beside it gives the model "
        "the catalog listing the scheme exists to avoid"
    )
    assert {"describe_action", "invoke_action"} <= declared, (
        "the model can search but cannot inspect or call what it found"
    )


@pytest.mark.asyncio
async def test_the_code_api_does_not_grow_with_the_catalog() -> None:
    """Tier 2: mechanism — the code-API is identical over a 3-action and a
    300-action catalog.

    The failure this is built to catch is not a crash: a cell that composed the
    ``content_fence`` encoder over the flat catalog would resolve, render and run
    — and would be ``enumerate-all`` wearing another name, with a system prompt
    that grows without bound. Comparing the renders exactly (not their sizes)
    also catches a fold that held in count while the identities drifted.

    Vacuity guard: the 100x growth has to be visible somewhere, or the two
    renders could be equal because the Ops never differed."""
    small = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_ON), _Ops(wrappers=_WRAPPERS, catalog=_catalog(3)),
    )
    large = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_ON), _Ops(wrappers=_WRAPPERS, catalog=_catalog(300)),
    )
    assert small.tool_use_sp == large.tool_use_sp, (
        "the code-API changed when only the catalog grew — the exposed set is "
        "being derived from the catalog, so the retrieval narrowing is bypassed"
    )

    grew = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_OFF), _Ops(wrappers=_WRAPPERS, catalog=_catalog(300)),
    )
    assert len(_declared(grew.tool_use_sp)) > 300, (
        "the degraded branch over the same 300-action Ops did NOT grow either, so "
        "'the search-first surface does not grow' distinguishes nothing here"
    )


# ── the degrade, and why it is not silent ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dead_search_degrades_to_the_flat_catalog_instead_of_stranding() -> None:
    """Tier 2: mechanism — when the D14 gate says the search is unusable, the cell
    renders the base tools plus the flat catalog rather than a search that
    returns nothing.

    This is the #2895 shape from the ``tool_calls`` cell: presenting a search
    affordance backed by no index leaves the model looping on empty results with
    no other route to any action — a silent dead session, not an error. Over this
    transport the consequence would be worse, because the code-API is the ONLY
    tool-use surface: there is no ``tools=`` payload to fall back on."""
    ops = _Ops(wrappers=_WRAPPERS, catalog=_catalog(5))
    degraded = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_OFF), ops,
    )
    declared = _declared(degraded.tool_use_sp)

    assert {e["function"]["name"] for e in _catalog(5)} <= declared, (
        "the catalog is not reachable from the code-API and the search is dead — "
        "the model has no route to any action at all"
    )
    assert "read_file" in declared, "the base tools were dropped from the degrade"
    assert "search_actions" not in declared, (
        "a search the D14 gate reports as unusable is still advertised"
    )


# ── the dispatch gate is wider than the code-API, on purpose ─────────────────


@pytest.mark.asyncio
async def test_the_dispatch_gate_keeps_what_the_code_api_withholds() -> None:
    """Tier 2: mechanism — ``list_actions`` and a contextually-denied wrapper are
    absent from the code-API but present in the DISPATCHABLE set.

    Two separate reasons, both load-bearing. The encoder derives the code-API's
    identifiers from the exposure's dispatchable universe while ``execute``
    derives the sandbox stub names from ``dispatchable_catalog``; if the second
    were the narrowed list, a collision suffix could shift and the model would
    call an identifier no stub answers to. And a narrowed dispatch gate answers
    an in-code call with ``unknown_tool`` rather than the truthful
    ``tool_excluded`` (#1618 root-1) — the model then believes the action does
    not exist instead of learning it is denied."""
    contextual = ContextualPermission(tool_deny=frozenset({"describe_action"}))
    pres = await RetrievalContentFenceScheme().build_presentation(
        {"contextual_permission": contextual},
        dict(_SEARCH_ON),
        _Ops(wrappers=_WRAPPERS, catalog=_catalog(5)),
    )
    declared = _declared(pres.tool_use_sp)
    dispatchable = {e["function"]["name"] for e in pres.dispatchable_catalog}

    assert "describe_action" not in declared, "the narrowing did not reach the code-API"
    assert "invoke_action" in declared, "the narrowing removed more than it was asked to"
    assert {"list_actions", "describe_action"} <= dispatchable, (
        "a withheld wrapper was dropped from the dispatch gate too, so an in-code "
        "call to it now reports unknown_tool instead of tool_excluded"
    )


@pytest.mark.asyncio
async def test_a_withheld_row_does_not_shift_the_identifier_of_a_shown_one() -> None:
    """Tier 2: mechanism — the code-API's identifiers are computed over the FULL
    dispatchable universe, so withholding a row leaves every other identifier
    exactly where it was.

    This is the half of the pre-narrowing rule that the ``dispatchable_catalog``
    arm above does NOT cover, and the more dangerous half. ``build_actions_map``
    disambiguates collisions by sorted position (``x`` / ``x_2``), so if the map
    were built over the exposed subset instead, dropping ``list_actions`` would
    slide ``list~actions`` from ``list_actions_2`` up into ``list_actions`` — the
    identifier of the very action being withheld. The model would then call
    ``list_actions`` believing it got the shown one, and the sandbox stub (named
    from the full set) would route it to the other. Silent, and a permission-
    adjacent silence.

    The colliding pair is constructed rather than found: MCP names carrying
    non-identifier characters are exactly this shape, and no real router can be
    steered into producing a specific collision on demand."""
    from reyn.tools.encoders import build_actions_map

    colliding = [*_WRAPPERS, _nested("list~actions", properties={"category": {}})]
    ops = _Ops(wrappers=colliding, catalog=_catalog(3))

    full_map = build_actions_map([e["function"]["name"] for e in colliding])
    shifted = {ident for ident, qn in full_map.items() if qn == "list~actions"}
    assert shifted == {"list_actions_2"}, (
        f"the collision this arm depends on did not occur ({full_map!r}) — it "
        "would pass without witnessing anything"
    )

    pres = await RetrievalContentFenceScheme().build_presentation({}, dict(_SEARCH_ON), ops)
    declared = _declared(pres.tool_use_sp)
    assert "list_actions_2" in declared, (
        "the shown `list~actions` was renamed to `list_actions` — the identifier "
        "map was built over the exposed subset, so it now collides with the name "
        "of the action this cell withholds"
    )
    assert "list_actions" not in declared


@pytest.mark.asyncio
async def test_the_cell_produces_all_three_channels_of_a_presentation() -> None:
    """Tier 2: mechanism — NO ``tools=`` channel, a rendered code-API, and a
    dispatch catalog.

    ``tools_channel`` is ``NoToolsChannel`` — this transport's way of saying "the
    ``tools=`` field does not apply to me" (#3421 made that the value's type
    rather than a comment beside an empty list). It is the encoder's answer, not a
    literal in the cell. The third channel is the easy one to forget: with nothing
    advertised, a missing ``dispatchable_catalog`` would leave the gate keyed on
    nothing and every in-code call would come back ``unknown_tool``."""
    pres = await RetrievalContentFenceScheme().build_presentation(
        {}, dict(_SEARCH_ON), _Ops(wrappers=_WRAPPERS, catalog=_catalog(5)),
    )
    assert isinstance(pres.tools_channel, NoToolsChannel)
    assert isinstance(pres.tool_use_sp, str) and "def search_actions(" in pres.tool_use_sp
    assert pres.dispatchable_catalog is not None
    assert {e["function"]["name"] for e in pres.dispatchable_catalog} == {
        e["function"]["name"] for e in _WRAPPERS
    }


def test_a_deviation_that_cannot_be_honoured_is_refused() -> None:
    """Tier 2: mechanism — the exposure builder REFUSES
    ``includes_base_tools=False`` rather than accepting a declaration it cannot
    act on.

    Both branches receive their base tools inside one composed entry list, so
    this cell has no seam at which base tools could be dropped. Accepting the
    flag and ignoring it would leave a declaration that reads as a decision and
    does nothing — the failure mode ``ExposureDeviation`` exists to prevent."""
    with pytest.raises(ValueError, match="includes_base_tools"):
        build_retrieval_content_fence_exposure(
            entries=list(_WRAPPERS),
            available={},
            layer_ctx={},
            deviation=ExposureDeviation(includes_base_tools=False),
        )


def test_both_shipped_deviations_declare_what_the_builder_accepts() -> None:
    """Tier 2: production-reaches — the two shipped deviations satisfy the guard
    above and differ in exactly the one value they are meant to differ in.

    The pair is the record of a decision: the search-first branch withholds the
    enumeration affordance, the degraded branch withholds nothing because nothing
    is hidden behind a wrapper there."""
    for deviation in (SEARCH_FIRST_EXPOSURE_DEVIATION, DEGRADED_EXPOSURE_DEVIATION):
        assert deviation.includes_base_tools is True
        assert deviation.applies_contextual_narrowing is True
        assert deviation.rationale
    assert SEARCH_FIRST_EXPOSURE_DEVIATION.excluded_names == frozenset({"list_actions"})
    assert DEGRADED_EXPOSURE_DEVIATION.excluded_names == frozenset()


# ── the RePresent asymmetry, asserted rather than described ──────────────────


def test_this_cell_reaches_the_paradigm_without_a_represent_round() -> None:
    """Tier 2: mechanism — the ``content_fence`` cell classifies a fenced search
    call as a ``CodeBlock``, while its ``tool_calls`` sibling classifies the same
    intent as a ``RePresent``.

    Both are correct, and the difference is structural rather than stylistic. A
    ``tools=`` payload can only change between LLM calls, so the ``tool_calls``
    cell has to intercept the search and have the OS re-present. This transport's
    whole surface is the system prompt, which ``router_loop`` builds once per turn
    (``messages[0]`` is assembled before the iteration loop and the ``RePresent``
    arm swaps ``tools=`` and the dispatch catalog, never the prompt) — so a
    re-presented code-API would go nowhere, and does not need to: the search
    result is an ordinary value inside the snippet, one round trip cheaper.

    This is asserted so that a later "unify the two retrieval cells" change has
    to confront the asymmetry instead of discovering it in production."""
    from reyn.tools.scheme import CodeBlock, PlainText, RePresent

    class _FenceResp:
        content = '```python\nhits = search_actions(query="read a file")\n```'

    class _ToolCallResp:
        content = ""
        tool_calls = [
            {
                "id": "c1",
                "function": {
                    "name": "search_actions",
                    "arguments": '{"query": "read a file"}',
                },
            }
        ]

    fence = RetrievalContentFenceScheme().interpret(
        _FenceResp(), tool_catalog={}, ops=None,
    )
    assert isinstance(fence, CodeBlock) and "search_actions" in fence.code, (
        "the search call was not routed into the snippet, so the narrowing that "
        "this transport gets for free is not happening"
    )

    represented = RetrievalScheme().interpret(
        _ToolCallResp(), tool_catalog={}, ops=None,
    )
    assert isinstance(represented, RePresent), (
        "the tool_calls sibling no longer RePresents, so this arm is contrasting "
        "nothing — the asymmetry it documents has changed"
    )

    prose = RetrievalContentFenceScheme().interpret(
        _FenceResp.__class__("_P", (), {"content": "Here is the answer."})(),
        tool_catalog={},
        ops=None,
    )
    assert isinstance(prose, PlainText), (
        "a prose final answer is being run as code — the pre-#1618 shape in which "
        "a turn never terminates"
    )


def test_all_three_content_fence_cells_run_the_same_transport_implementation() -> None:
    """Tier 2: production-reaches — the registered ``content_fence`` cells are the
    SAME transport code with different exposures.

    Fence extraction, sandboxed execution through the OS per-call gate and the
    observation turn are properties of the transport, not of a presentation. Two
    copies would drift — and a drift in the execute path is a drift in a
    permission-gated path. Driven on the live registry entries rather than on the
    classes, so a cell registered under a hand-rolled duplicate fails here."""
    names = [
        resolve_scheme_for_transport(presentation, Transport.CONTENT_FENCE)
        for presentation in ("category", "enumerate-all", "retrieval")
    ]
    assert len(set(names)) == 3, "two presentations resolved to the same cell"
    for scheme_name in names:
        scheme = get_scheme(scheme_name)
        assert isinstance(scheme, ContentFenceCellScheme)
        assert type(scheme).interpret is ContentFenceCellScheme.interpret
        assert type(scheme).execute is ContentFenceCellScheme.execute
        assert type(scheme).format_feedback is ContentFenceCellScheme.format_feedback


# ── the presentation's facts have one source ─────────────────────────────────


@pytest.mark.asyncio
async def test_both_retrieval_cells_derive_their_sp_facts_from_one_place() -> None:
    """Tier 2: production-reaches — the ``tool_calls`` cell was MOVED onto the
    shared facts function, not left beside it.

    The two cells cannot share an exposure builder (see
    ``schemes._retrieval_exposure`` for why merging them would smuggle in a
    per-cell composer), but the ``sp_facts`` genuinely are a property of the
    presentation. A second copy would rot in a way nothing else catches: the
    ``content_fence`` encoder never reads ``sp_facts`` at all, so a drifted copy
    there changes no rendered output for as long as nobody looks.

    Asserted through the rendered surface rather than the facts dict: what the
    shared source has to produce is the ``tool_calls`` cell's actual system-prompt
    slots. ``slot_post_catalog`` is excluded because retrieval overrides it with
    its own search guidance, which is scheme-owned text rather than a derived
    fact."""
    from reyn.tools.encoders import encoder_for_transport

    available = {"hot_list_aliases": ["x"]}
    layer_ctx = {"search_visible": True, "router_model": "gpt-4o", "non_interactive": True}
    ops = _Ops(wrappers=_WRAPPERS, catalog=_catalog(3))

    rendered = (await RetrievalScheme().build_presentation(available, layer_ctx, ops)).tool_use_sp
    from_shared = encoder_for_transport(Transport.TOOL_CALLS).encode_tool_use_sp(
        Exposure(descriptors=(), sp_facts=retrieval_sp_facts(layer_ctx)),
    )
    assert from_shared, "the shared facts rendered no slots — this arm would be vacuous"
    assert {k: v for k, v in rendered.items() if k != "slot_post_catalog"} == {
        k: v for k, v in from_shared.items() if k != "slot_post_catalog"
    }, (
        "the tool_calls cell's system-prompt slots no longer match what the shared "
        "facts produce — it has grown a second, divergent copy"
    )

    exposure = build_retrieval_content_fence_exposure(
        entries=list(_WRAPPERS),
        available=available,
        layer_ctx=layer_ctx,
        deviation=SEARCH_FIRST_EXPOSURE_DEVIATION,
    )
    assert exposure.sp_facts == retrieval_sp_facts(layer_ctx)
    assert exposure.sp_facts["universal_wrappers_enabled"] is False, (
        "retrieval stopped declaring the universal wrapper block off, which is "
        "the fact that distinguishes its SP from category's"
    )


# ── production reaches all of it ─────────────────────────────────────────────


def test_the_cell_resolves_to_a_registered_live_scheme() -> None:
    """Tier 2: production-reaches — ``(retrieval, content_fence)`` resolves through
    the pair table to a scheme instance the OS can actually select.

    Resolution and registration are two facts: a name in the pair table that no
    module registers resolves fine and then fails at ``get_scheme``, which is a
    config-time promise the runtime cannot keep."""
    name = resolve_scheme_for_transport("retrieval", Transport.CONTENT_FENCE)
    scheme = get_scheme(name)
    assert isinstance(scheme, RetrievalContentFenceScheme)
    assert scheme.name == name


@pytest.mark.asyncio
async def test_production_withholds_the_listing_against_the_live_catalog() -> None:
    """Tier 2: production-reaches — the registered cell, driven by a real
    ``RouterLoop`` over a real ``Session``, renders a search-first surface whose
    functions are disjoint from the live catalog.

    No Fake anywhere in this arm: ``SchemeOps`` is a Protocol ``RouterLoop``
    itself implements over a live host, so ``present`` / ``catalog_entries`` here
    are the production ones. The disjointness assertion is the vacuity guard that
    keeps "small" from being trivially true, and it is stated against the live
    catalog rather than a hardcoded count so the arm keeps meaning as the base
    tool set changes."""
    session = make_session(
        agent_name="p3-cell-agent",
        state_log=StateLog(Path(tempfile.mkdtemp(prefix="reyn-3376-p3-")) / "state.wal"),
        snapshot_path=Path(tempfile.mkdtemp(prefix="reyn-3376-p3-")) / "snapshot.json",
    )
    host = session._router_host
    available = {"hot_list_aliases": [], "contextual_permission": None}
    layer_ctx = {
        "univ_enabled": True,
        "search_visible": True,
        "ctx_signal_present": False,
        "router_model": "gpt-4o",
        "router_model_family": "other",
        "non_interactive": False,
        "available_skills": None,
    }
    loop = RouterLoop(
        host=host,
        chain_id="p3-3376",
        router_model="gpt-4o",
        scheme_name=resolve_scheme_for_transport("retrieval", Transport.CONTENT_FENCE),
    )
    pres = await loop._scheme.build_presentation(dict(available), dict(layer_ctx), ops=loop)
    declared = _declared(pres.tool_use_sp)

    assert "search_actions" in declared and "list_actions" not in declared, (
        "the production cell is not search-first"
    )
    live_names = {
        (e.get("function") if isinstance(e.get("function"), dict) else e).get("name")
        for e in await loop.catalog_entries()
    }
    assert live_names, "the live catalog enumerated nothing, so this arm witnesses nothing"
    assert not (live_names & declared), (
        "a live catalog action is declared as a code-API function — the exposed set "
        f"is being derived from the catalog: {sorted(live_names & declared)}"
    )
