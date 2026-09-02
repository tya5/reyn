"""Tier 2: #5276② — ``Session.hook_state()``'s reactive cache.

Root cause: this method used to re-walk ``self._hook_defs_by_name()`` (a
full merged-registry scan) on EVERY call, including every render frame the
status panel drew, for an answer that only changes on a toggle
(``set_hook_enabled``) or a hook-declaration hot-reload (``_reapply_hooks``).

Genuine finding caught mid-implementation (a real, pre-existing test
failure, ``test_hook_slash_disables_via_public_state``): an EventLog-
subscriber-based invalidation (the shape ``mcp_subscription_state`` uses)
is UNSAFE here, because ``set_hook_enabled`` is a plain SYNCHRONOUS method
and an existing caller toggles then reads ``hook_state()`` immediately,
with no intervening ``await`` — but ``EventLog.emit()`` QUEUES subscriber
dispatch onto a background consumer task whenever a loop is running
(#4966), so the invalidating subscriber would not have run yet by the time
such a caller reads the (still-stale) cache. Fix: bump a generation
SYNCHRONOUSLY, directly at each mutation site (``set_hook_enabled``'s
applied path, ``load_persisted_toggles`` — both bump ``Session``'s own
``_hook_toggle_generation``; ``_reapply_hooks`` bumps ``HookDispatcher``'s
own ``generation`` inside ``replace_registry`` — #5287) — no subscriber
for this cache at all, and (#5287) no explicit invalidation CALL either:
``hook_state()`` compares the live 2-part generation to its own
last-seen value on every read instead.

Real ``Session``/``HookDispatcher`` — no mocks, mirrors
``test_5222_hook_state_origin_aware.py``'s own real-seam pattern. Uses the
#4403 counting technique (wrap ``_hook_defs_by_name``, count real calls).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_STARTUP_HOOK_NAME = "project-supervision-hook"
_STARTUP_HOOKS = [
    {
        "on": "turn_end",
        "name": _STARTUP_HOOK_NAME,
        "template_push": {"message": "startup fired", "wake": True},
    },
]


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "alice" / "state" / "snapshot.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


def _write_agent_hook(tmp_path: Path, name: str) -> None:
    """Mirrors ``test_hook_applicability_2285.py``'s own helper — a
    per-agent-origin hook (freely disableable, unlike the startup/runtime
    origins #5230 protects), written BEFORE the session that reads it is
    constructed."""
    p = tmp_path / ".reyn" / "agents" / "alice" / "hooks.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({"hooks": [
            {"on": "turn_end", "name": name, "template_push": {"message": "ping", "wake": True}},
        ]}),
        encoding="utf-8",
    )


def _counting_wrapper(monkeypatch, session: Session) -> dict:
    """Mirrors #4403's own counting technique — counts real
    ``_hook_defs_by_name`` calls (the merged-registry walk ``hook_state``
    would otherwise re-run every call) from this point on."""
    real_fn = session._hook_defs_by_name
    call_count = {"n": 0}

    def _counting():
        call_count["n"] += 1
        return real_fn()

    monkeypatch.setattr(session, "_hook_defs_by_name", _counting)
    return call_count


def test_repeated_reads_cost_one_real_walk(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — 3 repeated ``hook_state()`` reads with no
    intervening toggle cost exactly 1 real registry walk, not 3."""
    s = _make_session(tmp_path)
    call_count = _counting_wrapper(monkeypatch, s)

    r1 = s.hook_state()
    r2 = s.hook_state()
    r3 = s.hook_state()

    assert call_count["n"] == 1, (
        f"expected exactly 1 real registry walk across 3 reads with no "
        f"intervening toggle, got {call_count['n']}"
    )
    assert r1 == r2 == r3


@pytest.mark.asyncio
async def test_a_synchronous_toggle_then_read_sees_the_fresh_value_immediately(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance — the exact #5276② witness. A caller that toggles
    a hook then reads ``hook_state()`` IMMEDIATELY, with no ``await`` in
    between, must see the fresh value (this is a SYNCHRONOUS method calling
    another SYNCHRONOUS method — no async boundary crossed at all), proving
    the cache is invalidated directly at the mutation site rather than via
    a queued event subscriber (which would still show the stale value
    here — this is precisely the regression the initial subscriber-based
    draft introduced and this test would have caught)."""
    _write_agent_hook(tmp_path, "myhook")  # per-agent origin — freely disableable
    s = _make_session(tmp_path)

    # Prime the cache with the pre-toggle (enabled) state.
    before = {h["name"]: h["enabled"] for h in s.hook_state()}
    assert before.get("myhook") is True

    s.set_hook_enabled("myhook", False)
    after_off = {h["name"]: h["enabled"] for h in s.hook_state()}
    assert after_off.get("myhook") is False, (
        "hook_state() read immediately after a synchronous toggle, with no "
        "await in between, must reflect it — got a stale/cached value"
    )

    s.set_hook_enabled("myhook", True)
    after_on = {h["name"]: h["enabled"] for h in s.hook_state()}
    assert after_on.get("myhook") is True, (
        "a second synchronous toggle-then-read must ALSO see the fresh "
        "value, not a value cached from before EITHER toggle"
    )


@pytest.mark.asyncio
async def test_a_refused_toggle_does_not_invalidate_unnecessarily(tmp_path, monkeypatch) -> None:
    """Tier 2: falsification contrast — a REFUSED disable (#5230, a
    protected startup/runtime-origin hook) changes no state
    (``_disabled_hooks`` untouched), so it must NOT invalidate the cache —
    a subsequent ``hook_state()`` read costs 0 additional registry walks.

    ``set_hook_enabled`` itself calls the SAME ``_hook_defs_by_name`` this
    test wraps (to resolve the hook's own origin, independent of
    ``hook_state``'s cache) — so the expected count includes that 1 call
    too. What this test actually isolates is the SECOND ``hook_state()``
    call below costing 0 MORE: if the refusal had incorrectly invalidated
    the cache, that call would add a 3rd, not stay at 2."""
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    call_count = _counting_wrapper(monkeypatch, s)

    s.hook_state()  # 1 real walk, fills the cache
    assert call_count["n"] == 1

    result = s.set_hook_enabled(_STARTUP_HOOK_NAME, False)  # +1 (its own resolve, not hook_state's cache)
    assert result.applied is False
    assert call_count["n"] == 2

    s.hook_state()
    assert call_count["n"] == 2, (
        f"a refused (no-op) toggle must not invalidate hook_state()'s "
        f"cache — expected 0 additional walks on this read, got "
        f"{call_count['n'] - 2} more (total {call_count['n']})"
    )


@pytest.mark.asyncio
async def test_a_hook_declaration_reload_invalidates_the_cache(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — ``_reapply_hooks`` (the hot-reload seam that
    swaps the LIVE dispatcher registry — #5278 does NOT apply here, unlike
    cron_jobs/mcp_servers/skills) invalidates the cache, so a NEWLY declared
    hook is visible on the very next read, with no intervening await needed
    beyond awaiting ``_reapply_hooks`` itself."""
    s = _make_session(tmp_path)
    before = {h["name"] for h in s.hook_state()}
    assert "reloaded-hook" not in before

    await s._reapply_hooks({
        "hooks": [{"on": "turn_end", "name": "reloaded-hook", "template_push": {"message": "x"}}],
    })

    after = {h["name"] for h in s.hook_state()}
    assert "reloaded-hook" in after


def test_load_persisted_toggles_invalidates_the_cache(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — the 3rd invalidation site (architect B on #5284:
    only 2 of 3 sites had a witness). Priming ``hook_state()``'s cache,
    THEN persisting a disabled-set directly to the per-session state dir's
    ``hooks.yaml`` (bypassing ``set_hook_enabled`` entirely — the exact
    restart-recovery shape ``load_persisted_toggles`` exists for), THEN
    calling ``load_persisted_toggles()`` must make the NEXT ``hook_state()``
    read reflect the persisted disable — proving this site's own
    ``self._hook_toggle_generation += 1`` line (#5287; was ``self.
    _cached_hook_items = None``) is load-bearing (removing it reproduces
    this test going red with no other change)."""
    import yaml

    _write_agent_hook(tmp_path, "myhook")
    s = _make_session(tmp_path)

    # Prime the cache with the pre-restore (enabled) state.
    before = {h["name"]: h["enabled"] for h in s.hook_state()}
    assert before.get("myhook") is True

    # Persist a disabled-set directly (mirrors a prior session's own
    # _persist_hook_disabled write, restored on a fresh instance) —
    # bypasses set_hook_enabled's own synchronous invalidation entirely.
    state_dir = Path(s._snapshot_path).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "hooks.yaml").write_text(
        yaml.safe_dump({"disabled": ["myhook"]}), encoding="utf-8",
    )

    s.load_persisted_toggles()
    after = {h["name"]: h["enabled"] for h in s.hook_state()}
    assert after.get("myhook") is False, (
        "hook_state() read after load_persisted_toggles() must reflect the "
        "just-restored disabled-set, not the primed pre-restore cache"
    )
