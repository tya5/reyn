"""file kind handler — read/write/glob/grep/delete/edit/regenerate_index/mkdir/move/stat."""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from reyn.data.workspace.text_codec import decode_text_or_none, encode_text
from reyn.schemas.models import FileIROp

from . import register
from .context import OpContext
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx

_WRITE_OPS = frozenset({"write", "edit", "delete", "regenerate_index", "mkdir", "move"})
_READ_OPS = frozenset({"read", "glob", "grep", "stat"})

# Issue #365: image extensions that trigger the binary read path.
# Extension-based detection (= no magic-byte sniff for the initial scope);
# unknown binaries still fall through to the text path with errors="replace"
# (= pre-#365 behaviour preserved).
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
}


def _image_mime_for_path(path: str) -> str | None:
    """Return the image MIME type for ``path`` if its extension is in
    ``_IMAGE_EXTENSIONS``, else None (= treat as text).
    """
    dot = path.rfind(".")
    if dot == -1:
        return None
    return _IMAGE_EXTENSIONS.get(path[dot:].lower())

# Max nearby-file suggestions returned on a not_found error. Mirrors the
# ~8-suggestion shape that invoke_action's UnknownActionError emits so the
# LLM's "did you mean X" narration looks the same across both surfaces.
_NOT_FOUND_SUGGESTIONS_LIMIT = 8


def _binary_skipped_result(ctx: OpContext, path: str, byte_size: int) -> dict:
    """#1449: structured result for a NON-image binary read — the bytes are NOT
    loaded into context (no garbled dump). Images go through the #365 media path
    above; this is the catch-all for compiled / archive / unknown binaries."""
    ctx.events.emit("tool_executed", op="read_file", path=path, mode="binary_skipped")
    return {
        "kind": "file",
        "op": "read",
        "path": path,
        "status": "error",
        "error": (
            f"binary file ({byte_size} bytes) — not text-loadable; its bytes were "
            "not loaded into context. If this is an image, it is shown via the "
            "media follow-up; otherwise it cannot be read as text."
        ),
        "binary": True,
        "byte_size": byte_size,
        "content": "",
    }


async def _read_image_file(op: FileIROp, ctx: OpContext, *, mime_type: str) -> dict:
    """Read an image file as bytes, apply the media-size gate, return as
    a media_blocks-bearing result (issue #365).

    Permission-gate flow: the outer ``handle`` already called
    ``require_file_read`` against ``op.path`` (= read-zone check). This
    helper additionally calls ``require_media_load`` for the multi-modal
    size cap (= shared with web__fetch / user input). When the gate
    rejects, returns ``status="denied"`` with no media payload.
    """
    image_bytes, found = ctx.workspace.read_file_bytes(op.path)
    if not found:
        ctx.events.emit("tool_executed", op="read_file", path=op.path, found=False)
        return {
            "kind": "file", "op": "read", "path": op.path,
            "status": "not_found",
            "error": f"file not found: {op.path}",
            "suggestions": _nearby_files(ctx.workspace, op.path),
            "content": "",
        }

    if ctx.permission_resolver is not None and ctx.multimodal_config is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError(
                "file read of binary image requires intervention_bus on "
                "OpContext (multimodal gate)"
            )
        try:
            await ctx.permission_resolver.require_media_load(
                size_bytes=len(image_bytes),
                source=f"file read {op.path}",
                mime_type=mime_type,
                max_bytes=ctx.multimodal_config.max_bytes,
                on_oversize=ctx.multimodal_config.on_oversize,
                bus=ctx.intervention_bus,
            )
        except PermissionError as exc:
            ctx.events.emit(
                "file_read_media_denied",
                path=op.path, size_bytes=len(image_bytes), mime_type=mime_type,
            )
            return {
                "kind": "file", "op": "read", "path": op.path,
                "status": "denied", "content_type": mime_type,
                "size_bytes": len(image_bytes), "error": str(exc),
            }

    # Issue #383 PR-C: emit path-ref via MediaStore when available;
    # fall back to inline base64 when not configured.
    media_block: dict
    if ctx.media_store is not None:
        media_block = ctx.media_store.save_image(
            image_bytes, mime_type=mime_type,
            chain_id=ctx.run_id or "", tool="file_read", seq=1,
        )
    else:
        import base64
        data_b64 = base64.b64encode(image_bytes).decode("ascii")
        media_block = {"type": "image", "data": data_b64, "mimeType": mime_type}
    ctx.events.emit(
        "tool_executed", op="read_file", path=op.path,
        mode="binary", mime_type=mime_type, media_block_count=1,
        stored_as=("path_ref" if ctx.media_store is not None else "inline_b64"),
    )
    return {
        "kind": "file", "op": "read", "path": op.path,
        "status": "ok", "content": "",
        "media_blocks": [media_block],
    }


