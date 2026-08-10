"""Tier 2: pure helpers in config/loader.py.

``_as_config_dict(val, key)``   — coerce to dict, default {} on wrong type
``_merge(base, override)``      — None values skip; unknown key overrides;
                                   models/permissions shallow-merge
``_find_project_root(start)``   — walk up until reyn.yaml found or root hit

#3218 / FP-0066 §7 P1a: ``_parse_mcp_search_threshold`` (+ its dead
``ReynConfig.mcp_search_threshold`` field) was fold-removed as a confirmed
no-op — the parsed value was never threaded through to ``build_tools()`` by
either router_loop.py call site. Its unit tests are removed with it (clean
break, no alias).
"""
from __future__ import annotations

import sys
from pathlib import Path

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.config.loader import (
    _as_config_dict,
    _find_project_root,
    _merge,
)

# ---------------------------------------------------------------------------
# _as_config_dict
# ---------------------------------------------------------------------------


def test_as_config_dict_none_returns_empty() -> None:
    """Tier 2: None → empty dict (missing key graceful default)."""
    assert _as_config_dict(None, "models") == {}


def test_as_config_dict_dict_passthrough() -> None:
    """Tier 2: dict value is returned as-is."""
    d = {"foo": "bar"}
    assert _as_config_dict(d, "models") is d


def test_as_config_dict_string_returns_empty() -> None:
    """Tier 2: scalar string → {} (user typo: models: standard_string)."""
    assert _as_config_dict("standard", "models") == {}


def test_as_config_dict_list_returns_empty() -> None:
    """Tier 2: list → {} (malformed config block)."""
    assert _as_config_dict(["a", "b"], "permissions") == {}


def test_as_config_dict_int_returns_empty() -> None:
    """Tier 2: integer → {}."""
    assert _as_config_dict(42, "models") == {}


# ---------------------------------------------------------------------------
# _merge — basic invariants
# ---------------------------------------------------------------------------


def test_merge_unknown_key_overrides() -> None:
    """Tier 2: plain keys in override replace base value."""
    result = _merge({"model": "lite"}, {"model": "standard"})
    assert result["model"] == "standard"


def test_merge_none_value_skips() -> None:
    """Tier 2: None value in override does NOT overwrite existing base value."""
    result = _merge({"model": "lite"}, {"model": None})
    assert result["model"] == "lite"


def test_merge_new_key_added() -> None:
    """Tier 2: key present only in override is added to result."""
    result = _merge({"model": "lite"}, {"debug": True})
    assert result["debug"] is True
    assert result["model"] == "lite"


def test_merge_models_shallow_merged() -> None:
    """Tier 2: 'models' dict is shallow-merged, not replaced."""
    base = {"models": {"lite": "openai/gpt-4o-mini"}}
    override = {"models": {"standard": "openai/gpt-4o"}}
    result = _merge(base, override)
    assert "lite" in result["models"]
    assert "standard" in result["models"]


def test_merge_permissions_shallow_merged() -> None:
    """Tier 2: 'permissions' dict is shallow-merged."""
    base = {"permissions": {"allow": ["file_read"]}}
    override = {"permissions": {"deny": ["file_write"]}}
    result = _merge(base, override)
    assert result["permissions"]["allow"] == ["file_read"]
    assert result["permissions"]["deny"] == ["file_write"]


def test_merge_base_unchanged() -> None:
    """Tier 2: _merge returns a new dict; base is not mutated."""
    base = {"model": "lite"}
    _merge(base, {"model": "standard"})
    assert base["model"] == "lite"


# ---------------------------------------------------------------------------
# _find_project_root
# ---------------------------------------------------------------------------


def test_find_project_root_finds_reyn_yaml(tmp_path: Path) -> None:
    """Tier 2: walking up finds the nearest reyn.yaml directory."""
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert _find_project_root(sub) == tmp_path


def test_find_project_root_exact_match(tmp_path: Path) -> None:
    """Tier 2: start dir containing reyn.yaml is returned immediately."""
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    assert _find_project_root(tmp_path) == tmp_path


def test_find_project_root_no_reyn_yaml_returns_none(tmp_path: Path) -> None:
    """Tier 2: no reyn.yaml in tree → None."""
    sub = tmp_path / "nested"
    sub.mkdir()
    assert _find_project_root(sub) is None


def test_find_project_root_second_call_does_not_walk_again(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: #3681 — a second call for the SAME resolved start path does not
    re-walk the filesystem (single-owner cache, #3671 P4 item A-3: `reyn
    chat` alone called this 3x per invocation for the same cwd before this).
    Witnessed through the PUBLIC filesystem side effect (`Path.exists` call
    count), not by reading the private `lru_cache` state directly."""
    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    calls = {"n": 0}
    real_exists = Path.exists

    def _counting_exists(self: Path) -> bool:
        calls["n"] += 1
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _counting_exists)

    first = _find_project_root(sub)
    after_first = calls["n"]
    assert after_first > 0, "the first call must have touched the filesystem"

    second = _find_project_root(sub)

    assert second == first == tmp_path
    assert calls["n"] == after_first, (
        "the second call for the same start path re-walked the filesystem "
        f"({calls['n'] - after_first} more Path.exists() calls) instead of "
        "using the cached result"
    )


def test_find_project_root_cache_clear_makes_a_later_reyn_yaml_visible(
    tmp_path: Path,
) -> None:
    """Tier 2: #3681 FALSIFY — the cache is not permanently stuck on a stale
    miss. A test (or any caller) that creates `reyn.yaml` AFTER an earlier
    query for the same path can still observe it, by explicitly clearing
    `_find_project_root_uncached`'s cache — the same seam the `tests/
    conftest.py` autouse fixture calls after every test."""
    from reyn.config.loader import _find_project_root_uncached

    sub = tmp_path / "nested"
    sub.mkdir()

    assert _find_project_root(sub) is None

    (tmp_path / "reyn.yaml").write_text("", encoding="utf-8")
    _find_project_root_uncached.cache_clear()

    assert _find_project_root(sub) == tmp_path
