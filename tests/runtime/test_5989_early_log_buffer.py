"""Tier 2: #5989 symptom 3 — reyn.runtime.early_log_buffer.

A WARNING+ record (or a bare ``warnings.warn``) emitted BEFORE the
interactive CUI's own ``RotatingFileHandler`` installs must not be
unrecoverably lost — it should replay into the real handler once one
exists, and the buffer holding it in the meantime must be bounded even
if no target ever attaches. See that module's own docstring for the
full design (including the ``MemoryHandler``-without-a-target gap this
was built to avoid).

Real ``logging`` module throughout (no mocks) — global logging state
(root handlers/level, ``logging.captureWarnings``'s own guard, and this
module's own installed-singleton) is saved+restored per test, the same
pattern ``test_inline_interactive_logging.py`` already established.
"""
from __future__ import annotations

import logging
import warnings

import pytest

from reyn.runtime import early_log_buffer


def _save_logging_state():
    root = logging.getLogger()
    return root.handlers[:], root.level


def _restore_logging_state(saved) -> None:
    handlers, level = saved
    root = logging.getLogger()
    logging.captureWarnings(False)
    root.handlers[:] = handlers
    root.setLevel(level)
    # early_log_buffer's own test-support seam (module docstring: "never
    # called from production code") -- not a private-attribute poke, this
    # module deliberately provides it so a test can start the next case
    # as if install() had never run.
    early_log_buffer._reset_for_tests()


def test_install_is_idempotent() -> None:
    """Tier 2: a second `install()` call returns the SAME instance, not a
    fresh one — the whole point is ONE buffer per process, installed once
    at the true start."""
    saved = _save_logging_state()
    try:
        first = early_log_buffer.install()
        second = early_log_buffer.install()
        assert first is second, (
            "#5989 REGRESSION: install() must not double-install a second "
            "buffer instance on a repeated call"
        )
    finally:
        _restore_logging_state(saved)


def test_a_warning_emitted_before_a_target_attaches_replays_into_it() -> None:
    """Tier 2: the core accept criterion — a record emitted in the "before
    any real handler exists" window is NOT lost, once a target attaches.

    Strip-falsifier (verified by hand: `install()` reverted to installing
    nothing / a no-op): this test goes red — the target handler's own
    captured records stays empty, since nothing buffered the early
    record for later replay."""
    saved = _save_logging_state()
    captured: "list[str]" = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    try:
        buffer = early_log_buffer.install()
        logging.getLogger("reyn.canary").warning("early-marker-4a11")

        target = _RecordingHandler()
        buffer.replay_into(target)

        assert captured == ["early-marker-4a11"], (
            f"#5989 REGRESSION: a WARNING emitted before a target attached "
            f"did not replay into it — got {captured!r}"
        )
    finally:
        _restore_logging_state(saved)


def test_replay_clears_the_buffer_so_a_second_replay_is_a_no_op() -> None:
    """Tier 2: `replay_into` is idempotent — a record already replayed once
    must not replay AGAIN into a second target (double-delivery)."""
    saved = _save_logging_state()
    first_capture: "list[str]" = []
    second_capture: "list[str]" = []

    class _RecordingHandler(logging.Handler):
        def __init__(self, sink: "list[str]") -> None:
            super().__init__()
            self._sink = sink

        def emit(self, record: logging.LogRecord) -> None:
            self._sink.append(record.getMessage())

    try:
        buffer = early_log_buffer.install()
        logging.getLogger("reyn.canary").warning("replay-once-marker")

        buffer.replay_into(_RecordingHandler(first_capture))
        buffer.replay_into(_RecordingHandler(second_capture))

        assert first_capture == ["replay-once-marker"]
        assert second_capture == [], (
            f"#5989 REGRESSION: a second replay_into() delivered the same "
            f"record again — got {second_capture!r}"
        )
    finally:
        _restore_logging_state(saved)


