"""Tier 2: #5146 — a purged agent's ``SurfaceManager`` does not survive a
same-name re-declare, and the process does not otherwise grow one manager
per URL-guessed name.

#5133 fixed cross-agent ``/attach`` to migrate a connection's own
``SurfaceManager`` registration. Reviewing it (architect, issuecomment-
5383185709) surfaced a SEPARATE finding: ``SurfaceRegistry._by_agent`` has
no removal path at all — measured, `for_agent` is only ever reached
through `agui_events`/`agui_submit`/`agui_seize`, all of which 404 first
via ``registry.exists(agent_name)``, so the manager COUNT is bounded by
declared-agent-count, not unbounded (architect's own correction to the
original finding's framing).

The real defect (architect ruling, issuecomment-5383365332): a **purge**
(``AgentRegistry.remove(name, purge=True)``) frees the agent name for
IMMEDIATE re-declaration, but the stale ``SurfaceManager`` — still keyed by
that same name, still holding the OLD identity's ``_active_driver`` token
and surface set — is found and reused by `for_agent` for the NEW identity's
first connection. The #5084-class name-reuse hole, this time for operator
authority instead of spawn lineage.

Fix: ``AgentRegistry.add_remove_listener`` (new, mirrors
``add_attach_listener``'s own idiom) fires on every ``remove(purge=True)``;
``AgentRegistry`` itself never imports or calls into ``interfaces``
(#5139's own layering ruling, pinned here by acceptance ④) — the AG-UI
endpoint module (which already depends on both) is what subscribes
``SurfaceRegistry.remove`` to it, exactly once per registry instance
(``_ensure_remove_listener_wired``).

4 acceptance witnesses (architect's own ruling):
1. purge -> same-name re-declare does NOT inherit the previous driver
   token (the main one this issue exists for).
2. a URL naming a NEVER-declared agent does not grow a manager (the
   already-bounded-by-`exists()` population, pinned so a future change
   can't silently remove that gate without a test noticing).
3. N connections to the SAME agent share exactly 1 manager (no
   accidental per-connection duplication).
4. ``src/reyn/runtime/registry.py`` does not import
   ``reyn.interfaces.transport.agui`` (the layering the fix deliberately
   keeps — grep, not an import-time assertion, so it holds even if the
   module is never imported in a given test run).

Chains the real wire pieces (real ASGI POST -> real ``AgentRegistry`` ->
real ``SurfaceRegistry``), mirroring
``test_5133_surface_migrates_on_cross_agent_attach.py``'s own harness. No
mocks.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.surface import monotonic, surface_registry
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


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

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
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


def _authorized(user_id) -> bool:
    return bool(user_id)


@pytest.mark.asyncio
async def test_purge_then_same_name_redeclare_does_not_inherit_the_old_driver_token(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ① — the main one. A connection holding the OLD
    identity's driver token must not leave the NEW identity's manager
    already claimed."""
    name = "5146-t1-agent"
    reg = _registry(tmp_path, monkeypatch)
    reg.create(name)
    app = _build_app(reg, monkeypatch)

    conn_old = "conn-old-driver"
    # A real POST (non-heartbeat) wires the purge-cleanup listener, exactly
    # as a live connection's own first non-heartbeat traffic would.
    resp = await _post(
        app, f"/agui/chat/{name}?connection_id={conn_old}&token=s3cret",
        {"type": "user_message", "text": "hello"},
    )
    assert resp.status_code == 200

    # The old connection is this (only) surface -> holds the driver token,
    # SurfaceManager.attach's own existing rule.
    mgr_old = surface_registry().for_agent(name, authorized=_authorized)
    mgr_old.attach(conn_old, "userOld", monotonic())
    assert mgr_old.is_active_driver(conn_old)

    reg.remove(name, purge=True)
    reg.create(name)  # same name, a genuinely NEW identity

    conn_new = "conn-new-arrival"
    mgr_new = surface_registry().for_agent(name, authorized=_authorized)
    assert mgr_new is not mgr_old, (
        "the purge listener did not replace the stale manager -- for_agent "
        "returned the SAME object for the new identity"
    )
    assert not mgr_new.is_active_driver(conn_old), (
        "the OLD connection's driver token survived the purge -- the new "
        "identity's first connection would find itself NOT the driver "
        "(silently deposed) or the old connection would silently retain "
        "authority over an agent it never attached to"
    )
    mgr_new.attach(conn_new, "userNew", monotonic())
    assert mgr_new.is_active_driver(conn_new), (
        "a genuinely fresh manager's first surface must take the driver "
        "token, same as any ordinary first connect"
    )


