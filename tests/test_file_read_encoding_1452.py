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

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext

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


# ── edit/write encoding round-trip (the critical corruption-prevention) ─────


def _edit(tmp_path, args):
    return asyncio.run(invoke_tool(get_default_registry(), "edit_file", args, _ctx(tmp_path)))


# Rich enough JP content that charset-normalizer detects a stable SJIS-family
# codec (short/ambiguous content can be mis-detected — that's a real caveat, and
# the impl handles it safely: a mis-decode just means old_string isn't found, an
# error, never a corrupting write).
_SJIS_DOC = "これは日本語のテスト文書です。\n設定: value = 旧\n複数行の内容があります。\n"


def test_sjis_edit_round_trips_encoding_preserved(tmp_path, monkeypatch):
    """Tier 2: #1452 — editing a Shift-JIS file preserves its encoding (writes
    back in that codec, not silently UTF-8) and leaves bytes outside the edit
    span unchanged. The corruption the unconditional UTF-8 write would cause."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.txt").write_bytes(_SJIS_DOC.encode("shift_jis"))
    result = _edit(tmp_path, {"path": "s.txt", "old_string": "旧", "new_string": "新"})
    assert result["status"] == "ok"
    enc = result.get("encoding")
    assert enc  # surfaced: a non-UTF-8 codec was preserved
    back = (tmp_path / "s.txt").read_bytes()
    # round-trips in the detected codec, and the edit landed.
    assert back.decode(enc) == _SJIS_DOC.replace("旧", "新")
    # the bytes are NOT UTF-8 (the encoding was preserved, not transcoded).
    assert back != _SJIS_DOC.replace("旧", "新").encode("utf-8")


def test_utf16_edit_preserves_bom_and_encoding(tmp_path, monkeypatch):
    """Tier 2: #1452 — editing a UTF-16 (BOM) file preserves the BOM + codec."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "u16.txt").write_bytes("alpha\nbeta\n".encode("utf-16"))
    result = _edit(tmp_path, {"path": "u16.txt", "old_string": "beta", "new_string": "gamma"})
    assert result["status"] == "ok"
    back = (tmp_path / "u16.txt").read_bytes()
    assert back.decode("utf-16") == "alpha\ngamma\n"
    assert back.startswith(b"\xff\xfe") or back.startswith(b"\xfe\xff")  # BOM preserved


def test_emoji_into_sjis_errors_and_leaves_file_untouched(tmp_path, monkeypatch):
    """Tier 2: #1452 — an edit not representable in the file's encoding (emoji →
    Shift-JIS) ERRORS and leaves the file byte-for-byte unchanged (no silent
    transcode to UTF-8). The critical no-corruption guarantee."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.txt").write_bytes(_SJIS_DOC.encode("shift_jis"))
    before = (tmp_path / "s.txt").read_bytes()
    result = _edit(tmp_path, {"path": "s.txt", "old_string": "旧", "new_string": "🎉"})
    assert result["status"] == "error"
    assert (tmp_path / "s.txt").read_bytes() == before  # untouched


def test_edit_binary_errors(tmp_path, monkeypatch):
    """Tier 2: #1452 — editing a binary file errors (cannot decode as text)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.dat").write_bytes(b"\x00\x01\x02\xff\xfebinary")
    result = _edit(tmp_path, {"path": "b.dat", "old_string": "x", "new_string": "y"})
    assert result["status"] == "error"
    assert result.get("binary") is True


def test_write_overwrite_sjis_becomes_utf8_with_note(tmp_path, monkeypatch):
    """Tier 2: #1452 — write (full replacement) of an existing Shift-JIS file
    writes UTF-8 and surfaces an encoding_note (the edit/write asymmetry)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.txt").write_bytes("古い内容\n".encode("shift_jis"))
    result = asyncio.run(invoke_tool(
        get_default_registry(), "write_file",
        {"path": "s.txt", "content": "new content 🎉\n"}, _ctx(tmp_path),
    ))
    assert result["status"] == "ok"
    assert result.get("encoding_note")  # noted the SJIS→UTF-8 change
    assert (tmp_path / "s.txt").read_bytes() == "new content 🎉\n".encode("utf-8")


# ── grep across encodings (backend-seam wiring) ─────────────────────────────


def _grep(tmp_path, pattern):
    return asyncio.run(invoke_tool(
        get_default_registry(), "grep_files", {"path": ".", "pattern": pattern}, _ctx(tmp_path),
    ))


def test_grep_matches_pattern_in_sjis_file(tmp_path, monkeypatch):
    """Tier 2: #1452 — grep finds a pattern inside a Shift-JIS file (the backend
    decode now goes through the codec; the old utf-8-replace silently missed it)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "doc.txt").write_bytes("検索対象 TARGET_TOKEN です\n".encode("shift_jis"))
    result = _grep(tmp_path, "TARGET_TOKEN")
    assert result["status"] == "ok"
    assert result.get("match_count", len(result.get("matches", []))) >= 1


def test_grep_skips_binary_file(tmp_path, monkeypatch):
    """Tier 2: #1452 — grep skips a binary file rather than matching against
    replacement-char garbage."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.dat").write_bytes(b"\x00\x01TARGET_TOKEN\x02\xff")
    result = _grep(tmp_path, "TARGET_TOKEN")
    assert result["status"] == "ok"
    assert result.get("match_count", len(result.get("matches", []))) == 0