def _nearby_files(ws, path: str, *, max_results: int = _NOT_FOUND_SUGGESTIONS_LIMIT) -> list[str]:
    """List sibling files under the parent of *path*, for use as not_found suggestions.

    Returns project-relative paths from ``Workspace.glob_files``. Empty list
    when the parent dir doesn't exist, permission is denied, or the glob
    yields nothing — never raises.
    """
    parent = str(Path(path).parent) if str(Path(path).parent) not in ("", ".") else "."
    pattern = f"{parent}/*" if parent != "." else "*"
    try:
        return ws.glob_files(pattern, max_results=max_results)
    except (PermissionError, OSError):
        return []


def _read_inline_cap(ctx: OpContext) -> int:
    """Window-derived inline cap (chars) for an unbounded read (#1209).

    Resolves ``ctx.model`` (a CLASS like ``"standard"``) to its litellm string
    BEFORE deriving the window (resolve-before-window — the #1172-correct path;
    a raw class mis-resolves to the fallback window), then reuses the shared
    ``control_ir_inline_cap`` so read-bounding and offload use the same cap.
    Falls back to the fixed floor when there is no resolver.
    """
    from reyn.core.context_builder import control_ir_inline_cap

    model_str: str | None = None
    if ctx.resolver is not None:
        try:
            model_str = ctx.resolver.resolve(ctx.model).model
        except Exception:
            model_str = None
    return control_ir_inline_cap(model_str, events=ctx.events, phase=ctx.actor)


def _resolve_for_gate(ctx: OpContext, path_str: str) -> str:
    """Resolve a file-op path against the workspace base_dir so the permission
    gate checks the SAME absolute target the op will actually read/write.

    #187 B3: the handler previously passed the raw (often relative) op.path to
    ``require_file_*``; the gate's SandboxLayer then resolved it with
    ``Path(path).resolve()`` against the HOST process cwd — not the workspace
    base_dir. Under a container backend (base_dir=/testbed) a relative repo
    write like ``astropy/io/ascii/html.py`` was therefore checked against the
    host cwd, fell outside the sandbox ``write_paths`` cap (``[/testbed]``), and
    was DENIED — even though ``Workspace.write_file`` resolves that same path
    against /testbed and would land it there. Resolving here closes the base
    mismatch (the Workspace already documents that the gate operates on the
    absolute paths it resolves). Behaviour-preserving for the host case
    (base_dir == cwd → the identical absolute path).
    """
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        return path_str
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return str(p.resolve())
    return str((ws.base_dir / p).resolve())


