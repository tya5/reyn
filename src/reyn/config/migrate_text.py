"""Comment-preserving text-level rewrite for ``reyn config migrate`` (#4295).

``_migrate`` used to round-trip the WHOLE file through ``yaml.safe_load`` +
``yaml.dump`` to apply a handful of key renames. PyYAML's loader has no
comment model at all — every operator comment, and every bit of the
operator's own formatting choices (flow-style lists, quoting, blank-line
grouping) on keys it never even touched, was silently discarded on every
migrate run. The owner hit this directly on their own 51-line ``reyn.yaml``:
migrate produced 17 lines, and every explanatory comment (API-key warnings,
rename history, why a key was removed) was gone with no warning that
anything but the moved keys had changed.

This module rewrites ONLY the renamed keys' own lines, byte-for-byte
untouched everywhere else — no round-trip through a YAML dumper at all, so
nothing an operator wrote (comment, flow list, quote style) on a key this
migrate run doesn't touch can be reformatted, let alone dropped.

Scope, deliberately narrow — anything outside it is refused, not guessed at
(the operator gets "migrate this by hand" instead of a silently wrong file):
  - Only TOP-LEVEL (column-0) keys are moved — no old key with a dot in it
    (a nested source) is supported.
  - A destination is either a bare key (``new_name``, same depth) or exactly
    one level of nesting (``parent.child``). Two or more dots is refused.
  - The key must be found in an unambiguous top-level ``key:`` line — inside
    a flow mapping (``{a: 1}``), inside a comment, or duplicated is refused.

Every rewrite is verified before it is trusted: the new text is itself
re-parsed and compared, key for key, against applying the SAME rename to the
ORIGINAL parsed structure (`config_migrate_check.verify_rewrite`) — if they
disagree, the rewrite is rejected and the caller reports "needs manual
review" instead of writing a file that might be silently wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class _Chunk:
    """One renamed key's own lines, extracted verbatim (leading comments +
    the key line + its block value if any) — never reformatted, only moved
    and, for the key line only, renamed."""

    lines: list[str]
    key_line_index: int  # index within `lines` of the actual `key:` line


@dataclass
class RewriteResult:
    text: str | None  # None when refused
    applied: list[tuple[str, str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # old_key names refused


_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+):(.*)$")
_COMMENT_ONLY_RE = re.compile(r"^\s*#")
_BLANK_RE = re.compile(r"^\s*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_top_level_key(lines: list[str], key: str) -> int | None:
    """Return the line index of the unambiguous top-level `key:` line, or
    None if not found / found more than once (refuse rather than guess)."""
    hits = [
        i for i, line in enumerate(lines)
        if not _COMMENT_ONLY_RE.match(line)
        and (m := _TOP_LEVEL_KEY_RE.match(line))
        and m.group(1) == key
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _block_extent(lines: list[str], key_line_idx: int) -> int:
    """Return the EXCLUSIVE end index of `key`'s value block starting right
    after its own line. A same-line scalar/flow value has an empty block
    (returns key_line_idx + 1). A block value (nothing but whitespace/comment
    after the colon) extends through every following line that is blank or
    indented past column 0, stopping at the next column-0 non-blank line."""
    key_line = lines[key_line_idx]
    m = _TOP_LEVEL_KEY_RE.match(key_line)
    assert m is not None
    trailing = m.group(2).strip()
    if trailing and not trailing.startswith("#"):
        return key_line_idx + 1  # same-line scalar/flow value, no block
    i = key_line_idx + 1
    while i < len(lines):
        line = lines[i]
        if _BLANK_RE.match(line):
            i += 1
            continue
        if _indent(line) == 0:
            break
        i += 1
    return i


def _leading_comment_start(lines: list[str], key_line_idx: int) -> int:
    """Return the start index of the contiguous run of column-0 comment
    lines directly above `key_line_idx` (no blank line breaking the run) —
    the operator's own explanation of that key, which moves WITH it."""
    i = key_line_idx - 1
    while i >= 0 and _indent(lines[i]) == 0 and _COMMENT_ONLY_RE.match(lines[i]):
        i -= 1
    return i + 1


def _extract_chunk(lines: list[str], old_key: str) -> tuple[_Chunk, int, int] | None:
    """Find + extract `old_key`'s chunk (leading comments + key line + block).
    Returns (chunk, start_idx, end_idx) [start, end) in the ORIGINAL lines,
    or None if the key can't be unambiguously located."""
    key_idx = _find_top_level_key(lines, old_key)
    if key_idx is None:
        return None
    start = _leading_comment_start(lines, key_idx)
    end = _block_extent(lines, key_idx)
    chunk_lines = lines[start:end]
    return _Chunk(lines=chunk_lines, key_line_index=key_idx - start), start, end


def _rename_key_line(chunk: _Chunk, new_leaf: str) -> _Chunk:
    """Return a copy of `chunk` with its key line's key token renamed to
    `new_leaf` (the trailing value/comment on that line is untouched)."""
    key_line = chunk.lines[chunk.key_line_index]
    m = _TOP_LEVEL_KEY_RE.match(key_line)
    assert m is not None
    new_key_line = f"{new_leaf}:{m.group(2)}"
    new_lines = list(chunk.lines)
    new_lines[chunk.key_line_index] = new_key_line
    return _Chunk(lines=new_lines, key_line_index=chunk.key_line_index)


