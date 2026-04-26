import json
import re
import litellm
from .models import ContextFrame

_SYSTEM_BASE = """\
You are an AI agent executing a phase in a structured workflow.
Respond with ONLY valid JSON — no markdown fences, no explanation, no comments.

You have TWO output formats depending on whether you need to perform operations first.

━━━ FORMAT A: act turn (perform operations, then be re-called with results) ━━━
Use this when you need to read a file, ask the user, or invoke a tool BEFORE deciding.
{
  "type": "act",
  "ops": [<op>, ...]
}
The OS will execute the ops and call you again with results in control_ir_results.
Leave ops non-empty — an act turn with empty ops is useless.

━━━ FORMAT B: decide turn (routing decision + artifact) ━━━
Use this when you have all the information needed to complete the phase.
{
  "type": "decide",
  "control": {
    "type": "transition|finish|abort",
    "decision": "continue|finish|abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0-1.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {"type": "<schema_name>", "data": {...}},
  "ops": []
}
ops in a decide turn: only write ops are useful here (reads would require another act turn).
Leave ops empty ([]) if no writes are needed.

━━━ DECIDE TURN RULES ━━━
control.type:
- "transition": move to next_phase (must be non-null, must be in candidate_outputs).
- "finish": end the workflow. next_phase MUST be null. Only when "end" is in candidate_outputs.
- "abort": unrecoverable error. next_phase MUST be null.

control.decision:
- "continue": normal transition to any next phase.
- "finish": workflow complete. type MUST be "finish", next_phase MUST be null.
- "abort": cannot continue. type MUST be "abort".

Consistency requirements (violations cause rejection):
- type="finish" → decision="finish", next_phase=null
- type="transition" → next_phase is non-null
- type="abort"    → decision="abort", next_phase=null

control.reason MUST be {"summary": "..."} — NOT a plain string.
control.confidence MUST be a float in [0.0, 1.0].

Artifact rules:
- artifact MUST always have: {"type": "<schema_name>", "data": {...}}
  - "type" is the schema_name of the chosen candidate_output.
  - "data" contains ONLY fields defined in the candidate's artifact_schema.
- Do NOT put "type" inside the "data" object.
- All user-facing text in artifact.data MUST be in the language specified by output_language.

━━━ ops rules (both turns) ━━━
- Available op kinds and schemas are listed in available_control_ops in the context.
- Use only listed kinds; unknown kinds are skipped.

━━━ control_ir_results ━━━
- When non-empty, this is a re-call after your previous act turn.
- Each entry is the result of one op you previously requested. Common shapes:
    file read:  {"kind": "file", "op": "read", "path": "...", "content": "...", "status": "ok"}
    ask_user:   {"kind": "ask_user", "question": "...", "answer": "...", "status": "ok"}
- Use these results together with input_artifact to complete the phase goal.
- Once you have what you need, output a decide turn to make your routing decision.
"""


def _system_prompt(output_language: str) -> str:
    lang_map = {"ja": "Japanese", "en": "English", "zh": "Chinese"}
    lang_label = lang_map.get(output_language, output_language)
    return _SYSTEM_BASE + f"\noutput_language is '{output_language}' — write all content in {lang_label}.\n"


def _extract_json(text: str) -> str:
    """
    Strip markdown code fences wrapping the entire response.
    Only matches fences that surround the whole text, not embedded ones
    (e.g. code blocks inside article body).
    Falls back to the original text if extraction yields an empty string.
    """
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)```\s*$", stripped, re.DOTALL)
    if match:
        inner = match.group(1).strip()
        if inner:
            return inner
    return stripped


def _repair_json(text: str) -> str:
    """Remove trailing commas — the most common LLM JSON mistake."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def call_llm(
    model: str,
    frame: ContextFrame,
    prior_attempts: list[dict[str, str]] | None = None,
) -> dict:
    """
    Call the LLM and return a parsed JSON dict.

    prior_attempts: list of {"raw": str, "error": str} from previous phase retries.
      Each entry is appended as an assistant/user turn so the LLM sees what was wrong.
    """
    system = _system_prompt(frame.output_language)
    user_content = json.dumps(frame.model_dump(), indent=2, ensure_ascii=False)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    # Inject semantic-rejection feedback from outer phase retry loop
    if prior_attempts:
        for pa in prior_attempts:
            messages.append({"role": "assistant", "content": pa["raw"]})
            messages.append({
                "role": "user",
                "content": (
                    f"Your output was rejected: {pa['error']}\n"
                    "Fix the issue and output a valid JSON response."
                ),
            })

    last_exc: Exception | None = None
    last_raw: str = ""
    attempt0_raw: str = ""

    for attempt in range(2):  # attempt 0 = first call, attempt 1 = JSON-repair retry
        if attempt == 1:
            # Only retry if we actually got a non-empty (but unparseable) response
            if not attempt0_raw:
                break
            messages = messages + [
                {"role": "assistant", "content": attempt0_raw},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON. "
                        "Output ONLY a single valid JSON object — no explanation, no markdown."
                    ),
                },
            ]

        # response_format may not be supported by all models; pass it only when available
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = litellm.completion(model=model, messages=messages)

        last_raw = response.choices[0].message.content or ""
        if attempt == 0:
            attempt0_raw = last_raw

        if not last_raw:
            last_exc = ValueError("LLM returned empty response")
            continue

        text = _extract_json(last_raw)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        try:
            return json.loads(_repair_json(text))
        except json.JSONDecodeError as exc:
            last_exc = exc
            continue  # retry

    raise ValueError(
        f"LLM returned invalid JSON after repair and retry.\n"
        f"Error: {last_exc}\n"
        f"Raw response (first 800 chars):\n{last_raw[:800]}"
    ) from last_exc
