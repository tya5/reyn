"""Tier 1 + Tier 2: no cell advertises one operation under both of its spellings.

``base_tools`` and the universal catalog are two populations that name many of
the same operations differently — ``read_file`` and ``file__read``,
``delegate_to_agent`` and ``multi_agent__delegate``. A cell that composes both
therefore shows the model up to 18 declarations twice, on every turn (#3428).

Two claims are kept apart, as in the #3376 cell tests: **the mechanism is
correct** (``without_duplicate_alias_spellings`` drops what it should and only
what it should), and **production reaches it** — the second driven by a real
``Session`` -> ``RouterHostAdapter`` -> ``RouterLoop`` (which *is* the
``SchemeOps`` Protocol implementation) -> the registered scheme instance, with
the cells ENUMERATED FROM THE PAIR REGISTRY rather than hand-listed, so a cell
added later cannot reintroduce the duplication unnoticed.

The registry arm carries its own vacuity guard: it first witnesses that the
pre-dedup composition (``base_tools`` + ``catalog_entries``) really does contain
a duplicated pair. Without that, "no cell duplicates" would also be satisfied by
a session in which the two populations happened not to overlap at all.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.exposure import without_duplicate_alias_spellings
from reyn.tools.scheme import advertised_entries, get_scheme
from reyn.tools.schemes._enumerate_exposure import (
    CONTENT_FENCE_EXPOSURE_DEVIATION,
    build_enumerate_all_exposure,
)
from reyn.tools.transport import (
    Transport,
    resolve_scheme_for_transport,
    valid_scheme_transport_pairs,
)
from reyn.tools.universal_dispatch import unwrapped_tool_name
from tests._support.agent_session import make_session

#: The code-API declares each callable as ``- `def <name>(...)```. Match the
#: DECLARATION, not a bare substring: a name also occurs inside other entries'
#: prose descriptions, where it is a cross-reference and not an exposed callable.
_DECLARED_RE = re.compile(r"`def (\w+)\(")

_AVAILABLE = {"hot_list_aliases": [], "contextual_permission": None}
_LAYER_CTX = {
    "univ_enabled": True,
    "search_visible": False,
    "ctx_signal_present": True,
    "router_model": "gpt-4o",
    "router_model_family": "other",
    "non_interactive": False,
    "available_skills": None,
}

#: ``retrieval`` x ``tool_calls`` forks on ``search_visible``: the false branch is
#: the #2895 auto-fallback that presents the flat catalog (where the duplication
#: can occur at all), the true branch presents the search affordance instead.
#: Both are the cell, so both are checked.
_LAYER_CTX_VARIANTS: "dict[tuple[str, str], dict[str, dict]]" = {
    ("retrieval", "tool_calls"): {
        "search_visible_false": {"search_visible": False},
        "search_visible_true": {"search_visible": True},
    },
}


def _nested(name: str, *, description: str = "d") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _names(entries: "list[dict]") -> "list[str]":
    out = []
    for entry in entries:
        body = entry.get("function")
        out.append(str((body if isinstance(body, dict) else entry).get("name", "")))
    return out


def _duplicated_pairs(names: "set[str]") -> "set[tuple[str, str]]":
    """Every ``(qualified, bare)`` in ``names`` that are two spellings of one
    operation, per ``invoke_action``'s own alias table."""
    pairs = set()
    for name in names:
        bare = unwrapped_tool_name(name)
        if bare and bare != name and bare in names:
            pairs.add((name, bare))
    return pairs


# ── Tier 1: the mechanism ────────────────────────────────────────────────────


def test_the_qualified_spelling_goes_and_the_bare_one_stays() -> None:
    """Tier 1: contract — a pair collapses to its bare spelling, order preserved.

    Which spelling survives is not cosmetic: the bare one is the spelling whose
    dispatch keeps the target's own canonicalization and result normalisation
    (#3429), so a change that flipped the direction would silently degrade every
    call the model makes to a deduplicated operation."""
    kept = without_duplicate_alias_spellings([
        _nested("read_file"),
        _nested("file__read"),
        _nested("web_search"),
        _nested("web__search"),
    ])
    assert _names(kept) == ["read_file", "web_search"]


def test_a_qualified_name_with_no_bare_twin_present_survives() -> None:
    """Tier 1: contract — the rule drops a DUPLICATE, never a capability.

    ``file__glob``'s bare alias (``glob_files``) is only in ``base_tools`` when
    file permissions are configured; when it is absent the qualified spelling is
    the only route the model has, and removing it would take the capability away
    rather than deduplicate it."""
    kept = without_duplicate_alias_spellings([
        _nested("read_file"),
        _nested("file__read"),
        _nested("file__glob"),
    ])
    assert _names(kept) == ["read_file", "file__glob"]


