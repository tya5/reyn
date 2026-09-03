"""Tier 2: #5226 — reyn cannot currently answer "how many of me are alive,
and whose" without an operator manually shelling out to ``ps``+``lsof``.

Owner's own observation (2026-08-21, relayed by lead-coder): "I only
launched one reyn session, so the rest are your own cleanup misses."
lead-coder's own real-machine trace confirmed it — 12 ``reyn``/``reyn:chat``
processes, 11 abandoned, the oldest 11 days, and had to reconstruct "whose"
by hand via ``ps -eo pid,etime,comm`` + ``lsof -a -p <pid> -d cwd`` (reyn
rewrites its own process NAME, ``reyn.runtime.proctitle``, so ``comm``
alone cannot say which workspace a listed PID belongs to). Design ruled by
lead-coder (issue #5226's own final comment, before implementation):
``~/.reyn/processes/<pid>.json`` markers, PID-keyed (never a shared subtree
— unlike ``~/.reyn/plugins/<name>/``'s own known #3212 clobber hazard),
``pid_alive`` reuse (not reinvented — ``data/index/build_lock.py``'s
version, not ``api/safe/process.py``'s sandbox-scoped one), "who" =
ppid+cwd+subcommand only (the same data lead-coder manually reconstructed —
no fabricated attribution, no full argv/paths).

Matches lead-coder's own 4 acceptance criteria exactly:
① 2 processes registered → 2 visible; one exits → exactly 1 visible.
② a dead-PID marker (left behind by a non-graceful exit) is reaped on the
  next read, not kept forever.
③ full path/argv never appears anywhere in a marker's own content — a
  CONTENT inspection, not a behavioral strip-falsify.
④ strip-falsify: removing the marker-write call makes ① go red.

Real processes throughout — a genuinely separate, real, controllable OS
subprocess for the "two processes" case (spawned via ``subprocess.Popen``,
released via its own stdin so no sleep/duration is ever waited on — CLAUDE.md's
own Ceiling/Floor rule), never a synthetic marker faked in-process for a PID
that isn't actually running that code. ``PROCESSES_DIR`` monkeypatched to a
``tmp_path`` for every test — this module's own real target,
``~/.reyn/processes/``, is real, shared, user-global state that tests must
never touch. That last claim now actually HOLDS: every direct
``register_process()`` call in this file is undone via the public
``unregister_process()`` (never the private ``_cleanup`` alone), which
also cancels the ``atexit`` handler ``register_process`` armed — architect's
TESTS-READ(B) finding (#5326) was that ``_cleanup`` alone left that handler
armed against whatever ``PROCESSES_DIR`` is live at interpreter shutdown,
i.e. the REAL one, once this test's own monkeypatch reverts.

``test_the_real_cli_entry_point_actually_calls_register_process`` closes a
second TESTS-READ(B) finding: every OTHER test here calls
``register_process()`` directly, so none of them witnessed the actual
production call site in ``interfaces/cli/__init__.py:main()`` — deleting
those two real lines left every other test in this file green while the
feature was completely dead in production."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reyn.runtime import process_registry
from tests._support.paths import REPO_ROOT


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test in this file gets its own ``PROCESSES_DIR`` — the real
    ``~/.reyn/processes/`` is shared, user-global state (mirrors
    ``~/.reyn/plugins/<name>/``'s own #3212 concurrency hazard this
    module's own docstring names) and must never be touched by a test."""
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    return processes_dir


