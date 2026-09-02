"""``/attachment PATH`` slash command — attach an ARBITRARY file to the
next user message.

#5509 (owner, 2026-08-29, verbatim): "スラッシュコマンド attachment みたいな
ので任意ファイル指定できるようにしたい" ("I want a slash command like
attachment that lets me specify any file"). ``/image`` (#366) already
established the whole mechanism this command reuses byte-for-byte — the
size gate (#364), the lossless path-ref block shape (#383 PR-C), the
shared ``session._pending_user_attachments`` queue and its drain-on-next-
-turn contract — narrowed to one extension table. This command is that
SAME mechanism opened to any file, not a new one: kept as its own module
(rather than folding into ``image.py``) because ``/image``'s own
image-specific completer/messaging stays a genuinely useful narrower
command on its own — no feature lost, nothing deprecated.

**Mime resolution — deliberately NOT a reyn-specific extension table.**
Mirrors ``core/present/artifact_payload.py``'s own established invariant
2 ("No reyn-specific extension table... two NARROWER tables already
exist — ``op_runtime/file.py``'s ``_IMAGE_EXTENSIONS`` and
``slash/image.py``'s own copy of it — but both answer a different,
narrower question... without adding a THIRD hand-maintained table
(#4431's own class)"): stdlib ``mimetypes.guess_type()``, unmodified,
falling back to ``application/octet-stream`` (RFC 2046's own generic
type) for an extension it cannot resolve — never a guess, and never a
reyn-invented mapping ``/image`` would then have TWO copies of drift
against.

**Block "type" — derived, never hand-picked** (#5526, closed in the same
PR this command lands in): ``router_loop.classify_media_block_type(mime)``
is the ONE place a mime maps to a reyn media block "type" — this command
calls it rather than writing a literal ``"type": "..."`` the way
``/image`` (pre-#5509, single fixed modality) safely could. See that
function's own docstring for the structural mismatch it closes.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash
from reyn.runtime.router_loop import classify_media_block_type

if TYPE_CHECKING:
    from reyn.runtime.session import Session

#: RFC 2046's own generic type — the fallback when ``mimetypes`` cannot
#: resolve *path*'s extension (unlike ``/image``'s narrower command, this
#: one accepts every extension, so "no mime resolved" cannot itself be a
#: refusal — it degrades to the generic type instead, same posture
#: ``_default_mime_for_block_type`` (router_loop.py) already takes for
#: every non-image block a producer emits without a declared mime).
_GENERIC_MIME = "application/octet-stream"


def _mime_for_path(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or _GENERIC_MIME


def _file_size_human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f}KB"
    return f"{n} bytes"


# Maximum number of completions surfaced in the picker — mirrors
# slash/image.py's own bound, same rationale (a directory with
# thousands of entries must not overwhelm the display).
_COMPLETER_MAX = 20


def _attachment_path_completer(
    session: "Session", arg_partial: str = "",
) -> list[str]:
    """Filesystem path completer for ``/attachment <path>``.

    Unlike ``/image``'s own completer, every regular file is a candidate
    (this command accepts any file) — only directories get special
    (trailing-``/``) treatment, and only non-regular entries (sockets,
    device files, ...) are excluded via ``is_file()``. ``session`` is
    accepted for the CompleterFn contract but unused (filesystem
    completion is session-independent, mirroring ``/image``'s own
    completer)."""
    try:
        expanded = arg_partial.expandtabs() if hasattr(arg_partial, "expandtabs") else arg_partial
        p = Path(expanded).expanduser() if expanded else Path("")
        if expanded.endswith("/"):
            dir_part = p
            prefix = ""
        elif "/" in expanded or expanded.startswith("~"):
            dir_part = p.parent
            prefix = p.name
        else:
            dir_part = Path(".")
            prefix = expanded

        abs_dir = (Path.cwd() / dir_part).resolve()
        if not abs_dir.is_dir():
            return []

        results: list[str] = []
        for entry in sorted(abs_dir.iterdir()):
            name = entry.name
            if not name.startswith(prefix):
                continue
            if entry.is_dir():
                candidate = str(dir_part / name) + "/"
                if candidate.startswith("./"):
                    candidate = candidate[2:]
                results.append(candidate)
            elif entry.is_file():
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
    "attachment",
    summary="Attach any file (any extension) to your next message",
    locus="session",
    usage="/attachment <path>",
    # No "attach" alias — /attach is already the (unrelated) agent-session
    # attach command (interfaces/slash/agents.py); a collision there
    # raises at import time (verified directly), so this command is
    # reachable only by its full name.
    completer=_attachment_path_completer,
)
async def attachment_cmd(ctx: "SlashContext", args: str) -> None:
    path_str = args.strip()
    if not path_str:
        await reply_error(
            ctx,
            "usage: /attachment <path>  (e.g. `/attachment ./report.pdf`). "
            "Any file extension is accepted.",
        )
        return

    # Resolve relative to CWD — same scope rule as /image (#4204's own
    # note on that command applies here identically: not the same scope
    # Session uses for file reads, a separate, tracked mismatch).
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    if not path.exists():
        await reply_error(ctx, f"file not found: {path_str}")
        return
    if not path.is_file():
        await reply_error(ctx, f"not a file: {path_str}")
        return

    mime = _mime_for_path(path)

    try:
        file_bytes = path.read_bytes()
    except OSError as exc:
        await reply_error(ctx, f"failed to read {path_str}: {exc}")
        return

    # Apply the shared media-size gate (= #364 infrastructure, same call
    # shape as /image). A session built without a ReynConfig (= direct
    # construction in tests) has no `_multimodal_config` — skip the gate
    # gracefully, same as /image.
    mm_cfg = getattr(ctx.session, "_multimodal_config", None)
    perm = getattr(ctx.session, "_perm", None)
    bus = getattr(ctx.session, "_intervention_bus", None)
    if mm_cfg is not None and perm is not None and bus is not None:
        try:
            await perm.require_media_load(
                size_bytes=len(file_bytes),
                source=f"chat /attachment {path.name}",
                mime_type=mime,
                max_bytes=mm_cfg.max_bytes,
                on_oversize=mm_cfg.on_oversize,
                bus=bus,
            )
        except PermissionError as exc:
            await reply_error(
                ctx,
                f"file not attached: {exc}",
            )
            return

    # #383 PR-C: a path-ref, not inline bytes — same reasoning as
    # /image (the user's file is the source of truth; reyn does not
    # copy it into history.jsonl).
    content_hash = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    block: dict = {
        "type": classify_media_block_type(mime),
        "path": str(path),
        "mime_type": mime,
        "content_hash": content_hash,
    }
    # Same shared queue /image writes to — Session._handle_inbox_text
    # drains it on the next user turn regardless of which command filled
    # it (the queue has never been image-specific; only the extension
    # table gating entry into it was).
    queue: list[dict] = getattr(ctx.session, "_pending_user_attachments", None)
    if queue is None:
        await reply_error(
            ctx,
            "attachment queue is unavailable on this session (=#366 wiring missing).",
        )
        return
    queue.append(block)
    await reply(
        ctx,
        f"attached: {path.name} ({_file_size_human(len(file_bytes))}, {mime}). "
        f"queued count: {len(queue)}. Send your next message to include it.",
    )
