"""Tier 2: #2095 — shell-hook consent routes through the intervention bus.

When an interactive surface is attached, a not-yet-allowlisted shell hook's
consent prompt is routed through the SAME ``RequestBus`` that ungated
permission-prompts use (→ the TUI Pending tab), instead of the stdin
``print``/``input`` that is invisible under a Textual app. When there is NO
interactive surface (headless / CI / mcp-serve), the runner degrades to its
pre-#2095 ``REYN_ACCEPT_HOOKS`` / fail-closed gate — the bus is NOT consulted.

No mocks: a real ``_RecordingBus`` implements the ``request(iv)`` contract and
returns a preset choice; a real ``NoopBackend`` executes the command; the
allowlist is a real file under ``tmp_path``. Whether the command actually RAN is
observed via a marker file it writes (shell_exec returns ``None`` either way, so
the return value alone can't distinguish approved-vs-skipped).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.hooks.shell_runner import _is_approved, _load_allowlist, run_shell_hook
from reyn.intervention_choices import ALWAYS, NO, YES
from reyn.security.sandbox import NoopBackend, SandboxPolicy
from reyn.user_intervention import InterventionAnswer, UserIntervention

_PY = sys.executable


class _RecordingBus:
    """A real ``RequestBus`` that records each iv and returns a preset choice.

    Concrete implementation of the ``request(iv)`` contract (NOT a mock) — the
    same shape the production ``AgentRequestBus`` exposes to the consent gate.
    """

    def __init__(self, choice_id: str | None) -> None:
        self._choice_id = choice_id
        self.seen: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.seen.append(iv)
        return InterventionAnswer(choice_id=self._choice_id)


def _policy() -> SandboxPolicy:
    return SandboxPolicy(network=False, allow_subprocess=False, timeout_seconds=10)


def _marker_command(marker: Path) -> str:
    script = f"open({str(marker)!r}, 'w').write('ran')"
    return f'{_PY} -c "{script}"'


async def _run(command: str, allowlist: Path, **kw) -> None:
    await run_shell_hook(
        command,
        event_context={"event": "turn_end"},
        timeout_seconds=10,
        sandbox_backend=NoopBackend(),
        sandbox_policy=_policy(),
        allowlist_path=allowlist,
        **kw,
    )


@pytest.mark.asyncio
async def test_consent_bus_always_records_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: interactive + ALWAYS → the hook runs AND the command is persisted
    to the allowlist (the "always" persistence), and the prompt was a
    ``permission.shell_hook`` intervention carrying the command."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(ALWAYS)

    await _run(command, allowlist, consent_bus=bus, interactive=True)

    assert marker.exists(), "ALWAYS should run the hook"
    assert bus.seen, "the consent prompt must route through the bus"
    assert bus.seen[0].kind == "permission.shell_hook"
    assert command in bus.seen[0].detail
    # Persisted: a second run would short-circuit on the allowlist.
    assert _is_approved(command, _load_allowlist(allowlist))


@pytest.mark.asyncio
async def test_consent_bus_yes_runs_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: interactive + YES → the hook runs once but is NOT persisted."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(YES)

    await _run(command, allowlist, consent_bus=bus, interactive=True)

    assert marker.exists(), "YES should run the hook"
    assert not _is_approved(command, _load_allowlist(allowlist)), "YES must not persist"


@pytest.mark.asyncio
async def test_consent_bus_no_denies_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: interactive + NO → the hook is skipped (the command never runs)."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(NO)

    await _run(command, allowlist, consent_bus=bus, interactive=True)

    assert bus.seen and bus.seen[0].kind == "permission.shell_hook", (
        "NO still routes through the bus"
    )
    assert not marker.exists(), "NO must skip the hook"


@pytest.mark.asyncio
async def test_headless_ignores_bus_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: the headless-preservation falsification — a bus is present but
    ``interactive=False`` (headless / CI / mcp-serve) → the bus is NOT consulted
    and the runner degrades to the pre-#2095 non-TTY fail-closed gate."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr("sys.stdin", _NonTTY())
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(ALWAYS)  # would approve IF consulted

    await _run(command, allowlist, consent_bus=bus, interactive=False)

    assert bus.seen == [], "headless must NOT consult the intervention bus"
    assert not marker.exists(), "headless non-TTY without accept flag → fail-closed"


@pytest.mark.asyncio
async def test_headless_accept_env_still_runs_without_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: REYN_ACCEPT_HOOKS=1 takes precedence over the bus path (unchanged
    CI behavior) — the hook runs and the bus is never consulted."""
    monkeypatch.setenv("REYN_ACCEPT_HOOKS", "1")
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(NO)  # would deny IF consulted

    await _run(command, allowlist, consent_bus=bus, interactive=True)

    assert bus.seen == [], "REYN_ACCEPT_HOOKS=1 short-circuits before the bus"
    assert marker.exists(), "accept-env should run the hook"


class _NonTTY:
    """Minimal ``sys.stdin`` replacement reporting non-TTY."""

    def isatty(self) -> bool:
        return False

    def read(self, *_):
        return ""
