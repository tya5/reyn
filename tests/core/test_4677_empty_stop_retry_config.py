"""Tier 1: `chat.empty_stop_retry` config parsing (#4677, owner instruction
2026-08-14: "resume 注入をデフォルト off にしてくれないかな").

Default False — the B42-NF-W6-1 empty-response detect-and-retry (one
resend on an empty `finish_reason="stop"`) was previously hardcoded ON in
production with no config knob at all (`router_loop_driver.py`'s own
`empty_stop_retry_auto=True`). See `ChatConfig.empty_stop_retry`'s own
docstring for the incident + the measured tradeoff being made. The knob
must remain settable back to True — an operator/environment relying on
the retry's measured narration-recovery benefit needs a way to keep it.
"""
from __future__ import annotations

from reyn.config.chat import ChatConfig, _build_chat_config


def test_empty_stop_retry_defaults_to_false() -> None:
    """Tier 1: ChatConfig.empty_stop_retry defaults to False (owner
    instruction, 2026-08-14 — was previously hardcoded True with no
    config field at all)."""
    assert ChatConfig().empty_stop_retry is False


def test_chat_empty_stop_retry_parses_true() -> None:
    """Tier 1: `chat.empty_stop_retry: true` in reyn.yaml turns it back
    on — the required "config で on にできる形" condition (#4677)."""
    assert _build_chat_config({"empty_stop_retry": True}).empty_stop_retry is True


def test_chat_empty_stop_retry_absent_stays_false() -> None:
    """Tier 1: falsification pair — omitting the key keeps the False
    default (this field does not accidentally flip on for any other
    chat: block)."""
    assert _build_chat_config({"render_mode": "plain"}).empty_stop_retry is False


def test_chat_empty_stop_retry_parses_when_compaction_block_present() -> None:
    """Tier 1: the field parses correctly whether or not a sibling
    `chat.compaction:` block is present — `_build_chat_config` has an
    early-return branch when `compaction` is absent/malformed, and this
    field's parse must survive BOTH branches, not just the one every
    other test in this file happens to exercise."""
    assert _build_chat_config({
        "empty_stop_retry": True,
        "compaction": {"body_token_cap": 5000},
    }).empty_stop_retry is True
    assert _build_chat_config({"empty_stop_retry": True}).empty_stop_retry is True


def test_chat_empty_stop_retry_non_bool_value_is_coerced_not_crashed() -> None:
    """Tier 1: a malformed value does not crash config load — same
    defensive-parse contract every other chat: field in this loader
    follows (bool() coercion, mirroring neutralize_body's own parse)."""
    assert _build_chat_config({"empty_stop_retry": "yes"}).empty_stop_retry is True
    assert _build_chat_config({"empty_stop_retry": ""}).empty_stop_retry is False
