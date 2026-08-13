"""Typed schema for a plugin's ``plugin.json`` manifest (ADR 0064 §3.1,
relocated to the plugin root + capabilities dropped — #4570 conversions
A/B, Agent Plugins 1.0 alignment).

**#4570 conversion A**: the manifest lives at ``<plugin_dir>/plugin.json``
(the Agent Plugins 1.0 canonical location, agent-plugins.org's
``plugin.schema.json``), not ``<plugin_dir>/.reyn-plugin/plugin.json`` —
architect's measurement (#4570) found reyn's own manifest FIELD shape
already standard-compatible; only its directory position and the required
``$schema`` const were the actual gaps. ``.reyn-plugin/`` still exists as
reyn's own internal state directory (``_install_state.json`` mid-install
marker, ``_source_kind.json``/``_provenance.json`` sidecars) — only the
manifest itself moved out of it.

**#4570 conversion B**: ``capabilities`` (and its nested ``entries``
subset-selection field) is REMOVED from this schema entirely, not moved
into ``extensions["dev.reyn"]`` (lead-coder ruling, #4570: "誰も使ってい
ない機能を extensions に残すのは一本化の逆" — owner's own stated goal
("互換性あるなら標準に寄せて一本化したい") argues against inventing a
never-used reyn-specific extension slot at the same moment the standard's
own root schema (``additionalProperties: false``) is being adopted).
Measured population before removal: 2 production readers
(``plugin_install.py``'s registration step), 0 manifests anywhere in this
repo declaring it (including the shipped ``rag`` plugin), 0 third-party
manifests (reyn is unreleased). A capability's presence is now derived
PURELY from directory/file existence at registration time — ``mcp.json``
for ``mcp``, ``pipelines/*.yaml`` for ``pipelines``, ``skills/*/SKILL.md``
for ``skills`` (ADR §3.1's own directory layout, unchanged) — mirroring
exactly how a standard-compliant client discovers ``skills/`` without any
manifest hint. See ``reyn.core.op_runtime.plugin_install``'s registration
step for where this presence check actually runs; this module has nothing
left to say about capabilities at all.

**A manifest still declaring ``capabilities`` is a typed-error REJECTION,
not a silent no-op** (lead-coder ruling, same "config that doesn't take
effect" trap class as ``_build_tool_use_config``'s ``chat``-key rejection,
verbatim: "a silently dropped old key is a 'config that doesn't take
effect' trap") — see :meth:`PluginManifest._reject_removed_capabilities_key`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The Agent Plugins 1.0 canonical manifest schema URL (agent-plugins.org,
# published 2026-08-06) — the ``$schema`` field's required, ``const`` value
# (#4570 conversion A). Measured directly against the published
# ``plugin.schema.json`` (architect, #4570) rather than assumed.
PLUGIN_MANIFEST_SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

# Reserved so a future collision-precedence read (ADR §3.8, see
# ``reyn.plugins.source``) can trust ``name`` as the stable collision key
# without also having to guard against '.', which pipeline/skill namespacing
# already treats as a separator (mirrors ``PipelineInstallIROp.name``'s '.'
# reservation, ``reyn.schemas.models``).
_RESERVED_NAME_CHARS = "."


class PluginManifestError(ValueError):
    """Raised when a plugin's ``plugin.json`` is missing, malformed, or
    fails schema validation. Wraps the lower-level ``OSError`` /
    ``json.JSONDecodeError`` / pydantic ``ValidationError`` so callers have
    one exception type to catch."""


class PluginManifest(BaseModel):
    """``plugin.json`` (plugin root) — the typed plugin manifest (ADR §3.1,
    relocated #4570 conversion A; ``capabilities`` removed #4570
    conversion B).

    ``$schema`` (JSON key, Python attribute ``schema_``) is REQUIRED and
    must equal :data:`PLUGIN_MANIFEST_SCHEMA_URL` — the Agent Plugins 1.0
    canonical manifest schema's own ``const`` requirement.

    ``name`` is the plugin's stable identity — the collision key for
    ``reyn.plugins.source.resolve_name_collision`` and the ``~/.reyn/plugins/
    <name>/`` install-target directory name (P2). ``.`` is reserved (mirrors
    ``PipelineInstallIROp``/``SkillInstallIROp``'s namespace-separator
    convention) so a plugin name never collides with a capability's own
    dotted namespace key.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(alias="$schema")
    name: str
    version: str
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_capabilities_key(cls, data: Any) -> Any:
        """#4570 conversion B: ``capabilities`` (the field's whole former
        purpose) is gone from this schema — a manifest still carrying it
        must be REJECTED, never silently accepted-and-ignored. Pydantic's
        default "extra fields dropped" behavior would otherwise turn a
        plugin author's real declaration into dead JSON with no error
        anywhere — the exact "wrote it, but it doesn't take effect" trap
        class lead-coder's ruling names by precedent
        (``_build_tool_use_config``'s ``chat``-key rejection)."""
        if isinstance(data, dict) and "capabilities" in data:
            raise ValueError(
                "PluginManifest no longer accepts a 'capabilities' field "
                "(#4570 conversion B: a capability's presence is derived "
                "from directory/file existence -- mcp.json / pipelines/ / "
                "skills/ -- never declared in the manifest). Remove the "
                "'capabilities' key; declaring it here would otherwise "
                "silently have no effect."
            )
        return data

    @model_validator(mode="after")
    def _validate(self) -> "PluginManifest":
        if self.schema_ != PLUGIN_MANIFEST_SCHEMA_URL:
            raise ValueError(
                f"PluginManifest.$schema must equal {PLUGIN_MANIFEST_SCHEMA_URL!r}, "
                f"got {self.schema_!r}"
            )
        if not self.name:
            raise ValueError("PluginManifest.name must be non-empty")
        if any(ch in self.name for ch in _RESERVED_NAME_CHARS):
            raise ValueError(
                f"PluginManifest.name {self.name!r} must not contain "
                f"reserved namespace-separator characters ({_RESERVED_NAME_CHARS!r})"
            )
        return self


