"""Tier 2: path-ref → data URL materialisation at the LLM wire boundary
(issue #383 PR-C).

The integration this file pins:
  - Tool handler emits a path-ref media block (via MediaStore).
  - ChatMessage stores the path-ref in its content list (no inline base64).
  - At wire-shape build time (= ``_build_history_for_router`` and the
    router_loop's ``_build_media_followup_message``), the path-ref is
    resolved to a data URL so the LLM sees the inline form it expects.

Both Reyn-owned (= ``.reyn/media/``) and user-attached (= arbitrary
project-relative path) refs are handled.
"""
from __future__ import annotations

import base64
from pathlib import Path

from reyn.chat.router_loop import _build_media_followup_message
from reyn.chat.session import (
    ChatMessage,
    _materialise_path_ref_content,
    _read_pathref_image,
)
from reyn.workspace.media_store import MediaStore, MediaStoreConfig

# ── helpers ────────────────────────────────────────────────────────────


def _new_store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path)


# ── _materialise_path_ref_content ──────────────────────────────────────


def test_materialise_str_content_passes_through(tmp_path):
    """Tier 2: str content (= text-only message) is returned unchanged."""
    out = _materialise_path_ref_content("hello", media_store=None)
    assert out == "hello"


def test_materialise_no_pathref_passes_through(tmp_path):
    """Tier 2: list content without any path-ref parts is returned unchanged."""
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    out = _materialise_path_ref_content(parts, media_store=None)
    assert out == parts


def test_materialise_resolves_media_store_pathref(tmp_path, monkeypatch):
    """Tier 2: a path-ref pointing inside the MediaStore resolves to a
    data URL via ``media_store.read_image``.
    """
    monkeypatch.chdir(tmp_path)
    store = _new_store(tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    block = store.save_image(raw, mime_type="image/png", tool="test", seq=1)

    content = [{"type": "text", "text": "look"}, block]
    out = _materialise_path_ref_content(content, media_store=store)

    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "look"}
    assert out[1]["type"] == "image_url"
    url = out[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_materialise_resolves_user_attached_pathref(tmp_path, monkeypatch):
    """Tier 2: path-ref pointing outside the MediaStore (= user-attached
    file via /image) is read directly from disk.
    """
    monkeypatch.chdir(tmp_path)
    store = _new_store(tmp_path)
    user_file = tmp_path / "user_shot.png"
    user_file.write_bytes(b"raw png data")

    content = [
        {"type": "text", "text": "this"},
        {"type": "image", "path": str(user_file), "mime_type": "image/png",
         "content_hash": "sha256:abc"},
    ]
    out = _materialise_path_ref_content(content, media_store=store)

    assert isinstance(out, list)
    url = out[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"raw png data"


def test_materialise_drops_pathref_when_file_missing(tmp_path, monkeypatch):
    """Tier 2: missing file → block is dropped (no crash, no fake content)."""
    monkeypatch.chdir(tmp_path)
    store = _new_store(tmp_path)
    content = [
        {"type": "text", "text": "this"},
        {"type": "image", "path": ".reyn/media/missing.png",
         "mime_type": "image/png"},
    ]
    out = _materialise_path_ref_content(content, media_store=store)

    # The missing block is silently dropped; text part survives.
    assert isinstance(out, list)
    assert {"type": "text", "text": "this"} in out
    # No image_url emitted for the missing path-ref.
    assert all(p.get("type") != "image_url" for p in out)


def test_read_pathref_falls_back_to_disk_for_user_files(tmp_path, monkeypatch):
    """Tier 2: ``_read_pathref_image`` reads files outside MediaStore via
    direct ``Path.read_bytes()`` rather than failing closed.
    """
    monkeypatch.chdir(tmp_path)
    user_path = tmp_path / "extern.png"
    user_path.write_bytes(b"external bytes")

    out = _read_pathref_image(str(user_path), media_store=_new_store(tmp_path))
    assert out == b"external bytes"


def test_read_pathref_returns_none_when_missing(tmp_path, monkeypatch):
    """Tier 2: missing user file → None (= block dropped at materialise time)."""
    monkeypatch.chdir(tmp_path)
    out = _read_pathref_image(
        str(tmp_path / "nope.png"), media_store=_new_store(tmp_path),
    )
    assert out is None


# ── _build_media_followup_message handles path-ref blocks ──────────────


def test_followup_resolves_pathref_via_media_store(tmp_path, monkeypatch):
    """Tier 2: router_loop's media follow-up builder materialises path-ref
    blocks via the media_store, same shape as the legacy inline path.
    """
    monkeypatch.chdir(tmp_path)
    store = _new_store(tmp_path)
    raw = b"binary content"
    block = store.save_image(
        raw, mime_type="image/jpeg", tool="mcp_playwright", seq=1,
    )

    msg = _build_media_followup_message(
        tool_name="mcp.tool__playwright.screenshot",
        media_blocks=[block],
        media_store=store,
    )
    assert msg is not None
    image_part = msg["content"][1]
    assert image_part["type"] == "image_url"
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_followup_handles_mixed_pathref_and_inline(tmp_path, monkeypatch):
    """Tier 2: mixed input (= one path-ref + one legacy inline base64)
    both materialise into image_url parts in order.
    """
    monkeypatch.chdir(tmp_path)
    store = _new_store(tmp_path)
    pathref = store.save_image(b"first", mime_type="image/png", seq=1)
    inline = {"type": "image", "data": base64.b64encode(b"second").decode("ascii"),
              "mimeType": "image/png"}

    msg = _build_media_followup_message(
        tool_name="mixed", media_blocks=[pathref, inline], media_store=store,
    )
    assert msg is not None
    (url0, url1) = [p["image_url"]["url"] for p in msg["content"] if p.get("type") == "image_url"]
    assert base64.b64decode(url0.split(",", 1)[1]) == b"first"
    assert base64.b64decode(url1.split(",", 1)[1]) == b"second"


# ── ChatMessage round-trip with path-ref content ───────────────────────


def test_chat_message_carries_pathref_content(tmp_path):
    """Tier 2: ChatMessage stores path-ref content list without inflating
    storage with base64 data.
    """
    block = {"type": "image", "path": ".reyn/media/foo.png",
             "mime_type": "image/png", "content_hash": "sha256:abc"}
    m = ChatMessage(
        role="user",
        content=[{"type": "text", "text": "see"}, block],
        ts="t1",
    )
    assert isinstance(m.content, list)
    assert m.content[1] == block
    # No base64 data inflated into the message.
    serialised = repr(m.content)
    assert "base64" not in serialised
