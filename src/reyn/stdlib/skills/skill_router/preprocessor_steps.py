"""Skill-local Python preprocessor functions for skill_router.

Runs in **pure mode** via reyn._python_harness. The previous run_op step
(`file/read` on history.jsonl) leaves the raw content at
`data.history_raw`; this function parses the JSON Lines body, then
applies the Head/Body/Tail compaction-aware slice.

`from __future__ import annotations` is intentionally not used: pure mode
disallows `__future__` imports.
"""
import json


_DEFAULT_HEAD_SIZE = 12
_DEFAULT_TAIL_SIZE = 12


def slice_chat_history(artifact: dict) -> list[dict]:
    """Parse history.jsonl content and slice for the LLM (HEAD + summary + TAIL).

    Reads `data.history_raw` (set by the previous run_op step). Optionally
    consults `data.compaction.head_size` and `data.compaction.tail_size`
    if the caller passes them; otherwise uses the K=N=12 defaults.

    Returns a list of `{role, text}` entries:

    - First K user/agent turns (HEAD region — protected anchor)
    - Plus the latest `role: "summary"` entry (rendered as markdown) if any
    - Plus the last N user/agent turns whose seq exceeds the latest
      summary's covers_through_seq (TAIL region — recent context)

    Empty list on any error condition so the route phase still sees a
    valid artifact.
    """
    data = artifact.get("data", {}) if isinstance(artifact, dict) else {}
    raw = data.get("history_raw")
    if not isinstance(raw, dict):
        return []
    content = raw.get("content") or ""
    if not content:
        return []

    compaction_cfg = data.get("compaction") or {}
    head_size = int(compaction_cfg.get("head_size") or _DEFAULT_HEAD_SIZE)
    tail_size = int(compaction_cfg.get("tail_size") or _DEFAULT_TAIL_SIZE)

    # First pass: parse all entries, assigning a synthetic seq to legacy
    # user/agent rows that don't have one (back-compat with PR3 history.jsonl
    # files written before seq existed).
    entries = []
    synthetic_seq = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        role = msg.get("role")
        if role in ("user", "agent"):
            seq = msg.get("seq") or 0
            if not seq:
                synthetic_seq += 1
                seq = synthetic_seq
            else:
                synthetic_seq = max(synthetic_seq, seq)
            entries.append({"kind": "turn", "role": role,
                            "text": msg.get("text", "") or "", "seq": seq})
        elif role == "summary":
            entries.append({
                "kind": "summary",
                "text": msg.get("text", "") or "",
                "structured": (msg.get("meta") or {}).get("structured") or {},
                "covers_through_seq": int(msg.get("covers_through_seq") or 0),
            })
        # other roles (skill_event etc.) are dropped from LLM context

    turns = [e for e in entries if e["kind"] == "turn"]
    summaries = [e for e in entries if e["kind"] == "summary"]

    head = turns[:head_size]
    latest_summary = summaries[-1] if summaries else None
    cutoff = latest_summary["covers_through_seq"] if latest_summary else head_size
    tail = [t for t in turns if t["seq"] > cutoff][-tail_size:]

    result = [{"role": t["role"], "text": t["text"]} for t in head]
    if latest_summary:
        result.append({
            "role": "summary",
            "text": _render_summary_markdown(latest_summary),
        })
    result.extend({"role": t["role"], "text": t["text"]} for t in tail)
    return result


def _render_summary_markdown(summary_entry: dict) -> str:
    """Render a structured chat_summary as markdown for LLM consumption.

    Falls back to the raw `text` field when `structured` is missing
    (back-compat with hypothetical legacy summary entries that store
    only narrative text).
    """
    s = summary_entry.get("structured") or {}
    if not s:
        return summary_entry.get("text", "") or ""

    lines = ["[Earlier conversation context]", ""]

    topic = (s.get("topic_arc") or "").strip()
    if topic:
        lines.append("**Topic / Arc**: " + topic)
        lines.append("")

    decisions = s.get("decisions") or []
    if decisions:
        lines.append("**Decisions**:")
        lines.extend("- " + d for d in decisions)
        lines.append("")

    pending = s.get("pending") or []
    if pending:
        lines.append("**Pending**:")
        lines.extend("- " + p for p in pending)
        lines.append("")

    user_facts = s.get("session_user_facts") or []
    if user_facts:
        lines.append("**Session-only user facts**:")
        lines.extend("- " + f for f in user_facts)
        lines.append("")

    artifacts = s.get("artifacts_referenced") or []
    if artifacts:
        lines.append("**Artifacts referenced**:")
        lines.extend("- " + a for a in artifacts)
        lines.append("")

    lines.append("[/Earlier conversation context]")
    return "\n".join(lines)
