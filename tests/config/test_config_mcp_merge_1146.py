"""Tier 2: OS invariant — mcp config merge (#1146).

``_merge``'s ``mcp`` branch used to drop the override's non-``servers`` keys
(``{**existing, "servers": union}``), so scalar sub-keys of ``mcp:`` (e.g.
``mcp.registries``) set in any config layer were silently discarded (always
the default). The fix (``{**existing, **val, "servers": union}``) preserves
override scalars while keeping the server union — verified here that
``mcp.registries`` now takes effect, and that the server union still works
(regression guard for the existing ``test_config_mcp_headers`` behavior).

#3218 / FP-0066 §7 P1a: ``ReynConfig.mcp_search_threshold`` (the derived,
``schema_internal``-flagged field this module's merge fix used as its worked
example — a confirmed no-op, never threaded to ``build_tools()``) was
fold-removed. ``mcp:`` stays a raw, unvalidated dict (no schema on its
sub-keys), so a bare ``mcp.search_threshold:`` in reyn.yaml is now simply an
inert free-form key inside ``cfg.mcp`` — same "unknown key inside a raw
dict block" policy as any other undeclared ``mcp:`` sub-key, verified below
in place of the old derived-field assertions.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.config import load_config


def _root(tmp_path: Path) -> Path:
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    return tmp_path


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
    (project ∪ local) AND now also carry an arbitrary scalar key (here the
    now-inert ``search_threshold``, #3218) set in one layer — both coexist in
    the raw ``cfg.mcp`` dict.
    """
    root = tmp_path
    (root / "reyn.yaml").write_text(
        "llm:\n  model: standard\n"
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
    # #3218: no derived field reads this anymore, but the raw dict merge still
    # preserves the scalar key alongside the server union (unknown-key-inside-
    # a-raw-dict policy — it is inert, not dropped).
    assert cfg.mcp.get("search_threshold") == 5, (
        "scalar key lost from the raw mcp dict when servers also present"
    )
