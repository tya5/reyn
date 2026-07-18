"""Typed schema for ``.reyn-plugin/plugin.json`` (ADR 0064 §3.1).

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
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, ValidationError, model_validator

# Reserved so a future collision-precedence read (ADR §3.8, see
# ``reyn.plugins.source``) can trust ``name`` as the stable collision key
# without also having to guard against '.', which pipeline/skill namespacing
# already treats as a separator (mirrors ``PipelineInstallIROp.name``'s '.'
# reservation, ``reyn.schemas.models``).
_RESERVED_NAME_CHARS = "."


class PluginManifestError(ValueError):
    """Raised when ``.reyn-plugin/plugin.json`` is missing, malformed, or
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
    """``.reyn-plugin/plugin.json`` — the typed plugin manifest (ADR §3.1).

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

    name: str
    version: str
    description: str = ""
    capabilities: tuple[PluginCapability, ...] = ()

    @property
    def capability_kinds(self) -> frozenset[str]:
        return frozenset(cap.kind for cap in self.capabilities)

    @model_validator(mode="after")
    def _validate(self) -> "PluginManifest":
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


_MANIFEST_RELATIVE_PATH = Path(".reyn-plugin") / "plugin.json"


def manifest_path_for(plugin_dir: Path) -> Path:
    """The canonical manifest path inside a plugin directory (ADR §3.1 layout)."""
    return plugin_dir / _MANIFEST_RELATIVE_PATH


def load_plugin_manifest(plugin_dir: Path) -> PluginManifest:
    """Read + validate ``<plugin_dir>/.reyn-plugin/plugin.json``.

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