def test_a_name_that_is_its_own_alias_is_not_dropped() -> None:
    """Tier 1: contract — ``plugin_management__install`` is registered under its
    own qualified name, so its ``unwrapped_tool_name`` is itself.

    That is one spelling, not two. A rule that only asked "is the unwrapped name
    in this set" would delete the operation outright — the arm exists because
    that mistake produces a silent capability loss rather than a failure."""
    assert unwrapped_tool_name("plugin_management__install") == "plugin_management__install"
    kept = without_duplicate_alias_spellings([_nested("plugin_management__install")])
    assert _names(kept) == ["plugin_management__install"]


def test_entries_that_are_not_aliases_pass_through_untouched() -> None:
    """Tier 1: contract — entries are returned by identity, not rebuilt.

    The helper sits between composition and ``descriptors_from_entries``, which
    classifies an entry by its exact key set; a rule that reshaped an entry on
    the way through would reclassify it as a provider-native passthrough."""
    entries = [_nested("session_spawn"), _nested("pipeline__run")]
    kept = without_duplicate_alias_spellings(entries)
    assert [id(e) for e in kept] == [id(e) for e in entries]


def test_the_dedup_does_not_narrow_the_dispatchable_set() -> None:
    """Tier 2: mechanism — the dropped spelling leaves the ADVERTISEMENT and stays
    in ``Exposure.dispatchable_names``.

    This is the #1618 root-1 contract ``mcp__call_tool`` already has: a cell whose
    executor keys on the dispatchable set must answer an in-code call to a
    withheld name with the per-call gate's verdict, not ``unknown_tool``. It is
    the reason the dedup is applied to the exposed list rather than to the
    composed population."""
    exposure, entries = build_enumerate_all_exposure(
        catalog_entries=[_nested("multi_agent__delegate"), _nested("pipeline__run")],
        available={},
        layer_ctx=_LAYER_CTX,
        ops=_BaseToolsOnly([_nested("delegate_to_agent")]),
        deviation=CONTENT_FENCE_EXPOSURE_DEVIATION,
    )
    assert "multi_agent__delegate" not in {d.name for d in exposure.descriptors}
    assert "multi_agent__delegate" in exposure.dispatchable_names
    assert "multi_agent__delegate" in _names(entries)


@pytest.mark.asyncio
async def test_the_retrieval_refinement_branch_dedups_its_searched_subset() -> None:
    """Tier 2: mechanism — a search hit on a qualified spelling whose bare twin is
    already among the base tools is not presented a second time.

    This branch is the one arm of the whole rule that the production-registry gate
    cannot reach: reaching it needs a live embedding index and provider, which is
    why the #3376 oracle recorded it as UNCAPTURED. So it gets its own falsifiable
    arm here, driven through the registered scheme's real
    ``build_presentation``."""
    scheme = get_scheme("retrieval")
    ops = _RetrievalOps(
        base=[_nested("web_search")],
        catalog=[_nested("web__search"), _nested("pipeline__run")],
        matched=["web__search", "pipeline__run"],
    )
    presentation = await scheme.build_presentation(
        dict(_AVAILABLE),
        {**_LAYER_CTX, "refinement": {"query": "search the web"}, "presented": ()},
        ops,
    )
    shown = set(_names(advertised_entries(presentation.tools_channel)))
    assert "web_search" in shown
    assert "web__search" not in shown, (
        "the searched subset re-presented an operation the base tools already name"
    )
    # Vacuity guard: the search really did match the qualified spelling, and a
    # non-alias hit still arrives — so the arm above is not passing because the
    # subset was empty.
    assert "pipeline__run" in shown
    assert presentation.candidates == ("web__search", "pipeline__run")


class _BaseToolsOnly:
    """A protocol-conforming ``SchemeOps`` Fake with one real callable and an
    explicit return — never a mock.

    Present because the input under test (a base-tool set holding exactly one
    half of one alias pair) is not one a real router can be steered into
    producing: ``build_tools`` composes its set from host config, not from a
    caller's choice of names."""

    def __init__(self, entries: "list[dict]") -> None:
        self._entries = entries

    def base_tools(self, available, layer_ctx) -> "list[dict]":
        return list(self._entries)