def _reindent(chunk: _Chunk, extra_spaces: int) -> _Chunk:
    return _Chunk(
        lines=[
            (" " * extra_spaces + line) if line.strip() else line
            for line in chunk.lines
        ],
        key_line_index=chunk.key_line_index,
    )


def rewrite_text(text: str, renames: dict[str, str]) -> RewriteResult:
    """Apply `renames` (old_key -> destination) to `text`, in place, moving
    only the renamed keys' own lines — see module docstring for scope.

    `destination` is either `new_name` (same depth) or `parent.child` (one
    level of nesting under `parent:`, created if absent, appended to if
    present). Anything outside that scope is refused per-key: the return's
    `refused` list names which old keys need manual migration; `applied`
    names which were rewritten. `text` is returned unmodified (`.text` is
    the caller's file content either way) when NOTHING could be applied.
    """
    lines = text.split("\n")
    had_trailing_newline = text.endswith("\n")
    if had_trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]

    applied: list[tuple[str, str]] = []
    refused: list[str] = []

    # ── extract every renamed key's chunk first (positions are stable
    # since we haven't mutated `lines` yet) ────────────────────────────
    extractions: dict[str, tuple[_Chunk, int, int]] = {}
    for old_key, destination in renames.items():
        if old_key.count(".") > 0:
            refused.append(old_key)  # nested source key — out of scope
            continue
        dots = destination.count(".")
        if dots > 1:
            refused.append(old_key)  # more than one level of nesting — out of scope
            continue
        found = _extract_chunk(lines, old_key)
        if found is None:
            refused.append(old_key)
            continue
        extractions[old_key] = found

    if not extractions:
        return RewriteResult(text=None if refused else text, refused=refused)

    # ── group by destination parent (None = same-depth rename) ─────────
    same_depth: list[tuple[str, str]] = []  # (old_key, new_key)
    by_parent: dict[str, list[tuple[str, str]]] = {}
    for old_key in extractions:
        destination = renames[old_key]
        if "." in destination:
            parent, child = destination.split(".", 1)
            by_parent.setdefault(parent, []).append((old_key, child))
        else:
            same_depth.append((old_key, destination))

    # ── determine which ORIGINAL-line index is being removed, and what
    # (if anything) gets inserted starting at which ORIGINAL index — both
    # computed against the ORIGINAL, untouched `lines`, then applied in a
    # single forward pass so no index-shift bookkeeping is needed ────────
    removed_ranges = [
        (extractions[old_key][1], extractions[old_key][2])
        for old_key in extractions
    ]

    # First pass: does `parent` already exist as a top-level key in the
    # ORIGINAL text (not counting what we're about to remove — a renamed
    # key can never itself be named `parent`, since parents here are new
    # namespaces like `llm`, not any of the old top-level keys)?
    inserts_at: dict[int, list[str]] = {}

    same_depth_lines: list[str] = []
    for old_key, new_key in same_depth:
        chunk, _s, _e = extractions[old_key]
        renamed = _rename_key_line(chunk, new_key)
        same_depth_lines.extend(renamed.lines)
        applied.append((old_key, new_key))
    if same_depth:
        anchor = min(extractions[old_key][1] for old_key, _new_key in same_depth)
        inserts_at.setdefault(anchor, []).extend(same_depth_lines)

    for parent, members in by_parent.items():
        member_lines: list[str] = []
        for old_key, child in members:
            chunk, _s, _e = extractions[old_key]
            renamed = _rename_key_line(chunk, child)
            reindented = _reindent(renamed, 2)
            member_lines.extend(reindented.lines)
            applied.append((old_key, f"{parent}.{child}"))
        existing_idx = _find_top_level_key(lines, parent)
        if existing_idx is not None:
            block_end = _block_extent(lines, existing_idx)
            # Append to the existing block. `block_end` is an index into the
            # ORIGINAL lines that is NOT itself part of any removed range
            # (it's the first line after the parent's own block) — safe to
            # use as an insertion anchor directly.
            inserts_at.setdefault(block_end, []).extend(member_lines)
        else:
            anchor = min(extractions[old_key][1] for old_key, _child in members)
            inserts_at.setdefault(anchor, []).extend([f"{parent}:", *member_lines])

    new_lines: list[str] = []
    for i, line in enumerate(lines):
        if i in inserts_at:
            new_lines.extend(inserts_at[i])
        if any(start <= i < end for start, end in removed_ranges):
            continue
        new_lines.append(line)
    if len(lines) in inserts_at:  # an insertion anchored at end-of-file
        new_lines.extend(inserts_at[len(lines)])

    result_text = "\n".join(new_lines)
    if had_trailing_newline:
        result_text += "\n"
    return RewriteResult(text=result_text, applied=applied, refused=refused)
