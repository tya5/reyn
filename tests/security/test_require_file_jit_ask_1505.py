"""Tier 2: #1505 PR-A — require_file_write/read JIT ask + config deny.

require_file_write and require_file_read are now async and accept a
``bus: RequestBus | None`` parameter, mirroring require_http_get (FP-0022).

Behaviour under test:
- bus≠None + outside zone + unapproved → JIT prompt fires, approval persists
- bus≠None + outside zone + user denies → PermissionError after prompt
- bus=None + outside zone → PermissionError (non-interactive, no prompt)
- config ``file.write: deny`` / ``file.read: deny`` → PermissionError even
  for default-zone paths (config deny overrides zone)
- default-zone paths still pass silently (regression guard)

No mocks. Real PermissionResolver + _FakeBus (non-mock fake).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.intervention_choices import JUST_PATH, YES
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _FakeBus:
    """Real RequestBus-compatible fake that pre-answers with a scripted choice."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _resolver(
    tmp_path: Path,
    *,
    config: dict | None = None,
    interactive: bool = True,
) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=interactive,
    )


# ── require_file_write JIT ask ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_write_jit_ask_fires_and_approves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — require_file_write with bus≠None prompts JIT and
    approves when user answers YES; no PermissionError raised."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    bus = _FakeBus(YES)
    outside_path = str(tmp_path / "junk" / "README.md")

    # Must not raise — user said yes
    await r.require_file_write(PermissionDecl(), outside_path, "skill_x", bus=bus)

    # Prompt was shown exactly once
    (ask,) = bus.asks
    assert "README.md" in ask.prompt or "README.md" in ask.detail


@pytest.mark.asyncio
async def test_file_write_jit_ask_persist_approves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — JUST_PATH answer persists approval; second call passes
    without prompting (non-default value: JUST_PATH ≠ default YES)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    outside_path = str(tmp_path / "output" / "result.txt")

    # First call: user picks JUST_PATH (persists)
    bus1 = _FakeBus(JUST_PATH)
    await r.require_file_write(PermissionDecl(), outside_path, "skill_x", bus=bus1)
    (ask,) = bus1.asks  # exactly one prompt fired

    # Second call: already approved — no prompt
    bus2 = _FakeBus("never_called")
    await r.require_file_write(PermissionDecl(), outside_path, "skill_x", bus=bus2)
    assert not bus2.asks


@pytest.mark.asyncio
async def test_file_write_jit_ask_deny_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — user denies JIT prompt → PermissionError raised."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    bus = _FakeBus("no")
    outside_path = str(tmp_path / "denied" / "file.txt")

    with pytest.raises(PermissionError, match="was not approved"):
        await r.require_file_write(PermissionDecl(), outside_path, "skill_x", bus=bus)

    # Prompt still fired (user was asked and said no)
    (ask,) = bus.asks


@pytest.mark.asyncio
async def test_file_write_bus_none_denies_outside_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — bus=None + outside zone → PermissionError (no prompt,
    non-interactive / eval context behaviour preserved)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path, interactive=False)
    outside_path = str(tmp_path / "junk" / "README.md")

    with pytest.raises(PermissionError, match="was not approved"):
        await r.require_file_write(PermissionDecl(), outside_path, "skill_x", bus=None)


@pytest.mark.asyncio
async def test_file_write_default_zone_passes_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — .reyn/ zone write still passes without bus or prompt
    (regression guard: JIT ask must not break default-zone behaviour)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    # No bus, in default zone → must not raise
    await r.require_file_write(PermissionDecl(), ".reyn/scratch/notes.txt", "skill_x")


# ── require_file_write config deny ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_write_config_deny_blocks_default_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — file.write: deny in config denies even .reyn/ zone writes
    (config deny overrides the default zone — mirrors require_http_get)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path, config={"file.write": "deny"})

    with pytest.raises(PermissionError, match="denied by config"):
        await r.require_file_write(PermissionDecl(), ".reyn/scratch/notes.txt", "skill_x")


@pytest.mark.asyncio
async def test_file_write_config_deny_blocks_outside_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — file.write: deny also blocks outside-zone writes
    (no prompt even when bus is provided)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path, config={"file.write": "deny"})
    bus = _FakeBus(YES)  # would approve if asked

    with pytest.raises(PermissionError, match="denied by config"):
        await r.require_file_write(PermissionDecl(), str(tmp_path / "out.txt"), "skill_x", bus=bus)

    # Bus was NOT consulted — config deny short-circuits before any prompt
    assert not bus.asks


# ── require_file_read JIT ask ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_read_jit_ask_fires_and_approves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — require_file_read with bus≠None prompts JIT and
    approves when user answers YES; no PermissionError raised."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    bus = _FakeBus(YES)
    outside_path = str(Path("/tmp") / "external_data.json")

    await r.require_file_read(PermissionDecl(), outside_path, "skill_x", bus=bus)

    (ask,) = bus.asks
    assert "external_data.json" in ask.prompt or "external_data.json" in ask.detail


@pytest.mark.asyncio
async def test_file_read_bus_none_denies_outside_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — require_file_read bus=None + outside zone → PermissionError."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path, interactive=False)
    outside_path = str(Path("/tmp") / "external.json")

    with pytest.raises(PermissionError, match="was not approved"):
        await r.require_file_read(PermissionDecl(), outside_path, "skill_x", bus=None)


@pytest.mark.asyncio
async def test_file_read_config_deny_blocks_cwd_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — file.read: deny in config denies even CWD-zone reads."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path, config={"file.read": "deny"})

    with pytest.raises(PermissionError, match="denied by config"):
        await r.require_file_read(PermissionDecl(), str(tmp_path / "src" / "main.py"), "skill_x")


@pytest.mark.asyncio
async def test_file_read_default_zone_passes_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 — CWD-zone read passes without bus (regression guard)."""
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    await r.require_file_read(PermissionDecl(), str(tmp_path / "README.md"), "skill_x")


# ── eval_builder zone regression guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_write_dot_reyn_evals_passes_bus_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #1505 PR-B — eval_builder's .reyn/evals/<name>/eval.md write passes
    with bus=None (non-interactive / eval pipeline), no prompt needed.

    Guards the zone change: .reyn/ is the default write zone; .reyn/evals/<name>/eval.md
    is in-zone and must never require a grant or interactive prompt.
    """
    monkeypatch.chdir(tmp_path)
    r = _resolver(tmp_path)
    eval_path = ".reyn/evals/my_skill/eval.md"

    # Must not raise — in-zone, bus=None is fine for default-zone paths
    await r.require_file_write(PermissionDecl(), eval_path, "eval_builder", bus=None)
