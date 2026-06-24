"""Tier 2: OS invariant — #2115 /rewind-cancel truthfulness + the await_quiescent
re-drain (no WAL append lands past the reset-record).

(1) Truthfulness: the rewind summary's in-flight disposition counts a cancelled
    task vs a finished-before-the-cancel-landed task (vs the old hardcoded
    "in-flight cancelled" literal that lied about finished runs).
(2) Re-drain: await_quiescent loops to a fixpoint, so an append SCHEDULED DURING
    the join (the join↔append race that let a skill_completed leak past the
    reset-record) is still joined before quiescence returns — vector-agnostic.

Real Session + StateLog (no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import _count_inflight_disposition
from reyn.runtime.session import Session


@pytest.mark.asyncio
async def test_inflight_disposition_counts_cancelled_vs_finished():
    """Tier 2: a finished-before-cancel task counts as finished (NOT cancelled) —
    the truthful disposition the /rewind summary now reports."""
    async def _finishes() -> None:
        return None

    async def _hangs() -> None:
        await asyncio.sleep(60)

    finished_t = asyncio.ensure_future(_finishes())
    await asyncio.gather(finished_t, return_exceptions=True)   # let it complete
    hung_t = asyncio.ensure_future(_hangs())
    hung_t.cancel()
    await asyncio.gather(hung_t, return_exceptions=True)        # settle the cancel

    cancelled, finished = _count_inflight_disposition([finished_t, hung_t])
    assert cancelled == 1
    assert finished == 1


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="t",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        hooks_config=None,
    )


@pytest.mark.asyncio
async def test_await_quiescent_redrains_append_scheduled_during_join(tmp_path):
    """Tier 2: await_quiescent re-drains to a fixpoint — a follow-up WAL-append task
    scheduled DURING the join (the #2115 join↔append race) is still joined before
    quiescence returns, so no append can land past the reset-record. A one-shot
    gather (the old code) would leave the follow-up pending."""
    session = _make_session(tmp_path)
    follow_up: list[asyncio.Task] = []

    async def _second() -> None:
        await asyncio.sleep(60)   # hangs — only the re-drain's cancel settles it

    async def _first() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            # Scheduled WHILE _first is being joined (after a one-shot snapshot
            # would have been taken) — the race the re-drain must catch.
            follow_up.append(
                session._track_wal_task(asyncio.ensure_future(_second()))
            )

    session._track_wal_task(asyncio.ensure_future(_first()))
    await asyncio.sleep(0)   # let _first reach its await point before quiescence cancels it

    await session.await_quiescent()

    # The re-drain cancelled+joined the follow-up scheduled during the join, so it
    # is SETTLED (done) when quiescence returns. A one-shot gather would miss it →
    # it'd still be pending (not done). (Public task state — no private-set assert.)
    assert follow_up and follow_up[0].done()
