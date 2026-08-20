"""Tier 1/2: `chat.theme` config parsing (#4840 ③ — the config-knob half;
the colour-direction half was owner-decided separately and already shipped,
#4869/#4875).

Default None — `TextualChatApp.on_mount` keeps shipping reyn's own
full-colour theme (`REYN_THEME`, registered name `"reyn"`) unless an
operator names one explicitly. Deliberately unrestricted at this layer
(no allowlist here — see `ChatConfig.theme`'s own docstring for why): a
config-layer allowlist would need to be kept in sync with Textual's own
theme registry forever, for no safety benefit (an unknown name raises at
`self.theme = ...`, not here).

`test_chat_theme_survives_a_real_load_config_round_trip` closes the
`load_config`-reload gap lead-coder's #4899 review found (a config field
was `set` but the loader never actually read it back on reload) — the
other tests in this file exercise `_build_chat_config` directly (matching
`test_4677_empty_stop_retry_config.py`'s own established pattern for a
`ChatConfig` field), which proves the PARSER reads the field; this one
additionally proves `load_config()` itself — the real entrypoint,
reading a real `reyn.yaml` off disk — surfaces the value all the way
through, not just the parser in isolation.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from reyn.config.chat import ChatConfig, _build_chat_config
from reyn.config.loader import load_config


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_chat_theme_defaults_to_none() -> None:
    """Tier 1: ChatConfig.theme defaults to None — the shipped default
    (reyn's own full-colour theme, "reyn") is unchanged unless overridden."""
    assert ChatConfig().theme is None


def test_chat_theme_parses_a_name() -> None:
    """Tier 1: `chat.theme: nord` in reyn.yaml carries through — the
    required "config で名前を書ける" condition (#4840 ③)."""
    assert _build_chat_config({"theme": "nord"}).theme == "nord"


def test_chat_theme_absent_stays_none() -> None:
    """Tier 1: falsification pair — omitting the key keeps the None
    default (this field does not accidentally pick up a value from any
    other chat: key)."""
    assert _build_chat_config({"render_mode": "plain"}).theme is None


def test_chat_theme_parses_when_compaction_block_present() -> None:
    """Tier 1: the field parses correctly whether or not a sibling
    `chat.compaction:` block is present — `_build_chat_config` has an
    early-return branch when `compaction` is absent/malformed, and this
    field's parse must survive BOTH branches, not just the one every
    other test in this file happens to exercise (mirrors #4677's own
    sibling test for exactly this shape)."""
    assert _build_chat_config({
        "theme": "dracula",
        "compaction": {"body_token_cap": 5000},
    }).theme == "dracula"
    assert _build_chat_config({"theme": "dracula"}).theme == "dracula"


def test_chat_theme_is_not_restricted_to_a_known_list() -> None:
    """Tier 1: any string parses through unchanged — this config layer
    does not validate against Textual's theme registry (see the field's
    own docstring for why: an unknown name is Textual's own concern, at
    the point `self.theme = ...` actually runs, not this parser's)."""
    assert _build_chat_config({"theme": "not-a-real-theme-name"}).theme == (
        "not-a-real-theme-name"
    )


def test_chat_theme_survives_a_real_load_config_round_trip(tmp_path, monkeypatch) -> None:
    """Tier 2: load_config() itself — not just _build_chat_config in
    isolation — reads chat.theme off a real reyn.yaml on disk. Closes the
    exact gap #4899 found (a field set in YAML but never actually read
    back through the real loader)."""
    monkeypatch.chdir(tmp_path)
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"chat": {"theme": "gruvbox"}})

    cfg = load_config(tmp_path)

    assert cfg.chat.theme == "gruvbox"


def test_chat_theme_absent_from_yaml_stays_none_through_load_config(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify for the round-trip test above — a reyn.yaml
    with no chat.theme key must leave load_config()'s own result at the
    None default, proving the prior test's assertion is reading a value
    that actually came from the YAML, not a value load_config() would
    produce regardless of what's on disk."""
    monkeypatch.chdir(tmp_path)
    reyn_yaml = tmp_path / "reyn.yaml"
    _write_yaml(reyn_yaml, {"chat": {"render_mode": "plain"}})

    cfg = load_config(tmp_path)

    assert cfg.chat.theme is None
