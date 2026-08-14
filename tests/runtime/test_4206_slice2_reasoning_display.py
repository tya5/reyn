"""Tier 2: #4206 slice 2 — ③ preference axis, ``chat.reasoning.display``.

Extends slice 1 (``output_language``) to a second ③ key, wired through the
new shared ``Session._resolve_session_preference`` helper and a
``reasoning_display_fn`` callback into ``RouterHostAdapter`` — the same
"live callback, not a frozen construction-time value" shape
``reasoning_continuity_section_fn`` already established. Deliberately
narrow: ``chat.reasoning.continuity``/``recent_turns`` are ② bounding
(#4206's own ratified classification), unaffected by this slice, and NOT
tested here (their own coverage is unchanged).

Real instances only: a real ``Session`` (``make_session``) + a real
``RouterHostAdapter`` reached through ``session.router_host``, never a
stand-in.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.chat import ReasoningConfig
from reyn.runtime.preferences import PREFERENCE_KEYS
from reyn.runtime.profile import AgentProfile
from tests._support.agent_session import make_session


def _agent_dir(session) -> Path:
    return session.workspace_dir


def _session_config_path(session) -> Path:
    return Path(session._snapshot_path).parent / "config.yaml"


def _write_agent_preferences(session, name: str, preferences: dict) -> None:
    profile = AgentProfile.new(name)
    object.__setattr__(profile, "preferences", preferences)
    profile.save(_agent_dir(session))


def test_chat_reasoning_display_is_a_declared_preference_key():
    """Tier 2: (accept-side) chat.reasoning.display is in PREFERENCE_KEYS —
    slice 1 already declared it; slice 2 wires it, doesn't add it."""
    assert "chat.reasoning.display" in PREFERENCE_KEYS


def test_reasoning_display_reads_the_project_level_default_with_no_overrides(tmp_path: Path):
    """Tier 2: a real Session with no agent/session preference file at all
    — reasoning_display reads the project-level default (ReasoningConfig.display)
    it was constructed with, byte-identical to pre-#4206-slice-2 behavior."""
    session = make_session(
        agent_name="reasoning-pref-1", workspace_state_dir=tmp_path / ".reyn",
        reasoning_config=ReasoningConfig(display=True),
    )
    assert session.reasoning_display is True


def test_reasoning_display_agent_layer_override_wins_over_project_default(tmp_path: Path):
    """Tier 2: a real profile.yaml preferences.chat.reasoning.display
    override wins over the Session's own project-level default."""
    session = make_session(
        agent_name="reasoning-pref-2", workspace_state_dir=tmp_path / ".reyn",
        reasoning_config=ReasoningConfig(display=False),
    )
    _write_agent_preferences(
        session, "reasoning-pref-2", {"chat.reasoning.display": True},
    )

    assert session.reasoning_display is True


def test_reasoning_display_session_layer_override_wins_over_agent_layer(tmp_path: Path):
    """Tier 2: session-layer config.yaml override wins over BOTH the
    agent-layer profile.yaml override and the project default."""
    import yaml

    session = make_session(
        agent_name="reasoning-pref-3", workspace_state_dir=tmp_path / ".reyn",
        reasoning_config=ReasoningConfig(display=False),
    )
    _write_agent_preferences(
        session, "reasoning-pref-3", {"chat.reasoning.display": True},
    )

    cfg_path = _session_config_path(session)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"name": "_session_x", "preferences": {"chat.reasoning.display": False}}),
        encoding="utf-8",
    )

    assert session.reasoning_display is False


def test_reasoning_display_is_a_live_re_read_not_a_frozen_snapshot(tmp_path: Path):
    """Tier 2: (accept-side) editing profile.yaml AFTER Session construction
    takes effect on the next read — same live-re-read shape output_language
    already established."""
    session = make_session(
        agent_name="reasoning-pref-4", workspace_state_dir=tmp_path / ".reyn",
        reasoning_config=ReasoningConfig(display=False),
    )
    assert session.reasoning_display is False

    _write_agent_preferences(
        session, "reasoning-pref-4", {"chat.reasoning.display": True},
    )

    assert session.reasoning_display is True


# ── RouterHostAdapter callback wiring ────────────────────────────────────


def test_router_host_reasoning_display_enabled_reflects_the_live_session_property(tmp_path: Path):
    """Tier 2: THE end-to-end witness — RouterHostAdapter.reasoning_display_enabled()
    (what the router loop actually consults) reflects the SAME live
    session/agent-preference override as Session.reasoning_display, via the
    reasoning_display_fn callback wired at construction — not the frozen
    reasoning_config.display value the adapter was built with."""
    session = make_session(
        agent_name="reasoning-pref-5", workspace_state_dir=tmp_path / ".reyn",
        reasoning_config=ReasoningConfig(display=False),
    )
    assert session.router_host.reasoning_display_enabled() is False

    _write_agent_preferences(
        session, "reasoning-pref-5", {"chat.reasoning.display": True},
    )

    assert session.reasoning_display is True
    assert session.router_host.reasoning_display_enabled() is True


def test_reasoning_display_fn_none_falls_back_to_frozen_config():
    """Tier 2: (accept-side) a RouterHostAdapter built WITHOUT
    reasoning_display_fn (every pre-slice-2 caller, every existing test
    host) falls back to the original frozen reasoning_config.display read
    — byte-identical to before this slice."""
    from reyn.runtime.services.router_host_adapter import RouterHostAdapter
    from tests._support.router_host_adapter import make_adapter

    adapter: RouterHostAdapter = make_adapter(
        universal_wrappers_enabled=False,
    )
    # make_adapter doesn't accept reasoning_config directly, so the default
    # (None -> getattr fallback -> False) is exercised here.
    assert adapter.reasoning_display_enabled() is False
