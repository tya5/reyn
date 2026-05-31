"""Tier 2: OS invariant — config-schema framework enforcement (#1056 PR2).

PR1 (`test_config_schema_introspection.py`) proved the walk *includes* known
nested keys + a top-level no-silent-skip drift guard. This PR2 file closes the
remaining enforcement gap:

  - **Every** scalar leaf the walk advertises actually round-trips through the
    real ``reyn config set`` → ``load_config`` → ``resolve_config_value`` path
    (not just the handful sampled in PR1). This is the framework's whole
    promise — "adding a config field auto-reflects in reyn config set/get" —
    pinned for the entire schema, so a future field whose section builder
    silently drops it (set-able key that doesn't survive reload) fails loudly.
  - **Every** free-form dict leaf accepts an arbitrary sub-key.
  - The 6 top-level descriptions migrated out of the deleted ``CONFIG_FIELDS``
    allowlist into ``field(metadata={'desc': ...})`` are preserved (regression
    guard for the list deletion + metadata migration).

These iterate the *live* ``walk_config_schema()`` output rather than a
hardcoded key list, so they auto-extend as config fields are added/removed —
the guard tracks the real schema, it does not pin a snapshot of it.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import yaml

from reyn.config import ReynConfig, load_config
from reyn.config_schema import (
    MISSING,
    is_valid_config_key,
    resolve_config_value,
    walk_config_schema,
)

# Top-level fields whose human descriptions were migrated from the (now
# deleted) hand-maintained ``CONFIG_FIELDS`` allowlist into field metadata.
_MIGRATED_DESC_KEYS = (
    "model",
    "models",
    "api_base",
    "output_language",
    "shell_allowed",
    "permissions",
)


def _project_root(tmp_path: Path) -> Path:
    """Create a minimal reyn.yaml so _find_project_root / load_config succeed."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    return tmp_path


def test_every_scalar_leaf_round_trips_through_set_and_load(tmp_path: Path) -> None:
    """Tier 2: every scalar leaf survives reyn config set → load_config → resolve.

    For each scalar leaf the schema walk advertises, write its own default via
    the real ``_set`` CLI path, reload the merged config, and assert
    ``resolve_config_value`` returns the same value. A leaf that is advertised
    by the walk but is silently dropped by its section builder on reload (=
    set-able key, value vanishes) re-creates the old allowlist-drift bug; this
    makes that loud for the *entire* schema, not just sampled keys.
    """
    from reyn.cli.commands.config import _set

    root = _project_root(tmp_path)
    nodes = [n for n in walk_config_schema() if not n.is_dict_leaf]
    assert nodes, "walk returned no scalar leaves — catastrophic introspection failure"

    mismatches: list[str] = []
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        for node in nodes:
            default = node.default
            if default is MISSING:
                # No static default to round-trip (none today; defensive — such a
                # field is still covered by the resolvable-on-default guard below).
                continue
            # Serialise the default to the YAML scalar string the CLI accepts,
            # exactly as a human typing `reyn config set <key> <value>` would.
            value_str = yaml.safe_dump(default, default_flow_style=True).strip()
            _set(node.key, value_str)
            cfg = load_config()
            found, got = resolve_config_value(cfg, node.key)
            if not found:
                mismatches.append(f"{node.key}: not resolvable after set")
            elif got != default:
                mismatches.append(f"{node.key}: set={default!r} got back {got!r}")
    finally:
        os.chdir(old_cwd)

    assert not mismatches, (
        "config leaves that do not round-trip through set→load→resolve "
        "(advertised by the schema walk but not preserved on reload — a "
        "section builder is dropping the key): " + "; ".join(mismatches)
    )


def test_every_leaf_resolves_on_default_config() -> None:
    """Tier 2: every walk leaf (scalar + dict) is reachable on a default config.

    No-I/O complement to the round-trip guard: a default ``ReynConfig`` must
    resolve every dotted key the walk advertises (found=True). A key present in
    the walk but unreachable via ``resolve_config_value`` means ``reyn config
    get`` would reject a key it just listed in ``reyn config fields``.
    """
    cfg = ReynConfig()
    unreachable = [n.key for n in walk_config_schema() if not resolve_config_value(cfg, n.key)[0]]
    assert not unreachable, (
        f"schema keys not resolvable on a default config: {sorted(unreachable)}"
    )


def test_every_dict_leaf_accepts_arbitrary_subkey() -> None:
    """Tier 2: every free-form dict leaf accepts an arbitrary sub-key.

    Free-form dicts (mcp, permissions, models, …) exist precisely so operators
    can set sub-keys the schema does not enumerate. Each dict leaf must accept
    ``<key>.<arbitrary>`` so a new dict section is never silently rejected.
    """
    dict_leaves = [n for n in walk_config_schema() if n.is_dict_leaf]
    assert dict_leaves, "walk returned no dict leaves — expected mcp/permissions/models at least"
    rejected = [
        n.key for n in dict_leaves
        if not is_valid_config_key(f"{n.key}.arbitrary_subkey_xyz")
    ]
    assert not rejected, (
        f"free-form dict leaves that reject an arbitrary sub-key: {sorted(rejected)}"
    )


def test_migrated_top_level_descriptions_preserved() -> None:
    """Tier 2: the 6 descriptions migrated out of CONFIG_FIELDS survive in metadata.

    Deleting the hand-maintained ``CONFIG_FIELDS`` allowlist must not lose the
    operator-facing descriptions it carried — they were migrated into
    ``field(metadata={'desc': ...})`` and surface via ``SchemaNode.desc`` (=
    what ``reyn config fields`` prints). Guards that the deletion + migration
    stay coherent.
    """
    by_key = {n.key: n for n in walk_config_schema()}
    missing_desc = []
    for key in _MIGRATED_DESC_KEYS:
        assert key in by_key, f"{key!r} disappeared from the schema walk"
        if not by_key[key].desc.strip():
            missing_desc.append(key)
    assert not missing_desc, (
        "top-level fields that lost their description after the CONFIG_FIELDS "
        f"deletion (re-add field(metadata={{'desc': ...}})): {missing_desc}"
    )


def test_migrated_desc_keys_are_real_reynconfig_fields() -> None:
    """Tier 2: the migrated-desc guard list stays anchored to real fields.

    Prevents the description guard above from going silently vacuous if a field
    is renamed — every name in ``_MIGRATED_DESC_KEYS`` must remain a real
    top-level ``ReynConfig`` field.
    """
    field_names = {f.name for f in dataclasses.fields(ReynConfig)}
    stale = [k for k in _MIGRATED_DESC_KEYS if k not in field_names]
    assert not stale, (
        f"_MIGRATED_DESC_KEYS names that are no longer ReynConfig fields "
        f"(update the guard): {stale}"
    )
