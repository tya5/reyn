"""Tier 2: #5129 — ``agui_submit`` resolves its ``agent_name`` from the
connection, not from its own URL path param (the 8th holder of "which agent
is this connection on" named in #5116's own count).

Owner-reported live bug (verbatim): "attach で切り替えできるようになったのは
良いけど、会話できないよ？メッセージ画面に会話更新されない。input messege は
enter で消えるだけ". #5116/#5118 fixed the SSE stream's own per-connection
"which agent" fact (``_SessionFrameSource.current_agent_name``) and made
``_ConnectionRetargetHub`` the connection-id-keyed channel that tells it about
a cross-agent ``/attach``. ``agui_submit`` is a SEPARATE HTTP request from
that SSE stream (correlated only by ``connection_id``) and, before this fix,
never consulted either — it decided every branch (heartbeat, ``exists``,
``ensure_running``, the ``TOOL_CALL_RESULT`` delegation, and every
client-names-an-operation branch below) off its OWN URL path param, which is
fixed to whatever agent the client's long-lived SSE URL originally named and
never updates. Input therefore kept reaching the connection's ORIGINAL agent
after a cross-agent ``/attach`` succeeded — "sent, but nothing renders",
exactly the owner's report (accepted by the ORIGINAL agent, not the one the
status bar/announce now claim).

Fix: ``_ConnectionRetargetHub`` gains a THIRD piece of state alongside its
existing listener/notify machinery — ``current_agent(connection_id)``,
seeded at ``subscribe`` time (so an untouched connection still resolves
correctly — #5116's own witness ④) and kept current at ``notify`` time (the
SAME call that already tells the SSE stream about a retarget).
``agui_submit`` resolves this ONE value once, at the top, and every
downstream branch reads it — never the raw path param again.

Chains the REAL wire pieces (real ASGI POST -> the real
``_ConnectionRetargetHub`` -> a real, started ``_SessionFrameSource`` ->
real ``Session.submit_user_text`` -> a real ``user_submitted`` audit-event),
mirroring ``test_5116_connection_agent_owner_cross_attach.py``'s own harness
and constructor calls. No mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource
from reyn.runtime.registry import AgentRegistry
from reyn.user_intervention import UserIntervention
from tests._async_wait import wait_until
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_CONNECTION_ID = "conn-5129-test"
_OTHER_CONNECTION_ID = "conn-5129-untouched"


def _two_agent_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create("coder-smith")
    return reg


def _build_app(reg, monkeypatch):
    from fastapi import FastAPI

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    return app


async def _post(app, path: str, payload: dict):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.post(path, json=payload)


def _collect_events(session) -> "list":
    collected: "list" = []
    session.subscribe_audit_events(lambda ev: collected.append(ev))
    return collected


@pytest.mark.asyncio
async def test_submit_after_cross_agent_attach_reaches_the_target_not_the_url_agent(
    tmp_path, monkeypatch,
):
    """Tier 2: #5129 witness — a ``user_message`` POSTed to the connection's
    ORIGINAL URL, after a real cross-agent ``attach_request`` on that same
    connection_id, must produce a ``user_submitted`` audit-event on the
    TARGET session — and NONE on the original ``default`` session (a
    one-sided check would pass on "arrives everywhere")."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    default_session = await reg.attach("default")
    app = _build_app(reg, monkeypatch)

    default_events = _collect_events(default_session)

    # The already-open stream: bound to "default", listening for a
    # cross-agent retarget under ITS OWN connection id — same server-side
    # state a --connect client's SSE GET leaves behind.
    source = _SessionFrameSource(default_session, registry=reg, agent_name="default")
    source.listen_for_retarget(_CONNECTION_ID)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "attach_request", "agent_name": "coder-smith"},
        )
        assert resp.json().get("attached") is True

        target_session = reg.get_session("coder-smith", "main")
        target_events = _collect_events(target_session)

        # The client's own SSE URL never changes — it keeps POSTing to
        # "default", exactly the owner's own live setup (attach switches,
        # the connect URL does not).
        submit_resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "user_message", "text": "hello coder-smith"},
        )
        assert submit_resp.status_code == 200
        assert submit_resp.json().get("status") == "ok"

        await asyncio.sleep(0)  # let the synchronous audit-event subscribers fire
    finally:
        source.close()

    target_submits = [e for e in target_events if getattr(e, "type", "") == "user_submitted"]
    default_submits = [e for e in default_events if getattr(e, "type", "") == "user_submitted"]

    assert target_submits, (
        "the target agent's own session never saw the submit -- input is "
        "still reaching the connection's ORIGINAL agent after attach"
    )
    assert target_submits[0].data.get("text") == "hello coder-smith"
    assert not default_submits, (
        f"the ORIGINAL agent's session ALSO saw a user_submitted event "
        f"({default_submits!r}) -- the submit is landing in two places, "
        f"not exclusively at the attached target"
    )


