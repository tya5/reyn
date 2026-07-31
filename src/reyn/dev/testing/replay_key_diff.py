"""Why a replay key missed — per-component attribution (#3473).

`MissingFixture` used to report only that a lookup failed:

    No fixture entry for model='gemini/gemini-2.5-flash-lite'.
    Prompt preview: 'Install the rag plugin.'

A replay key is a SHA-256 over four components (``model``, ``messages``,
``tools``, ``tool_choice``), and a hash that differs says nothing about which
of them moved. #3473 spent three sessions and two falsified causal stories on
exactly that silence — and the second falsification is why this module is not
scoped to the cause anyone suspected: an instrument that only lights up the
hypothesis under test can CONFIRM it but never REJECT it.

So each recorded entry carries a component fingerprint, and a miss is
reported by comparing this call's fingerprint against the recorded ones:
which of the four components differ, which message INDEX, which TOOL NAME.
The digests are one-way — this reports where the difference is, never what
the recorded value was — which is all attribution needs and keeps the
fixture free of a second copy of the payload.

The nearest recorded entry (fewest differing components, then fewest
differing parts) is the one reported: a fixture holds several rounds of one
conversation, and the useful comparison is against the round this call was
trying to be.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Digest length. Short enough to read in a failure report, long enough that
#: a collision is not the explanation anyone should reach for first.
_DIGEST_CHARS = 12


def _digest(value: Any) -> str:
    """Stable short digest of any JSON-able value."""
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001 — a fingerprint must never be the failure
        payload = repr(value)
    return hashlib.sha256(payload.encode()).hexdigest()[:_DIGEST_CHARS]


def fingerprint(
    model: str,
    messages: list[dict] | None,
    tools: list[dict] | None,
    tool_choice: str | None,
) -> dict[str, Any]:
    """Return the per-component fingerprint of a completion request.

    ``model`` and ``tool_choice`` are kept verbatim — they are short, and a
    report naming the actual model beats one naming its hash. ``messages``
    and ``tools`` are broken down one level (per index, per tool name) so the
    report can point INTO the component that differs rather than at it.
    """
    return {
        "model": model,
        "tool_choice": tool_choice or "",
        "messages": [
            f"{(m.get('role') if isinstance(m, dict) else '?')}:{_digest(m)}"
            for m in messages or []
        ],
        "tools": {
            _tool_name(t, index): _digest(t) for index, t in enumerate(tools or [])
        },
    }


def _tool_name(tool: Any, index: int) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
    return f"<tool #{index}>"


def _diff_parts(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, list[str]]:
    """Per-component differences, each as a list of human-readable part names."""
    differences: dict[str, list[str]] = {}

    if expected.get("model") != actual.get("model"):
        differences["model"] = [f"{expected.get('model')!r} -> {actual.get('model')!r}"]
    if expected.get("tool_choice") != actual.get("tool_choice"):
        differences["tool_choice"] = [
            f"{expected.get('tool_choice')!r} -> {actual.get('tool_choice')!r}"
        ]

    exp_messages = list(expected.get("messages") or [])
    act_messages = list(actual.get("messages") or [])
    message_parts: list[str] = []
    if len(exp_messages) != len(act_messages):
        message_parts.append(
            f"count {len(exp_messages)} -> {len(act_messages)}"
        )
    for index in range(min(len(exp_messages), len(act_messages))):
        if exp_messages[index] != act_messages[index]:
            message_parts.append(
                f"[{index}] {exp_messages[index]} -> {act_messages[index]}"
            )
    if message_parts:
        differences["messages"] = message_parts

    exp_tools = dict(expected.get("tools") or {})
    act_tools = dict(actual.get("tools") or {})
    tool_parts = [
        f"{name}: only in the recording" for name in exp_tools if name not in act_tools
    ]
    tool_parts += [
        f"{name}: only in this run" for name in act_tools if name not in exp_tools
    ]
    tool_parts += [
        f"{name}: schema differs ({exp_tools[name]} -> {act_tools[name]})"
        for name in exp_tools
        if name in act_tools and exp_tools[name] != act_tools[name]
    ]
    if tool_parts:
        differences["tools"] = tool_parts

    return differences


def explain_miss(
    actual: dict[str, Any], recorded: list[dict[str, Any]],
) -> str:
    """Return a report naming which key components differ, or why it cannot.

    ``recorded`` is every recorded entry's fingerprint. Entries recorded
    before #3473 have none; when that leaves nothing to compare against, the
    report says so rather than implying the components were checked and found
    equal — a report that cannot tell "no difference" from "not measured" is
    the failure this module exists to end.
    """
    components = ("model", "messages", "tools", "tool_choice")
    if not recorded:
        return (
            "Key-component attribution unavailable: no recorded entry carries a "
            "component fingerprint (fixtures recorded before #3473 do not). "
            "Re-record to make the next miss self-attributing."
        )

    scored = sorted(
        ((_diff_parts(entry, actual), entry) for entry in recorded),
        key=lambda pair: (len(pair[0]), sum(len(v) for v in pair[0].values())),
    )
    differences, _nearest = scored[0]
    if not differences:
        return (
            "Key-component attribution: all four components (model / messages / "
            "tools / tool_choice) match a recorded entry, yet the key did not. "
            "The fingerprint and the key disagree — suspect the key computation "
            "itself, not the payload."
        )

    lines = [
        "Key-component attribution (against the nearest recorded entry of "
        f"{len(recorded)}):",
    ]
    for component in components:
        parts = differences.get(component)
        if parts is None:
            lines.append(f"  {component}: MATCHES")
            continue
        lines.append(f"  {component}: DIFFERS")
        lines.extend(f"      {part}" for part in parts)
    return "\n".join(lines)