def test_the_buffer_never_grows_past_its_own_capacity_and_keeps_the_earliest(
) -> None:
    """Tier 2: six-questions #5 — MORE records than `capacity` are emitted
    with NO target ever attached; the buffer must stay capped, not grow
    without bound (the real gap in stdlib's own `MemoryHandler` without a
    target — module docstring, verified by reading cpython's source).

    Which end survives matters, not just "some 3 survive" (module
    docstring's own "which end drops" section, lead-coder review): this
    buffer exists to save the EARLIEST record (the one closest to
    process start, the actual motivating case) — dropping from the
    front on overflow would recreate that exact loss. Pinned here: the
    3 SURVIVORS are markers 0/1/2 (the earliest), not 7/8/9."""
    saved = _save_logging_state()
    try:
        buffer = early_log_buffer.install(capacity=3)
        logger = logging.getLogger("reyn.canary")
        for i in range(10):
            logger.warning("marker-%d", i)

        # The exact-contents check below also pins the count (exactly 3
        # survivors) -- six-questions #5's own answer, "capped at
        # capacity, EARLIEST kept, rest counted as dropped," is what
        # this asserts, not a bare size.
        kept = buffer.kept_messages
        assert kept == ("marker-0", "marker-1", "marker-2"), (
            f"#5989 REGRESSION: buffer must keep the EARLIEST capacity=3 "
            f"records (the motivating case — see module docstring's "
            f"'which end drops' section), not the most recent — "
            f"got {kept!r}"
        )
        assert buffer.dropped_count == 7, (
            f"#5989 REGRESSION: the 7 refused records (10 emitted - "
            f"capacity 3) must be COUNTED, not silently discarded with "
            f"no trace — got {buffer.dropped_count}"
        )
    finally:
        _restore_logging_state(saved)


def test_replay_reports_how_many_early_records_were_dropped() -> None:
    """Tier 2: lead-coder review — a dropped count that never reaches
    `reyn.log` leaves a reader unable to tell "0 lost" from "N lost"
    (the durable, cross-session surface — #5977's own measured
    incident — not the stderr mirror, which only this ONE process's own
    terminal sees). `replay_into` must emit ONE synthetic record stating
    the exact count, after the real buffered records.

    Strip-falsifier (verified by hand: the `if self._dropped:` branch in
    `replay_into` removed): this test goes red — no record mentioning
    the drop count reaches the target at all."""
    saved = _save_logging_state()
    captured: "list[str]" = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    try:
        buffer = early_log_buffer.install(capacity=2)
        logger = logging.getLogger("reyn.canary")
        for i in range(5):
            logger.warning("marker-%d", i)

        buffer.replay_into(_RecordingHandler())
    finally:
        _restore_logging_state(saved)

    # Exact-contents equality (not a bare len()) pins BOTH the count and
    # the order: the 2 kept records, then exactly one drop-count record
    # naming the real count (3 refused: 5 emitted - capacity 2) -- a
    # missing, duplicated, or miscounted drop record all fail this.
    assert captured == [
        "marker-0", "marker-1",
        "3 early log record(s) were dropped before the interactive CUI's "
        "own log file existed (buffer capacity=2 exceeded)",
    ], (
        f"#5989 REGRESSION: expected the 2 kept records plus exactly 1 "
        f"drop-count record naming the real count — got {captured!r}"
    )


def test_replay_with_nothing_dropped_reports_nothing_extra() -> None:
    """Tier 2: falsification contrast for the test above — when NOTHING
    was dropped, `replay_into` must not emit a spurious drop-count
    record (a reader must be able to trust its ABSENCE as "0 lost", not
    have to parse "0 record(s) dropped" every time)."""
    saved = _save_logging_state()
    captured: "list[str]" = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    try:
        buffer = early_log_buffer.install(capacity=10)
        logging.getLogger("reyn.canary").warning("only-marker")
        buffer.replay_into(_RecordingHandler())
    finally:
        _restore_logging_state(saved)

    assert captured == ["only-marker"], (
        f"#5989 REGRESSION: replay_into must not emit a drop-count "
        f"record when nothing was dropped — got {captured!r}"
    )


