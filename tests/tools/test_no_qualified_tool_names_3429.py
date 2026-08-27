"""Tier 2: #3429 — no tool name anywhere carries the catalog separator.

#3429 removed the second, ``<category>__<verb>`` spelling every catalog action
used to have. **Deletion is a state; this file is the property.** Removing the
names without a gate leaves nothing stopping the next one: a tool registered as
``widget_management__install`` next month would be a second namespace re-opening
under a new name, and the failure mode that motivated #3429 — every subsystem
that keys on a tool name has to decide whether to handle both spellings, and the
ones that forget break silently — comes back with it.

**Derived, never curated.** Every check below walks a LIVE source (the tool
registry, the catalog's membership table, the categories tuple, the assembled
``tools=`` payload) rather than comparing against a hand-written allowlist. The
census that motivated #3429 found 11 name-keyed subsystems, 4 compensating and 7
broken, precisely because the compensations were hand-written and the list of
places needing them was never enumerated. A gate with an allowlist would be the
twelfth.

**Not a lint.** The subject is a NAME the OS answers to, not a string in the
source. ``mcp_call_tool``'s ``tool`` argument legitimately carries a
``<server>__<tool>`` MCP identifier, and a fixture may name a nonexistent
qualified action to prove it is rejected; neither is a tool name, and neither is
in scope. What is in scope is anything the model can call.

Each check carries a falsification arm: the enumeration it walks is asserted
non-empty by SENTINEL MEMBERSHIP (not a size pin), and the registry check is
additionally proved live by registering a qualified-named tool into a real
``ToolRegistry`` and witnessing the check go red.
"""
from __future__ import annotations

from reyn.tools import get_default_registry
from reyn.tools.registry import ToolRegistry
from reyn.tools.types import ToolDefinition, ToolGates
from reyn.tools.universal_catalog import CATEGORIES
from reyn.tools.universal_dispatch import KNOWN_ACTION_NAMES_SORTED

#: The abolished separator. A name containing it is either a resurrected
#: qualified spelling or a new namespace being minted the same way.
SEPARATOR = "__"


def _offenders(names) -> list[str]:
    return sorted(n for n in names if SEPARATOR in n)


# ── the registry ─────────────────────────────────────────────────────────────


def test_no_registered_tool_name_is_qualified() -> None:
    """Tier 2: every name in the live tool registry is separator-free.

    This is acceptance condition 1 of the #3429 decision, stated over the
    registry itself. Three names used to violate it —
    ``plugin_management__install`` / ``__uninstall`` / ``__list``, registered
    under their own catalog-style spelling because they had no separate flat
    name — and are now ``install_plugin`` / ``uninstall_plugin`` /
    ``list_plugins``."""
    names = get_default_registry().names()
    assert {"read_file", "web_search", "install_plugin"} <= set(names), (
        "sentinel tool names are missing from the registry — it very likely "
        "failed to populate at test time, which would make this check vacuous"
    )
    offenders = _offenders(names)
    assert offenders == [], (
        f"registered tool name(s) carry the abolished catalog separator: "
        f"{offenders}. A tool has ONE name (#3429); see "
        f"docs/reference/runtime/tool-naming.md § R1."
    )


def test_the_registry_check_is_live() -> None:
    """Tier 2: falsification — a qualified name registered into a REAL
    ``ToolRegistry`` makes the check above go red.

    Not a check of the helper: a real ``ToolRegistry`` with a real
    ``ToolDefinition``, so a future change that (say) normalised names on
    registration would be caught here rather than making the gate quietly
    unfalsifiable. Registered into a throwaway registry, never the default one,
    so nothing leaks into the process-wide surface."""

    async def _handler(args, ctx):  # pragma: no cover — never invoked
        return {}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="widget_management__install",
        description="a hypothetical new tool minted with a qualified name",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"),
        handler=_handler,
        category="widget_management",
        purity="side_effect",
    ))
    assert _offenders(registry.names()) == ["widget_management__install"], (
        "a qualified tool name registered into a real ToolRegistry was NOT "
        "flagged — the registry check cannot fail, so its green is worthless"
    )