def _spawn_registering_subprocess(cwd: "Path | None" = None) -> subprocess.Popen:
    """A REAL, separate OS subprocess that imports this exact module,
    calls ``register_process`` for ITS OWN real PID (genuinely different
    from this test process's own PID), then blocks on a real stdin read
    (never a sleep) until the test releases it by closing stdin. The
    PROCESSES_DIR override is threaded through an env var, since a
    subprocess has no access to this test's own monkeypatched module
    attribute.

    ``cwd`` (#5358): the child's own working directory, hence its
    marker's own ``cwd`` field — deliberately independent of THIS test
    process's cwd, so a test can prove a #5358-shaped fix resolves the
    marker's own project, not whatever directory the reaping caller
    happens to be running from."""
    script = (
        "import sys\n"
        "from reyn.runtime import process_registry\n"
        "process_registry.PROCESSES_DIR = __import__('pathlib').Path(sys.argv[1])\n"
        "process_registry.register_process('subproc')\n"
        "sys.stdin.readline()\n"  # blocks for real — released by closing stdin
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(process_registry.PROCESSES_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(cwd) if cwd is not None else None,
    )


def _wait_until(condition) -> None:
    """CLAUDE.md's own Ceiling rule: poll a real condition unboundedly, no
    sleep-based fixed wait count or custom timeout of our own — pytest's
    own ``--timeout`` (or CI's) is the kill switch if this genuinely
    never resolves."""
    while not condition():
        time.sleep(0)  # yield the GIL, not a duration this assertion depends on


def test_two_real_processes_both_visible_then_one_exits_and_drops_to_one() -> None:
    """Tier 2: acceptance ① — 2 real processes registered → exactly 2
    markers visible; one exits gracefully (its own atexit cleanup fires)
    → exactly 1 remains."""
    process_registry.register_process("self")
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 2)
        pids = {e["pid"] for e in process_registry.live_processes()}
        assert pids == {os.getpid(), proc.pid}, (
            f"#5226 REGRESSION: expected exactly this process's own pid and "
            f"the subprocess's, got {pids!r}"
        )

        assert proc.stdin is not None  # sanity: PIPE was requested above
        proc.stdin.close()  # release the real blocked read — a graceful exit
        proc.wait()  # the child is guaranteed to terminate once stdin closes

        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        remaining = process_registry.live_processes()
        assert remaining[0]["pid"] == os.getpid(), (
            "#5226 REGRESSION: after the subprocess exited, the SELF marker "
            f"should be the only one left — got {remaining!r}"
        )
    finally:
        process_registry.unregister_process(os.getpid())
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_dead_pid_marker_is_reaped_on_the_next_read(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: acceptance ② — a marker left behind by a process that did
    NOT exit gracefully (no atexit ever ran — the real shape a SIGKILL or
    hard crash leaves) is reaped the next time anything reads the
    registry, once its PID is confirmed dead."""
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        marker_path = _isolated_processes_dir / f"{proc.pid}.json"
        assert marker_path.exists()  # sanity: the real marker is really there

        proc.kill()  # SIGKILL — atexit never runs; the marker is left behind
        proc.wait()  # a killed process is guaranteed to terminate

        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        remaining = process_registry.live_processes()
        assert remaining == [], (
            f"#5226 REGRESSION: a confirmed-dead PID's marker was not reaped "
            f"on read — got {remaining!r}"
        )
        assert not marker_path.exists(), (
            "the stale marker file itself should have been deleted by the read, "
            "not just excluded from the returned list"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_marker_content_never_carries_argv_or_a_path_beyond_cwd(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: acceptance ③ — a CONTENT inspection (not a behavioral
    strip-falsify): the marker this process writes about itself carries
    EXACTLY {pid, ppid, cwd, subcommand, started_at, agent_name,
    broker_session_id, last_loop_beat_at} and nothing else — no full
    argv, no path beyond cwd. Mirrors ``proctitle.py``'s own explicit
    stance against leaking more than the minimum. #5350 added
    agent_name/broker_session_id (both absent — ``None`` — until a
    later :func:`record_process_identity` call sets one or both);
    #5709 added ``last_loop_beat_at`` the same way (absent until
    :func:`~reyn.runtime.process_registry.ProcessLoopBeatDriver.check`
    first writes it). This test's own closed-set assertion is what
    makes a future field a deliberate change, not a silent drift."""
    process_registry.register_process("chat")
    try:
        marker_path = _isolated_processes_dir / f"{os.getpid()}.json"
        raw = json.loads(marker_path.read_text(encoding="utf-8"))

        assert set(raw.keys()) == {
            "pid", "ppid", "cwd", "subcommand", "started_at",
            "agent_name", "broker_session_id", "last_loop_beat_at",
        }, (
            f"#5226 REGRESSION: the marker carries an unexpected field — "
            f"got keys {sorted(raw.keys())!r}"
        )
        assert raw["agent_name"] is None and raw["broker_session_id"] is None, (
            "#5350: register_process() must never guess an identity — "
            "both fields start absent until record_process_identity sets one"
        )
        assert raw["last_loop_beat_at"] is None, (
            "#5709: a marker whose owning process never armed the loop-beat "
            "driver must stay honest — no fabricated beat"
        )
        # `subcommand` must be the bare word passed in — never the full
        # argv this process was actually launched with (pytest's own,
        # which carries this test file's real absolute path).
        assert raw["subcommand"] == "chat"
        this_process_own_argv_path = str(Path(sys.argv[0]).resolve())
        assert this_process_own_argv_path not in json.dumps(raw), (
            "#5226 REGRESSION: the marker's own JSON content contains this "
            "process's real argv[0] path — argv must never be recorded"
        )
    finally:
        process_registry.unregister_process(os.getpid())


# ── #5358: reap destroys the only evidence a process ever ran — a P6
# event before the unlink is what makes the stop no longer silent ──────────


def _read_events_of_kind(events_dir: Path, kind: str) -> list[dict]:
    """Read every JSONL event of *kind* from anywhere under *events_dir*
    (same helper shape ``test_asyncio_diagnostics.py`` uses for the SAME
    ``emit_cli_event`` seam)."""
    found: list[dict] = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


def test_reaping_a_dead_marker_emits_one_event_under_the_markers_own_project(
    _isolated_processes_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5358 — the reap in ``live_processes()`` used to unlink a
    confirmed-dead marker with zero logger/audit-event calls (docs-maintainer's
    real-machine measurement, #4850's own investigation): the ONLY record
    that PID ever ran was the marker file itself, and the reap destroyed it
    with nothing written first. One ``process_marker_reaped`` P6 event must
    now land, BEFORE the unlink, carrying the marker's own content (not a
    summary/subset of it — a reader needs to reconstruct what was lost) plus
    an ``observed_at`` timestamp (an upper bound on when the process actually
    stopped, never asserted as an exact stop time — this is a
    detection mechanism, not a diagnosis one).

    lead-coder's TESTS-READ(B) BLOCK (#5359): ``PROCESSES_DIR`` is one
    machine-wide directory but ``.reyn/events`` is per-project — the
    REAPING caller's own cwd has nothing to do with the DEAD process's
    project. This test deliberately runs the dying subprocess from its OWN
    project directory (``dead_process_project``) while THIS test process
    (the reaping caller, standing in for ``reyn doctor``) sits in a
    SEPARATE directory that has no ``.reyn/`` at all — the shape that used
    to make ``emit_cli_event``'s own cwd-based discovery warn-and-skip
    silently, reintroducing the exact silence #5358 exists to close. The
    event must land under the marker's OWN project, never the caller's
    (which has nowhere for it to land at all)."""
    dead_process_project = tmp_path / "dead_process_project"
    dead_process_project.mkdir()
    (dead_process_project / ".reyn").mkdir()
    caller_cwd = tmp_path / "caller_with_no_reyn_dir"
    caller_cwd.mkdir()
    monkeypatch.chdir(caller_cwd)

    proc = _spawn_registering_subprocess(cwd=dead_process_project)
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        marker_path = _isolated_processes_dir / f"{proc.pid}.json"
        original_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert original_marker["cwd"] == str(dead_process_project.resolve()), (
            "sanity: the marker's own cwd is the subprocess's real cwd"
        )

        proc.kill()
        proc.wait()
        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        before_reap = time.time()
        result = process_registry.live_processes()
        after_reap = time.time()

        assert result == []  # sanity: the dead marker was reaped, as before
        assert not marker_path.exists()  # sanity: same reap behavior, unchanged

        # Nothing landed under the CALLER's own (nonexistent) .reyn/ — there
        # isn't one to land under; this only fails if some OTHER code path
        # accidentally created one.
        assert not (caller_cwd / ".reyn").exists()

        events = _read_events_of_kind(
            dead_process_project / ".reyn" / "events", "process_marker_reaped",
        )
        [event] = events  # exactly one — unpack raises otherwise
        data = event["data"]
        assert data["marker"] == original_marker, (
            "#5358 REGRESSION: the event must carry the marker's own full "
            f"content, not a summary — got {data['marker']!r}, expected "
            f"{original_marker!r}"
        )
        assert before_reap <= data["observed_at"] <= after_reap, (
            "observed_at must be a real timestamp taken at reap time, not a "
            "constant or the marker's own started_at"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_live_marker_is_never_reaped_and_emits_no_event(
    _isolated_processes_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5358 deny side, in the SAME shape as the positive witness
    above (CLAUDE.md's own six-questions ④ — a deny-only assertion is green
    even if the whole mechanism silently never ran; pairing it with the
    positive witness above closes that gap the same way #5331's own
    reviewer named it). A confirmed-ALIVE process's marker must be neither
    reaped NOR reported — reaping only fires for a confirmed-dead PID."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    process_registry.register_process("self")
    try:
        result = process_registry.live_processes()
        assert os.getpid() in {e["pid"] for e in result}  # sanity: still reported live

        events = _read_events_of_kind(reyn_dir / "events", "process_marker_reaped")
        assert events == [], (
            "#5358 REGRESSION: a live process's marker must never be "
            f"reported as reaped — got {events!r}"
        )
    finally:
        process_registry.unregister_process(os.getpid())


def test_reap_still_happens_when_the_audit_emit_itself_fails(
    _isolated_processes_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5358, lead-coder's TESTS-READ(B) BLOCK ③ — the pre-existing
    ``except OSError: pass`` around the unlink only ever protected the
    enumeration loop from a filesystem error on the unlink itself; the NEW
    audit-emit call sits ahead of it with no guard of its own. Forces
    ``emit_direct_event`` to raise (a real exception, not a mock — the
    genuinely-importable production function, monkeypatched to fail on
    THIS one call) and confirms ``live_processes()`` still completes,
    still returns the empty-of-dead-markers result, and still reaps the
    marker file — an audit-emit failure must degrade to a log line, never
    break the read path this module's whole reason to exist is to keep
    working.

    The spawned process needs a REAL ``.reyn/``-reachable cwd (this repo's
    own autouse ``_isolated_cwd`` fixture already chdirs THIS test process
    itself to a bare tmp dir with none — otherwise ``reyn_dir is None``
    would skip the emit call entirely, and this test would pass without
    ever exercising the code path it means to falsify, invisibly."""
    dead_process_project = tmp_path / "dead_process_project"
    dead_process_project.mkdir()
    (dead_process_project / ".reyn").mkdir()

    from reyn.core.events import events as events_mod

    def _raising_emit(*args, **kwargs):
        raise RuntimeError("#5358 test: simulated emit_direct_event failure")

    monkeypatch.setattr(events_mod, "emit_direct_event", _raising_emit)

    proc = _spawn_registering_subprocess(cwd=dead_process_project)
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        marker_path = _isolated_processes_dir / f"{proc.pid}.json"

        proc.kill()
        proc.wait()
        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        result = process_registry.live_processes()  # must not raise

        assert result == [], (
            "#5358 REGRESSION: a failing audit-emit must not prevent the "
            f"dead marker from being correctly excluded — got {result!r}"
        )
        assert not marker_path.exists(), (
            "#5358 REGRESSION: a failing audit-emit must not prevent the "
            "reap itself — the marker should still be gone"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_the_real_cli_entry_point_actually_calls_register_process(
    tmp_path: Path,
) -> None:
    """Tier 2: BLOCK finding, architect's TESTS-READ(B) review (#5326) —
    every OTHER test in this file calls ``register_process()`` directly,
    so none of them witness the actual production call site:
    ``register_process(getattr(args, "command", None))`` in
    ``interfaces/cli/__init__.py:main()``. Deleting those two real lines
    left all 5 original tests green while the feature was completely
    dead in production (reyn would register nothing, ``reyn doctor``
    would always say "no reyn process markers found", and CI stayed
    silent) — architect read the diff and reasoned this out (no live
    verification, by design — owner ruling scopes architect to
    review, not to running code); the strip-falsify confirming it was
    run by this PR's own author.

    Spawns the REAL ``reyn.interfaces.cli.main()`` entry point as a
    genuinely separate subprocess, with ``HOME`` pointed at an isolated
    directory via the child's own environment — ``PROCESSES_DIR``
    resolves ``Path.home()`` at IMPORT time, so this has to be an env var
    on the child process, not a monkeypatched attribute on this test's
    own module. The child stubs OUT ``doctor.run`` (the chosen
    subcommand's own business logic) with a real blocked stdin read
    before calling ``main()`` — this test verifies CLI STARTUP wiring
    (parse_args -> set_process_title -> register_process -> args.func),
    not what ``doctor`` itself does (covered by the other #5226 test
    file); replacing only the downstream business logic, while leaving
    ``main()``, argument parsing, and ``register_process`` itself
    completely real, is what makes this a genuine witness rather than a
    fake collaborator standing in for the code under test. The block is
    necessary because ``register_process``'s own ``atexit`` cleanup would
    otherwise remove the marker the instant a real, un-stubbed
    ``doctor`` command finished running — before this test could ever
    observe it."""
    home = tmp_path / "home"
    home.mkdir()
    child_script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from reyn.interfaces.cli.commands import doctor as _doctor_mod\n"
        # Stub out ONLY doctor's business logic — CLI startup (including
        # register_process) stays completely real.
        "_doctor_mod.run = lambda args: sys.stdin.readline()\n"
        "from reyn.interfaces.cli import main\n"
        "sys.argv = ['reyn', 'doctor']\n"
        "main()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_script, str(REPO_ROOT / "src")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={**os.environ, "HOME": str(home)},
    )
    try:
        marker_dir = home / ".reyn" / "processes"
        # #5342 CI observation (lead-coder): this test failed on json.loads
        # under an unrelated PR's CI run — real, not a flake. Root cause:
        # register_process() writes via Path.write_text(), which is NOT
        # atomic — the file is created (and so becomes glob-visible) the
        # moment it's opened, before its content is written and flushed.
        # The OLD wait predicate here only checked for the file's EXISTENCE
        # (``any(marker_dir.glob("*.json"))``), so under real disk-I/O
        # contention (12000+ tests across xdist workers measured the night
        # this was found) a reader could win the race between "file exists"
        # and "file's bytes are fully written" — reading a truncated/empty
        # file straight into json.loads.
        #
        # Fixed two ways at once: (a) the wait predicate now requires the
        # file to be SUCCESSFULLY PARSEABLE, not merely present — closing
        # the exact race; (b) the marker is addressed by this test's own
        # already-known ``proc.pid`` directly, not "whichever file glob
        # returns first" — closing a SEPARATE concern lead-coder raised
        # (``markers[0]`` had no guarantee of being THIS test's own marker
        # rather than some other file, however unlikely under `tmp_path`
        # isolation in practice).
        marker_path = marker_dir / f"{proc.pid}.json"

        def _marker_is_readable() -> bool:
            if not marker_path.is_file():
                return False
            try:
                json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return True

        _wait_until(_marker_is_readable)

        markers = list(marker_dir.glob("*.json"))
        assert markers == [marker_path], (
            f"#5226 REGRESSION: the real CLI entry point (main()) should "
            f"register itself via register_process under exactly this "
            f"process's own pid ({proc.pid}) — got {markers!r} under "
            f"{marker_dir}"
        )
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        assert data["pid"] == proc.pid, (
            "the marker's own pid should be the real child process's pid"
        )
        assert data["subcommand"] == "doctor", (
            "the marker should carry the real subcommand main() parsed, "
            f"got {data['subcommand']!r}"
        )
    finally:
        assert proc.stdin is not None  # sanity: PIPE was requested above
        proc.stdin.close()  # release the real blocked read — a graceful exit
        proc.wait()


# ── #5346: register_process() writes via a .tmp staging file + atomic rename ─


def test_a_tmp_only_marker_is_invisible_to_live_processes_and_never_misglobbed(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5346 — architect's own prescribed witness. A ``.tmp``
    staging file (the intermediate state ``register_process`` passes
    through between writing content and the atomic rename onto the real
    marker name) must be invisible to ``live_processes()`` two ways at
    once: the pid it names must not appear in the returned list, AND the
    file itself must never be mistaken for a real ``*.json`` marker (the
    exact failure #5345 fixed on the READER side of this same file —
    #5346 closes the WRITER side)."""
    pid = os.getpid() + 1  # a pid guaranteed not to be this test process's own
    _isolated_processes_dir.mkdir(parents=True, exist_ok=True)
    tmp_marker = _isolated_processes_dir / f"{pid}.json.tmp"
    tmp_marker.write_text(json.dumps({"pid": pid, "subcommand": "mid-write"}), encoding="utf-8")

    result = process_registry.live_processes()

    assert all(entry["pid"] != pid for entry in result), (
        f"#5346 REGRESSION: a bare .tmp staging file's pid should never "
        f"appear as a live process — got {result!r}"
    )
    real_markers = list(_isolated_processes_dir.glob("*.json"))
    assert real_markers == [], (
        f"#5346 REGRESSION: a .tmp file should never be matched by the "
        f"*.json glob — got {real_markers!r}"
    )


def test_dead_pid_tmp_marker_is_reaped_on_the_next_read(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5346 — a ``.tmp`` orphaned by a process that died BETWEEN
    writing it and the atomic rename (a narrow window, but a real one —
    architect's own charter Q1 bounding: something must eventually stop
    this from accumulating) is reaped the next time anything reads the
    registry, once its pid is confirmed dead — mirrors the EXISTING
    dead-``.json``-marker reap exactly, same criterion
    (``pid_alive``), same file."""
    proc = _spawn_registering_subprocess()
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        real_marker = _isolated_processes_dir / f"{proc.pid}.json"
        assert real_marker.exists()  # sanity: the real write+rename completed

        proc.kill()
        proc.wait()
        _wait_until(lambda: not process_registry.pid_alive(proc.pid))

        # Simulate the orphan-.tmp shape directly (deterministic — not
        # trying to force the real, narrow write/death race): a .tmp
        # file for this now-confirmed-dead pid, exactly what would be
        # left if the kill had landed between the tmp write and the
        # rename instead of after it.
        orphan_tmp = _isolated_processes_dir / f"{proc.pid}.json.tmp"
        orphan_tmp.write_text(
            json.dumps({"pid": proc.pid, "subcommand": "orphaned"}), encoding="utf-8",
        )

        process_registry.live_processes()

        assert not orphan_tmp.exists(), (
            "#5346 REGRESSION: a .tmp orphan for a confirmed-dead pid should "
            "be reaped on the next read, the same as a dead .json marker is"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_live_pid_tmp_marker_is_never_reaped(
    _isolated_processes_dir: Path,
) -> None:
    """Tier 2: #5346 — the reap must never touch a ``.tmp`` belonging to a
    process that is still ALIVE (mid-write, or simply slow) — reaping a
    live process's in-flight staging file would be indistinguishable from
    corrupting its next write. Uses this TEST's own real, indisputably-
    alive pid (never a synthetic one) as the ``.tmp`` owner."""
    _isolated_processes_dir.mkdir(parents=True, exist_ok=True)
    own_tmp = _isolated_processes_dir / f"{os.getpid()}.json.tmp"
    own_tmp.write_text(json.dumps({"pid": os.getpid(), "subcommand": "still-writing"}), encoding="utf-8")
    try:
        process_registry.live_processes()
        assert own_tmp.exists(), (
            "#5346 REGRESSION: live_processes() must never reap a .tmp file "
            "belonging to a confirmed-ALIVE pid"
        )
    finally:
        own_tmp.unlink(missing_ok=True)


def test_register_process_writes_via_tmp_then_atomic_rename_never_direct(
    _isolated_processes_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5346 — strip-falsify target for the fix itself, not just
    its surrounding reap logic. Forces the atomic-rename step
    (``Path.replace``) to fail AFTER the content write succeeds, then
    checks what's on disk: a COMPLETE, valid ``.tmp`` file, and NO
    ``.json`` file at all. Under the OLD code (a direct ``write_text`` to
    the final path, no ``.tmp``/rename at all), patching ``Path.replace``
    has no effect on that code path — the final ``.json`` would exist
    directly and no ``.tmp`` would exist, failing BOTH assertions here.
    No duration anywhere: the failure is forced structurally (a raised
    exception), never a real race waited out."""
    real_replace = Path.replace

    def _failing_replace(self: Path, target) -> None:
        raise OSError("#5346 test: simulated rename failure, proves write-then-rename order")

    monkeypatch.setattr(Path, "replace", _failing_replace)
    pid = os.getpid()
    process_registry.register_process("rename-fails")
    monkeypatch.setattr(Path, "replace", real_replace)  # restore before any cleanup .replace() call

    tmp_marker = _isolated_processes_dir / f"{pid}.json.tmp"
    final_marker = _isolated_processes_dir / f"{pid}.json"
    try:
        assert tmp_marker.is_file(), (
            "#5346 REGRESSION: the content write should still have happened "
            "before the (forced-failing) rename was attempted"
        )
        assert json.loads(tmp_marker.read_text(encoding="utf-8"))["pid"] == pid
        assert not final_marker.exists(), (
            "#5346 REGRESSION: the final marker must never exist unless the "
            "atomic rename actually succeeded"
        )
    finally:
        tmp_marker.unlink(missing_ok=True)
        # register_process()'s own except-OSError branch returns BEFORE
        # arming atexit when the write+rename fails (confirmed by reading
        # the source, not asserted here as private state) — no
        # unregister_process() call needed to avoid a stray atexit handler.