async def handle(op: FileIROp, ctx: OpContext) -> dict:
    # Permission check (single point for both frontends). For
    # `regenerate_index` the file actually written is `output_path`, not
    # `path`; everything else writes to `path`.
    if ctx.permission_resolver is not None:
        # #1199 S3.1c-2: fold the phase sandbox policy into the file gate's ∩ —
        # the path must also fall within the policy's read/write path caps. None
        # (no phase default_sandbox_policy) → SandboxLayer is ⊤ (unchanged).
        _sandbox = _sandbox_policy_from_ctx(ctx)
        if op.op in _WRITE_OPS:
            write_target = op.output_path if op.op == "regenerate_index" and op.output_path else op.path
            await ctx.permission_resolver.require_file_write(
                ctx.permission_decl, _resolve_for_gate(ctx, write_target), ctx.actor,
                sandbox_policy=_sandbox, bus=ctx.intervention_bus,
            )
            # move also writes to dest_path — gate both source (= the file
            # being effectively deleted) and dest (= the file being created).
            if op.op == "move" and op.dest_path:
                await ctx.permission_resolver.require_file_write(
                    ctx.permission_decl, _resolve_for_gate(ctx, op.dest_path), ctx.actor,
                    sandbox_policy=_sandbox, bus=ctx.intervention_bus,
                )
        elif op.op in _READ_OPS:
            # read / glob / grep / stat — gate against read scope
            await ctx.permission_resolver.require_file_read(
                ctx.permission_decl, _resolve_for_gate(ctx, op.path), ctx.actor,
                sandbox_policy=_sandbox, bus=ctx.intervention_bus,
            )

    if op.op == "write":
        # #1452: write authors NEW content (the author is now the LLM), so it
        # writes UTF-8 — UNLIKE edit, which preserves a file's existing encoding
        # in place. (Rationale-backed asymmetry, owner-vetoable: full replacement
        # vs partial edit. Preserving a legacy encoding on write would surface
        # not-representable errors for the common case of adding emoji/unicode.)
        # When OVERWRITING a non-UTF-8 file, surface a note that the encoding
        # changed. The pre-read is best-effort (skipped if read is denied).
        _prev_enc: str | None = None
        _prev_size: int | None = None  # #1466: bytes of the file before overwrite
        try:
            _prev_bytes, _existed = ctx.workspace.read_file_bytes(op.path)
            if _existed:
                _, _prev_enc = decode_text_or_none(_prev_bytes)
                _prev_size = len(_prev_bytes)  # zero extra I/O — reuse the encoding pre-read
        except PermissionError:
            pass
        _content = op.content or ""
        ctx.workspace.write_file(op.path, _content)
        ctx.events.emit("tool_executed", op="write_file", path=op.path)
        result: dict = {
            "kind": "file", "op": "write", "path": op.path, "status": "ok",
            "bytes_written": len(_content.encode("utf-8")),
        }
        if _prev_size is not None:
            result["previous_size_bytes"] = _prev_size
        if _prev_enc:
            result["encoding_note"] = (
                f"overwrote a {_prev_enc}-encoded file; the new content is written "
                "as UTF-8."
            )
        return result

    if op.op == "read":
        # Issue #365: image extensions → binary path with media-size gate.
        image_mime = _image_mime_for_path(op.path)
        if image_mime is not None:
            return await _read_image_file(op, ctx, mime_type=image_mime)

        # #1449: read bytes so a NON-image binary can be guarded BEFORE it is
        # decoded into garbled text and dumped into context. (Images already
        # short-circuited above via the #365 media-blocks path.) Permission
        # gating is identical — read_file_bytes resolves the read-zone the same
        # way read_file does.
        raw_bytes, found = ctx.workspace.read_file_bytes(op.path)
        if not found:
            suggestions = _nearby_files(ctx.workspace, op.path)
            ctx.events.emit("tool_executed", op="read_file", path=op.path, found=False)
            return {
                "kind": "file",
                "op": "read",
                "path": op.path,
                "status": "not_found",
                "error": f"file not found: {op.path}",
                "suggestions": suggestions,
                "content": "",
            }
        # #1452 decode ladder (extends the #1449 binary guard): BOM → UTF-8 fast
        # path → NUL-sniff binary-reject → charset-normalizer detection. Order is
        # load-bearing: the BOM check runs BEFORE the NUL-sniff because UTF-16/32
        # ASCII text is NUL-heavy and would be mis-rejected as binary. Returns
        # (None, None) for a non-text payload (→ the structured binary marker).
        content, _detected_encoding = decode_text_or_none(raw_bytes)
        if content is None:
            return _binary_skipped_result(ctx, op.path, len(raw_bytes))
        # `encoding` is surfaced ONLY when a non-UTF-8 codec was used (BOM or
        # charset-normalizer); the plain-UTF-8 fast path keeps the result shape
        # byte-identical (no `encoding` field) for the common case.
        _enc_field = {"encoding": _detected_encoding} if _detected_encoding else {}
        # #2335: read MODE. An explicit LINE window (offset/limit given, no char_offset) is honored
        # VERBATIM — the LLM's line-based read contract, byte-identical — AS LONG AS its slice fits
        # the inline cap. Otherwise (an unbounded read, a char_offset mid-line RESUME, OR an explicit
        # window whose slice exceeds the cap) the read is SELF-BOUNDING (truncate + re-read hint).
        cap = _read_inline_cap(ctx)
        explicit_line_window = (
            op.offset is not None or op.limit is not None
        ) and op.char_offset is None
        if explicit_line_window:
            lines = content.splitlines(keepends=True)
            start = op.offset or 0
            sliced = lines[start:start + op.limit] if op.limit is not None else lines[start:]
            joined = "".join(sliced)
            # A window that fits the cap is honored verbatim. An OVERSIZED window is NOT offloaded:
            # file_read's source already lives on disk at op.path, so duplicating it into an offload
            # file is wasteful (owner steer). Fall through to the self-bounding truncation below —
            # truncate inline + surface a re-read hint; the full content stays at op.path.
            if len(joined) <= cap:
                ctx.events.emit("tool_executed", op="read_file", path=op.path)
                return {
                    "kind": "file",
                    "op": "read",
                    "path": op.path,
                    "status": "ok",
                    "content": joined,
                    **_enc_field,
                }

        # #1209 read-bounding (keep-in-decide-context, not offloaded-out-of-view) + #2335 char-level
        # truncation. Accumulate WHOLE lines from (start_line, start_char); a multi-line overflow
        # stops at the LINE boundary (byte-identical to pre-#2335 — next_char_offset stays absent);
        # a SINGLE line/segment that alone exceeds the cap is CHAR-truncated so `content` is
        # GENUINELY ≤ cap (honest `_self_bounded`, #2335 fix), its tail paged via next_char_offset.
        all_lines = content.splitlines(keepends=True)
        start_line = op.offset or 0
        start_char = op.char_offset or 0
        shown: list[str] = []
        acc = 0
        next_offset: "int | None" = None
        next_char_offset: "int | None" = None
        i = start_line
        seg_start = start_char
        while i < len(all_lines):
            seg = all_lines[i][seg_start:]
            if shown and acc + len(seg) > cap:
                # A subsequent line overflows → stop at the LINE boundary (exclude it; the
                # continuation is a normal line read at its start). Pre-#2335 behavior.
                next_offset = i
                break
            if not shown and len(seg) > cap:
                # #2335: a single line/segment ALONE exceeds the cap → char-truncate for an honest
                # bound; its tail resumes mid-line via next_char_offset.
                shown.append(seg[:cap])
                acc += cap
                next_offset = i
                next_char_offset = seg_start + cap
                break
            shown.append(seg)
            acc += len(seg)
            i += 1
            seg_start = 0

        if next_offset is not None:
            ctx.events.emit(
                "tool_executed", op="read_file", path=op.path,
                truncated=True, shown_lines=len(shown), total_lines=len(all_lines),
            )
            result: dict = {
                "kind": "file",
                "op": "read",
                "path": op.path,
                "status": "truncated",
                "content": "".join(shown),
                "shown_lines": len(shown),
                "total_lines": len(all_lines),
                "next_offset": next_offset,
                "total_chars": len(content),
                # Owner: the LLM must recognize the content was cut (not the whole file). Explicit
                # marker + a plain re-read hint pointing at the on-disk source — no offload copy is
                # made for a file_read (the full content already lives at op.path).
                "_truncated": True,
                "note": (
                    f"content truncated to fit context ({len(''.join(shown))} of {len(content)} "
                    f"chars shown); the full file is on disk at {op.path!r} — re-read from "
                    f"offset {next_offset} to continue."
                ),
                # #2296: content bound ≤ the inline cap BY CONSTRUCTION → exempt from the generic
                # control_ir offload (else re-offload on envelope size alone and recurse).
                "_self_bounded": True,
                **_enc_field,
            }
            if next_char_offset is not None:
                # #2335: mid-line resume position (set ONLY when a single line was char-truncated).
                result["next_char_offset"] = next_char_offset
            return result

        ctx.events.emit("tool_executed", op="read_file", path=op.path)
        return {
            "kind": "file",
            "op": "read",
            "path": op.path,
            "status": "ok",
            "content": "".join(shown),
            # #2296: content ≤ the inline cap by construction → exempt from the generic offload.
            "_self_bounded": True,
            **_enc_field,
        }

    if op.op == "glob":
        # #2782: offloaded — pure read (tree-walk), no atomicity concern, safe to
        # move wholesale to a worker thread. `ctx.events.emit` stays on the event
        # loop thread (called after the `await`, not inside the threaded call) —
        # EventStore.write ultimately does an `asyncio.Queue.put_nowait`, which is
        # NOT thread-safe if called from a to_thread worker thread.
        matches = await asyncio.to_thread(ctx.workspace.glob_files, op.path, max_results=op.max_results)
        ctx.events.emit("tool_executed", op="glob_files", path=op.path, match_count=len(matches))
        return {
            "kind": "file",
            "op": "glob",
            "pattern": op.path,
            "status": "ok",
            "matches": matches,
            "count": len(matches),
        }

    if op.op == "delete":
        deleted = ctx.workspace.delete_file(op.path)
        ctx.events.emit("tool_executed", op="delete_file", path=op.path, deleted=deleted)
        return {"kind": "file", "op": "delete", "path": op.path, "status": "ok", "deleted": deleted}

    if op.op == "grep":
        return await _execute_grep(op, ctx)

    if op.op == "edit":
        return await _execute_edit(op, ctx)

    if op.op == "regenerate_index":
        return _execute_regenerate_index(op, ctx)

    if op.op == "mkdir":
        try:
            created = ctx.workspace.make_directory(op.path)
        except FileExistsError as exc:
            ctx.events.emit("tool_executed", op="mkdir", path=op.path, status="error")
            return {
                "kind": "file", "op": "mkdir", "path": op.path,
                "status": "error", "error": str(exc),
            }
        ctx.events.emit("tool_executed", op="mkdir", path=op.path, created=created)
        return {
            "kind": "file", "op": "mkdir", "path": op.path,
            "status": "ok", "created": created,
        }

    if op.op == "move":
        if not op.dest_path:
            return {
                "kind": "file", "op": "move", "path": op.path,
                "status": "error", "error": "dest_path is required for move",
            }
        moved = ctx.workspace.move_path(op.path, op.dest_path)
        if not moved:
            ctx.events.emit("tool_executed", op="move", path=op.path, found=False)
            return {
                "kind": "file", "op": "move", "path": op.path,
                "dest_path": op.dest_path, "status": "not_found",
                "error": f"source file not found: {op.path}",
            }
        ctx.events.emit("tool_executed", op="move", path=op.path, dest_path=op.dest_path)
        return {
            "kind": "file", "op": "move", "path": op.path,
            "dest_path": op.dest_path, "status": "ok", "moved": True,
        }

    if op.op == "stat":
        info = ctx.workspace.stat_path(op.path)
        if info is None:
            ctx.events.emit("tool_executed", op="stat", path=op.path, found=False)
            return {
                "kind": "file", "op": "stat", "path": op.path,
                "status": "not_found",
                "error": f"path not found: {op.path}",
            }
        ctx.events.emit("tool_executed", op="stat", path=op.path)
        return {
            "kind": "file", "op": "stat", "path": op.path,
            "status": "ok", "info": info,
        }

    raise ValueError(f"unsupported file op: {op.op!r}")


