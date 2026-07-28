"""Tier 2: the #3376 P2 ``(category, content_fence)`` cell.

The cell exists because of one invariant, and everything below is arranged
around it: **``category`` folds the catalog into a handful of wrapper functions,
and the fold has to survive the transport change.** A test that only checked
"the cell produces output" would pass on a version that quietly enumerated every
action into the code-API — which is the version that has no reason to exist, so
that is the version these arms are built to fail.

Two claims are kept apart throughout, as in the P1 sibling
(``test_tool_use_exposure_encoder_3376.py``): **the mechanism is correct**, and
**production reaches the mechanism**. A fold that holds in a helper nobody calls
protects nothing.

Byte-identity of the pre-existing cells was never asserted here — that was the
scaffolded oracle's job, and it was deleted when #3376 P3 registered the last
cell. What remains is what should: the invariants this cell has to keep, which
are not a snapshot of what it happened to render on the day it landed.

The Fake ``SchemeOps`` below is the idiom the existing scheme tests use (real
callables, explicit returns, never a mock) and appears only where the input under
test — a catalog grown to 300 entries, a contextual narrowing that hides a
wrapper — is one a real router cannot be steered into producing. The two
production arms drive the real object graph instead: a real ``Session`` → its
``RouterHostAdapter`` → a real ``RouterLoop`` (which *is* the ``SchemeOps``
Protocol implementation) → the registered scheme instance.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.effective import ContextualPermission
from reyn.tools.scheme import (
    AdvertisedTools,
    NoToolsChannel,
    Presentation,
    advertised_entries,
    get_scheme,
)
from reyn.tools.schemes._category_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    TOOL_CALLS_EXPOSURE_DEVIATION,
    build_category_exposure,
)
from reyn.tools.schemes._content_fence_cell import ContentFenceCellScheme
from reyn.tools.schemes.category_content_fence import CategoryContentFenceScheme
from reyn.tools.schemes.codeact import CodeActScheme
from reyn.tools.schemes.universal_category import UniversalCategoryScheme
from reyn.tools.transport import Transport, resolve_scheme_for_transport
from tests._support.agent_session import make_session

#: The code-API declares each callable as ``- `def <name>(...)```. Match the
#: DECLARATION rather than a bare substring: an action name also occurs inside
#: other entries' prose descriptions, where it is a cross-reference and not an
#: exposed callable (the same trap the oracle scaffold names).
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


#: What ``build_tools`` composes for the ``category`` presentation: the base
#: tools plus the three catalog wrappers. Size is a property of the scheme, not
#: of the catalog — which is the whole claim under test.
_WRAPPERS = [
    _nested("present"),
    _nested("list_actions", properties={"category": {}}),
    _nested("describe_action", properties={"action_name": {}}),
    _nested("invoke_action", properties={"action_name": {}, "args": {}}),
]


class _Ops:
    """A protocol-conforming ``SchemeOps`` Fake with real callables and explicit
    returns — never a mock.

    ``present`` mirrors production's defining property: it returns the folded
    wrapper composition and does NOT consult the catalog, exactly as
    ``RouterLoop.present`` calls ``build_tools`` (whose output is base tools +
    wrappers) rather than ``catalog_entries``. ``catalog_entries`` returns the
    flat M-action catalog, so an implementation that reached for it instead is
    immediately visible in the numbers."""

    def __init__(self, *, wrappers: "list[dict]", catalog: "list[dict]"):
        self._wrappers = wrappers
        self._catalog = catalog

    def present(self, available, layer_ctx) -> Presentation:
        return Presentation(tools_channel=AdvertisedTools(entries=list(self._wrappers)))

    def base_tools(self, available, layer_ctx) -> "list[dict]":
        return list(self._wrappers)

    async def catalog_entries(self) -> "list[dict]":
        return list(self._catalog)


def _catalog(size: int) -> "list[dict]":
    return [_nested(f"cat{i}__verb", description=f"action {i}") for i in range(size)]


# ── the fold: the code-API is N wrappers, not M actions ──────────────────────


@pytest.mark.asyncio
async def test_the_code_api_does_not_grow_with_the_catalog() -> None:
    """Tier 2: mechanism — the rendered code-API declares the same functions
    whether the catalog holds 3 actions or 300.

    This is ``category``'s reason to exist, restated as a measurement. The
    failure it is built to catch is not a crash: a cell that composed the
    ``content_fence`` encoder over the FLAT catalog would resolve, render, and
    run — and would silently be ``enumerate-all`` wearing another name, with a
    system prompt that grows without bound. Comparing the two renders exactly
    (not merely their sizes) also catches a fold that held in count while the
    identities drifted."""
    small = CategoryContentFenceScheme()
    large = CategoryContentFenceScheme()

    small_api = (await small.build_presentation(
        {}, {}, _Ops(wrappers=_WRAPPERS, catalog=_catalog(3)),
    )).tool_use_sp
    large_api = (await large.build_presentation(
        {}, {}, _Ops(wrappers=_WRAPPERS, catalog=_catalog(300)),
    )).tool_use_sp

    assert _declared(small_api) == {e["function"]["name"] for e in _WRAPPERS}
    assert _declared(small_api) == _declared(large_api)
    assert small_api == large_api, (
        "the code-API changed when only the catalog grew — the exposed set is "
        "being derived from the catalog, so the category fold has been bypassed"
    )

    # Vacuity guard: the 100x growth has to be visible SOMEWHERE, or the two
    # renders could be equal because the Ops never differed in the first place.
    assert len(await _Ops(wrappers=_WRAPPERS, catalog=_catalog(300)).catalog_entries()) == 300
    enumerating = await CodeActScheme().build_presentation(
        {}, {}, _Ops(wrappers=_WRAPPERS, catalog=_catalog(300)),
    )
    # A superset, not an equality: the enumerate-all cell also exposes the base
    # tools (#3381), which this Fake supplies as ``_WRAPPERS``. What has to hold
    # for the contrast to mean anything is that all 300 catalog actions arrived.
    assert _declared(enumerating.tool_use_sp) >= {e["function"]["name"] for e in _catalog(300)}, (
        "the enumerate-all cell over the same Ops did NOT grow with the catalog, "
        "so 'category does not grow' distinguishes nothing here"
    )


def test_the_fold_lives_in_the_exposure_so_no_encoder_can_undo_it() -> None:
    """Tier 2: mechanism — the exposure the ``category`` cells hand their encoders
    already carries only the wrappers, for BOTH transports.

    The architect's placement ruling ("if the answer changes when the transport
    changes it belongs to the Encoder; if not, to the Exposure") is what makes
    the cell composable at all: the fold is a property of what is shown, so it
    is decided once, transport-neutrally, and each encoder renders the folded
    set in its own notation. Asserting it on the exposure — rather than only on
    the rendered string — is what pins the fold to the layer that cannot be
    swapped out per transport."""
    for deviation in (TOOL_CALLS_EXPOSURE_DEVIATION, CONTENT_FENCE_EXPOSURE_DEVIATION):
        exposure = build_category_exposure(
            present_entries=list(_WRAPPERS),
            available={},
            layer_ctx={},
            deviation=deviation,
        )
        assert {d.name for d in exposure.descriptors} == {
            e["function"]["name"] for e in _WRAPPERS
        }


@pytest.mark.asyncio
async def test_production_folds_against_the_live_catalog() -> None:
    """Tier 2: production-reaches — the registered cell, driven by a real
    ``RouterLoop`` over a real ``Session``, renders the SAME small set its
    ``tool_calls`` sibling advertises while the live catalog is many times larger.

    No Fake anywhere in this arm: ``SchemeOps`` is a Protocol ``RouterLoop``
    itself implements over a live host, so ``present`` / ``catalog_entries`` here
    are the production ones. The comparison is against the sibling cell rather
    than against a hardcoded count, so the arm keeps meaning as the base-tool set
    changes; the ratio assertion is the vacuity guard that keeps "small" from
    being trivially true."""
    session = make_session(
        agent_name="p2-cell-agent",
        state_log=StateLog(Path(_tmpdir()) / "state.wal"),
        snapshot_path=Path(_tmpdir()) / "snapshot.json",
    )
    host = session._router_host
    available = {"hot_list_aliases": [], "contextual_permission": None}
    layer_ctx = {
        "univ_enabled": True,
        "search_visible": False,
        "ctx_signal_present": False,
        "router_model": "gpt-4o",
        "router_model_family": "other",
        "non_interactive": False,
        "available_skills": None,
    }

    def _loop(scheme_name: str) -> RouterLoop:
        return RouterLoop(
            host=host, chain_id="p2-3376", router_model="gpt-4o", scheme_name=scheme_name,
        )

    fence_loop = _loop(resolve_scheme_for_transport("category", Transport.CONTENT_FENCE))
    calls_loop = _loop(resolve_scheme_for_transport("category", Transport.TOOL_CALLS))

    fence = await fence_loop._scheme.build_presentation(
        dict(available), dict(layer_ctx), ops=fence_loop,
    )
    calls = await calls_loop._scheme.build_presentation(
        dict(available), dict(layer_ctx), ops=calls_loop,
    )

    advertised = {e["function"]["name"] for e in advertised_entries(calls.tools_channel)}
    declared = _declared(fence.tool_use_sp)
    assert declared == advertised, (
        "the two category cells no longer show the same set — the transport "
        "change altered WHAT is exposed, which is the exposure's decision"
    )

    live_names = {
        (e.get("function") if isinstance(e.get("function"), dict) else e).get("name")
        for e in await fence_loop.catalog_entries()
    }
    assert live_names, (
        "the live catalog enumerated nothing, so this arm cannot witness a fold"
    )
    assert not (live_names & declared), (
        "a live catalog action is declared as a code-API function — the exposed set "
        f"is being derived from the catalog: {sorted(live_names & declared)}"
    )


def _tmpdir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="reyn-3376-p2-")


# ── the cell itself ──────────────────────────────────────────────────────────


def test_the_cell_resolves_to_a_registered_live_scheme() -> None:
    """Tier 2: production-reaches — ``(category, content_fence)`` resolves through
    the pair table to a scheme instance the OS can actually select.

    Resolution and registration are two facts: a name in the pair table that no
    module registers resolves fine and then fails at ``get_scheme``, which is a
    config-time promise the runtime cannot keep."""
    name = resolve_scheme_for_transport("category", Transport.CONTENT_FENCE)
    scheme = get_scheme(name)
    assert isinstance(scheme, CategoryContentFenceScheme)
    assert scheme.name == name


@pytest.mark.asyncio
async def test_the_cell_produces_both_channels_of_a_working_presentation() -> None:
    """Tier 2: mechanism — the presentation declares NO ``tools=`` channel, a
    rendered code-API, and a dispatch catalog.

    ``tools_channel`` is ``NoToolsChannel`` — this transport's way of saying "the
    ``tools=`` field does not apply to me", which #3421 moved out of a comment and
    into the type so it stops reading like "there happen to be zero tools". It is
    the encoder's answer, not a literal in the cell. The third channel is the one
    that would be easy to forget: with nothing advertised, a missing
    ``dispatchable_catalog`` would leave the gate keyed on nothing and every
    in-code call would come back ``unknown_tool``."""
    pres = await CategoryContentFenceScheme().build_presentation(
        {}, {}, _Ops(wrappers=_WRAPPERS, catalog=_catalog(5)),
    )
    assert isinstance(pres.tools_channel, NoToolsChannel)
    assert isinstance(pres.tool_use_sp, str) and "def invoke_action(" in pres.tool_use_sp
    assert pres.dispatchable_catalog is not None
    assert {e["function"]["name"] for e in pres.dispatchable_catalog} == {
        e["function"]["name"] for e in _WRAPPERS
    }


@pytest.mark.asyncio
async def test_the_dispatch_gate_keeps_a_narrowed_wrapper() -> None:
    """Tier 2: mechanism — contextual narrowing hides a wrapper from the code-API
    but leaves it in the DISPATCHABLE set.

    Two separate reasons, both load bearing. The encoder derives the code-API's
    identifiers from the exposure's dispatchable universe while ``execute``
    derives the sandbox stub names from ``dispatchable_catalog``; if the second
    were the narrowed list, a collision suffix could shift and the model would
    call an identifier no stub answers to. And a narrowed dispatch gate answers
    an in-code call to a hidden action with ``unknown_tool`` rather than the
    truthful ``tool_excluded`` (#1618 root-1) — the model then believes the
    action does not exist instead of learning it is denied."""
    contextual = ContextualPermission(tool_deny=frozenset({"describe_action"}))
    pres = await CategoryContentFenceScheme().build_presentation(
        {"contextual_permission": contextual},
        {},
        _Ops(wrappers=_WRAPPERS, catalog=_catalog(5)),
    )
    declared = _declared(pres.tool_use_sp)
    assert "describe_action" not in declared, "the narrowing did not reach the code-API"
    assert "invoke_action" in declared, "the narrowing removed more than it was asked to"
    assert "describe_action" in {
        e["function"]["name"] for e in pres.dispatchable_catalog
    }, (
        "the narrowed wrapper was dropped from the dispatch gate too, so an "
        "in-code call to it now reports unknown_tool instead of tool_excluded"
    )


def test_a_deviation_that_cannot_be_honoured_is_refused() -> None:
    """Tier 2: mechanism — the category exposure builder REFUSES
    ``includes_base_tools=False`` rather than accepting a declaration it cannot act on.

    ``ops.present`` composes the base tools and the wrappers inside one
    ``build_tools`` call, so unlike ``enumerate-all`` — which composes them itself
    and can honour either value — this presentation has no seam at which base
    tools could be dropped. Accepting the flag and ignoring it would leave a
    declaration that reads as a decision and does nothing, which is the failure
    mode the ``ExposureDeviation`` type exists to prevent."""
    from reyn.tools.exposure import ExposureDeviation

    with pytest.raises(ValueError, match="includes_base_tools"):
        build_category_exposure(
            present_entries=list(_WRAPPERS),
            available={},
            layer_ctx={},
            deviation=ExposureDeviation(includes_base_tools=False),
        )


def test_both_production_category_deviations_declare_what_the_builder_accepts() -> None:
    """Tier 2: production-reaches — the two shipped deviations satisfy the guard
    above, and differ in exactly the one value they are meant to differ in.

    The pair is the record of a decision: the ``content_fence`` cell applies the
    narrowing itself because its whole surface is a string and the OS's
    post-presentation ``tools=`` filter has nothing to act on; the ``tool_calls``
    cell does not, because that filter already runs."""
    for deviation in (TOOL_CALLS_EXPOSURE_DEVIATION, CONTENT_FENCE_EXPOSURE_DEVIATION):
        assert deviation.includes_base_tools is True
        assert deviation.excluded_names == frozenset()
        assert deviation.rationale
    assert TOOL_CALLS_EXPOSURE_DEVIATION.applies_contextual_narrowing is False
    assert CONTENT_FENCE_EXPOSURE_DEVIATION.applies_contextual_narrowing is True


# ── the transport is shared, not copied ──────────────────────────────────────


def test_both_content_fence_cells_run_the_same_transport_implementation() -> None:
    """Tier 2: production-reaches — the two registered ``content_fence`` cells are
    the SAME transport code with different exposures.

    Fence extraction, sandboxed execution through the OS per-call gate and the
    observation turn are properties of the transport, not of a presentation. Two
    copies would drift — and a drift in the execute path is a drift in a
    permission-gated path. Driven on the live registry entries rather than on the
    classes, so a cell registered under a hand-rolled duplicate fails here."""
    for scheme_name in (
        resolve_scheme_for_transport("category", Transport.CONTENT_FENCE),
        resolve_scheme_for_transport("enumerate-all", Transport.CONTENT_FENCE),
    ):
        scheme = get_scheme(scheme_name)
        assert isinstance(scheme, ContentFenceCellScheme)
        assert type(scheme).interpret is ContentFenceCellScheme.interpret
        assert type(scheme).execute is ContentFenceCellScheme.execute
        assert type(scheme).format_feedback is ContentFenceCellScheme.format_feedback


def test_the_new_cell_interprets_a_fenced_snippet_as_a_code_block() -> None:
    """Tier 2: mechanism-reaches — the inherited classifier is live on the new
    cell: a fenced snippet becomes a ``CodeBlock`` and prose stays terminal.

    ``isinstance`` on the base class says the method was inherited; this says it
    answers. The prose arm is the half that matters operationally — without it a
    plain final answer would be run as code, which is how the pre-#1618 loop
    failed to terminate."""
    from reyn.tools.scheme import CodeBlock, PlainText

    scheme = CategoryContentFenceScheme()

    class _Resp:
        def __init__(self, content: str) -> None:
            self.content = content

    fenced = scheme.interpret(
        _Resp('```python\nresult = invoke_action(action_name="file__read", args={})\n```'),
        tool_catalog={},
        ops=None,
    )
    assert isinstance(fenced, CodeBlock) and "invoke_action" in fenced.code

    prose = scheme.interpret(_Resp("Here is the answer."), tool_catalog={}, ops=None)
    assert isinstance(prose, PlainText)


def test_the_tool_calls_category_cell_still_runs_the_shared_exposure() -> None:
    """Tier 2: production-reaches — the pre-existing ``(category, tool_calls)``
    cell was MOVED onto the shared exposure builder, not left beside it.

    If it had kept its own inline copy, the two cells could disagree about what
    ``category`` exposes while every arm above still passed. The oracle proves
    the move changed no bytes; this proves the move happened at all — the
    payload the real scheme emits is exactly the shared builder's descriptor set,
    re-encoded."""
    ops = _Ops(wrappers=_WRAPPERS, catalog=_catalog(50))
    exposure = build_category_exposure(
        present_entries=advertised_entries(ops.present({}, {}).tools_channel),
        available={"hot_list_aliases": []},
        layer_ctx={"univ_enabled": True},
        deviation=TOOL_CALLS_EXPOSURE_DEVIATION,
    )
    pres = asyncio.run(
        UniversalCategoryScheme().build_presentation(
            {"hot_list_aliases": []}, {"univ_enabled": True}, ops,
        )
    )
    assert advertised_entries(pres.tools_channel) == [d.as_tool_calls_entry() for d in exposure.descriptors]
    assert advertised_entries(pres.tools_channel) == _WRAPPERS
