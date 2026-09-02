"""Tier 2: #5648 point 5 (lead-coder, from reyn-self's own real anchor data)
— the rewind-timeline anchor (#1547) must prefer the last genuine human
prompt, never a hook/peer/external-message turn's own triggering text.

Real incident: `.reyn/generation-anchors.json` on reyn-self carried entries
like "broker から 1 件受け取りました…" and "このターンで ★宣言した作業
は…" as the "last user message" anchor — `SnapshotJournal.cut_generation`'s
own call site (`Session._run_router_loop`) used to pass THIS TURN'S OWN
triggering text unconditionally, and `snapshot_journal.py` never looked at
where that text came from. A hook self-continuation's own declaration text
is not a prompt anyone typed — owner's own ask was explicitly "当時の
プロンプトの先頭行" (the PROMPT's own first line).

Fix: `Session._handle_inbox_text` (the shared body every text-bearing inbox
kind funnels through) now stamps `meta["origin"]` on the `role="user"`
history entry it appends, from `self._current_turn_origin` (already
computed per-turn by `_stamp_execution_context`, #0060 A7 — "user_directed"
only for a genuine `TurnOrigin.CLIENT_INPUT` turn). At the `cut_generation`
call site, a turn whose own origin is NOT "user_directed" (hook, in this
test) walks history backward for the last "user_directed"-origin entry and
anchors on ITS text instead of this turn's own.

Real Session + real WAL/AnchorStore/SnapshotJournal, no fakes for anything
this test asserts on — only `_loop_driver.run_turn` is replaced with a
plain async no-op function (real method-assignment, the same seam
`tests/runtime/test_3475_mcp_probe_priming_all_turn_kinds.py` already
uses to isolate turn-boundary mechanics from an actual LLM call).

Drives the hook push via `Session._put_inbox(TurnOrigin.HOOK, ...)` — the
SAME internal seam `HookDispatcher`'s own real E-path uses
(`dispatcher.py`'s own `_put_inbox(TurnOrigin.HOOK, ...)` call, verified by
reading it) and 5+ existing test files already call directly — a raw
`session.inbox.put((...))` (no WAL append at all) would never advance
`last_assigned_seq`, making this test's own "genuinely a NEW checkpoint"
claim vacuous.

Reads `anchors.json` DIRECTLY off disk (the durable, on-disk artifact
`AnchorStore` itself is, per its own module docstring — a public surface,
not an in-memory private attribute) rather than trying to independently
recompute the WAL seq `cut_generation` assigned its own checkpoint at —
`AnchorStore.get(seq)` degrades an UNKNOWN seq to `""` the exact same way
it degrades a KNOWN seq with no anchor, so guessing the wrong seq would
make a deny-shaped assertion pass VACUOUSLY.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reyn.core.events.anchor_store import AnchorStore
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    session = make_session(
        agent_name="fp5648-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    session.attach_anchor_store(AnchorStore(tmp_path / "anchors.json"))

    async def _noop_run_turn(user_text: str, chain_id: str) -> None:
        return None

    session._loop_driver.run_turn = _noop_run_turn
    return session


def _read_anchors(tmp_path: Path) -> dict:
    """The durable, on-disk anchor store content — `{seq_str: {"anchor":
    ..., "full": ...}}` — read directly, per this file's own module
    docstring on why a computed seq is not used instead."""
    path = tmp_path / "anchors.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


async def _drive_one_turn(session: Session) -> None:
    await session.run_one_iteration()
    await session.journal.flush()


@pytest.mark.asyncio
async def test_hook_driven_checkpoints_anchor_on_the_preceding_human_prompt(
    tmp_path: Path,
) -> None:
    """Tier 2: accept — a real human turn establishes the anchor baseline;
    a SUBSEQUENT hook-driven turn's own checkpoint still anchors on that
    SAME human prompt, never the hook's own declaration text."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path)

        human_prompt = "sleep のたびにトークン消費しないで、tmux で待って"
        await session.submit_user_text(human_prompt)
        await _drive_one_turn(session)

        after_human = _read_anchors(tmp_path)
        assert any(v["anchor"] == human_prompt for v in after_human.values()), (
            f"sanity: the human turn's own checkpoint must anchor on its "
            f"own text — establishes the baseline this test's real claim "
            f"depends on; got {after_human!r}"
        )

        hook_declaration = "このターンで ★宣言した作業は broker への報告です"
        await session._put_inbox(
            TurnOrigin.HOOK,
            {"name": "session_start", "text": hook_declaration, "chain_id": "chain-hook"},
        )
        await _drive_one_turn(session)

        after_hook = _read_anchors(tmp_path)
        new_seqs = set(after_hook) - set(after_human)
        assert new_seqs, (
            f"sanity: a NEW checkpoint must have been cut by the hook turn "
            f"— before {set(after_human)!r}, after {set(after_hook)!r}"
        )
        for seq in new_seqs:
            new_entry = after_hook[seq]
            assert new_entry["anchor"] == human_prompt, (
                f"a hook-driven turn's own checkpoint must anchor on the "
                f"last CONFIRMED human prompt, not this turn's own "
                f"triggering text — got {new_entry['anchor']!r}"
            )
            assert hook_declaration not in new_entry["anchor"], (
                "deny: the hook's own declaration text must never appear "
                "in the rewind-timeline anchor"
            )
    finally:
        os.chdir(old_cwd)


@pytest.mark.asyncio
async def test_hook_driven_checkpoint_with_no_prior_human_turn_captures_no_anchor(
    tmp_path: Path,
) -> None:
    """Tier 2: deny sibling — a session whose FIRST turn is hook-driven (no
    confirmed human prompt exists yet anywhere in history) degrades to the
    SAME "no anchor" behaviour `cut_generation` already has for an empty
    anchor (#1547: `if anchor_store is not None and anchor:` — an empty
    anchor is never captured at all) — never falls back to the hook's own
    text either."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        session = _make_session(tmp_path)

        await session._put_inbox(
            TurnOrigin.HOOK,
            {"name": "session_start", "text": "wake up and continue", "chain_id": "chain-1"},
        )
        await _drive_one_turn(session)

        anchors = _read_anchors(tmp_path)
        assert anchors == {}, (
            f"no confirmed human prompt exists yet — nothing should have "
            f"been captured at all (the pre-existing empty-anchor no-op), "
            f"never the hook's own text — got {anchors!r}"
        )
    finally:
        os.chdir(old_cwd)