def _execute_grep_sync(op: FileIROp, ctx: OpContext) -> tuple[dict, int | None]:
    """The I/O-bound core of ``grep`` (tree-walk + per-file read + regex scan) —
    pure, safe to run wholesale on a worker thread (#2782). Returns
    ``(result, match_count_to_emit)``; ``match_count_to_emit`` is ``None`` for the
    validation-error branches (no event was emitted for those before #2782
    either). The caller emits ``tool_executed`` AFTER the ``to_thread`` call
    returns, back on the event loop thread — ``ctx.events.emit`` ultimately does
    an ``asyncio.Queue.put_nowait`` (EventStore's DurabilityWorker), which is NOT
    thread-safe if called from a worker thread."""
    if not op.pattern:
        return {"kind": "file", "op": "grep", "status": "error", "error": "pattern is required for grep"}, None
    flags = re.IGNORECASE if op.case_insensitive else 0
    try:
        regex = re.compile(op.pattern, flags)
    except re.error as exc:
        return {"kind": "file", "op": "grep", "status": "error", "error": f"invalid regex: {exc}"}, None

    # FP-0008 #1115 Stage 1: the glob+read+regex scan is an environment-internal
    # primitive run by the backend (Workspace.grep gates the root + delegates).
    # The handler keeps presentation: regex compile / error envelopes / relativize.
    try:
        result = ctx.workspace.grep(
            op.path or ".",
            regex,
            glob=op.glob,
            file_type=op.file_type,
            output_mode=op.output_mode,
            head_limit=op.head_limit,
            context_before=op.context_before,
            context_after=op.context_after,
        )
    except PermissionError as exc:
        return {"kind": "file", "op": "grep", "status": "denied", "error": str(exc)}, None

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(ctx.workspace.base_dir))
        except ValueError:
            return str(p)

    if result.output_mode == "files_with_matches":
        matched = [_rel(f) for f in result.files]
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "files_with_matches", "files": matched, "count": len(matched)}, len(matched)

    if result.output_mode == "count":
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "count", "count": result.count}, result.count

    matches: list[dict] = []
    for hit in result.matches:
        entry: dict[str, Any] = {
            "path": _rel(hit["path"]),
            "line_number": hit["line_number"],
            "content": hit["content"],
        }
        if "context" in hit:
            entry["context"] = hit["context"]
        matches.append(entry)

    return {"kind": "file", "op": "grep", "status": "ok",
            "output_mode": "content", "pattern": op.pattern,
            "matches": matches, "count": len(matches)}, len(matches)


