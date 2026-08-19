"""Tier 1 + Tier 2: no cell advertises the same operation twice.

``base_tools`` and the universal catalog are two populations that name many of
the same operations — ``read_file``, ``delegate_to_agent``, … A cell that
composes both therefore shows the model up to 18 declarations twice, on every
turn (#3428).

#3429 changed what "twice" means here, not whether it happens. The two rows used
to carry two DIFFERENT names for one operation (``read_file`` and
``file__read``), so deduplicating meant resolving one spelling to the other
through the alias table and choosing which to keep. With one name per operation
the two rows are literally the same name and the rule is a plain first-wins
dedup — but the duplication itself is unchanged, so this file still exists.

Two claims are kept apart, as in the #3376 cell tests: **the mechanism is
correct** (``without_duplicate_names`` drops what it should and only
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
from reyn.tools.encoders import sanitize_identifier
from reyn.tools.exposure import without_duplicate_names
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


def _repeated_names(names: "list[str]") -> "set[str]":
    """Every name that appears more than once in ``names``."""
    seen: set[str] = set()
    repeated: set[str] = set()
    for name in names:
        if name in seen:
            repeated.add(name)
        seen.add(name)
    return repeated


# ── Tier 1: the mechanism ────────────────────────────────────────────────────


def test_the_first_row_wins_and_order_is_preserved() -> None:
    """Tier 1: contract — a repeat collapses to its FIRST occurrence.

    Which row survives is not cosmetic: callers compose ``base_tools`` first, and
    ``delegate_to_agent``'s base row carries the live ``enum`` of peer names that
    the catalog projection does not, so a change that kept the later row would
    let a model name a peer that does not exist."""
    kept = without_duplicate_names([
        _nested("read_file", description="base"),
        _nested("read_file", description="catalog"),
        _nested("web_search", description="base"),
        _nested("web_search", description="catalog"),
    ])
    assert _names(kept) == ["read_file", "web_search"]
    assert [e["function"]["description"] for e in kept] == ["base", "base"]


def test_a_name_appearing_once_survives() -> None:
    """Tier 1: contract — the rule drops a DUPLICATE, never a capability.

    ``glob_files`` is in ``base_tools`` only when file permissions are
    configured; when it is absent the catalog row is the only route the model
    has, and removing it would take the capability away rather than
    deduplicate it."""
    kept = without_duplicate_names([
        _nested("read_file"),
        _nested("read_file"),
        _nested("glob_files"),
    ])
    assert _names(kept) == ["read_file", "glob_files"]


def test_entries_that_are_not_aliases_pass_through_untouched() -> None:
    """Tier 1: contract — entries are returned by identity, not rebuilt.

    The helper sits between composition and ``descriptors_from_entries``, which
    classifies an entry by its exact key set; a rule that reshaped an entry on
    the way through would reclassify it as a provider-native passthrough."""
    entries = [_nested("spawn_session"), _nested("run_pipeline")]
    kept = without_duplicate_names(entries)
    assert [id(e) for e in kept] == [id(e) for e in entries]


def test_the_dedup_does_not_narrow_the_dispatchable_set() -> None:
    """Tier 2: mechanism — the dropped spelling leaves the ADVERTISEMENT and stays
    in ``Exposure.dispatchable_names``.

    This is the #1618 root-1 contract ``mcp_call_tool`` already has: a cell whose
    executor keys on the dispatchable set must answer an in-code call to a
    withheld name with the per-call gate's verdict, not ``unknown_tool``. It is
    the reason the dedup is applied to the exposed list rather than to the
    composed population."""
    exposure, entries = build_enumerate_all_exposure(
        catalog_entries=[_nested("delegate_to_agent"), _nested("run_pipeline")],
        available={},
        layer_ctx=_LAYER_CTX,
        ops=_BaseToolsOnly([_nested("delegate_to_agent")]),
        deviation=CONTENT_FENCE_EXPOSURE_DEVIATION,
    )
    # Shown once, not twice — and the withheld row's name is still dispatchable
    # and still in the composed population the executor derives from.
    assert [d.name for d in exposure.descriptors].count("delegate_to_agent") == 1
    assert "delegate_to_agent" in exposure.dispatchable_names
    assert _names(entries).count("delegate_to_agent") == 2


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
        catalog=[_nested("web_search"), _nested("run_pipeline")],
        matched=["web_search", "run_pipeline"],
    )
    presentation = await scheme.build_presentation(
        dict(_AVAILABLE),
        {**_LAYER_CTX, "refinement": {"query": "search the web"}, "presented": ()},
        ops,
    )
    shown = _names(advertised_entries(presentation.tools_channel))
    assert shown.count("web_search") == 1, (
        "the searched subset re-presented an operation the base tools already "
        f"name: {shown!r}"
    )
    # Vacuity guard: the search really did match the base-tool name, and a
    # non-overlapping hit still arrives — so the arm above is not passing
    # because the subset was empty.
    assert "run_pipeline" in shown
    assert presentation.candidates == ("web_search", "run_pipeline")


class _BaseToolsOnly:
    """A protocol-conforming ``SchemeOps`` Fake with one real callable and an
    explicit return — never a mock.

    Present because the input under test (a base-tool set holding exactly one
    of the two duplicated rows) is not one a real router can be steered into
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


