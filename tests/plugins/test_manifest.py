"""Tier 1: Contract — ``plugin.json`` typed manifest schema (ADR 0064 §3.1,
#3067; relocated to the plugin root + required ``$schema`` — #4570
conversion A; ``capabilities`` removed entirely — #4570 conversion B,
Agent Plugins 1.0 alignment).

Round-trips a manifest through the real ``PluginManifest`` schema and the
real ``load_plugin_manifest`` file-reading path — no fakes, real
``Path``/JSON I/O via ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from reyn.plugins.manifest import (
    PLUGIN_MANIFEST_SCHEMA_URL,
    PluginManifest,
    PluginManifestError,
    capability_kinds_present,
    load_plugin_manifest,
    manifest_path_for,
)


def _write_manifest(plugin_dir, data: dict) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(data), encoding="utf-8")


def test_manifest_round_trips_nondefault_values(tmp_path):
    """Tier 1: non-default round-trip — description set, real file I/O +
    model_dump_json -> model_validate round-trip."""
    plugin_dir = tmp_path / "my-plugin"
    data = {
        "$schema": PLUGIN_MANIFEST_SCHEMA_URL,
        "name": "rag",
        "version": "1.2.3",
        "description": "builtin RAG plugin (dogfood template, ADR §3.1)",
    }
    _write_manifest(plugin_dir, data)

    manifest = load_plugin_manifest(plugin_dir)

    assert manifest.schema_ == PLUGIN_MANIFEST_SCHEMA_URL
    assert manifest.name == "rag"
    assert manifest.version == "1.2.3"
    assert manifest.description == "builtin RAG plugin (dogfood template, ADR §3.1)"

    # Round-trip through model_dump -> model_validate (JSON mode, the
    # serialised shape a P2 install step would persist/copy). by_alias=True
    # so the required "$schema" key round-trips under its real wire name,
    # not the Python attribute name (schema_).
    dumped = json.loads(manifest.model_dump_json(by_alias=True))
    reloaded = PluginManifest.model_validate(dumped)
    assert reloaded == manifest


def test_manifest_description_defaults_to_empty_string(tmp_path):
    """Tier 1: description is optional — a manifest declaring only
    identity is valid (§1: the primary use case is "just an MCP server",
    which may carry no prose description at all)."""
    plugin_dir = tmp_path / "bare"
    _write_manifest(
        plugin_dir,
        {"$schema": PLUGIN_MANIFEST_SCHEMA_URL, "name": "bare-server", "version": "0.1.0"},
    )

    manifest = load_plugin_manifest(plugin_dir)

    assert manifest.description == ""


def test_manifest_path_for_matches_the_plugin_root_layout(tmp_path):
    """Tier 1: the manifest path is the plugin ROOT (#4570 conversion A —
    the Agent Plugins 1.0 canonical location), not the pre-#4570
    ``.reyn-plugin/`` subdirectory."""
    plugin_dir = tmp_path / "some-plugin"
    assert manifest_path_for(plugin_dir) == plugin_dir / "plugin.json"


def test_manifest_missing_file_raises_typed_error(tmp_path):
    """Tier 1: a missing manifest file raises the typed ``PluginManifestError``,
    not a bare ``OSError``."""
    plugin_dir = tmp_path / "does-not-exist"
    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_invalid_json_raises_typed_error(tmp_path):
    """Tier 1: malformed JSON raises the typed ``PluginManifestError``, not a
    bare ``json.JSONDecodeError``."""
    plugin_dir = tmp_path / "bad-json"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_missing_required_field_raises_typed_error(tmp_path):
    """Tier 1: schema-level — ``version`` is required; omitting it fails
    validation (via the typed ``PluginManifestError``), not a silent
    default."""
    plugin_dir = tmp_path / "no-version"
    _write_manifest(plugin_dir, {"$schema": PLUGIN_MANIFEST_SCHEMA_URL, "name": "incomplete"})

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_missing_schema_field_raises_typed_error(tmp_path):
    """Tier 1: (#4570) ``$schema`` is required — the Agent Plugins 1.0
    canonical schema's own requirement, not a reyn-invented rule. A
    manifest omitting it is malformed, same treatment as omitting
    ``version``."""
    plugin_dir = tmp_path / "no-schema"
    _write_manifest(plugin_dir, {"name": "incomplete", "version": "1.0.0"})

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_wrong_schema_value_raises_typed_error(tmp_path):
    """Tier 1: (#4570) ``$schema`` must equal the canonical URL exactly (the
    spec's own ``const`` requirement) — an arbitrary string is rejected,
    not silently accepted as "some schema or other"."""
    plugin_dir = tmp_path / "wrong-schema"
    _write_manifest(
        plugin_dir,
        {"$schema": "https://example.com/not-the-real-schema.json",
         "name": "incomplete", "version": "1.0.0"},
    )

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_declaring_capabilities_raises_typed_error(tmp_path):
    """Tier 1: (#4570 conversion B) ``capabilities`` is REMOVED from this
    schema — a manifest still declaring it is a typed-error REJECTION,
    never a silent no-op (lead-coder ruling: the same "config that
    doesn't take effect" trap class as ``_build_tool_use_config``'s
    ``chat``-key rejection). Pydantic's default "drop unknown fields"
    behavior would otherwise swallow this with no error at all."""
    plugin_dir = tmp_path / "still-declares-capabilities"
    _write_manifest(
        plugin_dir,
        {
            "$schema": PLUGIN_MANIFEST_SCHEMA_URL,
            "name": "old-style", "version": "1.0.0",
            "capabilities": [{"kind": "mcp"}],
        },
    )

    with pytest.raises(PluginManifestError, match="capabilities"):
        load_plugin_manifest(plugin_dir)


def test_manifest_name_rejects_reserved_namespace_separator():
    """Tier 1: ``name`` rejects ``.`` — the reserved namespace-separator
    character (mirrors ``PipelineInstallIROp``/``SkillInstallIROp``)."""
    with pytest.raises(ValidationError):
        PluginManifest(schema_=PLUGIN_MANIFEST_SCHEMA_URL, name="a.b", version="1.0.0")


# ── capability_kinds_present (directory/file-existence derivation) ─────────


def test_capability_kinds_present_detects_mcp_json(tmp_path):
    """Tier 1: (#4570 conversions B/C1) ``mcp.json`` presence alone is the
    mcp capability declaration — no manifest field involved."""
    plugin_dir = tmp_path / "mcp-only"
    plugin_dir.mkdir()
    (plugin_dir / "mcp.json").write_text("{}", encoding="utf-8")

    assert capability_kinds_present(plugin_dir) == frozenset({"mcp"})


def test_capability_kinds_present_detects_pipelines_and_skills_dirs(tmp_path):
    """Tier 1: a ``pipelines/`` and/or ``skills/`` directory each
    independently contributes its own capability kind."""
    plugin_dir = tmp_path / "pipes-and-skills"
    (plugin_dir / "pipelines").mkdir(parents=True)
    (plugin_dir / "skills").mkdir(parents=True)

    assert capability_kinds_present(plugin_dir) == frozenset({"pipelines", "skills"})


def test_capability_kinds_present_empty_for_bare_plugin_dir(tmp_path):
    """Tier 1: (accept-side) a plugin directory with none of the three
    marker paths -> no capabilities, not a crash."""
    plugin_dir = tmp_path / "bare"
    plugin_dir.mkdir()

    assert capability_kinds_present(plugin_dir) == frozenset()
