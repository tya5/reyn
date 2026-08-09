"""Tier 1: `chat.image_url_schemes` config parsing (#3846, owner ruling C).

Default unrestricted ([]) — the owner's stated default for image-src fetch
scope. See `core/present/image_fetch.py` for where this list is consumed.
"""
from __future__ import annotations

from reyn.config.chat import ChatConfig, _build_chat_config


def test_image_url_schemes_defaults_unrestricted() -> None:
    """Tier 1: ChatConfig.image_url_schemes defaults to [] (owner ruling C)."""
    assert ChatConfig().image_url_schemes == []


def test_chat_image_url_schemes_parses_a_list() -> None:
    """Tier 1: `chat.image_url_schemes: [https]` in reyn.yaml parses through."""
    assert _build_chat_config({"image_url_schemes": ["https"]}).image_url_schemes == [
        "https"
    ]


def test_chat_image_url_schemes_absent_stays_unrestricted() -> None:
    """Tier 1: falsification pair — omitting the key keeps the [] default
    (this field does not accidentally narrow for any other chat: block)."""
    assert _build_chat_config({"render_mode": "plain"}).image_url_schemes == []


def test_chat_image_url_schemes_non_list_value_is_ignored() -> None:
    """Tier 1: a malformed value (not a list) falls back to unrestricted
    rather than crashing config load — same defensive-parse contract every
    other chat: field in this loader follows."""
    assert _build_chat_config({"image_url_schemes": "https"}).image_url_schemes == []
