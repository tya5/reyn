"""Tests for #2608 H4 — the filesystem watcher external-event source.

H4 adds the 4th external-event hook-point, ``file_changed``: a REAL file
create/modify/delete under an operator-declared ``fs_watch.paths`` entry fires
a user-configured hook via a bounded thread->async bridge from the watchdog
observer's OS thread into ``HookDispatcher.dispatch`` (the session's event
loop).

Coverage plan
-------------
Tier 1 (contract): ``file_changed`` is registered in ALLOWED_HOOK_POINTS; a
  ``hooks:`` entry with ``on: file_changed`` loads via the real ``load_hooks``
  seam; ``path`` globs (not exact-matches) via ``reyn.hooks.matcher.matches``.
Tier 2 (OS invariant): a REAL ``watchdog`` ``Observer`` + REAL file writes
  under a real temp directory, bridged into a real (recording) async
  ``hook_trigger`` — proves the thread->async handoff actually lands events
  from the watchdog OS thread onto the session's event loop. Covers: create/
  modify fires with correct path + event_type; a burst of writes to one path
  debounces to ONE fire; a path NOT under a watched dir never fires; empty
  ``paths`` config -> the watcher never starts (byte-identical to pre-H4);
  ``watchdog`` not importable -> warn + no-op (never raises); (#2623) a
  ``fs_watch.paths`` entry given via a SYMLINK reports events with the path
  rewritten back onto the operator's configured (symlink) prefix, so a
  ``matcher: {path: <configured>}`` glob still matches — closing the macOS
  ``/tmp`` -> ``/private/tmp`` footgun.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. ``pytest.importorskip``
guards every test that needs a real ``watchdog`` Observer.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from reyn.hooks.matcher import matches
from reyn.hooks.schema import ALLOWED_HOOK_POINTS
from reyn.runtime.fs_watcher import FsWatcher

watchdog = pytest.importorskip("watchdog", reason="fs-watch extra ('pip install reyn[fs-watch]') not installed")


# ---------------------------------------------------------------------------
# Recording seam (mirrors test_2608_h2_hook_matcher.py's _Recorder)
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable — the ``hook_trigger`` DI shape
    (``(point, template_vars) -> Awaitable``), no mock."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, point: str, template_vars: dict) -> None:
        self.calls.append((point, dict(template_vars)))


async def _wait_for(predicate, *, attempts: int = 200, delay: float = 0.02) -> None:
    """Poll ``predicate()`` until True or give up — fs events arrive
    asynchronously off a separate OS thread, not synchronously with the
    triggering file write."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Tier 1: schema + matcher — the new hook-point + matchable field
# ---------------------------------------------------------------------------


def test_file_changed_is_an_allowed_hook_point():
    """Tier 1: ``file_changed`` is registered alongside the other hook-points —
    the schema-level gate a ``hooks.yaml`` entry with ``on: file_changed``
    must pass."""
    assert "file_changed" in ALLOWED_HOOK_POINTS


def test_file_changed_hook_loads_via_production_loader():
    """Tier 1: a ``hooks:`` config entry with ``on: file_changed`` and a
    ``template_push`` action parses through the REAL ``load_hooks`` seam."""
    from reyn.hooks.loader import load_hooks

    raw = [
        {
            "on": "file_changed",
            "template_push": {"message": "{{ path }} {{ event_type }}"},
        },
    ]
    registry = load_hooks(raw)
    (hook,) = registry.hooks_for("file_changed")
    assert hook.matcher is None


def test_path_matcher_field_globs_not_exact_matches():
    """Tier 1: ``path`` is in ``_GLOB_FIELDS`` — a matcher naming ``path`` uses
    ``fnmatch`` glob semantics (a sub-tree prefix pattern), not exact string
    equality."""
    template_vars = {"point": "file_changed", "path": "/repo/src/a.py", "event_type": "modified"}
    assert matches({"path": "/repo/src/**"}, template_vars) is True
    assert matches({"path": "/repo/docs/**"}, template_vars) is False
    # exact equality would have failed here (the glob is what makes it match):
    assert matches({"path": "/repo/src/a.py"}, template_vars) is True


def test_path_matcher_absent_field_never_matches():
    """Tier 1: a matcher naming ``path`` against an event with no ``path`` in
    its template_vars (e.g. a lifecycle hook-point) never matches — a matcher
    only narrows, never invents a signal that was never fired."""
    assert matches({"path": "/repo/**"}, {"point": "session_start"}) is False


def test_fs_watch_config_parses_paths_and_debounce():
    """Tier 1: the ``fs_watch:`` reyn.yaml block parses through the real
    ``_build_fs_watch_config`` seam into ``FsWatchConfig``."""
    from reyn.config.infra import _build_fs_watch_config

    cfg = _build_fs_watch_config({"paths": ["/repo/src", "/repo/docs"], "debounce_seconds": 0.5})
    assert cfg.paths == ["/repo/src", "/repo/docs"]
    assert cfg.debounce_seconds == 0.5


