"""Tier 1: #4655's new Kind① free-form-leaf validators — each real, valid
sub-key is NOT flagged, each invalid/typo'd sub-key IS flagged. Same
pattern as ``tests/config/test_external_transports_schema_4515.py`` and
``tests/interfaces/test_4631_config_validate_mcp_placement.py``: calls
``config_schema.unknown_config_keys`` directly (the SAME function
``reyn config validate`` / ``load_config`` / hot-reload validation all
call — #4174 T0's one implementation), rather than calling a validator
function in isolation, so these tests prove the real end-to-end
registration wiring, not just the standalone predicate.
"""
from __future__ import annotations

from reyn.config import config_schema, loader  # noqa: F401

# `loader`'s import (above) runs every #4655 registration in
# reyn.config.infra at module-import time (see
# test_4655_freeform_leaf_registration_completeness.py's docstring for why
# this import is load-bearing, not decorative).


# ── external_transports ─────────────────────────────────────────────────


def test_external_transports_valid_entry_is_not_flagged() -> None:
    """Tier 1: a well-formed ``external_transports`` entry (real
    ``mcp_tool``/``args_template`` keys) is not flagged."""
    result = config_schema.unknown_config_keys({
        "external_transports": {
            "broker": {"mcp_tool": "broker__post_message", "args_template": {}},
        },
    })
    assert result == {}


def test_external_transports_transport_name_itself_is_never_flagged() -> None:
    """Tier 1: a genuinely arbitrary transport NAME (the operator's own
    choice) is not itself checked against any vocabulary — only each
    entry's inner keys are."""
    result = config_schema.unknown_config_keys({
        "external_transports": {
            "any_operator_chosen_name": {"mcp_tool": "x__y", "args_template": {}},
        },
    })
    assert result == {}


def test_external_transports_invalid_inner_key_is_flagged() -> None:
    """Tier 1: an unrecognized per-entry key under ``external_transports``
    is flagged, prefixed two levels deep relative to the leaf itself."""
    result = config_schema.unknown_config_keys({
        "external_transports": {
            "broker": {"mcp_tool": "broker__post_message", "unexpected_field": 1},
        },
    })
    assert "external_transports.broker.unexpected_field" in result


# ── mcp (top-level) ─────────────────────────────────────────────────────


def test_mcp_servers_and_registries_are_not_flagged() -> None:
    """Tier 1: ``mcp.servers`` and ``mcp.registries`` — the only two real
    top-level sub-keys any consumer reads — are not flagged."""
    result = config_schema.unknown_config_keys({
        "mcp": {"servers": {"github": {"command": "x"}}, "registries": ["https://x"]},
    })
    assert result == {}


def test_mcp_unknown_direct_sub_key_is_flagged() -> None:
    """Tier 1: a direct ``mcp:`` sub-key other than ``servers``/
    ``registries`` is flagged."""
    result = config_schema.unknown_config_keys({"mcp": {"typo_field": 1}})
    assert "mcp.typo_field" in result


# ── chat.compaction.component_weights ───────────────────────────────────


def test_component_weights_all_five_real_keys_are_not_flagged() -> None:
    """Tier 1: all five real ``component_weights`` keys (head/body/tail/
    new_msg/compaction_batch) are not flagged."""
    result = config_schema.unknown_config_keys({
        "chat": {"compaction": {"component_weights": {
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        }}},
    })
    assert result == {}


def test_component_weights_invalid_key_is_flagged() -> None:
    """Tier 1: a typo'd ``component_weights`` key is flagged."""
    result = config_schema.unknown_config_keys({
        "chat": {"compaction": {"component_weights": {"head": 10, "typo_weight": 1}}},
    })
    assert "chat.compaction.component_weights.typo_weight" in result


# ── llm.model_class_by_purpose ──────────────────────────────────────────


def test_model_class_by_purpose_valid_purpose_is_not_flagged() -> None:
    """Tier 1: a real ``MODEL_CLASS_PURPOSES`` member is not flagged."""
    result = config_schema.unknown_config_keys({
        "llm": {"model_class_by_purpose": {"router": "light", "judge": "strong"}},
    })
    assert result == {}


