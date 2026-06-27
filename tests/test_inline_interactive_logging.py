"""Tier 2: interactive CUI routes logging to a file (no traceback leak into UI).

The inline CUI owns the terminal; a caught-error traceback logged via
logger.exception (or a litellm banner) must NOT print into the live chat region.
`_setup_interactive_logging` redirects the root logger to .reyn/logs/reyn.log and
quiets litellm's direct banners. Global logging/litellm state is saved+restored
so the assertion does not leak into the rest of the suite.
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


def test_interactive_logging_quiets_litellm_banners(tmp_path) -> None:
    """Tier 2: litellm's direct stderr banners are suppressed for the CUI."""
    import litellm
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_suppress = getattr(litellm, "suppress_debug_info", False)
    try:
        _setup_interactive_logging(tmp_path)
        assert litellm.suppress_debug_info is True
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        litellm.suppress_debug_info = saved_suppress
