"""Tier 2b: #4387 Phase B ② (remaining consumers) — TUI scrollback paging and
in-conversation search actually page BEYOND what #4387 Phase B ① bounded
``Session.load_history()``'s startup read to, via the real
``RegistryReadModel.load_older_conversation_history`` -> ``Session.
extend_history_backward`` -> ``history_tail_reader.read_history_before`` chain.

The existing #3476 paging/search suites (``test_lazy_history_paging_3476.py``,
``test_search_bar_3476.py``) both use a hand-rolled ``_HistoryReadModel``
fake over a synthetic in-memory ``list[ChatMessage]`` — real for what THEY
cover (paging/search over whatever ``conversation_history()`` already
returns), but neither ever exercises a ``self.history`` that is BOUNDED
smaller than the full log, so neither would catch a regression in the real
disk-extension wiring this file's own PR adds. This file closes that gap:
a REAL ``AgentRegistry`` + real ``Session`` (durable ``history.jsonl``,
written via ``Session._append_history``, the same seam
``test_4387_extend_history_backward.py`` uses) + the real
``RegistryReadModel`` — no fakes anywhere in the read path.

``self.history`` is truncated directly after real turns are appended,
simulating "this is what a BOUNDED ``load_history()`` startup read left in
memory" — the same technique ``test_4387_active_branch_history_extend_on_
demand.py`` already established for this exact precondition.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual.widgets import Static
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.restore import RESUME_DIVIDER, project_restored_frames
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


class _Transport(ClientTransport):
    """A real, minimal :class:`ClientTransport` — no live frames needed for
    a restore/paging/search test (mirrors the #3476 suites' own shape)."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _registry(tmp_path: Path) -> AgentRegistry:
    """Mirrors ``test_3310_n2_reset_hydrate.py``'s own helper — a real
    ``AgentRegistry`` whose session factory builds a real ``Session`` with
    ``load_history()`` called at load time, same as production attach."""

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


def _append_turns(s: Session, n: int, *, needle: "str | None" = None) -> None:
    """Real, durable turns (user+assistant pairs), via ``_append_history`` —
    written to ``history.jsonl`` on disk AND appended to ``s.history``, the
    same seam ``test_4387_extend_history_backward.py`` uses. An optional
    ``needle`` is planted as the FIRST (oldest) turn's user text."""
    if needle is not None:
        s._append_history(ChatMessage(role="user", content=needle))
        s._append_history(ChatMessage(role="assistant", content="old reply"))
    for i in range(n):
        s._append_history(ChatMessage(role="user", content=f"question {i}"))
        s._append_history(ChatMessage(role="assistant", content=f"answer {i}"))


def _texts(app: TextualChatApp) -> "list[str]":
    return [entry.item.text for entry in app.conversation]


def _content_texts(texts: "list[str]") -> "list[str]":
    """``texts`` with the ``RESUME_DIVIDER`` chrome row(s) filtered out.

    ``project_restored_frames`` (#3273-owned, outside #4387②'s scope)
    unconditionally prepends ONE divider row per call, always at the
    front of THAT call's own output — correct for the single-shot
    ``project_restored_frames(full_log)`` this test compares against, but
    flowview offers no way to REMOVE or reposition an already-painted row,
    so once disk-extension has run at least once the app's own divider
    (painted by the FIRST non-empty projection, #4387②'s
    ``_extend_older_frames_from_disk`` dedupes duplicates but cannot move
    it) may sit at a different position than the canonical single-shot
    projection's. This test's actual correctness claim is CONTENT
    reachability + ORDER, not the divider's cosmetic exact position — so
    comparisons here compare content only."""
    return [t for t in texts if t != RESUME_DIVIDER]


def _count_text(app: TextualChatApp) -> str:
    return str(app.query_one("#search-count", Static).render())


def _addressed_text(app: TextualChatApp) -> "str | None":
    entry = app.query_one(FlowView).current
    return None if entry is None else entry.item.text


async def _type(pilot, text: str) -> None:
    for ch in text:
        await pilot.press(ch)
    await pilot.pause()


@pytest.mark.asyncio
async def test_scrolling_to_top_extends_from_disk_beyond_the_bounded_load(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: ``self.history`` bounded to the newest few turns (simulating
    #4387 Phase B ①'s bounded startup load) — scrolling to the top must
    still recover the FULL durable history from disk via
    ``load_older_conversation_history``, not stop at the in-memory bound."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        s = reg.get_session("alpha")
        assert s is not None
        _append_turns(s, 20)  # 40 durable messages
        full_log = list(s.history)
        s.history = s.history[-4:]  # bounded load: only the newest 4 in memory
        assert len(s.history) < len(full_log), (
            "test setup: self.history must be genuinely bounded below the full "
            "durable log, or there is nothing left to page in from disk — this "
            "would make the test vacuous"
        )

        app = TextualChatApp(
            transport=_Transport(), read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            expected_full = [f.text for f in project_restored_frames(full_log)]
            # NOT asserted here: "fewer than the full log materialised right after
            # mount." With this little content, FlowView's own ReachedTop can fire
            # on mount itself (the viewport already shows everything painted so
            # far) — a legitimate scroll-position trigger, not a bug — so the
            # extension may already be complete before the explicit scroll loop
            # below ever runs. What this test actually proves is the END STATE:
            # the full durable log is reachable, whether recovery happened at
            # mount or via an explicit scroll.
            flow = app.query_one(FlowView)
            for _ in range(8):  # more rounds than there are pages
                flow.scroll_to_top()
                await pilot.pause()
                await pilot.pause()
                if len(_texts(app)) == len(expected_full):
                    break
                flow.scroll_to_bottom()
                await pilot.pause()

            assert _content_texts(_texts(app)) == _content_texts(expected_full), (
                "scrolling to the top did not recover the full durable history "
                f"from disk ({len(_texts(app))} of {len(expected_full)} frames) "
                "— the bounded in-memory prefix was silently treated as "
                "'the true start of the conversation'"
            )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_search_finds_a_match_only_on_disk_beyond_the_bounded_load(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2b: the real correctness gap #4387 Phase B ① opened for search —
    a needle that lives ONLY in the durable log on disk, older than
    anything currently in ``self.history``, must still be found when
    search opens (``_materialise_all_older`` now drains
    ``load_older_conversation_history`` to the true start FIRST, closing
    the gap where a hit older than the bounded startup load silently read
    as 'no match')."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        s = reg.get_session("alpha")
        assert s is not None
        _append_turns(s, 60, needle="needle-only-on-disk")  # oldest turn
        # Bounded load large enough that it does NOT all fit in one screen
        # (100x30) — with only a handful of in-memory messages, flowview's
        # own ReachedTop can fire immediately on mount (everything painted
        # so far already fits the viewport), which would extend from disk
        # before this test's own search-open step ever runs, making the
        # search-specific gap this test targets unreachable to falsify.
        s.history = s.history[-40:]  # needle (the oldest turn) is NOT in memory
        assert not any(m.content == "needle-only-on-disk" for m in s.history), (
            "test setup: the needle must be genuinely bounded out of memory"
        )

        app = TextualChatApp(
            transport=_Transport(), read_model=RegistryReadModel(reg), agent_name="alpha",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not any(
                "needle-only-on-disk" in (e.item.text or "") for e in app.conversation
            ), "test setup: the needle must not already be materialised"

            await pilot.press("ctrl+n")
            await _type(pilot, "needle")

            selected = _addressed_text(app)
            assert selected is not None and "needle-only-on-disk" in selected, (
                f"a match living only on disk (beyond the bounded in-memory "
                f"load) was not found by search (selected: {selected!r})"
            )
            assert _count_text(app) == "1/1"
    finally:
        await reg.shutdown()
