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

#5873 (owner-hit — "放置してるだけで reyn.log 肥大化してシステム止まらない
ようにしてね"): the handler installed here is now a size-bounded
``RotatingFileHandler``, not a bare unrotated ``FileHandler``. The tests
below pin the 5 acceptance witnesses from that fix.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import warnings

from reyn.config.chat import LogsConfig
from reyn.interfaces.cli.commands.chat import (
    _apply_logs_config,
    _setup_interactive_logging,
)
from tests._support.paths import REPO_ROOT


def test_interactive_logging_redirects_root_logger_to_file(tmp_path) -> None:
    """Tier 2: a WARNING record lands in .reyn/logs/reyn.log, not on stderr."""
    from reyn.runtime import stall_trace

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_registered_path = stall_trace.find_file_handler_path()
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
        # #5873 follow-up: _setup_interactive_logging now also registers its
        # handler's path with stall_trace (a FOURTH piece of process-global
        # state) — left uncleared here, this leaks into other test files
        # that assert stall_trace.find_file_handler_path() is None.
        stall_trace.register_file_handler_path(saved_registered_path)


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
    from reyn.runtime import stall_trace

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_registered_path = stall_trace.find_file_handler_path()
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
        stall_trace.register_file_handler_path(saved_registered_path)


# ── #5873: size-based rotation (5 acceptance witnesses) ─────────────────────


def test_small_max_bytes_rotates_and_bounds_the_live_file(tmp_path) -> None:
    """Tier 2: #5873 accept ① — a small configured max_bytes, written past
    repeatedly, produces a rotated reyn.log.1 AND keeps the LIVE reyn.log
    from growing past max_bytes without bound. strip: reverting the
    RotatingFileHandler to a bare FileHandler (this fix's own before-state)
    never produces a .1 file and lets reyn.log grow past max_bytes freely
    (verified during this fix)."""
    from reyn.runtime import stall_trace

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_registered_path = stall_trace.find_file_handler_path()
    try:
        _setup_interactive_logging(tmp_path)
        _apply_logs_config(LogsConfig(max_bytes=4096, backup_count=2))
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"
        logger = logging.getLogger("reyn.rotation_test")
        # ~60 bytes/record with the timestamp+level prefix; 400 records is
        # ~24 KiB unrotated — several multiples of the 4 KiB cap, so a
        # genuinely bounded file (not just "happens to be small this run")
        # is the only way to stay under it.
        for i in range(400):
            logger.warning("x" * 50 + f" line {i}")
        for h in root.handlers:
            h.flush()
        assert (tmp_path / ".reyn" / "logs" / "reyn.log.1").exists()
        assert log_file.stat().st_size < 4096 * 2
    finally:
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        stall_trace.register_file_handler_path(saved_registered_path)


def test_backup_count_bounds_total_disk_usage(tmp_path) -> None:
    """Tier 2: #5873 accept ② — old rotated files beyond backup_count are
    deleted (never reyn.log.<backup_count+1>), and the total on-disk size
    across reyn.log + every reyn.log.N stays <= max_bytes *
    (backup_count + 1)."""
    from reyn.runtime import stall_trace

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_registered_path = stall_trace.find_file_handler_path()
    try:
        _setup_interactive_logging(tmp_path)
        _apply_logs_config(LogsConfig(max_bytes=2048, backup_count=2))
        logger = logging.getLogger("reyn.rotation_test2")
        # Enough volume to roll past backup_count+1 generations several
        # times over (2000 records * ~60 bytes ~= 120 KiB, vs a 6 KiB cap).
        for i in range(2000):
            logger.warning("y" * 50 + f" line {i}")
        for h in root.handlers:
            h.flush()
        log_dir = tmp_path / ".reyn" / "logs"
        assert not (log_dir / "reyn.log.3").exists()
        total = sum(f.stat().st_size for f in log_dir.glob("reyn.log*"))
        # (backup_count + 1) capped files, plus slack for the live file's
        # own in-flight overshoot (see the previous test's own margin).
        assert total <= 2048 * 3 + 2048
    finally:
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        stall_trace.register_file_handler_path(saved_registered_path)


def test_no_bare_basicconfig_filename_call_remains_in_src() -> None:
    """Tier 2: #5873 accept ③ — exactly one function
    (``_setup_interactive_logging``) installs the interactive log
    redirect, at both of this module's own call sites; no bare
    ``logging.basicConfig(filename=...)`` CALL (the un-rotated shape this
    fix replaces) remains anywhere in ``src/``. A comment/docstring MENTION
    of the old literal (``litellm_bootstrap.py``'s own historical note on
    why it used to scan ``handlers[0]``) is not a call site and is
    deliberately excluded — the accept criterion is about live code, not
    prose that documents what code used to do."""
    result = subprocess.run(
        ["git", "grep", "-n", "basicConfig(", "--", "src/"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    hits = []
    for line in result.stdout.splitlines():
        _, _, code = line.partition(":")  # path:lineno:code -> partition once more
        _, _, code = code.partition(":")
        if "filename=" in line and not code.strip().startswith("#"):
            hits.append(line)
    assert hits == []


def test_logs_config_typo_is_an_unknown_key_warning() -> None:
    """Tier 2: #5873 accept ④ — a typo'd `logs:` sub-key (`max_byte`
    instead of `max_bytes`) is caught as an unknown config key, the same
    mechanism every other typed `ReynConfig` field gets automatically
    (``config_schema`` walks ``ReynConfig``'s own dataclass fields — see
    ``LogsConfig``'s own registration, unlike `permissions:`/`sandbox:`,
    needs no separate #5849③-style registry since it is a real dataclass
    field, not a raw dict)."""
    from reyn.config import config_schema

    hits = config_schema.unknown_config_keys({"logs": {"max_byte": 1024}})
    assert "logs.max_byte" in hits


def test_existing_file_handler_readers_still_find_the_rotating_handler(
    tmp_path,
) -> None:
    """Tier 2: #5873 accept ⑤ — the two existing readers that structurally
    detect the interactive log handler (``stall_trace.find_file_handler_path``
    and ``litellm_bootstrap._litellm_import_logs_to_file``) still find it
    once it is a ``RotatingFileHandler``.

    #5873 follow-up (architect-requested, riding with #5877):
    ``find_file_handler_path`` no longer scans ``logging.getLogger().
    handlers`` for a path-shape match — it returns whatever
    ``_setup_interactive_logging`` last registered via
    ``stall_trace.register_file_handler_path``. This test now pins THAT
    contract (the registered path equals the real log file path), while
    ``_litellm_import_logs_to_file`` is still checked against the real,
    structural ``isinstance(handler, logging.FileHandler)`` reader
    unchanged by this follow-up — a ``RotatingFileHandler`` is a
    ``FileHandler`` subclass, so that check still passes unchanged."""
    from reyn.llm.litellm_bootstrap import _litellm_import_logs_to_file
    from reyn.runtime import stall_trace

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_registered_path = stall_trace.find_file_handler_path()
    try:
        _setup_interactive_logging(tmp_path)
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"

        assert stall_trace.find_file_handler_path() == str(log_file)

        with _litellm_import_logs_to_file():
            print("litellm-reader-marker-2b91", file=sys.stdout)
        for h in root.handlers:
            h.flush()
        assert "litellm-reader-marker-2b91" in log_file.read_text()
    finally:
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        stall_trace.register_file_handler_path(saved_registered_path)