async def _execute_grep(op: FileIROp, ctx: OpContext) -> dict:
    result, match_count = await asyncio.to_thread(_execute_grep_sync, op, ctx)
    if match_count is not None:
        ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=match_count)
    return result


def _changed_region_preview(
    new_content: str,
    start_offset: int,
    new_len: int,
    *,
    context_lines: int = 3,
    max_lines: int = 40,
) -> str:
    """A numbered-line view of the changed region for an edit result (#1418).

    **show-not-judge**: renders the lines around where ``new_string`` landed as
    1-based ``<lineno>\\t<text>`` only — NO syntax check, NO validity verdict, no
    encoding of "correct". The agent self-assesses (e.g. notices it inserted at
    indent 0 while the surrounding body is indent 8).

    **language-agnostic**: pure line slicing, no language parsing — works on any
    text file, not just Python.

    **bounded-by-construction**: capped at ``max_lines`` so a large multi-line
    insert cannot bloat the result.

    ``start_offset`` is the char offset (in ``new_content``) where the change
    begins — i.e. ``content.index(old_string)``, which is identical in the old
    and new content because the prefix is unchanged. ``new_len`` is
    ``len(new_string)`` (0 for a deletion, which then shows the surrounding
    context at the seam).
    """
    lines = new_content.split("\n")
    total = len(lines)
    start_line = new_content.count("\n", 0, start_offset)
    end_line = new_content.count("\n", 0, start_offset + new_len)
    lo = max(0, start_line - context_lines)
    hi = min(total - 1, end_line + context_lines)
    truncated = False
    if hi - lo + 1 > max_lines:
        hi = lo + max_lines - 1
        truncated = True
    rendered = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(lo, hi + 1))
    if truncated:
        rendered += "\n…\t(preview truncated)"
    return rendered


