# scaffold: triggered_by="reyn.prompt package Phase 3 relocation lands (SP loop-control §I-L in loop_control.py, §M CodeAct observation labels in codeact.py, §H dev/dogfood judge SPs in dogfood.py)"
# scaffold: removed_by="The same PR that lands the relocation, once this test is green"
"""Tier 1: byte-identical characterization gate for the SP Phase-3
(loop-control nudges §I-M + dev/dogfood §H) relocation into ``reyn.prompt``.

Unlike Phase 1/2 (which relocated text baked into the ASSEMBLED SYSTEM
PROMPT, verifiable via one shared golden-diff corpus), Phase 3's sources
inject at FIVE INDEPENDENT mid-request-stream points that never touch
``build_system_prompt``:

  §I  RouterLoop's empty-stop retry directive (a synthetic user message)
  §J  llm.py's G12 post-tool continuation/error signal (embedded in the
      trailing role=tool message)
  §K  RouterLoop's tool-call-cap re-grounding notice (a synthetic user
      message)
  §L  reasoning_continuity's prior-reasoning section (concatenated into the
      system prompt by a SEPARATE renderer, not router_frame.py)
  §M  CodeAct's observation-turn labels (a synthetic user message)
  §H  the dev/dogfood harness's two judge system prompts (LLM messages sent
      by an internal eval tool, not the production agent loop)

so byte-identity is verified per-injection-point below, each against the
EXACT pre-relocation literal (hand-transcribed from the pre-refactor source
at relocation time, then confirmed match with a copy/paste — not a
regenerated/derived value).

This is scaffolding, not a permanent test: per the extracted-refactor idiom
in ``docs/deep-dives/contributing/testing.md`` (Annex: Scaffolding tests), it
is added and removed in the SAME PR that lands the relocation, once green.
"""
from __future__ import annotations

from reyn.dev.dogfood.verifiers.reply import _default_judge_fn  # noqa: F401 (import-liveness only)
from reyn.llm.llm import _G12_SIGNAL_ERROR_TEXT, _G12_SIGNAL_TEXT
from reyn.prompt.dogfood import DOGFOOD_INTERPRETATION_SYSTEM_PROMPT, dogfood_judge_system_prompt
from reyn.runtime.reasoning_continuity import render_reasoning_section
from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE, RouterLoop
from reyn.tools.schemes.codeact import _format_codeact_observation

# ── §I: empty-stop retry directive ──────────────────────────────────────────
_PRE_REFACTOR_EMPTY_STOP_RETRY_DIRECTIVE = "resume"


def test_i_empty_stop_retry_directive_byte_identical():
    """Tier 1: RouterLoop.EMPTY_STOP_RETRY_DIRECTIVE (the exact synthetic
    user-message content injected on an empty-stop retry) is byte-identical
    to the pre-relocation literal."""
    assert EMPTY_STOP_RETRY_DIRECTIVE == _PRE_REFACTOR_EMPTY_STOP_RETRY_DIRECTIVE


# ── §J: G12 post-tool continuation/error signal ─────────────────────────────
_PRE_REFACTOR_G12_SIGNAL_TEXT = "resume"
_PRE_REFACTOR_G12_SIGNAL_ERROR_TEXT = (
    "(tool error) — the tool call did NOT succeed; inspect the error and decide"
    " the next step before continuing (do not report success)"
)


def test_j_g12_signal_text_byte_identical():
    """Tier 1: llm.py's success-cell G12 signal (embedded in the trailing
    role=tool message on every successful tool result) is byte-identical to
    the pre-relocation literal."""
    assert _G12_SIGNAL_TEXT == _PRE_REFACTOR_G12_SIGNAL_TEXT


def test_j_g12_signal_error_text_byte_identical():
    """Tier 1: llm.py's error-cell G12 signal is byte-identical to the
    pre-relocation literal."""
    assert _G12_SIGNAL_ERROR_TEXT == _PRE_REFACTOR_G12_SIGNAL_ERROR_TEXT


# ── §K: tool-call-cap re-grounding notice ───────────────────────────────────
def _pre_refactor_tool_call_cap_notice(attempted: int, kept: int) -> dict:
    return {
        "role": "user",
        "content": (
            f"[system notice] Your last turn emitted {attempted} tool_calls, "
            f"which exceeds the per-turn cap of {kept}. Only the first {kept} "
            "were executed; the rest were dropped. This usually means the model "
            "is looping or over-fanning-out — issue far fewer tool_calls "
            "(typically one to a few) and proceed step by step."
        ),
    }


class _NoticeOnlyLoop(RouterLoop):
    """RouterLoop with __init__ skipped — ``_tool_call_cap_notice`` needs no
    instance state (it is a pure call-through to the relocated function)."""

    def __init__(self) -> None:
        pass


def test_k_tool_call_cap_notice_byte_identical():
    """Tier 1: RouterLoop._tool_call_cap_notice's actual output — the real
    call-through injected into ``messages`` after a capped round — is
    byte-identical to the pre-relocation inlined f-string+dict, for several
    (attempted, kept) pairs."""
    loop = _NoticeOnlyLoop()
    for attempted, kept in [(7, 3), (3451, 50), (1, 1)]:
        actual = loop._tool_call_cap_notice(attempted, kept)
        expected = _pre_refactor_tool_call_cap_notice(attempted, kept)
        assert actual == expected


