"""Tier 2: OS invariant — config-schema framework enforcement (#1056 PR2).

PR1 (`test_config_schema_introspection.py`) proved the walk *includes* known
nested keys + a top-level no-silent-skip drift guard. This PR2 file closes the
remaining enforcement gap:

  - **Every** scalar leaf the walk advertises actually takes effect through the
    real ``reyn config set`` → ``load_config`` → ``resolve_config_value`` path,
    set to a **non-default** value (not just the handful sampled in PR1, and not
    its own default — see #1146). This is the framework's whole promise —
    "adding a config field auto-reflects in reyn config set/get" — pinned for the
    entire schema, so a future field whose loader never reads the key (the
    operator's value silently discarded = a no-op set) fails loudly. A
    default-valued round-trip passes trivially for such a field; a non-default
    value exposes it.
  - **Every** free-form dict leaf accepts an arbitrary sub-key.
  - The 6 descriptions migrated out of the deleted ``CONFIG_FIELDS`` allowlist
    into ``field(metadata={'desc': ...})`` are preserved (regression guard for
    the list deletion + metadata migration). #4174 T3 moved 3 of the 6
    (``model`` / ``models`` / ``api_base``) from top-level ``ReynConfig``
    fields to nested ``LLMConfig`` fields (``llm.model`` etc.) — the guard
    keys below use the schema-walk's dotted path, and the anchoring test
    checks each key's OWN dataclass (``ReynConfig`` for the 3 that stayed
    top-level, ``LLMConfig`` for the 3 that moved), not just ``ReynConfig``.

These iterate the *live* ``walk_config_schema()`` output rather than a
hardcoded key list, so they auto-extend as config fields are added/removed —
the guard tracks the real schema, it does not pin a snapshot of it.
"""
from __future__ import annotations

import dataclasses
import os
import typing
from pathlib import Path

import yaml

from reyn.config import ReynConfig, load_config
from reyn.config.config_schema import (
    MISSING,
    SchemaNode,
    is_valid_config_key,
    resolve_config_value,
    walk_config_schema,
)
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

# Fields that silently COERCE an invalid value back to their default (rather
# than raising a validation error), so a generic "<default>_nd" candidate can't
# distinguish "loader read+coerced" from a real no-op. Supply a domain-valid
# non-default so the guard stays decisive. Keyed by dotted key; the iteration
# itself is still live-walk-derived — this only overrides the candidate VALUE.
#
# `audit_events.backend` (#4496 PR-2) does NOT need an entry here: it is
# typed `Literal["local", "discard"]`, not a bare coercing `str`, so
# `_nondefault_candidate`'s existing Literal branch (below) already picks
# a real, non-default member automatically — no override needed. Left as
# the discriminator for FUTURE soft-coercing string fields (the override
# mechanism itself stays correct; this dict is empty because nothing
# currently needs it, not because it doesn't work).
_VALID_NONDEFAULT_OVERRIDES: dict[str, object] = {}


def _nondefault_candidate(node: SchemaNode) -> object | None:
    """Return a non-default value for a scalar leaf, or None to skip it.

    Derives a distinct-but-type-valid value from the node's default + field
    type. ``Literal`` members come from the type itself; list leaves are skipped
    (structured entries need shaped dicts — covered by their own builder tests).
    """
    if node.key in _VALID_NONDEFAULT_OVERRIDES:
        return _VALID_NONDEFAULT_OVERRIDES[node.key]
    d = node.default
    t = node.field_type
    if typing.get_origin(t) is typing.Literal:
        for member in typing.get_args(t):
            if member != d:
                return member
        return None
    if isinstance(d, bool):
        return not d
    if isinstance(d, int):
        return d + 1
    if isinstance(d, float):
        return round(d / 2, 6) if 0.0 < d < 1.0 else (d + 1.0 if d != 0.0 else 1.0)
    if isinstance(d, str):
        return (d + "_nd") if d else "nd_val"
    if isinstance(d, list):
        return None  # structured-list leaves need shaped entries — skip
    if d is None:
        if t is bool:
            return True
        if t is int:
            return 1
        if t is float:
            return 1.0
        return "nd_val"
    return None

# Top-level fields whose human descriptions were migrated from the (now
# deleted) hand-maintained ``CONFIG_FIELDS`` allowlist into field metadata.
_MIGRATED_DESC_KEYS = (
    "llm.model",
    "llm.models",
    "llm.api_base",
    "output_language",
    "permissions",
)