def test_a_bare_warnings_warn_before_a_target_attaches_also_replays(
) -> None:
    """Tier 2: accept criterion ③ — `captureWarnings(True)` is armed in
    THIS early window too (not only later inside `_setup_interactive_
    logging`), so a bare `warnings.warn` before any real handler exists
    is captured into the SAME buffer as a logging call, and replays the
    same way.

    Strip-falsifier (verified by hand: `install()`'s own
    `logging.captureWarnings(True)` call removed): this test goes red —
    the bare warning never reaches the logging system at all, so nothing
    is in the buffer to replay."""
    saved = _save_logging_state()
    captured: "list[str]" = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    try:
        buffer = early_log_buffer.install()
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(
                "early-bare-warning-marker", category=ResourceWarning, stacklevel=1,
            )

        target = _RecordingHandler()
        buffer.replay_into(target)

        assert any("early-bare-warning-marker" in m for m in captured), (
            f"#5989 REGRESSION: a bare warnings.warn() before a target "
            f"attached did not reach the early buffer — got {captured!r}"
        )
    finally:
        _restore_logging_state(saved)


def test_with_no_target_ever_attached_the_exit_fallback_reaches_stderr() -> None:
    """Tier 2: #6043 — the "handler never attaches" path (--cui / non-TTY,
    or an early crash) must not lose buffered records forever; the
    `atexit` fallback (`_flush_to_stderr_at_exit`, called here directly
    rather than by actually exiting the process — the real trigger is
    `atexit`, this drives the SAME function) delivers them to stderr.

    NOT live visibility (module docstring's own "A real regression"
    section — that was #6043's own bug): this is deferred to the
    fallback running, matching production's own `atexit`-triggered
    timing, not `install()` time."""
    import io
    import sys

    saved = _save_logging_state()
    fake_stderr = io.StringIO()
    real_stderr = sys.stderr
    try:
        sys.stderr = fake_stderr
        buffer = early_log_buffer.install()
        logging.getLogger("reyn.canary").warning("stderr-fallback-marker")

        assert "stderr-fallback-marker" not in fake_stderr.getvalue(), (
            "test setup sanity: a WARNING must NOT reach stderr live "
            "(the #6043 bug this module no longer has) -- only at the "
            "exit-fallback below"
        )

        early_log_buffer._flush_to_stderr_at_exit(buffer)

        assert "stderr-fallback-marker" in fake_stderr.getvalue(), (
            "#5989 REGRESSION: with no target ever attached, the exit-time "
            "fallback must still deliver a WARNING to stderr — got "
            f"{fake_stderr.getvalue()!r}"
        )
    finally:
        sys.stderr = real_stderr
        _restore_logging_state(saved)


def test_the_exit_fallback_never_constructs_a_handler_once_already_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #6043 — `_flush_to_stderr_at_exit`'s own `if buffer.
    replayed: return` guard, witnessed directly (NOT via stderr content —
    disclosed, not hidden: `replay_into` is independently idempotent on
    an already-empty buffer, so a version of this test asserting merely
    "stderr stays empty" would pass even with the guard deleted, since
    the empty buffer alone already produces no output either way — that
    shape was caught writing this test and is NOT what is asserted
    below). Witnessed instead: whether `_flush_to_stderr_at_exit` even
    CONSTRUCTS a `logging.StreamHandler` at all — spied via monkeypatch
    (the real class still runs; only the call is recorded, same "wrap
    the real thing" shape this file's other tests already use).

    Strip-falsifier (verified by hand: the `if buffer.replayed: return`
    guard removed): this test goes red — a `StreamHandler` gets
    constructed even though nothing was buffered to replay."""
    saved = _save_logging_state()
    constructed: "list[object]" = []
    real_stream_handler = logging.StreamHandler

    def spy_stream_handler(*args, **kwargs):  # type: ignore[no-untyped-def]
        handler = real_stream_handler(*args, **kwargs)
        constructed.append(handler)
        return handler

    try:
        buffer = early_log_buffer.install()
        logging.getLogger("reyn.canary").warning("already-replayed-marker")
        buffer.replay_into(logging.NullHandler())
        assert buffer.replayed is True

        monkeypatch.setattr(logging, "StreamHandler", spy_stream_handler)
        early_log_buffer._flush_to_stderr_at_exit(buffer)

        assert constructed == [], (
            f"#6043 REGRESSION: _flush_to_stderr_at_exit constructed a "
            f"StreamHandler even though replay_into() already ran — "
            f"got {constructed!r}"
        )
    finally:
        _restore_logging_state(saved)