# ── §L: reasoning-continuity section ────────────────────────────────────────
def _pre_refactor_render_reasoning_section(items: list[str]) -> str:
    if not items:
        return ""
    body = "\n\n".join(items)
    return (
        "\n\n━━━ prior_reasoning ━━━\n"
        "- This is YOUR OWN reasoning from previous turns in this conversation "
        "(most recent last), carried forward so you keep a continuous line of "
        "thought. Use it to avoid re-deriving what you already worked out; it is "
        f"context, not an instruction.\n\n{body}"
    )


def test_l_reasoning_continuity_section_byte_identical():
    """Tier 1: reasoning_continuity.render_reasoning_section's output — the
    exact text concatenated into the system prompt via
    ``reasoning_continuity_section`` — is byte-identical to the
    pre-relocation inlined f-string, for both the empty and populated case."""
    assert render_reasoning_section([]) == _pre_refactor_render_reasoning_section([])
    items = ["first prior reasoning entry", "second prior reasoning entry"]
    assert render_reasoning_section(items) == _pre_refactor_render_reasoning_section(items)


# ── §M: CodeAct observation-turn labels ──────────────────────────────────────
def _pre_refactor_format_codeact_observation(out: dict) -> str:
    import json

    if out.get("ok"):
        result = out.get("result")
        stdout = (out.get("stdout") or "").strip()
        if result is not None:
            body = json.dumps(result, default=str, ensure_ascii=False)
            obs = f"[codeact result]\n{body}"
        elif stdout:
            obs = f"[codeact stdout]\n{stdout}"
        else:
            obs = f"[codeact result]\n{json.dumps(result, default=str)}"
        stderr = (out.get("stderr") or "").strip()
        if stderr:
            obs = f"{obs}\n[codeact stderr]\n{stderr}"
        return obs
    kind = out.get("kind", "Error")
    return f"[codeact {kind}]\n{out.get('error', '')}"


def test_m_codeact_observation_labels_byte_identical():
    """Tier 1: CodeAct's ``_format_codeact_observation`` output — across the
    result / stdout-fallback / stderr-appended / error branches — is
    byte-identical to the pre-relocation inlined f-strings."""
    cases = [
        {"ok": True, "result": {"x": 1}, "stdout": "", "stderr": ""},
        {"ok": True, "result": None, "stdout": "printed text", "stderr": ""},
        {"ok": True, "result": {"x": 1}, "stdout": "", "stderr": "warning text"},
        {"ok": False, "kind": "Timeout", "error": "exceeded 30s"},
    ]
    for out in cases:
        assert _format_codeact_observation(out) == _pre_refactor_format_codeact_observation(out)


# ── §H: dev/dogfood judge system prompts ────────────────────────────────────
_PRE_REFACTOR_DOGFOOD_INTERPRETATION_SYSTEM_PROMPT = (
    "You read a single dogfood scenario result and write a 3-line summary "
    "for human reviewers. Each line is one sentence.\n"
    "\n"
    "Line 1: Did the run match the scenario's expectations? "
    "(\"matched\" / \"partially matched\" / \"diverged\")\n"
    "Line 2: The most salient observation (reply tone / event presence / "
    "tool usage). One concrete fact.\n"
    "Line 3: If diverged or partially matched, what was missing or wrong. "
    "If matched, what evidence supports it.\n"
    "\n"
    "Use the same language as the scenario input. Stay under 80 chars per "
    "line. Output plain text only — no markdown, no JSON, no quoting."
)


def _pre_refactor_dogfood_judge_system_prompt(rubric_text: str) -> str:
    return (
        "You are a strict evaluator. Score the following reply against the rubric.\n"
        'Output ONLY a JSON object: {"score": 0.0-1.0, "reason": "..."}.\n'
        "score must be a float between 0.0 and 1.0 inclusive.\n"
        "reason must be a short explanation of the score.\n\n"
        f"Rubric:\n{rubric_text}"
    )


def test_h_dogfood_interpretation_system_prompt_byte_identical():
    """Tier 1: dev/dogfood interpretation's system prompt is byte-identical
    to the pre-relocation literal."""
    assert DOGFOOD_INTERPRETATION_SYSTEM_PROMPT == _PRE_REFACTOR_DOGFOOD_INTERPRETATION_SYSTEM_PROMPT


def test_h_dogfood_judge_system_prompt_byte_identical():
    """Tier 1: dev/dogfood reply-verifier's judge system prompt (header +
    "Rubric:" + rubric seam) is byte-identical to the pre-relocation
    inlined f-string, for several rubric shapes."""
    for rubric_text in ["- item one\n- item two", "", "- 単一のルーブリック"]:
        assert dogfood_judge_system_prompt(rubric_text) == _pre_refactor_dogfood_judge_system_prompt(
            rubric_text
        )