def _execute_edit_sync(op: FileIROp, ctx: OpContext) -> tuple[dict, int | None, str | None]:
    """The read-modify-write core of ``edit_file`` — run WHOLESALE on a worker
    thread (#2782), preserving today's atomicity: no ``await`` used to appear
    between the read and the write (the whole stretch ran uninterrupted on the
    event loop), so no ``await`` may appear inside this function either — it
    must stay one uninterrupted synchronous call, just now off-loop. Splitting
    the read and the write into separate ``to_thread`` calls would reintroduce
    an interleaving window (a concurrent session's edit landing between this
    read and this write) that does not exist today.

    Returns ``(result, replacements_to_emit, written_path)``. NEITHER
    ``tool_executed`` NOR ``workspace_updated`` may be emitted from inside this
    function — ``ctx.workspace.write_file_bytes`` transitively emits
    ``workspace_updated`` unconditionally (a bug caught in review: emitting
    off-loop doesn't raise, it falls to ``EventStore``'s non-serialized
    sync-fallback write path, racing the DurabilityWorker's own writes to the
    same file — see ``write_file_bytes``'s docstring). Called with
    ``emit=False`` here; the caller emits BOTH events AFTER this returns, back
    on the event loop thread. See ``_execute_grep_sync``'s docstring for the
    ``asyncio.Queue.put_nowait`` half of why off-loop emit is unsafe."""
    if op.old_string is None:
        return {"kind": "file", "op": "edit", "status": "error", "error": "old_string is required"}, None, None
    if op.new_string is None:
        return {"kind": "file", "op": "edit", "status": "error", "error": "new_string is required"}, None, None

    raw_bytes, found = ctx.workspace.read_file_bytes(op.path)
    if not found:
        suggestions = _nearby_files(ctx.workspace, op.path)
        return {
            "kind": "file",
            "op": "edit",
            "path": op.path,
            "status": "not_found",
            "error": f"file not found: {op.path}",
            "suggestions": suggestions,
        }, None, None

    # #1452: decode via the shared codec ladder so a non-UTF-8 text file can be
    # edited in place (encoding preserved on write-back below). A binary file
    # cannot be edited as text.
    content, _encoding = decode_text_or_none(raw_bytes)
    if content is None:
        return {
            "kind": "file", "op": "edit", "path": op.path, "status": "error",
            "binary": True,
            "error": "binary file — cannot edit as text (its bytes were not loaded).",
        }, None, None

    count = content.count(op.old_string)
    if count == 0:
        return {"kind": "file", "op": "edit", "status": "error",
                "error": "old_string not found in file"}, None, None
    if not op.replace_all and count > 1:
        return {"kind": "file", "op": "edit", "status": "error",
                "error": f"old_string appears {count} times; set replace_all=true to replace all occurrences"}, None, None

    new_content = content.replace(op.old_string, op.new_string) if op.replace_all \
        else content.replace(op.old_string, op.new_string, 1)
    # #1452: re-encode with the file's ORIGINAL encoding (BOM restored for
    # utf-8-sig/utf-16/utf-32). If the edit isn't representable in that codec
    # (e.g. an emoji written into a Shift-JIS file), ERROR and leave the file
    # untouched — never silently transcode the whole file to UTF-8.
    encoded = encode_text(new_content, _encoding)
    if encoded is None:
        return {
            "kind": "file", "op": "edit", "path": op.path, "status": "error",
            "encoding": _encoding or "utf-8",
            "error": (
                f"the edit is not representable in the file's encoding "
                f"({_encoding or 'utf-8'}) — file left unchanged. Some new "
                "characters cannot be encoded there."
            ),
        }, None, None
    written_path = ctx.workspace.write_file_bytes(op.path, encoded, emit=False)
    replacements = count if op.replace_all else 1
    # #1418: an additive, show-not-judge preview of the changed region so the
    # agent can SEE what landed (and at what indent), not just the count. For
    # replace_all this shows the first changed region; the count is in
    # ``replacements``.
    start_offset = content.index(op.old_string)
    preview = _changed_region_preview(new_content, start_offset, len(op.new_string))
    return {
        "kind": "file", "op": "edit", "path": op.path, "status": "ok",
        "replacements": replacements, "preview": preview,
        **({"encoding": _encoding} if _encoding else {}),
    }, replacements, written_path


