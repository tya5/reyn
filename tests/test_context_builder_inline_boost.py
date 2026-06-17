"""Tier 2: context_builder inline-boost range invariants (FP-0008 PR-K).

The inline-boost fix adds a second threshold MAX_INLINE_BOOST = 65_536.
Artifacts in the range (ARTIFACT_REF_THRESHOLD, MAX_INLINE_BOOST] are
inlined in the prompt instead of being replaced by an artifact_ref pointer.
Artifacts above MAX_INLINE_BOOST still get artifact_ref'd to prevent prompt
explosion.

These tests pin all three regions of the decision, the observability event
for the inline-boost path, and the P1/P5 schema-integrity invariant.
"""
from __future__ import annotations

import json

from reyn.core.context_builder import (
    ARTIFACT_REF_THRESHOLD,
    MAX_INLINE_BOOST,
    maybe_ref_artifact,
)
from reyn.core.events.events import EventLog

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_artifact(target_size: int) -> dict:
    """Return an artifact whose JSON serialisation is approximately *target_size* chars."""
    payload = "x" * max(0, target_size - 20)  # leave room for JSON wrapping
    return {"type": "generic_input", "data": payload}


def _serialized_size(artifact: dict) -> int:
    return len(json.dumps(artifact, ensure_ascii=False))


# ── 1. small artifact inlines normally (existing-behaviour pin) ───────────────


def test_small_artifact_inlined_normally() -> None:
    """Tier 2: artifact below ARTIFACT_REF_THRESHOLD inlines (= existing path, no change)."""
    artifact = _make_artifact(ARTIFACT_REF_THRESHOLD - 500)
    assert _serialized_size(artifact) < ARTIFACT_REF_THRESHOLD

    result = maybe_ref_artifact(artifact, artifact_path="workspace/run/artifact.jsonl")

    # Inlined: result IS the original artifact, not an artifact_ref dict.
    assert result is artifact


# ── 2. inline-boost range inlines (NEW path) ──────────────────────────────────


def test_artifact_in_inline_boost_range_inlines() -> None:
    """Tier 2: artifact between ARTIFACT_REF_THRESHOLD and MAX_INLINE_BOOST inlines (inline-boost)."""
    # Build an artifact just over the lower threshold but well under MAX_INLINE_BOOST.
    artifact = _make_artifact(ARTIFACT_REF_THRESHOLD + 500)
    size = _serialized_size(artifact)
    assert ARTIFACT_REF_THRESHOLD < size <= MAX_INLINE_BOOST

    result = maybe_ref_artifact(artifact, artifact_path="workspace/run/artifact.jsonl")

    # Inline-boost: result is still the original artifact, NOT an artifact_ref.
    assert result is artifact
    assert result.get("type") != "artifact_ref"


# ── 3. above MAX_INLINE_BOOST artifact_refs (prompt-explosion prevention) ─────


def test_artifact_above_max_inline_boost_artifact_refs() -> None:
    """Tier 2: artifact above MAX_INLINE_BOOST is artifact_ref'd (= prevents prompt explosion)."""
    artifact = _make_artifact(MAX_INLINE_BOOST + 1000)
    assert _serialized_size(artifact) > MAX_INLINE_BOOST

    result = maybe_ref_artifact(
        artifact,
        artifact_path="workspace/run/large.jsonl",
    )

    # Should be an artifact_ref, not the original artifact.
    assert result["type"] == "artifact_ref"
    assert result["ref_path"] == "workspace/run/large.jsonl"
    assert "size_bytes" in result


# ── 4. inline-boost emits observability event ─────────────────────────────────


def test_inline_boost_emits_observability_event() -> None:
    """Tier 2: inline-boost path emits artifact_inline_boost event with size_chars + phase."""
    events = EventLog()
    artifact = _make_artifact(ARTIFACT_REF_THRESHOLD + 500)
    size = _serialized_size(artifact)
    assert ARTIFACT_REF_THRESHOLD < size <= MAX_INLINE_BOOST

    maybe_ref_artifact(
        artifact,
        artifact_path="workspace/run/mid.jsonl",
        events=events,
        phase="test_phase",
    )

    emitted = events.all()
    boost_events = [e for e in emitted if e.type == "artifact_inline_boost"]
    (only,) = boost_events  # exactly one boost event
    assert only.data["phase"] == "test_phase"
    assert only.data["size_chars"] == size
    assert only.data["artifact_path"] == "workspace/run/mid.jsonl"


# ── 5. small artifact emits no inline-boost event (control) ───────────────────


def test_small_artifact_emits_no_inline_boost_event() -> None:
    """Tier 2: small artifact (below threshold) does not emit artifact_inline_boost event."""
    events = EventLog()
    artifact = _make_artifact(ARTIFACT_REF_THRESHOLD - 500)
    assert _serialized_size(artifact) < ARTIFACT_REF_THRESHOLD

    maybe_ref_artifact(
        artifact,
        artifact_path="workspace/run/small.jsonl",
        events=events,
        phase="test_phase",
    )

    boost_events = [e for e in events.all() if e.type == "artifact_inline_boost"]
    assert boost_events == []


# ── 6. inline-boost preserves artifact schema integrity (P1 / P5) ─────────────


def test_inline_boost_preserves_artifact_schema_integrity() -> None:
    """Tier 2: inlined mid-size artifact retains its original type and data fields intact.

    P5 (SSOT): inlining is a prompt-shape decision; the artifact data itself
    must be identical to what was written to the workspace. P1: schema integrity
    means the artifact's 'type' field is unchanged (= still valid against
    the phase's input_schema at the call site).
    """
    artifact = {
        "type": "swe_bench_input",
        "instance_id": "django__django-12345",
        "problem_statement": "A" * (ARTIFACT_REF_THRESHOLD + 1000),
        "extra": {"version": 2},
    }
    size = _serialized_size(artifact)
    assert ARTIFACT_REF_THRESHOLD < size <= MAX_INLINE_BOOST

    result = maybe_ref_artifact(artifact, artifact_path="workspace/run/swe.jsonl")

    # The returned dict is the original artifact, fields intact.
    assert result["type"] == artifact["type"]
    assert result["instance_id"] == artifact["instance_id"]
    assert result["problem_statement"] == artifact["problem_statement"]
    assert result["extra"] == artifact["extra"]
    # Confirm it is NOT an artifact_ref
    assert result.get("ref_path") is None
