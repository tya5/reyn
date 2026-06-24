"""Tier 2: #2095 P3 — a shell-hook execution surfaces as a P6 event.

When a shell hook actually runs (consent passed — incl. a silent allowlisted /
accepted auto-run), the runner emits ``hook_shell_executed`` through the session
event sink so the events tab shows it instead of it being an invisible
side-effect. A refused / skipped hook (nothing ran) emits nothing.

No mocks: a real ``_RecordingEvents`` callable implements the sink contract; a
real ``NoopBackend`` executes the command; the allowlist is a real tmp file. The
events-tab hint is exercised through the real ``_event_hint`` renderer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef
from reyn.hooks.shell_runner import run_shell_hook
from reyn.interfaces.tui.widgets.right_panel.events_tab import _event_hint
from reyn.security.sandbox import NoopBackend, SandboxPolicy

_PY = sys.executable


class _RecordingEvents:
    """A real ``emit_event(type, **data)`` sink (NOT a mock)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, **data) -> None:
        self.events.append((event_type, data))


def _policy() -> SandboxPolicy:
    return SandboxPolicy(network=False, allow_subprocess=False, timeout_seconds=10)


async def _run(command: str, allowlist: Path, **kw):
    return await run_shell_hook(
        command,
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=NoopBackend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
        **kw,
    )


def _executed(sink: _RecordingEvents) -> list[dict]:
    return [d for t, d in sink.events if t == "hook_shell_executed"]


@pytest.mark.asyncio
async def test_runner_emits_on_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: an approved shell-hook run emits ``hook_shell_executed`` carrying
    the command, mode and (zero) returncode."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")  # auto-approve → it runs
    allowlist = tmp_path / "a.json"
    allowlist.write_text("[]", encoding="utf-8")
    sink = _RecordingEvents()
    cmd = f'{_PY} -c "pass"'

    await _run(cmd, allowlist, emit_event=sink)

    ev = _executed(sink)
    assert ev, "an executed shell hook must emit hook_shell_executed"
    assert ev[0]["command"] == cmd
    assert ev[0]["mode"] == "shell_exec"
    assert ev[0]["returncode"] == 0


@pytest.mark.asyncio
async def test_runner_emits_returncode_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a non-zero exit still emits the event with the real returncode (the
    command ran — it just failed)."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    allowlist = tmp_path / "a.json"
    allowlist.write_text("[]", encoding="utf-8")
    sink = _RecordingEvents()

    await _run(f'{_PY} -c "import sys; sys.exit(3)"', allowlist, emit_event=sink)

    ev = _executed(sink)
    assert ev and ev[0]["returncode"] == 3


@pytest.mark.asyncio
async def test_runner_no_emit_when_consent_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a refused consent (non-TTY, no accept flag, no bus → fail-closed)
    runs nothing, so NO ``hook_shell_executed`` is emitted."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr("sys.stdin", _NonTTY())
    allowlist = tmp_path / "a.json"
    allowlist.write_text("[]", encoding="utf-8")
    sink = _RecordingEvents()

    await _run(f'{_PY} -c "pass"', allowlist, emit_event=sink)

    assert sink.events == [], "a skipped hook must not emit an execution event"


@pytest.mark.asyncio
async def test_dispatcher_threads_emit_event() -> None:
    """Tier 2: the dispatcher wires its ``emit_event`` sink through to the runner."""
    seen: dict = {}

    async def fake_run(*_a, **kwargs):
        seen.update(kwargs)
        return None

    async def _noop(*_a, **_k):
        return None

    sink = _RecordingEvents()
    reg = HookRegistry([HookDef(on="turn_end", shell_exec="echo hi")])
    disp = HookDispatcher(
        reg, put_inbox=_noop, stage_next_turn_context=_noop,
        run_shell=fake_run, emit_event=sink,
    )

    await disp.dispatch("turn_end", {})

    assert seen.get("emit_event") is sink


def test_event_hint_renders_shell_execution() -> None:
    """Tier 2: the events-tab hint summarizes a hook_shell_executed event, and a
    non-zero returncode surfaces."""
    ok = _event_hint({
        "type": "hook_shell_executed",
        "data": {"command": "echo hi", "mode": "shell_exec", "returncode": 0},
    })
    assert "echo hi" in ok
    assert "shell_exec" in ok
    assert "rc=" not in ok  # a clean exit shows no rc suffix

    failed = _event_hint({
        "type": "hook_shell_executed",
        "data": {"command": "x", "mode": "shell_exec", "returncode": 3},
    })
    assert "rc=3" in failed


class _NonTTY:
    """Minimal ``sys.stdin`` replacement reporting non-TTY."""

    def isatty(self) -> bool:
        return False

    def read(self, *_):
        return ""
