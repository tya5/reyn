"""Typed schema for a plugin's ``plugin.json`` manifest (ADR 0064 §3.1,
relocated to the plugin root — #4570 conversion A).

**#4570 conversion A**: the manifest lives at ``<plugin_dir>/plugin.json``
(the Agent Plugins 1.0 canonical location, agent-plugins.org's
``plugin.schema.json``), not ``<plugin_dir>/.reyn-plugin/plugin.json`` —
architect's measurement (#4570) found reyn's own manifest FIELD shape
already standard-compatible; only its directory position and the required
``$schema`` const were the actual gaps. ``.reyn-plugin/`` still exists as
reyn's own internal state directory (``_install_state.json`` mid-install
marker, ``_source_kind.json``/``_provenance.json`` sidecars) — only the
manifest itself moved out of it.

A plugin is a self-contained directory; the manifest declares its identity
(``name`` / ``version``) and WHICH capability subdirs are present — every
capability is optional (§3.1: "a valid plugin may be *just* an MCP server,
*just* a pipeline, or any combination"). Mirrors the house convention for
typed, discriminated-union side-effect payloads used across reyn's op
schemas (``reyn.schemas.models`` — ``kind: Literal[...]`` per variant,
``Field(discriminator="kind")`` on the union) rather than a form-sniffed
untyped string.

``capabilities`` declares presence + an optional explicit entry list per
capability; an empty ``entries`` tuple means "discover everything reyn's
plugin layout convention expects" (root ``.mcp.json`` for ``mcp``,
``pipelines/*.yaml`` for ``pipelines``, ``skills/*/SKILL.md`` for
``skills`` — ADR §3.1's directory layout). Discovery/registration itself is
P2 (install machinery); this module only defines and validates the shape.
This field is a reyn-native extension the standard's ``additionalProperties:
false`` root does not permit (#4570 conversion B moves it into
``extensions["dev.reyn"]`` — not yet done as of this module's own #4570
conversion A slice; kept here unchanged in the meantime)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Union

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


class PluginMCPCapability(BaseModel):
    """The plugin ships an MCP server declared at its root ``.mcp.json``
    (ADR §3.1 — standard shape, no reyn-specific fields)."""

    kind: Literal["mcp"] = "mcp"


class PluginPipelinesCapability(BaseModel):
    """The plugin ships one or more pipeline DSL files under ``pipelines/``
    (ADR §3.1 — a declared reyn extension, no standard equivalent).

    ``entries``: explicit list of DSL filenames (relative to ``pipelines/``)
    to register. Empty = discover every ``pipelines/*.yaml`` file.
    """

    kind: Literal["pipelines"] = "pipelines"
    entries: tuple[str, ...] = ()


class PluginSkillsCapability(BaseModel):
    """The plugin ships one or more standard ``SKILL.md`` skills under
    ``skills/<name>/`` (ADR §3.1 — honoured as-is, the one genuine open
    standard per §3.6).

    ``entries``: explicit list of skill directory names under ``skills/``
    to register. Empty = discover every ``skills/*/SKILL.md``.
    """

    kind: Literal["skills"] = "skills"
    entries: tuple[str, ...] = ()


PluginCapability = Annotated[
    Union[PluginMCPCapability, PluginPipelinesCapability, PluginSkillsCapability],
    Field(discriminator="kind"),
]


class PluginManifest(BaseModel):
    """``plugin.json`` (plugin root) — the typed plugin manifest (ADR §3.1,
    relocated #4570 conversion A).

    ``$schema`` (JSON key, Python attribute ``schema_``) is REQUIRED and
    must equal :data:`PLUGIN_MANIFEST_SCHEMA_URL` — the Agent Plugins 1.0
    canonical manifest schema's own ``const`` requirement.

    ``name`` is the plugin's stable identity — the collision key for
    ``reyn.plugins.source.resolve_name_collision`` and the ``~/.reyn/plugins/
    <name>/`` install-target directory name (P2). ``.`` is reserved (mirrors
    ``PipelineInstallIROp``/``SkillInstallIROp``'s namespace-separator
    convention) so a plugin name never collides with a capability's own
    dotted namespace key.

    ``capabilities`` is a discriminated union list (`kind` in
    ``{"mcp", "pipelines", "skills"}``) — every entry optional, any subset,
    duplicates of the same ``kind`` rejected (a manifest declares each
    capability at most once).
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(alias="$schema")
    name: str
    version: str
    description: str = ""
    capabilities: tuple[PluginCapability, ...] = ()

    @property
    def capability_kinds(self) -> frozenset[str]:
        return frozenset(cap.kind for cap in self.capabilities)

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
        kinds = [cap.kind for cap in self.capabilities]
        if len(kinds) != len(set(kinds)):
            raise ValueError(
                f"PluginManifest.capabilities declares duplicate kinds: {kinds!r} "
                "(each capability kind may appear at most once)"
            )
        return self


_MANIFEST_RELATIVE_PATH = Path("plugin.json")


def manifest_path_for(plugin_dir: Path) -> Path:
    """The canonical manifest path inside a plugin directory (ADR §3.1 layout)."""
    return plugin_dir / _MANIFEST_RELATIVE_PATH


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
