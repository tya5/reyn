"""Tier 2: #1800 slice 7 — the hook-driven-turn loop valve.

#5561 (owner ruling, 2026-08-30) retired the valve this file was named for
entirely: "hook 起動を回数で制限なんて誰も設定できないでしょ。どんな回数が
妥当か誰も判断できない" — no operator could derive a correct cap value. The
4 tests that exercised the counter/cap/reset/ride-along behavior
(``test_no_hooks_valve_never_engages``, ``test_hook_loop_exceeding_cap_is_
suppressed``, ``test_counter_resets_on_user_turn``,
``test_c_ride_alongs_do_not_increment``) were deleted along with their
dedicated helpers (``_collect_events``, ``_ran_chain_ids``,
``_checkpoint_kinds``, ``_push_hook``, ``_push_user``) — there is no
mechanism left for them to pin. Replaced by: ``CostConfig`` (total spend,
cause-independent), #5516's own N-into-one push folding, and per-push size
bounds (``spillability_max_chars``) — see ``LoopConfig``'s own docstring,
config/chat.py, for the full rationale.

Only ``test_hook_message_is_fanned_out_to_live_outbox`` survives — it is
NOT actually about the valve (the ``cap=`` it passed to ``_make_session``
was incidental, unused by its own assertions); it pins hook-message fan-out
to the live outbox. Kept at this file's ORIGINAL PATH deliberately, even
though the module is no longer "about" the loop valve: other test files
(e.g. the #5558/#5563-era suite) cross-reference this exact
file+test-name in their own comments ("mirrors
test_hook_loop_valve_1800_7.py::test_hook_message_is_fanned_out_to_live_
outbox's own established pattern") — moving or renaming it would orphan
those references. Anyone following one of those old cross-references and
finding this docstring: the pattern being pointed at is still here, just
alone now.

Policy (docs/deep-dives/contributing/testing.md): Real Session / EventLog /
StateLog / SafetyConfig. No private-state assertions — the live outbox
subscription is the public seam.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    safety = SafetyConfig(
        loop=LoopConfig(),
        on_limit=OnLimitConfig(mode="unattended"),   # deny deterministically, no bus
    )
    return make_session(
        agent_name="valve-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=safety,
    )


@pytest.mark.asyncio
async def test_hook_message_is_fanned_out_to_live_outbox(tmp_path):
    """Tier 2: a hook-injected message is visible on the live outbox, not only history."""
    session = _make_session(tmp_path)
    subscription = session.outbox_hub.subscribe()
    async def _noop(*_args):
        return None

    session._run_router_loop = _noop  # type: ignore[method-assign]

    await session._handle_hook_message({"name": "probe", "text": "injected", "wake": True})
    message = await subscription.get()
    assert message is not None
    assert message.kind == "system"
    assert "[hook:probe]" in message.text
    subscription.close()
