"""Tier 2: #6042 — the per-message durable-content byte cap
(``Session._enforce_per_message_content_cap``, checked inside
``_append_history``'s own convergence point).

Real ``Session`` + real ``MediaStore`` (via ``multimodal_config=``) + real
disk I/O throughout — never a Mock. Every "was it actually kept small"
claim is verified by reading the ACTUAL bytes written to ``history.jsonl``
on disk (lead-coder's own explicit condition: "durable な行が一度も過大な
content を持たないことを witness する", not asserted from private state or
claimed in prose).
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.config import MultimodalConfig
from reyn.config.chat import HistoryResidentConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, ChatMessage
from tests._support.agent_session import make_session
from tests._support.events import collect_events


def _session_with_cap(
    tmp_path: Path, *, per_message_max_bytes: int = 500, with_media: bool = True,
):
    return make_session(
        agent_name="pr6042-agent",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / "ws",
        history_resident_config=HistoryResidentConfig(per_message_max_bytes=per_message_max_bytes),
        multimodal_config=MultimodalConfig() if with_media else None,
    )


def _durable_line_bytes(s) -> "list[int]":
    """The ACTUAL byte length of every line currently on disk in
    ``history.jsonl`` — read fresh from the file, never from ``s.history``
    (the resident cache) or any other in-memory state."""
    return [
        len(line.encode("utf-8"))
        for line in s.history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_oversized_content_is_spilled_and_the_durable_line_stays_small(tmp_path: Path):
    """Tier 2: the PRIMARY path — a real MediaStore is configured, so the
    oversized content is fully preserved off-row (never truncated), and
    the durable history.jsonl line itself is small. Reads the ACTUAL file
    bytes on disk, the load-bearing witness for lead-coder's own
    ordering condition."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=500)
    events = collect_events(s)
    big_content = "X" * 50_000

    s._append_history(ChatMessage(role="tool", content=big_content, ts="t1"))

    line_bytes = _durable_line_bytes(s)
    assert line_bytes, "sanity: a line was actually written"
    assert max(line_bytes) < 5_000, (
        f"the durable history.jsonl line must stay small (the oversized "
        f"content must never reach disk as-is), got max line size "
        f"{max(line_bytes)} bytes"
    )
    # The resident message was mutated too (byte-identical to what's durable).
    msg = s.history[-1]
    assert big_content not in msg.content
    assert msg.meta["oversized_content_spilled"] is True
    assert msg.meta["oversized_content_original_bytes"] == len(big_content.encode("utf-8"))

    (spill_event,) = [e for e in events if e.type == "history_oversized_content_spilled"]
    assert spill_event.data["role"] == "tool"
    assert spill_event.data["original_bytes"] == len(big_content.encode("utf-8"))
    assert spill_event.data["cap_bytes"] == 500
    spilled_path = spill_event.data["path"]

    # Round-trip: the FULL original content survives, off-row, on disk.
    _, found = s._media_store.read_tool_result(spilled_path)
    assert found is True


def test_falls_back_to_truncate_with_a_preview_when_no_media_store(tmp_path: Path):
    """Tier 2: the FALLBACK — no MediaStore configured. The content is
    truncated, but never silently: a durable marker AND a preview of the
    first bytes survive on the actual disk line (lead-coder ruling: a
    bare boolean is not enough)."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=500, with_media=False)
    events = collect_events(s)
    big_content = "Y" * 50_000

    s._append_history(ChatMessage(role="assistant", content=big_content, ts="t1"))

    line_bytes = _durable_line_bytes(s)
    assert max(line_bytes) < 5_000, "the truncated line must also stay small on disk"

    raw_line = s.history_path.read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(raw_line)
    assert record["meta"]["content_truncated"] is True
    assert record["meta"]["content_truncated_original_bytes"] == len(big_content.encode("utf-8"))
    assert record["content"], "a preview must survive, not an empty string"
    assert record["content"] == "Y" * 2000, "the preview must be the actual leading bytes"

    (truncate_event,) = [e for e in events if e.type == "history_oversized_content_truncated"]
    assert truncate_event.data["role"] == "assistant"
    assert truncate_event.data["preview_bytes"] == 2000


def test_content_already_externalized_via_content_ref_is_left_alone(tmp_path: Path):
    """Tier 2: a row already carrying CONTENT_REF_META_KEY went through the
    EXISTING write-ahead externalization (#5364 §1.1 "A") — this cap must
    not re-measure or re-spill it (that content is a legitimate small
    resident cache the write-ahead path already externalized; measuring it
    here would be a double-count of an already-solved case)."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=500)
    msg = ChatMessage(
        role="tool", content="Z" * 50_000, ts="t1",
        meta={CONTENT_REF_META_KEY: "some/ref/path.txt"},
    )

    s._append_history(msg)

    assert msg.content == "Z" * 50_000, "content already handled by the write-ahead path must be untouched"
    assert "oversized_content_spilled" not in msg.meta
    assert "content_truncated" not in msg.meta


def test_role_agnostic_assistant_content_is_also_capped(tmp_path: Path):
    """Tier 2: lead-coder ruling — assistant-authored content is IN SCOPE,
    not excluded by role (the convergence-point argument for this design
    would be undercut by a role-based branch)."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=500)
    events = collect_events(s)

    s._append_history(ChatMessage(role="assistant", content="A" * 50_000, ts="t1"))

    assert any(e.type == "history_oversized_content_spilled" for e in events)


def test_content_under_cap_is_never_touched(tmp_path: Path):
    """Tier 2: sanity — ordinary, small content is byte-identical after
    ``_append_history``, no spill/truncate machinery runs at all."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=500)
    events = collect_events(s)
    msg = ChatMessage(role="user", content="hello", ts="t1")

    s._append_history(msg)

    assert msg.content == "hello"
    assert "oversized_content_spilled" not in msg.meta
    assert "content_truncated" not in msg.meta
    assert not any(
        e.type in ("history_oversized_content_spilled", "history_oversized_content_truncated")
        for e in events
    )


def test_a_nonpositive_cap_is_inert(tmp_path: Path):
    """Tier 2: the runtime guard for a directly-constructed non-positive
    cap (the config BUILDER already falls back to the default for this —
    see test_config_mirror_coverage_1056.py's own coverage of that — this
    pins the runtime method's own independent defensive check).

    Strip: remove the ``if cap is None or cap <= 0: return`` guard --
    this goes RED (the oversized content gets spilled even with a
    disabled cap, performed during review)."""
    s = _session_with_cap(tmp_path, per_message_max_bytes=0)
    events = collect_events(s)
    msg = ChatMessage(role="tool", content="B" * 50_000, ts="t1")

    s._append_history(msg)

    assert msg.content == "B" * 50_000
    assert not any(
        e.type in ("history_oversized_content_spilled", "history_oversized_content_truncated")
        for e in events
    )
