"""``/image PATH`` slash command — attach an image to the next user message.

Issue #366 — multi-modal cluster path 3/3.

The command:
  1. Resolves PATH relative to CWD (= same scope rule as file__read).
  2. Reads the bytes + infers the MIME type from the extension.
  3. Applies the shared media-size gate landed in #364
     (``PermissionResolver.require_media_load`` — 4-layer approval).
  4. Base64-encodes the bytes into a litellm-style ``image_url`` content
     part and queues it on ``session._pending_user_images``.
  5. The next user message in this session consumes the queue: its
     ``ChatMessage.media`` carries the queued blocks, and the router
     loop's history builder switches that turn to content-list shape.

Multiple ``/image`` calls before the next user message stack into a
multi-image attachment. The queue is drained per user turn — images
do not leak into subsequent turns.
"""
from __future__ import annotations

import base64
from pathlib import Path

from reyn.chat.slash import reply, reply_error, slash

# Mirror op_runtime/file._IMAGE_EXTENSIONS so the slash command accepts
# the same set #365 accepts via `file__read`.
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
}


def _mime_for_path(path: Path) -> str | None:
    return _IMAGE_EXTENSIONS.get(path.suffix.lower())


def _file_size_human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f}KB"
    return f"{n} bytes"


@slash(
    "image",
    summary="Attach an image to the next user message (multimodal input).",
    aliases=("img",),
)
async def image_cmd(session: object, args: str) -> None:
    path_str = args.strip()
    if not path_str:
        await reply_error(
            session,
            "usage: /image <path>  (e.g. `/image ./shot.png`). "
            "Supported extensions: .png / .jpg / .jpeg / .gif / .webp / .svg.",
        )
        return

    # Resolve relative to CWD (= the same scope ChatSession uses for
    # file reads). Absolute paths are honoured as-is.
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    if not path.exists():
        await reply_error(session, f"image not found: {path_str}")
        return
    if not path.is_file():
        await reply_error(session, f"not a file: {path_str}")
        return

    mime = _mime_for_path(path)
    if mime is None:
        await reply_error(
            session,
            f"unsupported image extension {path.suffix!r}. "
            f"Supported: {', '.join(sorted(_IMAGE_EXTENSIONS))}",
        )
        return

    try:
        image_bytes = path.read_bytes()
    except OSError as exc:
        await reply_error(session, f"failed to read {path_str}: {exc}")
        return

    # Apply the shared media-size gate (= #364 infrastructure). When the
    # session was built without a ReynConfig (= direct construction in
    # tests), `_multimodal_config` is None — skip the gate gracefully.
    mm_cfg = getattr(session, "_multimodal_config", None)
    perm = getattr(session, "_perm", None)
    bus = getattr(session, "_intervention_bus", None)
    if mm_cfg is not None and perm is not None and bus is not None:
        try:
            await perm.require_media_load(
                size_bytes=len(image_bytes),
                source=f"chat /image {path.name}",
                mime_type=mime,
                max_bytes=mm_cfg.max_bytes,
                on_oversize=mm_cfg.on_oversize,
                bus=bus,
            )
        except PermissionError as exc:
            await reply_error(
                session,
                f"image not attached: {exc}",
            )
            return

    data_b64 = base64.b64encode(image_bytes).decode("ascii")
    block: dict = {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{data_b64}"},
    }
    # Queue is drained by ChatSession._handle_user_message on the next
    # user turn (= attached to that ChatMessage.media).
    queue: list[dict] = getattr(session, "_pending_user_images", None)
    if queue is None:
        # ChatSession variants without #366 wiring shouldn't accept the
        # command — surface a clear error rather than silently no-op.
        await reply_error(
            session,
            "image queue is unavailable on this session (=#366 wiring missing).",
        )
        return
    queue.append(block)
    await reply(
        session,
        f"image attached: {path.name} ({_file_size_human(len(image_bytes))}, {mime}). "
        f"queued count: {len(queue)}. Send your next message to include it.",
    )
