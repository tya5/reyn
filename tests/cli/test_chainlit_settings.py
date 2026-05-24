"""Tier 1: ``reyn.chainlit_app.settings`` round-trip contracts.

The chat-settings panel surfaces ``output_language`` as a Select
widget. Two round-trips need to stay consistent:

1. ``ChatSession.output_language`` (str | None) → widget select value
   (always non-empty string, ``"auto"`` for None) so the dropdown is
   never blank.
2. widget select value → ``ChatSession.output_language`` (str | None)
   so ``"auto"`` round-trips back to ``None`` and reaches reyn's
   "let the LLM decide" branch.

Plus: every value in ``LANGUAGE_ITEMS`` round-trips through
``value_to_language(language_to_value(...))`` (= catalog
self-consistency).
"""
from __future__ import annotations

import pytest

from reyn.chainlit_app.settings import (
    LANGUAGE_ITEMS,
    LANGUAGE_SETTING_ID,
    language_label_for,
    language_to_value,
    value_to_language,
)


def test_setting_id_is_stable():
    """Tier 1: the dict key the widget emits + ``on_settings_update``
    expects must agree. Pin the literal so a rename surfaces here."""
    assert LANGUAGE_SETTING_ID == "output_language"


def test_language_items_non_empty():
    """Tier 1: at least Auto + 1 language so the dropdown isn't useless."""
    assert len(LANGUAGE_ITEMS) >= 2
    assert "auto" in LANGUAGE_ITEMS.values()


def test_language_to_value_none_maps_to_auto():
    """Tier 1: reyn's `None` (= LLM decides) → widget shows "Auto"."""
    assert language_to_value(None) == "auto"


def test_language_to_value_empty_string_maps_to_auto():
    """Tier 1: empty / whitespace-only → "auto" (= same as None)."""
    assert language_to_value("") == "auto"
    assert language_to_value("   ") == "auto"


@pytest.mark.parametrize("lang", ["ja", "en", "zh", "ko"])
def test_language_to_value_known_codes_pass_through(lang: str):
    """Tier 1: known BCP-47 codes hit the widget as-is."""
    assert language_to_value(lang) == lang


def test_language_to_value_unknown_code_preserved():
    """Tier 1: yaml-set custom code (= not in items) still round-trips
    so the operator's setting isn't silently flattened to Auto."""
    assert language_to_value("de") == "de"


def test_value_to_language_auto_maps_to_none():
    """Tier 1: widget "auto" → reyn None (= "LLM decides")."""
    assert value_to_language("auto") is None


def test_value_to_language_empty_and_none_map_to_none():
    """Tier 1: defensive — empty / None input → None."""
    assert value_to_language(None) is None
    assert value_to_language("") is None
    assert value_to_language("   ") is None


@pytest.mark.parametrize("v", ["ja", "en", "zh", "ko", "de"])
def test_value_to_language_known_and_unknown_pass_through(v: str):
    """Tier 1: any non-"auto" value reaches reyn verbatim — no dispatch
    table to maintain when adding a new widget item."""
    assert value_to_language(v) == v


def test_round_trip_for_every_catalog_value():
    """Tier 1: ``value_to_language(language_to_value(v))`` is the
    identity for every curated value (= catalog self-consistency)."""
    for v in LANGUAGE_ITEMS.values():
        # auto round-trips as: "auto" → None → "auto"
        if v == "auto":
            assert value_to_language(v) is None
            assert language_to_value(None) == "auto"
        else:
            assert value_to_language(v) == v
            assert language_to_value(v) == v


def test_language_label_for_known_value():
    """Tier 1: confirmation message uses the human label, not the code."""
    assert language_label_for("ja") == "日本語"
    assert language_label_for("en") == "English"
    assert language_label_for("auto") == "Auto (LLM decides)"


def test_language_label_for_unknown_falls_back_to_value():
    """Tier 1: yaml-set custom code without a curated label still gets
    a readable acknowledgement (= the raw code itself)."""
    assert language_label_for("de") == "de"