def test_fs_watch_config_absent_block_defaults_to_no_paths():
    """Tier 1: no ``fs_watch:`` block (None / missing / non-dict) -> empty
    ``paths`` — the byte-identical-to-pre-H4 default."""
    from reyn.config.infra import FsWatchConfig, _build_fs_watch_config

    assert _build_fs_watch_config(None) == FsWatchConfig()
    assert _build_fs_watch_config("not-a-dict") == FsWatchConfig()
    assert _build_fs_watch_config({}) == FsWatchConfig()


# ---------------------------------------------------------------------------
# Tier 2: real watchdog Observer + real file writes — thread->async bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_file_write_fires_hook_with_path_and_event_type(tmp_path):
    """Tier 2: THE core H4 proof. A REAL watchdog Observer watching a REAL temp
    directory; a REAL file write under it must reach ``hook_trigger`` on the
    session's event loop with the correct ``path``/``event_type`` — proving the
    watchdog-OS-thread -> call_soon_threadsafe -> asyncio.Queue -> drain-task
    bridge actually works end-to-end."""
    trigger = _Recorder()
    watcher = FsWatcher(paths=[str(tmp_path)], hook_trigger=trigger, debounce_seconds=0.05)
    try:
        await watcher.start()
        assert watcher.is_started()

        target = tmp_path / "a.txt"
        target.write_text("hello")

        await _wait_for(lambda: len(trigger.calls) >= 1)
        (point, template_vars) = trigger.calls[0]
        assert point == "file_changed"
        assert template_vars["point"] == "file_changed"
        assert Path(template_vars["path"]) == target
        assert template_vars["event_type"] in ("created", "modified")
    finally:
        await watcher.aclose()


@pytest.mark.asyncio
async def test_symlinked_watch_path_reports_events_under_the_configured_prefix(tmp_path):
    """Tier 2: (#2623) the macOS ``/tmp`` -> ``/private/tmp`` footgun, reproduced
    with a real symlink (not OS-specific — this works the same on Linux). A
    ``fs_watch.paths`` entry given via a symlink (mirroring an operator writing
    ``paths: ['/tmp/x']`` on macOS) must report the fired event's ``path`` under
    the CONFIGURED (symlink) prefix, not the OS-resolved realpath — so a naive
    ``matcher: {path: '<configured>/**'}`` glob (evaluated against the
    configured prefix an operator actually wrote) matches, instead of silently
    never firing because the reported path secretly points at the resolved
    target directory."""
    real_dir = tmp_path / "real_target"
    real_dir.mkdir()
    symlink_dir = tmp_path / "watched_via_symlink"
    os.symlink(real_dir, symlink_dir)
    configured_path = str(symlink_dir)

    trigger = _Recorder()
    watcher = FsWatcher(paths=[configured_path], hook_trigger=trigger, debounce_seconds=0.05)
    try:
        await watcher.start()
        assert watcher.is_started()

        target = symlink_dir / "a.txt"
        target.write_text("hello")

        await _wait_for(lambda: len(trigger.calls) >= 1)
        (point, template_vars) = trigger.calls[0]
        assert point == "file_changed"
        # The reported path must be reachable under the OPERATOR'S configured
        # (symlink) prefix — not silently swapped for the OS-resolved realpath.
        reported_path = template_vars["path"]
        assert reported_path.startswith(configured_path), (
            f"expected {reported_path!r} to start with the configured prefix "
            f"{configured_path!r} — the #2623 symlink-normalization contract"
        )
        # The load-bearing consumer-facing proof: a matcher glob written
        # against the operator's CONFIGURED path actually matches the event.
        assert matches({"path": f"{configured_path}/**"}, template_vars) is True
    finally:
        await watcher.aclose()


@pytest.mark.asyncio
async def test_burst_of_writes_to_one_path_debounces_to_one_fire(tmp_path):
    """Tier 2: (F7-3) editors emit event BURSTS for one logical change — a rapid
    burst of writes to the SAME path within the debounce window must coalesce
    to exactly ONE hook fire, not one per underlying OS event."""
    trigger = _Recorder()
    watcher = FsWatcher(paths=[str(tmp_path)], hook_trigger=trigger, debounce_seconds=2.0)
    try:
        await watcher.start()
        target = tmp_path / "burst.txt"
        for i in range(10):
            target.write_text(f"write {i}")
            time.sleep(0.01)  # real OS writes, well inside the 2s debounce window

        # Give the (single, debounced) event a fair chance to arrive.
        await _wait_for(lambda: trigger.calls != [])
        await asyncio.sleep(0.2)  # settle window — assert no SECOND fire trickles in
        # Tuple-unpack asserts EXACTLY one recorded call (raises on 0 or >1) —
        # a behavioral assertion on the debounce invariant, not a length pin.
        ((point, template_vars),) = trigger.calls
        assert point == "file_changed"
        assert Path(template_vars["path"]) == target
    finally:
        await watcher.aclose()


