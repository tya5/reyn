"""spike_judge.py — LLM-as-judge + heuristic grader for G4 spike.

Public API (imported by the driver, track B):
    judge_narration(*, final_output, narration, judge_focus) -> dict
    heuristic_grade(*, final_output, narration) -> dict
"""

from __future__ import annotations

import json
import logging
import os
import re
import warnings

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """\
You are evaluating the quality of a one-sentence narration that
summarizes what a skill produced. The skill returned this raw output:

  {final_output_json}

The narration produced was:

  {narration_text}

The user-relevant fields to weight when scoring field_extraction:

  {judge_focus_list}

Grade on three axes (0-10 each, 10 best):

  - field_extraction: Did the narration name specific user-relevant
    fields from the output (e.g., a path, a count, a status)? 0 = generic
    "skill completed" with no specifics. 10 = clear identification of
    the most useful fields, especially those listed in judge_focus.
  - accuracy: Does the narration faithfully describe the output without
    hallucinating fields that aren't there? 0 = invents content. 10 =
    every claim in the narration is supported by the raw output.
  - utility: Would a user reading this narration know what happened
    and (if applicable) what to do next? 0 = unhelpful. 10 = clear
    next step or confirmation.

Output JSON ONLY (no markdown, no fences):
  {{"field_extraction": <0-10>, "accuracy": <0-10>, "utility": <0-10>, "comment": "<one short sentence>"}}\
"""

_RETRY_ADDENDUM = "\n\nReturn JSON only. No prose."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LITELLM_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000")
_MODEL = "gemini-2.5-flash"


def _make_prompt(
    final_output: dict,
    narration: str,
    judge_focus: list[str],
    *,
    extra: str = "",
) -> str:
    return _JUDGE_PROMPT_TEMPLATE.format(
        final_output_json=json.dumps(final_output, ensure_ascii=False, indent=2),
        narration_text=narration,
        judge_focus_list=", ".join(judge_focus) if judge_focus else "(none specified)",
    ) + extra


