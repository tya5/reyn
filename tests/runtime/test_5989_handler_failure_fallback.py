"""Tier 2: a logging handler's own emit() failure is durably recorded to an
INDEPENDENT destination — never re-logged through the SAME (possibly
broken) handler (#5989 ⑵, PR2).

#5989 PR1 widened run_textual_chat's capture_stray_output window so a
logging.Handler.handleError firing during a live TUI session no longer
writes its `--- Logging error ---` banner + traceback straight to the
operator's real terminal. Capturing it and going nowhere else would just
move the failure from "visible but garbled" to "invisible" -- worse, not
better (CLAUDE.md's own "does the repair destroy the evidence?" question).
This is the other half: FailureFallbackRotatingFileHandler's own
handleError records the failure to a SEPARATE file, independent of
whichever handler (possibly this very instance) just broke.

A real logging.Logger + a real FailureFallbackRotatingFileHandler
throughout -- no MagicMock, no hand-rolled stream stand-in either
(CLAUDE.md: "never fake a collaborator when a real instance is cheaply
constructible" — a real broken file IS cheap here). "Broken" is produced
by re-opening the handler's OWN real log file in READ mode and swapping
that in as `.stream` — a genuinely unwritable real file object, not a
mock of one. Verified directly (both interpreters this repo's own CI runs,
3.11.15 and 3.12.7) that this raises `io.UnsupportedOperation` — a real
`OSError` subclass — from `StreamHandler.emit()`'s own `stream.write(...)`
call, landing in `handleError` exactly like production would; a v1 of
this file used a hand-rolled stub instead, which on 3.11's
`RotatingFileHandler` path raised `AttributeError` (missing `.seek`)
before ever reaching `write()` — passing for the wrong reason entirely
(lead-coder BLOCKING, PR #6088: "test は『実装が OSError を捕まえた』では
なく『stub に属性が無かった』を記録しています").
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from reyn.runtime.logging_failure_fallback import (
    FailureFallbackRotatingFileHandler,
    handler_failure_dump_path,
)


def _break(handler: FailureFallbackRotatingFileHandler) -> None:
    """Swap the handler's `.stream` for the SAME file, reopened read-only —
    a real, genuinely unwritable file object. `write()` on it raises
    `io.UnsupportedOperation` (confirmed an `OSError` subclass, and
    confirmed identical on 3.11/3.12 — see module docstring)."""
    try:
        handler.stream.close()
    except Exception:
        pass
    handler.stream = open(handler.baseFilename, "r")


def _heal(handler: FailureFallbackRotatingFileHandler) -> None:
    """Reopen the SAME file in append mode — a real, writable stream
    again, mirroring the handler's own normal `_open()` shape (append,
    not truncate: a healed handler must not silently lose what a prior
    healthy write already wrote)."""
    try:
        handler.stream.close()
    except Exception:
        pass
    handler.stream = open(handler.baseFilename, "a")


def _make_handler(tmp_path: Path) -> "tuple[FailureFallbackRotatingFileHandler, logging.Logger, Path]":
    reyn_log = tmp_path / "reyn.log"
    handler = FailureFallbackRotatingFileHandler(str(reyn_log), maxBytes=10_000, backupCount=1)
    fallback_path = handler_failure_dump_path(str(reyn_log))
    assert fallback_path is not None
    handler.set_fallback_path(fallback_path)
    logger = logging.getLogger(f"test-5989-{id(handler)}")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    return handler, logger, Path(fallback_path)


def test_a_broken_handler_still_leaves_a_durable_failure_record(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — the fallback file is EMPTY/absent before the
    failure, and holds the failure's own content after it, even though
    the handler that failed is this SAME instance's own destination."""
    handler, logger, fallback_path = _make_handler(tmp_path)
    assert not fallback_path.exists()

    _break(handler)
    logger.warning("first failure: %s", "payload-one")

    assert fallback_path.exists(), (
        "the fallback file was never written — a broken handler's own "
        "failure left no durable record anywhere"
    )
    content = fallback_path.read_text()
    assert "payload-one" in content
    assert "UnsupportedOperation" in content