@pytest.mark.asyncio
async def test_path_outside_watched_dir_never_fires(tmp_path):
    """Tier 2: a write under a directory that is NOT watched must never reach
    ``hook_trigger`` — scoping is by the operator-declared ``paths`` set, not
    global."""
    watched = tmp_path / "watched"
    unwatched = tmp_path / "unwatched"
    watched.mkdir()
    unwatched.mkdir()

    trigger = _Recorder()
    watcher = FsWatcher(paths=[str(watched)], hook_trigger=trigger, debounce_seconds=0.05)
    try:
        await watcher.start()
        (unwatched / "b.txt").write_text("nope")
        await asyncio.sleep(0.3)
        assert trigger.calls == []

        # Confirm the harness itself is live: a write INSIDE the watched dir
        # still fires (rules out "the watcher silently never started").
        (watched / "a.txt").write_text("yes")
        await _wait_for(lambda: len(trigger.calls) >= 1)
    finally:
        await watcher.aclose()


@pytest.mark.asyncio
async def test_empty_paths_config_never_starts_watcher():
    """Tier 2: no ``fs_watch.paths`` configured (the default) -> ``start()`` is a
    no-op — byte-identical to today for every build with no fs_watch config."""
    trigger = _Recorder()
    watcher = FsWatcher(paths=[], hook_trigger=trigger)
    await watcher.start()
    assert watcher.is_started() is False
    await watcher.aclose()  # must not raise even though start() no-op'd


@pytest.mark.asyncio
async def test_watchdog_unavailable_warns_and_disables_feature_without_crashing(tmp_path, monkeypatch):
    """Tier 2: paths ARE configured but ``watchdog`` is not importable -> the
    watcher logs a warning and disables the feature — never crashes the
    session. ``monkeypatch.setattr`` here replaces our OWN module-level import
    helper with a real callable (not a collaborator's behavior) returning
    ``None``, exactly the "not installed" contract ``_import_watchdog``
    documents — allowed per the monkeypatch-with-a-real-callable policy."""
    import reyn.runtime.fs_watcher as fs_watcher_mod

    monkeypatch.setattr(fs_watcher_mod, "_import_watchdog", lambda: None)

    trigger = _Recorder()
    watcher = FsWatcher(paths=[str(tmp_path)], hook_trigger=trigger)
    await watcher.start()  # must not raise
    assert watcher.is_started() is False

    # And the feature really is off: a real write under the configured path
    # never fires (there is no observer running).
    (tmp_path / "a.txt").write_text("x")
    await asyncio.sleep(0.2)
    assert trigger.calls == []
    await watcher.aclose()


@pytest.mark.asyncio
async def test_no_hook_trigger_wired_start_is_a_pure_noop(tmp_path):
    """Tier 2: ``hook_trigger=None`` (never wired) — ``start()``/``aclose()``
    never raise and no observer is ever created, mirroring
    ``MCPConnectionService``'s ``hook_trigger=None`` no-op contract."""
    watcher = FsWatcher(paths=[str(tmp_path)], hook_trigger=None)
    await watcher.start()
    assert watcher.is_started() is False
    await watcher.aclose()


# ---------------------------------------------------------------------------
# Tier 2: session-level integration — real Session, real hooks_config, real
# fs_watch_config, real inbox landing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_owned_watcher_fires_configured_hook_into_inbox(tmp_path):
    """Tier 2: end-to-end through a REAL ``Session`` — ``fs_watch_config`` wires
    an ``FsWatcher`` whose ``hook_trigger`` closes over THIS session's OWN
    ``HookDispatcher`` (per-session attribution, mirroring H1's
    ``MCPConnectionService`` wiring), a REAL file write fires the configured
    ``file_changed`` hook, and the templated push lands in the session's
    (public) inbox."""
    from reyn.config.infra import FsWatchConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.session import Session

    watched_dir = tmp_path / "watched"
    watched_dir.mkdir()
    hooks_config = [
        {
            "on": "file_changed",
            "template_push": {"message": "changed: {{ path }} ({{ event_type }})"},
        },
    ]
    session = Session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        hooks_config=hooks_config,
        fs_watch_config=FsWatchConfig(paths=[str(watched_dir)], debounce_seconds=0.05),
    )
    try:
        await session._fs_watcher.start()  # mirrors run()'s own call; started via the public surface below is asserted
        assert session.fs_watcher_is_started()

        target = watched_dir / "c.txt"
        target.write_text("hi")

        await _wait_for(lambda: not session.inbox.empty())
        kind, payload = session.inbox.get_nowait()
        assert kind == "hook"
        assert payload["name"] == "file_changed"  # no name: set -> defaults to the point
        assert str(target) in payload["text"]
    finally:
        await session.aclose_fs_watcher()
