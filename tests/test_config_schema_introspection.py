"""Tier 2: OS invariant — ReynConfig schema introspection (#1056 PR1).

Covers:
  - walk_config_schema() completeness (known nested key present + correct type/default)
  - _set() writes correctly nested YAML for 3-level dotted keys
  - _set() accepts free-form dict sub-keys (mcp, permissions, models, etc.)
  - _get() resolves dotted paths against a real loaded config
  - Invalid key rejection by is_valid_config_key()
  - Forward-ref robustness: walk completes without raising
  - None-value ≠ unknown-key: output_language default None is returned, not error
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
import yaml

from reyn.config import ReynConfig
from reyn.config_schema import (
    MISSING,
    SchemaNode,
    is_valid_config_key,
    resolve_config_value,
    walk_config_schema,
)

# ---------------------------------------------------------------------------
# Helper: write a minimal reyn.yaml so load_config() has a project root
# ---------------------------------------------------------------------------

def _project_root(tmp_path: Path) -> Path:
    """Create a minimal reyn.yaml so _find_project_root succeeds."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Walk completeness
# ---------------------------------------------------------------------------


def test_walk_includes_safety_loop_max_phase_visits() -> None:
    """Tier 2: walk includes the real 3-level nested field safety.loop.max_phase_visits."""
    nodes = walk_config_schema()
    keys = {n.key for n in nodes}
    assert "safety.loop.max_phase_visits" in keys, (
        "safety.loop.max_phase_visits missing from walk — walk may not be recursing"
    )


def test_walk_safety_loop_max_phase_visits_type_and_default() -> None:
    """Tier 2: safety.loop.max_phase_visits has type=int and default=25."""
    nodes = walk_config_schema()
    node = next(n for n in nodes if n.key == "safety.loop.max_phase_visits")
    assert node.type_repr == "int"
    assert node.default == 25
    assert node.is_dict_leaf is False


def test_walk_forward_ref_robustness() -> None:
    """Tier 2: walk completes without raising (forward-ref eval regression guard).

    ReynConfig contains forward-ref string annotations (ExternalTransportRouting,
    ActionRetrievalConfig, OAuthProviderConfig).  Naive get_type_hints() raises;
    the robust per-class-module resolver must not.
    """
    # Must not raise — if it does, forward-ref resolution is broken.
    nodes = walk_config_schema()
    assert len(nodes) > 0, "walk returned no nodes — catastrophic failure"


def test_walk_includes_free_form_dict_leaves() -> None:
    """Tier 2: known free-form dict fields are present with is_dict_leaf=True."""
    nodes = walk_config_schema()
    dict_keys = {n.key for n in nodes if n.is_dict_leaf}
    # permissions, mcp, models are the three documented free-form dicts at top level
    for expected in ("permissions", "mcp", "models"):
        assert expected in dict_keys, f"{expected!r} not in dict_leaf keys: {sorted(dict_keys)}"


def test_walk_covers_every_top_level_field_no_silent_skip() -> None:
    """Tier 2: every ReynConfig top-level field is represented in the walk
    (= no section silently skipped by the forward-ref empty-hints fallback).

    Drift-proof invariant (the framework's whole point): adding a config
    field auto-reflects in ``reyn config set/get/fields``. But
    ``_get_hints_safe`` returns ``{}`` on a forward-ref resolution failure,
    which makes ``_walk`` emit ZERO nodes for that class — silently dropping
    the section and re-creating the old allowlist-reject bug for a future
    field, with NO error (the original bug, but invisible).

    This test makes the drift loud: a dropped section → its field name
    absent from the walk's top-level keys → CI fail. If a new forward-ref
    dataclass field is added and its section disappears, this fails until
    the type is injected in ``config_schema._patch_localns`` (PR2 will
    auto-resolve and retire the manual injection).

    Fields explicitly flagged ``field(metadata={'schema_internal': True})``
    (#1146 — internal storage that is NOT an operator-settable key, e.g.
    ``mcp_search_threshold`` which the loader derives from
    ``mcp.search_threshold``) are intentionally omitted from the walk and so
    excluded here — their omission is deliberate, not a silent forward-ref drop.
    """
    nodes = walk_config_schema()
    top_level = {n.key.split(".", 1)[0] for n in nodes}
    field_names = {
        f.name
        for f in dataclasses.fields(ReynConfig)
        if not f.metadata.get("schema_internal", False)
    }
    missing = field_names - top_level
    assert not missing, (
        f"ReynConfig fields with NO node in the schema walk (silently "
        f"skipped — the section produced zero dotted keys): {sorted(missing)}. "
        f"Most likely a forward-ref resolution failure dropped the section; "
        f"inject the type in config_schema._patch_localns."
    )