def _project_root(tmp_path: Path) -> Path:
    """Create a minimal reyn.yaml so _find_project_root / load_config succeed."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    return tmp_path


def test_every_scalar_leaf_takes_effect_on_nondefault_set(tmp_path: Path) -> None:
    """Tier 2: setting a NON-DEFAULT value on every scalar leaf actually takes effect.

    The original default-valued round-trip (#1142) passed *trivially* for a field
    the loader silently ignores: set X → reload → default, and default == X when X
    is the default. #1146: set a value ≠ default so a no-op-set field (loader never
    reads the key — e.g. a parse-derived alias like the formerly-dead
    ``ReynConfig.mcp_search_threshold`` (fold-removed #3218 / FP-0066 §7 P1a), or a
    field declared + consumed but never wired into ``load_config`` like the
    formerly-dead ``prompt_cache_enabled`` / ``project_context_path``) is caught.

    Per field, set V ≠ default and reload (each in an isolated project root so one
    field's value can't perturb another's load). Then require EITHER:
      - the value round-trips (``resolve == V``) — loader read the key, OR
      - ``load_config`` raises (the candidate hit the field's validation) — which
        also proves the loader *read* the key.
    A field that silently returns its default after a non-default set is a no-op
    (the operator's value is discarded on reload) = FAIL.
    """
    from reyn.interfaces.cli.commands.config import _set

    nodes = [n for n in walk_config_schema() if not n.is_dict_leaf]
    assert nodes, "walk returned no scalar leaves — catastrophic introspection failure"

    noops: list[str] = []
    checked = 0
    old_cwd = os.getcwd()
    # load_config() materializes some config keys into process env
    # (LITELLM_API_BASE from api_base, REYN_MCP_REGISTRY_URLS, REYN_FETCH_
    # ALLOW_PRIVATE_IPS). This test loads a synthetic config per leaf — snapshot
    # + restore os.environ so those exports don't leak across the loop OR past
    # this test into the rest of the suite (the same per-field isolation the
    # per-leaf project root already gives on disk).
    old_environ = dict(os.environ)
    try:
        for i, node in enumerate(nodes):
            if node.default is MISSING:
                continue
            cand = _nondefault_candidate(node)
            if cand is None or cand == node.default:
                continue
            root = tmp_path / f"leaf{i}"
            root.mkdir()
            (root / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
            os.chdir(root)
            _set(node.key, yaml.safe_dump(cand, default_flow_style=True).strip())
            try:
                cfg = load_config()
            except Exception:
                # Loader read the key and rejected the candidate during
                # validation → the key IS wired (not a no-op). Counts as covered.
                checked += 1
                continue
            checked += 1
            found, got = resolve_config_value(cfg, node.key)
            if not found or got != cand:
                noops.append(f"{node.key}: set {cand!r} → reload {got!r} (set ignored)")
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environ)

    assert checked > 0, "no scalar leaf exercised — candidate generator returned None for all"
    assert not noops, (
        "config leaves whose set is a NO-OP (advertised by the schema walk + "
        "set-able, but load_config never reads the key, so the operator's value "
        "is silently discarded on reload): " + "; ".join(noops)
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
    is renamed — every dotted key in ``_MIGRATED_DESC_KEYS`` must remain a
    real field on the dataclass its OWN path names: a bare name (no dot) is a
    top-level ``ReynConfig`` field; a dotted ``llm.<leaf>`` name (#4174 T3:
    ``model`` / ``models`` / ``api_base`` moved from top-level to nested)
    must be a real ``LLMConfig`` field instead.
    """
    from reyn.config import LLMConfig

    reyn_field_names = {f.name for f in dataclasses.fields(ReynConfig)}
    llm_field_names = {f.name for f in dataclasses.fields(LLMConfig)}
    stale = []
    for k in _MIGRATED_DESC_KEYS:
        if "." in k:
            prefix, leaf = k.split(".", 1)
            if prefix != "llm" or leaf not in llm_field_names:
                stale.append(k)
        elif k not in reyn_field_names:
            stale.append(k)
    assert not stale, (
        f"_MIGRATED_DESC_KEYS names that are no longer real fields on the "
        f"dataclass their own path names (update the guard): {stale}"
    )
