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
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
