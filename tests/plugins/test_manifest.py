"""Tier 1: Contract — ``.reyn-plugin/plugin.json`` typed manifest schema (ADR 0064 §3.1, #3067).

Round-trips a manifest with non-default values (all three capability kinds,
non-empty explicit ``entries``, a non-empty ``description``) through the real
``PluginManifest`` schema and the real ``load_plugin_manifest`` file-reading
path — no fakes, real ``Path``/JSON I/O via ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from reyn.plugins.manifest import (
    PluginManifest,
    PluginManifestError,
    PluginMCPCapability,
    PluginPipelinesCapability,
    PluginSkillsCapability,
    load_plugin_manifest,
    manifest_path_for,
)


def _write_manifest(plugin_dir, data: dict) -> None:
    manifest_dir = plugin_dir / ".reyn-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json.dumps(data), encoding="utf-8")


def test_manifest_round_trips_nondefault_values(tmp_path):
    """Tier 1: non-default round-trip — all 3 capability kinds present,
    non-empty entries, description set, real file I/O + model_dump_json ->
    model_validate round-trip."""
    plugin_dir = tmp_path / "my-plugin"
    data = {
        "name": "rag",
        "version": "1.2.3",
        "description": "builtin RAG plugin (dogfood template, ADR §3.1)",
        "capabilities": [
            {"kind": "mcp"},
            {"kind": "pipelines", "entries": ["ingest.yaml", "query.yaml"]},
            {"kind": "skills", "entries": ["rag-search"]},
        ],
    }
    _write_manifest(plugin_dir, data)

    manifest = load_plugin_manifest(plugin_dir)

    assert manifest.name == "rag"
    assert manifest.version == "1.2.3"
    assert manifest.description == "builtin RAG plugin (dogfood template, ADR §3.1)"
    assert manifest.capability_kinds == frozenset({"mcp", "pipelines", "skills"})

    pipelines_cap = next(
        cap for cap in manifest.capabilities if isinstance(cap, PluginPipelinesCapability)
    )
    assert pipelines_cap.entries == ("ingest.yaml", "query.yaml")
    skills_cap = next(
        cap for cap in manifest.capabilities if isinstance(cap, PluginSkillsCapability)
    )
    assert skills_cap.entries == ("rag-search",)
    assert any(isinstance(cap, PluginMCPCapability) for cap in manifest.capabilities)

    # Round-trip through model_dump -> model_validate (JSON mode, the
    # serialised shape a P2 install step would persist/copy).
    dumped = json.loads(manifest.model_dump_json())
    reloaded = PluginManifest.model_validate(dumped)
    assert reloaded == manifest


def test_manifest_capabilities_are_optional_any_subset(tmp_path):
    """Tier 1: §3.1 'every capability subdir is optional' — a manifest with
    zero capabilities (declares only identity) is valid, matching the common
    case of building just an MCP server with no skill (§1)."""
    plugin_dir = tmp_path / "bare"
    _write_manifest(plugin_dir, {"name": "bare-server", "version": "0.1.0"})

    manifest = load_plugin_manifest(plugin_dir)

    assert manifest.capabilities == ()
    assert manifest.capability_kinds == frozenset()


def test_manifest_path_for_matches_adr_layout(tmp_path):
    """Tier 1: the manifest path is the ADR §3.1 layout constant, not an
    implementation-chosen path."""
    plugin_dir = tmp_path / "some-plugin"
    assert manifest_path_for(plugin_dir) == plugin_dir / ".reyn-plugin" / "plugin.json"


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
    manifest_dir = plugin_dir / ".reyn-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_missing_required_field_raises_typed_error(tmp_path):
    """Tier 1: schema-level — ``version`` is required; omitting it fails
    validation (via the typed ``PluginManifestError``), not a silent
    default."""
    plugin_dir = tmp_path / "no-version"
    _write_manifest(plugin_dir, {"name": "incomplete"})

    with pytest.raises(PluginManifestError):
        load_plugin_manifest(plugin_dir)


def test_manifest_name_rejects_reserved_namespace_separator():
    """Tier 1: ``name`` rejects ``.`` — the reserved namespace-separator
    character (mirrors ``PipelineInstallIROp``/``SkillInstallIROp``)."""
    with pytest.raises(ValidationError):
        PluginManifest(name="a.b", version="1.0.0")


def test_manifest_rejects_duplicate_capability_kind():
    """Tier 1: a manifest declaring the same capability kind twice is
    malformed — each capability is registered at most once (P2 register step
    has no 'merge two pipelines blocks' semantics to fall back on)."""
    with pytest.raises(ValidationError):
        PluginManifest(
            name="dup",
            version="1.0.0",
            capabilities=[
                PluginPipelinesCapability(entries=("a.yaml",)),
                PluginPipelinesCapability(entries=("b.yaml",)),
            ],
        )


def test_manifest_capability_kind_is_a_typed_discriminated_union_not_a_string():
    """Tier 1: Tool-Contract lens — an unrecognised ``kind`` value fails
    validation at the schema boundary rather than being silently
    form-sniffed/ignored."""
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(
            {
                "name": "x",
                "version": "1.0.0",
                "capabilities": [{"kind": "not-a-real-capability"}],
            }
        )
