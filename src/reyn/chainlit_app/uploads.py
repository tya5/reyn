"""Convert chainlit ``Element`` uploads into reyn's image-attach queue.

Reyn's ``/image`` slash and ``ChatSession._handle_user_message`` already
share a per-session queue (``session._pending_user_images: list[dict]``)
drained on the next user turn. This module mirrors that queue's
contract from the chainlit side so a file dropped via chainlit's
attachment button rides the same code path as a typed ``/image PATH``.

The block shape is the path-ref form introduced by issue #383 PR-C
(= storage points at the file on disk; LLM-call time reads + embeds
binary). Mirrored here so we don't fork the wire shape.

Supported extensions mirror ``reyn.chat.slash.image._IMAGE_EXTENSIONS``
(= same set ``file__read`` accepts via #365). Non-image elements
(.pdf / audio / video / etc.) are dropped — multimodal pipelines for
those are V2.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# Mirror reyn.chat.slash.image._IMAGE_EXTENSIONS. Duplicated rather
# than imported because the slash module pulls in ChatSession
# internals; the chainlit adapter intentionally stays decoupled from
# that import graph so unit tests run fast.
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
}


@dataclass(frozen=True)
class _ElementLike:
    """Minimal subset of ``chainlit.Element`` the helper needs.

    The real chainlit Element has many fields; we only touch path /
    mime / name so tests can pass a small fake.
    """
    path: str | None = None
    mime: str | None = None
    name: str | None = None


def _mime_for_path(path: Path) -> str | None:
    return _IMAGE_EXTENSIONS.get(path.suffix.lower())


def build_image_block(element_path: str, *, element_mime: str | None = None) -> dict | None:
    """Build a path-ref image block for ``_pending_user_images``.

    Returns ``None`` when the file is missing, has an unsupported
    extension, or can't be read (= permission / IO failure). The
    chainlit side drops silently and continues with text + remaining
    elements; explicit error messaging is the caller's responsibility.
    """
    p = Path(element_path)
    if not p.is_file():
        return None
    mime = _mime_for_path(p) or (
        element_mime if (element_mime or "").startswith("image/") else None
    )
    if mime is None:
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    content_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    return {
        "type": "image",
        "path": str(p.resolve()),
        "mime_type": mime,
        "content_hash": content_hash,
    }


def collect_image_blocks(elements: list) -> list[dict]:
    """Walk a chainlit ``Message.elements`` list, returning supported image blocks.

    Non-image elements + unreadable paths are dropped. The returned
    order preserves the input order so the caller's enqueue keeps the
    upload sequence intact.
    """
    out: list[dict] = []
    for el in elements or []:
        path = getattr(el, "path", None)
        if not path:
            continue
        mime = getattr(el, "mime", None)
        block = build_image_block(str(path), element_mime=mime)
        if block is not None:
            out.append(block)
    return out


__all__ = [
    "_IMAGE_EXTENSIONS",
    "_ElementLike",
    "build_image_block",
    "collect_image_blocks",
]
