"""Tier 2: OS invariant — mcp config merge + schema-internal field (#1146).

Two coupled fixes, both exercised through the public ``load_config`` surface:

1. ``_merge``'s ``mcp`` branch used to drop the override's non-``servers`` keys
   (``{**existing, "servers": union}``), so ``mcp.search_threshold`` and
   ``mcp.registries`` set in any config layer were silently discarded (always
   the default). The fix (``{**existing, **val, "servers": union}``) preserves
   override scalars while keeping the server union — verified here that both now
   take effect, and that the server union still works (regression guard for the
   existing ``test_config_mcp_headers`` behavior).

2. ``mcp_search_threshold`` is internal storage the loader derives from
   ``mcp.search_threshold``; it is not an operator-settable top-level key
   (setting it would be a no-op on reload). It is flagged
   ``field(metadata={'schema_internal': True})`` so ``walk_config_schema`` omits
   it — verified the operator key ``mcp.search_threshold`` stays settable while
   the top-level alias is no longer advertised, and the consumer field is intact.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.config import ReynConfig, load_config
from reyn.config.config_schema import (
    is_valid_config_key,
    resolve_config_value,
    walk_config_schema,
)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    return tmp_path


def test_mcp_search_threshold_set_via_operator_key_takes_effect(tmp_path: Path) -> None:
    """Tier 2: ``mcp.search_threshold`` set in config reaches cfg.mcp_search_threshold.

    Pre-fix the merge dropped it → always the 30 default. Guards the override-
    scalar-preservation in ``_merge``.
    """
    root = _root(tmp_path)
    (root / "reyn.local.yaml").write_text(
        "mcp:\n  search_threshold: 88\n", encoding="utf-8"
    )
    old = os.getcwd()
    os.chdir(root)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)
    assert cfg.mcp_search_threshold == 88, (
        f"mcp.search_threshold=88 did not reach the derived field "
        f"(got {cfg.mcp_search_threshold}) — merge dropped the override scalar"
    )
    assert resolve_config_value(cfg, "mcp.search_threshold")[1] == 88


def test_mcp_registries_set_in_config_takes_effect(tmp_path: Path) -> None:
    """Tier 2: ``mcp.registries`` set in config survives merge + exports the env var.

    Pre-fix the merge dropped ``registries`` so the propagation at config.py:2020
    never fired. Guards the same override-scalar-preservation fix.

    ``load_config`` *sets* ``REYN_MCP_REGISTRY_URLS`` as a side effect (and only
    when it is unset), so this test explicitly saves/clears/restores both env
    vars itself — pytest's ``monkeypatch.delenv`` does not track a var the code
    under test creates, which would otherwise leak into later registry tests.
    """
    saved = {k: os.environ.pop(k, None) for k in ("REYN_MCP_REGISTRY_URLS", "REYN_MCP_REGISTRY_URL")}
    root = _root(tmp_path)
    (root / "reyn.local.yaml").write_text(
        "mcp:\n  registries:\n    - https://reg.example.com/v1\n", encoding="utf-8"
    )
    old = os.getcwd()
    os.chdir(root)
    try:
        cfg = load_config()
        assert cfg.mcp.get("registries") == ["https://reg.example.com/v1"], (
            f"mcp.registries did not survive the merge: {cfg.mcp.get('registries')!r}"
        )
        assert os.environ.get("REYN_MCP_REGISTRY_URLS") == "https://reg.example.com/v1"
    finally:
        os.chdir(old)
        os.environ.pop("REYN_MCP_REGISTRY_URLS", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_mcp_servers_union_preserved_across_layers(tmp_path: Path) -> None:
    """Tier 2: server entries from different layers union, alongside scalar keys.

    Regression guard: the merge fix must keep the existing servers-union behavior
    (project ∪ local) AND now also carry a scalar key (search_threshold) set in
    one layer — both coexist.
    """
    root = tmp_path
    (root / "reyn.yaml").write_text(
        "model: standard\n"
        "mcp:\n  search_threshold: 5\n  servers:\n    alpha:\n      type: stdio\n      command: a\n",
        encoding="utf-8",
    )
    (root / "reyn.local.yaml").write_text(
        "mcp:\n  servers:\n    beta:\n      type: stdio\n      command: b\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(root)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)
    servers = cfg.mcp.get("servers") or {}
    assert "alpha" in servers and "beta" in servers, (
        f"server union broken — expected alpha+beta, got {sorted(servers)}"
    )
    assert cfg.mcp_search_threshold == 5, "scalar key lost when servers also present"


def test_mcp_search_threshold_field_is_unadvertised_but_operator_key_valid() -> None:
    """Tier 2: top-level mcp_search_threshold omitted from the schema; operator key valid.

    The internal field is flagged schema_internal so the walk omits it (a no-op
    set is not advertised), while the real operator key ``mcp.search_threshold``
    (a free-form sub-key of the ``mcp`` dict) stays valid and the consumer field
    survives on the dataclass.
    """
    walk_keys = {n.key for n in walk_config_schema()}
    assert "mcp_search_threshold" not in walk_keys, (
        "mcp_search_threshold should be omitted from the settable schema "
        "(schema_internal) — it's a no-op set"
    )
    assert not is_valid_config_key("mcp_search_threshold")
    assert is_valid_config_key("mcp.search_threshold"), (
        "the real operator key mcp.search_threshold must stay settable"
    )
    # Consumer field intact (router_tools reads cfg.mcp_search_threshold).
    assert ReynConfig().mcp_search_threshold == 30
