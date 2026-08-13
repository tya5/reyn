"""Tier 1/2: #4515 — a real `external_transports:` config was falsely
reported "not recognized ... NOT APPLIED", when `loader.py`'s
`_build_external_transports_config` was in fact applying it correctly.

Root cause: `ReynConfig.external_transports`'s field TYPE
(`ExternalTransportRouting`) wraps a single `transports: dict` field for
the wrapper's own `.get(name)` convenience — but the real reyn.yaml shape
has no nested `transports:` key at all (`external_transports: {broker:
{...}}` directly). The schema walk recursed into the wrapper and
registered the dict-leaf one level too deep
(`external_transports.transports`), so every real transport name matched
none of the known-key sets.

Fixed via `field(metadata={'dict_leaf': True})` — a reusable escape hatch
(`config_schema._is_dict_leaf_override`) for the whole shape class, not a
single-field patch. `mcp`/`skills`/`pipelines`/`presentations` were
audited as the "family" (lead-coder's own instruction) and confirmed
NOT to share this bug — they're declared as bare `dict` fields directly
on `ReynConfig`, never wrapped in an intermediate dataclass, so they
never had the mismatch in the first place.
"""
from __future__ import annotations

from reyn.config import config_schema
from reyn.config.loader import _warn_unknown_config_keys


def test_external_transports_with_a_real_transport_name_is_not_flagged_unknown():
    """Tier 1: #4515's own reproduction — a real `broker` transport under
    `external_transports:` must not be reported unknown."""
    result = config_schema.unknown_config_keys({
        "external_transports": {
            "broker": {"mcp_tool": "broker__post_message", "args_template": {}},
        },
    })
    assert result == {}


def test_external_transports_is_a_dict_leaf_at_its_own_key():
    """Tier 1: the schema registers `external_transports` itself as a
    dict-leaf — never a deeper `external_transports.transports` node
    that no real config ever reaches."""
    by_key = {n.key: n for n in config_schema.walk_config_schema()}
    assert by_key["external_transports"].is_dict_leaf is True
    assert "external_transports.transports" not in by_key


def test_external_transports_is_a_known_top_level_key():
    """Tier 1: regression guard for `known_top_level_keys()`, the OTHER
    consumer of the same schema walk (#4174 T0's single-source-of-truth
    requirement)."""
    assert "external_transports" in config_schema.known_top_level_keys()


def test_a_genuinely_unknown_key_is_still_flagged():
    """Tier 1: the fix does not over-broaden — an actually-unknown
    top-level key is still reported (the fix targets the ONE mismatched
    field, not the detector in general)."""
    result = config_schema.unknown_config_keys({"totally_bogus_key": 1})
    assert "totally_bogus_key" in result


def test_sandbox_policy_invalid_sub_key_is_still_flagged():
    """Tier 1: regression guard — `sandbox.policy`'s own registered
    freeform-leaf validator (an unrelated dict-leaf mechanism, #3823)
    still rejects a truly invalid sub-key; the #4515 fix does not touch
    it."""
    result = config_schema.unknown_config_keys({
        "sandbox": {"policy": {"not_a_real_policy_key": 1}},
    })
    assert "sandbox.policy.not_a_real_policy_key" in result


def test_end_to_end_warn_unknown_config_keys_no_longer_flags_a_real_transport():
    """Tier 2: the REAL operator-visible symptom (#4515's own
    reproduction) through `_warn_unknown_config_keys` — the exact
    function that generates "config key ... is not recognized ... it was
    NOT APPLIED" — no longer flags a real `external_transports:` block."""
    result = _warn_unknown_config_keys({
        "external_transports": {
            "broker": {"mcp_tool": "broker__post_message", "args_template": {}},
        },
    })
    assert result == {}


# ── family sweep (lead-coder: "1 つ直して終わりにしない ── 族で探す") ──────


def test_mcp_servers_arbitrary_name_still_recognized():
    """Tier 1: `mcp:` (bare dict field, no wrapper dataclass) never had
    the #4515 mismatch — confirmed as part of the family sweep, not
    assumed."""
    result = config_schema.unknown_config_keys({
        "mcp": {"servers": {"my_server": {"command": "foo"}}},
    })
    assert result == {}


def test_llm_router_fallbacks_arbitrary_name_still_recognized():
    """Tier 1: `llm.router.fallbacks` — its builder reads
    `raw.get("fallbacks", ...)`, matching the schema's own nesting
    exactly (unlike external_transports's builder, which received the
    whole section with no wrapper key). Confirmed, not assumed."""
    result = config_schema.unknown_config_keys({
        "llm": {"router": {"fallbacks": {"gpt-4": ["gpt-3.5"]}}},
    })
    assert result == {}


def test_auth_providers_arbitrary_name_still_recognized():
    """Tier 1: `auth.providers` — same audit as above."""
    result = config_schema.unknown_config_keys({
        "auth": {"providers": {"github": {"client_id": "x"}}},
    })
    assert result == {}


def test_gateway_surfaces_enabled_arbitrary_name_still_recognized():
    """Tier 1: `gateway.surfaces.enabled` — same audit as above."""
    result = config_schema.unknown_config_keys({
        "gateway": {"surfaces": {"enabled": {"web_ui": True}}},
    })
    assert result == {}