# ── the catalog ──────────────────────────────────────────────────────────────


def test_no_catalog_action_name_is_qualified() -> None:
    """Tier 2: every name the catalog offers is separator-free.

    The registry and the catalog are different sets: the catalog is what
    ``list_actions`` browses and ``invoke_action`` accepts, and it is where the
    qualified spelling lived. A name minted here without a registry entry would
    pass the registry check above and still be callable."""
    assert {"read_file", "exec", "search_knowledge"} <= set(KNOWN_ACTION_NAMES_SORTED), (
        "sentinel action names are missing from the membership table — the "
        "enumeration very likely failed, which would make this check vacuous"
    )
    offenders = _offenders(KNOWN_ACTION_NAMES_SORTED)
    assert offenders == [], (
        f"catalog action name(s) carry the abolished catalog separator: "
        f"{offenders}"
    )


def test_no_category_name_is_qualified() -> None:
    """Tier 2: a category is a browsing label, not a name prefix.

    A ``__`` in a category name would be the qualified spelling growing back
    from the other end — ``file__ops`` browsing to ``file__ops__read``."""
    assert {"file", "mcp", "exec"} <= set(CATEGORIES)
    offenders = _offenders(CATEGORIES)
    assert offenders == [], f"category name(s) carry the separator: {offenders}"


# ── the wire ─────────────────────────────────────────────────────────────────


def test_no_advertised_tool_name_is_qualified() -> None:
    """Tier 2: the check that does not depend on WHERE a name came from.

    #3376's lesson is why this arm exists: a gate written over the place a
    mapping is DECLARED is a condition on that place, not on the value. So this
    reads the assembled ``tools=`` payload — what ``build_tools`` actually hands
    the provider — and the flat catalog projection every scheme concatenates
    into it, and asks the question of the names on the wire.

    Both are exercised at their MAXIMAL configuration (agents present, file
    permissions granted, MCP servers connected), because a name gated behind a
    config the default does not enable would otherwise never be looked at."""
    from reyn.runtime.router_tools import build_tools
    from reyn.tools.types import RouterCallerState, ToolContext
    from reyn.tools.universal_catalog import catalog_entries

    advertised = [
        entry.get("function", {}).get("name") or entry.get("name", "")
        for entry in build_tools(
            file_permissions={"read": ["."], "write": ["."]},
            mcp_servers=[{"name": "echo", "tools": [{"name": "ping"}]}],
            universal_wrappers_enabled=True,
            search_actions_visible=True,
        )
    ]
    assert {"invoke_action", "list_actions"} <= set(advertised), (
        "sentinel wrapper names are missing from the assembled payload — "
        "build_tools very likely returned something unexpected"
    )
    offenders = _offenders(n for n in advertised if n)
    assert offenders == [], (
        f"the assembled tools= payload advertises qualified name(s): {offenders}"
    )

    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None,
        caller_kind="router", router_state=RouterCallerState(),
    )
    catalog_names = [entry["name"] for entry in catalog_entries(ctx)]
    assert "read_file" in catalog_names, (
        "the flat catalog projection produced no sentinel — it very likely "
        "returned nothing, which would make this check vacuous"
    )
    offenders = _offenders(catalog_names)
    assert offenders == [], (
        f"the flat catalog projection carries qualified name(s): {offenders}"
    )


# ── the operator-facing surfaces ─────────────────────────────────────────────


def test_the_system_prompt_teaches_no_qualified_tool_name() -> None:
    """Tier 2: acceptance condition 3, prompt half.

    The qualified spelling reached the model through prose as well as through
    schemas — the "## Action categories" slot taught ``<category>__<entry>`` as
    the addressing form. A line still teaching it would keep minting the
    spelling from the model's side even with every table clean.

    The doc/DSL half of condition 3 is a separate concern from a unit test
    (it lives in the PR's own verification); this arm covers the surface the OS
    itself assembles on every turn.
    """
    from reyn.prompt.universal_slots import (
        ACTION_CATEGORIES_INTRO,
        ACTION_CATEGORIES_LINES,
    )

    slot_text = "\n".join([ACTION_CATEGORIES_INTRO, *ACTION_CATEGORIES_LINES])
    assert "list_actions" in slot_text, (
        "the action-categories slot lost its vocabulary — this check would be "
        "reading the wrong string"
    )
    assert SEPARATOR not in slot_text, (
        "the '## Action categories' system-prompt slot still teaches a "
        f"``{SEPARATOR}`` name form:\n{slot_text}"
    )


