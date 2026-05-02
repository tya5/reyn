"""file kind handler — read/write/glob/grep/delete/edit/regenerate_index."""
from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from . import register
from .context import OpContext
from reyn.schemas.models import FileIROp


_WRITE_OPS = frozenset({"write", "edit", "delete", "regenerate_index"})


async def handle(op: FileIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    # Permission check (single point for both frontends). For
    # `regenerate_index` the file actually written is `output_path`, not
    # `path`; everything else writes to `path`.
    if ctx.permission_resolver is not None and op.op in _WRITE_OPS:
        write_target = op.output_path if op.op == "regenerate_index" and op.output_path else op.path
        ctx.permission_resolver.require_file_write(
            ctx.permission_decl, write_target, ctx.skill_name,
        )

    if op.op == "write":
        ctx.workspace.write_file(op.path, op.content or "")
        ctx.events.emit("tool_executed", op="write_file", path=op.path)
        return {"kind": "file", "op": "write", "path": op.path, "status": "ok"}

    if op.op == "read":
        content, found = ctx.workspace.read_file(op.path)
        if found and (op.offset is not None or op.limit is not None):
            lines = content.splitlines(keepends=True)
            start = op.offset or 0
            sliced = lines[start:start + op.limit] if op.limit is not None else lines[start:]
            content = "".join(sliced)
        ctx.events.emit("tool_executed", op="read_file", path=op.path)
        return {
            "kind": "file",
            "op": "read",
            "path": op.path,
            "status": "ok" if found else "not_found",
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

    raise ValueError(f"unsupported file op: {op.op!r}")


def _execute_grep(op: FileIROp, ctx: OpContext) -> dict:
    if not op.pattern:
        return {"kind": "file", "op": "grep", "status": "error", "error": "pattern is required for grep"}
    flags = re.IGNORECASE if op.case_insensitive else 0
    try:
        regex = re.compile(op.pattern, flags)
    except re.error as exc:
        return {"kind": "file", "op": "grep", "status": "error", "error": f"invalid regex: {exc}"}

    search_root = Path(op.path) if op.path else Path(".")
    try:
        resolved_root = ctx.workspace._resolve_read(str(search_root))
    except PermissionError as exc:
        return {"kind": "file", "op": "grep", "status": "denied", "error": str(exc)}

    if resolved_root.is_file():
        candidates = [resolved_root]
    else:
        glob_pattern = op.glob or "**/*"
        candidates = sorted(f for f in resolved_root.glob(glob_pattern) if f.is_file())
    if op.file_type:
        ext = op.file_type.lstrip(".")
        candidates = [f for f in candidates if f.suffix.lstrip(".") == ext]

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(ctx.workspace.base_dir))
        except ValueError:
            return str(p)

    if op.output_mode == "files_with_matches":
        matched: list[str] = []
        for f in candidates:
            try:
                if regex.search(f.read_text(encoding="utf-8", errors="replace")):
                    matched.append(_rel(f))
            except OSError:
                continue
        ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=len(matched))
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "files_with_matches", "files": matched, "count": len(matched)}

    if op.output_mode == "count":
        total = 0
        for f in candidates:
            try:
                total += len(regex.findall(f.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        ctx.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=total)
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "count", "count": total}

    matches: list[dict] = []
    head_limit = op.head_limit
    done = False
    for f in candidates:
        if done:
            break
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = _rel(f)
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            entry: dict[str, Any] = {"path": rel, "line_number": i + 1, "content": line}
            if op.context_before or op.context_after:
                start = max(0, i - op.context_before)
                end = min(len(lines), i + op.context_after + 1)
                entry["context"] = [
                    {"line_number": j + 1, "content": lines[j], "is_match": j == i}
                    for j in range(start, end)
                ]
            matches.append(entry)
            if head_limit is not None and len(matches) >= head_limit:
                done = True
                break

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
        return {"kind": "file", "op": "edit", "status": "not_found", "path": op.path}

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
