"""Tier 2: #5598 — a summary turn reaches the wire with a `role` the
provider actually accepts, never reyn's own internal `SUMMARY_MESSAGE_
ROLE` ("summary") discriminator.

Owner's real machine (relayed by lead-coder, 2026-08-30): the turn right
after a compaction SUCCEEDS always 400s. Verbatim upstream error:
"Invalid value: 'summary'. Supported values are: 'assistant', 'system',
'developer', and 'user'." — `param: input[391]`, rejected in ~2 seconds
regardless of payload size (63万字 ≈ 160k tokens against a 1.05M window —
not a size problem; the request is rejected before inference even starts,
so no amount of shrinking can ever recover from it).

## Root cause (traced directly, not assumed)

`retry_loop`'s own fold-success branch (`engine.py`) appends a FRESH
`wrap_summary_as_message(...)` result straight into `head` —
`{"role": SUMMARY_MESSAGE_ROLE, ...}` — bypassing `_serialise_turn`
entirely (that dict is built directly, never routed through the
normal-turn wire-serialisation path). `RouterLoopDriver._router_main_call`
(`router_loop_driver.py`) then sends `head` (plus `tail`) to `loop.run()`
as-is, after only stripping the internal `spillability` key — `role`
itself was never remapped.

`build_history()` (the NORMAL, non-overflow turn path) does NOT have
this bug: its own summary handling attaches a SEPARATE, already-
`"assistant"`-role synthetic bridge turn (`router_history_buffer.py`,
"the bridge") and never touches `wrap_summary_as_message` at all — this
issue's own shape is confined to the overflow-recovery path
(`decompose_history_for_retry` → `retry_loop` → `_router_main_call`),
which is exactly why the owner's incident showed up specifically as
"the turn right after a compaction succeeds" (compaction only ever runs
as part of overflow recovery here) rather than on every ordinary turn.

## Falsified before writing (lead-coder's own 2-point ask)

1. Does `wrap_summary_as_message`'s output ALSO feed `HistoryChunkToCompact`
   (`compaction_controller.py`'s own call site, and `retry_loop`'s own
   `raw_middle` inclusion)? Yes — confirmed by reading both call sites
   directly. The fix therefore does NOT touch `wrap_summary_as_message`
   itself, nor `_serialise_turn`'s summary branch (whose OWN output can
   also flow into `HistoryChunkToCompact.messages` via `raw_middle`) —
   only `_router_main_call`'s own existing wire-shaping step, the ONE
   place `head`/`tail` (never `raw_middle`) actually become `loop.run`'s
   payload (matching the file's own pre-existing precedent: it already
   strips `spillability` HERE rather than inside `_serialise_turn`, for
   exactly the same "canonical quantity vs. wire shape" reason).
2. Does `engine.py`'s own `m.get("role") == SUMMARY_MESSAGE_ROLE` check
   (`compact()`'s own "does a previous summary already exist" read) ever
   see a wire-normalized dict? No — confirmed by reading `compact()`
   directly: `input_chunk.messages` is embedded as JSON TEXT inside one
   `"user"`-role wire message to compact()'s OWN LLM call, never
   serialised as individual wire roles — `SUMMARY_MESSAGE_ROLE` never
   reaches a real wire role field on THAT path either way, so this fix
   (scoped to `_router_main_call` alone) cannot touch it.

Real `Session`/`RouterLoopDriver`/`CompactionEngine` throughout (a fake
``litellm.acompletion`` for the compaction call, same idiom
``test_5582_compaction_forced_non_streaming.py`` already establishes; a
content-driven fake `loop.run` for the main call, same idiom
``test_5296_pr2_byte_reduction_same_turn_retry.py`` already establishes
for this exact scenario shape).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage, Spillability
from tests._support.agent_session import make_session
from tests._support.events import settle

_SUMMARY_CONTENT = {
    "topic_arc": "the conversation so far", "new_turn_seqs": [],
    "decisions": [], "pending": [], "session_user_facts": [], "artifacts_referenced": [],
}


def _now() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RecordingLoop:
    """A fake ``RouterLoop`` that raises a genuine overflow-shaped error
    exactly while the FIRST call's own content (``raw_middle``'s
    candidates, still un-folded) is present — mirroring
    ``_ContentDrivenLoop``'s own "content-driven, not a hardcoded call
    count" idiom — and succeeds from then on, once ``retry_loop``'s own
    fold has replaced that content with a summary. Records the exact
    ``history`` payload of EVERY call, so the test inspects the real wire
    shape ``_router_main_call`` produced rather than inferring it."""

    def __init__(self, *, fail_marker: str) -> None:
        self._fail_marker = fail_marker
        self.calls: "list[list[dict]]" = []

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        self.calls.append(history)
        if any(self._fail_marker in str(t.get("content", "")) for t in history):
            raise _FakeStatusError("request too large", status_code=413)
        return None


def test_summary_reaches_wire_with_an_accepted_role_not_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5598 accept — after retry_loop's own compact() succeeds
    on real, non-empty ``raw_middle`` content and folds it into a fresh
    summary appended to ``head``, the VERY NEXT ``main_call`` (still
    inside the SAME recovery episode — the owner's own incident shape)
    must never include a wire dict whose ``role`` is
    ``SUMMARY_MESSAGE_ROLE`` ("summary")."""
    from reyn.services.compaction.engine import SUMMARY_MESSAGE_ROLE

    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2_500)

    async def _fake_acompletion(model, messages, **kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(_SUMMARY_CONTENT)),
            )],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
        )
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    cfg = CompactionConfig(
        body_token_cap=1500, use_chars4_estimate=True, section_caps_spec_tokens=0,
        max_shrink_iterations=1,
    )
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    session = make_session(
        agent_name="default", agent_role="", output_language="en",
        budget_tracker=bt, state_log=state_log, compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )

    # Real filler (plain user/assistant, spill-ineligible) surrounding a
    # single, large tool-result turn — enough that
    # decompose_history_for_retry puts genuine content into raw_middle
    # (not just head/tail), so retry_loop's own rung① compact() call
    # actually fires. Filler AFTER the tool turn keeps `tail` itself free
    # of any "tool"-role content, isolating `_run_with_shrink`'s own
    # FIRST attempt (via `build_history()`, which sends everything raw)
    # as the one call whose content genuinely triggers the fake 413 —
    # not a later, already-folded call whose tail happens to share the
    # same marker text.
    for i in range(20):
        _push(session, "user", f"filler question {i} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"filler answer {i} " * 8, spillability=Spillability.NEVER)
    _push(session, "tool", "mid result payload " * 100, tool_call_id="tc-mid0", name="tool")
    for i in range(3):
        _push(session, "user", f"tail filler {i} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"tail reply {i} " * 8, spillability=Spillability.NEVER)

    head, raw_middle, tail, _summary, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    assert mid_ids == {"tc-mid0"}, (
        f"test setup sanity: raw_middle must hold the real candidate for "
        f"retry_loop's own compact() to fire — got {mid_ids!r}"
    )
    assert not [t for t in tail if t.get("role") == "tool"], (
        "test setup sanity: tail must hold no tool candidate, or the "
        "fake 413's own content marker cannot isolate the FIRST "
        "(un-folded) attempt from a later, already-folded one"
    )

    loop = _RecordingLoop(fail_marker="mid result payload")
    asyncio.run(session._loop_driver._run_with_shrink(loop, "continue please", chain_id="c1"))
    asyncio.run(settle(session))

    assert loop.calls, "loop.run must have been called at least once"
    for i, history in enumerate(loop.calls):
        offending = [t for t in history if t.get("role") == SUMMARY_MESSAGE_ROLE]
        assert not offending, (
            f"call {i}: history sent to loop.run() carries a wire dict "
            f"with role=={SUMMARY_MESSAGE_ROLE!r} — the exact shape a "
            f"provider that validates role names rejects with a 400 in "
            f"~2 seconds, before inference even starts: {offending!r}"
        )
    summary_present = any(
        "[summary of earlier conversation]" in str(t.get("content", ""))
        for history in loop.calls for t in history
    )
    assert summary_present, (
        "test setup sanity: the fresh summary must actually be present "
        "in what was sent (as an accepted role) — otherwise this test "
        "isn't exercising the bug's own trigger at all"
    )