@pytest.mark.asyncio
async def test_a_connection_that_never_attached_still_submits_to_its_own_url_agent(
    tmp_path, monkeypatch,
):
    """Tier 2: #5116 witness ④'s own sibling — a connection that never sent
    ``attach_request`` (the hub has never seen its connection_id notified,
    only subscribed) must resolve to its OWN URL agent, not silently break.
    Pins the ``subscribe``-time seed: without it this connection would read
    as "unknown" and depend entirely on the URL fallback happening to be
    right, rather than the hub genuinely tracking it from the start."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    default_session = await reg.attach("default")
    app = _build_app(reg, monkeypatch)
    default_events = _collect_events(default_session)

    source = _SessionFrameSource(default_session, registry=reg, agent_name="default")
    source.listen_for_retarget(_OTHER_CONNECTION_ID)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/default?connection_id={_OTHER_CONNECTION_ID}&token=s3cret",
            {"type": "user_message", "text": "still default"},
        )
        assert resp.status_code == 200
        await asyncio.sleep(0)
    finally:
        source.close()

    default_submits = [e for e in default_events if getattr(e, "type", "") == "user_submitted"]
    assert default_submits and default_submits[0].data.get("text") == "still default"


@pytest.mark.asyncio
async def test_answer_after_cross_agent_attach_resolves_against_the_target_session(
    tmp_path, monkeypatch,
):
    """Tier 2: #5129 scope-widen (architect, issuecomment-5382969115,
    acceptance ⑥) — a ``TOOL_CALL_RESULT`` POST is delegated by ``agui_submit``
    to ``_handle_answer`` with whatever ``agent_name`` ``agui_submit`` itself
    resolved; since that resolution now reads the connection (this file's
    first test), a HITL answer POSTed to the connection's original URL after
    a cross-agent attach must resolve against the TARGET session's own
    pending intervention — the #5050/#5064 regression surface architect
    named."""
    reg = _two_agent_registry(tmp_path, monkeypatch)
    default_session = await reg.attach("default")
    app = _build_app(reg, monkeypatch)

    target_session = await reg.ensure_running("coder-smith")
    target_session.register_intervention_listener("tui")
    iv = UserIntervention(kind="ask_user", prompt="proceed?", run_id="r1")
    iv.future = asyncio.get_running_loop().create_future()
    iv_task = asyncio.ensure_future(target_session._dispatch_intervention(iv))
    await wait_until(lambda: bool(target_session.interventions.list_active()))

    source = _SessionFrameSource(default_session, registry=reg, agent_name="default")
    source.listen_for_retarget(_CONNECTION_ID)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "attach_request", "agent_name": "coder-smith"},
        )
        assert resp.json().get("attached") is True

        # Still POSTed to the connection's ORIGINAL URL ("default"), and
        # naming the pending intervention's real id — exactly what a real
        # remote client's answer_intervention_text round-trip sends.
        answer_resp = await _post(
            app, f"/agui/chat/default?connection_id={_CONNECTION_ID}&token=s3cret",
            {"type": "TOOL_CALL_RESULT", "toolCallId": iv.id, "text": "yes, proceed"},
        )
        assert answer_resp.json().get("answered") is True, answer_resp.json()

        answer = await asyncio.wait_for(iv_task, timeout=2.0)
        assert answer.text == "yes, proceed"
    finally:
        source.close()