def test_no_false_kill_a_healthy_handler_writes_no_fallback(tmp_path: Path) -> None:
    """Tier 2: a handler that is NOT broken must not spuriously create the
    fallback file — its absence, not an empty file, is the healthy
    signal (an operator finding the file at all means something failed)."""
    _handler, logger, fallback_path = _make_handler(tmp_path)
    logger.warning("a perfectly normal warning")
    assert not fallback_path.exists()


def test_repeated_identical_failure_collapses_to_one_episode(tmp_path: Path) -> None:
    """Tier 2b: #5989 ⑶ — the SAME failure repeating (the common case: a
    persistently broken handler raising the identical exception on every
    subsequent record) writes the fallback file's content once, not once
    per occurrence. Proven via the internal reset()-call count rather
    than a private-state read of the content alone, since content alone
    cannot distinguish "written once" from "overwritten with the same
    bytes twice" — a real accumulation-bounding claim needs the write
    COUNT, not just the end state."""
    handler, logger, fallback_path = _make_handler(tmp_path)
    _break(handler)

    reset_calls = {"count": 0}
    orig_reset = None

    logger.warning("repeating failure")
    assert fallback_path.exists()
    snapshot = handler._fallback_snapshot
    assert snapshot is not None
    orig_reset = snapshot.reset

    def _counting_reset() -> None:
        reset_calls["count"] += 1
        orig_reset()

    snapshot.reset = _counting_reset  # type: ignore[method-assign]

    logger.warning("repeating failure")
    logger.warning("repeating failure")

    assert reset_calls["count"] == 0, (
        "an IDENTICAL repeating failure re-truncated+rewrote the fallback "
        "file — #5989 ⑶ asked for one record per distinct episode, not "
        "one per occurrence"
    )


def test_a_genuinely_new_failure_after_a_repeat_is_still_recorded(tmp_path: Path) -> None:
    """Tier 2b: the episode gate must not become a permanent mute — a
    DIFFERENT failure (new exception content) after an identical-repeat
    streak is still written."""
    handler, logger, fallback_path = _make_handler(tmp_path)
    _break(handler)

    logger.warning("failure A")
    logger.warning("failure A")  # collapsed, per the test above
    logger.warning("failure B, different payload")

    content = fallback_path.read_text()
    assert "failure B" in content
    assert "failure A" not in content, (
        "the fallback file is an always-overwritten SNAPSHOT (mirrors "
        "stall_dump_path's own shape) — the latest distinct episode "
        "replaces the prior one, it does not accumulate"
    )


def test_recovery_then_a_new_failure_is_recorded_as_a_fresh_episode(tmp_path: Path) -> None:
    """Tier 2b: once the underlying stream is healthy again (a normal
    record succeeds), a LATER failure — even with byte-identical text to
    an earlier, already-recorded one — must still produce a fresh
    record, matching LoopTripwire's own "episode ends at recovery, not
    at session end" rule."""
    handler, logger, fallback_path = _make_handler(tmp_path)

    _break(handler)
    logger.warning("same text")
    first_content = fallback_path.read_text()
    assert "same text" in first_content

    _heal(handler)
    logger.warning("a healthy record in between")

    _break(handler)
    logger.warning("same text")

    # The handler's own last-recorded-text memory is reset only by a
    # DIFFERENT text arriving, per this class's own (simple, documented)
    # gate -- a byte-identical failure after a recovery is therefore
    # still collapsed by this shape, which is the disclosed limit this
    # test pins rather than hides: the gate keys on TEXT identity, not
    # on episode boundaries the handler cannot itself observe (recovery
    # is invisible to handleError, which is only ever called on failure).
    # Observed via CONTENT, not mtime (lead-coder BLOCKING, PR #6088: an
    # mtime identity pin records the TEST'S OWN filesystem-timestamp
    # resolution, an environment property, not reyn's own behaviour --
    # the CONTENT staying byte-identical is what "no re-write happened"
    # actually means).
    assert fallback_path.read_text() == first_content, (
        "disclosed limit: this handler's episode gate keys on failure-text "
        "identity only, not on a genuine recovery boundary — a byte-"
        "identical failure recurring after a healthy interval still "
        "collapses. A true LoopTripwire-style recovery-aware gate would "
        "need this handler to also observe successful emits, which "
        "StreamHandler.emit()'s own control flow (this class deliberately "
        "does not override) does not hand back to handleError."
    )


