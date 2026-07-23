"""Tests for #1800 slice C / #2069 — exec-hook runner (exec + exec_capture,
renamed from ``shell_exec``/``shell_push`` in #3226 Phase 4 — naming honesty
only; ``run_shell_hook`` always argv-executed with ``shell=False``, never
``/bin/sh -c <string>``).

Coverage
--------
All tests use REAL subprocesses (``python -c`` one-liners, passed as argv —
#3226 Phase 4 argv-list-only payload) — no mocks of collaborators.  The
sandbox backend is NoopBackend (always available on every platform), which
exercises the real backend.run() path without requiring platform-specific
setup.

Tier 1 — Contract:
  - ``run_shell_hook`` is exported from ``reyn.hooks`` (public API surface).
  - ``exec`` mode (``capture_stdout=False``, the default): output is NOT
    parsed — run_shell_hook returns None (pure side-effect).
  - ``exec_capture`` mode (``capture_stdout=True``, #2069): an exit-0 run returns
    the decoded stdout; a non-zero exit returns None (fail-safe → skip push).
  - A command that reads stdin receives valid JSON context.
  - A command whose sleep exceeds the timeout → returns None, no crash.
  - Non-allowlisted command in non-TTY without REYN_ACCEPT_HOOKS → refuses
    (fail-closed) and returns None.

Filesystem isolation: allowlist tests point at a tmp_path file so
``~/.reyn/shell-hooks-allowlist.json`` is never touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from reyn.security.sandbox import NoopBackend, SandboxPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Python interpreter (same executable running pytest) — keeps tests hermetic
# across venvs.
_PY = sys.executable


def _noop_backend() -> NoopBackend:
    return NoopBackend()


def _policy(timeout: int = 10) -> SandboxPolicy:
    return SandboxPolicy(network=False, allow_subprocess=False, timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# Tier 1 — Contract: run_shell_hook is part of the public reyn.hooks API
# ---------------------------------------------------------------------------


def test_run_shell_hook_exported_from_reyn_hooks() -> None:
    """Tier 1: run_shell_hook is re-exported from reyn.hooks (public API surface)."""
    import reyn.hooks as hooks

    assert hasattr(hooks, "run_shell_hook")
    assert callable(hooks.run_shell_hook)


# ---------------------------------------------------------------------------
# Tier 1 — Contract: output is ignored — run_shell_hook always returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_ignored_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a command that writes JSON to stdout is NOT parsed as a push
    directive — run_shell_hook returns None regardless of output content.
    REYN_ACCEPT_HOOKS=1 simulates CI mode.
    """
    from reyn.hooks.shell_runner import run_shell_hook

    allowlist = tmp_path / "allowlist.json"
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")

    # Write a valid-looking push directive JSON to stdout — must be ignored.
    script = (
        "import json, sys; "
        "sys.stdout.write(json.dumps({'message': 'should be ignored', 'wake': True}))"
    )
    argv = [_PY, "-c", script]

    result = await run_shell_hook(
        argv,
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Tier 1 — Contract: capture_stdout (exec_capture, #2069) returns / fails-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_stdout_returns_decoded_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: capture_stdout=True (exec_capture) returns the decoded stdout of an
    exit-0 run — the caller parses it as a JSON push-directive (vs exec,
    which ignores output and returns None for the SAME command)."""
    from reyn.hooks.shell_runner import run_shell_hook

    allowlist = tmp_path / "allowlist.json"
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")

    directive = {"push_when": True, "wake": True, "message": "go"}
    script = f"import json,sys; sys.stdout.write(json.dumps({directive!r}))"
    argv = [_PY, "-c", script]

    result = await run_shell_hook(
        argv,
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
        capture_stdout=True,
    )

    assert result is not None
    assert json.loads(result) == directive


@pytest.mark.asyncio
async def test_capture_stdout_nonzero_exit_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: capture_stdout=True with a NON-ZERO exit returns None (fail-safe) —
    even if the command wrote to stdout, a failed run yields no push-directive."""
    from reyn.hooks.shell_runner import run_shell_hook

    allowlist = tmp_path / "allowlist.json"
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")

    # Writes a directive to stdout then exits non-zero → must NOT be returned.
    script = "import json,sys; sys.stdout.write('{\\\"message\\\": \\\"x\\\"}'); sys.exit(3)"
    argv = [_PY, "-c", script]

    result = await run_shell_hook(
        argv,
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
        capture_stdout=True,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Tier 1 — Contract: JSON context is delivered on stdin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_context_delivered_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: the hook subprocess receives event_context serialised as JSON on
    stdin.  A command that reads stdin + exits with code 0 iff the JSON is
    valid confirms delivery.  Exit code 0 = context arrived; non-zero = did not.
    """
    from reyn.hooks.shell_runner import run_shell_hook

    # Write a marker file if stdin contains valid JSON with the expected key.
    marker = tmp_path / "context_received.txt"
    allowlist = tmp_path / "allowlist.json"
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")

    script = (
        "import json, sys; "
        f"data = json.loads(sys.stdin.read()); "
        f"open({str(marker)!r}, 'w').write(data.get('event', '')) "
        "if 'event' in data else None"
    )
    argv = [_PY, "-c", script]

    await run_shell_hook(
        argv,
        event_context={"event": "skill_end", "skill": "my-skill"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
    )

    # The hook wrote the event name to the marker file — context was delivered.
    assert marker.exists(), "hook did not receive event_context on stdin"
    assert marker.read_text() == "skill_end"


# ---------------------------------------------------------------------------
# Tier 1 — Contract: timeout returns None, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_none_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a command that sleeps past the timeout returns None and does not
    crash or raise — the runner absorbs the timeout gracefully.
    """
    from reyn.hooks.shell_runner import run_shell_hook

    allowlist = tmp_path / "allowlist.json"
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")

    # sleep for 60 s but timeout is 1 s → times out.
    argv = [_PY, "-c", "import time; time.sleep(60)"]

    result = await run_shell_hook(
        argv,
        event_context={"event": "session_end"},
        timeout_seconds=1,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(timeout=1),
        allowlist_path=allowlist,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Tier 1 — Contract: consent fail-closed (non-allowlisted, non-TTY, no flag)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonapproved_command_nontty_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: a non-allowlisted command in a non-TTY environment without
    REYN_ACCEPT_HOOKS=1 is refused (fail-closed) and returns None.
    """
    from reyn.hooks.shell_runner import run_shell_hook

    allowlist = tmp_path / "allowlist.json"
    # Ensure allowlist is empty (no pre-existing approval).
    allowlist.write_text("[]", encoding="utf-8")

    # Simulate non-TTY: monkeypatch sys.stdin.isatty to return False.
    monkeypatch.setattr("sys.stdin", _FakeTTY(is_tty=False))
    # Ensure accept flag is NOT set.
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)

    argv = [_PY, "-c", "pass"]

    result = await run_shell_hook(
        argv,
        event_context={"event": "session_start"},
        timeout_seconds=10,
        sandbox_backend=_noop_backend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
    )

    # Refused — fail-closed.
    assert result is None


# ---------------------------------------------------------------------------
# Helper: fake stdin object with configurable isatty()
# ---------------------------------------------------------------------------


class _FakeTTY:
    """Minimal sys.stdin replacement for TTY-check tests."""

    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def read(self, *_):
        return ""