class _RetrievalOps:
    """A protocol-conforming ``SchemeOps`` Fake with real callables and explicit
    returns — never a mock.

    ``search_actions`` is the reason a Fake is right here rather than a real
    ``RouterLoop``: production's implementation reads an ``ActionEmbeddingIndex``
    and an embedding provider, so the refinement branch cannot be reached offline
    at all (the #3376 oracle recorded it as UNCAPTURED for exactly this reason)."""

    def __init__(
        self, *, base: "list[dict]", catalog: "list[dict]", matched: "list[str]"
    ) -> None:
        self._base = base
        self._catalog = catalog
        self._matched = matched

    def base_tools(self, available, layer_ctx) -> "list[dict]":
        return list(self._base)

    async def catalog_entries(self) -> "list[dict]":
        return list(self._catalog)

    async def search_actions(self, query: str) -> "list[str]":
        return list(self._matched)


# ── Tier 2: production, enumerated from the cell registry ────────────────────


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="reyn-3428-"))


def _cells() -> "list[tuple[str, str, str, dict]]":
    """Every registered ``(scheme, transport)`` cell, from the pair registry.

    Enumerated rather than hand-listed: a hand-list is a marker subset that a new
    cell joins only if someone remembers to add it, which is precisely the
    failure this gate exists to prevent."""
    out = []
    for scheme, transport in valid_scheme_transport_pairs():
        variants = _LAYER_CTX_VARIANTS.get((scheme, transport.value), {"default": {}})
        for variant, overrides in variants.items():
            key = f"{scheme}|{transport.value}"
            if variant != "default":
                key = f"{key}|{variant}"
            out.append((key, scheme, transport, {**_LAYER_CTX, **overrides}))
    return out


async def _exposed_names(host, scheme: str, transport, layer_ctx: dict) -> "set[str]":
    """What THIS cell shows the model, in whichever channel it shows it."""
    loop = RouterLoop(
        host=host,
        chain_id="dedup-3428",
        router_model="gpt-4o",
        scheme_name=resolve_scheme_for_transport(scheme, transport),
    )
    presentation = await loop._scheme.build_presentation(
        dict(_AVAILABLE), dict(layer_ctx), ops=loop,
    )
    if transport is Transport.CONTENT_FENCE:
        return set(_DECLARED_RE.findall(presentation.tool_use_sp))
    return set(_names(advertised_entries(presentation.tools_channel)))


@pytest.mark.asyncio
async def test_no_registered_cell_shows_one_operation_under_two_spellings() -> None:
    """Tier 2: production-reaches — for every cell in the pair registry, the set
    the model is shown contains no qualified name whose bare alias is in it too.

    The vacuity guard comes first and is the load-bearing half: it witnesses that
    the un-deduplicated composition this session really does produce a duplicated
    pair, so a green result means the rule fired rather than that there was
    nothing to fire on."""
    session = make_session(
        agent_name="dedup-3428-agent",
        state_log=StateLog(_tmpdir() / "state.wal"),
        snapshot_path=_tmpdir() / "snapshot.json",
    )
    host = session._router_host
    probe = RouterLoop(
        host=host, chain_id="dedup-3428", router_model="gpt-4o",
        scheme_name="enumerate-all",
    )
    composed = set(_names(
        list(probe.base_tools(dict(_AVAILABLE), dict(_LAYER_CTX)))
        + await probe.catalog_entries()
    ))
    witnessed = _duplicated_pairs(composed)
    assert witnessed, (
        "base_tools + the live catalog produced no duplicated alias pair in this "
        "session, so 'no cell duplicates' would hold vacuously"
    )

    for key, scheme, transport, layer_ctx in _cells():
        shown = await _exposed_names(host, scheme, transport, layer_ctx)
        offenders = _duplicated_pairs(shown)
        assert not offenders, (
            f"{key} shows both spellings of the same operation: "
            f"{sorted(offenders)} — the model reads each of these declarations "
            "twice on every turn"
        )


@pytest.mark.asyncio
async def test_the_two_enumerate_all_cells_still_show_the_same_set() -> None:
    """Tier 2: production-reaches — deduplicating did not re-split the populations
    #3381 joined.

    The #3419 invariant is that ``enumerate-all``'s two cells expose the same set;
    a dedup applied at one composition site and not the other would satisfy this
    file's other arm while quietly restoring exactly the asymmetry #3381 was
    about."""
    session = make_session(
        agent_name="dedup-3428-parity",
        state_log=StateLog(_tmpdir() / "state.wal"),
        snapshot_path=_tmpdir() / "snapshot.json",
    )
    host = session._router_host
    calls = await _exposed_names(host, "enumerate-all", Transport.TOOL_CALLS, _LAYER_CTX)
    fence = await _exposed_names(host, "enumerate-all", Transport.CONTENT_FENCE, _LAYER_CTX)
    assert calls, "the tool_calls cell advertised nothing — nothing to compare"
    assert calls == fence, (
        "the two enumerate-all cells no longer show the same set: "
        f"only in tool_calls {sorted(calls - fence)}, only in the code-API "
        f"{sorted(fence - calls)}"
    )
