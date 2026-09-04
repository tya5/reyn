"""Tier 2: #5720 (architect's confirmed spec, issuecomment-5533959117) —
the MAIN LLM (``main_call``) learns, from every summary message's own
``content``, that spilled tool-result bodies are recoverable, WITHOUT that
knowledge ever riding through a summarizing LLM's own judgment.

owner's real-machine incident: "compact 成功しました。ただし、その後何を覚え
てるか聞いてみたところ、ほとんどのことを忘れてました" / "spill out ファイル
あるか聞いたけど、知らないていうてたよ" — measured (issue #5720's own thread,
e2e-coder, real execution, no mocks) in two stages: (1) a spilled turn's own
``read_file(path=...)`` marker IS on the wire for that turn, but (2) once
that turn is folded into a summary, the marker vanishes — survival of ANY
reference past a fold depends entirely on whether the summarizing LLM's own
JSON response happens to echo it into ``artifacts_referenced``, which has no
deterministic guarantee at all.

Architect's confirmed acceptance (verbatim, issuecomment-5533959117):
  - [ ] render された text に実在する (structured への追加だけでは不十分)
  - [ ] 毎回 store から導出される — structured に保存されていない
  - [ ] ★ 2 回 fold しても残る — persisted-via-LLM carry would be invisible
        to a single-fold test; this is the witness that closes that gap
  - [ ] section の長さが spill 件数に依存しない (wire cost O(1), charter Q1)
  - [ ] spill 0 件で section が出ない ("no spills" is a normal answer)
  - [ ] artifacts_referenced に決定論的な値を書いていない (2 つの出所を持たせない)

Real ``CompactionController`` + real ``RouterHistoryBuffer`` + real
``MediaStore`` + real ``RetryLoop``/``RecoveryLadder`` throughout — the ONLY
stand-in anywhere in this file is the compaction LLM's own ``compact()``
call (the one LLM-call boundary every sibling compaction test in this repo
already stubs, ``test_5719``/``test_5721``'s own idiom), and it is a
CAPTURING stub, never a mock (no assertions on call count/args beyond what
`compact()`'s own real signature requires).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.runtime.session_pure import render_summary_for_storage
from reyn.services.compaction.engine import (
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    CoversThrough,
    HistoryChunkToCompact,
    RetryPayload,
    retry_loop,
)

_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000, tail_budget=50,
    new_msg_budget=10_000, B_M=80_000, main_M_room=150_000, effective_trigger=150_000,
    section_caps={},
)


class _CapturingEngine(CompactionEngine):
    """Stands in for the LLM-call boundary only — same idiom as
    ``test_5719``'s own ``_SucceedingEngine`` / ``test_5721``'s own
    ``_CapturingEngine``. Never asked about spill reachability at all —
    that section is composed entirely OUTSIDE this stub's own output,
    which is exactly the property under test."""

    def __init__(self) -> None:
        self._model = ""
        self._events = EventLog()
        self._budgets = _STUB_BUDGETS

    async def compact(
        self, input_chunk: "HistoryChunkToCompact", *, covers_through: "CoversThrough",
    ) -> "ChatSummary":
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub summary", covers_through_seq=max(seqs, default=0))


def _history(n: int, *, start: int = 1) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 else "assistant", content="x" * 200, seq=i)
        for i in range(start, start + n)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: "CompactionEngine",
) -> CompactionController:
    return CompactionController(
        event_log=engine._events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=render_summary_for_storage,
    )


def _make_history_buffer(
    *, history: "list[ChatMessage]", media_store: "MediaStore",
) -> RouterHistoryBuffer:
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: "test-model",
        events=EventLog(),
        media_store=media_store,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=True,
        history_appender=history.append,
    )


# ---------------------------------------------------------------------------
# RouterHistoryBuffer.spill_reachability_snapshot — unit level
# ---------------------------------------------------------------------------


def test_snapshot_is_none_when_nothing_was_ever_spilled(tmp_path: Path) -> None:
    """Tier 2: acceptance "spill 0 件で section が出ない" — the pure-count
    witness. A store that has written nothing yields None, not a
    zero-count tuple (the caller distinguishes "nothing to report" from
    "report a count of zero" — the former omits the section entirely)."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    hb = _make_history_buffer(history=[], media_store=store)
    assert hb.spill_reachability_snapshot() is None