#: Tools whose LLM-facing text legitimately contains the separator, because the
#: string it appears in is an MCP SERVER's own ``<server>__<tool>`` identifier —
#: an argument VALUE in a namespace reyn does not own, not a reyn tool name.
#: Declared per tool with a reason, so a new entry is a deliberate edit.
_MCP_IDENTIFIER_TOOLS = frozenset({
    "mcp_call_tool",      # its ``tool`` argument IS that identifier
    "mcp_install_local",  # describes what the installed server's tools will be called
})


def test_no_registered_tools_llm_facing_text_teaches_a_qualified_name() -> None:
    """Tier 2: no registered tool's RENDERED description or parameter schema
    teaches an ``<a>__<b>`` name.

    ★ This arm was widened after review. It used to cover the three catalog
    wrappers only, on the reasoning that they are where a model reads what an
    ``action_name`` looks like. That reasoning picked the most LIKELY place
    rather than the whole surface, and review found a real leftover outside
    it: ``cron_register`` / ``cron_disable`` were still telling the model to
    re-enable a job "via `cron__enable`". A curated subset of a surface is the
    marker-list shape this file's own docstring argues against, so the check
    now enumerates every registered tool and the EXCEPTIONS are what is
    curated — with a reason each.

    Scoped to the rendered payload (``render_for_router``), i.e. exactly the
    bytes that reach the provider — not source strings.
    """
    offenders: dict[str, list[str]] = {}
    registry = get_default_registry()
    for tool in registry:
        if tool.name in _MCP_IDENTIFIER_TOOLS:
            continue
        body = tool.render_for_router()["function"]
        blob = body["description"] + repr(body["parameters"])
        if SEPARATOR in blob:
            offenders[tool.name] = [
                seg for seg in blob.split() if SEPARATOR in seg
            ]
    assert offenders == {}, (
        f"registered tool(s) whose LLM-facing text still teaches a "
        f"``{SEPARATOR}`` name form: {offenders}"
    )


def test_the_llm_facing_text_scan_is_live() -> None:
    """Tier 2: falsification — the scan above fires on a real registered tool
    whose description carries the separator.

    Uses a REAL ``ToolDefinition`` in a throwaway registry, so a future change
    that (say) stripped the separator during rendering would be caught here
    rather than making the arm above quietly unfalsifiable."""

    async def _handler(args, ctx):  # pragma: no cover — never invoked
        return {}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="widget_reader",
        description="Re-read it later via `widget__reread`.",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"),
        handler=_handler,
        category="widget",
        purity="read_only",
    ))
    offenders = [
        t.name for t in registry
        if SEPARATOR in t.render_for_router()["function"]["description"]
    ]
    assert offenders == ["widget_reader"], (
        "a real tool description carrying the separator was NOT flagged — the "
        "LLM-facing scan cannot fail, so its green is worthless"
    )


def test_the_mcp_identifier_exception_is_not_a_blanket_pass() -> None:
    """Tier 2: the exception set is small, and every member really does carry
    an MCP ``<server>__<tool>`` identifier rather than being a place someone
    parked a failing tool.

    Without this, ``_MCP_IDENTIFIER_TOOLS`` becomes the allowlist the gate
    above exists to avoid — a name added to it would silently stop being
    checked."""
    registry = get_default_registry()
    for name in sorted(_MCP_IDENTIFIER_TOOLS):
        tool = registry.lookup(name)
        assert tool is not None, f"{name!r} is exempted but not registered"
        blob = repr(tool.render_for_router()["function"])
        assert "<server>__<tool>" in blob, (
            f"{name!r} is exempted as an MCP-identifier carrier but its "
            f"LLM-facing text does not show the ``<server>__<tool>`` form — "
            f"the exemption is covering something else"
        )
