"""``list_builtin_plugins`` — the read-only enumerator over
``reyn.builtin.registry.BUILTIN_PLUGINS`` (#3202 symptom 3).

Before this module, the ONLY path by which an LLM could learn that
``rag`` exists as an installable builtin plugin was the ``rag_ingest`` /
``rag_query`` pipelines failing at run time with an error message naming it
-- a chicken-and-egg gap (discover-by-failure). ``BUILTIN_PLUGINS`` names
WHICH plugin directories are advertised (the allowlist, #3196-shaped: a
directory appearing under ``src/reyn/builtin/plugins/`` never
self-advertises); this module answers WHAT each one is, by reading its own
``.reyn-plugin/plugin.json`` manifest (``description`` + ``capabilities``)
at call time rather than duplicating that text into the registry (the
redundant-projection drift class #3164 hit for a different value). Two
disjoint reads, one SSoT each:

  registry (``BUILTIN_PLUGINS``)  -> which names to advertise
  manifest (``plugin.json``)      -> what a name IS (description, capabilities)

``reyn.tools.plugin_management_verbs._handle_plugin_list`` is the tool-level
consumer that surfaces this to the LLM through the ordinary tool-call flow
(not an error path) -- see that module for the ``plugin_management__list``
``ToolDefinition``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import reyn.builtin.registry as _registry
from reyn.plugins.manifest import PluginManifestError, load_plugin_manifest


def _builtin_plugins_root() -> Path:
    """``src/reyn/builtin/plugins/`` -- computed relative to THIS package's
    own file location (mirrors ``registry.py``'s ``_BUILTIN_DIR`` and
    ``op_runtime/plugin_install.py``'s ``_builtin_plugin_dir``, so the three
    never drift relative to each other)."""
    import reyn.builtin as _builtin_pkg

    return Path(_builtin_pkg.__file__).resolve().parent / "plugins"


def list_builtin_plugins() -> "list[dict[str, Any]]":
    """Enumerate every ``BUILTIN_PLUGINS``-advertised, enabled plugin as
    ``{name, description, capabilities, install}``, derived from its own
    manifest.

    ``install`` is the concrete, typed ``plugin_management__install`` call
    for this exact entry -- ``{"tool": "plugin_management__install", "args":
    {"source": {"kind": "builtin", "name": <name>}}}`` -- so a caller that
    just enumerated this list already has the next call to make, without
    having to compose the ``source`` shape itself (owner firm, #3202: the
    tool must carry discovery AND install-guidance together).

    A disabled entry (``enabled: False``) is silently skipped -- same
    semantics as a disabled ``BUILTIN_SKILLS``/``BUILTIN_PIPELINES`` entry.
    A manifest that fails to load (missing/malformed) surfaces as an
    ``error`` field rather than raising, so one broken plugin does not take
    the whole discovery surface down for the LLM caller.

    Reads ``reyn.builtin.registry.BUILTIN_PLUGINS`` via module-attribute
    access (not a name imported at module load time) so the registry is
    genuinely load-bearing for every call, including in a test that
    substitutes the attribute on the real module (axis-4 witness, #3202).
    """
    plugins_root = _builtin_plugins_root()
    out: "list[dict[str, Any]]" = []
    for name, entry in _registry.BUILTIN_PLUGINS.items():
        if not entry.get("enabled", True):
            continue
        plugin_dir = plugins_root / name
        try:
            manifest = load_plugin_manifest(plugin_dir)
        except PluginManifestError as exc:
            out.append({"name": name, "error": f"manifest unreadable: {exc}"})
            continue
        out.append(
            {
                "name": name,
                "description": manifest.description,
                "capabilities": sorted(manifest.capability_kinds),
                "install": {
                    "tool": "plugin_management__install",
                    "args": {"source": {"kind": "builtin", "name": name}},
                },
            }
        )
    return out
