"""Tier 2: #2073 per-agent-hooks add-on — the per-agent hooks layer on the COMBINE.

The LAST #2073 piece (owner GO #2, additive). The hook registry is now a THREE-layer
additive COMBINE in order startup → runtime → per-agent:

- startup    — reyn.yaml hooks (OUT-set, captured at boot, never re-read);
- runtime    — global .reyn/hooks.yaml (IN-set, hot-reloadable);
- per-agent  — .reyn/agents/<name>/hooks.yaml (same IN-set grain, scoped per agent,
               read directly like the per-agent profile.yaml).

Boot resilience is now per-LAYER: the trusted startup layer must load (fail loud),
then each UNTRUSTED layer (runtime, per-agent) is try-added INDEPENDENTLY — a bad
runtime keeps startup ∪ per-agent; a bad per-agent keeps startup ∪ runtime. No single
bad untrusted layer crashes boot OR drops a good sibling layer.

No mocks: load_per_agent_hooks is a pure loader; the layered boot + reapply run on a
real Session, observed via the public inbox (E-hooks push there).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.loader import load_hot_reload_config, load_per_agent_hooks
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session

_AGENT = "pa-agent"
_HOOK = "hooks:\n  - on: turn_end\n    template_push:\n      message: {msg}\n      wake: true\n"
_STARTUP = [{"on": "turn_end", "template_push": {"message": "startup", "wake": True}}]


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return Session(
        agent_name=_AGENT,
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        hooks_config=hooks_config,
    )


def _write_runtime(tmp_path: Path, msg: str) -> None:
    (tmp_path / ".reyn").mkdir(exist_ok=True)
    (tmp_path / ".reyn" / "hooks.yaml").write_text(_HOOK.format(msg=msg), encoding="utf-8")


def _write_per_agent(tmp_path: Path, msg: str) -> Path:
    agent_dir = tmp_path / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "hooks.yaml"
    path.write_text(_HOOK.format(msg=msg), encoding="utf-8")
    return path


async def _drain_texts(session: Session) -> set:
    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    return texts


# ── the per-agent loader (read directly, not via the top-level IN-set) ──────


def test_load_per_agent_hooks_reads_scoped_file(tmp_path: Path) -> None:
    """Tier 2: load_per_agent_hooks reads .reyn/agents/<name>/hooks.yaml's hooks list."""
    _write_per_agent(tmp_path, "agent")
    hooks = load_per_agent_hooks(tmp_path, _AGENT)
    assert [h.get("on", h.get(True)) for h in hooks] == ["turn_end"]


def test_load_per_agent_hooks_absent_is_empty(tmp_path: Path) -> None:
    """Tier 2: an absent per-agent file (or dir) yields [] — a no-op layer, never error."""
    assert load_per_agent_hooks(tmp_path, _AGENT) == []


# ── the three-layer additive COMBINE (boot) ─────────────────────────────────


@pytest.mark.asyncio
async def test_boot_combines_all_three_layers_additively(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: at boot the dispatcher carries ALL THREE layers — reyn.yaml startup ∪
    global .reyn/hooks.yaml runtime ∪ per-agent .reyn/agents/<name>/hooks.yaml.
    Dispatching turn_end fires all three (additive; observed via the inbox)."""
    monkeypatch.chdir(tmp_path)
    _write_runtime(tmp_path, "runtime")
    _write_per_agent(tmp_path, "agent")
    session = _make_session(tmp_path, hooks_config=_STARTUP)
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "runtime", "agent"}


@pytest.mark.asyncio
async def test_boot_per_agent_only_no_runtime(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a per-agent layer fires even with no global runtime layer present
    (startup ∪ per-agent)."""
    monkeypatch.chdir(tmp_path)
    _write_per_agent(tmp_path, "agent")
    session = _make_session(tmp_path, hooks_config=_STARTUP)
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "agent"}


# ── the reapply seam re-reads the per-agent layer ───────────────────────────


@pytest.mark.asyncio
async def test_reapply_rereads_per_agent_layer(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the hooks reapply seam re-reads the per-agent .reyn/agents/<name>/hooks.yaml
    (alongside the global runtime layer) — a changed per-agent file reloads while the
    fixed reyn.yaml startup layer persists."""
    monkeypatch.chdir(tmp_path)
    _write_runtime(tmp_path, "runtime")
    _write_per_agent(tmp_path, "agent_v1")
    session = _make_session(tmp_path, hooks_config=_STARTUP)

    _write_per_agent(tmp_path, "agent_v2")  # operator/LLM rewrites the per-agent layer
    changed = await session._reapply_hooks(load_hot_reload_config(tmp_path))
    assert changed is True

    await session._hook_dispatcher.dispatch("turn_end", {})
    texts = await _drain_texts(session)
    assert texts == {"startup", "runtime", "agent_v2"}  # per-agent reloaded
    assert "agent_v1" not in texts                       # the old per-agent hook is gone


@pytest.mark.asyncio
async def test_reapply_handles_per_agent_removal(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: removing the per-agent file reloads back to startup ∪ runtime — removal
    is handled by the rebuild-from-scratch COMBINE."""
    monkeypatch.chdir(tmp_path)
    _write_runtime(tmp_path, "runtime")
    path = _write_per_agent(tmp_path, "agent")
    session = _make_session(tmp_path, hooks_config=_STARTUP)

    path.unlink()  # per-agent hooks removed
    await session._reapply_hooks(load_hot_reload_config(tmp_path))

    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "runtime"}  # per-agent gone


# ── per-LAYER independent boot resilience (the add-on refinement) ───────────


@pytest.mark.asyncio
async def test_bad_per_agent_keeps_startup_and_runtime(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a malformed per-agent layer degrades INDEPENDENTLY — it is dropped while
    startup ∪ runtime are kept (a bad per-agent file can't drop the good sibling layers
    or crash boot)."""
    monkeypatch.chdir(tmp_path)
    _write_runtime(tmp_path, "runtime")
    # malformed per-agent: a hook with no scheme → load_hooks raises
    agent_dir = tmp_path / ".reyn" / "agents" / _AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "hooks.yaml").write_text("hooks:\n  - on: turn_end\n", encoding="utf-8")

    session = _make_session(tmp_path, hooks_config=_STARTUP)  # must NOT raise
    await session._hook_dispatcher.dispatch("turn_end", {})
    assert await _drain_texts(session) == {"startup", "runtime"}  # good layers kept


@pytest.mark.asyncio
async def test_bad_runtime_keeps_startup_and_per_agent(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the independent-degrade refinement — a malformed RUNTIME layer is dropped
    while startup ∪ PER-AGENT are kept (pre-add-on the runtime failure degraded to
    startup-ONLY, dropping a good per-agent sibling; now each untrusted layer degrades
    on its own)."""
    monkeypatch.chdir(tmp_path)
    # malformed global runtime: a hook with no scheme → load_hooks raises
    (tmp_path / ".reyn").mkdir()
    (tmp_path / ".reyn" / "hooks.yaml").write_text("hooks:\n  - on: turn_end\n", encoding="utf-8")
    _write_per_agent(tmp_path, "agent")  # a GOOD per-agent layer

    session = _make_session(tmp_path, hooks_config=_STARTUP)  # must NOT raise
    await session._hook_dispatcher.dispatch("turn_end", {})
    texts = await _drain_texts(session)
    assert texts == {"startup", "agent"}  # the good per-agent sibling is NOT dropped
