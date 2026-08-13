"""Tier 2: ``action_retrieval:`` config block — now EMPTY (#4552 arc).

This block's fields, in order of removal:
  - `hot_list_n` / `hot_list_seed` — removed, owner directive (PR-1, #4560).
  - `mode` — removed, 0 real consumers (PR-2, #4563).
  - `universal_wrappers_enabled` — MOVED to
    `tool_use.universal_wrappers_enabled` (PR-3, this arc's last field —
    tests for it now live in `tests/config/test_tool_use_config.py`,
    same coverage carried over).

`ActionRetrievalConfig` is now a bare, fieldless dataclass. What remains
here: the class default-constructs to an empty instance, `ReynConfig`
carries one, and `_build_action_retrieval_config` treats every key in a
`reyn.yaml` `action_retrieval:` block as unknown (forward-compat: ignored,
not an error) — including the keys this section used to recognize, per
the standard unknown-key tolerance every retired config key gets (no
migration path — there is nothing left under `action_retrieval:` to
migrate TO). PR-4 deletes this class and the section entirely, closing
the #4552 arc.

No mocks; uses real load_config with a yaml file written to tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    ActionRetrievalConfig,
    ReynConfig,
    _build_action_retrieval_config,
    load_config,
)
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

# ── 1. Empty by construction ──────────────────────────────────────────────


def test_default_construction_has_no_fields() -> None:
    """Tier 2: ActionRetrievalConfig is a bare dataclass — every field it
    ever had has been removed or relocated (#4552 PR-1/2/3)."""
    import dataclasses
    cfg = ActionRetrievalConfig()
    assert dataclasses.fields(cfg) == ()


def test_reyn_config_carries_an_action_retrieval_instance() -> None:
    """Tier 2: ReynConfig still default-constructs an ActionRetrievalConfig
    — the SECTION exists until PR-4, even though it carries no fields."""
    cfg = ReynConfig()
    assert isinstance(cfg.action_retrieval, ActionRetrievalConfig)


# ── 2. Parser — every key is now unknown (forward-compat tolerance) ──────


def test_parser_none_returns_default() -> None:
    """Tier 2: omitted block → the empty default."""
    cfg = _build_action_retrieval_config(None)
    assert cfg == ActionRetrievalConfig()


def test_parser_empty_dict_returns_default() -> None:
    """Tier 2: empty dict → defaults (no-op)."""
    cfg = _build_action_retrieval_config({})
    assert cfg == ActionRetrievalConfig()


def test_parser_ignores_every_key_including_former_fields() -> None:
    """Tier 2: unknown keys are silently ignored (forward compat) — now
    including EVERY key this block used to recognize, since none remain.
    Regression guard for the PR-3 move specifically: a stray
    `universal_wrappers_enabled` left under `action_retrieval:` (an
    operator who hasn't moved it to `tool_use:` yet) must not raise or
    silently resurrect the old field."""
    cfg = _build_action_retrieval_config({
        "universal_wrappers_enabled": True,  # stale location, now unknown
        "mode": "performance",  # retired PR-2
        "hot_list_n": 10,  # retired PR-1
        "embedding_class": "standard",  # retired earlier (FP-0066 §7 P1a)
        "phase3_cold_start_seed": ["x", "y"],  # never-implemented future field
    })
    assert cfg == ActionRetrievalConfig()


def test_parser_rejects_non_dict() -> None:
    """Tier 2: non-mapping at the top level still raises ValueError — the
    top-level type check survives the field removal."""
    with pytest.raises(ValueError, match="must be a mapping"):
        _build_action_retrieval_config("not a dict")


# ── 3. End-to-end load_config integration ─────────────────────────────────


def test_load_config_with_stray_universal_wrappers_enabled_ignores_it(
    tmp_path: Path,
) -> None:
    """Tier 2: an operator who hasn't moved `universal_wrappers_enabled`
    from `action_retrieval:` to `tool_use:` yet (#4552 PR-3) gets the
    standard unknown-key tolerance — no parse error, no silent effect."""
    (tmp_path / "reyn.yaml").write_text(
        """
action_retrieval:
  universal_wrappers_enabled: true
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval == ActionRetrievalConfig()
    # The stray key has no effect on tool_use's OWN default either — it
    # is a genuinely different, unread key at this point, not an alias.
    assert cfg.tool_use.universal_wrappers_enabled is True  # unaffected default


def test_load_config_without_action_retrieval_uses_defaults(tmp_path: Path) -> None:
    """Tier 2: omitting action_retrieval: keeps the empty default."""
    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML,
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.action_retrieval == ActionRetrievalConfig()
