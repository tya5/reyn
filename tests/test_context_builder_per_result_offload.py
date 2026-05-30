"""Tier 2: control_ir_results per-result offload invariants (C5 — FP-0008, v9-IN + Phase 1 migration).

When a single control_ir_result's JSON serialisation exceeds
MAX_CONTROL_IR_RESULT_INLINE_BYTES (~8KB), the OS offloads the full content
to a workspace scratch file and replaces the inline slot with a bounded
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

C5-completeness additions (v9-IN):
8. LIST-bulk invariant: result with huge list field → inline ≤ MAX_OFFLOADED_INLINE_BYTES;
   ref reads back to original (THE regression guard).
9. dict-bulk invariant: result with huge dict field → same bound.
10. string-bulk regression: existing string behavior preserved (head+tail+ref).
11. many-medium-fields: many fields each just under per-field threshold → hard-bound
    fallback triggers → inline ≤ MAX_OFFLOADED_INLINE_BYTES.
12. build_frame integration with list-bulk: multi-MB list result → bound holds via
    the real build_frame path.

Phase 1 migration additions:
13. Offloaded result now carries ``_offload_content_hash`` (new: Phase 1 common core wires content hash).
14. Content hash read-back: read_offloaded(path_ref, content_hash=hash) == original full result.
15. build_frame list-bulk: inline ≤ MAX_OFFLOADED_INLINE_BYTES + content_hash present (integration via real build_frame).

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
    MAX_OFFLOADED_INLINE_BYTES,
    OFFLOAD_HEAD_CHARS,
    OFFLOAD_TAIL_CHARS,
    maybe_offload_control_ir_results,
    offload_control_ir_result,
)
from reyn.events.events import EventLog
from reyn.kernel.runtime import OSRuntime
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.services.offload.store import read_offloaded

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


# ---------------------------------------------------------------------------
# C5-completeness: list-bulk invariant (THE regression guard — FP-0008 v9-IN)
# ---------------------------------------------------------------------------


def test_list_bulk_inline_bounded_and_ref_complete(tmp_path: Path) -> None:
    """Tier 2: LIST-bulk result inline is bounded by MAX_OFFLOADED_INLINE_BYTES.

    A result whose bulk is a large LIST (e.g. grep matches with content) MUST
    produce an inline whose serialised size is ≤ MAX_OFFLOADED_INLINE_BYTES,
    AND the ref file must contain the full original result (no info loss).

    This is the direct regression guard for the C5-completeness bug: before
    the fix, list/dict fields were left full-size in the inline, causing
    multi-MB prompt balloons (observed: 3.4M chars, 987K tokens).
    """
    # Build a result with a large list field (~multi-MB).
    # 20,000 items × ~210 chars each ≈ 4.2MB — well above any inline threshold.
    big_list_result = {
        "kind": "file",
        "op": "grep",
        "status": "ok",
        "matches": [{"path": f"p{i}", "line": "x" * 200} for i in range(20_000)],
    }

    offload_dir = tmp_path / "offload"
    offloaded = offload_control_ir_result(big_list_result, 0, offload_dir)

    # THE invariant: inline must be bounded regardless of list bulk
    inline_size = len(json.dumps(offloaded, ensure_ascii=False))
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"LIST-bulk inline_size={inline_size:,} exceeds MAX_OFFLOADED_INLINE_BYTES="
        f"{MAX_OFFLOADED_INLINE_BYTES:,}; list bulk was NOT bounded"
    )

    # No info loss: ref reads back to original
    ref_path = offloaded.get("_offload_ref")
    assert ref_path is not None, "Offloaded result must carry _offload_ref"
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == big_list_result, (
        "Ref file must contain the full original list-bulk result (no info loss)"
    )


def test_list_bulk_head_preview_present(tmp_path: Path) -> None:
    """Tier 2: offloaded list-bulk result contains a partial list preview in the inline.

    The LLM must see the start of the list without a round-trip file.read.
    The first element of the original list must appear somewhere in the inline.
    """
    big_list_result = {
        "kind": "grep",
        "status": "ok",
        "matches": [{"path": f"file_{i}.py", "line": f"match line {i}"} for i in range(5_000)],
    }

    offload_dir = tmp_path / "offload"
    offloaded = offload_control_ir_result(big_list_result, 0, offload_dir)

    inline_json = json.dumps(offloaded, ensure_ascii=False)
    # First element path "file_0.py" must appear in inline (head preview)
    assert "file_0.py" in inline_json, (
        "Inline must contain the first list element (head preview for LLM utility)"
    )


# ---------------------------------------------------------------------------
# C5-completeness: dict-bulk invariant
# ---------------------------------------------------------------------------


def test_dict_bulk_inline_bounded_and_ref_complete(tmp_path: Path) -> None:
    """Tier 2: dict-bulk result inline is bounded by MAX_OFFLOADED_INLINE_BYTES.

    A result with a large DICT field must produce a bounded inline AND a
    complete ref file. Same guarantee as the list-bulk test.
    """
    big_dict = {str(i): "value_" + "y" * 500 for i in range(5_000)}
    result = {
        "kind": "env",
        "status": "ok",
        "variables": big_dict,
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized) > MAX_CONTROL_IR_RESULT_INLINE_BYTES, (
        "Precondition: result must exceed inline threshold"
    )

    offload_dir = tmp_path / "offload"
    offloaded = offload_control_ir_result(result, 0, offload_dir)

    inline_size = len(json.dumps(offloaded, ensure_ascii=False))
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"dict-bulk inline_size={inline_size:,} exceeds MAX_OFFLOADED_INLINE_BYTES="
        f"{MAX_OFFLOADED_INLINE_BYTES:,}"
    )

    ref_path = offloaded.get("_offload_ref")
    assert ref_path is not None, "dict-bulk: must carry _offload_ref"
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == result, "Ref file must contain full original dict-bulk result"


# ---------------------------------------------------------------------------
# C5-completeness: string-bulk regression (existing behavior preserved)
# ---------------------------------------------------------------------------


def test_string_bulk_inline_bounded_head_tail_preserved(tmp_path: Path) -> None:
    """Tier 2: string-bulk result still gets head+tail preview after C5-completeness fix.

    Regression guard: the new type-aware code must preserve existing str behavior —
    head (OFFLOAD_HEAD_CHARS) + marker + tail (OFFLOAD_TAIL_CHARS) and bounded.
    """
    original_content = "S" * 300_000
    result = {"kind": "file.read", "status": "ok", "content": original_content}

    offload_dir = tmp_path / "offload"
    offloaded = offload_control_ir_result(result, 0, offload_dir)

    inline_size = len(json.dumps(offloaded, ensure_ascii=False))
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"string-bulk inline_size={inline_size:,} exceeds bound"
    )

    # Head is present in the content field
    content_inline = offloaded.get("content", "")
    assert isinstance(content_inline, str), "String field preview must remain a string"
    expected_head = original_content[:OFFLOAD_HEAD_CHARS]
    assert content_inline.startswith(expected_head), (
        "String field must start with OFFLOAD_HEAD_CHARS preview"
    )

    # Ref reachable
    ref_path = offloaded.get("_offload_ref")
    assert ref_path is not None
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == result, "Ref must contain full original string-bulk result"


# ---------------------------------------------------------------------------
# C5-completeness: many-medium-fields hard-bound fallback
# ---------------------------------------------------------------------------


def test_many_medium_fields_hard_bound_fallback(tmp_path: Path) -> None:
    """Tier 2: many-medium-fields result triggers hard-bound fallback → inline ≤ bound.

    A result with many fields each just under _FIELD_KEEP_THRESHOLD can sum
    to an inline well above MAX_OFFLOADED_INLINE_BYTES after per-field previews.
    The hard-bound fallback must catch this and replace the whole inline with a
    compact head+tail representation that fits within MAX_OFFLOADED_INLINE_BYTES.

    This tests the correctness of the whole-inline-replace fallback (step 3 of
    the bounded-by-construction design).
    """
    from reyn.context_builder import _FIELD_KEEP_THRESHOLD

    # Build a result with many fields each slightly under the per-field threshold
    # so per-field preview logic keeps them all, but the total blows up.
    # Each field value: just under _FIELD_KEEP_THRESHOLD chars of string
    field_value_size = _FIELD_KEEP_THRESHOLD - 100  # just under per-field keep threshold
    # Number of fields to exceed MAX_OFFLOADED_INLINE_BYTES after small-field pass
    n_fields = (MAX_OFFLOADED_INLINE_BYTES // field_value_size) + 20
    result = {f"field_{i}": "M" * field_value_size for i in range(n_fields)}

    serialized = json.dumps(result, ensure_ascii=False)
    assert len(serialized) > MAX_OFFLOADED_INLINE_BYTES, (
        f"Precondition: result must exceed MAX_OFFLOADED_INLINE_BYTES; "
        f"got {len(serialized):,}"
    )
    assert len(serialized) > MAX_CONTROL_IR_RESULT_INLINE_BYTES, (
        "Precondition: result must also exceed the basic inline threshold"
    )

    offload_dir = tmp_path / "offload"
    offloaded = offload_control_ir_result(result, 0, offload_dir)

    inline_size = len(json.dumps(offloaded, ensure_ascii=False))
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"many-medium-fields fallback did not fire: inline_size={inline_size:,} "
        f"exceeds MAX_OFFLOADED_INLINE_BYTES={MAX_OFFLOADED_INLINE_BYTES:,}"
    )

    # Ref reachable and complete
    ref_path = offloaded.get("_offload_ref")
    assert ref_path is not None, "Fallback inline must carry _offload_ref"
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == result, "Ref must contain full original many-medium-fields result"


# ---------------------------------------------------------------------------
# C5-completeness: build_frame integration with list-bulk
# ---------------------------------------------------------------------------


def test_build_frame_list_bulk_control_ir_result_bounded(tmp_path: Path) -> None:
    """Tier 2: build_frame with list-bulk control_ir_result produces bounded inline.

    Integration path: the real build_frame (via OSRuntime) must apply the
    bounded offload to a multi-MB list-bulk result, producing an inline that
    fits within MAX_OFFLOADED_INLINE_BYTES. The ref file must contain the full
    original result.

    This is the integration-path regression guard (the C6 regression missed
    a similar integration path test).
    """
    pytest.importorskip("litellm")

    import os
    os.chdir(tmp_path)

    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model",
        run_id="list_bulk_integration_test",
        workspace_base_dir=tmp_path,
    )

    # 20,000 items × ~210 chars each ≈ 4.2MB — deterministically multi-MB
    big_list_result = {
        "kind": "file",
        "op": "grep",
        "status": "ok",
        "matches": [{"path": f"p{i}", "line": "x" * 200} for i in range(20_000)],
    }

    frame = rt.build_frame(
        "draft",
        {"type": "input", "data": {}},
        [],
        "en",
        control_ir_results=[big_list_result],
    )

    (inline,) = frame.control_ir_results
    inline_size = len(json.dumps(inline, ensure_ascii=False))

    # THE invariant: inline bounded regardless of list bulk
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"build_frame list-bulk inline_size={inline_size:,} exceeds "
        f"MAX_OFFLOADED_INLINE_BYTES={MAX_OFFLOADED_INLINE_BYTES:,}"
    )

    # No info loss via ref
    ref_path = inline.get("_offload_ref")
    assert ref_path is not None, "build_frame offloaded inline must carry _offload_ref"
    stored = json.loads(Path(ref_path).read_text(encoding="utf-8"))
    assert stored == big_list_result, (
        "build_frame ref file must contain the full original list-bulk result"
    )


# ---------------------------------------------------------------------------
# Phase 1 migration: content_hash present in offloaded inline
# ---------------------------------------------------------------------------


def test_offloaded_result_carries_content_hash(tmp_path: Path) -> None:
    """Tier 2: Phase 1 — offloaded inline now carries _offload_content_hash.

    After the common-core migration, every offloaded result must include
    ``_offload_content_hash`` in the inline dict so callers can verify
    integrity via read_offloaded(path_ref, content_hash=...).
    """
    result = _make_big_result(200_000)
    offload_dir = tmp_path / "offload"
    inline = offload_control_ir_result(result, 0, offload_dir)

    assert "_offload_content_hash" in inline, (
        "Phase 1: offloaded inline must carry _offload_content_hash key"
    )
    content_hash = inline["_offload_content_hash"]
    assert content_hash.startswith("sha256:"), (
        f"content_hash must start with 'sha256:', got {content_hash!r}"
    )


def test_offloaded_content_hash_read_back_matches_original(tmp_path: Path) -> None:
    """Tier 2: Phase 1 — content_hash from inline enables verified read-back == original.

    Using the content_hash from the inline, read_offloaded must return the
    full original result without raising, and the parsed result must equal
    the original dict.
    """
    original_content = "E" * 200_000
    result = {"kind": "file.read", "status": "ok", "content": original_content}
    offload_dir = tmp_path / "offload"
    inline = offload_control_ir_result(result, 0, offload_dir)

    ref_path = inline["_offload_ref"]
    content_hash = inline["_offload_content_hash"]

    # Verified read-back using the hash from the inline
    raw, found = read_offloaded(ref_path, base_dir=offload_dir, content_hash=content_hash)
    assert found is True, "read_offloaded must find the offloaded file"
    stored = json.loads(raw)
    assert stored == result, (
        "Verified read-back via content_hash must yield the full original result"
    )


# ---------------------------------------------------------------------------
# Phase 1 migration: build_frame list-bulk — inline bounded + content_hash present
# ---------------------------------------------------------------------------


def test_build_frame_list_bulk_bounded_and_content_hash_present(tmp_path: Path) -> None:
    """Tier 2: Phase 1 — build_frame list-bulk: inline ≤ MAX_OFFLOADED_INLINE_BYTES + hash present.

    Integration path via real build_frame (OSRuntime): list-bulk result must
    produce a bounded inline AND the inline must carry _offload_content_hash
    so callers can do verified read-back.

    This is the integration-path guard for Phase 1 migration: both C5-completeness
    (the bound invariant) and the new content_hash addition must hold simultaneously.
    """
    pytest.importorskip("litellm")

    import os
    os.chdir(tmp_path)

    rt = OSRuntime(
        _one_phase_skill(),
        model="stub/model",
        run_id="list_bulk_hash_integration_test",
        workspace_base_dir=tmp_path,
    )

    big_list_result = {
        "kind": "file",
        "op": "grep",
        "status": "ok",
        "matches": [{"path": f"p{i}", "line": "x" * 200} for i in range(20_000)],
    }

    frame = rt.build_frame(
        "draft",
        {"type": "input", "data": {}},
        [],
        "en",
        control_ir_results=[big_list_result],
    )

    (inline,) = frame.control_ir_results
    inline_size = len(json.dumps(inline, ensure_ascii=False))

    # C5-completeness invariant still holds
    assert inline_size <= MAX_OFFLOADED_INLINE_BYTES, (
        f"build_frame list-bulk inline_size={inline_size:,} exceeds "
        f"MAX_OFFLOADED_INLINE_BYTES={MAX_OFFLOADED_INLINE_BYTES:,}"
    )

    # Phase 1: content_hash is present
    assert "_offload_content_hash" in inline, (
        "Phase 1: build_frame offloaded inline must carry _offload_content_hash"
    )

    # Verified read-back == original
    ref_path = inline["_offload_ref"]
    content_hash = inline["_offload_content_hash"]
    # offload_dir is inside tmp_path; use tmp_path as base_dir for boundary check
    raw, found = read_offloaded(ref_path, base_dir=tmp_path, content_hash=content_hash)
    assert found is True
    stored = json.loads(raw)
    assert stored == big_list_result, (
        "Phase 1: verified read-back via build_frame content_hash must yield full original"
    )
