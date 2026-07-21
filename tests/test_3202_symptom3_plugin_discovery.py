"""Tier 1/2: builtin plugin discovery (#3202 symptom 3).

Before this PR, the ONLY path an LLM had to learn "rag can be installed as a
builtin plugin" was the rag_ingest/rag_query pipelines failing at run time
with an error message naming it (discover-by-failure, a chicken-and-egg gap
-- `plugin.json` already carried a complete `description` + `capabilities`,
but nothing enumerated it). The architect's firm design layers this as:

  1. registry (``BUILTIN_PLUGINS``, ``src/reyn/builtin/registry.py``) --
     the explicit-dict SSoT of WHICH plugin names are advertised (#3196-
     shaped: no directory auto-scan).
  2. ``reyn.builtin.discovery.list_builtin_plugins`` -- reads the registry
     × each name's own ``.reyn-plugin/plugin.json`` manifest, deriving
     description/capabilities rather than duplicating them (the
     redundant-projection drift class #3164 hit for a different value).
  3. ``plugin_management__list`` tool (``src/reyn/tools/plugin_management_verbs.py``)
     -- the ordinary-tool-call reachability surface, mirroring
     ``skill_list`` (#2971).

This module pins:
  A. The consumer-reachability witness -- the REAL registered tool's ACTUAL
     output contains ``rag`` with a description, not merely that
     ``BUILTIN_PLUGINS`` contains the key (the "loaded a dict but nothing
     reads it" shape hit three times earlier today).
  B. Description-from-manifest derivation -- the tool's description is
     BYTE-IDENTICAL to ``plugin.json``'s own ``description`` field, proving
     it is read live rather than copied into the registry.
  C. Wiring completeness -- registered in the default tool registry AND
     routed in ``_OPERATION_RULES`` AND enumerated by its category (the
     #3083/#2032 "registered but LLM-invisible" bug class this repo has hit
     three separate times for plugin/skill/pipeline management verbs).
  D. Axis 4 -- with ``BUILTIN_PLUGINS`` replaced by an empty dict (a real,
     not mocked, substitute value on the real module), the discovery
     surface goes empty/RED, proving the registry is actually load-bearing
     for the tool's output rather than some other hardcoded path.

No mocks: the real ``reyn.builtin.registry`` module, the real
``.reyn-plugin/plugin.json`` on disk, and the real registered
``plugin_management__list`` ``ToolDefinition``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.discovery import list_builtin_plugins
from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.tools import get_default_registry
from reyn.tools.types import ToolContext


def _run(coro):
    return asyncio.run(coro)


def _tool_ctx() -> ToolContext:
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=None,
        workspace=Workspace(events=events),
        caller_kind="router",
    )


def _manifest_json() -> dict:
    manifest_path = (
        Path(registry_module.__file__).parent / "plugins" / "rag" / ".reyn-plugin" / "plugin.json"
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# ── A. Consumer reachability: the ACTUAL tool output, not just the dict ─────


def test_plugin_management_list_tool_surfaces_rag_with_description() -> None:
    """Tier 2: the REAL registered plugin_management__list tool's ACTUAL
    output names 'rag' with a non-empty description AND the concrete
    plugin_management__install call to install it -- the direct fix for
    #3202 symptom 3 ("LLM can only learn about rag by hitting an install
    error") plus the owner-firm install-guidance requirement (the listing
    must hand the caller its own next call, not just a name). Asserting on
    the tool's return value (not on BUILTIN_PLUGINS membership alone) is the
    witness the brief calls for."""
    tool = get_default_registry().lookup("plugin_management__list")
    assert tool is not None, "plugin_management__list is not registered"

    result = _run(tool.handler({}, _tool_ctx()))
    plugins = {p["name"]: p for p in result["plugins"]}

    assert "rag" in plugins, (
        f"plugin_management__list did not surface 'rag': {result['plugins']}"
    )
    assert plugins["rag"]["description"], "rag's description must be non-empty"
    assert "capabilities" in plugins["rag"]
    assert plugins["rag"]["install"] == {
        "tool": "plugin_management__install",
        "args": {"source": {"kind": "builtin", "name": "rag"}},
    }, (
        "the listing must carry the exact install call for this entry, not "
        f"just its name: {plugins['rag']}"
    )


# ── A2. The tool's OWN description names the install call, not embedding ───


def test_plugin_management_list_description_names_the_install_call() -> None:
    """Tier 1: the registered tool's description is where install-guidance
    must live (owner firm) -- it must literally name
    plugin_management__install and the {"kind": "builtin", ...} source shape,
    so a model reading the tool catalog (not just this tool's OWN output)
    already knows what to call next."""
    tool = get_default_registry().lookup("plugin_management__list")
    assert tool is not None
    assert "plugin_management__install" in tool.description
    assert "kind" in tool.description and "builtin" in tool.description


def test_plugin_management_list_description_never_mentions_embedding() -> None:
    """Tier 1: reyn's internal semantic_search (embedding-backed) and the
    user-facing rag PLUGIN are deliberately different things (owner firm,
    #3202) -- this tool's description must stay scoped to builtin PLUGIN
    install-eligibility only, never blur the two by mentioning embedding/
    semantic_search."""
    tool = get_default_registry().lookup("plugin_management__list")
    assert tool is not None
    lowered = tool.description.lower()
    assert "embedding" not in lowered
    assert "semantic_search" not in lowered


# ── B. Description is derived from the manifest, not duplicated ────────────


def test_rag_description_matches_manifest_verbatim() -> None:
    """Tier 2: the discovery surface's description for 'rag' is verbatim
    identical to plugin.json's own 'description' field -- proving it is
    READ from the manifest at call time, not copied into BUILTIN_PLUGINS (the
    redundant-projection drift class #3164 hit for a different value). Edit
    plugin.json and this test's expectation changes with it -- there is no
    second string anywhere to fall out of sync."""
    manifest = _manifest_json()
    plugins = {p["name"]: p for p in list_builtin_plugins()}

    assert plugins["rag"]["description"] == manifest["description"]
    assert set(plugins["rag"]["capabilities"]) == {
        cap["kind"] for cap in manifest["capabilities"]
    }


def test_builtin_plugins_registry_holds_no_description_key() -> None:
    """Tier 1: BUILTIN_PLUGINS is an allowlist ONLY (registry = which names
    to advertise) -- if a future edit adds a 'description' key here, a
    second, driftable copy of manifest text has been created, which is
    exactly the redundant-projection shape the architect's design forbids."""
    from reyn.builtin.registry import BUILTIN_PLUGINS

    for name, entry in BUILTIN_PLUGINS.items():
        assert "description" not in entry, (
            f"BUILTIN_PLUGINS[{name!r}] must not carry its own 'description' -- "
            "derive it from the plugin's manifest instead (see discovery.py)"
        )


# ── C. Wiring completeness (the #3083/#2032 bug class, 3rd occurrence) ──────


def test_plugin_management_list_is_registered_and_routed() -> None:
    """Tier 1: reachable by BOTH surfaces a router tool needs -- the default
    registry (bare call) and the universal-catalog route (invoke_action). A
    tool wired to only one is the "registered but LLM-invisible" bug class
    this repo's plugin/skill/pipeline management verbs have each hit once
    already (#3083/#2032/#2589/#2621)."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    assert get_default_registry().lookup("plugin_management__list") is not None
    assert _OPERATION_RULES["plugin_management__list"][0] == "plugin_management__list"


def test_plugin_management_list_is_enumerated_in_its_category() -> None:
    """Tier 1: list_actions(category=["plugin_management"]) surfaces
    plugin_management__list -- the enumeration half of the same wiring gap
    (dispatch-wired but absent from a static CATEGORIES/enumeration list)."""
    from reyn.tools.universal_catalog import _enumerate_static_category

    names = {a["qualified_name"] for a in _enumerate_static_category("plugin_management")}
    assert "plugin_management__list" in names


# ── D. Axis 4: strip the registry to a no-op, discovery must go RED ────────


def test_stripping_builtin_plugins_to_empty_makes_discovery_empty(
    monkeypatch,
) -> None:
    """Tier 2: axis-4 witness (brief's explicit ask) -- replacing
    BUILTIN_PLUGINS with an empty dict on the REAL registry module (no mock,
    a real substitute value on the real object the production code reads)
    drives list_builtin_plugins() to empty. This proves the registry is
    load-bearing for discovery, not vestigial alongside some other hardcoded
    'rag' path."""
    monkeypatch.setattr(registry_module, "BUILTIN_PLUGINS", {})

    assert list_builtin_plugins() == [], (
        "list_builtin_plugins() must go empty when BUILTIN_PLUGINS is emptied -- "
        "if this is non-empty, discovery is not actually reading the registry"
    )
