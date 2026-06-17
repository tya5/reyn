"""Tier 2: config separation — ``.reyn/mcp.yaml`` is the canonical
write target for op-managed MCP server registry (#470, 2026-05-22).

Pins the architectural separation between static deployment config
(= ``reyn.yaml`` / ``reyn.local.yaml`` — operator-owned, edit + restart)
and dynamic MCP server registry (= ``.reyn/mcp.yaml`` — op-managed,
runtime-mutable).

Backward compat: existing ``reyn.yaml`` ``mcp.servers`` blocks continue
to load (= operator-hand-edited entries still work); new installs
land in the new location.

Tier 2 because the separation is the foundational invariant for the
config UX axis — a regression that re-introduced mcp.servers writes
to reyn.yaml would silently re-mix the static / dynamic kinds.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── Loader: .reyn/mcp.yaml is read and merged ────────────────────────


def test_load_config_reads_dynamic_mcp_yaml(tmp_path, monkeypatch):
    """Tier 2: ``load_config`` reads ``.reyn/mcp.yaml`` and merges its
    ``mcp.servers`` into the merged config. New canonical location
    for dynamic registry.
    """
    from reyn.config import load_config

    # Plant a reyn.yaml so _find_project_root finds tmp_path.
    _write_yaml(tmp_path / "reyn.yaml", "model: standard\n")
    _write_yaml(
        tmp_path / ".reyn" / "mcp.yaml",
        "mcp:\n  servers:\n    sqlite:\n      type: stdio\n      command: npx\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert "sqlite" in config.mcp.get("servers", {})
    sqlite_entry = config.mcp["servers"]["sqlite"]
    assert sqlite_entry["type"] == "stdio"
    assert sqlite_entry["command"] == "npx"


def test_load_config_dynamic_mcp_yaml_overrides_reyn_yaml(tmp_path, monkeypatch):
    """Tier 2: when both ``reyn.yaml`` and ``.reyn/mcp.yaml`` carry
    entries for the same server, the dynamic file wins (= newer
    op-managed value beats older operator-edited value). Backward
    compat is preserved by READING both; conflict resolution favours
    the runtime-mutable source.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: old-cmd\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "mcp.yaml",
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: new-cmd\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert config.mcp["servers"]["git"]["command"] == "new-cmd"


def test_load_config_dynamic_mcp_yaml_and_reyn_yaml_servers_union(
    tmp_path, monkeypatch,
):
    """Tier 2: a server present only in reyn.yaml (= operator legacy)
    and one only in ``.reyn/mcp.yaml`` (= new install) both surface
    in the merged config. The merge is a UNION on the servers dict,
    not an override of the whole section.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    legacy:\n      type: stdio\n      command: legacy-cmd\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "mcp.yaml",
        "mcp:\n  servers:\n    fresh:\n      type: stdio\n      command: fresh-cmd\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    servers = config.mcp["servers"]
    assert "legacy" in servers
    assert "fresh" in servers
    assert servers["legacy"]["command"] == "legacy-cmd"
    assert servers["fresh"]["command"] == "fresh-cmd"


def test_load_config_works_without_dynamic_mcp_yaml(tmp_path, monkeypatch):
    """Tier 2: an existing project with no ``.reyn/mcp.yaml`` (= the
    common case during the migration window) still loads cleanly.
    No spurious empty-section, no warning.
    """
    from reyn.config import load_config

    _write_yaml(
        tmp_path / "reyn.yaml",
        "model: standard\n"
        "mcp:\n  servers:\n    only-in-reyn:\n      type: stdio\n      command: x\n",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config(cwd=tmp_path)

    assert "only-in-reyn" in config.mcp.get("servers", {})


# ── mcp_install: write target redirected ─────────────────────────────


def test_mcp_install_scope_to_path_returns_dot_reyn_mcp_yaml(tmp_path):
    """Tier 2: ``_scope_to_path`` (= mcp_install) now returns
    ``.reyn/mcp.yaml`` regardless of scope arg. The scope kwarg is a
    no-op for CLI backward compat; the architectural decision is "one
    canonical dynamic registry location".
    """
    from reyn.core.op_runtime.mcp_install import _scope_to_path

    for scope in ("local", "project", "user", ""):
        result = _scope_to_path(scope, tmp_path)
        assert result == tmp_path / ".reyn" / "mcp.yaml", (
            f"scope={scope!r} should resolve to .reyn/mcp.yaml, "
            f"got {result}"
        )


# ── mcp_drop_server: legacy detection + new ──────────────────────────


def test_mcp_drop_detects_dynamic_scope_first(tmp_path):
    """Tier 2: when a server exists in both ``.reyn/mcp.yaml`` AND
    a legacy location, ``_detect_scope`` returns ``"dynamic"`` first
    (= canonical drop target is the newer location).
    """
    from reyn.core.op_runtime.mcp_drop_server import _detect_scope

    _write_yaml(
        tmp_path / "reyn.yaml",
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: old\n",
    )
    _write_yaml(
        tmp_path / ".reyn" / "mcp.yaml",
        "mcp:\n  servers:\n    git:\n      type: stdio\n      command: new\n",
    )

    assert _detect_scope("git", tmp_path) == "dynamic"


def test_mcp_drop_detects_legacy_when_dynamic_missing(tmp_path):
    """Tier 2: a server present only in legacy reyn.yaml (= operator
    pre-migration) is still droppable — ``_detect_scope`` walks
    through ``dynamic`` → ``local`` → ``project`` → ``user``.
    """
    from reyn.core.op_runtime.mcp_drop_server import _detect_scope

    _write_yaml(
        tmp_path / "reyn.yaml",
        "mcp:\n  servers:\n    legacy-only:\n      type: stdio\n      command: x\n",
    )

    assert _detect_scope("legacy-only", tmp_path) == "project"


def test_mcp_drop_returns_none_for_absent_server(tmp_path):
    """Tier 2: a server absent from all scopes returns None — drop
    op handles this as "nothing to remove".
    """
    from reyn.core.op_runtime.mcp_drop_server import _detect_scope

    # No reyn.yaml, no .reyn/mcp.yaml.
    assert _detect_scope("nope", tmp_path) is None


def test_mcp_drop_scope_to_path_dynamic_returns_dot_reyn_mcp_yaml(tmp_path):
    """Tier 2: ``_scope_to_path("dynamic", root)`` (= mcp_drop) returns
    ``.reyn/mcp.yaml``. The new scope name lets ``_detect_scope``
    identify which location to mutate after finding the server there.
    """
    from reyn.core.op_runtime.mcp_drop_server import _scope_to_path

    assert _scope_to_path("dynamic", tmp_path) == tmp_path / ".reyn" / "mcp.yaml"


def test_mcp_drop_legacy_scopes_unchanged(tmp_path):
    """Tier 2: legacy scope names (= ``local`` / ``project`` / ``user``)
    still resolve to the same legacy locations they always did. New
    ``dynamic`` scope is additive; old behaviour is preserved for
    backward-compat drop paths.
    """
    from reyn.core.op_runtime.mcp_drop_server import _scope_to_path

    assert _scope_to_path("local", tmp_path) == tmp_path / "reyn.local.yaml"
    assert _scope_to_path("project", tmp_path) == tmp_path / "reyn.yaml"
    # "user" goes to home dir; we just verify the suffix to avoid
    # coupling to the test runner's $HOME.
    user_path = _scope_to_path("user", tmp_path)
    assert user_path.name == "config.yaml"
    assert ".reyn" in user_path.parts
