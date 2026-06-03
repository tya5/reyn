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
from typing import TYPE_CHECKING

from reyn.chat.slash import reply, reply_error, slash

if TYPE_CHECKING:
    from reyn.chat.session import ChatSession

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


# Maximum number of completions surfaced in the picker — keeps the
# dropdown bounded so a directory with thousands of entries doesn't
# overwhelm the display.
_COMPLETER_MAX = 20


def _image_path_completer(
    session: "ChatSession", arg_partial: str = "",
) -> list[str]:
    """Filesystem path completer for ``/image <path>``.

    Expands ``~``, splits ``arg_partial`` into a directory part + a
    basename prefix, lists entries in that directory, and returns:
    - Directories with a trailing ``/`` (so the user can keep navigating).
    - Image files whose extension is in ``_IMAGE_EXTENSIONS``
      (case-insensitive, same set the command itself accepts).

    Results are sorted and capped at ``_COMPLETER_MAX``. Any filesystem
    error returns ``[]`` — a broken completer must not break the picker.
    ``session`` is accepted for the CompleterFn contract but unused
    (filesystem completion is session-independent).
    """
    try:
        expanded = arg_partial.expandtabs() if hasattr(arg_partial, "expandtabs") else arg_partial
        # Split into dir part + basename prefix.  When there is no slash
        # the user hasn't chosen a directory yet — use CWD.
        p = Path(expanded).expanduser() if expanded else Path("")
        if "/" in expanded or expanded.startswith("~"):
            # Separate the already-typed directory from the prefix being
            # completed.  ``parent`` resolves to ``"."`` for bare names.
            dir_part = p.parent
            prefix = p.name
        else:
            dir_part = Path(".")
            prefix = expanded

        # Resolve to an absolute path so relative refs work from CWD.
        abs_dir = (Path.cwd() / dir_part).resolve()
        if not abs_dir.is_dir():
            return []

        results: list[str] = []
        for entry in sorted(abs_dir.iterdir()):
            name = entry.name
            if not name.startswith(prefix):
                continue
            if entry.is_dir():
                # Append trailing slash so the user can immediately
                # continue typing into the chosen directory.
                candidate = str(dir_part / name) + "/"
                # Normalise "./<name>/" → "<name>/"
                if candidate.startswith("./"):
                    candidate = candidate[2:]
                results.append(candidate)
            elif entry.is_file() and _mime_for_path(entry) is not None:
                candidate = str(dir_part / name)
                if candidate.startswith("./"):
                    candidate = candidate[2:]
                results.append(candidate)
            if len(results) >= _COMPLETER_MAX:
                break

        return results
    except Exception:
        return []


@slash(
    "image",
    summary="Send an image (png/jpg/gif/webp/svg/jpeg)",
    usage="/image <path>",
    aliases=("img",),
    completer=_image_path_completer,
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

    # Issue #383 PR-C: store a path-ref to the user's original file
    # instead of duplicating the bytes as a data URL in history.jsonl.
    # The user's file is the source of truth — Reyn does not copy.
    # ``content_hash`` lets the wire-shape boundary detect file drift
    # (= the user modifying shot.png after attach surfaces as a hash
    # mismatch in the materialisation path).
    import hashlib
    content_hash = "sha256:" + hashlib.sha256(image_bytes).hexdigest()
    block: dict = {
        "type": "image",
        "path": str(path),
        "mime_type": mime,
        "content_hash": content_hash,
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
