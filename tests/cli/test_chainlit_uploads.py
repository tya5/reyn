"""Tier 1: ``reyn.chainlit_app.uploads`` contract.

Chainlit's attachment button drops files onto disk + hands a
``Message.elements`` list to ``@cl.on_message``. The uploads module
converts each supported image element into the same path-ref block
shape ``reyn.chat.slash.image`` writes to
``session._pending_user_images``. This test pins:

1. Supported extensions are accepted (= same set as the slash
   command + ``file__read`` via #365).
2. Unsupported extensions are dropped (= silent skip, not crash).
3. Missing / unreadable paths are dropped.
4. ``content_hash`` uses sha256 prefix shape (= path-ref boundary
   for drift detection in materialisation).
5. Order preserved across ``collect_image_blocks`` (= upload sequence
   intact when caller enqueues).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from reyn.chainlit_app.uploads import (
    _IMAGE_EXTENSIONS,
    build_image_block,
    collect_image_blocks,
)


@dataclass
class _FakeElement:
    """Mimics chainlit Element's path / mime / name surface."""
    path: str | None = None
    mime: str | None = None
    name: str | None = None


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # minimal not-a-real-PNG


def _write_png(tmp_path: Path, name: str = "shot.png") -> Path:
    p = tmp_path / name
    p.write_bytes(_PNG_BYTES)
    return p


@pytest.mark.parametrize("ext,expected_mime", sorted(_IMAGE_EXTENSIONS.items()))
def test_supported_extensions_build_blocks(
    tmp_path: Path, ext: str, expected_mime: str
):
    """Tier 1: each whitelisted extension → block with correct mime."""
    p = _write_png(tmp_path, f"x{ext}")
    block = build_image_block(str(p))
    assert block is not None
    assert block["type"] == "image"
    assert block["mime_type"] == expected_mime
    assert block["path"] == str(p.resolve())


def test_unsupported_extension_is_dropped(tmp_path: Path):
    """Tier 1: .txt / .pdf / unknown → returns None (silent skip)."""
    p = tmp_path / "doc.txt"
    p.write_bytes(b"hello")
    assert build_image_block(str(p)) is None


def test_missing_file_is_dropped(tmp_path: Path):
    """Tier 1: nonexistent path → returns None, no exception."""
    assert build_image_block(str(tmp_path / "ghost.png")) is None


def test_content_hash_uses_sha256_prefix(tmp_path: Path):
    """Tier 1: content_hash is ``sha256:<hex>`` (= matches reyn slash shape
    so the materialisation path's drift detector works uniformly)."""
    p = _write_png(tmp_path)
    block = build_image_block(str(p))
    assert block is not None
    assert block["content_hash"].startswith("sha256:")
    expected = "sha256:" + hashlib.sha256(_PNG_BYTES).hexdigest()
    assert block["content_hash"] == expected


def test_mime_override_from_element_used_when_extension_unknown(tmp_path: Path):
    """Tier 1: element-side mime ``image/png`` is accepted even when the
    file extension wouldn't normally match (= chainlit's stored upload
    keeps a UUID name with no useful suffix)."""
    p = tmp_path / "uuid_no_extension"
    p.write_bytes(_PNG_BYTES)
    block = build_image_block(str(p), element_mime="image/png")
    assert block is not None
    assert block["mime_type"] == "image/png"


def test_mime_override_non_image_still_dropped(tmp_path: Path):
    """Tier 1: element-side mime ``application/pdf`` → dropped (= image-only
    PoC, V2 expands to other multimodal kinds)."""
    p = tmp_path / "doc"
    p.write_bytes(b"%PDF")
    assert build_image_block(str(p), element_mime="application/pdf") is None


def test_collect_preserves_order(tmp_path: Path):
    """Tier 1: 3 elements in order → 3 blocks in same order (= upload sequence)."""
    a = _write_png(tmp_path, "a.png")
    b = _write_png(tmp_path, "b.png")
    c = _write_png(tmp_path, "c.png")
    elements = [_FakeElement(path=str(p)) for p in (a, b, c)]
    out = collect_image_blocks(elements)
    assert [block["path"] for block in out] == [
        str(a.resolve()), str(b.resolve()), str(c.resolve()),
    ]


def test_collect_skips_non_image_silently(tmp_path: Path):
    """Tier 1: mixed list → image kept, non-image dropped, no exception."""
    img = _write_png(tmp_path, "good.png")
    txt = tmp_path / "bad.txt"
    txt.write_bytes(b"hi")
    elements = [
        _FakeElement(path=str(img)),
        _FakeElement(path=str(txt)),
        _FakeElement(path=None),
        _FakeElement(path=str(tmp_path / "ghost.png")),
    ]
    out = collect_image_blocks(elements)
    assert [b["path"] for b in out] == [str(img.resolve())]


def test_collect_handles_empty_or_none(tmp_path: Path):
    """Tier 1: empty list / None → empty list (= safe to call unconditionally)."""
    assert collect_image_blocks([]) == []
    assert collect_image_blocks(None) == []  # type: ignore[arg-type]
