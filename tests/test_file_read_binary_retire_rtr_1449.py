"""Tier 2: #1449 — file__read binary guard + read_tool_result retired.

read_tool_result is retired; its same-host path-ref read folds into file__read
(refs are plain files under `.reyn/tool-results/`), its image guard is superseded
by file__read's #365 media-blocks path, and a new binary guard replaces the
silent garbled-decode of non-image binaries.

Real Workspace + real op_runtime / registry — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.workspace import Workspace


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path,
            interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _read(tmp_path: Path, rel: str) -> dict:
    return asyncio.run(invoke_tool(get_default_registry(), "read_file", {"path": rel}, _ctx(tmp_path)))


# ── binary guard ────────────────────────────────────────────────────────────


def test_non_image_binary_is_guarded_not_dumped(tmp_path, monkeypatch):
    """Tier 2: #1449 — a non-image binary (NUL bytes) is NOT decoded/dumped; the
    result is a structured marker with the byte size, no content."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x89ELF\x00\x00\x01\x02\xff\xferaw")
    result = _read(tmp_path, "blob.bin")
    assert result["status"] == "error"
    assert result["binary"] is True
    assert result["content"] == ""          # no garbled dump
    assert result["byte_size"] == 13
    assert "not text-loadable" in result["error"]


def test_undetectable_non_utf8_bytes_are_guarded(tmp_path, monkeypatch):
    """Tier 2: #1449/#1452 — non-UTF-8 bytes with no confident charset detection
    (and no NUL) route to the binary guard, not a lossy decode. (#1452 update:
    *detectable* legacy encodings like latin-1 are now decoded as text — see
    test_file_read_encoding_1452; only undetectable bytes hit this guard.)"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rand.bin").write_bytes(bytes(range(0, 256)) * 4)
    result = _read(tmp_path, "rand.bin")
    assert result["status"] == "error"
    assert result["binary"] is True


def test_utf8_multibyte_text_is_NOT_false_positive(tmp_path, monkeypatch):
    """Tier 2: #1449 — valid multibyte UTF-8 (日本語 / emoji) is read as TEXT, not
    mis-flagged as binary. This is the owner's explicit false-positive caution:
    detection is NUL-byte + decode-failure, never a printable-ratio."""
    monkeypatch.chdir(tmp_path)
    text = "日本語 café ☕ 🎉 — normal text\n"
    (tmp_path / "uni.txt").write_text(text, encoding="utf-8")
    result = _read(tmp_path, "uni.txt")
    assert result["status"] == "ok"
    assert result.get("binary") is not True
    assert result["content"] == text


def test_plain_text_read_unchanged(tmp_path, monkeypatch):
    """Tier 2: #1449 — ordinary text reads are byte-identical (regression)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("line one\nline two\n", encoding="utf-8")
    result = _read(tmp_path, "f.txt")
    assert result["status"] == "ok"
    assert result["content"] == "line one\nline two\n"


def test_tool_result_ref_path_readable_via_file_read(tmp_path, monkeypatch):
    """Tier 2: #1449 — a `.reyn/tool-results/` ref (a plain text file, as web_fetch
    spills) is readable via file__read — the read_tool_result same-host case."""
    monkeypatch.chdir(tmp_path)
    ref_dir = tmp_path / ".reyn" / "tool-results"
    ref_dir.mkdir(parents=True)
    (ref_dir / "web-body.txt").write_text("the full fetched body\n", encoding="utf-8")
    result = _read(tmp_path, ".reyn/tool-results/web-body.txt")
    assert result["status"] == "ok"
    assert result["content"] == "the full fetched body\n"


# ── retire read_tool_result ─────────────────────────────────────────────────


def test_read_tool_result_no_longer_registered():
    """Tier 2: #1449 — the read_tool_result tool is retired (unregistered)."""
    reg = get_default_registry()
    assert reg.lookup("read_tool_result") is None
    # file__read (its replacement for same-host path reads) is present.
    assert reg.lookup("file__read") is not None or reg.lookup("read_file") is not None


def test_web_fetch_preview_points_to_file_read():
    """Tier 2: #1449 — web_fetch's preview message tells the model to call
    file__read(path), not the retired read_tool_result."""
    from reyn.tools.web_fetch import WEB_FETCH

    desc = WEB_FETCH.description
    assert "file__read(path)" in desc
    assert "read_tool_result" not in desc
