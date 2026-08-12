"""Tier 2: interactive CUI routes logging to a file (no traceback leak into UI).

The inline CUI owns the terminal; a caught-error traceback logged via
logger.exception must NOT print into the live chat region.
`_setup_interactive_logging` redirects the root logger to .reyn/logs/reyn.log.
Global logging state is saved+restored so the assertion does not leak into the
rest of the suite.

perf (lazy-load litellm off the chat startup path): `_setup_interactive_logging`
no longer imports litellm — see `test_litellm_lazy_load.py` for the
sys.modules-clean-at-startup proof and the moved #2929 log-routing tests
(now targeting `reyn.llm.litellm_bootstrap.ensure_litellm_ready`, the first-
real-litellm-use chokepoint).
"""
from __future__ import annotations

import logging
import warnings

from reyn.interfaces.cli.commands.chat import _setup_interactive_logging


def test_interactive_logging_redirects_root_logger_to_file(tmp_path) -> None:
    """Tier 2: a WARNING record lands in .reyn/logs/reyn.log, not on stderr."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        _setup_interactive_logging(tmp_path)
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"
        targets = [getattr(h, "baseFilename", None) for h in root.handlers]
        assert str(log_file) in targets  # a FileHandler now targets the reyn log

        logging.getLogger("reyn.canary").warning("canary-marker-7f3a")
        for h in root.handlers:
            h.flush()
        assert "canary-marker-7f3a" in log_file.read_text()
    finally:
        # #4362: _setup_interactive_logging now also calls
        # logging.captureWarnings(True), whose own on/off guard
        # (logging._warnings_showwarning) is a THIRD piece of process-global
        # state alongside root's handlers/level — left uncleared here, an
        # earlier-run test's leftover guard silently no-ops the NEXT test's
        # own captureWarnings(True) call (found via
        # test_interactive_logging_routes_warnings_warn_to_the_file_not_stderr
        # failing only when run after this test, never in isolation).
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_interactive_logging_routes_warnings_warn_to_the_file_not_stderr(
    tmp_path, capsys,
) -> None:
    """Tier 2: #4362 — a bare `warnings.warn(...)` (the stdlib's own library-
    warning mechanism, not a logging call) also lands in .reyn/logs/reyn.log
    instead of stderr.

    This docstring's function-under-test already declared "route library
    warnings ... so they don't corrupt the live region" before #4362 —
    `logging.basicConfig` alone only ever redirected *logging* records, so a
    bare `warnings.warn` (e.g. the ResourceWarning an unclosed async client
    emits) still reached stderr uncaught. `logging.captureWarnings(True)`
    closes that gap.

    Both sides checked, not just "not on stderr" (test-review Q3: a capsys
    check alone stays green even if captureWarnings is silently dropped,
    because nothing here would force the warning to fire AND land somewhere
    observable) — the warning is actually fired via bare `warnings.warn`,
    then BOTH absence from stderr AND presence in the log file are asserted.
    `warnings.simplefilter("always")` forces the fire regardless of Python's
    default once-per-location dedup, so an earlier test in the same process
    already having triggered this exact warning can't make it silently not
    fire here.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        _setup_interactive_logging(tmp_path)
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"

        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(
                "resource-warning-marker-9c1e", category=ResourceWarning, stacklevel=1,
            )

        for h in root.handlers:
            h.flush()

        captured = capsys.readouterr()
        assert "resource-warning-marker-9c1e" not in captured.err
        assert "resource-warning-marker-9c1e" in log_file.read_text()
    finally:
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
