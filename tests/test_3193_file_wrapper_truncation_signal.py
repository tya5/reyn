"""Tier 2: Session._file_* wrapper truncation-signal forwarding (#3193).

#3193: `Session._file_read` (and its siblings) whitelisted `status == "ok"`
and collapsed every other status — most damagingly `"truncated"` — into
`{"error": "read failed"}`, discarding content that had actually been read
successfully. The fix routes every wrapper through the single
`classify_op_status` classifier and forwards the #3193 signal fields
(`truncated`, `note`, ...) untouched instead of dropping them.

Real files, real Session, real MemoryService — no mocks (testing policy).
A genuinely large file (2MB) is used to force a REAL op_runtime truncation
(the window-derived inline cap floors at 8KB and, even for the largest
plausible model window, tops out far below 2MB — see
src/reyn/core/context_builder.py's `control_ir_inline_cap`), not a
hand-constructed `{"status": "truncated"}` fixture.

Both halves of the invariant are pinned together per witness (per the
issue's explicit requirement): content is present AND the truncation is
signalled. Pinning only one half would not catch a regression that
resurrects the "drop the signal, keep the content" bug in a new form.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.agent_session import make_session

_LINE = "x" * 40 + "\n"
_LINE_COUNT = 100_000  # 100_000 * 41 bytes ~= 4.1 MB — far past any plausible inline cap.


@pytest.mark.asyncio
async def test_file_read_forwards_content_and_truncation_signal_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a real truncated read returns BOTH usable content AND the
    truncation signal — neither alone. Pre-#3193 this returned
    {"error": "read failed"} instead, discarding the content it had
    actually read."""
    monkeypatch.chdir(tmp_path)
    big_file = tmp_path / "big.txt"
    big_file.write_text(_LINE * _LINE_COUNT, encoding="utf-8")

    session = make_session(agent_name="test-agent-3193-read")
    result = await session._file_read("big.txt")

    assert "error" not in result, f"unexpected error: {result.get('error')}"
    # Half 1: content survived — it is a real, non-empty prefix of the file.
    assert result["content"], "content must not be dropped on a truncated read"
    assert result["content"] == _LINE * (len(result["content"]) // len(_LINE))
    assert len(result["content"]) < len(_LINE) * _LINE_COUNT, (
        "expected content to actually be truncated (shorter than the full file) "
        "— if this fails, the inline cap did not trigger for this file size"
    )
    # Half 2: the truncation signal is present, not silently dropped.
    assert result.get("truncated") is True
    assert "note" in result and "truncated" in result["note"]
    assert "next_offset" in result


@pytest.mark.asyncio
async def test_file_read_plain_success_has_no_truncation_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: regression guard — a small, un-truncated read must NOT carry
    a spurious truncation signal (the two states stay distinguishable)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.txt").write_text("hello world\n", encoding="utf-8")

    session = make_session(agent_name="test-agent-3193-read-small")
    result = await session._file_read("small.txt")

    assert result == {"path": "small.txt", "content": "hello world\n"}


@pytest.mark.asyncio
async def test_file_read_not_found_still_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: regression guard — a genuinely missing file still reports the
    specific not-found error, unaffected by the classifier refactor."""
    monkeypatch.chdir(tmp_path)
    session = make_session(agent_name="test-agent-3193-read-missing")
    result = await session._file_read("does-not-exist.txt")
    assert result == {"error": "file not found: does-not-exist.txt"}


@pytest.mark.asyncio
async def test_memory_read_body_forwards_truncation_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: MemoryService.read_body is a live consumer downstream of
    Session._file_read (session.py wires `file_read=self._file_read`
    directly — #3193's ticket calls this out as the second live consumer
    to verify, not just the wrapper's own return dict). Pre-fix, read_body
    hand-picked only `content` off the `_file_read` result, so even after
    `_file_read` started forwarding `truncated`/`note`, read_body silently
    dropped them again at its own layer — the same family of bug at a
    second altitude."""
    monkeypatch.chdir(tmp_path)
    session = make_session(agent_name="test-agent-3193-memory")

    # Write directly at the path MemoryService.read_body will read from,
    # bypassing `remember()` (which would re-wrap frontmatter around
    # content, complicating the truncation-size math) — the read path
    # under test is read_body -> Session._file_read, not remember().
    body_path = Path(session._memory.memory_path("agent", "big-note"))
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(_LINE * _LINE_COUNT, encoding="utf-8")

    read = await session._memory.read_body(layer="agent", slug="big-note")

    assert "error" not in read, f"unexpected error: {read.get('error')}"
    assert read["content"], "content must not be dropped"
    assert len(read["content"]) < len(_LINE) * _LINE_COUNT
    assert read.get("truncated") is True
    assert "note" in read and "truncated" in read["note"]
