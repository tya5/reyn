"""Skill-local Python preprocessor functions for skill_router.

Runs in **pure mode** via reyn._python_harness. The previous run_op step
(`file/read` on history.jsonl) leaves the raw content at
`data.history_raw`; this function parses the JSON Lines body, filters to
user/agent turns, and returns the slice the LLM should see.

Slicing happens here so PR4 (compaction) only edits this one function.

`from __future__ import annotations` is intentionally not used: pure mode
disallows `__future__` imports.
"""
import json


_DEFAULT_MAX_TURNS = 12


def slice_chat_history(artifact: dict) -> list[dict]:
    """Parse the raw history.jsonl content placed by the run_op step.

    Returns a list of `{"role": "user" | "agent", "text": str}` entries,
    keeping the last `_DEFAULT_MAX_TURNS` after filtering. Empty list on
    any error condition (missing field, file/read returned `not_found`,
    malformed JSON) so the route phase still sees a valid artifact.
    """
    data = artifact.get("data", {}) if isinstance(artifact, dict) else {}
    raw = data.get("history_raw")
    if not isinstance(raw, dict):
        return []
    content = raw.get("content") or ""
    if not content:
        return []

    turns: list[dict] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        role = msg.get("role")
        if role not in ("user", "agent"):
            continue
        turns.append({"role": role, "text": msg.get("text", "") or ""})

    return turns[-_DEFAULT_MAX_TURNS:]