_MANIFEST_RELATIVE_PATH = Path("plugin.json")


def manifest_path_for(plugin_dir: Path) -> Path:
    """The canonical manifest path inside a plugin directory (ADR §3.1 layout)."""
    return plugin_dir / _MANIFEST_RELATIVE_PATH


def capability_kinds_present(plugin_dir: Path) -> "frozenset[str]":
    """Which capability kinds *plugin_dir* ships, derived PURELY from
    directory/file existence (#4570 conversion B) — ``mcp.json`` (#4570
    conversion C1 — renamed from ``.mcp.json``, the Agent Plugins 1.0
    canonical filename) for ``mcp``, a ``pipelines/`` directory for
    ``pipelines``, a ``skills/`` directory for ``skills`` (ADR §3.1's own
    layout, unchanged). The ONE derivation both :mod:`reyn.core.op_runtime.
    plugin_install`'s registration step and :mod:`reyn.builtin.discovery`'s
    listing consult — so "this plugin has an mcp capability" can never
    independently drift between what gets REGISTERED and what gets
    LISTED."""
    kinds: set[str] = set()
    if (plugin_dir / "mcp.json").is_file():
        kinds.add("mcp")
    if (plugin_dir / "pipelines").is_dir():
        kinds.add("pipelines")
    if (plugin_dir / "skills").is_dir():
        kinds.add("skills")
    return frozenset(kinds)


def load_plugin_manifest(plugin_dir: Path) -> PluginManifest:
    """Read + validate ``<plugin_dir>/plugin.json``.

    Raises ``PluginManifestError`` (never a bare ``OSError`` / JSON /
    pydantic error) on a missing file, invalid JSON, or a schema violation,
    so every caller catches one exception type.
    """
    path = manifest_path_for(plugin_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginManifestError(f"cannot read plugin manifest at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginManifestError(f"invalid JSON in plugin manifest {path}: {exc}") from exc
    try:
        return PluginManifest.model_validate(data)
    except ValidationError as exc:
        raise PluginManifestError(f"invalid plugin manifest {path}: {exc}") from exc
