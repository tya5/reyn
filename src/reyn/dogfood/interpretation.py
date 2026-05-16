"""Per-scenario LLM-judge interpretation (FP-0036 Component G).

Generates a 3-line natural-language summary for one scenario run:
- Did the run match the scenario's declared expectations?
- If not, what specifically diverged (reply / events / artifacts)?

The summary is consumed by ``reyn dogfood publish`` to embed a human-
readable activity block per scenario inside the Discussion thread.

LLM transport
-------------
Reuses the same LiteLLM path as ``judge_output``: ``proxy_kwargs()`` reads
``LITELLM_API_BASE`` so local proxies (= ``localhost:4000`` w/ flash-lite)
work out of the box. ``OPENAI_API_KEY`` is forwarded by litellm.

Cost
----
~1 call per scenario at flash-lite tier (~$0.0005 each). 58 scenarios is
roughly $0.03 per batch — cheap enough to keep on by default at publish
time, but the runner exposes an explicit opt-in (``--with-interpretation``)
so cost is never surprise.

Failure mode
------------
If the LLM call raises or returns non-text, the function returns a single-
line fallback string starting with ``"(interpretation unavailable: ...)"``
so the run never fails on this auxiliary surface.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.dogfood.runner import ScenarioRunResult
    from reyn.dogfood.scenarios import Scenario


DEFAULT_MODEL = "openai/gemini-2.5-flash-lite"

_SYSTEM_PROMPT = (
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


def _format_expected(scenario: "Scenario") -> str:
    blocks: list[str] = []

    if scenario.expected_reply is not None:
        er = scenario.expected_reply
        if er.kind == "judge":
            blocks.append("reply.judge_rubric: " + " / ".join(er.rubric))
        else:
            blocks.append(f"reply.{er.kind}: {er.value}")

    if scenario.expected_events is not None:
        ee = scenario.expected_events
        must_emit = [a.type for a in ee.must_emit]
        must_not = [a.type for a in ee.must_not_emit]
        if must_emit:
            blocks.append("must_emit: " + ", ".join(must_emit))
        if must_not:
            blocks.append("must_not_emit: " + ", ".join(must_not))
        if ee.sequence:
            blocks.append("sequence: " + " -> ".join(ee.sequence))

    if scenario.expected_artifacts is not None:
        ea = scenario.expected_artifacts
        names = [a.type for a in ea.assertions]
        if names:
            blocks.append("artifacts: " + ", ".join(names))

    return "\n".join(blocks) if blocks else "(no explicit expectations)"


def _format_event_types(events: list[dict], limit: int = 40) -> str:
    if not events:
        return "(no events captured)"
    types: list[str] = []
    for ev in events[:limit]:
        t = ev.get("type") or ev.get("event") or "?"
        types.append(str(t))
    suffix = f" (+{len(events) - limit} more)" if len(events) > limit else ""
    return ", ".join(types) + suffix


def build_prompt(
    scenario: "Scenario", scenario_result: "ScenarioRunResult"
) -> list[dict]:
    """Build the chat-completions messages for interpretation.

    Kept as a public helper so unit tests can assert the payload shape
    without invoking the live LLM.
    """
    input_text = scenario.input or (
        "\n".join(scenario.prompts) if scenario.prompts else "(no input)"
    )
    reply = scenario_result.reply_text or "(empty reply)"
    if len(reply) > 1500:
        reply = reply[:1500] + "...(truncated)"

    user_text = (
        f"Scenario id: {scenario.id}\n"
        f"Input:\n{input_text}\n"
        "\n"
        f"Reply:\n{reply}\n"
        "\n"
        f"Expected:\n{_format_expected(scenario)}\n"
        "\n"
        f"Event types observed: {_format_event_types(scenario_result.events)}\n"
        "\n"
        f"Verifier verdicts: reply={scenario_result.reply_outcome}, "
        f"events={scenario_result.events_outcome}, "
        f"artifacts={scenario_result.artifacts_outcome}, "
        f"overall={scenario_result.overall_outcome}\n"
    )

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


async def generate_interpretation(
    scenario: "Scenario",
    scenario_result: "ScenarioRunResult",
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
) -> str:
    """Return a 3-line natural-language summary of *scenario_result*.

    Never raises — failure modes return a single-line fallback prefixed with
    ``"(interpretation unavailable: ...)"``.
    """
    try:
        import litellm  # type: ignore[import]

        from reyn.llm.llm import proxy_kwargs
    except ImportError as exc:
        return f"(interpretation unavailable: {exc})"

    messages = build_prompt(scenario, scenario_result)
    extra = proxy_kwargs()
    effective_model = (
        model.split("/", 1)[1] if extra and "/" in model else model
    )

    try:
        response = await litellm.acompletion(
            model=effective_model,
            messages=messages,
            timeout=timeout,
            num_retries=1,
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 — auxiliary surface, must not bubble
        return f"(interpretation unavailable: {type(exc).__name__}: {exc})"

    try:
        raw = (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError) as exc:
        return f"(interpretation unavailable: malformed response: {exc})"

    if not raw:
        return "(interpretation unavailable: empty response)"

    # Defensive: collapse to at most 3 non-empty lines
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return "\n".join(lines[:3])


__all__ = [
    "DEFAULT_MODEL",
    "build_prompt",
    "generate_interpretation",
]