async def _execute_edit(op: FileIROp, ctx: OpContext) -> dict:
    result, replacements, written_path = await asyncio.to_thread(_execute_edit_sync, op, ctx)
    if replacements is not None:
        # Order matches pre-#2782 behavior: write_file_bytes's workspace_updated
        # emit ran BEFORE the handler's tool_executed emit.
        if written_path is not None:
            ctx.events.emit("workspace_updated", path=written_path)
        ctx.events.emit("tool_executed", op="edit_file", path=op.path, replacements=replacements)
    return result


def regenerate_index_impl(
    *,
    dir_path: Path,
    output_path: Path,
    entry_template: str,
    header: str = "",
) -> int:
    """Pure helper: rebuild `output_path` from the YAML frontmatter of every
    `*.md` file in `dir_path`. Returns the number of entries written.

    The OS layer is intentionally format-agnostic — every memory-specific
    string (the index filename, header text, entry markup) is supplied by
    the caller. Used by:
    - the `file/regenerate_index` op handler (LLM-driven regen)
    - the `reyn memory` CLI (post-mutation sync)

    Behavior:
    - Scans direct children of `dir_path` matching `*.md`, sorted by name.
    - Skips any file whose basename equals `output_path.name` so the index
      doesn't include itself.
    - Parses YAML frontmatter via `split_frontmatter`. Files with no /
      malformed frontmatter are skipped silently.
    - Substitutes `entry_template` placeholders against frontmatter keys
      plus `slug` (= filename without `.md`). Missing placeholders fall
      back to empty strings via `defaultdict`, never raise KeyError.
    - Writes `header + "\\n".join(entries) + "\\n"` (trailing newline only
      when entries exist).
    """
    from reyn.core.frontmatter import split_frontmatter

    output_basename = output_path.name
    entries: list[str] = []
    if dir_path.is_dir():
        for child in sorted(dir_path.glob("*.md")):
            if child.name == output_basename:
                continue
            try:
                content = child.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, _body = split_frontmatter(content)
            if not isinstance(fm, dict) or not fm:
                # No / malformed frontmatter — skip rather than emit a
                # placeholder-empty entry like `- []() — `.
                continue
            ctx_dict: dict = defaultdict(str, **{str(k): "" if v is None else str(v) for k, v in fm.items()})
            ctx_dict["slug"] = child.stem
            try:
                entries.append(entry_template.format_map(ctx_dict))
            except (KeyError, IndexError, ValueError):
                continue
    output_path.parent.mkdir(parents=True, exist_ok=True)
    body_text = "\n".join(entries)
    output_path.write_text(
        header + body_text + ("\n" if entries else ""),
        encoding="utf-8",
    )
    return len(entries)


def _execute_regenerate_index(op: FileIROp, ctx: OpContext) -> dict:
    if not op.output_path:
        return {"kind": "file", "op": "regenerate_index", "status": "error",
                "error": "output_path is required for regenerate_index"}
    if not op.entry_template:
        return {"kind": "file", "op": "regenerate_index", "status": "error",
                "error": "entry_template is required for regenerate_index"}
    # Resolve through workspace's permission-aware path methods so reads
    # outside the project hit the same denylist as the rest of the runtime.
    dir_resolved = ctx.workspace._resolve_read(op.path)
    output_resolved = ctx.workspace._resolve_write(op.output_path)
    n = regenerate_index_impl(
        dir_path=dir_resolved,
        output_path=output_resolved,
        entry_template=op.entry_template,
        header=op.header or "",
    )
    ctx.events.emit(
        "tool_executed", op="regenerate_index",
        path=op.path, output_path=op.output_path, entries=n,
    )
    return {
        "kind": "file", "op": "regenerate_index",
        "path": op.path, "output_path": op.output_path,
        "status": "ok", "entries": n,
    }


from reyn.core.offload.canonical import file_to_canonical  # noqa: E402

register("file", handle, canonical=file_to_canonical)
