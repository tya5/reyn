"""Tier 2: #2095 — shell-hook consent routes through the intervention bus.

When an answering surface is attached, a not-yet-allowlisted shell hook's
consent prompt is routed through the SAME ``RequestBus`` that ungated
permission-prompts use (→ the TUI Pending tab), instead of the stdin
``print``/``input`` that is invisible under a Textual app. The ``HookDispatcher``
passes a non-None ``consent_bus`` to the runner ONLY when a live intervention
listener is registered (``consent_gate``); otherwise the runner takes its
pre-#2095 stdin / fail-closed path — so plain ``mcp-serve`` / headless (no
listener) and ``reyn run`` on a TTY (no listener) all preserve the old behavior.

No mocks: a real ``_RecordingBus`` implements the ``request(iv)`` contract; a
real ``NoopBackend`` executes the command; the allowlist is a real file. Whether
the command actually RAN is observed via a marker file it writes (shell_exec
returns ``None`` either way, so the return value can't distinguish
approved-vs-skipped).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef
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


# ── runner: consent_bus present → route through the bus ──────────────────────


@pytest.mark.asyncio
async def test_consent_bus_always_records_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a consent_bus + ALWAYS → the hook runs AND the command is
    persisted to the allowlist (the "always" persistence), and the prompt was a
    ``permission.shell_hook`` intervention carrying the command."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(ALWAYS)

    await _run(command, allowlist, consent_bus=bus)

    assert marker.exists(), "ALWAYS should run the hook"
    assert bus.seen, "the consent prompt must route through the bus"
    assert bus.seen[0].kind == "permission.shell_hook"
    assert command in bus.seen[0].detail
    assert _is_approved(command, _load_allowlist(allowlist))


@pytest.mark.asyncio
async def test_consent_prompt_names_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: #2095 P2 — when a ``hook_name`` is supplied, the consent prompt
    identifies WHICH hook is asking; without one it stays generic."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)

    named = _RecordingBus(NO)
    await _run(command, allowlist, consent_bus=named, hook_name="nightly-sync")
    assert "nightly-sync" in named.seen[0].prompt

    anon = _RecordingBus(NO)
    await _run(command, allowlist, consent_bus=anon, hook_name=None)
    assert "nightly-sync" not in anon.seen[0].prompt
    assert "shell hook" in anon.seen[0].prompt.lower()


@pytest.mark.asyncio
async def test_consent_bus_yes_runs_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a consent_bus + YES → the hook runs once but is NOT persisted."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(YES)

    await _run(command, allowlist, consent_bus=bus)

    assert marker.exists(), "YES should run the hook"
    assert not _is_approved(command, _load_allowlist(allowlist)), "YES must not persist"


@pytest.mark.asyncio
async def test_consent_bus_no_denies_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: a consent_bus + NO → the hook is skipped (the command never runs)."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(NO)

    await _run(command, allowlist, consent_bus=bus)

    assert bus.seen and bus.seen[0].kind == "permission.shell_hook", (
        "NO still routes through the bus"
    )
    assert not marker.exists(), "NO must skip the hook"


@pytest.mark.asyncio
async def test_consent_bus_empty_answer_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: an empty answer (choice_id=None — e.g. the iv was parked stalled
    when its origin channel closed) → deny + skip (fail-safe)."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)
    bus = _RecordingBus(None)  # empty answer

    await _run(command, allowlist, consent_bus=bus)

    assert not marker.exists(), "an unanswered/empty consent must skip the hook"


@pytest.mark.asyncio
async def test_no_bus_nontty_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: no consent_bus + non-TTY + no accept flag → fail-closed (the
    pre-#2095 headless gate, unchanged)."""
    monkeypatch.delenv("REYN_ACCEPT_HOOKS", raising=False)
    monkeypatch.setattr("sys.stdin", _NonTTY())
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    marker = tmp_path / "ran.txt"
    command = _marker_command(marker)

    await _run(command, allowlist, consent_bus=None)

    assert not marker.exists(), "no bus + non-TTY → fail-closed"


@pytest.mark.asyncio
async def test_accept_env_short_circuits_before_bus(
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

    await _run(command, allowlist, consent_bus=bus)

    assert bus.seen == [], "REYN_ACCEPT_HOOKS=1 short-circuits before the bus"
    assert marker.exists(), "accept-env should run the hook"


# ── dispatcher: the consent_gate decides whether a bus reaches the runner ─────


class _RecordingShell:
    """A real run_shell seam that records the consent_bus + hook_name it got."""

    def __init__(self) -> None:
        self.consent_buses: list[object] = []
        self.hook_names: list[object] = []

    async def __call__(self, *args, **kwargs):
        self.consent_buses.append(kwargs.get("consent_bus"))
        self.hook_names.append(kwargs.get("hook_name"))
        return None


def _shell_exec_dispatcher(*, gate, run_shell, bus, hook_name="e2e-probe") -> HookDispatcher:
    async def _noop(*_a, **_k):
        return None

    reg = HookRegistry([HookDef(on="turn_end", name=hook_name, shell_exec="echo hi")])
    return HookDispatcher(
        reg,
        put_inbox=_noop,
        stage_next_turn_context=_noop,
        run_shell=run_shell,
        consent_bus=bus,
        consent_gate=gate,
    )


@pytest.mark.asyncio
async def test_dispatcher_passes_bus_when_listener_present() -> None:
    """Tier 2: when ``consent_gate()`` is true (a listener is attached —
    TUI/web), the dispatcher hands the bus to the runner."""
    bus = _RecordingBus(YES)
    shell = _RecordingShell()
    disp = _shell_exec_dispatcher(gate=lambda: True, run_shell=shell, bus=bus)

    await disp.dispatch("turn_end", {})

    assert shell.consent_buses == [bus]
    # The dispatcher wires the hook's name through for the consent prompt (P2).
    assert shell.hook_names == ["e2e-probe"]


@pytest.mark.asyncio
async def test_dispatcher_withholds_bus_when_no_listener() -> None:
    """Tier 2: the mcp-serve / headless arm — when ``consent_gate()`` is false (no
    listener — plain mcp-serve, headless, reyn-run-no-listener), the dispatcher
    passes ``consent_bus=None`` so the runner takes its stdin / fail-closed path
    and never blocks on an unanswerable bus future."""
    bus = _RecordingBus(YES)
    shell = _RecordingShell()
    disp = _shell_exec_dispatcher(gate=lambda: False, run_shell=shell, bus=bus)

    await disp.dispatch("turn_end", {})

    assert shell.consent_buses == [None]


class _NonTTY:
    """Minimal ``sys.stdin`` replacement reporting non-TTY."""

    def isatty(self) -> bool:
        return False

    def read(self, *_):
        return ""