def test_snapshot_reports_the_real_count_and_a_project_relative_directory(
    tmp_path: Path,
) -> None:
    """Tier 2: a real spilled body makes the snapshot non-None, with the
    EXACT count and a directory rendered project-relative — the SAME
    shape every individual spill's own read_file(path=...) marker
    already uses (tool_result_cap.py's _build_preview), not this
    process's own absolute filesystem layout."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    store.save_tool_result("Y" * 50_000, chain_id="c1", tool="tool", seq=5)
    store.save_tool_result("Z" * 50_000, chain_id="c1", tool="tool", seq=9)
    hb = _make_history_buffer(history=[], media_store=store)

    snapshot = hb.spill_reachability_snapshot()

    assert snapshot is not None
    count, directory = snapshot
    assert count == 2
    assert not directory.startswith("/"), (
        f"directory must be project-relative, got an absolute-looking "
        f"path: {directory!r}"
    )
    assert directory == ".reyn/memory/history-content/a/s"


def test_snapshot_is_none_for_a_store_with_no_agent_identity(tmp_path: Path) -> None:
    """Tier 2: falsify pair (deny side) — a legacy/read-only MediaStore
    construction (no agent_name, 4 of 5 production call sites per
    MediaStore's own docstring) has no session directory to report;
    the snapshot degrades to None rather than raising."""
    store = MediaStore(project_root=tmp_path, session_id="s")
    hb = _make_history_buffer(history=[], media_store=store)
    assert hb.spill_reachability_snapshot() is None


def test_snapshot_is_none_when_media_store_itself_is_none(tmp_path: Path) -> None:
    """Tier 2: falsify pair — RouterHistoryBuffer's own media_store param
    is documented ``MediaStore | None``; this method must degrade the
    same way _materialise_path_ref_content already does."""
    hb = _make_history_buffer(history=[], media_store=None)
    assert hb.spill_reachability_snapshot() is None


# ---------------------------------------------------------------------------
# render_summary_for_storage / wrap_summary_as_message — section shape
# ---------------------------------------------------------------------------


def test_render_summary_omits_the_section_when_spill_reachability_is_none() -> None:
    """Tier 2: byte-identical to pre-#5720 when there is nothing to
    report — the default parameter value changes nothing for every
    existing caller/test of this function."""
    structured = {"topic_arc": "x"}
    before = render_summary_for_storage(structured)
    after = render_summary_for_storage(structured, spill_reachability=None)
    assert before == after
    assert "spilled_content" not in after


def test_render_summary_section_length_does_not_scale_with_spill_count() -> None:
    """Tier 2: acceptance "section の長さが spill 件数に依存しない" — charter
    Q1's own O(1) wire-cost bound. The rendered [spilled_content] block
    stays a fixed 2 lines whether count is 1 or 5000 — only the digit
    inside those 2 lines changes, never a per-path enumeration."""
    structured = {"topic_arc": "x"}
    small = render_summary_for_storage(
        structured, spill_reachability=(1, ".reyn/memory/history-content/a/s"),
    )
    huge = render_summary_for_storage(
        structured, spill_reachability=(5000, ".reyn/memory/history-content/a/s"),
    )
    small_block = small.split("[spilled_content]", 1)[1]
    huge_block = huge.split("[spilled_content]", 1)[1]
    assert small_block.count("\n") == huge_block.count("\n"), (
        "the section's own line count must not grow with the spill count"
    )
    # The only byte-length delta allowed is the digit string itself
    # ("1" vs "5000") — never a per-path listing riding along.
    assert len(huge_block) - len(small_block) == len("5000") - len("1")


def test_render_summary_never_writes_into_artifacts_referenced() -> None:
    """Tier 2: acceptance "artifacts_referenced に決定論的な値を書いていない"
    — the architect's own rejected-alternative check. A caller that
    (incorrectly) tried folding this into artifacts_referenced would
    give it two provenances (LLM output AND this deterministic write);
    this asserts the real render path never does."""
    structured = {"topic_arc": "x", "artifacts_referenced": ["a-real-llm-value"]}
    rendered = render_summary_for_storage(
        structured, spill_reachability=(3, ".reyn/memory/history-content/a/s"),
    )
    assert "a-real-llm-value" in rendered, "the LLM's own value must still render"
    # The deterministic count must never appear as a bullet UNDER
    # [artifacts_referenced] — only under its own, separate section.
    artifacts_block = rendered.split("[artifacts_referenced]", 1)[1].split("[", 1)[0]
    assert "3 tool result" not in artifacts_block


# ---------------------------------------------------------------------------
# RouterHistoryBuffer._serialise_turn — the normal (build_history) wire path
# ---------------------------------------------------------------------------


def test_serialise_turn_omits_the_section_with_no_spills(tmp_path: Path) -> None:
    """Tier 2: acceptance "spill 0 件で section が出ない", exercised through
    the real wire-egress point (not the pure renderer directly)."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    hb = _make_history_buffer(history=[], media_store=store)
    m = ChatMessage(
        role="summary", content="", seq=0,
        meta={"structured": {"topic_arc": "x"}},
    )
    wire = hb._serialise_turn(m)
    assert "spilled_content" not in wire["content"]


def test_serialise_turn_includes_the_section_when_something_was_spilled(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance "render された text に実在する" — the ACTUAL wire
    dict main_call/build_history receive, not an intermediate value."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    store.save_tool_result("Y" * 50_000, chain_id="c1", tool="tool", seq=5)
    hb = _make_history_buffer(history=[], media_store=store)
    m = ChatMessage(
        role="summary", content="", seq=0,
        meta={"structured": {"topic_arc": "x"}},
    )
    wire = hb._serialise_turn(m)
    assert '1 tool result(s)' in wire["content"]
    assert 'glob(path=".reyn/memory/history-content/a/s/*")' in wire["content"]
    assert "read_file(path=" in wire["content"]


def test_the_stored_structured_dict_never_carries_the_derived_value(
    tmp_path: Path,
) -> None:
    """Tier 2: acceptance "毎回 store から導出される — structured に保存され
    ていない", exercised behaviourally (not a static git-grep): after a
    real render with a real spill present, the meta['structured'] dict
    this ChatMessage carries must be untouched — the ONLY place the
    value exists is the rendered `content` string, which is REBUILT
    fresh on the next _serialise_turn call, never read back FROM this
    dict."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    store.save_tool_result("Y" * 50_000, chain_id="c1", tool="tool", seq=5)
    hb = _make_history_buffer(history=[], media_store=store)
    structured = {"topic_arc": "x"}
    m = ChatMessage(role="summary", content="", seq=0, meta={"structured": structured})

    hb._serialise_turn(m)

    assert "spilled_content" not in structured
    assert set(structured.keys()) == {"topic_arc"}, (
        "rendering must never write back into the caller's own "
        "structured dict"
    )


# ---------------------------------------------------------------------------
# ★ The central witness — 2 REAL folds, no persisted carry
# ---------------------------------------------------------------------------


def test_two_real_folds_each_render_a_fresh_section_never_a_stale_one(
    tmp_path: Path,
) -> None:
    """Tier 2: ★ architect's own named central witness ("2 回 fold しても
    残る") — a test exercising only ONE fold cannot distinguish "derived
    fresh every time" from "persisted once and carried through the
    summarizing LLM," the exact failure class ``artifacts_referenced``
    already has (measured directly in #5720's own investigation: a real
    fold discarding an unprotected reference).

    Drives 2 REAL ``CompactionController._run_compaction`` calls in
    sequence (the 2nd's own ``previous_summary`` is the 1st's real
    persisted ``ChatMessage`` — genuine fold-over-fold, not two
    independent compactions), with MORE spills added between them, then
    re-serialises the FINAL summary the same way ``build_history``
    would. If the count were being carried via ``structured`` (the
    ①-rejected design), it would read stale from fold 1; instead it
    reflects the CURRENT total after fold 2."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    engine = _CapturingEngine()
    history: "list[ChatMessage]" = []
    ctrl = _make_controller(history=history, engine=engine)
    hb = _make_history_buffer(history=history, media_store=store)

    store.save_tool_result("Y" * 50_000, chain_id="c1", tool="tool", seq=1)

    history.extend(_history(10, start=1))
    asyncio.run(ctrl._run_compaction(list(history), previous_summary=None, spill_fn=lambda _c: []))
    summary_1 = [m for m in history if m.role == "summary"][-1]

    structured_1 = (summary_1.meta or {}).get("structured", {})
    assert "spilled_content" not in structured_1, (
        "fold 1 must not persist the derived value into structured"
    )

    wire_after_fold_1 = hb._serialise_turn(summary_1)
    assert "1 tool result(s)" in wire_after_fold_1["content"]

    # 2 MORE spills happen between the two folds.
    store.save_tool_result("Z" * 50_000, chain_id="c2", tool="tool", seq=15)
    store.save_tool_result("W" * 50_000, chain_id="c2", tool="tool", seq=16)

    history.extend(_history(10, start=11))
    asyncio.run(
        ctrl._run_compaction(list(history), previous_summary=summary_1, spill_fn=lambda _c: []),
    )
    summary_2 = [m for m in history if m.role == "summary"][-1]
    assert summary_2 is not summary_1, "fold 2 must produce its own summary entry"

    structured_2 = (summary_2.meta or {}).get("structured", {})
    assert "spilled_content" not in structured_2, (
        "fold 2 must not persist the derived value into structured either"
    )

    wire_after_fold_2 = hb._serialise_turn(summary_2)
    assert "3 tool result(s)" in wire_after_fold_2["content"], (
        f"the section after fold 2 must reflect the CURRENT total (3), "
        f"not fold 1's stale count (1) or an empty one — got: "
        f"{wire_after_fold_2['content']!r}"
    )


# ---------------------------------------------------------------------------
# The retry_loop / RecoveryLadder wire-egress point (#5732-adjacent path,
# the SAME injection shape ``on_summary_used``/``spill_fn`` already use)
# ---------------------------------------------------------------------------


def test_retry_loop_fold_wires_spill_reachability_straight_into_main_calls_wire(
    tmp_path: Path,
) -> None:
    """Tier 2: the RETRY-driven fold path (context-overflow recovery,
    ``RecoveryLadder._advance_state_after_fold``) is a SEPARATE
    wire-egress point from ``RouterHistoryBuffer._serialise_turn`` — its
    own ``wrap_summary_as_message`` call feeds ``main_call``'s ``head``
    DIRECTLY (``_router_main_call_for_retry``'s own docstring: "bypassing
    _serialise_turn"). Falsified without the ``spill_reachability_fn``
    injection: this test would see no [spilled_content] section in
    main_call's own received head at all."""
    received_heads: "list[list[dict]]" = []
    attempt = [0]

    async def _main_call(**kwargs):
        attempt[0] += 1
        received_heads.append(list(kwargs.get("head", [])))
        if attempt[0] == 1:
            from reyn.services.compaction.engine import ContextOverflowError
            raise ContextOverflowError("simulated overflow")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    with tempfile.TemporaryDirectory() as td:
        from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner

        learner = TokenMultiplierLearner(storage_path=Path(td) / "m.json")
        raw_middle = [
            {"role": "user", "content": "x" * 500, "seq": i} for i in range(1, 30)
        ]
        asyncio.run(retry_loop(
            SP="sp",
            payload=RetryPayload(
                head=[], raw_middle=raw_middle,
                tail=[{"role": "user", "content": "tail", "seq": 99}],
                new_msg={"role": "user", "content": "hi", "seq": 100},
                seq_by_id={},
            ),
            cfg=CompactionConfig(use_chars4_estimate=True),
            model="test-model",
            engine=_CapturingEngine(),
            learner=learner,
            main_call=_main_call,
            spill_reachability_fn=lambda: (7, ".reyn/memory/history-content/x/y"),
        ))

    # exactly 2 attempts: the overflow, then the recovered retry — unpack
    # raises otherwise, so this also pins the count without a bare len().
    [_overflowed_head, _recovered_head] = received_heads
    for i, head in enumerate(received_heads):
        summary_entries = [t for t in head if t.get("role") == "summary"]
        assert summary_entries, f"attempt {i + 1}'s head carried no fold at all"
        assert "7 tool result(s)" in summary_entries[0]["content"], (
            f"attempt {i + 1}'s own fold did not carry the injected "
            f"spill_reachability_fn's value onto main_call's wire"
        )


def test_retry_loop_fold_omits_the_section_when_no_fn_is_injected() -> None:
    """Tier 2: falsify pair (deny side) — a caller that wires no
    spill_reachability_fn at all (the pre-#5720 shape, and every
    OTHER existing retry_loop test in this repo) is byte-unaffected;
    the new parameter's default preserves prior behaviour exactly."""
    received_heads: "list[list[dict]]" = []
    attempt = [0]

    async def _main_call(**kwargs):
        attempt[0] += 1
        received_heads.append(list(kwargs.get("head", [])))
        if attempt[0] == 1:
            from reyn.services.compaction.engine import ContextOverflowError
            raise ContextOverflowError("simulated overflow")
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000), choices=[])

    with tempfile.TemporaryDirectory() as td:
        from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner

        learner = TokenMultiplierLearner(storage_path=Path(td) / "m.json")
        raw_middle = [
            {"role": "user", "content": "x" * 500, "seq": i} for i in range(1, 30)
        ]
        asyncio.run(retry_loop(
            SP="sp",
            payload=RetryPayload(
                head=[], raw_middle=raw_middle,
                tail=[{"role": "user", "content": "tail", "seq": 99}],
                new_msg={"role": "user", "content": "hi", "seq": 100},
                seq_by_id={},
            ),
            cfg=CompactionConfig(use_chars4_estimate=True),
            model="test-model",
            engine=_CapturingEngine(),
            learner=learner,
            main_call=_main_call,
        ))

    summary_entries = [t for t in received_heads[-1] if t.get("role") == "summary"]
    assert summary_entries
    assert "spilled_content" not in summary_entries[0]["content"]
