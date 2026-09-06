"""Tier 2: PermissionResolver.require_network (#5825①, architect ruling
2026-09-06) — the host-less network-request gate for `sandboxed_exec`.

Mirrors `test_1199_s31b2c2_http_get.py` / `test_require_file_jit_ask_1505.py`'s
own pattern: a real `PermissionResolver` + a real-`RequestBus`-compatible
Fake that pre-answers a scripted choice — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.intervention_choices import ALWAYS, NO, YES
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


def _resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {}, project_root=tmp_path, interactive=True,
    )


@pytest.mark.asyncio
async def test_config_deny_denies_without_asking(tmp_path: Path) -> None:
    """Tier 2: permissions.network: deny (the floor) raises without ever
    touching bus — the operator's floor always wins, before any ask."""
    r = _resolver(tmp_path, config={"network": "deny"})
    bus = _FakeBus(YES)  # would grant if asked — proves it was never asked
    with pytest.raises(PermissionError, match="denied by config"):
        await r.require_network(PermissionDecl(), bus, "skill", argv=["curl", "x"])
    assert bus.asks == []


@pytest.mark.asyncio
async def test_config_allow_grants_without_asking(tmp_path: Path) -> None:
    """Tier 2: permissions.network: allow (config pre-approval) passes
    silently, no ask — even with no bus at all."""
    r = _resolver(tmp_path, config={"network": "allow"})
    await r.require_network(PermissionDecl(), None, "skill", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_no_bus_and_no_prior_grant_denies(tmp_path: Path) -> None:
    """Tier 2: bus=None with no config/ledger grant denies — the same
    "bus=None is not a pause, it's a deny" posture require_http_get's own
    legacy-compat path and require_file_write already apply."""
    r = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="requires an interactive prompt"):
        await r.require_network(PermissionDecl(), None, "skill", argv=["curl", "x"])


@pytest.mark.asyncio
async def test_ask_fires_once_and_yes_grants(tmp_path: Path) -> None:
    """Tier 2: an interactive bus asks exactly once; YES grants
    (session-only — not yet persisted, see the ALWAYS test below)."""
    r = _resolver(tmp_path)
    bus = _FakeBus(YES)
    await r.require_network(PermissionDecl(), bus, "skill", argv=["curl", "x"])
    (_ask,) = bus.asks  # exactly one — tuple-unpack raises on any other count


@pytest.mark.asyncio
async def test_ask_no_denies(tmp_path: Path) -> None:
    """Tier 2: NO denies with a PermissionError, after exactly one ask —
    the request never silently falls through to running closed."""
    r = _resolver(tmp_path)
    bus = _FakeBus(NO)
    with pytest.raises(PermissionError, match="denied"):
        await r.require_network(PermissionDecl(), bus, "skill", argv=["curl", "x"])
    (_ask,) = bus.asks  # exactly one — tuple-unpack raises on any other count


@pytest.mark.asyncio
async def test_always_persists_ledger_and_a_later_headless_call_passes_silently(
    tmp_path: Path,
) -> None:
    """Tier 2: ALWAYS persists to the `<actor>/sandbox.network/*` ledger
    key — a LATER call for the SAME actor+agent, even with bus=None,
    passes without asking again. This is the "declared → silent" step
    applying to a PRIOR interactive grant, not just config."""
    r = _resolver(tmp_path)
    bus = _FakeBus(ALWAYS)
    await r.require_network(PermissionDecl(), bus, "skill", argv=["curl", "x"])
    (_ask,) = bus.asks  # exactly one — tuple-unpack raises on any other count

    # Second call, same actor, no bus at all — must pass silently.
    await r.require_network(PermissionDecl(), None, "skill", argv=["curl", "y"])


@pytest.mark.asyncio
async def test_ledger_grant_is_scoped_per_agent_not_shared_across_agents(
    tmp_path: Path,
) -> None:
    """Tier 2: #5052 — an ALWAYS grant recorded while running as one AGENT
    does not silently apply to a DIFFERENT agent under the same actor
    role/key — the exact leak #5052 closed for `http.get`, reused here via
    the same `_scope_covers_agent` check `_approve` already applies. A
    FRESH resolver (same project_root, so it loads the SAME persisted
    ledger record — #5153) is used for the second call: the in-memory
    `_session` cache is intentionally NOT itself agent-scoped (only the
    on-disk `_saved`/`_saved_scopes` pair is, via #5052's own mechanism),
    so reusing one resolver instance would test the wrong layer."""
    r1 = _resolver(tmp_path)
    await r1.require_network(
        PermissionDecl(), _FakeBus(ALWAYS), "chat_router",
        argv=["curl", "x"], agent_name="agent_a",
    )
    r2 = _resolver(tmp_path)
    with pytest.raises(PermissionError, match="requires an interactive prompt"):
        await r2.require_network(
            PermissionDecl(), None, "chat_router",
            argv=["curl", "y"], agent_name="agent_b",
        )
