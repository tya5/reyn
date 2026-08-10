"""Tier 2: httpx import is off the CLI cold-start path (`reyn --help` etc, #2930 companion).

``import httpx`` used to run eagerly on EVERY ``reyn`` invocation — via a
module-level ``from reyn.llm.llm import run_async`` in
``reyn.interfaces.cli.commands.chat`` / ``reyn.interfaces.cli.commands.mcp``,
which pulled in ``reyn.llm.llm``'s own module-level ``import httpx``. httpx's
own CLI pretty-printing subtree (``rich.progress`` / ``rich.syntax`` /
``pygments``) makes this cost ~90ms, ~25% of `reyn.interfaces.cli`'s total
import time, for a capability (network calls) the CLI entry never invokes.

This mirrors the ``ensure_litellm_ready`` chokepoint pattern (#2930,
``test_litellm_lazy_load.py``): `run_async` is now imported lazily inside the
functions that call it, and ``reyn.llm.llm``'s two ``isinstance(exc, httpx.X)``
exception-classification sites (``_is_llm_timeout_exc`` /
``_is_retryable_exc``) resolve the httpx exception classes lazily via
``_get_httpx_exc_types()`` (same shape as the existing
``_get_retryable_litellm_exceptions()`` for litellm).

Exercised via a real subprocess (not mocks) so ``sys.modules`` state is the
actual process-level fact, not an in-process assumption contaminated by an
earlier test's ``import httpx``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run(src_root: str, script: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": src_root}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_chat_command_module_import_does_not_import_httpx(out_of_process_reyn) -> None:
    """Tier 2: importing `reyn.interfaces.cli.commands.chat` leaves httpx unimported.

    FALSIFY: restoring the eager `from reyn.llm.llm import run_async` at module
    scope in chat.py (which pulls llm.py's `import httpx`) flips this RED —
    confirmed manually during development (see PR body).
    """
    script = """
        import sys
        import reyn.interfaces.cli.commands.chat  # noqa: F401
        assert "httpx" not in sys.modules, sorted(m for m in sys.modules if "httpx" in m)
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_mcp_command_module_import_does_not_import_httpx(out_of_process_reyn) -> None:
    """Tier 2: importing `reyn.interfaces.cli.commands.mcp` leaves httpx unimported."""
    script = """
        import sys
        import reyn.interfaces.cli.commands.mcp  # noqa: F401
        assert "httpx" not in sys.modules, sorted(m for m in sys.modules if "httpx" in m)
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_llm_module_import_does_not_import_httpx(out_of_process_reyn) -> None:
    """Tier 2: importing `reyn.llm.llm` directly leaves httpx unimported — the
    two `isinstance(exc, httpx.X)` exception-classification sites must resolve
    httpx lazily, not via a module-level `import httpx`."""
    script = """
        import sys
        import reyn.llm.llm  # noqa: F401
        assert "httpx" not in sys.modules, sorted(m for m in sys.modules if "httpx" in m)
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_help_invocation_does_not_import_httpx(out_of_process_reyn) -> None:
    """Tier 2: the full `reyn --help` entry path (the actual cold-start
    invocation this fix targets) never touches httpx.

    Decisive: runs `reyn._cli.main()` with `--help` (the CLI's own SystemExit
    on --help is caught) and asserts `sys.modules` is clean — the exact
    checkpoint the #2930-style cold-start sweep is measured against.
    """
    script = """
        import sys
        sys.argv = ["reyn", "--help"]
        from reyn._cli import main
        try:
            main()
        except SystemExit:
            pass
        assert "httpx" not in sys.modules, sorted(m for m in sys.modules if "httpx" in m)
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_httpx_exception_classification_still_resolves_lazily(out_of_process_reyn) -> None:
    """Tier 2: `_get_httpx_exc_types()` (the httpx-equivalent of
    `_get_retryable_litellm_exceptions`) imports httpx on first call and
    returns the real `(httpx.ConnectError, httpx.ReadTimeout)` pair, so the
    lazy-load refactor does not silently break the retry/timeout
    classification `_is_retryable_exc` / `_is_llm_timeout_exc` depend on.

    Behavioral tests for the classification itself already live in
    `tests/test_llm_call_retry.py` (test_httpx_errors_retried,
    test_is_retryable_exc_classification) — this test pins only the
    lazy-resolution invariant those tests rely on.
    """
    script = """
        import sys
        assert "httpx" not in sys.modules
        from reyn.llm.llm import _get_httpx_exc_types
        connect_exc, read_timeout_exc = _get_httpx_exc_types()
        assert "httpx" in sys.modules
        import httpx
        assert connect_exc is httpx.ConnectError
        assert read_timeout_exc is httpx.ReadTimeout
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
