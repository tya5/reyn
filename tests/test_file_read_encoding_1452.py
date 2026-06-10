"""Tier 2: #1452 — file__read decode ladder (charset-normalizer).

Follow-up to #1449's binary guard: non-UTF-8 *text* (SJIS / EUC-JP / UTF-16) is
decoded and returned as text with a detected `encoding` field, instead of being
rejected as binary. The decode ladder is BOM → UTF-8 fast-path → NUL-sniff →
charset-normalizer; genuine binary still routes to the binary-skipped marker,
and plain UTF-8 is byte-identical (no `encoding` field).

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

_JP = "こんにちは、世界。これはテスト文書です。\n複数行あります。\n"


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


# ── non-UTF-8 text is decoded (not binary-rejected) + carries encoding ──────


def test_shift_jis_text_is_decoded_with_encoding(tmp_path, monkeypatch):
    """Tier 2: #1452 — a Shift-JIS Japanese file reads as TEXT (the content
    round-trips) with a detected `encoding`, not the #1449 binary marker."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sjis.txt").write_bytes(_JP.encode("shift_jis"))
    result = _read(tmp_path, "sjis.txt")
    assert result["status"] == "ok"
    assert result.get("binary") is not True
    assert result["content"] == _JP
    assert result.get("encoding"), "non-UTF-8 read must surface the detected encoding"


def test_euc_jp_text_is_decoded(tmp_path, monkeypatch):
    """Tier 2: #1452 — EUC-JP Japanese decodes to text with an encoding field."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "euc.txt").write_bytes(_JP.encode("euc-jp"))
    result = _read(tmp_path, "euc.txt")
    assert result["status"] == "ok"
    assert result["content"] == _JP
    assert result.get("encoding")


def test_utf16_bom_text_decoded_bom_checked_before_nul_sniff(tmp_path, monkeypatch):
    """Tier 2: #1452 — UTF-16 (BOM) text decodes correctly. This pins the
    load-bearing ladder order: UTF-16 ASCII text is NUL-heavy, so the BOM check
    MUST precede the NUL-sniff or it would be mis-rejected as binary."""
    monkeypatch.chdir(tmp_path)
    ascii_heavy = "plain ascii line one\nplain ascii line two\n"  # NUL-heavy in UTF-16
    (tmp_path / "u16.txt").write_bytes(ascii_heavy.encode("utf-16"))  # includes BOM
    result = _read(tmp_path, "u16.txt")
    assert result["status"] == "ok"
    assert result.get("binary") is not True
    assert result["content"] == ascii_heavy
    assert "16" in (result.get("encoding") or "")


# ── plain UTF-8 fast path unchanged (no encoding field) ─────────────────────


def test_plain_utf8_is_fast_path_no_encoding_field(tmp_path, monkeypatch):
    """Tier 2: #1452 — plain UTF-8 (incl. multibyte) keeps the byte-identical
    result shape with NO `encoding` field (the common-case fast path)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "u8.txt").write_text(_JP, encoding="utf-8")
    result = _read(tmp_path, "u8.txt")
    assert result["status"] == "ok"
    assert result["content"] == _JP
    assert "encoding" not in result, "plain UTF-8 must not carry an encoding field"


# ── genuine binary still rejected ───────────────────────────────────────────


def test_png_binary_still_skipped(tmp_path, monkeypatch):
    """Tier 2: #1452 — a non-image binary (PNG bytes named .dat) still routes to
    the binary-skipped marker (NUL-sniff), not a garbled decode."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blob.dat").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe")
    result = _read(tmp_path, "blob.dat")
    assert result["status"] == "error"
    assert result["binary"] is True
    assert result["content"] == ""


def test_undetectable_bytes_fall_back_to_binary(tmp_path, monkeypatch):
    """Tier 2: #1452 — bytes with no confident charset-normalizer match (and no
    BOM / not UTF-8 / NUL present) fall back to the safe binary marker."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rand.dat").write_bytes(bytes(range(0, 256)) * 4)
    result = _read(tmp_path, "rand.dat")
    assert result["status"] == "error"
    assert result["binary"] is True
