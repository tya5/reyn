"""Tier 2: #5237 — A2A/web (`deps.get_registry`) actually delivers a
declared reyn.yaml `hooks:` entry to a real Session.

#5228 restructured `hooks_config`/`composers_config`/`fs_watch_config`
from flat kwargs into `SessionFactoryConfig.reactivity_config`
(`build_scoped_chat_session` now reads `factory_config.reactivity_config`
directly). #5228's own accept② ("hooks actually reach an A2A/web session")
had 0 witnesses when #5234 merged it — #5234's own test covered a
DIFFERENT invariant (`Session(reactivity=None)` raising `TypeError`), not
this one. This is the missing witness.

Drives the REAL production path — `deps.get_registry()` (the exact
callable `Depends(get_registry)` resolves to in every A2A/web router) →
`AgentRegistry.resolve_session` (the real routing-key get-or-spawn
primitive, same one `a2a.py`'s handlers call) — never a hand-built
Session or a test-owned session factory. `resolve_session` spawns via the
registry's real `session_factory` closure (`deps.py`'s `_session_factory`,
unchanged, un-mocked).

Observation: `_hook_dispatcher` is reached only to CALL its own public
``dispatch()`` — never read as state inside an assertion — mirroring
``test_5222_hook_state_origin_aware.py``'s own established idiom
("Corroborate against the REAL dispatch outcome"). The actual assertion
is on ``session.inbox`` (a public ``asyncio.Queue``), which is what a
``wake: true`` push production-fires into — proof the hook actually
FIRES, not merely that a `HookDef` object sits in a list somewhere.
"""
from __future__ import annotations

import pytest

from reyn.hooks.schema_registry import build_hook_payload
from reyn.interfaces.web import deps
from reyn.interfaces.web.deps import CliScopedOverrides, cli_scoped_overrides
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_HOOK_YAML = (
    MINIMAL_REYN_YAML
    + "hooks:\n"
    "  - \"on\": session_start\n"
    "    template_push:\n"
    "      message: \"witness hook fired\"\n"
    "      wake: true\n"
)


def _real_web_session(tmp_path, monkeypatch, *, sid: str):
    """Spawn a Session through the REAL A2A/web construction path —
    `deps.get_registry()` (what every router's `Depends(get_registry)`
    resolves to) then `AgentRegistry.resolve_session` (the same
    routing-key get-or-spawn primitive `a2a.py`'s handlers call)."""
    monkeypatch.chdir(tmp_path)
    with cli_scoped_overrides(CliScopedOverrides()):
        registry = deps.get_registry()
        return registry.resolve_session("default", "web", sid)


@pytest.mark.asyncio
async def test_declared_startup_hook_actually_fires_on_a_real_a2a_web_session(
    tmp_path, monkeypatch,
):
    """Tier 2: #5228 accept② — a `hooks:` entry declared in reyn.yaml
    actually FIRES on a Session spawned via `deps.get_registry()`. Not
    "construction succeeded" (#5228 already had that — #5234's own test)
    — the declared hook's push actually lands in the session's inbox."""
    (tmp_path / "reyn.yaml").write_text(_HOOK_YAML, encoding="utf-8")

    session = _real_web_session(tmp_path, monkeypatch, sid="witness-declared")

    await session._hook_dispatcher.dispatch(
        "session_start", build_hook_payload("session_start", agent_name=session.agent_name),
    )
    assert session.inbox.qsize() >= 1, (
        "the reyn.yaml session_start hook did not fire on the real A2A/web "
        "session — its inbox is empty"
    )


@pytest.mark.asyncio
async def test_no_hooks_block_yields_no_dispatch_at_all(tmp_path, monkeypatch):
    """Tier 2: differential half — an A2A/web session with NO declared
    hooks dispatches to nothing (a genuinely empty inbox, not a
    construction failure silently swallowed into a look-alike no-op).
    Same construction path, same dispatch call, only the config differs —
    proves the positive test above is reading real config-sensitivity,
    not a fixture artifact that would fire either way."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")

    session = _real_web_session(tmp_path, monkeypatch, sid="witness-empty")

    await session._hook_dispatcher.dispatch(
        "session_start", build_hook_payload("session_start", agent_name=session.agent_name),
    )
    assert session.inbox.qsize() == 0
