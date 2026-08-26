"""Tier 1/2: `chat.stream_repaint_min_interval` config parsing.

The TTY repaint budget (#3570) was a module constant with no way to reach
it. Its default is a knee measured on ONE terminal; a slower one (SSH, a
multiplexer, a corporate laptop) has a different knee and its operator had
no way to find it without editing source. This knob does not move the
shipped default — it only stops the value being unreachable.

Test shape mirrors `test_4840_chat_theme_config.py`, including the
`load_config` round-trip and ITS falsification pair: exercising
`_build_chat_config` alone proves the parser reads the key, not that the
real entrypoint surfaces it (the gap #4899 found on a sibling field).
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


def test_stream_repaint_interval_defaults_to_the_measured_knee() -> None:
    """Tier 1: the shipped default is unchanged by this knob existing."""
    assert ChatConfig().stream_repaint_min_interval == 1 / 30


def test_stream_repaint_interval_parses_a_slower_value() -> None:
    """Tier 1: an operator on a slow terminal can widen the budget."""
    assert _build_chat_config(
        {"stream_repaint_min_interval": 0.2},
    ).stream_repaint_min_interval == 0.2


def test_stream_repaint_interval_absent_keeps_the_default() -> None:
    """Tier 1: falsification pair — omitting the key does not pick a value
    up from any sibling `chat:` key."""
    assert _build_chat_config(
        {"render_mode": "plain"},
    ).stream_repaint_min_interval == 1 / 30


def test_stream_repaint_interval_parses_with_and_without_a_compaction_block() -> None:
    """Tier 1: `_build_chat_config` returns early when `compaction:` is
    absent or malformed, so a field must parse on BOTH branches — the shape
    #4677 and #4840 each pinned for their own field."""
    with_block = _build_chat_config(
        {"stream_repaint_min_interval": 0.1, "compaction": {"body_token_cap": 5000}},
    )
    without_block = _build_chat_config({"stream_repaint_min_interval": 0.1})
    assert with_block.stream_repaint_min_interval == 0.1
    assert without_block.stream_repaint_min_interval == 0.1


def test_non_positive_interval_falls_back_rather_than_disabling_the_budget() -> None:
    """Tier 1: 0 (or a negative) means "repaint every delta" — the
    pre-#3570 behaviour whose measured cost (wall-clock 16.1 s vs 3.3 s on
    a 2000-delta reply) is the reason the budget exists. An operator does
    not reach that by typing a number this parser could have rejected."""
    assert _build_chat_config(
        {"stream_repaint_min_interval": 0},
    ).stream_repaint_min_interval == 1 / 30
    assert _build_chat_config(
        {"stream_repaint_min_interval": -1},
    ).stream_repaint_min_interval == 1 / 30


def test_unparseable_interval_falls_back_to_the_default() -> None:
    """Tier 1: a typo leaves the shipped default in place instead of
    raising into session startup."""
    assert _build_chat_config(
        {"stream_repaint_min_interval": "fast"},
    ).stream_repaint_min_interval == 1 / 30


def test_stream_repaint_interval_survives_a_real_load_config_round_trip(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: `load_config()` itself — a real reyn.yaml on disk — surfaces
    the value, not just `_build_chat_config` in isolation."""
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path / "reyn.yaml", {"chat": {"stream_repaint_min_interval": 0.25}})

    cfg = load_config(tmp_path)

    assert cfg.chat.stream_repaint_min_interval == 0.25


def test_absent_from_yaml_keeps_the_default_through_load_config(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify for the round-trip above — without the key,
    `load_config()` must still report the default, proving the previous
    test read a value that actually came off disk."""
    monkeypatch.chdir(tmp_path)
    _write_yaml(tmp_path / "reyn.yaml", {"chat": {"render_mode": "plain"}})

    cfg = load_config(tmp_path)

    assert cfg.chat.stream_repaint_min_interval == 1 / 30
