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
    AGENT_ROLE_SETTING_ID,
    LANGUAGE_ITEMS,
    LANGUAGE_SETTING_ID,
    MODEL_SETTING_ID,
    NEW_AGENT_NAME_SETTING_ID,
    language_label_for,
    language_to_value,
    list_model_names,
    normalise_new_agent_name,
    normalise_role,
    value_to_language,
    value_to_model,
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


# ── model select ──────────────────────────────────────────────────────────


class _FakeResolver:
    """Minimal stand-in for ``ModelResolver`` — only ``_resolved`` matters
    to ``list_model_names``. Holds an arbitrary value dict; the helper
    never inspects the values, only the keys."""

    def __init__(self, names: list[str]) -> None:
        self._resolved = {n: object() for n in names}


def test_model_setting_id_is_stable():
    """Tier 1: dispatcher key matches what the widget emits + the
    settings_update handler reads."""
    assert MODEL_SETTING_ID == "model"


def test_list_model_names_returns_sorted_keys():
    """Tier 1: namespace keys sorted for stable popup ordering across
    reloads — input order is irrelevant."""
    resolver = _FakeResolver(["zebra", "alpha", "claude-sonnet", "standard"])
    assert list_model_names(resolver) == [
        "alpha", "claude-sonnet", "standard", "zebra",
    ]


def test_list_model_names_empty_when_resolver_lacks_resolved():
    """Tier 1: stripped resolver / None → ``[]`` so the chainlit-side
    fallback hides the Select instead of crashing."""
    class _NoResolved:
        pass
    assert list_model_names(_NoResolved()) == []
    assert list_model_names(None) == []


def test_list_model_names_handles_resolver_with_non_dict_resolved():
    """Tier 1: defensive — if ``_resolved`` somehow isn't a dict,
    treat as empty rather than blow up."""
    class _Broken:
        _resolved = "not a dict"
    assert list_model_names(_Broken()) == []


def test_value_to_model_known_passes_through():
    """Tier 1: any non-empty value reaches reyn verbatim (= reyn's
    resolver re-validates the name)."""
    assert value_to_model("claude-sonnet", default="standard") == "claude-sonnet"


def test_value_to_model_empty_falls_back_to_default():
    """Tier 1: empty / whitespace / None → default (= preserves the
    operator's current model rather than silently switching)."""
    assert value_to_model("", default="standard") == "standard"
    assert value_to_model("   ", default="standard") == "standard"
    assert value_to_model(None, default="standard") == "standard"


# ── agent role normaliser ─────────────────────────────────────────────────


def test_agent_role_setting_id_is_stable():
    """Tier 1: the widget id + on_settings_update dispatch key agree."""
    assert AGENT_ROLE_SETTING_ID == "agent_role"


def test_normalise_role_trims_whitespace():
    """Tier 1: leading / trailing whitespace stripped before save —
    matches the ``/agent edit role`` slash path's ``.strip()``."""
    assert normalise_role("  you are a pirate  ") == "you are a pirate"


def test_normalise_role_empty_returns_none():
    """Tier 1: blank input → None (= caller skips persistence, preserves
    current role) instead of writing a stale blank over the agent's
    persona."""
    assert normalise_role("") is None
    assert normalise_role("   ") is None


def test_normalise_role_none_input_returns_none():
    """Tier 1: defensive — None or non-string → None."""
    assert normalise_role(None) is None
    assert normalise_role(12345) is None  # type: ignore[arg-type]


def test_normalise_role_pass_through_multiline():
    """Tier 1: multi-line role text preserved verbatim (= just trim
    outer whitespace, not internal newlines / formatting)."""
    multiline = "line one\nline two\n  indented line"
    assert normalise_role("\n" + multiline + "\n") == multiline


# ── new agent name normaliser ─────────────────────────────────────────────


def test_new_agent_name_setting_id_is_stable():
    """Tier 1: the widget id + on_settings_update dispatch key agree."""
    assert NEW_AGENT_NAME_SETTING_ID == "new_agent_name"


def test_normalise_new_agent_name_trims_whitespace():
    """Tier 1: leading / trailing whitespace stripped before the
    registry's name regex enforcement."""
    assert normalise_new_agent_name("  research  ") == "research"


def test_normalise_new_agent_name_blank_returns_none():
    """Tier 1: empty / whitespace-only → None (= operator left the
    TextInput unchanged; skip the create round-trip)."""
    assert normalise_new_agent_name("") is None
    assert normalise_new_agent_name("   ") is None


def test_normalise_new_agent_name_non_string_returns_none():
    """Tier 1: defensive — None / non-string → None."""
    assert normalise_new_agent_name(None) is None
    assert normalise_new_agent_name(42) is None  # type: ignore[arg-type]


def test_normalise_new_agent_name_pass_through_valid_format():
    """Tier 1: valid name shapes pass through verbatim — the registry's
    ``_validate_agent_name`` is the authoritative regex check, so this
    helper deliberately doesn't pre-filter (= invalid names hit
    ``ValueError`` at create time + the chainlit handler surfaces the
    reyn error verbatim)."""
    assert normalise_new_agent_name("agent_with_underscore") == "agent_with_underscore"
    assert normalise_new_agent_name("agent-with-dash") == "agent-with-dash"
    assert normalise_new_agent_name("a") == "a"
    # Invalid names also pass through this helper (= registry rejects):
    assert normalise_new_agent_name("UPPER") == "UPPER"
    assert normalise_new_agent_name("-leading-dash") == "-leading-dash"
