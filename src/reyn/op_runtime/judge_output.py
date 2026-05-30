"""judge_output op handler — LLM-based output scorer (FP-0007 Component D).

The OS:
  1. Resolves `target` (a dot-path like "artifact.data.summary") to a value
     from the current artifact dict stored in ctx.workspace.
  2. Calls an LLM with the caller-supplied `rubric` and the resolved value.
  3. Parses the score and pass/fail from the JSON response.
  4. Emits a `tool_executed` event (P6 audit) with score, passed, threshold.
  5. Returns a result dict for the caller to act on.

P3: OS does target resolution + LLM call + score parse only; rubric content
    is never interpreted by the OS.
P6: `tool_executed` event is emitted unconditionally.
P7: rubric content and `on_fail` vocabulary are kept skill-agnostic.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from reyn.schemas.models import JudgeOutputIROp

from . import register
from .context import OpContext

# ---------------------------------------------------------------------------
# Target path resolution
# ---------------------------------------------------------------------------


def _resolve_target(workspace_artifact: dict[str, Any], target: str) -> Any:
    """Resolve a dot-path `target` against `workspace_artifact`.

    Supports paths like:
      "artifact.data.summary"   → obj["artifact"]["data"]["summary"]
      "data.items"              → obj["data"]["items"]
      "text"                    → obj["text"]

    Raises KeyError when any segment is missing.
    """
    parts = target.split(".")
    current: Any = workspace_artifact
    for part in parts:
        if not isinstance(current, dict):
            raise KeyError(
                f"judge_output: cannot traverse into non-dict at segment "
                f"{part!r} of target {target!r}"
            )
        current = current[part]
    return current


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle(
    op: JudgeOutputIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute a judge_output op (FP-0007 Component D).

    Returns:
        {
            "kind": "judge_output",
            "score": float,
            "passed": bool,
            "reason": str,
            "threshold": float,
            "on_fail": str,
        }
    """
    # ── 1. Resolve target value from artifact ────────────────────────────────
    # Build resolution context: {"artifact": <latest_stored_artifact>}.
    # Skills store their working artifact via workspace.store_artifact; we read
    # the most recent entry so "artifact.data.summary" resolves against it.
    # When no artifact has been stored yet, resolution falls back to an empty
    # dict, which will raise KeyError for any non-trivial target.
    latest: dict[str, Any] = {}
    if ctx.workspace.artifacts:
        latest = ctx.workspace.artifacts[-1].get("artifact", {})
    resolution_ctx: dict[str, Any] = {"artifact": latest}

    try:
        value = _resolve_target(resolution_ctx, op.target)
    except KeyError as exc:
        ctx.events.emit(
            "tool_executed",
            op="judge_output",
            target=op.target,
            score=None,
            passed=False,
            threshold=op.threshold,
            reason=f"target resolution failed: {exc}",
        )
        return {
            "kind": "judge_output",
            "status": "error",
            "error": f"target resolution failed: {exc}",
        }

    # ── 2. Resolve model string ───────────────────────────────────────────────
    model_class = op.model or ctx.model or "standard"
    if ctx.resolver is not None:
        resolved_model = ctx.resolver.resolve(model_class).model
    else:
        resolved_model = model_class

    # ── 3. Build LLM messages ────────────────────────────────────────────────
    system_text = (
        "You are a strict evaluator. Score the following output against the rubric.\n"
        'Output ONLY a JSON object: {"score": 0.0-1.0, "reason": "..."}.\n'
        "score must be a float between 0.0 and 1.0 inclusive.\n"
        "reason must be a short explanation of the score.\n\n"
        f"Rubric:\n{op.rubric}"
    )
    user_text = (
        "Output to evaluate:\n"
        + json.dumps(value, ensure_ascii=False, indent=2)
    )
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]

    # ── 4. LLM call ──────────────────────────────────────────────────────────
    from reyn.llm.llm import proxy_kwargs

    extra = proxy_kwargs()
    # Strip provider prefix when routing via local proxy (same as call_llm).
    effective_model = (
        resolved_model.split("/", 1)[1] if extra and "/" in resolved_model else resolved_model
    )

    import litellm
    try:
        response = await litellm.acompletion(
            model=effective_model,
            messages=messages,
            response_format={"type": "json_object"},
            timeout=30.0,
            num_retries=2,
            **extra,
        )
    except Exception:
        # Fallback: retry without response_format (for providers that don't support it)
        response = await litellm.acompletion(
            model=effective_model,
            messages=messages,
            timeout=30.0,
            num_retries=2,
            **extra,
        )

    raw_content: str = (response.choices[0].message.content or "").strip()

    # ── 5. Parse JSON response ────────────────────────────────────────────────
    # Strip markdown fences if present (defensive; same pattern as llm.py)
    if raw_content.startswith("```"):
        import re
        m = re.match(r"^```(?:json)?\s*(.*?)```\s*$", raw_content, re.DOTALL)
        if m:
            raw_content = m.group(1).strip()

    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        ctx.events.emit(
            "tool_executed",
            op="judge_output",
            target=op.target,
            score=None,
            passed=False,
            threshold=op.threshold,
            reason=f"LLM response not valid JSON: {exc}",
        )
        return {
            "kind": "judge_output",
            "status": "error",
            "error": f"LLM response not valid JSON: {exc}; raw={raw_content[:200]}",
        }

    score: float = float(parsed.get("score", 0.0))
    reason: str = str(parsed.get("reason", ""))
    passed: bool = score >= op.threshold

    # ── 6. Emit P6 audit event ────────────────────────────────────────────────
    ctx.events.emit(
        "tool_executed",
        op="judge_output",
        target=op.target,
        score=score,
        passed=passed,
        threshold=op.threshold,
        reason=reason,
    )

    # ── 7. Return result ──────────────────────────────────────────────────────
    return {
        "kind": "judge_output",
        "score": score,
        "passed": passed,
        "reason": reason,
        "threshold": op.threshold,
        "on_fail": op.on_fail,
    }


register("judge_output", handle)
