"""Tier 2: #5091 — the broker-participation use case #5084 ③-b built a
dedicated mechanism for (`broker_hooks.py`, `AgentProfile.broker_identity`,
a derived hook-registry layer) is fully served by the ALREADY-EXISTING
per-session hooks layer (#2285, `Session._read_per_session_hooks`) — no
mechanism was lost by removing the dedicated one.

Owner ruling (relayed via architect): "broker" is an external MCP server,
not a reyn-runtime concept — the derivation baked a specific integration's
name (`"server": "broker"`) into reyn's own source, and #5012-A's own
discriminator ("would another integration need the SAME code?" — yes,
`slack_identity` + `derive_slack_hooks` — disqualified) applies. Architect's
own measured finding: `_read_per_session_hooks`'s own docstring already
says a hook defined there "is visible ONLY to this session" — exactly the
scoping a per-agent broker-inbox subscription needs, so a HAND-WRITTEN,
literal (non-templated) hook in that file already does the job; no
identity field, no derivation function, no dedicated layer.

Real `Session` construction (`make_session`, the same helper #2073's own
per-agent-hooks tests use) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path, agent_name: str) -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / agent_name / "snap.json",
        reactivity=ReactivityConfig(),
    )


def _write_broker_inbox_hook(session: Session, *, own_identity: str) -> None:
    """The hand-written form architect's own #5091 demonstration used —
    a LITERAL uri, no token/templating, because the per-session hooks
    layer is already scoped to exactly one agent's own session."""
    snapshot_dir = Path(session._snapshot_path).parent
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "hooks.yaml").write_text(
        "hooks:\n"
        "  - on: mcp_resource_updated\n"
        "    matcher:\n"
        "      server: broker\n"
        f"      uri: \"broker://inbox/{own_identity}\"\n"
        "    template_push:\n"
        "      message: \"new broker message\"\n"
        "      wake: true\n",
        encoding="utf-8",
    )


def test_two_agents_each_subscribe_their_own_broker_inbox_with_no_derivation(
    tmp_path: Path,
):
    """Tier 2: #5091's own acceptance witness — 2 agents, each with a
    hand-written per-session `hooks.yaml` naming its OWN broker inbox URI,
    resolve to 2 DIFFERENT `mcp_resource_updated` matchers — neither sees
    the other's, and neither required any per-agent identity field or
    derivation function. Real ``Session._build_hook_registry`` (the
    layered COMBINE), not a stand-in."""
    session_a = _make_session(tmp_path, "coder-smith")
    session_b = _make_session(tmp_path, "coder-brown")
    _write_broker_inbox_hook(session_a, own_identity="coder-smith")
    _write_broker_inbox_hook(session_b, own_identity="coder-brown")

    registry_a = session_a._build_hook_registry()
    registry_b = session_b._build_hook_registry()

    (hook_a,) = registry_a.hooks_for("mcp_resource_updated")
    (hook_b,) = registry_b.hooks_for("mcp_resource_updated")
    assert hook_a.matcher == {"server": "broker", "uri": "broker://inbox/coder-smith"}
    assert hook_b.matcher == {"server": "broker", "uri": "broker://inbox/coder-brown"}


def test_no_hooks_yaml_means_no_broker_subscription(tmp_path: Path):
    """Tier 2: regression guard — an agent with no per-session `hooks.yaml`
    at all (every agent that isn't opting into broker participation,
    including the default agent) subscribes nothing. Absence must not
    silently opt an agent in."""
    session = _make_session(tmp_path, "plain-agent")
    registry = session._build_hook_registry()
    assert registry.hooks_for("mcp_resource_updated") == []


def test_strip_falsifier_removing_the_per_session_read_breaks_the_witness(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: strip-falsifier — with `_read_per_session_hooks` forced to
    return `[]` (simulating its removal from `_build_hook_registry`'s
    COMBINE), the SAME per-session `hooks.yaml` this module's own positive
    witness relies on no longer resolves — confirming the positive witness
    genuinely depends on that layer, not on some other coincidental path."""
    session = _make_session(tmp_path, "coder-smith")
    _write_broker_inbox_hook(session, own_identity="coder-smith")
    monkeypatch.setattr(session, "_read_per_session_hooks", lambda: [])

    registry = session._build_hook_registry()

    assert registry.hooks_for("mcp_resource_updated") == []
