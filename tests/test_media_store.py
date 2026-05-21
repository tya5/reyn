"""Tier 2: MediaStore — flat-file image + tool-result storage (issue #383 PR-C).

Pins the storage layer that all multimodal cluster consumers (web_fetch
binary, file_read binary, mcp image, /image attach) emit path-refs
against.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reyn.workspace.media_store import MediaStore, MediaStoreConfig


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path)


# ── save_image ─────────────────────────────────────────────────────────


def test_save_image_writes_file_under_media_dir(tmp_path):
    """Tier 2: save_image writes the binary under .reyn/media/ and the
    returned path-ref's ``path`` is project-relative.
    """
    store = _store(tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    block = store.save_image(
        data, mime_type="image/png", chain_id="abc123", tool="web_fetch", seq=1,
    )

    assert block["type"] == "image"
    assert block["mime_type"] == "image/png"
    assert block["content_hash"] == "sha256:" + hashlib.sha256(data).hexdigest()
    # Path is project-relative and lives inside .reyn/media/.
    assert block["path"].startswith(".reyn/media/")
    full = tmp_path / block["path"]
    assert full.exists()
    assert full.read_bytes() == data


def test_save_image_filename_encodes_metadata(tmp_path):
    """Tier 2: filename has timestamp + chain_short + tool + seq + extension."""
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="image/png", chain_id="abc123def", tool="web_fetch", seq=2,
    )
    name = Path(block["path"]).name
    # Anchor on the structural pieces; the exact timestamp varies.
    assert "abc123" in name  # chain_short = first 6 of chain_id
    assert "web_fetch" in name
    assert name.endswith("-2.png")


def test_save_image_unknown_mime_no_extension(tmp_path):
    """Tier 2: unknown MIME type → filename written without extension; user
    can rename with their preferred tool. Storage still works.
    """
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="application/octet-stream",
        chain_id="", tool="test", seq=1,
    )
    name = Path(block["path"]).name
    # No extension expected for unknown MIME.
    assert "." not in name.split("-")[-1] or name.endswith("-1")


def test_save_image_sanitises_tool_token(tmp_path):
    """Tier 2: tool names with slashes / spaces are sanitised to safe tokens."""
    store = _store(tmp_path)
    block = store.save_image(
        b"x", mime_type="image/png", chain_id="abc",
        tool="mcp/playwright tool", seq=1,
    )
    name = Path(block["path"]).name
    # Slashes and spaces replaced with underscores.
    assert "/" not in name
    assert " " not in name
    assert "mcp_playwright_tool" in name


# ── read_image ─────────────────────────────────────────────────────────


def test_read_image_round_trips_saved_block(tmp_path):
    """Tier 2: save then read returns the same bytes."""
    store = _store(tmp_path)
    data = b"hello world bytes"
    block = store.save_image(data, mime_type="image/png", tool="test", seq=1)

    out, found = store.read_image(block["path"])
    assert found is True
    assert out == data


def test_read_image_returns_not_found_for_missing(tmp_path):
    """Tier 2: missing path → (b"", False)."""
    store = _store(tmp_path)
    out, found = store.read_image(".reyn/media/nope.png")
    assert out == b""
    assert found is False


def test_read_image_rejects_path_outside_media_dir(tmp_path):
    """Tier 2: path traversal outside media_dir raises PermissionError —
    defends against adversarial / corrupted path-ref ChatMessage content.
    """
    store = _store(tmp_path)
    (tmp_path / "secret.txt").write_text("not media")
    with pytest.raises(PermissionError, match="outside media_dir"):
        store.read_image("secret.txt")


def test_read_image_rejects_traversal_attempt(tmp_path):
    """Tier 2: a ../ traversal also rejected."""
    store = _store(tmp_path)
    with pytest.raises(PermissionError):
        store.read_image("../etc/passwd")


# ── save_tool_result + read_tool_result ────────────────────────────────


def test_save_tool_result_writes_to_tool_results_dir(tmp_path):
    """Tier 2: save_tool_result writes under .reyn/tool-results/ with the
    parallel naming convention as save_image.
    """
    store = _store(tmp_path)
    block = store.save_tool_result(
        "hello world", mime_type="text/plain",
        chain_id="xyz", tool="web_fetch_text", seq=1,
    )
    assert block["type"] == "tool_result_ref"
    assert block["mime_type"] == "text/plain"
    assert block["path"].startswith(".reyn/tool-results/")
    assert block["path"].endswith(".txt")
    full = tmp_path / block["path"]
    assert full.exists()
    assert full.read_text(encoding="utf-8") == "hello world"


def test_save_tool_result_html_extension(tmp_path):
    """Tier 2: text/html MIME → .html extension."""
    store = _store(tmp_path)
    block = store.save_tool_result(
        "<html>...</html>", mime_type="text/html", tool="web_fetch", seq=1,
    )
    assert Path(block["path"]).suffix == ".html"


def test_read_tool_result_round_trip(tmp_path):
    """Tier 2: save + read for text content round-trips identically."""
    store = _store(tmp_path)
    content = "Line 1\nLine 2\nLine 3\n"
    block = store.save_tool_result(content, mime_type="text/plain")

    out, found = store.read_tool_result(block["path"])
    assert found is True
    assert out == content


def test_read_tool_result_rejects_outside_dir(tmp_path):
    """Tier 2: path traversal outside tool_results_dir raises
    PermissionError — same defence as read_image.
    """
    store = _store(tmp_path)
    (tmp_path / "leak.txt").write_text("secret")
    with pytest.raises(PermissionError, match="outside tool_results_dir"):
        store.read_tool_result("leak.txt")


# ── isolation across separate save_* calls ─────────────────────────────


def test_image_and_tool_result_dirs_are_distinct(tmp_path):
    """Tier 2: save_image writes to media_dir only; save_tool_result writes
    to tool_results_dir only. Each path-ref carries its own ``type``.
    """
    store = _store(tmp_path)
    img_block = store.save_image(b"img", mime_type="image/png")
    txt_block = store.save_tool_result("txt", mime_type="text/plain")

    assert (tmp_path / ".reyn" / "media").is_dir()
    assert (tmp_path / ".reyn" / "tool-results").is_dir()
    assert img_block["type"] == "image"
    assert txt_block["type"] == "tool_result_ref"
    # Each path lives only in its own dir.
    assert "/media/" in img_block["path"]
    assert "/tool-results/" in txt_block["path"]


def test_custom_dirs_via_config(tmp_path):
    """Tier 2: MediaStoreConfig overrides the default subdirectory names."""
    cfg = MediaStoreConfig(
        media_dir=".alt/img", tool_results_dir=".alt/text",
    )
    store = MediaStore(cfg, project_root=tmp_path)
    img = store.save_image(b"x", mime_type="image/png")
    txt = store.save_tool_result("y", mime_type="text/plain")
    assert img["path"].startswith(".alt/img/")
    assert txt["path"].startswith(".alt/text/")
