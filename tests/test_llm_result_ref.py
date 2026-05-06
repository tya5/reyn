"""Tier 2: OS invariant — LLM result workspace ref handling (R-D10).

Pin the contract for ``write_if_large`` / ``resolve`` /
``cleanup_for_run``: small payloads stay inline, large payloads go to
disk with a ``{"_ref": ...}`` WAL placeholder, missing refs fall
through, and the directory is removed on skill completion.

Reference: PR-llm-payload-size (R-D10) in the active plan.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.skill import llm_result_ref

_RUN_ID = "run_llmref"
_ARGS_HASH = "abcdef0123456789"


# ---------------------------------------------------------------------------
# write_if_large — size threshold behavior
# ---------------------------------------------------------------------------


def test_small_payload_stays_inline(tmp_path: Path):
    """Tier 2: payload <= threshold returns unchanged, no file written."""
    result = {"control": {"type": "finish"}, "small": "data"}
    out = llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=result,
        threshold=32_768,
    )
    assert out is result, (
        "small payload should be returned unchanged (no copy)"
    )
    # No directory was created
    d = llm_result_ref.llm_results_dir(tmp_path, _RUN_ID)
    assert not d.exists()


def test_large_payload_writes_ref_file(tmp_path: Path):
    """Tier 2: payload > threshold writes to disk and returns {"_ref": ...}."""
    big_string = "x" * 100_000
    result = {"control": {"type": "finish"}, "data": big_string}
    out = llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=result,
        threshold=32_768,
    )
    # Returned a ref placeholder
    assert isinstance(out, dict)
    assert list(out.keys()) == ["_ref"]
    assert out["_ref"] == f"{_ARGS_HASH}.json"
    # File exists with the JSON content
    p = llm_result_ref.llm_results_dir(tmp_path, _RUN_ID) / out["_ref"]
    assert p.is_file()
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded == result


def test_threshold_boundary_inline(tmp_path: Path):
    """Tier 2: payload exactly == threshold stays inline (boundary check)."""
    # Build a payload that serializes to exactly 100 bytes.
    result = {"x": "a" * 90}  # ~100 bytes serialized
    serialized_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    out = llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=result,
        threshold=serialized_size,  # threshold == size → inline
    )
    assert out is result, (
        f"size {serialized_size} == threshold should stay inline"
    )


def test_threshold_just_above_writes_ref(tmp_path: Path):
    """Tier 2: payload one byte above threshold triggers ref write."""
    result = {"x": "a" * 90}
    serialized_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    out = llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=result,
        threshold=serialized_size - 1,  # threshold < size → ref
    )
    assert isinstance(out, dict) and "_ref" in out


# ---------------------------------------------------------------------------
# resolve — read path
# ---------------------------------------------------------------------------


def test_resolve_returns_value_unchanged_when_not_ref(tmp_path: Path):
    """Tier 2: non-ref values pass through resolve untouched."""
    inline = {"control": {"type": "finish"}, "data": "small"}
    assert llm_result_ref.resolve(
        agent_state_dir=tmp_path, run_id=_RUN_ID, value=inline,
    ) is inline


def test_resolve_loads_from_ref_file(tmp_path: Path):
    """Tier 2: ref placeholder is transparently resolved to the stored result."""
    big_string = "x" * 100_000
    result = {"control": {"type": "finish"}, "data": big_string}
    placeholder = llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=result,
        threshold=32_768,
    )
    loaded = llm_result_ref.resolve(
        agent_state_dir=tmp_path, run_id=_RUN_ID, value=placeholder,
    )
    assert loaded == result


def test_resolve_returns_none_when_ref_file_missing(tmp_path: Path):
    """Tier 2: dangling ref → None (caller falls through to fresh call).

    File system corruption / partial cleanup must not crash resume.
    Returning None signals "memo unavailable" so the runtime can
    re-execute as if there had been no committed step.
    """
    placeholder = {"_ref": "nonexistent.json"}
    out = llm_result_ref.resolve(
        agent_state_dir=tmp_path, run_id=_RUN_ID, value=placeholder,
    )
    assert out is None


def test_resolve_returns_none_for_malformed_ref_value(tmp_path: Path):
    """Tier 2: malformed ref placeholder (non-string) → None."""
    out = llm_result_ref.resolve(
        agent_state_dir=tmp_path, run_id=_RUN_ID,
        value={"_ref": 123},  # int instead of str
    )
    assert out is None


# ---------------------------------------------------------------------------
# cleanup_for_run — lifecycle
# ---------------------------------------------------------------------------


def test_cleanup_removes_per_run_directory(tmp_path: Path):
    """Tier 2: cleanup_for_run removes the entire <run_id>_llm_results dir."""
    big = {"data": "x" * 100_000}
    llm_result_ref.write_if_large(
        agent_state_dir=tmp_path,
        run_id=_RUN_ID,
        args_hash=_ARGS_HASH,
        result=big,
        threshold=32_768,
    )
    d = llm_result_ref.llm_results_dir(tmp_path, _RUN_ID)
    assert d.is_dir()
    llm_result_ref.cleanup_for_run(tmp_path, _RUN_ID)
    assert not d.exists()


def test_cleanup_is_noop_when_dir_missing(tmp_path: Path):
    """Tier 2: cleanup is safe to call when dir was never created."""
    # No file written; just call cleanup
    llm_result_ref.cleanup_for_run(tmp_path, "never_existed")
    # No exception; nothing to verify beyond not crashing


def test_cleanup_only_removes_target_run_dir(tmp_path: Path):
    """Tier 2: cleanup of run A leaves run B's results intact."""
    big = {"data": "x" * 100_000}
    llm_result_ref.write_if_large(
        agent_state_dir=tmp_path, run_id="run_A",
        args_hash="hashA", result=big, threshold=32_768,
    )
    llm_result_ref.write_if_large(
        agent_state_dir=tmp_path, run_id="run_B",
        args_hash="hashB", result=big, threshold=32_768,
    )
    a_dir = llm_result_ref.llm_results_dir(tmp_path, "run_A")
    b_dir = llm_result_ref.llm_results_dir(tmp_path, "run_B")
    assert a_dir.is_dir() and b_dir.is_dir()
    llm_result_ref.cleanup_for_run(tmp_path, "run_A")
    assert not a_dir.exists()
    assert b_dir.is_dir(), "run_B's results must survive run_A's cleanup"
