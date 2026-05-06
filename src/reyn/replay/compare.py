"""src/reyn/replay/compare.py — diff compare across two recorded sessions.

``compare(before_trace, after_trace, scope)`` yields one ``DiffFrame`` per
aligned step (or phase / skill_run when aggregated).  Each ``DiffFrame``
carries three structured diffs:

  events_diff  — added / removed event kinds
  state_diff   — changed keys in state_snapshot
  llm_diff     — prompt text diff + response text diff

Use case (headline): "fix landing 前後で同じ session を side-by-side 比較"

    for frame in compare("/tmp/pre_fix.jsonl", "/tmp/post_fix.jsonl", scope="phase"):
        if frame.has_diff:
            print(frame.before.checkpoint, frame.events_diff)
"""
from __future__ import annotations

from typing import Any, Iterator, Literal

from reyn.replay.engine import ReplayEngine, ScopeType
from reyn.replay.model import Checkpoint, DiffFrame, StepFrame


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compare(
    before_trace: str | list[str],
    after_trace: str | list[str],
    scope: ScopeType = "step",
) -> Iterator[DiffFrame]:
    """Yield DiffFrames comparing *before_trace* against *after_trace*.

    Alignment is by sequential position (frame N of before vs frame N of
    after).  When one trace has more frames than the other, the surplus frames
    are yielded with ``before=None`` or ``after=None``.

    Args:
        before_trace: path (or list of paths) to JSONL trace recorded before
            the fix.  Pass a list of paths to feed both WAL and LLM trace
            files (the typical operational pattern when capturing real
            sessions).  See :class:`ReplayEngine` for details.
        after_trace:  path (or list of paths) to JSONL trace recorded after
            the fix.
        scope:        "step" | "phase" | "skill_run" — aggregation level

    Yields:
        DiffFrame for each aligned step position.  ``has_diff`` is True
        whenever any of the three diff dicts is non-empty.
    """
    before_engine = ReplayEngine(before_trace)
    after_engine = ReplayEngine(after_trace)

    before_frames = list(before_engine.walk(scope=scope))
    after_frames = list(after_engine.walk(scope=scope))

    length = max(len(before_frames), len(after_frames))
    for i in range(length):
        b = before_frames[i] if i < len(before_frames) else None
        a = after_frames[i] if i < len(after_frames) else None
        yield _make_diff_frame(b, a)


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _make_diff_frame(before: StepFrame | None, after: StepFrame | None) -> DiffFrame:
    return DiffFrame(
        before=before,
        after=after,
        events_diff=_diff_events(before, after),
        state_diff=_diff_state(before, after),
        llm_diff=_diff_llm(before, after),
    )


def _diff_events(
    before: StepFrame | None, after: StepFrame | None
) -> dict[str, Any]:
    """Compute added / removed event kinds between two frames.

    Returns {} when the event kind multisets are identical.
    """
    b_kinds = _event_kind_counts(before)
    a_kinds = _event_kind_counts(after)
    if b_kinds == a_kinds:
        return {}

    all_kinds = set(b_kinds) | set(a_kinds)
    changes: list[dict] = []
    for kind in sorted(all_kinds):
        bc = b_kinds.get(kind, 0)
        ac = a_kinds.get(kind, 0)
        if bc != ac:
            changes.append({"kind": kind, "before_count": bc, "after_count": ac})
    return {"changes": changes} if changes else {}


def _event_kind_counts(frame: StepFrame | None) -> dict[str, int]:
    if frame is None:
        return {}
    counts: dict[str, int] = {}
    for ev in frame.events:
        k = ev.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + 1
    return counts


def _diff_state(
    before: StepFrame | None, after: StepFrame | None
) -> dict[str, Any]:
    """Compute changed / added / removed keys in state_snapshot."""
    b_state = before.state_snapshot if before else {}
    a_state = after.state_snapshot if after else {}
    if b_state == a_state:
        return {}

    all_keys = set(b_state) | set(a_state)
    changes: list[dict] = []
    added: list[str] = []
    removed: list[str] = []
    for key in sorted(all_keys):
        in_before = key in b_state
        in_after = key in a_state
        if not in_before:
            added.append(key)
        elif not in_after:
            removed.append(key)
        elif b_state[key] != a_state[key]:
            changes.append({"key": key, "before": b_state[key], "after": a_state[key]})
    result: dict[str, Any] = {}
    if changes:
        result["changed"] = changes
    if added:
        result["added"] = added
    if removed:
        result["removed"] = removed
    return result


def _diff_llm(
    before: StepFrame | None, after: StepFrame | None
) -> dict[str, Any]:
    """Diff LLM payloads and results.

    Detects:
    - ``prompt_diff``: whether the concatenated message text changed
    - ``response_diff``: whether the response content / tool_calls changed
    - ``model_changed``: whether the model name changed between the two
    """
    result: dict[str, Any] = {}

    b_req = before.llm_payload if before else None
    a_req = after.llm_payload if after else None
    b_resp = before.llm_result if before else None
    a_resp = after.llm_result if after else None

    # Model change.
    b_model = b_req.get("model") if b_req else None
    a_model = a_req.get("model") if a_req else None
    if b_model != a_model:
        result["model_changed"] = {"before": b_model, "after": a_model}

    # Prompt diff (compare concatenated message content strings).
    b_prompt = _flatten_messages(b_req.get("messages", []) if b_req else [])
    a_prompt = _flatten_messages(a_req.get("messages", []) if a_req else [])
    if b_prompt != a_prompt:
        result["prompt_diff"] = {
            "before_len": len(b_prompt),
            "after_len": len(a_prompt),
            "changed": True,
        }

    # Response diff.
    b_content = _flatten_response(b_resp) if b_resp else ""
    a_content = _flatten_response(a_resp) if a_resp else ""
    if b_content != a_content:
        result["response_diff"] = {
            "before_len": len(b_content),
            "after_len": len(a_content),
            "changed": True,
        }

    # Tool call names diff.
    b_tc_names = _tool_call_names(b_resp) if b_resp else []
    a_tc_names = _tool_call_names(a_resp) if a_resp else []
    if b_tc_names != a_tc_names:
        result["tool_calls_diff"] = {"before": b_tc_names, "after": a_tc_names}

    return result


# ---------------------------------------------------------------------------
# LLM payload helpers
# ---------------------------------------------------------------------------

def _flatten_messages(messages: list) -> str:
    """Concatenate all text content from a messages list."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)


def _flatten_response(resp: dict) -> str:
    """Extract response text content as a single string."""
    content = resp.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _tool_call_names(resp: dict) -> list[str]:
    """Extract sorted tool call function names from a response record."""
    tcs = resp.get("tool_calls", [])
    return sorted(
        tc.get("function", {}).get("name", "") for tc in tcs if isinstance(tc, dict)
    )
