"""Tier 2: dispatcher invariant — `_compute_llm_args_hash` is memo-stable.

R-D2 (LLM memoization). The function hashes LLM call args for use as a
memoization key on resume. The single known non-deterministic field is
``current_datetime`` (set by ``ContextFrame`` via ``datetime.now()`` each
call), which would cause every resume to mismatch the args_hash recorded
at original-run time and silently miss the memo. This test pins that the
hash is stable across datetime variation but sensitive to every other
input.
"""
from __future__ import annotations

from reyn.dispatch.dispatcher import _compute_llm_args_hash


def test_current_datetime_difference_yields_same_hash():
    """Tier 2: current_datetime in frame is stripped before hashing."""
    frame_a = {
        "phase": "draft",
        "current_datetime": "2026-05-03T10:00:00+00:00",
        "artifact": {"x": 1},
    }
    frame_b = {
        "phase": "draft",
        "current_datetime": "2026-05-03T11:00:00+00:00",
        "artifact": {"x": 1},
    }
    assert _compute_llm_args_hash(model="m", frame=frame_a) == \
        _compute_llm_args_hash(model="m", frame=frame_b)


def test_different_model_yields_different_hash():
    """Tier 2: model name participates in hash."""
    frame = {"phase": "draft"}
    assert _compute_llm_args_hash(model="gpt-4", frame=frame) != \
        _compute_llm_args_hash(model="claude-3", frame=frame)


def test_different_frame_content_yields_different_hash():
    """Tier 2: frame content (other than datetime) matters."""
    frame_a = {"phase": "draft", "artifact": {"x": 1}}
    frame_b = {"phase": "draft", "artifact": {"x": 2}}
    assert _compute_llm_args_hash(model="m", frame=frame_a) != \
        _compute_llm_args_hash(model="m", frame=frame_b)


def test_prior_attempts_affect_hash():
    """Tier 2: retry chain produces different hash (drift / retry detection)."""
    frame = {"phase": "draft"}
    h_clean = _compute_llm_args_hash(model="m", frame=frame, prior_attempts=None)
    h_retry = _compute_llm_args_hash(
        model="m", frame=frame,
        prior_attempts=[{"raw": "{}", "error": "bad json"}],
    )
    assert h_clean != h_retry


def test_rollback_context_affects_hash():
    """Tier 2: rollback context participates in hash."""
    frame = {"phase": "draft"}
    h_no_rb = _compute_llm_args_hash(model="m", frame=frame, rollback_context=None)
    h_with_rb = _compute_llm_args_hash(
        model="m", frame=frame,
        rollback_context={
            "rejected_artifact": {"a": 1},
            "reason": "no good",
            "rollback_from": "review",
        },
    )
    assert h_no_rb != h_with_rb


def test_system_inputs_affect_hash():
    """Tier 2: skill_name / phase_role / project_context / agent_role affect hash.

    These all flow into ``_system_prompt`` in ``call_llm`` and so are part of
    the LLM input. Memo must distinguish them.
    """
    frame = {"phase": "draft"}
    h_a = _compute_llm_args_hash(
        model="m", frame=frame, system_inputs={"skill_name": "a", "phase_role": "x"},
    )
    h_b = _compute_llm_args_hash(
        model="m", frame=frame, system_inputs={"skill_name": "b", "phase_role": "x"},
    )
    h_c = _compute_llm_args_hash(
        model="m", frame=frame, system_inputs={"skill_name": "a", "phase_role": "y"},
    )
    assert h_a != h_b
    assert h_a != h_c


def test_hash_is_deterministic():
    """Tier 2: same inputs always produce same hash (across separate calls)."""
    frame = {"phase": "draft", "artifact": {"x": 1}}
    h1 = _compute_llm_args_hash(model="m", frame=frame)
    h2 = _compute_llm_args_hash(model="m", frame=frame)
    assert h1 == h2


def test_hash_format_matches_op_args_hash():
    """Tier 2: hex-string output (matches dispatcher's `_compute_args_hash` format).

    Pinned so the WAL ``args_hash`` field is a non-empty lowercase hex string
    whether the step is op-kind or llm-kind. Audit tooling can rely on the format.
    """
    frame = {"phase": "draft"}
    h = _compute_llm_args_hash(model="m", frame=frame)
    assert h  # non-empty
    assert all(c in "0123456789abcdef" for c in h)


def test_unhashable_frame_falls_back_safely():
    """Tier 2: non-JSON-serializable frame entries fall back to repr (no crash)."""
    # Functions are not JSON-serializable; should not raise.
    frame = {"phase": "draft", "callback": lambda x: x}
    h = _compute_llm_args_hash(model="m", frame=frame)
    assert isinstance(h, str)
    assert h  # non-empty hex string produced without raising
