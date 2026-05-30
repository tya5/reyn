"""Tier 2: control_ir_results per-result offload invariants (C5 — FP-0008).

When a single control_ir_result's JSON serialisation exceeds
MAX_CONTROL_IR_RESULT_INLINE_BYTES (~8KB), the OS offloads the full content
to a workspace scratch file and replaces the inline slot with a head+tail
preview + a ref path. The LLM can file.read the full content when needed.
No information is lost.

Covered invariants:
1. Oversized result is bounded inline (≤ OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS +
   marker, well under original 200K).
2. Head preview present: inline result starts with the original head text.
3. Ref reaches full text: reading the offload file yields the full original content.
4. Small result passes through unchanged (identity).
5. Multiple large results each offloaded independently.
6. Observability: control_ir_result_offloaded event emitted with idx + original size.
7. build_frame integration: oversized result in control_ir_results is bounded in
   the resulting ContextFrame's control_ir_results field.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch.
- No private-state assertions.
- Each docstring opens with ``Tier 2: ...``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.context_builder import (
    MAX_CONTROL_IR_RESULT_INLINE_BYTES,
    OFFLOAD_HEAD_CHARS,
    OFFLOAD_TAIL_CHARS,
    maybe_offload_control_ir_results,
    offload_control_ir_result,
)
from reyn.events.events import EventLog
from reyn.kernel.runtime import OSRuntime
from reyn.schemas.models import Phase, Skill, SkillGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_big_result(content_size: int = 200_000) -> dict:
    """Build a control_ir_result with a ``content`` field of *content_size* chars."""
    return {
        "kind": "file.read",
        "status": "ok",
        "content": "A" * content_size,
    }


def _serialized_size(obj: dict) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def _make_events() -> EventLog:
    return EventLog()


def _one_phase_skill() -> Skill:
    draft = Phase(
        name="draft",
        instructions="draft instructions",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="offload_test_skill",
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


# ---------------------------------------------------------------------------
# 1. Oversized single result is bounded inline
# ---------------------------------------------------------------------------


def test_oversized_result_is_bounded_inline(tmp_path: Path) -> None:
    """Tier 2: oversized control_ir_result inline size is bounded after offload.

    A result with a ~200K content field (>> 8KB threshold) must produce an
    inline dict whose JSON serialisation is well under the original size —
    specifically bounded by OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS plus
    overhead, not the original 200K.
    """
    result = _make_big_result(200_000)
    original_size = _serialized_size(result)
    assert original_size > MAX_CONTROL_IR_RESULT_INLINE_BYTES, "precondition: result is oversized"

    offload_dir = tmp_path / "offload"
    inline = offload_control_ir_result(result, 0, offload_dir)

    inline_size = _serialized_size(inline)
    # The inline size must be well below the original (not just slightly smaller)
    # and within a factor of 2× of (OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS) for overhead.
    bounded_ceiling = (OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS) * 3 + 2_048  # generous overhead
    assert inline_size < bounded_ceiling, (
        f"inline_size={inline_size} exceeds bounded ceiling={bounded_ceiling}; "
        f"original={original_size}"
    )
    assert inline_size < original_size, "inline must be smaller than original"


# ---------------------------------------------------------------------------
# 2. Head preview present
# ---------------------------------------------------------------------------


def test_head_preview_present_in_offloaded_result(tmp_path: Path) -> None:
    """Tier 2: offloaded result retains the first OFFLOAD_HEAD_CHARS chars of the original content.

    The LLM must be able to see the start of the result without a round-trip
    file.read, so the head preview is non-negotiable.
    """
    original_content = "B" * 200_000
    result = {"kind": "file.read", "status": "ok", "content": original_content}

    offload_dir = tmp_path / "offload"
    inline = offload_control_ir_result(result, 0, offload_dir)

    inline_content = inline["content"]
    expected_head = original_content[:OFFLOAD_HEAD_CHARS]
    assert inline_content.startswith(expected_head), (
        f"inline content does not start with the expected head preview "
        f"(first {OFFLOAD_HEAD_CHARS} chars)"
    )


# ---------------------------------------------------------------------------
# 3. Ref reaches full text (no information loss)
# ---------------------------------------------------------------------------


def test_ref_reaches_full_original_content(tmp_path: Path) -> None:
    """Tier 2: the offload file at _offload_ref contains the complete original content.

    No information is lost: reading the workspace ref path yields the exact
    same JSON as the original result dict.
    """
    original_content = "C" * 200_000
    result = {"kind": "file.read", "status": "ok", "content": original_content}

    offload_dir = tmp_path / "offload"
    inline = offload_control_ir_result(result, 0, offload_dir)

    ref_path = inline.get("_offload_ref")
    assert ref_path is not None, "offloaded result must carry _offload_ref key"

    # Read back and verify full content equality
    stored_text = Path(ref_path).read_text(encoding="utf-8")
    stored = json.loads(stored_text)
    assert stored == result, (
        "Offload file content does not match original result — information lost"
    )


# ---------------------------------------------------------------------------
# 4. Small result passes through unchanged (identity)
# ---------------------------------------------------------------------------


def test_small_result_passes_through_unchanged(tmp_path: Path) -> None:
    """Tier 2: a control_ir_result below MAX_CONTROL_IR_RESULT_INLINE_BYTES is returned as-is.

    The offload path must be transparent for small results so only genuinely
    oversized results incur the file write.
    """
    small_result = {"kind": "file.read", "status": "ok", "content": "tiny content"}
    assert _serialized_size(small_result) <= MAX_CONTROL_IR_RESULT_INLINE_BYTES

    offload_dir = tmp_path / "offload"
    returned = offload_control_ir_result(small_result, 0, offload_dir)

    assert returned is small_result, (
        "Small result must be returned unchanged (identity) — no copy, no offload"
    )
    assert not offload_dir.exists(), "No offload dir should be created for small results"


# ---------------------------------------------------------------------------
# 5. Multiple large results each offloaded independently
# ---------------------------------------------------------------------------


def test_multiple_large_results_each_offloaded(tmp_path: Path) -> None:
    """Tier 2: two oversized control_ir_results are each offloaded to separate files.

    Each result must carry its own independent _offload_ref — they must not
    share a file (= independent offload per result).
    """
    result_a = _make_big_result(100_000)
    result_b = _make_big_result(150_000)
    # Make content distinguishable
    result_a["content"] = "A" * 100_000
    result_b["content"] = "B" * 150_000

    offload_dir = tmp_path / "offload"
    inline_a = offload_control_ir_result(result_a, 0, offload_dir)
    inline_b = offload_control_ir_result(result_b, 1, offload_dir)

    ref_a = inline_a.get("_offload_ref")
    ref_b = inline_b.get("_offload_ref")
    assert ref_a is not None and ref_b is not None, "Both results must have _offload_ref"
    assert ref_a != ref_b, "Each result must be offloaded to a separate file"

    # Verify each ref reaches the correct full original
    stored_a = json.loads(Path(ref_a).read_text(encoding="utf-8"))
    stored_b = json.loads(Path(ref_b).read_text(encoding="utf-8"))
    assert stored_a == result_a, "Ref A must point at result_a"
    assert stored_b == result_b, "Ref B must point at result_b"


# ---------------------------------------------------------------------------
# 6. Observability: offload event emitted
# ---------------------------------------------------------------------------


def test_offload_event_emitted_with_metadata(tmp_path: Path) -> None:
    """Tier 2: offloading a large result emits control_ir_result_offloaded event.

    The event carries result_idx + original_size_chars so the offload is
    audit-visible and not silent.
    """
    events = _make_events()
    original_content = "D" * 50_000
    result = {"kind": "file.read", "status": "ok", "content": original_content}
    original_size = _serialized_size(result)

    offload_dir = tmp_path / "offload"
    offload_control_ir_result(result, 3, offload_dir, events=events, phase="explore")

    emitted = [e for e in events.all() if e.type == "control_ir_result_offloaded"]
    (only_event,) = emitted  # exactly one offload event for one oversized result
    ev = only_event.data
    assert ev["result_idx"] == 3
    assert ev["original_size_chars"] == original_size
    assert ev["phase"] == "explore"
    assert "offload_path" in ev


def test_no_event_emitted_for_small_result(tmp_path: Path) -> None:
    """Tier 2: no control_ir_result_offloaded event is emitted for a small result.

    Only genuinely oversized results should trigger events (= no noise).
    """
    events = _make_events()
    small = {"kind": "file.read", "status": "ok", "content": "tiny"}

    offload_dir = tmp_path / "offload"
    offload_control_ir_result(small, 0, offload_dir, events=events, phase="explore")

    offload_events = [e for e in events.all() if e.type == "control_ir_result_offloaded"]
    assert offload_events == [], "No offload event for a small result"


# ---------------------------------------------------------------------------
# 7. maybe_offload_control_ir_results: None offload_dir = passthrough
# ---------------------------------------------------------------------------


def test_none_offload_dir_passes_results_unchanged() -> None:
    """Tier 2: when offload_dir is None, results are returned unchanged (backward compat).

    Callers that don't supply an offload_dir must see exact passthrough
    so existing behaviour is preserved for non-OSRuntime callers.
    """
    results = [
        {"kind": "file.read", "status": "ok", "content": "X" * 200_000},
        {"kind": "file.write", "status": "ok"},
    ]
    returned = maybe_offload_control_ir_results(results, offload_dir=None)
    # Must be the same list objects (passthrough, no copy)
    assert returned is results


# ---------------------------------------------------------------------------
# 8. build_frame integration: oversized result is bounded in ContextFrame
# ---------------------------------------------------------------------------


def test_build_frame_oversized_control_ir_result_is_bounded(tmp_path: Path) -> None:
    """Tier 2: build_frame with a real OSRuntime offloads oversized control_ir_results.

    A ~200K content field in a control_ir_result must produce a bounded inline
    entry in the resulting ContextFrame.control_ir_results. Uses a real OSRuntime
    (tmp_path workspace, no MagicMock).
    """
    import pytest
    pytest.importorskip("litellm")  # skip if litellm not installed

    import os
    os.chdir(tmp_path)

    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model",
        run_id="offload_integration_test",
        workspace_base_dir=tmp_path,
    )

    big_result = _make_big_result(200_000)
    original_size = _serialized_size(big_result)

    frame = rt.build_frame(
        "draft",
        {"type": "input", "data": {}},
        [],
        "en",
        control_ir_results=[big_result],
    )

    (inline,) = frame.control_ir_results  # exactly one result (the one we passed in)
    inline_size = _serialized_size(inline)

    bounded_ceiling = (OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS) * 3 + 2_048
    assert inline_size < bounded_ceiling, (
        f"Frame control_ir_results[0] inline_size={inline_size} not bounded "
        f"(original={original_size}, ceiling={bounded_ceiling})"
    )
    # Ref must be present and reachable
    ref_path = inline.get("_offload_ref")
    assert ref_path is not None, "Offloaded result must carry _offload_ref"
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == big_result, "Ref file must contain the full original result"