def test_walk_forward_ref_sections_produce_nested_keys() -> None:
    """Tier 2: the forward-ref-bearing dataclass sections produce their nested
    keys (targeted regression guard for the _patch_localns injection).

    ``external_transports`` (ExternalTransportRouting) and ``auth``
    (OAuthProviderConfig) annotate forward refs that naive get_type_hints
    cannot resolve. If their injection in ``_patch_localns`` is dropped or a
    new forward-ref section is added without injection, the section silently
    yields zero nodes. This asserts each forward-ref section contributes at
    least one dotted key under its own prefix.
    """
    nodes = walk_config_schema()
    by_prefix: dict[str, int] = {}
    for n in nodes:
        by_prefix[n.key.split(".", 1)[0]] = by_prefix.get(n.key.split(".", 1)[0], 0) + 1
    # Each forward-ref section must contribute >= 1 node (key under its prefix).
    for section in ("external_transports", "auth"):
        # Only assert if the section is a real ReynConfig field (guards against
        # a future rename making the test silently vacuous).
        assert section in {f.name for f in dataclasses.fields(ReynConfig)}, (
            f"{section!r} is no longer a ReynConfig field — update this guard"
        )
        assert by_prefix.get(section, 0) >= 1, (
            f"forward-ref section {section!r} produced 0 nodes — its type "
            f"likely failed to resolve (check config_schema._patch_localns)"
        )


# ---------------------------------------------------------------------------
# 2. _set() nested write
# ---------------------------------------------------------------------------


def test_set_nested_three_level_produces_nested_yaml(tmp_path: Path) -> None:
    """Tier 2: _set safety.loop.max_phase_visits writes {safety: {loop: {max_phase_visits: 50}}}."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _set
        _set("safety.loop.max_phase_visits", "50")
        local_yaml = root / "reyn.local.yaml"
        assert local_yaml.exists(), "reyn.local.yaml not created by _set"
        data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        # Must NOT be flat {safety: {'loop.max_phase_visits': 50}}
        assert isinstance(data.get("safety"), dict), (
            f"safety key not a nested dict; got: {data}"
        )
        assert isinstance(data["safety"].get("loop"), dict), (
            f"safety.loop not a nested dict; got: {data['safety']}"
        )
        assert data["safety"]["loop"].get("max_phase_visits") == 50, (
            f"safety.loop.max_phase_visits not set correctly; got: {data}"
        )
        # Flat key must NOT exist (old bug: {'loop.max_phase_visits': 50})
        assert "loop.max_phase_visits" not in data.get("safety", {}), (
            f"Flat 'loop.max_phase_visits' key found — nested-write bug not fixed: {data}"
        )
    finally:
        os.chdir(old_cwd)


def test_set_two_level_nested_key(tmp_path: Path) -> None:
    """Tier 2: _set cost.rate_limit_warn_ratio writes {cost: {rate_limit_warn_ratio: 0.9}}."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _set
        _set("cost.rate_limit_warn_ratio", "0.9")
        local_yaml = root / "reyn.local.yaml"
        data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
        assert isinstance(data.get("cost"), dict)
        assert abs(data["cost"].get("rate_limit_warn_ratio", -1) - 0.9) < 1e-9
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 3. _set() free-form dict sub-key acceptance
# ---------------------------------------------------------------------------


def test_set_free_form_dict_subkey_mcp(tmp_path: Path) -> None:
    """Tier 2: mcp.servers.github.url is accepted without being enumerated."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _set
        # Must not sys.exit — free-form sub-keys under mcp are valid
        _set("mcp.servers.github.url", "https://api.example.com/mcp/")
        local_yaml = root / "reyn.local.yaml"
        data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
        assert isinstance(data.get("mcp"), dict)
    finally:
        os.chdir(old_cwd)


def test_set_free_form_dict_subkey_permissions(tmp_path: Path) -> None:
    """Tier 2: permissions.shell accepts arbitrary sub-key."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _set
        _set("permissions.shell", "allow")
        local_yaml = root / "reyn.local.yaml"
        data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
        assert data.get("permissions", {}).get("shell") == "allow"
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 4. _get() dotted-path resolution
# ---------------------------------------------------------------------------


