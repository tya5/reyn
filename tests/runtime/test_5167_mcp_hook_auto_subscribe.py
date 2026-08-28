"""Tier 2: #5167 — declaring an ``mcp_resource_updated`` hook with a
CONCRETE (server, uri) matcher auto-subscribes it at session start, with
NO LLM turn ever required (architect ruling, issuecomment-5384120494).

Before this: the only construction site for a resource subscription was
the LLM-facing ``subscribe_mcp_resource`` tool (``tools/mcp.py``) — a
declared hook whose agent never happened to call it silently never fired,
with no warning anywhere. Charter lens 3 (deterministic, not stuffed into
the prompt) names exactly this gap.

Acceptance (architect, issuecomment-5384120494/5384128053):
  ① declare -> session start -> never speak -> subscription exists.
     Observed through ``Session.mcp_subscription_state()`` (#4686's public
     read model over ``MCPConnectionService.subscribed_uris`` — #2597 s2b's
     own observation point, reached here through the public surface rather
     than the private connection-service attribute, per the testing policy).
  ② unreachable/denied -> a ``mcp_hook_subscribe_not_applied`` warning +
     audit-event names the hook — never silent.
  ③ strip (revert the auto-subscribe call) -> ① goes red. Demonstrated by
     the PR's own strip-falsify pass (this module's own witness IS ①;
     there is no separate "strip" test — reverting ① and re-running it is
     what proves ③, not a second copy of the same assertion).
  ④ NONE of ①-③ requires an LLM turn. Structural here, not incidental:
     every test below drives ``Session._auto_subscribe_mcp_resource_hooks``
     directly (mirroring ``test_5091_broker_participation_via_per_session_
     hooks.py``'s own convention of driving ``HookDispatcher.dispatch``
     directly rather than the full ``run()`` loop) — no test in this module
     ever calls ``run_one_iteration``/dispatches a router turn, so an LLM
     call is structurally unreachable from any of them, not merely absent
     by observation.

Real ``Session`` + a real subscribable MCP server subprocess (the SAME
support double ``test_2597_s2b_resource_subscriptions.py`` uses) — no
mocks, per the testing policy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from reyn.security.permissions.permissions import PermissionResolver
from reyn.security.sandbox.noop_backend import NoopBackend
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle
from tests._support.paths import REPO_ROOT

_SUPPORT_DIR = REPO_ROOT / "tests" / "_support"
_SUBSCRIBABLE_SERVER = _SUPPORT_DIR / "mcp_subscribable_resources_server.py"
_URI = "resource://counter"


def _stdio_cfg(script: Path) -> dict:
    return {"type": "stdio", "command": sys.executable, "args": [str(script)]}


def _write_hooks_yaml(session: Session, *, server: "str | None", uri: "str | None") -> None:
    snapshot_dir = Path(session._snapshot_path).parent
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    matcher_lines = ""
    if server is not None:
        matcher_lines += f"      server: {server}\n"
    if uri is not None:
        matcher_lines += f'      uri: "{uri}"\n'
    matcher_block = f"    matcher:\n{matcher_lines}" if matcher_lines else ""
    (snapshot_dir / "hooks.yaml").write_text(
        "hooks:\n"
        "  - on: mcp_resource_updated\n"
        f"{matcher_block}"
        "    template_push:\n"
        '      message: "resource pushed"\n'
        "      wake: true\n",
        encoding="utf-8",
    )


def _make_session(
    tmp_path: Path,
    agent_name: str,
    *,
    mcp_servers: "dict | None" = None,
    permission_resolver: "PermissionResolver | None" = None,
) -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / agent_name / "snap.json",
        reactivity=ReactivityConfig(),
        sandbox_backend=NoopBackend(),
        mcp_servers=mcp_servers,
        permission_resolver=permission_resolver,
    )


def _subscribed_uris(session: Session, server: str) -> list:
    """The subscribed-URI set for *server*, read through the PUBLIC
    ``Session.mcp_subscription_state()`` read model (#4686 — the same one
    the status-bar/MCP-pane use) rather than reaching into
    ``session._mcp_connection_service`` directly — the testing policy
    forbids a private-state assertion, and this is the public surface that
    exists for exactly this observation."""
    for entry in session.mcp_subscription_state():
        if entry["server"] == server:
            return entry["uris"]
    return []


def _allow_all_resolver(tmp_path: Path, server: str) -> PermissionResolver:
    """Non-interactive, config-pre-approved for *server* — no prompt ever
    reachable (mirrors #2597 s2b's own ``interactive=False`` + config-approve
    pattern), so a real subscribe succeeds with zero human/LLM involvement."""
    return PermissionResolver(
        config_permissions={"mcp": {server: "allow"}},
        project_root=tmp_path,
        interactive=False,
    )


# ── ① real subscribe, no LLM turn, no explicit tool call ──────────────────


@pytest.mark.asyncio
async def test_declared_concrete_hook_is_subscribed_with_no_turn_and_no_tool_call(
    tmp_path: Path,
):
    """Tier 2: acceptance ① — a concrete (server, uri) matcher subscribes
    for real, driven only by session startup's own auto-subscribe pass.
    Never calls run()/run_one_iteration/dispatches any router turn — the
    LLM is structurally never in this call graph (acceptance ④)."""
    resolver = _allow_all_resolver(tmp_path, "srv")
    session = _make_session(
        tmp_path, "coder-x",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )
    _write_hooks_yaml(session, server="srv", uri=_URI)
    session._hook_dispatcher.replace_registry(session._build_hook_registry())

    try:
        await session._auto_subscribe_mcp_resource_hooks()

        assert _subscribed_uris(session, "srv") == [_URI], (
            "declaring the hook did not produce a real subscription at "
            "session start — the agent never called subscribe_mcp_resource "
            "and no LLM turn ever ran"
        )
    finally:
        await session.aclose_mcp_connections()


@pytest.mark.asyncio
async def test_no_declared_hook_means_no_subscribe_attempt(tmp_path: Path):
    """Tier 2: regression guard mirroring #5091's own "absence must not
    silently opt in" — a session with no mcp_resource_updated hook declared
    at all subscribes nothing and touches the connection service not at all."""
    resolver = _allow_all_resolver(tmp_path, "srv")
    session = _make_session(
        tmp_path, "coder-y",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )

    await session._auto_subscribe_mcp_resource_hooks()

    assert _subscribed_uris(session, "srv") == []


# ── ② silence closed: every non-concrete/failed case is named, never mute ──


@pytest.mark.asyncio
async def test_unconfigured_server_emits_warning_and_audit_event(tmp_path: Path):
    """Tier 2: acceptance ② — a hook naming a server that isn't in
    mcp_servers at all cannot be auto-subscribed; this must be VISIBLE
    (warning + audit-event naming the hook), never a silent no-op."""
    events = EventLog()
    session = _make_session(tmp_path, "coder-z", mcp_servers={})
    session._audit_events = events
    _write_hooks_yaml(session, server="not-configured", uri=_URI)
    session._hook_dispatcher.replace_registry(session._build_hook_registry())
    collected = collect_events(events)

    await session._auto_subscribe_mcp_resource_hooks()
    await settle(events)

    matching = [e for e in collected if e.type == "mcp_hook_subscribe_not_applied"]
    assert matching, (
        "an unconfigured-server hook produced no mcp_hook_subscribe_not_applied "
        "audit-event — the declaration's non-effect went silent"
    )
    assert matching[0].data.get("server") == "not-configured"
    assert matching[0].data.get("uri") == _URI
    assert "not configured" in str(matching[0].data.get("reason", ""))


@pytest.mark.asyncio
async def test_permission_denied_emits_warning_and_audit_event(tmp_path: Path):
    """Tier 2: acceptance ② second path — permission genuinely refused
    (no config approval, non-interactive so no prompt can rescue it) also
    surfaces via the SAME warning+audit-event path, not a raised exception
    that could crash session startup."""
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    session = _make_session(
        tmp_path, "coder-w",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )
    session._audit_events = events
    _write_hooks_yaml(session, server="srv", uri=_URI)
    session._hook_dispatcher.replace_registry(session._build_hook_registry())
    collected = collect_events(events)

    await session._auto_subscribe_mcp_resource_hooks()
    await settle(events)

    matching = [e for e in collected if e.type == "mcp_hook_subscribe_not_applied"]
    assert matching, "a permission-denied auto-subscribe must not fail silently"
    assert matching[0].data.get("server") == "srv"
    assert "permission" in str(matching[0].data.get("reason", "")).lower()
    assert _subscribed_uris(session, "srv") == []


@pytest.mark.asyncio
async def test_glob_uri_matcher_is_not_auto_subscribed_but_is_reported(tmp_path: Path):
    """Tier 2: a matcher's uri may glob (reyn.hooks.matcher's own field) —
    a real, useful pattern for narrowing which pushes a hook reacts to, but
    NOT a concrete resource an MCP subscribe request can name. Auto-
    subscribe must never invent an ambiguous subscription; it reports the
    non-effect the same way as any other unhonored case, and the agent's
    own explicit subscribe_mcp_resource tool call is left as the path for
    this case (unchanged from before #5167)."""
    events = EventLog()
    resolver = _allow_all_resolver(tmp_path, "srv")
    session = _make_session(
        tmp_path, "coder-v",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )
    session._audit_events = events
    _write_hooks_yaml(session, server="srv", uri="resource://job/*/progress")
    session._hook_dispatcher.replace_registry(session._build_hook_registry())
    collected = collect_events(events)

    await session._auto_subscribe_mcp_resource_hooks()
    await settle(events)

    assert _subscribed_uris(session, "srv") == [], (
        "a glob uri must never be auto-subscribed — it names a SET, not one "
        "concrete resource"
    )
    matching = [e for e in collected if e.type == "mcp_hook_subscribe_not_applied"]
    assert matching, "a glob-uri hook's non-effect must still be reported"
    assert "glob" in str(matching[0].data.get("reason", "")).lower()


@pytest.mark.asyncio
async def test_matcher_missing_server_or_uri_is_reported_not_silently_skipped(
    tmp_path: Path,
):
    """Tier 2: a hook with no matcher (or one missing server/uri) has
    nothing concrete to auto-subscribe to — this is a LEGITIMATE
    configuration (the agent may still call subscribe_mcp_resource itself
    for whatever it decides to react to), but it must still be named, not
    silently absorbed as "nothing to do here."""
    events = EventLog()
    resolver = _allow_all_resolver(tmp_path, "srv")
    session = _make_session(
        tmp_path, "coder-u",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )
    session._audit_events = events
    _write_hooks_yaml(session, server=None, uri=None)
    session._hook_dispatcher.replace_registry(session._build_hook_registry())
    collected = collect_events(events)

    await session._auto_subscribe_mcp_resource_hooks()
    await settle(events)

    matching = [e for e in collected if e.type == "mcp_hook_subscribe_not_applied"]
    assert matching, "a matcher-less hook's non-effect must still be reported"
    assert matching[0].data.get("server") is None
    assert matching[0].data.get("uri") is None


@pytest.mark.asyncio
async def test_ephemeral_session_never_subscribes_but_still_reports_why(
    tmp_path: Path,
):
    """Tier 2: architect non-blocking review on #5180 (TESTS-READY(A),
    issuecomment-5384348643) — mirrors _mcp_subscribe_resource's own
    ephemeral refusal (a subscription is only meaningful on a persistent
    connection), but the refusal must NOT be a silent early-return before
    even looking at declared hooks. A declared hook on an ephemeral session
    that subscribed nothing and said nothing would be indistinguishable
    from the original #5167 bug (declared, never honored, never
    explained) — so this still enumerates and reports through the SAME
    mcp_hook_subscribe_not_applied path every other unhonored case uses."""
    events = EventLog()
    resolver = _allow_all_resolver(tmp_path, "srv")
    session = _make_session(
        tmp_path, "coder-t",
        mcp_servers={"srv": _stdio_cfg(_SUBSCRIBABLE_SERVER)},
        permission_resolver=resolver,
    )
    session.mark_ephemeral()
    session._audit_events = events
    _write_hooks_yaml(session, server="srv", uri=_URI)
    session._hook_dispatcher.replace_registry(session._build_hook_registry())
    collected = collect_events(events)

    await session._auto_subscribe_mcp_resource_hooks()
    await settle(events)

    assert _subscribed_uris(session, "srv") == [], (
        "an ephemeral session must never actually subscribe — no persistent "
        "connection exists for a push to arrive on"
    )
    matching = [e for e in collected if e.type == "mcp_hook_subscribe_not_applied"]
    assert matching, (
        "an ephemeral session's declared hook produced no "
        "mcp_hook_subscribe_not_applied audit-event — declared, never "
        "honored, never explained is the exact #5167 bug shape"
    )
    assert matching[0].data.get("server") == "srv"
    assert matching[0].data.get("uri") == _URI
    assert "ephemeral" in str(matching[0].data.get("reason", "")).lower()
