"""Tier 2: #5475 — ``compaction_started`` moves to ``CompactionEngine.compact()``'s
own entry (once, covering both real callers), not duplicated.

architect ruling (#5382 review, refined on #5475 after an investigation
disproved "payload derivable from ``input_chunk`` alone" for one field):
event は行為に属し、呼び手には属しません — ``compact()`` is the one real
entry both ``CompactionController.force_compact_now`` and ``retry_loop``'s
own internal compaction attempts share, so the emit lives there, moved
from the controller (not added alongside it — #5382/#5455's own ruling on
two emit sites for the same kind).

``new_turn_count``/``had_previous`` are genuinely derivable from
``input_chunk`` alone for EITHER caller. ``covers_through_seq`` is not:
the controller's own ``new_turns`` carry a real ``seq`` per turn
(``_turn_to_compactor_input``); ``retry_loop``'s own ``new_turns`` are
litellm wire dicts (``_serialise_turn``'s output) with no ``seq`` field at
all. ``compact()`` therefore takes ``covers_through`` as a REQUIRED
keyword-only argument (no default — an omission is a mypy error at the
call site, never a silently-accepted null) typed
``CoversThrough = int | SeqUnavailable``, so a consumer of the emitted
event can tell "seq is a real value" apart from "seq is unknown, and
here is WHY" rather than conflating both into a bare ``None``.
"""
from __future__ import annotations

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.dev.testing.llm_stub import LLMStub
from reyn.services.compaction.engine import (
    CompactionEngine,
    HistoryChunkToCompact,
    SeqUnavailable,
)
from tests._support.events import collect_events, settle
from tests._support.session import make_session

# "compaction_started" has exactly ONE emit site in the whole tree (inside
# CompactionEngine.compact() — compaction_controller.py's own former emit
# is genuinely gone, not left alongside the new one) — checked BY HAND
# (`git grep -n 'compaction_started' src/`), not as its own test: a
# `len(x) == 1` population-count assertion is this repo's own testing
# policy's "format pinning" (Tier 4) — see
# `test_tier_audit.py`'s own rejection of exactly this shape. Not pinned
# as a runtime test for the same reason `test_3868_collect_events_helper
# .py`'s own strip-falsify witness is documented as run-by-hand rather
# than encoded as an assertion whose failure mode this policy already
# disallows. The two tests below are the BEHAVIORAL proof instead: if
# compaction_controller.py still emitted its own compaction_started
# alongside the engine's, the strip-falsify pass below (temporarily
# removing the engine's own emit) would leave `test_controller_path_
# carries_a_real_seq`'s event still present (from the surviving
# controller-side emit) — it did not; both behavioral tests below went
# red together when the engine's own emit was stripped, confirmed by
# hand, restored.


@pytest.mark.asyncio
async def test_controller_path_carries_a_real_seq(tmp_path, monkeypatch):
    """Tier 2: the CONTROLLER's own caller (whose new_turns carry a real
    `seq` per turn) makes `compaction_started`'s `covers_through_seq` a
    real int, with no `covers_through_unavailable_reason` — driven through
    a real Session/CompactionController/CompactionEngine, no stand-in."""
    session = make_session(tmp_path, monkeypatch=monkeypatch, t_max=2_500)
    collected = collect_events(session)

    stub = LLMStub()
    stub.install()
    try:
        for i in range(8):
            session._append_history(
                _msg(f"filler turn {i} " * 40, seq=i + 1),
            )
        await session._compaction_controller.force_compact_now()
        await settle(session)
    finally:
        stub.restore()

    started = [e for e in collected if e.type == "compaction_started"]
    assert started, "expected a real compaction_started event via the controller"
    payload = started[0].data
    assert isinstance(payload.get("covers_through_seq"), int), (
        f"controller path must carry a real int seq, got "
        f"{payload.get('covers_through_seq')!r}"
    )
    assert payload.get("covers_through_unavailable_reason") is None, (
        "a real seq must not ALSO carry an unavailable-reason — the two "
        "fields are mutually exclusive"
    )


@pytest.mark.asyncio
async def test_retry_loop_shaped_path_carries_a_named_absence():
    """Tier 2: the shape retry_loop's own caller actually has — wire-dict
    `new_turns` with no `seq` — makes `covers_through_seq` None and names
    WHY via `covers_through_unavailable_reason`, never a bare, unexplained
    null. Drives the real `CompactionEngine.compact()` directly with the
    SAME `SeqUnavailable` sentinel `engine.py`'s own retry_loop call site
    passes, proving the sentinel's `.value` is what actually reaches the
    event (not merely that the enum exists)."""
    events = EventLog()
    collected = collect_events(events)
    engine = CompactionEngine(
        "openai/test-standard-model", events, CompactionConfig(use_chars4_estimate=True),
    )

    stub = LLMStub()
    stub.install()
    try:
        # A litellm-wire-dict-shaped turn (role/content only) — no "seq"
        # key at all, matching what `decompose_history_for_retry`'s
        # `raw_middle` actually contains (see module docstring).
        chunk = HistoryChunkToCompact(
            previous_summary=None,
            new_turns=[{"role": "user", "content": "wire-shaped, no seq"}],
            section_token_caps={},
        )
        await engine.compact(chunk, covers_through=SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ)
        await settle(events)
    finally:
        stub.restore()

    started = [e for e in collected if e.type == "compaction_started"]
    assert started, "expected a real compaction_started event"
    payload = started[0].data
    assert payload.get("covers_through_seq") is None, (
        "no real seq is available on this path — must not fabricate one"
    )
    assert payload.get("covers_through_unavailable_reason") == "wire_dicts_carry_no_seq", (
        f"expected the named reason, got "
        f"{payload.get('covers_through_unavailable_reason')!r} — a bare "
        "None here would be indistinguishable from a bug that silently "
        "dropped a real seq"
    )


def _msg(text: str, *, seq: int):
    from reyn.runtime.chat_message import ChatMessage
    return ChatMessage(role="user", content=text, seq=seq)