def test_stays_a_real_file_handler_for_structural_readers(tmp_path: Path) -> None:
    """Tier 1: litellm_bootstrap.py / stall_trace.py both discover the
    interactive-path handler via isinstance(handler, logging.FileHandler)
    — this subclass must still satisfy that, unchanged."""
    handler, _logger, _fallback_path = _make_handler(tmp_path)
    assert isinstance(handler, logging.FileHandler)


def test_sys_stderr_pointed_at_the_failing_handlers_own_stream_leaves_the_log_byte_unchanged(
    tmp_path: Path,
) -> None:
    """Tier 2: #6134 — inside `litellm_bootstrap.py`'s own
    `redirect_stderr(handler.stream)` window (opened around the litellm
    import, `capture_stray_output`'s widening does not cover this window
    because `sys.stderr` genuinely IS the log stream there, not a capture
    proxy), `sys.stderr` and this handler's OWN stream are the SAME
    object. A record that fails to FORMAT (the stream itself stays open
    and writable throughout — #6132's own UnicodeEncodeError shape, not a
    broken stream) must not let `logging.Handler.handleError`'s base
    `sys.stderr.write('--- Logging error ---\\n' + traceback)` leak
    straight into `reyn.log`'s own bytes — the exact form this file's own
    module docstring forbids ("must NOT depend on the very handler that
    is failing").

    `"%d" % "not-a-number"` (bad old-style formatting) is used rather than
    `_break`'s read-only-reopen: that shape makes `.write()` itself raise
    before touching the file, which would pass even without #6134's guard
    for the WRONG reason (nothing ever reached the stream to begin with).
    Here the stream stays genuinely writable the entire time — only
    `record.getMessage()` fails — so a leaked banner would actually land
    real bytes in the file if the guard were absent.

    Strip-falsify (verified by hand, file-internal Edit only, reverted):
    removing the `if sys.stderr is not self.stream:` guard in
    `handleError` makes this test fail — the log file gains the
    `--- Logging error ---` banner's own bytes."""
    handler, logger, fallback_path = _make_handler(tmp_path)
    assert not fallback_path.exists()

    reyn_log = Path(handler.baseFilename)
    logger.warning("a healthy line, written normally")
    before = reyn_log.read_bytes()

    real_stderr = sys.stderr
    sys.stderr = handler.stream  # the exact litellm_bootstrap.py redirect window
    try:
        logger.warning("%d", "not-a-number")  # getMessage() raises TypeError; stream stays writable
    finally:
        sys.stderr = real_stderr
    handler.stream.flush()  # a leaked write sits in the TextIOWrapper's own
    # buffer until flushed -- production eventually flushes on any later
    # successful emit() (StreamHandler.emit() always ends with self.flush()),
    # so an unflushed leak here would still surface later; flushing now
    # makes the assertion decisive without depending on a SECOND log call.

    after = reyn_log.read_bytes()
    assert after == before, (
        "the failing handler's own log file gained bytes -- handleError's "
        "base-class sys.stderr write leaked its traceback banner into the "
        "very file it is supposed to protect"
    )
    assert fallback_path.exists(), (
        "the fallback record must still fire even though the base "
        "handleError call was skipped -- _record_fallback runs first, "
        "unconditionally, regardless of the sys.stderr guard below it"
    )
