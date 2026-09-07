"""Tier 2: #3671 follow-up — ``REYN_STALL_TRACE`` (#4405) extended to the
startup path (``run_textual_chat`` / ``TextualChatApp.on_mount``), mirroring
``tests/runtime/test_4405_stall_trace_wiring.py``'s own turn-side pattern
and its own stated reason: prove the WIRING (arm/disarm actually called,
with the right value, at the right points), never the N-second
stall-detection behavior itself (banned by testing policy's duration
rules — see ``stall_trace.py``'s own docstring).

Two disarm sites exist by design (``on_mount()``'s own
``mark_first_frame()`` call — the real, intended boundary — and
``run_textual_chat()``'s ``finally`` — a safety net for "never reached
first frame"), so this file pins BOTH independently: the safety net via
``run_textual_chat`` with a stubbed ``TextualChatApp.run_async`` that
never mounts, and the real boundary via a directly-run, real
``TextualChatApp.on_mount()`` (headless ``run_test()``, the same
technique ``test_loop_probe_3539.py`` uses) — never touching
``run_textual_chat`` itself for that half, since the real disarm site is
inside ``on_mount``, not that function.

``monkeypatch.setattr(stall_trace, "arm"/"disarm", ...)`` only intercepts
because both call sites do a function-local ``from reyn.runtime.
stall_trace import arm/disarm as ...`` (a fresh module-attribute lookup
every call) — the same hoisting trap ``test_4405_stall_trace_wiring.py``
already documents for the turn-side; keep these imports function-local if
you touch either call site.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.runtime import stall_trace
from tests._support.textual_chat_test_helpers import QueueTransport


@pytest.fixture
def installed_file_handler(tmp_path: Path):
    """A REAL ``logging.FileHandler`` installed on the root logger, torn
    down unconditionally — #5877 (architect ruling): the tripwire's own
    dead-man's switch only arms when one of these exists (never a
    ``sys.stderr`` fallback). Root-logger mutation is global process
    state, so this fixture is the one place that installs/removes it,
    rather than each test hand-rolling its own (a leaked handler would
    silently arm every OTHER test's own tripwire worker too).

    Path is exactly ``.reyn/logs/reyn.log`` under ``tmp_path`` — the SAME
    shape ``chat.py``'s own ``_setup_interactive_logging`` uses, and the
    ONE shape ``stall_trace.find_file_handler_path`` actually matches
    (measured directly: pytest's OWN logging plugin unconditionally
    installs its own ``logging.FileHandler`` subclass pointed at
    ``/dev/null`` on the root logger — a bare ``tmp_path / "reyn.log"``
    here would sit BEHIND that pytest-owned handler in the lookup order
    and never be the one this fixture's own callers actually want).

    #5873 follow-up: ``find_file_handler_path`` no longer scans
    ``handlers`` for a path-shape match — it returns whatever was last
    declared via ``stall_trace.register_file_handler_path``. This
    fixture bypasses ``_setup_interactive_logging`` (the one production
    caller of that registration function), so it must register the path
    itself — restoring the PRIOR registration on teardown is
    `tests/conftest.py`'s own `_isolate_stall_trace_file_handler_
    registration` autouse fixture's job now (CI finding, 2026-09-06: a
    6th hand-rolled save/restore here would be the exact per-test
    duplication that fixture's own docstring says to stop adding)."""
    log_dir = tmp_path / ".reyn" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "reyn.log"
    handler = logging.FileHandler(str(log_path))
    logging.getLogger().addHandler(handler)
    stall_trace.register_file_handler_path(str(log_path))
    try:
        yield log_path
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


@pytest.mark.asyncio
async def test_stall_trace_armed_before_app_construction_when_env_set(
    monkeypatch,
) -> None:
    """Tier 2: with REYN_STALL_TRACE set, run_textual_chat() calls
    stall_trace.arm(N) BEFORE mark_app_constructed()/TextualChatApp(...) —
    real function swapped for a recorder, real call observed. run_async
    is stubbed to raise immediately (simulating "never reached first
    frame"), so the finally safety net is what disarms — the OTHER
    disarm site (on_mount) is pinned separately below."""
    monkeypatch.setenv("REYN_STALL_TRACE", "7")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append(f"arm:{seconds}"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_async(self, *args, **kwargs):
        # arm() must have already run by the time run_async is reached.
        assert calls == ["arm:7.0"], "arm() must fire before app.run_async()"
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)

    from reyn.interfaces.inline.textual_chat.app import run_textual_chat

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport())

    assert calls == ["arm:7.0", "disarm"], (
        "the finally safety net must disarm even though on_mount() (the "
        "real boundary) never ran"
    )


@pytest.mark.asyncio
async def test_stall_trace_not_touched_when_env_unset(monkeypatch) -> None:
    """Tier 2: accept-side — with REYN_STALL_TRACE unset (the default),
    neither arm() nor disarm() is called around startup. Proves the
    wiring costs nothing for the overwhelming majority of runs that never
    opt in."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_async(self, *args, **kwargs):
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)

    from reyn.interfaces.inline.textual_chat.app import run_textual_chat

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport())

    assert calls == [], "arm/disarm must not be touched when the env var is unset"


@pytest.mark.asyncio
async def test_stall_trace_disarmed_at_first_frame_via_on_mount(
    monkeypatch, installed_file_handler: Path,
) -> None:
    """Tier 2: the REAL boundary — a real, headless TextualChatApp
    (``run_test()``, matching ``test_loop_probe_3539.py``'s own
    technique) reaching ``on_mount()``'s ``mark_first_frame()`` call
    disarms the trace THERE, not only via run_textual_chat's safety net
    (pinned separately above) — this test never goes through
    run_textual_chat at all, since the site under test here
    (``on_mount``) is reached the same way regardless of which function
    constructed the app.

    **#5870 stage 1 / #5877**: a SECOND, later ``disarm()`` is now
    expected too — ``_watch_loop_responsiveness``'s own worker (started
    right after this first-frame disarm, see that method's own
    docstring) holds the SAME global timer continuously re-armed for as
    long as the app runs, and disarms it in its own ``finally`` when the
    app shuts down and the worker is cancelled (the assertion below is
    deliberately OUTSIDE the ``async with`` block, so it reads state
    AFTER that shutdown has already happened). Both are real, distinct
    cleanup events now — the first-frame handoff this test is actually
    about, and the tripwire's own worker-exit cleanup — not a regression
    of the first. Needs ``installed_file_handler``: the worker's own
    dead-man's switch only arms (and so only later disarms) when a real
    reyn.log-shaped destination exists (#5877 architect ruling) — without
    it this test's own SECOND-disarm premise would not hold at all."""
    monkeypatch.setenv("REYN_STALL_TRACE", "5")

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert calls == ["disarm"], (
            "on_mount()'s own mark_first_frame() call must disarm the trace "
            "directly, before the app has even shut down — this is the "
            "real, intended boundary, not just the finally-block safety net"
        )

    assert calls == ["disarm", "disarm"], (
        "expected exactly ONE further disarm() after app shutdown — the "
        "tripwire worker's own finally cleanup (#5870 stage 1) — not zero "
        f"(a dangling timer) or more than one: {calls!r}"
    )


@pytest.mark.asyncio
async def test_the_tripwire_arms_its_own_fd_when_a_file_handler_exists(
    monkeypatch, installed_file_handler: Path,
) -> None:
    """Tier 2: #5870 stage 1 / #5877 review (architect ruling,
    real-machine measurement) — right after ``on_mount()``'s own disarm
    above hands the one process-wide timer off, ``_watch_loop_
    responsiveness``'s own worker arms it AGAIN itself, with
    ``repeat=False`` — the per-tick dead-man's switch, not the
    ``REYN_STALL_TRACE`` bracket's own ``repeat=True`` shape.

    ``file`` must be the worker's OWN ``os.open()``-ed integer fd against
    the installed ``FileHandler``'s ``baseFilename`` — asserted by
    ``os.fstat().st_ino`` matching the path's own inode, never by
    comparing file/stream OBJECTS (the whole point of #5877's fix: a
    borrowed stream object's fd number can be silently reused once that
    stream closes — see ``find_file_handler_path``'s own docstring for
    the reproduced mechanism). Deliberately with ``REYN_STALL_TRACE``
    UNSET: this arm must fire regardless, the same "arrives unannounced,
    so it cannot wait for a manual opt-in" reasoning ``loop_probe.py``'s
    own module docstring already states for the tripwire itself. Wiring
    only — no real delay, no threshold crossing (banned by testing
    policy's duration rules, this file's own module docstring)."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    calls: "list[tuple[float, object, bool | None]]" = []
    monkeypatch.setattr(
        stall_trace, "arm",
        lambda seconds, **kw: calls.append((seconds, kw.get("file"), kw.get("repeat"))),
    )
    monkeypatch.setattr(stall_trace, "disarm", lambda: None)

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # Asserted INSIDE the ``async with`` block, deliberately: the
        # worker's own REAL ``finally`` (``stall_trace.disarm`` is stubbed
        # above, but this test does not stub ``os.close``) closes this fd
        # for real the moment the app shuts down at block-exit — reading
        # it afterward would race an already-closed descriptor.
        assert calls, (
            "the tripwire's own worker never armed the dead-man's switch — "
            "expected it to fire right after on_mount()'s own disarm, with a "
            "FileHandler installed and no REYN_STALL_TRACE opt-in required"
        )
        seconds, file_arg, repeat = calls[0]
        assert seconds == pytest.approx(0.25), f"expected the 250ms tripwire threshold, got {seconds!r}"
        assert repeat is False, (
            "the tripwire's own arm must use repeat=False (a re-armed "
            "dead-man's switch), not repeat=True (a fixed-cadence alarm)"
        )
        assert isinstance(file_arg, int), (
            f"expected the worker's OWN os.open()-ed int fd, got {file_arg!r} "
            "(a stream/file OBJECT here would be exactly the #5877 hazard — "
            "its fd number can be reused once IT closes, silently redirecting "
            "a still-pending dump)"
        )
        assert os.fstat(file_arg).st_ino == installed_file_handler.stat().st_ino, (
            "the armed fd does not point at the installed FileHandler's "
            "own baseFilename"
        )


@pytest.mark.asyncio
async def test_the_tripwire_reopens_its_fd_after_a_log_rotation(monkeypatch, tmp_path: Path) -> None:
    """Tier 2: #5873 co-vet finding (architect 🔴-1) — a ``RotatingFileHandler``
    rollover renames the path this worker's self-opened fd points at
    (``reyn.log`` -> ``reyn.log.1``, then ``.2``, ...), eventually UNLINKING
    it past ``backup_count`` — PERMANENTLY, not "one rollover behind" as an
    earlier version of ``find_file_handler_path``'s own docstring claimed
    (true only before this module could ever rotate, #5877). Left
    unhandled, every dump after the first rollover this worker's fd
    survives writes to an ever-more-stale, eventually deleted generation
    nobody reads.

    Each tick now compares ``os.stat(path).st_ino`` (the file currently AT
    that path) against ``os.fstat(fd).st_ino`` (what this worker's fd still
    points at) and, on a mismatch, disarms + closes + reopens against the
    CURRENT file before re-arming — deterministic (cut on file identity,
    not a clock).

    Drives the rollover directly (``handler.doRollover()``, real production
    API, not a simulated size threshold), then waits UNBOUNDED for the
    worker's own next ``arm()`` call to carry a fd whose inode matches the
    POST-rollover file (testing policy: wait on the condition, never a
    fixed tick count — CI's own ``--timeout=120`` is the ceiling). strip
    (recorded during this fix): removing the inode-comparison block in
    ``_watch_loop_responsiveness`` leaves every later ``arm()`` call's fd
    permanently pointing at the pre-rollover (renamed) generation, so this
    assertion never becomes true and the test times out."""
    from logging.handlers import RotatingFileHandler

    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)

    log_dir = tmp_path / ".reyn" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "reyn.log"
    # backupCount >= 1 is load-bearing for this test's own premise:
    # RotatingFileHandler.doRollover() with backupCount == 0 just closes
    # and reopens the SAME path with no rename, which keeps the SAME
    # inode — no rotation for this test to detect at all.
    handler = RotatingFileHandler(str(log_path), maxBytes=1, backupCount=2)
    # #5909 (architect prescription 2, #5922 CI finding): a DEDICATED,
    # non-propagating logger — never ``logging.getLogger()`` (root) — owns
    # this handler. Root is a process-wide SHARED sink: any unrelated
    # ``logging.warning(...)`` reaching it (this test's own real,
    # headless ``TextualChatApp`` runs live below) is one record through
    # this handler, and with ``maxBytes=1`` ANY record rolls it over —
    # replacing ``log_path``'s inode BEFORE this test's own controlled
    # ``handler.doRollover()`` call below, falsifying the setup premise at
    # ``pre_ino == log_path.stat().st_ino``. The subject under test here
    # is ``_watch_loop_responsiveness``'s own fd/inode-reopen logic keyed
    # on the PATH ``stall_trace.register_file_handler_path`` declares
    # (below) — never which logger owns the handler feeding that path
    # (``find_file_handler_path`` is a plain declared-path lookup since
    # #5873, not a root-logger handler scan — see its own docstring), so
    # this narrows WHO can write through this handler without changing
    # what the test proves. ``maxBytes=1`` stays as small as it was
    # (that's what makes ``doRollover()`` a real rollover to detect, not
    # the bug) — the fix is limiting the WRITER, never loosening the
    # threshold.
    private_logger = logging.getLogger(f"{__name__}.log_rotation_witness")
    private_logger.propagate = False
    private_logger.addHandler(handler)
    # Registration restore is tests/conftest.py's own autouse
    # _isolate_stall_trace_file_handler_registration fixture's job — see
    # installed_file_handler's own docstring above for why this file no
    # longer hand-rolls it per test.
    stall_trace.register_file_handler_path(str(log_path))

    calls: "list[tuple[float, object, bool | None]]" = []
    monkeypatch.setattr(
        stall_trace, "arm",
        lambda seconds, **kw: calls.append((seconds, kw.get("file"), kw.get("repeat"))),
    )
    monkeypatch.setattr(stall_trace, "disarm", lambda: None)

    try:
        app = TextualChatApp(transport=QueueTransport())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert calls, (
                "the tripwire's own worker never armed the dead-man's "
                "switch before this test could even drive a rotation"
            )
            pre_ino = os.fstat(calls[-1][1]).st_ino  # type: ignore[arg-type]
            assert pre_ino == log_path.stat().st_ino, (
                "setup: the worker's own arm() fd must start out pointing "
                "at the installed handler's own path"
            )

            handler.doRollover()
            post_ino = log_path.stat().st_ino
            assert post_ino != pre_ino, (
                "setup: doRollover() must produce a NEW inode at the same "
                "path (backupCount=2 makes this a rename, not a truncate) "
                "for this test's own premise to hold"
            )

            while os.fstat(calls[-1][1]).st_ino != post_ino:  # type: ignore[arg-type]
                await pilot.pause()
    finally:
        private_logger.removeHandler(handler)
        private_logger.propagate = True
        handler.close()


@pytest.mark.asyncio
async def test_the_tripwire_never_arms_without_a_file_handler(monkeypatch) -> None:
    """Tier 2: accept-side pair — #5877 architect ruling: "FileHandlerが
    無ければarmしない...決定論で、pytest特有のband-aidではない". No REYN
    ``reyn.log``-shaped ``FileHandler`` installed (the ordinary state for
    every OTHER test in this repo, which construct ``TextualChatApp``
    directly) — the tripwire's own worker must never call ``arm`` at
    all, not even once, for its entire lifetime. This is the exact case
    that hung a real CI run before this fix (a fallback to ``sys.
    stderr``, repeatedly re-armed against a fd number pytest's own
    capture manager could — and did — reuse for something else).

    Premise is ``find_file_handler_path() is None``, NOT "no
    ``FileHandler`` at all" — measured directly (a real pytest run):
    pytest's OWN logging plugin unconditionally installs its own
    ``logging.FileHandler`` subclass pointed at ``/dev/null`` on the
    root logger; :func:`~reyn.runtime.stall_trace.find_file_handler_path`
    is specifically built to see through that one (its own docstring),
    so ITS answer, not a bare handler-type scan, is this test's real
    premise."""
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)
    # Belt-and-braces: this test's OWN premise is "no REYN FileHandler
    # exists" — assert that's actually true rather than assuming no
    # earlier test leaked one onto the root logger (the shared, global
    # object every test in this file also touches).
    assert stall_trace.find_file_handler_path() is None, (
        "setup: a reyn.log-shaped FileHandler is already installed -- "
        "this test's own premise does not hold"
    )

    calls: list[object] = []
    monkeypatch.setattr(stall_trace, "arm", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(stall_trace, "disarm", lambda: None)

    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()

    assert calls == [], (
        f"the tripwire armed with no stable destination available: {calls!r} "
        "-- this is the exact #5877 CI hang shape (a fallback stream a "
        "third party can swap/close out from under a still-pending timer)"
    )


def test_log_stream_falls_back_to_the_original_stderr_not_the_reassignable_name(
    monkeypatch,
) -> None:
    """Tier 1: #5877 architect ruling — with no REYN ``reyn.log``-shaped
    ``FileHandler`` installed, ``stall_trace.default_log_stream()``
    returns ``sys.__stderr__`` (the process's ORIGINAL stderr, fd 2, never closed
    for the process's lifetime), not ``sys.stderr`` (a NAME anything —
    pytest's own capture manager included — can rebind mid-session).
    Reassigning ``sys.stderr`` to something else must not change the
    answer.

    Premise is ``find_file_handler_path() is None``, not "no
    ``FileHandler`` at all" — see ``test_the_tripwire_never_arms_
    without_a_file_handler``'s own docstring, right above, for the
    measured reason (pytest's own ``/dev/null`` handler)."""
    assert stall_trace.find_file_handler_path() is None, (
        "setup: a reyn.log-shaped FileHandler is already installed -- "
        "this test's own premise does not hold"
    )

    import io

    monkeypatch.setattr(sys, "stderr", io.StringIO())

    assert stall_trace.default_log_stream() is sys.__stderr__, (
        "expected the fallback to be sys.__stderr__, unaffected by "
        "reassigning sys.stderr"
    )


pytest_plugins = ["pytester"]

#: Inner conftest for the witness below — puts the repo root AND its own
#: ``src`` on ``sys.path`` (a subprocess gets none of this outer session's
#: own ``pyproject.toml`` ``pythonpath`` favour — ``out_of_process_reyn``'s
#: own docstring in tests/conftest.py has the full reasoning) then imports
#: the REAL fixture under test from the REAL tests/conftest.py, never a
#: reimplementation — pytest activates any ``@pytest.fixture``-decorated
#: callable present in a conftest module's namespace, imported or not.
_INNER_CONFTEST = """
import sys
sys.path.insert(0, {repo_root!r})
sys.path.insert(0, {src_root!r})
from tests.conftest import _cancel_any_pending_faulthandler_dump  # noqa: F401
"""

#: The inner test pair itself — same shape as the removed xdist_group
#: version, but ordering and same-process-ness now come from pytest's own
#: normal (non-distributed) collection order inside ONE real inner
#: session, not from an xdist scheduling guarantee this repo's CI
#: invocation does not actually provide (lead-coder finding, #5926:
#: ``pytest -n auto`` with no ``--dist`` defaults xdist's own ``load``
#: scheduler, which ignores ``xdist_group`` entirely — only ``loadgroup``
#: honors it, and this repo's CI passes neither).
_INNER_TEST = """
import faulthandler
import os
from pathlib import Path

from tests.conftest import faulthandler_safety_net_call_count

_baseline = []

def test_a_leaves_a_faulthandler_dump_armed_without_disarming(tmp_path):
    _baseline.append(faulthandler_safety_net_call_count())
    sink = tmp_path / "leaked_dump.txt"
    fd = os.open(str(sink), os.O_WRONLY | os.O_CREAT)
    try:
        faulthandler.dump_traceback_later(9999, file=fd, repeat=False)
    finally:
        os.close(fd)

def test_b_the_conftest_safety_net_cancelled_it():
    assert _baseline, "setup: test_a must run first, in this same process"
    assert faulthandler_safety_net_call_count() > _baseline[-1]
"""


def test_the_conftest_safety_net_cancels_a_dump_a_test_left_armed(
    pytester: pytest.Pytester,
) -> None:
    """Tier 2: #5909 (architect prescription 1) witness — a REAL, isolated
    inner pytest session (pytester's own subprocess seam, same technique
    ``tests/dev/test_stall_dump_4986.py`` already uses for a real
    ``faulthandler`` timer) runs two tests that could only observe each
    other if they land in the SAME process, in order: the first arms a
    REAL ``faulthandler.dump_traceback_later`` one-shot and returns
    WITHOUT disarming it — the exact shape ``test_the_tripwire_arms_its_
    own_fd_when_a_file_handler_exists`` (above, this same outer file)
    leaves behind when its own ``stall_trace.disarm`` stub lets the app's
    real shutdown close the armed fd out from under a still-pending
    timer; the second asserts ``tests/conftest.py``'s own public
    ``faulthandler_safety_net_call_count()`` counter advanced across the
    first test's own teardown.

    Subprocess (not pytester's in-process ``runpytest()``): this outer
    test's own ``faulthandler`` state must never interact with the inner
    session's — same reasoning ``test_stall_dump_4986.py``'s own module
    docstring states for its own inner runs.

    The counter, not "the inner session exited 0", is this witness's real
    evidence — ``faulthandler``'s own public API (``enable``/``disable``/
    ``is_enabled``/``dump_traceback``/``dump_traceback_later``/
    ``cancel_dump_traceback_later``/``register``/``unregister`` — the
    complete list, confirmed by reading the stdlib module) exposes no
    getter for "is a timer currently armed", so a green inner session
    alone cannot distinguish "the safety net fired" from "nothing ever
    checked" (a dump left armed for 9999s would ALSO exit 0, since
    nothing in the inner session runs anywhere near that long).

    Strip-falsifier: comment out this fixture's own
    ``faulthandler.cancel_dump_traceback_later()`` call in
    ``tests/conftest.py`` and ``test_b`` inside the inner session goes
    red (the counter never advances) — this test's own assertion below
    then reports that inner failure via ``result.assert_outcomes``."""
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    src_root = str(repo_root / "src")

    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(repo_root), src_root=src_root))
    pytester.makepyfile(test_inner=_INNER_TEST)

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=2)