def _call_llm(prompt: str) -> str:
    """Send one request to the LiteLLM proxy; return raw content string."""
    resp = requests.post(
        f"{_LITELLM_BASE}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'dummy')}",
        },
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": prompt}],
            # Thinking off — match strong-experimental condition.
            # Sent at the top level so the LiteLLM proxy passes it through;
            # wrapping in extra_body causes a 400 on some proxy versions.
            "thinkingConfig": {"thinkingBudget": 0},
            "temperature": 0,  # deterministic grading
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_scores(content: str) -> dict | None:
    """Try to parse judge JSON from LLM content. Returns None on failure."""
    # Fast path — content is pure JSON
    stripped = content.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Fallback — extract first {...} block
    match = re.search(r"\{[^{}]+\}", stripped, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _clamp_scores(parsed: dict) -> dict:
    """Clamp field_extraction / accuracy / utility to [0, 10] ints."""
    for key in ("field_extraction", "accuracy", "utility"):
        raw = parsed.get(key)
        if not isinstance(raw, (int, float)):
            warnings.warn(
                f"judge: '{key}' is not numeric ({raw!r}), defaulting to 0",
                stacklevel=2,
            )
            parsed[key] = 0
            continue
        clamped = max(0, min(10, int(raw)))
        if clamped != raw:
            warnings.warn(
                f"judge: '{key}' value {raw!r} out of [0, 10], clamped to {clamped}",
                stacklevel=2,
            )
        parsed[key] = clamped
    return parsed


# ---------------------------------------------------------------------------
# Public: LLM judge
# ---------------------------------------------------------------------------


def judge_narration(
    *,
    final_output: dict,
    narration: str,
    judge_focus: list[str],
) -> dict:
    """Score narration quality via gemini-2.5-flash judge call.

    Returns: {"field_extraction": int, "accuracy": int, "utility": int,
              "comment": str, "raw_response": str, "judge_calls": int}
    """
    prompt = _make_prompt(final_output, narration, judge_focus)

    # --- Attempt 1 ---
    content = _call_llm(prompt)
    parsed = _parse_scores(content)

    if parsed is not None:
        parsed = _clamp_scores(parsed)
        parsed.setdefault("comment", "")
        parsed["raw_response"] = content
        parsed["judge_calls"] = 1
        return parsed

    # --- Attempt 2 (retry with stricter addendum) ---
    logger.warning("judge_narration: first parse failed, retrying with stricter prompt")
    retry_prompt = _make_prompt(
        final_output, narration, judge_focus, extra=_RETRY_ADDENDUM
    )
    content2 = _call_llm(retry_prompt)
    parsed2 = _parse_scores(content2)

    if parsed2 is not None:
        parsed2 = _clamp_scores(parsed2)
        parsed2.setdefault("comment", "")
        parsed2["raw_response"] = content2
        parsed2["judge_calls"] = 2
        return parsed2

    # Both attempts failed
    return {
        "field_extraction": -1,
        "accuracy": -1,
        "utility": -1,
        "comment": "judge parse failed: " + content2[:120],
        "raw_response": content2,
        "judge_calls": 2,
    }


# ---------------------------------------------------------------------------
# Public: heuristic grader
# ---------------------------------------------------------------------------


def _flatten_strings(obj: object):
    """Yield candidate substrings from final_output."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and len(k) >= 3:
                yield k
            yield from _flatten_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten_strings(item)
    elif isinstance(obj, str) and len(obj) >= 3:
        yield obj


def heuristic_grade(
    *,
    final_output: dict,
    narration: str,
) -> dict:
    """Structural narration check, no LLM call.

    Returns: {"auto_score": 0|1, "signals": list[str]}
    """
    signals: list[str] = []

    # Signal 1: non-empty narration
    if not narration or not narration.strip():
        signals.append("empty_narration")

    # Signal 2: length sanity
    length = len(narration)
    if length < 30:
        signals.append(f"too_short_{length}")
    elif length > 500:
        signals.append(f"too_long_{length}")

    # Signal 3: at least one final_output field name or value appears in narration
    candidates = list(_flatten_strings(final_output))
    narration_lower = narration.lower()
    matched = [c for c in candidates if c.lower() in narration_lower]
    if not matched:
        signals.append("no_field_or_value_match")

    # Signal 4: hallucinated JSON dump (= raw json fences in narration)
    if "```json" in narration or '"data":' in narration:
        signals.append("raw_json_dump")

    auto_score = 0 if signals else 1
    return {"auto_score": auto_score, "signals": signals}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("heuristic_grade smoke tests")
    print("=" * 60)

    _example_output = {
        "status": "success",
        "output_path": "/tmp/report.pdf",
        "row_count": 42,
        "errors": [],
    }

    # Case 1: good narration
    g1 = heuristic_grade(
        final_output=_example_output,
        narration="Skill completed successfully: report.pdf written with 42 rows, no errors.",
    )
    print(f"[GOOD]    auto_score={g1['auto_score']}  signals={g1['signals']}")

    # Case 2: empty narration
    g2 = heuristic_grade(
        final_output=_example_output,
        narration="",
    )
    print(f"[EMPTY]   auto_score={g2['auto_score']}  signals={g2['signals']}")

    # Case 3: generic completion — no field mention
    g3 = heuristic_grade(
        final_output=_example_output,
        narration="The skill ran and finished without any problems whatsoever.",
    )
    print(f"[GENERIC] auto_score={g3['auto_score']}  signals={g3['signals']}")

    print()
    print("=" * 60)
    print("judge_narration live proxy test")
    print("=" * 60)

    _judge_output = {
        "status": "success",
        "output_path": "/tmp/report.pdf",
        "row_count": 42,
    }
    _judge_narration = (
        "The skill succeeded: report.pdf was written with 42 rows at /tmp/report.pdf."
    )
    _judge_focus = ["output_path", "row_count"]

    try:
        result = judge_narration(
            final_output=_judge_output,
            narration=_judge_narration,
            judge_focus=_judge_focus,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except requests.exceptions.ConnectionError as exc:
        print(
            f"[WARN] Proxy unreachable at {_LITELLM_BASE} — skipping live judge test.\n"
            f"       Start the LiteLLM proxy and re-run to verify judge_narration.\n"
            f"       Error: {exc}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] judge_narration failed: {exc}", file=sys.stderr)
        sys.exit(1)