def test_model_class_by_purpose_typo_purpose_is_flagged() -> None:
    """Tier 1: a typo'd purpose key is flagged as unknown (``None`` hint)."""
    result = config_schema.unknown_config_keys({
        "llm": {"model_class_by_purpose": {"routre": "light"}},
    })
    assert "llm.model_class_by_purpose.routre" in result


def test_model_class_by_purpose_compaction_gets_its_own_removal_note() -> None:
    """Tier 1: #3785 — ``compaction`` is a KNOWN, deliberately-removed key
    (hard-fails at real ``load_config`` time) — not an ordinary typo, so it
    carries an explanatory note rather than a bare unknown-key ``None``."""
    result = config_schema.unknown_config_keys({
        "llm": {"model_class_by_purpose": {"compaction": "light"}},
    })
    hint = result["llm.model_class_by_purpose.compaction"]
    assert hint is not None
    assert "3785" in hint.note


# ── llm.router.retry_policy ─────────────────────────────────────────────


def test_retry_policy_is_registered_open_not_validated() -> None:
    """Tier 1: #4655 review (lead-coder) reverted this leaf from Kind① to
    Kind② — an earlier revision imported ``litellm.types.router.RetryPolicy``
    directly to introspect its field names, which (a) broke the
    litellm-boundary import seam and (b) took over a third party's
    vocabulary reyn doesn't need to duplicate (litellm already fails
    loudly on an unknown key at Router-build time). A bogus field is
    accepted here (unflagged) because litellm's own TypeError is the real
    enforcement — this is the CORRECT disposition, not an oversight."""
    assert config_schema.freeform_leaf_registration_kind("llm.router.retry_policy") == "open"
    result = config_schema.unknown_config_keys({
        "llm": {"router": {"retry_policy": {"NotARealField": 2}}},
    })
    assert result == {}


# ── gateway.surfaces.enabled ─────────────────────────────────────────────


def test_gateway_surfaces_enabled_real_surface_is_not_flagged() -> None:
    """Tier 1: real surface names from ``build_registry()`` are not
    flagged."""
    result = config_schema.unknown_config_keys({
        "gateway": {"surfaces": {"enabled": {"api": True, "webui": False}}},
    })
    assert result == {}


def test_gateway_surfaces_enabled_unknown_surface_is_flagged() -> None:
    """Tier 1: a surface name absent from ``build_registry()`` is
    flagged."""
    result = config_schema.unknown_config_keys({
        "gateway": {"surfaces": {"enabled": {"not_a_real_surface": True}}},
    })
    assert "gateway.surfaces.enabled.not_a_real_surface" in result


# ── pipelines / presentations (top-level) ────────────────────────────────


def test_entries_only_leaves_accept_entries_and_flag_everything_else() -> None:
    """Tier 1: ``pipelines``/``presentations`` each accept only the real
    ``entries`` sub-key and flag any other direct sub-key."""
    for dotted_key in ("pipelines", "presentations"):
        accepted = config_schema.unknown_config_keys({dotted_key: {"entries": []}})
        assert accepted == {}, dotted_key

        rejected = config_schema.unknown_config_keys({dotted_key: {"typo_field": 1}})
        assert f"{dotted_key}.typo_field" in rejected, dotted_key


# ── skills (top-level, wider vocabulary) ─────────────────────────────────


def test_skills_entries_and_internal_bookkeeping_keys_are_not_flagged() -> None:
    """Tier 1: ``_provenance``/``_collisions`` are internal bookkeeping keys
    ``config/loader.py``'s tier-merge rides inside ``skills`` — a
    well-formed, freshly-merged config carries them and must not warn."""
    result = config_schema.unknown_config_keys({
        "skills": {"entries": {}, "_provenance": {}, "_collisions": {}},
    })
    assert result == {}


def test_skills_unknown_key_is_still_flagged() -> None:
    """Tier 1: a genuinely unrecognized ``skills`` sub-key is still
    flagged — the wider vocabulary above isn't a blanket free-pass."""
    result = config_schema.unknown_config_keys({"skills": {"typo_field": 1}})
    assert "skills.typo_field" in result
