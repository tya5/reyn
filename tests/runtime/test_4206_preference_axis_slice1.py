"""Tier 2: #4206 slice 1 — the ③ preference axis (free-override composition).

Real instances only: `reyn.runtime.preferences`'s pure functions are tested
directly (duck-typed dict-in/value-out helpers, same category as
`test_events_pure_helpers.py`); `AgentProfile.preferences` and
`Session.output_language` are tested against real on-disk files and a real
`Session` (`make_session`), never a stand-in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.preferences import (
    PREFERENCE_KEYS,
    UnknownPreferenceKeyError,
    resolve_preference,
    validate_preferences,
)
from reyn.runtime.profile import AgentProfile
from tests._support.agent_session import make_session

# ── PREFERENCE_KEYS / validate_preferences ──────────────────────────────


def test_preference_keys_names_all_9_confirmed_keys():
    """Tier 2: the 9 keys #4206's confirmed classification names for this
    slice — output_language, chat.reasoning.display, 6x cost.*.warn_ratio,
    cost.rate_limit_warn_ratio."""
    assert PREFERENCE_KEYS == frozenset({
        "output_language",
        "chat.reasoning.display",
        "cost.per_agent_tokens.warn_ratio",
        "cost.per_agent_cost_usd.warn_ratio",
        "cost.daily_tokens.warn_ratio",
        "cost.daily_cost_usd.warn_ratio",
        "cost.monthly_tokens.warn_ratio",
        "cost.monthly_cost_usd.warn_ratio",
        "cost.rate_limit_warn_ratio",
    })


def test_validate_preferences_accepts_a_known_key():
    """Tier 2: accept-side — a real declared key passes without raising."""
    validate_preferences({"output_language": "ja"}, source="test")


def test_validate_preferences_accepts_empty_dict():
    """Tier 2: accept-side — the common "no override set" case."""
    validate_preferences({}, source="test")


def test_validate_preferences_raises_on_unknown_key():
    """Tier 2: a typo'd/unrecognized key raises loudly rather than being
    silently ignored — the #4655 Kind① discipline applied to this axis."""
    with pytest.raises(UnknownPreferenceKeyError, match="typo_key"):
        validate_preferences({"typo_key": "x"}, source="test-source")


# ── resolve_preference ──────────────────────────────────────────────────


def test_resolve_preference_returns_default_when_no_overrides():
    """Tier 2: no agent/session override present — the project default
    passes through unchanged."""
    assert resolve_preference("output_language", "en") == "en"


def test_resolve_preference_agent_override_wins_over_default():
    """Tier 2: an agent-layer override, with no session-layer override
    present, wins over the project default."""
    assert resolve_preference(
        "output_language", "en", agent_preferences={"output_language": "ja"},
    ) == "ja"


def test_resolve_preference_session_override_wins_over_agent_and_default():
    """Tier 2: THE free-override composition rule — session beats agent
    beats project default, no restriction/ceiling check (unlike ①/②)."""
    assert resolve_preference(
        "output_language", "en",
        agent_preferences={"output_language": "ja"},
        session_preferences={"output_language": "fr"},
    ) == "fr"


def test_resolve_preference_agent_override_survives_an_absent_session_key():
    """Tier 2: a session_preferences dict that doesn't mention this key
    falls through to the agent override, not the default."""
    assert resolve_preference(
        "output_language", "en",
        agent_preferences={"output_language": "ja"},
        session_preferences={"chat.reasoning.display": True},
    ) == "ja"


def test_resolve_preference_rejects_an_unknown_key():
    """Tier 2: resolve_preference itself enforces PREFERENCE_KEYS membership
    — a caller cannot silently resolve a key that isn't declared."""
    with pytest.raises(UnknownPreferenceKeyError):
        resolve_preference("not_a_real_key", "default")


# ── AgentProfile.preferences ─────────────────────────────────────────────


def test_agent_profile_preferences_defaults_to_empty_dict():
    """Tier 2: a freshly-created AgentProfile has no preference overrides —
    the common case for most agents."""
    profile = AgentProfile.new("alice")
    assert profile.preferences == {}


def test_agent_profile_preferences_round_trips_through_save_and_load(tmp_path: Path):
    """Tier 2: a real save() then load() cycle preserves preferences
    exactly — the on-disk YAML shape round-trips."""
    profile = AgentProfile.new("alice")
    object.__setattr__(profile, "preferences", {"output_language": "ja"})
    profile.save(tmp_path)

    loaded = AgentProfile.load(tmp_path)
    assert loaded.preferences == {"output_language": "ja"}


