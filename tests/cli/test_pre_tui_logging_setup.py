"""Tier 2: _setup_pre_tui_logging routes root logger to file, not stderr.

Pre-TUI log leak (observed: config-load warnings appear on raw terminal before
Textual launches because root logger → stderr by default). Fix: call
_setup_pre_tui_logging(project_root) before load_project_context in the TUI
startup path. This routes root logging to .reyn/logs/reyn.log.

Falsification:
- Without the fix the root logger has a StreamHandler(stderr) and no FileHandler.
  A warning emitted during config load would appear on the terminal.
- After _setup_pre_tui_logging the root logger has a FileHandler pointing to
  .reyn/logs/reyn.log and no StreamHandler writing to stderr.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def _get_root_handler_types(root_logger: logging.Logger) -> tuple[list, list]:
    """Return (file_handlers, stderr_stream_handlers) from root logger."""
    file_handlers = [h for h in root_logger.handlers if isinstance(h, logging.FileHandler)]
    stderr_handlers = [
        h for h in root_logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stderr
    ]
    return file_handlers, stderr_handlers


def test_setup_pre_tui_logging_adds_file_handler(tmp_path):
    """Tier 2: _setup_pre_tui_logging adds a FileHandler at .reyn/logs/reyn.log.

    Falsification: if the function were a no-op, file_handlers would be empty
    and this assertion would fail.
    """
    from reyn.interfaces.cli.commands.chat import _setup_pre_tui_logging

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        _setup_pre_tui_logging(tmp_path)
        file_handlers, _ = _get_root_handler_types(root)
        assert file_handlers, "expected at least one FileHandler after setup"
        log_path = tmp_path / ".reyn" / "logs" / "reyn.log"
        assert any(
            Path(h.baseFilename) == log_path for h in file_handlers
        ), f"expected FileHandler at {log_path}, got {[h.baseFilename for h in file_handlers]}"
    finally:
        # Restore root logger state so this test doesn't affect other tests.
        for h in root.handlers[:]:
            if h not in original_handlers:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_setup_pre_tui_logging_no_stderr_stream_handler(tmp_path):
    """Tier 2: _setup_pre_tui_logging leaves no StreamHandler writing to stderr.

    Falsification: if basicConfig(force=True) failed to remove the default
    stderr StreamHandler, a stderr_handler would be present and the assertion
    would fail — the leak would still exist.
    """
    from reyn.interfaces.cli.commands.chat import _setup_pre_tui_logging

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    # Simulate the default state: add a StreamHandler(stderr) as if logging
    # had been lazily initialised before our setup call.
    pre_handler = logging.StreamHandler(sys.stderr)
    root.addHandler(pre_handler)
    try:
        _setup_pre_tui_logging(tmp_path)
        _, stderr_handlers = _get_root_handler_types(root)
        assert not stderr_handlers, (
            f"expected no stderr StreamHandler after setup, got {stderr_handlers}"
        )
    finally:
        for h in root.handlers[:]:
            if h not in original_handlers:
                h.close()
                root.removeHandler(h)
        if pre_handler in root.handlers:
            root.removeHandler(pre_handler)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_setup_pre_tui_logging_warning_goes_to_file(tmp_path):
    """Tier 2: a WARNING emitted after setup appears in the log file, not stderr.

    End-to-end: calls _setup_pre_tui_logging, emits a sentinel warning via
    the same logger that _reconcile_embedding_class uses, confirms the warning
    is in the log file.

    Falsification: without setup, the warning would go to stderr (not the file)
    and the log file would be empty — the assertion would fail.
    """
    from reyn.interfaces.cli.commands.chat import _setup_pre_tui_logging

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        _setup_pre_tui_logging(tmp_path)
        sentinel = "test_pre_tui_log_leak_sentinel_xq7z"
        logging.getLogger("reyn.config.loader").warning(sentinel)
        # Flush any buffered writes
        for h in root.handlers:
            h.flush()
        log_path = tmp_path / ".reyn" / "logs" / "reyn.log"
        assert log_path.exists(), f"expected log file at {log_path}"
        content = log_path.read_text(encoding="utf-8")
        assert sentinel in content, (
            f"expected sentinel in log file, got: {content!r}"
        )
    finally:
        for h in root.handlers[:]:
            if h not in original_handlers:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
