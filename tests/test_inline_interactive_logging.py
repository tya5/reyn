"""Tier 2: interactive CUI routes logging to a file (no traceback leak into UI).

The inline CUI owns the terminal; a caught-error traceback logged via
logger.exception (or a litellm banner) must NOT print into the live chat region.
`_setup_interactive_logging` redirects the root logger to .reyn/logs/reyn.log and
quiets litellm's direct banners. Global logging/litellm state is saved+restored
so the assertion does not leak into the rest of the suite.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


def test_interactive_logging_routes_litellm_logger_to_file_not_console(
    tmp_path,
) -> None:
    """Tier 2: litellm's "LiteLLM" logger has no console handler after setup —
    its own module-level ``StreamHandler`` (attached at ``import litellm`` time,
    which already ran earlier in this test process) is stripped, and a runtime
    warning it emits lands in reyn.log via root-logger propagation instead.
    FALSIFY: without `_litellm_import_logs_to_file`'s exit-time strip, the
    "LiteLLM" logger keeps its own StreamHandler (→ stderr) alongside/instead of
    reaching the file, so this assertion would fail.
    """
    import litellm  # noqa: F401 — ensures the "LiteLLM" logger + its handler exist

    root = logging.getLogger()
    litellm_logger = logging.getLogger("LiteLLM")
    saved_root_handlers, saved_root_level = root.handlers[:], root.level
    saved_litellm_handlers = litellm_logger.handlers[:]
    saved_litellm_propagate = litellm_logger.propagate
    try:
        _setup_interactive_logging(tmp_path)
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"

        # No leftover console (StreamHandler) sink on litellm's own logger.
        assert not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in litellm_logger.handlers
        )
        assert litellm_logger.propagate is True

        litellm_logger.warning("runtime-marker-9a1b-not-a-mock")
        for h in root.handlers:
            h.flush()
        assert "runtime-marker-9a1b-not-a-mock" in log_file.read_text()
    finally:
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        litellm_logger.handlers[:] = saved_litellm_handlers
        litellm_logger.propagate = saved_litellm_propagate


def test_interactive_logging_routes_litellm_import_time_warning_to_file(
    tmp_path,
) -> None:
    """Tier 2: litellm's cost-map-fetch-failure warning, emitted synchronously
    *during* ``import litellm`` (not a runtime call reyn controls, so exercised
    via a real subprocess), lands in reyn.log and NOT on stderr.

    Forces the fetch-failure path explicitly (overrides the #2928
    ``LITELLM_LOCAL_MODEL_COST_MAP`` setdefault so the remote fetch is
    attempted) against an unreachable URL, so the warning is guaranteed to
    fire — this is the decisive evidence for the import-time handler-
    construction patch in `_litellm_import_logs_to_file`, independent of
    whether the #2928 env var happens to skip the fetch entirely on a given
    run. FALSIFY: without the patch, litellm's own StreamHandler prints this
    warning straight to stderr — captured via a real subprocess, no mocks.
    """
    project_root = tmp_path
    script = textwrap.dedent(
        """
        import os
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "False"
        os.environ["LITELLM_MODEL_COST_MAP_URL"] = "http://127.0.0.1:1/unreachable.json"

        from pathlib import Path
        from reyn.interfaces.cli.commands.chat import _setup_interactive_logging

        _setup_interactive_logging(Path(%r))
        """
        % str(project_root)
    )
    # This test's own `src/` (not whatever path an editable install's
    # site-packages .pth happens to point at — e.g. a sibling git worktree's
    # checkout) must win on the child's sys.path, so the subprocess exercises
    # THIS checkout's `_setup_interactive_logging`/`_litellm_import_logs_to_file`.
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src_dir)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    log_file = project_root / ".reyn" / "logs" / "reyn.log"
    log_text = log_file.read_text()
    assert "Failed to fetch remote model cost map" in log_text
    assert "Failed to fetch remote model cost map" not in result.stderr
