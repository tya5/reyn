import json
import re
import litellm
from .models import ContextFrame

_SYSTEM_BASE = """\
You are an AI agent executing a phase in a structured workflow.
Respond with ONLY valid JSON — no markdown fences, no explanation, no comments.

Output format:
{
  "control": {
    "type": "transition|finish|abort",
    "decision": "continue|revise|finish|abort",
    "next_phase": "<phase_name> or null",
    "confidence": 0.0-1.0,
    "reason": {"summary": "one-sentence explanation"}
  },
  "artifact": {"type": "<schema_name>", "data": {...}},
  "control_ir": []
}

STRICT CONTROL IR REQUIRED — output will be rejected if any field is missing or invalid:

control.type rules:
- "transition": move to the next phase. next_phase MUST be a phase name (not null).
- "finish": end the workflow. next_phase MUST be null. Only when "end" appears in candidate_outputs.
- "abort": unrecoverable error. next_phase MUST be null.

control.decision rules:
- "continue": normal progression to the next phase.
- "revise": revision needed. type MUST be "transition" and next_phase MUST be "revise".
- "finish": workflow is complete. type MUST be "finish" and next_phase MUST be null.
- "abort": cannot continue. type MUST be "abort".

control.reason MUST be an object: {"summary": "..."} — NOT a plain string.
control.confidence MUST be a float in [0.0, 1.0].

Consistency requirements (violations are rejected):
- type="finish" → decision="finish", next_phase=null
- type="transition" → next_phase is non-null
- decision="revise" → type="transition", next_phase="revise"

Do not rely on automatic correction. Every field must be present and valid.

control_ir rules:
- control_ir is a list of side-effect operations to execute after this phase completes.
- Leave it empty ([]) if no file or tool operations are needed.
- Available op kinds and their schemas are listed in available_control_ops in the context.
- Use only the kinds listed there; unknown kinds are safely skipped but waste tokens.

Artifact rules:
- artifact MUST always have exactly this structure: {"type": "<schema_name>", "data": {...}}
  - "type" must be the schema_name of the chosen candidate_output.
  - "data" must contain only the fields defined in the candidate's artifact_schema.
- IMPORTANT: Do NOT put "type" inside the "data" object. "type" belongs only at artifact level.
- IMPORTANT: The "data" object must contain ONLY schema fields — no meta fields.
- All user-facing text in artifact.data MUST be written in the language specified by output_language.
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
