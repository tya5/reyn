"""Tier 2: ``tool_use.universal_wrappers_enabled`` field/parser contract.

#4552 PR-3: this field is RELOCATED here from
``action_retrieval.universal_wrappers_enabled`` (architect's ruling — it
is a ``tool_use``/presentation-scheme property, not a retrieval setting;
only ``universal-category``'s own wrapper functions ever read it, per
``RouterHostAdapter.get_universal_wrappers_enabled()``'s real read path).
These tests were carried over from ``tests/config/test_action_retrieval_
config.py`` (which now tests an empty ``ActionRetrievalConfig``) —
same coverage, new location, same field.

``ToolUseConfig``'s other two fields (``scheme`` / ``transport``) have
their own parser tests exercised via the (scheme, transport)-pair
validation elsewhere (``reyn.tools.transport.resolve_scheme_for_transport``)
— not duplicated here.

Deliberately validated SOFT (never raises) unlike ``ToolUseConfig``'s
OTHER two fields (an invalid `chat` key or an unregistered
(scheme, transport) pair both raise): an explicit, standing owner ruling
governs config validation uniformly ("warn, never hard-fail, anywhere —
including sandbox.policy, no special case", ``loader.py``'s
``_warn_unknown_config_keys`` docstring) and overrides this class's own
local fail-loud convention for this ONE field. Type-validation (non-bool
raises) is a DIFFERENT axis from the scheme-mismatch soft-warning
(#4231(C), tested separately in
``tests/config/test_4231_c_disabled_by_dependency_config_keys.py``) — a
malformed VALUE still raises here, same as every other typed field;
only the "this VALUE is a no-op under the current scheme" case is soft.

No mocks; uses real load_config with a yaml file written to tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import ReynConfig, load_config
from reyn.config.execution import ToolUseConfig, _build_tool_use_config
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

# ── 1. Default values ─────────────────────────────────────────────────────


def test_default_universal_wrappers_enabled_is_on() -> None:
    """Tier 2: out-of-the-box config has universal wrappers ENABLED
    (wrapper-only path — PR-3b-iv flipped the default to True)."""
    cfg = ToolUseConfig()
    assert cfg.universal_wrappers_enabled is True


def test_reyn_config_carries_tool_use_default() -> None:
    """Tier 2: ReynConfig default-constructs with a ToolUseConfig whose
    universal_wrappers_enabled default is True."""
    cfg = ReynConfig()
    assert isinstance(cfg.tool_use, ToolUseConfig)
    assert cfg.tool_use.universal_wrappers_enabled is True


# ── 2. Parser — happy path ────────────────────────────────────────────────


def test_parser_universal_wrappers_enabled_true() -> None:
    """Tier 2: setting universal_wrappers_enabled True flows through
    alongside the field's scheme/transport siblings."""
    cfg = _build_tool_use_config({"universal_wrappers_enabled": True})
    assert cfg.universal_wrappers_enabled is True


def test_parser_universal_wrappers_enabled_false() -> None:
    """Tier 2: setting universal_wrappers_enabled False flows through."""
    cfg = _build_tool_use_config({"universal_wrappers_enabled": False})
    assert cfg.universal_wrappers_enabled is False


def test_parser_omitted_defaults_to_true() -> None:
    """Tier 2: omitting the key keeps the True default."""
    cfg = _build_tool_use_config({"scheme": "retrieval"})
    assert cfg.universal_wrappers_enabled is True


def test_parser_all_fields_at_once() -> None:
    """Tier 2: all three tool_use fields can be set together."""
    cfg = _build_tool_use_config({
        "scheme": "category",
        "transport": "tool_calls",
        "universal_wrappers_enabled": False,
    })
    assert cfg.scheme == "category"
    assert cfg.transport == "tool_calls"
    assert cfg.universal_wrappers_enabled is False


# ── 3. Parser — validation errors ─────────────────────────────────────────


def test_parser_rejects_non_bool_wrappers_enabled() -> None:
    """Tier 2: universal_wrappers_enabled with a non-bool value raises —
    a malformed VALUE, not a scheme-mismatch soft-warning (that's #4231(C),
    tested separately)."""
    with pytest.raises(ValueError, match="universal_wrappers_enabled"):
        _build_tool_use_config({"universal_wrappers_enabled": "yes"})


# ── 4. End-to-end load_config integration ─────────────────────────────────


def test_load_config_picks_up_tool_use_universal_wrappers_enabled(
    tmp_path: Path,
) -> None:
    """Tier 2: load_config reads tool_use.universal_wrappers_enabled from
    reyn.yaml, alongside scheme."""
    (tmp_path / "reyn.yaml").write_text(
        """
tool_use:
  scheme: category
  universal_wrappers_enabled: true
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.tool_use.universal_wrappers_enabled is True
    assert cfg.tool_use.scheme == "category"


def test_load_config_without_tool_use_uses_defaults(tmp_path: Path) -> None:
    """Tier 2: omitting tool_use: keeps the default (wrappers on, per
    PR-3b-iv). Opt-out via ``universal_wrappers_enabled: false`` in
    reyn.yaml."""
    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML,
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.tool_use.universal_wrappers_enabled is True


def test_load_config_with_explicit_opt_out(tmp_path: Path) -> None:
    """Tier 2: explicit `universal_wrappers_enabled: false` opt-out flows
    through end-to-end. Operators who don't want the wrappers can disable
    them via reyn.yaml — this path must keep working after the #4552 PR-3
    relocation."""
    (tmp_path / "reyn.yaml").write_text(
        """
tool_use:
  universal_wrappers_enabled: false
""",
        encoding="utf-8",
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.tool_use.universal_wrappers_enabled is False