def test_get_nested_key_resolves_correctly(tmp_path: Path, capsys) -> None:
    """Tier 2: _get('safety.loop.max_phase_visits') prints the default value, not an error."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _get
        _get("safety.loop.max_phase_visits")
        captured = capsys.readouterr()
        assert "25" in captured.out, (
            f"Expected '25' in output, got: {captured.out!r}"
        )
        assert captured.err == "", (
            f"Unexpected stderr: {captured.err!r}"
        )
    finally:
        os.chdir(old_cwd)


def test_get_top_level_scalar(tmp_path: Path, capsys) -> None:
    """Tier 2: _get('model') prints 'standard'."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _get
        _get("model")
        captured = capsys.readouterr()
        assert "standard" in captured.out
        assert captured.err == ""
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 5. Invalid key rejection
# ---------------------------------------------------------------------------


def test_is_valid_config_key_rejects_nonexistent() -> None:
    """Tier 2: a genuinely absent key is rejected by is_valid_config_key."""
    assert not is_valid_config_key("safety.loop.nonexistent_xyz_12345"), (
        "Nonexistent key should be invalid"
    )


def test_is_valid_config_key_accepts_known_nested() -> None:
    """Tier 2: safety.loop.max_phase_visits is accepted."""
    assert is_valid_config_key("safety.loop.max_phase_visits")


def test_is_valid_config_key_accepts_free_form_subkey() -> None:
    """Tier 2: mcp.servers.github.url is accepted as a free-form sub-key."""
    assert is_valid_config_key("mcp.servers.github.url")


def test_set_rejects_invalid_key(tmp_path: Path) -> None:
    """Tier 2: _set with an invalid key calls sys.exit(1)."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _set
        with pytest.raises(SystemExit) as exc_info:
            _set("safety.loop.totally_nonexistent_key_xyz", "42")
        assert exc_info.value.code == 1
    finally:
        os.chdir(old_cwd)


def test_get_rejects_invalid_key(tmp_path: Path) -> None:
    """Tier 2: _get with an invalid key calls sys.exit(1)."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _get
        with pytest.raises(SystemExit) as exc_info:
            _get("completely_nonexistent_key_xyz_12345")
        assert exc_info.value.code == 1
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 6. None value ≠ unknown key
# ---------------------------------------------------------------------------


def test_get_none_value_is_not_unknown_key(tmp_path: Path, capsys) -> None:
    """Tier 2: output_language default=None prints '(not set)', not 'unknown key'."""
    root = _project_root(tmp_path)
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        from reyn.interfaces.cli.commands.config import _get
        # Should NOT sys.exit — None is a legitimate value, not an unknown key
        _get("output_language")
        captured = capsys.readouterr()
        assert "unknown" not in captured.err.lower(), (
            f"_get('output_language') treated None default as unknown key: {captured.err!r}"
        )
        assert "(not set)" in captured.out, (
            f"Expected '(not set)' for None-default field, got: {captured.out!r}"
        )
    finally:
        os.chdir(old_cwd)


def test_resolve_config_value_none_not_unknown() -> None:
    """Tier 2: resolve_config_value returns (found=True, None) for output_language, not (False, None)."""
    config = ReynConfig()
    found, value = resolve_config_value(config, "output_language")
    assert found is True, "output_language should be found in schema"
    assert value is None, f"output_language default should be None, got {value!r}"


# ---------------------------------------------------------------------------
# 7. resolve_config_value contract
# ---------------------------------------------------------------------------


def test_resolve_nested_key_default_value() -> None:
    """Tier 2: resolve_config_value returns the default for safety.loop.max_phase_visits."""
    config = ReynConfig()
    found, value = resolve_config_value(config, "safety.loop.max_phase_visits")
    assert found is True
    assert value == 25


def test_resolve_unknown_key_returns_not_found() -> None:
    """Tier 2: resolve_config_value returns (False, None) for an absent key."""
    config = ReynConfig()
    found, value = resolve_config_value(config, "safety.loop.nonexistent_xyz")
    assert found is False
    assert value is None
