"""Tier 2: OS invariant — chat tool-result cap is offload-based + by-construction bounded (#1128).

The size-axis fix for conversation dead-end #1: an oversized chat tool result is
OFFLOADED via the #385 store (full body saved, lossless, restorable) and replaced
inline with a bounded preview whose estimated tokens are ``<= cap_tokens``.
Because ``cap_tokens = min(FIXED_CEIL, floor(0.5·effective_trigger)) < effective_trigger``,
the capped result is single-turn compactable on every model — so retry_loop's
shrink can always fold it (closes the dead-end).

These pin the helper's contract against the real ``MediaStore.save_tool_result``
store + real ``read_tool_result`` read-back (no mocks):
  - under-cap content is identity (no offload),
  - over-cap content is offloaded + the inline preview is ``<= cap_tokens`` (the
    by-construction bound, across small + large caps = model-independent),
  - the full body reads back losslessly via ``read_tool_result`` (the
    no-lossy-truncate guarantee — body is never discarded or raw-``[:N]``-cut),
  - cap_tokens<=0 disables the cap.

``use_chars4=True`` matches the chars//4 estimator deterministically offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.services.tool_result_cap import (
    MAX_TOOL_RESULT_INLINE_BYTES,
    cap_tool_result_content,
    compute_cap_tokens,
)
from reyn.services.compaction.engine import estimate_tokens

_MODEL = "gpt-4o"


def test_under_cap_content_is_identity(tmp_path: Path) -> None:
    """Tier 2: a result within cap_tokens is returned unchanged (no offload)."""
    store = MediaStore(project_root=tmp_path)
    content = "small tool result"
    out = cap_tool_result_content(
        content, cap_tokens=2048, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True,
    )
    assert out == content
    # Nothing stored when under cap.
    tr_dir = store.tool_results_dir
    assert not tr_dir.exists() or not any(tr_dir.iterdir())


@pytest.mark.parametrize("cap_tokens", [256, 1024, 4096])
def test_over_cap_preview_is_within_cap_tokens(tmp_path: Path, cap_tokens: int) -> None:
    """Tier 2: an oversized result's inline preview is <= cap_tokens (by-construction).

    Holds across small + large caps — the dead-end-#1 closure is model-independent
    (covers the α-composition + bare-marker sanity-checks: the preview always
    fits the budget regardless of cap size).
    """
    store = MediaStore(project_root=tmp_path)
    content = "X" * 400_000  # ~100k tokens (chars//4) — far over any cap
    out = cap_tool_result_content(
        content, cap_tokens=cap_tokens, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True,
    )
    assert out != content, "oversized content must be offloaded, not returned raw"
    assert estimate_tokens(out, _MODEL, use_chars4=True) <= cap_tokens, (
        "the offloaded inline preview must itself fit cap_tokens so it is "
        "single-turn compactable (the by-construction dead-end-#1 bound)"
    )
    assert len(out) <= MAX_TOOL_RESULT_INLINE_BYTES
    assert "_offload_ref" in out


def test_offloaded_body_reads_back_lossless(tmp_path: Path) -> None:
    """Tier 2: the full body is recoverable via MediaStore.read_tool_result (lossless).

    The no-lossy-truncate guarantee: the body is stored, never discarded or
    raw-truncated. The inline preview is just a bounded pointer.
    """
    store = MediaStore(project_root=tmp_path)
    content = "LINE\n" * 50_000  # large, distinctive
    out = cap_tool_result_content(
        content, cap_tokens=512, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True,
    )
    ref = json.loads(out)["_offload_ref"]

    body, found = store.read_tool_result(ref)
    assert found, f"read_tool_result could not locate the offloaded body at {ref!r}"
    assert body == content, "read-back must return the full original body (lossless)"


def test_cap_disabled_when_zero(tmp_path: Path) -> None:
    """Tier 2: cap_tokens<=0 disables the cap (identity, no offload)."""
    store = MediaStore(project_root=tmp_path)
    content = "Y" * 100_000
    out = cap_tool_result_content(
        content, cap_tokens=0, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True,
    )
    assert out == content


# ── load-bearing integration: capped turn folds via retry_loop, uncapped dead-ends ──
#
# The dead-end-#1 proof (#1128): a single oversized tool result can never be
# compacted away → retry_loop's shrink can't fold it → UnrecoveredError. Capping
# it (≤ cap_tokens < B_M) makes the single turn fit a compaction call, so
# retry_loop folds it WITHOUT CompactionOverflowError. This drives the REAL
# retry_loop + real ComputedBudgets + real compute_cap_tokens + real cap helper;
# engine.compact is gated to the real B_M overflow semantics (engine.py:930-934)
# without a live LLM.

def _budgets():
    from reyn.services.compaction.engine import ComputedBudgets
    return ComputedBudgets(
        main_pool=10_000, head_budget=200, body_budget=500,
        tail_budget=200, new_msg_budget=1_000,
        B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
        section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                      "session_user_facts": 50, "artifacts_referenced": 175},
    )


class _BMGatedEngine:
    """Real ComputedBudgets + a compact() gated on B_M, mirroring the real
    engine's overflow semantics (engine.py:930-934) — no live LLM. A chunk whose
    estimated tokens exceed B_M raises CompactionOverflowError (= the dead-end);
    otherwise it folds into a stub summary."""

    def __init__(self, budgets):
        self.budgets = budgets

    async def compact(self, input_chunk):
        from reyn.services.compaction.engine import (
            ChatSummary,
            CompactionOverflowError,
        )
        prev = json.dumps(input_chunk.previous_summary or {}, ensure_ascii=False)
        turns = json.dumps(input_chunk.new_turns, ensure_ascii=False, default=str)
        total = estimate_tokens(prev + turns, _MODEL, use_chars4=True)
        if total > self.budgets.B_M:
            raise CompactionOverflowError(f"chunk {total} tok > B_M {self.budgets.B_M}")
        seq = max(
            (t.get("seq", 0) for t in input_chunk.new_turns if isinstance(t, dict)),
            default=0,
        )
        return ChatSummary(topic_arc="folded", covers_through_seq=seq)


async def _size_aware_main_call(**kwargs):
    """Main send that overflows when the assembled prompt exceeds main_M_room.

    Needed to surface the dead-end: retry_loop *defers* an overflowing
    raw_middle turn into the tail (it doesn't raise), so without a size-aware
    main call a big-but-deferred turn would just pass. With this, the uncapped
    big turn bounces raw_middle↔tail (compact overflows in middle, main overflows
    in tail) until UnrecoveredError — exactly the dead-end the cap closes.
    """
    from types import SimpleNamespace

    from reyn.services.compaction.engine import ContextOverflowError

    head = kwargs.get("head") or []
    summary = kwargs.get("summary")
    tail = kwargs.get("tail") or []
    new_msg = kwargs.get("new_msg") or {}
    text = (
        json.dumps(head, default=str)
        + json.dumps(summary or {}, default=str)
        + json.dumps(tail, default=str)
        + json.dumps(new_msg, default=str)
    )
    if estimate_tokens(text, _MODEL, use_chars4=True) > _budgets().main_M_room:
        raise ContextOverflowError("main prompt exceeds main_M_room")
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=500), choices=[])


def _retry_loop_with(raw_middle_turn: dict):
    import asyncio
    import tempfile

    from reyn.config import CompactionConfig
    from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
    from reyn.services.compaction.engine import retry_loop

    learner = TokenMultiplierLearner(
        storage_path=Path(tempfile.mkdtemp()) / "m.json"
    )
    return asyncio.run(retry_loop(
        SP="system",
        head=[],
        summary=None,
        raw_middle=[raw_middle_turn],
        tail=[],
        new_msg={"role": "user", "content": "hi", "seq": 99},
        cfg=CompactionConfig(),
        model=_MODEL,
        engine=_BMGatedEngine(_budgets()),  # type: ignore[arg-type]
        learner=learner,
        main_call=_size_aware_main_call,
        max_iterations=8,
    ))


def test_capped_tool_turn_folds_via_retry_loop_without_overflow(tmp_path: Path) -> None:
    """Tier 2: load-bearing — a CAPPED oversized tool result folds via retry_loop.

    The dead-end-#1 closure: cap an oversized tool result (≤ cap_tokens < B_M),
    put it in raw_middle, run the real retry_loop — engine.compact succeeds (the
    capped turn fits B_M), so it folds with NO CompactionOverflowError /
    UnrecoveredError.
    """
    store = MediaStore(project_root=tmp_path)
    cap_tokens = compute_cap_tokens(_budgets().effective_trigger)
    capped = cap_tool_result_content(
        "Z" * 80_000,  # ~20k tokens — far over B_M=8000, the dead-end shape
        cap_tokens=cap_tokens, model=_MODEL,
        save_fn=store.save_tool_result, use_chars4=True,
    )
    assert estimate_tokens(capped, _MODEL, use_chars4=True) < _budgets().B_M, (
        "precondition: the capped turn must fit the compaction budget"
    )
    result = _retry_loop_with({"role": "tool", "content": capped, "seq": 5})
    assert result is not None  # folded + main_call succeeded, no exception raised


def test_uncapped_oversized_tool_turn_dead_ends(tmp_path: Path) -> None:
    """Tier 2: contrast — the UNCAPPED oversized turn dead-ends, proving the cap is load-bearing.

    Without the cap, the single oversized tool turn exceeds B_M, engine.compact
    keeps raising CompactionOverflowError, retry_loop's shrink cannot fold a
    single un-splittable turn → it terminates in error. This is exactly the
    dead-end the cap closes.
    """
    from reyn.services.compaction.engine import (
        CompactionOverflowError,
        UnrecoveredError,
    )

    uncapped = "Z" * 80_000  # ~20k tokens > B_M=8000
    assert estimate_tokens(uncapped, _MODEL, use_chars4=True) > _budgets().B_M
    with pytest.raises((UnrecoveredError, CompactionOverflowError)):
        _retry_loop_with({"role": "tool", "content": uncapped, "seq": 5})
