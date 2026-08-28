"""Tier 2: #5367 — ``RouterHistoryBuffer.build_history``'s elide branch
(head + tail window-utilization cut) used to drop the elided MIDDLE with
NO representation at all, model- or operator-side (owner: "llm に mid
渡さないとかありえないでしょ" — an LLM never getting mid at all is
unacceptable). Root cause: ``trim_head``/``trim_tail`` are genuine
prefix/suffix cuts of the (already watermark-filtered) turns, so
everything strictly between them fell through ``selected = head +
tail_deduped`` untouched.

(b) design (lead-coder/architect ruling, issuecomment-5449838534):
- A SPILLED mid turn (``ChatMessage.meta[SPILLED_META_KEY]``) already has
  a small ref-preview ``content`` (write-time cap, #5364 §1.2) — folded
  back into ``selected`` as-is, no new transform.
- An UNSPILLED mid turn's raw content is bundled into ONE synthetic
  report turn naming the affected seq range and count, so the model at
  least KNOWS turns were omitted (never "畳まずに捨てる", the owner's own
  forbidden shape).
- A ``wire_turns_elided`` audit-event fires once per ``build_history``
  call that actually elides a non-empty middle — architect ruling (B):
  the wire payload is not persisted by default, so this is the one
  operator-visible record that an elide happened on this turn at all.

Mirrors ``tests/runtime/test_1128_step3_token_budget_headtail.py``'s own
real-``Session`` harness (``_make_session_with_t_max``/``T_max=2800``,
80-token filler turns) — no mocks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import reyn.llm.model_budget as _mb
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY, ChatMessage
from tests._support.agent_session import make_session

_CONTENT_80TOK = "X" * 320  # 320 chars / 4 (use_chars4_estimate=True) = 80 tokens


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session_with_t_max(tmp_path: Path, monkeypatch, t_max: int):
    """Mirrors test_1128_step3_token_budget_headtail.py's own helper of the
    same name — see that file's own docstring for why ``monkeypatch`` (not
    a manual save/restore) is required (CompactionEngine builds lazily)."""
    from reyn.config import CompactionConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=CompactionConfig(
            use_chars4_estimate=True,
            section_caps_spec_tokens=0,
        ),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _push(session, role: str, content: str, **kw) -> None:
    if role == "agent":
        role = "assistant"
    session._append_history(ChatMessage(role=role, content=content, ts=_now(), **kw))


def test_a_spilled_mid_turn_is_folded_back_in_as_its_own_ref(tmp_path, monkeypatch) -> None:
    """Tier 2: #5367 ① — a mid turn already carrying
    ``meta[SPILLED_META_KEY]`` (its own ``content`` is ALREADY the small
    ref-preview text, write-time cap, #5364 §1.2) must survive the elide
    branch as its own wire entry — not silently dropped just because it
    fell in the middle gap between head and tail."""
    monkeypatch.chdir(tmp_path)  # _resolve_spilled_content's project_dir_fn reads Path.cwd()
    (tmp_path / "spilled_body_5367.txt").write_text("the huge original body", encoding="utf-8")
    session = _make_session_with_t_max(tmp_path, monkeypatch, t_max=2800)
    for i in range(6):
        _push(session, "user", f"head-filler-{i}:" + _CONTENT_80TOK)
    ref_preview = 'read_file(path="spilled_body_5367.txt") ...'
    _push(
        session, "tool", ref_preview,
        meta={SPILLED_META_KEY: True, CONTENT_REF_META_KEY: "spilled_body_5367.txt"},
        tool_call_id="tc-5367", name="tool",
    )
    for i in range(6):
        _push(session, "assistant", f"tail-filler-{i}:" + _CONTENT_80TOK)

    head, raw_middle, tail, _summary, _seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    assert any(t.get("tool_call_id") == "tc-5367" for t in raw_middle), (
        "test setup sanity: the spilled turn must actually land in the "
        "elided middle, not head/tail — adjust filler counts"
    )

    msgs = session._history_buffer.build_history()
    contents = [m.get("content", "") for m in msgs]
    assert any(ref_preview in str(c) for c in contents), (
        f"the spilled mid turn's own ref-preview must survive into the "
        f"wire, got contents={contents!r}"
    )


def test_unspilled_mid_turns_are_bundled_into_one_report_turn(tmp_path, monkeypatch) -> None:
    """Tier 2: #5367 ② — unspilled mid turns (no ref available) must
    produce ONE synthetic report turn naming the affected seq range and
    count — the model at least KNOWS turns were omitted, rather than the
    omission being invisible to it too (never "畳まずに捨てる")."""
    session = _make_session_with_t_max(tmp_path, monkeypatch, t_max=2800)
    texts = [f"turn-{i}:" + _CONTENT_80TOK for i in range(30)]
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    head, raw_middle, tail, _summary, seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    assert raw_middle, "test setup sanity: some turns must land in the elided middle"
    mid_seqs = [seq_by_id[id(t)] for t in raw_middle]

    msgs = session._history_buffer.build_history()
    contents = [m.get("content", "") for m in msgs]

    assert not any(t in contents for t in texts[1:-1]), (
        "raw mid content must not appear verbatim on the wire"
    )
    report_candidates = [c for c in contents if "omitted to fit the context window" in str(c)]
    (report,) = report_candidates  # raises unless exactly one synthetic report turn
    assert str(len(mid_seqs)) in report, (
        f"report turn must name the count ({len(mid_seqs)}), got {report!r}"
    )
    assert str(min(mid_seqs)) in report and str(max(mid_seqs)) in report, (
        f"report turn must name the seq range ({min(mid_seqs)}-{max(mid_seqs)}), "
        f"got {report!r}"
    )


def test_wire_turns_elided_event_names_the_full_elided_range(tmp_path, monkeypatch) -> None:
    """Tier 2: #5367 ③ — a real ``wire_turns_elided`` audit-event fires
    once per ``build_history`` call whose elide branch drops a non-empty
    middle, naming the TOTAL elided count and seq range (spilled-and-
    ref'd + unspilled-and-reported combined) — the one operator-visible
    record that an elide happened at all (architect ruling (B): the wire
    payload itself is not persisted by default)."""
    session = _make_session_with_t_max(tmp_path, monkeypatch, t_max=2800)
    texts = [f"turn-{i}:" + _CONTENT_80TOK for i in range(30)]
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    head, raw_middle, tail, _summary, seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_seqs = [seq_by_id[id(t)] for t in raw_middle]
    assert mid_seqs, "test setup sanity: some turns must land in the elided middle"

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))
    session._history_buffer.build_history()

    elided_candidates = [e for e in events if e.type == "wire_turns_elided"]
    (elided,) = elided_candidates  # raises unless exactly one such event fired
    assert elided.data["count"] == len(mid_seqs)
    assert elided.data["seq_start"] == min(mid_seqs)
    assert elided.data["seq_end"] == max(mid_seqs)


def test_no_event_and_no_report_turn_when_nothing_is_elided(tmp_path, monkeypatch) -> None:
    """Tier 2: regression guard — a small chat (fits raw, no elide branch
    at all) must fire no ``wire_turns_elided`` event and produce no
    synthetic report turn."""
    session = _make_session_with_t_max(tmp_path, monkeypatch, t_max=2800)
    for text in ["alpha", "beta", "gamma"]:
        _push(session, "user", text)

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))
    msgs = session._history_buffer.build_history()

    assert not [e for e in events if e.type == "wire_turns_elided"]
    assert not any(
        "omitted to fit the context window" in str(m.get("content", "")) for m in msgs
    )