def test_agent_profile_load_raises_on_an_unknown_preference_key(tmp_path: Path):
    """Tier 2: a hand-edited profile.yaml with a typo'd preferences key
    fails LOUDLY at load time, matching #4655's own Kind① discipline."""
    (tmp_path / "profile.yaml").write_text(
        "name: alice\nrole: ''\ncreated_at: ''\n"
        "preferences:\n  not_a_real_key: x\n",
        encoding="utf-8",
    )
    with pytest.raises(UnknownPreferenceKeyError):
        AgentProfile.load(tmp_path)


def test_agent_profile_save_omits_empty_preferences_from_the_yaml(tmp_path: Path):
    """Tier 2: (accept-side) the on-disk shape stays minimal when no
    preference is set — same discipline as the existing allowed_mcp field."""
    profile = AgentProfile.new("alice")
    profile.save(tmp_path)
    assert "preferences" not in (tmp_path / "profile.yaml").read_text(encoding="utf-8")


# ── Session.output_language — real Session, real files ──────────────────


def _agent_dir(session) -> Path:
    return session.workspace_dir


def _session_config_path(session) -> Path:
    return Path(session._snapshot_path).parent / "config.yaml"


def test_output_language_reads_the_project_level_default_with_no_overrides(tmp_path: Path):
    """Tier 2: a real Session with no agent/session preference file at all
    — output_language reads the project-level default it was constructed
    with, byte-identical to pre-#4206 behavior."""
    session = make_session(
        agent_name="pref-test-1", workspace_state_dir=tmp_path / ".reyn",
        output_language="en",
    )
    assert session.output_language == "en"


def _write_agent_preferences(session, name: str, preferences: dict) -> None:
    # Session construction does not itself write profile.yaml to disk
    # (make_session builds Agent/Session directly) — a fresh AgentProfile
    # is the real on-disk shape a caller like `reyn agent new` produces.
    profile = AgentProfile.new(name)
    object.__setattr__(profile, "preferences", preferences)
    profile.save(_agent_dir(session))


def test_output_language_agent_layer_override_wins_over_project_default(tmp_path: Path):
    """Tier 2: a real profile.yaml `preferences.output_language` override,
    written to the SAME on-disk location `AgentProfile.load` reads, wins
    over the Session's own project-level default."""
    session = make_session(
        agent_name="pref-test-2", workspace_state_dir=tmp_path / ".reyn",
        output_language="en",
    )
    _write_agent_preferences(session, "pref-test-2", {"output_language": "ja"})

    assert session.output_language == "ja"


def test_output_language_session_layer_override_wins_over_agent_layer(tmp_path: Path):
    """Tier 2: THE composition witness for a real Session — a session-layer
    config.yaml override wins over BOTH the agent-layer profile.yaml
    override and the project default, matching resolve_preference's own
    unit-level composition test above but through the real file-reading
    path this time."""
    import yaml

    session = make_session(
        agent_name="pref-test-3", workspace_state_dir=tmp_path / ".reyn",
        output_language="en",
    )
    _write_agent_preferences(session, "pref-test-3", {"output_language": "ja"})

    cfg_path = _session_config_path(session)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"name": "_session_x", "preferences": {"output_language": "fr"}}),
        encoding="utf-8",
    )

    assert session.output_language == "fr"


def test_output_language_is_a_live_re_read_not_a_frozen_snapshot(tmp_path: Path):
    """Tier 2: (accept-side) editing profile.yaml AFTER Session construction
    takes effect on the next read — same "live re-read" shape
    `_workspace_base_dir` already established for `base_dir`."""
    session = make_session(
        agent_name="pref-test-4", workspace_state_dir=tmp_path / ".reyn",
        output_language="en",
    )
    assert session.output_language == "en"

    _write_agent_preferences(session, "pref-test-4", {"output_language": "de"})

    assert session.output_language == "de"


def test_output_language_malformed_agent_preferences_falls_back_not_crashes(tmp_path: Path):
    """Tier 2: an unknown preference key in profile.yaml is surfaced (not a
    crash) — output_language falls back to the project default rather than
    raising through a live property access."""
    session = make_session(
        agent_name="pref-test-5", workspace_state_dir=tmp_path / ".reyn",
        output_language="en",
    )
    (_agent_dir(session) / "profile.yaml").write_text(
        "name: pref-test-5\nrole: ''\ncreated_at: ''\n"
        "preferences:\n  not_a_real_key: x\n",
        encoding="utf-8",
    )
    assert session.output_language == "en"
