"""Tier 2: #5848 (delete `PermissionDecl.tool`) + #5849 ①③ (delete
`exec: allow`, close the `permissions:` unknown-key gap).

#5849③ is the structural fix both issues share a root with: before this,
`config_schema.py` treated the WHOLE `permissions:` block as an opaque
free-form leaf (Kind② — "genuinely open, deliberately unchecked"), so
`permissions.exec:`, `permissions.tool:`, or any operator typo under
`permissions:` accepted silently regardless of whether a real consumer
ever read it. `unknown_permissions_config_keys` (`security/permissions/
permissions.py`) is the ONE registry both the `config_schema` unknown-key
walk (registered from `config/infra.py`) and any future direct caller
read from — derived from the real consumers (`from_dict`'s own parsed
keys + the pre-approval gate's own literal/composite keys), not
hand-guessed.

Real registries/functions throughout — no mocks.
"""
from __future__ import annotations

from reyn.config import config_schema  # noqa: F401 -- import registers the validator
from reyn.security.permissions.permissions import (
    PERMISSIONS_EXACT_CONFIG_KEYS,
    PermissionDecl,
    unknown_permissions_config_keys,
)


def test_permissions_leaf_is_registered_validated_not_open():
    """Tier 1: #5849③ — `permissions` moved from Kind② (declared-open) to
    Kind① (a real validator), the #4655 registration-kind read surface."""
    assert config_schema.freeform_leaf_registration_kind("permissions") == "validated"


def test_exec_is_unknown_after_5849_clean_break():
    """Tier 2: accept ① — `permissions.exec:` (`allow` or `deny`) now warns.
    #5849's own root finding: there was never an interactive `exec` prompt
    for `allow` to skip, and `exec` has no reader in `from_dict` or the
    pre-approval gate."""
    hits = config_schema.unknown_config_keys({"permissions": {"exec": "allow"}})
    assert "permissions.exec" in hits
    hits2 = config_schema.unknown_config_keys({"permissions": {"exec": "deny"}})
    assert "permissions.exec" in hits2


def test_tool_is_unknown_after_5848_delete():
    """Tier 2: accept — `permissions.tool:` (the deleted decl-list shape)
    now warns. `require_tool`'s own confirm-approval vocabulary (a NESTED
    `tool: {name: allow}` shape) is deliberately ALSO unrecognized here
    (see the registry's own docstring: zero production callers of
    require_tool per #5841, so nothing currently reads a `tool` key in
    any shape)."""
    hits = config_schema.unknown_config_keys({"permissions": {"tool": ["x"]}})
    assert "permissions.tool" in hits


def test_owners_real_config_shape_warns_on_exec_only():
    """Tier 2: accept — the owner's REAL config
    (`permissions: {exec: allow, file.delete: deny, mcp: {github: allow}}`
    — #5849's own cited example) warns on `exec` alone; `file.delete` and
    `mcp` (with its nested server nesting) are untouched."""
    hits = config_schema.unknown_config_keys({
        "permissions": {
            "exec": "allow", "file.write": "allow", "mcp": {"github": "allow"},
        },
    })
    assert set(hits) == {"permissions.exec"}


def test_file_write_still_works_unwarned_regression_control():
    """Tier 2: non-regression — a real, still-consumed key never warns."""
    hits = config_schema.unknown_config_keys({"permissions": {"file.write": "allow"}})
    assert hits == {}


def test_http_get_composite_host_key_is_recognized_via_prefix():
    """Tier 2: accept — the composite flat-string pre-approval key shape
    (`f"http.get.{host}"`, `_is_config_approved`'s own literal construction)
    is recognized via the prefix registry, for an arbitrary host name."""
    hits = config_schema.unknown_config_keys({
        "permissions": {"http.get.example.com": "deny", "http.get.another-host.io": "allow"},
    })
    assert hits == {}


def test_python_mode_composite_key_is_recognized_via_prefix():
    """Tier 2: accept — `python.safe: allow` (`reyn init`'s own generated
    `reyn.yaml` template, `interfaces/cli/templates.py`) is recognized via
    the `PYTHON_MODE_PREFIX` registry entry, the same flat-composite shape
    as `http.get.<host>`. Disclosed elsewhere (the registry's own
    `PYTHON_MODE_PREFIX` comment): no live reader currently consumes this
    key — recognized so `reyn init`'s own output validates clean, not
    because the mechanism is confirmed reachable."""
    hits = config_schema.unknown_config_keys({
        "permissions": {"python.safe": "allow", "python.unsafe": "allow"},
    })
    assert hits == {}


def test_legacy_bool_axis_keys_are_recognized_not_double_reported():
    """Tier 2: the #571 collapse-arc legacy bool-axis keys (which
    `from_dict` recognizes and warns about via its OWN DeprecationWarning
    loop) must not ALSO surface here as unknown — one warning per key, not
    two different ones with different wording."""
    hits = config_schema.unknown_config_keys({
        "permissions": {
            "mcp_install": True, "mcp_drop_server": True,
            "cron_register": True, "index_drop": True,
        },
    })
    assert hits == {}


def test_derivation_is_real_strip_file_write_from_registry_warns(monkeypatch):
    """Tier 2: the strip falsify — removing `file.write` from the registry
    makes an OTHERWISE-valid `file.write: allow` config warn too, proving
    the unknown-key walk actually reads the registry rather than having a
    hardcoded exemption for well-known keys."""
    import reyn.security.permissions.permissions as perm_module

    stripped = frozenset(k for k in PERMISSIONS_EXACT_CONFIG_KEYS if k != "file.write")
    monkeypatch.setattr(perm_module, "PERMISSIONS_EXACT_CONFIG_KEYS", stripped)
    hits = config_schema.unknown_config_keys({"permissions": {"file.write": "allow"}})
    assert "permissions.file.write" in hits


def test_unknown_permissions_config_keys_handles_non_dict_input():
    """Tier 2: a malformed (non-dict) `permissions:` value doesn't crash
    the registry's own detection function — same fail-secure discipline
    every other builder in this module has."""
    assert unknown_permissions_config_keys("oops") == frozenset()
    assert unknown_permissions_config_keys(None) == frozenset()
    assert unknown_permissions_config_keys({}) == frozenset()


# ── #5848: PermissionDecl.tool is genuinely gone ────────────────────────────


def test_permission_decl_has_no_tool_field():
    """Tier 1: `PermissionDecl` (a Python dataclass) genuinely has no
    `tool` field — a third-party/language-level fact about the class
    itself, not this test's own claim."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(PermissionDecl)}
    assert "tool" not in field_names


def test_from_dict_silently_drops_a_stray_tool_key():
    """Tier 2: `PermissionDecl.from_dict({"tool": [...]})` loads cleanly
    (no crash) and produces a decl with no `.tool` attribute — the same
    "removed key silently dropped" discipline `from_dict` already applies
    to the PYTHON axis's own stray key. Silent, not warned, BY DESIGN
    (architect ruling on #5862's co-vet): `from_dict`'s one production
    caller hands it a dict (`PermissionResolver._config`, reyn.yaml's own
    `permissions:` block) that `config_schema`'s unknown-key walk already
    checked once at config-load time — re-checking it here would
    double-report the identical stray key on every read."""
    decl = PermissionDecl.from_dict({"tool": ["grep"], "mcp": ["filesystem"]})
    assert not hasattr(decl, "tool")
    assert decl.mcp == ["filesystem"]