async def _exposed_names(host, scheme: str, transport, layer_ctx: dict) -> "list[str]":
    """What THIS cell shows the model, in whichever channel it shows it.

    A LIST, not a set: the property under test is "shown twice", which a set
    silently satisfies."""
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
        return list(_DECLARED_RE.findall(presentation.tool_use_sp))
    return _names(advertised_entries(presentation.tools_channel))


@pytest.mark.asyncio
async def test_no_registered_cell_shows_one_operation_twice() -> None:
    """Tier 2: production-reaches — for every cell in the pair registry, no name
    the model is shown appears twice.

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
    composed = _names(
        list(probe.base_tools(dict(_AVAILABLE), dict(_LAYER_CTX)))
        + await probe.catalog_entries()
    )
    witnessed = _repeated_names(composed)
    assert witnessed, (
        "base_tools + the live catalog produced no repeated name in this "
        "session, so 'no cell duplicates' would hold vacuously"
    )

    for key, scheme, transport, layer_ctx in _cells():
        shown = await _exposed_names(host, scheme, transport, layer_ctx)
        offenders = _repeated_names(shown)
        assert not offenders, (
            f"{key} shows the same operation twice: "
            f"{sorted(offenders)} — the model reads each of these declarations "
            "twice on every turn"
        )


@pytest.mark.asyncio
async def test_the_two_enumerate_all_cells_still_show_the_same_set() -> None:
    """Tier 2: production-reaches — deduplicating did not re-split the populations
    #3381 joined.

    The #3419 invariant is that ``enumerate-all``'s two cells expose the SAME
    ACTIONS; a dedup applied at one composition site and not the other would
    satisfy this file's other arm while quietly restoring exactly the
    asymmetry #3381 was about.

    #4932 (2026-08-19): with ``exec``'s own visibility gate retired, ``exec``
    is now unconditionally in this comparison for the first time — surfacing
    a PRE-EXISTING, legitimate spelling difference this test never had cause
    to normalize before: the code-API cell (``Transport.CONTENT_FENCE``)
    renders every name through ``sanitize_identifier`` (``reyn.tools.
    encoders``), which suffixes ``exec`` to ``exec_`` because it collides
    with BOTH the Python keyword and safe-mode's banned-builtin AST check
    (that module's own docstring, #3429). Comparing the RAW ``tool_calls``
    names against ``sanitize_identifier``-mapped names is the correct
    "same actions" check — a literal-string comparison was never actually
    the invariant's intent, it simply never had a keyword-colliding name to
    expose the gap until ``exec`` became unconditionally visible."""
    session = make_session(
        agent_name="dedup-3428-parity",
        state_log=StateLog(_tmpdir() / "state.wal"),
        snapshot_path=_tmpdir() / "snapshot.json",
    )
    host = session._router_host
    calls = set(await _exposed_names(host, "enumerate-all", Transport.TOOL_CALLS, _LAYER_CTX))
    fence = set(await _exposed_names(host, "enumerate-all", Transport.CONTENT_FENCE, _LAYER_CTX))
    assert calls, "the tool_calls cell advertised nothing — nothing to compare"
    calls_as_identifiers = {sanitize_identifier(name) for name in calls}
    assert calls_as_identifiers == fence, (
        "the two enumerate-all cells no longer show the same ACTIONS (after "
        "mapping tool_calls names through the SAME sanitize_identifier the "
        "code-API cell itself uses): "
        f"only in tool_calls (mapped) {sorted(calls_as_identifiers - fence)}, "
        f"only in the code-API {sorted(fence - calls_as_identifiers)}"
    )
