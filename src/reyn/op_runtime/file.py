"""file kind handler — read/write/glob/grep/delete/edit/regenerate_index/mkdir/move/stat."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from reyn.schemas.models import FileIROp

from . import register
from .context import OpContext

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
    from reyn.context_builder import control_ir_inline_cap

    model_str: str | None = None
    if ctx.resolver is not None:
        try:
            model_str = ctx.resolver.resolve(ctx.model).model
        except Exception:
            model_str = None
    return control_ir_inline_cap(model_str, events=ctx.events, phase=ctx.skill_name)


async def handle(op: FileIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    # Permission check (single point for both frontends). For
    # `regenerate_index` the file actually written is `output_path`, not
    # `path`; everything else writes to `path`.
    if ctx.permission_resolver is not None:
        if op.op in _WRITE_OPS:
            write_target = op.output_path if op.op == "regenerate_index" and op.output_path else op.path
            ctx.permission_resolver.require_file_write(
                ctx.permission_decl, write_target, ctx.skill_name,
            )
            # move also writes to dest_path — gate both source (= the file
            # being effectively deleted) and dest (= the file being created).
            if op.op == "move" and op.dest_path:
                ctx.permission_resolver.require_file_write(
                    ctx.permission_decl, op.dest_path, ctx.skill_name,
                )
        elif op.op in _READ_OPS:
            # read / glob / grep / stat — gate against read scope
            ctx.permission_resolver.require_file_read(
                ctx.permission_decl, op.path, ctx.skill_name,
            )

    if op.op == "write":
        ctx.workspace.write_file(op.path, op.content or "")
        ctx.events.emit("tool_executed", op="write_file", path=op.path)
        return {"kind": "file", "op": "write", "path": op.path, "status": "ok"}

    if op.op == "read":
        # Issue #365: image extensions → binary path with media-size gate.
        image_mime = _image_mime_for_path(op.path)
        if image_mime is not None:
            return await _read_image_file(op, ctx, mime_type=image_mime)

        content, found = ctx.workspace.read_file(op.path)
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
        explicit_bounded = op.offset is not None or op.limit is not None
        if explicit_bounded:
            # Caller asked for an explicit window — honor it verbatim.
            lines = content.splitlines(keepends=True)
            start = op.offset or 0
            sliced = lines[start:start + op.limit] if op.limit is not None else lines[start:]
            content = "".join(sliced)
            ctx.events.emit("tool_executed", op="read_file", path=op.path)
            return {
                "kind": "file",
                "op": "read",
                "path": op.path,
                "status": "ok",
                "content": content,
            }

        # #1209 (1) read-bounding, bound-only-when-over: an UNBOUNDED read whose
        # content exceeds the window-derived inline cap is truncated to a head
        # window and flagged with a STRUCTURAL truncation signal in SEPARATE
        # fields (not embedded in `content`). This keeps the content in the
        # model's decide context instead of being offloaded out of view (the
        # apply-starvation root cause, #1209); the model pages the rest via
        # `next_offset`. Small reads are returned unchanged.
        cap = _read_inline_cap(ctx)
        if len(content) > cap:
            all_lines = content.splitlines(keepends=True)
            shown: list[str] = []
            acc = 0
            for line in all_lines:
                if shown and acc + len(line) > cap:
                    break
                shown.append(line)
                acc += len(line)
            ctx.events.emit(
                "tool_executed", op="read_file", path=op.path,
                truncated=True, shown_lines=len(shown), total_lines=len(all_lines),
            )
            return {
                "kind": "file",
                "op": "read",
                "path": op.path,
                "status": "truncated",
                "content": "".join(shown),
                "shown_lines": len(shown),
                "total_lines": len(all_lines),
                "next_offset": len(shown),
                "total_chars": len(content),
            }
        ctx.events.emit("tool_executed", op="read_file", path=op.path)
        return {
            "kind": "file",
            "op": "read",
            "path": op.path,
            "status": "ok",
            "content": content,
        }

    if op.op == "glob":
        matches = ctx.workspace.glob_files(op.path, max_results=op.max_results)
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
        return _execute_grep(op, ctx)

    if op.op == "edit":
        return _execute_edit(op, ctx)

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


def _execute_grep(op: FileIROp, ctx: OpContext) -> dict:
    if not op.pattern:
        return {"kind": "file", "op": "grep", "status": "error", "error": "pattern is required for grep"}
    flags = re.IGNORECASE if op.case_insensitive else 0
    try:
        regex = re.compile(op.pattern, flags)
    except re.error as exc:
        return {"kind": "file", "op": "grep", "status": "error", "error": f"invalid regex: {exc}"}

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
        return {"kind": "file", "op": "grep", "status": "denied", "error": str(exc)}

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(ctx.workspace.base_dir))
        except ValueError:
            return str(p)

    if result.output_mode == "files_with_matches":
        matched = [_rel(f) for f in result.files]
        ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=len(matched))
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "files_with_matches", "files": matched, "count": len(matched)}

    if result.output_mode == "count":
        ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=result.count)
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "count", "count": result.count}

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

    ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=len(matches))
    return {"kind": "file", "op": "grep", "status": "ok",
            "output_mode": "content", "pattern": op.pattern,
            "matches": matches, "count": len(matches)}


def _execute_edit(op: FileIROp, ctx: OpContext) -> dict:
    if op.old_string is None:
        return {"kind": "file", "op": "edit", "status": "error", "error": "old_string is required"}
    if op.new_string is None:
        return {"kind": "file", "op": "edit", "status": "error", "error": "new_string is required"}

    content, found = ctx.workspace.read_file(op.path)
    if not found:
        suggestions = _nearby_files(ctx.workspace, op.path)
        return {
            "kind": "file",
            "op": "edit",
            "path": op.path,
            "status": "not_found",
            "error": f"file not found: {op.path}",
            "suggestions": suggestions,
        }

    count = content.count(op.old_string)
    if count == 0:
        return {"kind": "file", "op": "edit", "status": "error",
                "error": "old_string not found in file"}
    if not op.replace_all and count > 1:
        return {"kind": "file", "op": "edit", "status": "error",
                "error": f"old_string appears {count} times; set replace_all=true to replace all occurrences"}

    new_content = content.replace(op.old_string, op.new_string) if op.replace_all \
        else content.replace(op.old_string, op.new_string, 1)
    ctx.workspace.write_file(op.path, new_content)
    replacements = count if op.replace_all else 1
    ctx.events.emit("tool_executed", op="edit_file", path=op.path, replacements=replacements)
    return {"kind": "file", "op": "edit", "path": op.path, "status": "ok", "replacements": replacements}


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
    - Parses YAML frontmatter via `_split_frontmatter`. Files with no /
      malformed frontmatter are skipped silently.
    - Substitutes `entry_template` placeholders against frontmatter keys
      plus `slug` (= filename without `.md`). Missing placeholders fall
      back to empty strings via `defaultdict`, never raise KeyError.
    - Writes `header + "\\n".join(entries) + "\\n"` (trailing newline only
      when entries exist).
    """
    from reyn.compiler.parser import _split_frontmatter

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
            fm, _body = _split_frontmatter(content)
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


register("file", handle)