def test_a_raising_remove_listener_does_not_stop_the_others_or_remove_itself(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: architect co-vet (issuecomment-5383416113) — the notify loop
    isolates each callback. Before this fix, one listener raising would
    both skip every LATER listener AND make ``remove()`` itself raise,
    even though the purge's own ``shutil.rmtree`` (earlier in the same
    method) had already succeeded — a caller seeing an exception from
    ``remove()`` here would have no way to tell "the purge itself failed"
    from "a listener misbehaved after it already succeeded"."""
    name = "5146-t5-agent"
    reg = _registry(tmp_path, monkeypatch)
    reg.create(name)

    calls: "list[str]" = []

    def _raising_listener(_name: str) -> None:
        calls.append("raising")
        raise RuntimeError("a listener that misbehaves")

    def _second_listener(_name: str) -> None:
        calls.append("second")

    reg.add_remove_listener(_raising_listener)
    reg.add_remove_listener(_second_listener)

    reg.remove(name, purge=True)  # must not raise

    assert calls == ["raising", "second"], (
        f"the second listener must still run after the first raised; got "
        f"{calls!r}"
    )
    assert not (tmp_path / ".reyn" / "agents" / name).exists(), (
        "the purge itself (rmtree, BEFORE the listener loop) must have "
        "already completed regardless of a later listener's own failure"
    )


@pytest.mark.asyncio
async def test_a_never_declared_agent_name_never_grows_a_manager(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ② — the population is bounded by declared agents
    (the `registry.exists()` gate ahead of every `for_agent` call site),
    not unbounded. Pinned so a future change can't silently drop that gate
    without a test noticing (six questions ⑤'s answer for this file)."""
    name = "5146-t2-agent"
    never_declared = "5146-t2-never-declared"
    reg = _registry(tmp_path, monkeypatch)
    reg.create(name)
    app = _build_app(reg, monkeypatch)

    assert surface_registry().get(never_declared) is None

    resp = await _post(
        app, f"/agui/chat/{never_declared}?connection_id=conn-x&token=s3cret",
        {"type": "user_message", "text": "hello"},
    )
    assert resp.status_code == 404, resp.text

    assert surface_registry().get(never_declared) is None, (
        "a request naming a never-declared agent grew a SurfaceManager for "
        "it -- the exists() gate ahead of for_agent stopped doing its job"
    )


@pytest.mark.asyncio
async def test_n_connections_to_the_same_agent_share_exactly_one_manager(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ③ — no accidental per-connection duplication."""
    name = "5146-t3-agent"
    reg = _registry(tmp_path, monkeypatch)
    reg.create(name)
    app = _build_app(reg, monkeypatch)

    for conn_id in ("conn-p", "conn-q", "conn-r"):
        resp = await _post(
            app, f"/agui/chat/{name}?connection_id={conn_id}&token=s3cret",
            {"type": "user_message", "text": "hello"},
        )
        assert resp.status_code == 200

    mgr = surface_registry().for_agent(name, authorized=_authorized)
    for conn_id in ("conn-p", "conn-q", "conn-r"):
        mgr.attach(conn_id, f"user-{conn_id}", monotonic())
    same_mgr = surface_registry().for_agent(name, authorized=_authorized)
    assert same_mgr is mgr
    assert mgr.surface_count() == 3


def test_runtime_registry_does_not_import_the_agui_transport_module() -> None:
    """Tier 2: acceptance ④ — the layering #5146's fix deliberately keeps
    (architect: "AgentRegistry does not call into transport", #5139's own
    ruling reused here). Scoped to actual ``import`` statements (an ``ast``
    walk, not a bare substring search) so a docstring cross-reference to
    the AG-UI consumer class (legitimate, pre-existing prose elsewhere in
    this file) cannot make this test a false red -- only a real dependency
    would."""
    import ast

    from tests._support.paths import REPO_ROOT

    src = REPO_ROOT / "src" / "reyn" / "runtime" / "registry.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names
                if alias.name.startswith("reyn.interfaces")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("reyn.interfaces.transport.agui"):
                offenders.append(node.module)
    assert not offenders, (
        f"src/reyn/runtime/registry.py imports {offenders!r} -- AgentRegistry "
        f"must not import or call into the AG-UI transport layer; the "
        f"purge-notification seam (add_remove_listener) exists so transport "
        f"can subscribe FROM its own side instead"
    )
