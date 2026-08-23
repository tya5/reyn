"""Tier 2: #5119 (architect co-vet on #5118, issuecomment-5380501066, item
④) — an exception raised between ``source.listen_for_retarget(connection_id)``
and ``gen()``'s own ``try`` starting (Starlette does not begin pulling from
``gen()`` until AFTER ``agui_events`` returns) used to leave the
``_ConnectionRetargetHub`` subscription orphaned forever: no ``finally`` in
this window ever ran ``source.close()`` (that lives inside ``gen()``'s
``finally``, never reached).

Why this is worse than the pre-existing agent-keyed listener's own
equivalent exposure (``registry.add_attach_listener``, also called before
``gen()`` starts): that one is keyed by AGENT NAME — bounded by however many
agents exist. ``_ConnectionRetargetHub`` is keyed by ``connection_id`` — a
fresh value every connection, no natural ceiling. A leak here grows
unboundedly with connection churn, not agent count (the band's own
"who stops this if it repeats" question — #5119's answer, before this fix,
was nobody, ever).

Real ``AgentRegistry``/``Session`` + a real ``agui_events`` call throughout
— no mocks. The failure is injected via a real ``monkeypatch.setattr`` on
``session_backlog_page`` (module-level, in the endpoint module's own
namespace), not a fake collaborator standing in for a real one.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui import endpoint as endpoint_mod
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _make_get_request(query_string: bytes, app):
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/agui/chat/default/events",
        "query_string": query_string, "headers": [], "client": ("127.0.0.1", 12345),
        "app": app,
    }
    return Request(scope)


def _registry(tmp_path, monkeypatch):
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

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    return reg


def _build_app(reg, monkeypatch):
    from fastapi import FastAPI

    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    return app


@pytest.mark.asyncio
async def test_exception_before_gen_starts_does_not_leak_the_hub_subscription(
    tmp_path, monkeypatch,
):
    """Tier 2: #5119's own witness — an exception raised AFTER ``listen_
    for_retarget`` but BEFORE ``gen()`` starts must not leave the
    connection's own hub subscription behind.

    Injects the failure at the LAST call in the vulnerable window
    (``session_backlog_page``, called to build ``AgUiEmitter``'s own
    ``backlog=`` argument) — the latest possible failure point, so this
    witness covers the WHOLE window, not just an early step.

    Strip-falsifier: reverting the ``try``/``except`` wrapper around this
    window back to unguarded calls (this PR's own diff) turns this red —
    the hub retains the ``conn_id`` key with the leaked listener still
    registered. Verified locally."""
    reg = _registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)
    conn_id = "conn-5119-leak-test"

    def _raising_backlog(*_args, **_kwargs):
        raise RuntimeError("deliberate #5119 injected failure")

    # #5139 C: the endpoint's own initial-backlog call site now goes through
    # ``session_backlog_page`` (bounded, page-aware), not the unbounded
    # ``session_backlog_frames`` this test used to target — the injection
    # point moves to match, still the LAST call in the vulnerable window.
    monkeypatch.setattr(endpoint_mod, "session_backlog_page", _raising_backlog)

    assert not endpoint_mod.connection_retarget_has_subscribers(conn_id), (
        "test precondition: the hub must start with no entry for this id"
    )

    req = _make_get_request(f"token=s3cret&connection_id={conn_id}".encode(), app)
    with pytest.raises(RuntimeError, match="deliberate #5119 injected failure"):
        await endpoint_mod.agui_events(req, "default")

    assert not endpoint_mod.connection_retarget_has_subscribers(conn_id), (
        f"the hub subscription for {conn_id!r} leaked past the exception"
    )


@pytest.mark.asyncio
async def test_the_success_path_still_subscribes_normally(tmp_path, monkeypatch):
    """Tier 2: regression guard — the fix must not accidentally close
    ``source`` (and thus unsubscribe) on the SUCCESS path too. A real,
    non-failing GET must leave the hub entry PRESENT (the connection is
    still open and listening) until ``source.close()`` is explicitly
    called later (mirrors #5116's own tests, which already exercise that
    close path via ``source.close()`` in their own ``finally``)."""
    reg = _registry(tmp_path, monkeypatch)
    app = _build_app(reg, monkeypatch)
    conn_id = "conn-5119-success-test"

    req = _make_get_request(f"token=s3cret&connection_id={conn_id}".encode(), app)
    resp = await endpoint_mod.agui_events(req, "default")
    from starlette.responses import StreamingResponse

    assert isinstance(resp, StreamingResponse)
    assert endpoint_mod.connection_retarget_has_subscribers(conn_id), (
        "a successfully-opened connection must still be subscribed"
    )

    # No manual cleanup: this test never iterates ``resp.body_iterator``, so
    # ``gen()``'s own ``finally`` (which calls ``source.close()`` and
    # unsubscribes the hub entry) never runs — by design, since driving the
    # generator is #5116's own concern, not this test's. A leftover
    # single-entry subscription under a per-test ``conn_id`` in the
    # module-level ``_CONNECTION_RETARGET_HUB`` singleton is harmless: no
    # other test reuses this id, and the entry holds no resources beyond a
    # closure reference. ``has_subscribers`` has no method to force-clear an
    # id without a callback reference (deliberately — that reference only
    # ever legitimately comes from ``source.close()``'s own unsubscribe).
