"""Tier 2: #5084 ③-b — an agent's ``profile.yaml`` ``broker_identity`` is
turned into the 2 broker-participation hooks (:mod:`reyn.runtime.
broker_hooks`) on the SAME layered COMBINE ``_build_hook_registry`` already
does for runtime/per-agent/per-session — a "derived" layer, additive like
its untrusted siblings.

Owner's own acceptance witness (relayed via architect on #5084's issue
thread): two ``profile.yaml``s, no slash command, each ``--connect`` boots
wired to the broker under its OWN identity. This is the piece that makes
"wired to the broker" true — ①(base_dir)/②(project_context_path)/③-a
(the field itself) already land; this is ③-b.

Real ``Session``/``AgentProfile`` construction throughout (``make_session``,
the same test-support helper #2073's own per-agent-hooks tests use) — no
mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_AGENT = "coder-smith"


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name=_AGENT,
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(),
    )


def _write_profile(session: Session, *, broker_identity: "str | None") -> None:
    AgentProfile.new(_AGENT, broker_identity=broker_identity).save(session.workspace_dir)


def test_broker_identity_derives_the_two_broker_hooks(tmp_path: Path) -> None:
    """Tier 2: a profile WITH ``broker_identity`` set derives both the
    ``mcp_resource_updated`` inbox-wake hook and the ``session_start``
    register-with-broker exec hook, addressed to THIS agent's own identity."""
    session = _make_session(tmp_path)
    _write_profile(session, broker_identity="coder-smith")

    registry = session._build_hook_registry()

    # Unpacking (not a len() pin) is itself the assertion that derivation
    # produced EXACTLY one of each — it raises if the count differs.
    (push_hook,) = registry.hooks_for("mcp_resource_updated")
    assert push_hook.matcher == {"server": "broker", "uri": "broker://inbox/coder-smith"}
    assert "coder-smith" in push_hook.template_push.message

    (start_hook,) = registry.hooks_for("session_start")
    assert start_hook.exec == ("python3", "register_with_broker.py")


def test_no_broker_identity_derives_nothing(tmp_path: Path) -> None:
    """Tier 2: regression guard — an agent with NO ``broker_identity``
    (every pre-#5084 agent, including a profile-less one) derives NEITHER
    hook. Absence must not silently opt an agent INTO broker participation."""
    session = _make_session(tmp_path)
    _write_profile(session, broker_identity=None)

    registry = session._build_hook_registry()

    assert registry.hooks_for("mcp_resource_updated") == []
    assert registry.hooks_for("session_start") == []


def test_no_profile_at_all_derives_nothing(tmp_path: Path) -> None:
    """Tier 2: a Session with no profile.yaml on disk at all (every
    programmatically-constructed test Session, ``reyn pipe run``'s default
    identity) derives nothing — the same "absent file -> no override" non-
    error posture as ``_agent_profile_preferences``, one level up."""
    session = _make_session(tmp_path)

    registry = session._build_hook_registry()

    assert registry.hooks_for("mcp_resource_updated") == []
    assert registry.hooks_for("session_start") == []


@pytest.mark.asyncio
async def test_a_malformed_derived_layer_never_taints_the_other_layers(tmp_path: Path) -> None:
    """Tier 2: the derived layer follows the SAME per-layer boot resilience
    every other untrusted layer already has — if derivation ever produced a
    hook the loader rejects, the startup layer (and any other good layer)
    must survive. Exercised here via a hand-broken profile.yaml that raises
    ValueError out of AgentProfile.load (an unknown/malformed key), which
    ``_derive_broker_hooks`` must swallow into ``[]``, not propagate."""
    session = _make_session(tmp_path)
    profile_path = session.workspace_dir / "profile.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        "name: coder-smith\nrole: null\ncreated_at: '2026-01-01T00:00:00+00:00'\n"
        "preferences:\n  this_key_does_not_exist: true\n",
        encoding="utf-8",
    )

    registry = session._build_hook_registry()  # must not raise

    assert registry.hooks_for("mcp_resource_updated") == []
