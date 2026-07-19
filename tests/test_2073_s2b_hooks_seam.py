"""Tier 2: #2073 S2b — global hooks reapply seam (.reyn/hooks.yaml, layered).

S2b makes hooks hot-reloadable via a LAYERED model: the reyn.yaml startup layer
(OUT-set, captured once at boot, never re-read) ∪ the .reyn/hooks.yaml runtime layer
(IN-set, hot-reloadable; the LLM-op writes it in S3). The dispatcher reads its
registry fresh per dispatch, so HookDispatcher.replace_registry(startup ∪ re-read-
runtime) swaps the live hooks at the turn boundary, preserving the startup layer.

No mocks: validate is a pure function; replace_registry is exercised through a real
HookDispatcher; the layered boot + reapply run on a real Session, observed via the
public inbox (E-hooks push there).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.loader import load_hooks
from reyn.runtime.hot_reload import validate_in_set
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_HOOK = "hooks:\n  - on: turn_end\n    template_push:\n      message: {msg}\n      wake: true\n"


# ── validate-before-apply: the hooks-shape check (atomic reject) ────────────


def test_validate_accepts_good_hooks() -> None:
    """Tier 2: a well-formed runtime hooks list validates (None)."""
    assert validate_in_set(
        {"hooks": [{"on": "turn_end", "template_push": {"message": "hi"}}]}
    ) is None


def test_validate_rejects_bad_hooks() -> None:
    """Tier 2: a malformed .reyn/hooks.yaml (no scheme) is rejected via the real
    loader → the whole reload is rejected atomically."""
    reason = validate_in_set({"hooks": [{"on": "turn_end"}]})  # missing the scheme
    assert reason is not None and "hooks:" in reason


# ── HookDispatcher.replace_registry swaps the live registry ─────────────────


@pytest.mark.asyncio
async def test_replace_registry_swaps_live() -> None:
    """Tier 2: replace_registry swaps the registry the dispatcher reads on the NEXT
    dispatch (no re-threading) — the new hooks fire, the old ones don't."""
    pushed: list = []

    async def put_inbox(kind, payload):
        pushed.append(payload["text"])

    async def stage(kind, payload):
        pass

    disp = HookDispatcher(
        load_hooks([{"on": "turn_end", "template_push": {"message": "old", "wake": True}}]),
        put_inbox=put_inbox, stage_next_turn_context=stage,
    )
    await disp.dispatch("turn_end", {})
    assert pushed == ["old"]

    disp.replace_registry(
        load_hooks([{"on": "turn_end", "template_push": {"message": "new", "wake": True}}])
    )
    await disp.dispatch("turn_end", {})
    assert pushed == ["old", "new"]  # the swap took effect on the next dispatch


# ── layered boot + reapply on a real Session (observed via the inbox) ───────


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name="s2b-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


async def _drain_texts(session: Session) -> set:
    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    return texts


@pytest.mark.asyncio
async def test_boot_layers_startup_and_runtime(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: at boot the dispatcher carries BOTH the reyn.yaml startup hook AND the
    .reyn/hooks.yaml runtime hook (active from session start, mirroring .reyn/mcp.yaml).
    Dispatching turn_end fires both (observed via the inbox)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(_HOOK.format(msg="runtime"), encoding="utf-8")
    session = _make_session(
        tmp_path,
        hooks_config=[{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}],
    )
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "runtime"}


@pytest.mark.asyncio
async def test_reapply_recombines_runtime_preserving_startup(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the hooks reapply seam re-reads ONLY the runtime layer + re-combines
    with the FIXED startup — a changed .reyn/hooks.yaml reloads while the reyn.yaml
    startup hook persists (the safety boundary: reyn.yaml is never re-read)."""
    from reyn.config.loader import load_hot_reload_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(_HOOK.format(msg="runtime_v1"), encoding="utf-8")
    session = _make_session(
        tmp_path,
        hooks_config=[{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}],
    )

    # operator/LLM-op rewrites the runtime layer; reapply at the boundary
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(_HOOK.format(msg="runtime_v2"), encoding="utf-8")
    changed = await session._reapply_hooks(load_hot_reload_config(tmp_path))
    assert changed is True

    await session._hook_dispatcher.dispatch("turn_end", {})
    texts = await _drain_texts(session)
    assert texts == {"startup", "runtime_v2"}  # startup preserved, runtime reloaded
    assert "runtime_v1" not in texts            # the old runtime hook is gone


@pytest.mark.asyncio
async def test_reapply_handles_runtime_removal(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: removing all runtime hooks (.reyn/hooks.yaml emptied) reloads back to
    startup-only — removal is handled by the rebuild-from-scratch (unlike cron's
    add-only seam)."""
    from reyn.config.loader import load_hot_reload_config

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(_HOOK.format(msg="runtime"), encoding="utf-8")
    session = _make_session(
        tmp_path,
        hooks_config=[{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}],
    )

    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text("hooks: []\n", encoding="utf-8")  # runtime removed
    await session._reapply_hooks(load_hot_reload_config(tmp_path))

    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup"}  # back to startup-only


@pytest.mark.asyncio
async def test_boot_resilience_malformed_runtime_degrades_to_startup(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: BOOT resilience — a malformed .reyn/hooks.yaml (e.g. one the S3 LLM-op
    wrote, rejected on reload but PERSISTED) must NOT crash Session construction. The
    boot degrades to the reyn.yaml startup hooks only (a loud warning), so the agent
    can't brick its own boot by writing a bad runtime layer."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".reyn" / "config").mkdir(parents=True)
    # malformed: a hook with no scheme (template_push/shell_exec/shell_push) → load_hooks raises
    (tmp_path / ".reyn" / "config" / "hooks.yaml").write_text(
        "hooks:\n  - on: turn_end\n", encoding="utf-8",
    )
    # construction must NOT raise (the bug was: unguarded boot load_hooks crashes here)
    session = _make_session(
        tmp_path,
        hooks_config=[{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}],
    )
    # booted startup-only: dispatch fires the startup hook, not the (skipped) runtime
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup"}
