"""The ONE tool-result history-content resolver — #5364 §1.2.

``resolve()`` decides what a persisted history entry's tool-result content
actually is, from three persisted signals — ``spilled`` (checked FIRST),
``ref`` (the backing file, #5364 §1.1 "A": every tool result is file-backed
since #5896) and ``content`` (the inline body, present for a pre-#5896 row,
a §1.5 write-refused row, or an in-process row that already holds its
body) — and, only when the answer depends on it, whether the backing file
still exists::

                              │ file present      │ file lost
    ──────────────────────────┼───────────────────┼──────────
    not spilled, no ref       │ inline (content)  │ inline (content)
    not spilled, ref, content │ inline (content)  │ inline (content)
    not spilled, ref, empty   │ inline (read ref) │ lost
    spilled                   │ ref (path)        │ lost

Row by row:

- ``not spilled, no ref`` — a pre-#5896 row (body inline in
  ``history.jsonl``) or a #5364 §1.5 row (the write was refused, so the
  body stayed inline and no ref was ever minted). Self-sufficient; the
  filesystem is never consulted (#5506, architect ruling — ``spilled``
  first, and an unspilled entry with no ref never calls ``file_exists``).
- ``not spilled, ref, content`` — the row already holds its body: an
  in-process row (``RouterLoop.feedback`` keeps the body on the resident
  ``ChatMessage``; only the durable ``history.jsonl`` line drops it — see
  ``chat_message.history_record``) or a disk row hydrated once at parse
  time (``Session._parse_history_line``). Also self-sufficient: the file
  is the DURABLE copy, not the live one, so a live process never re-reads
  disk for a body it already has (#5896 architect ruling: "process 内の
  buffer は今どおり本体を持つ（cache）— 生きている process は disk を読み
  直さない").
- ``not spilled, ref, empty`` — a row read back from ``history.jsonl``
  after a restart: the body lives ONLY in the file. Present → the body is
  read through the injected ``read_text``; missing → ``lost`` (the file
  was deleted by something other than reyn's own GC — #5896 stage ①
  excludes an un-spilled file from every eviction pass precisely so this
  cell is never reached by reyn's own hand; see
  ``MediaStore._evict_history_content_over_cap``).
- ``spilled`` — the model already sees a ref-preview for this row (its
  content IS the preview); present → ``ref``, missing → ``lost``.

``spilled`` is never re-derived from content shape (e.g. sniffing for a
``read_file(path=...)`` marker in the text) — it is read from the entry's
own persisted field, set once at write time and never re-guessed, so a
restart never flips the answer (#5364 §1.2 "D"). Whether a body is
"empty" is likewise the persisted field's own value: an un-spilled row
whose tool genuinely returned nothing has an empty file too, so reading it
back yields the same empty body — the two agree by construction.

This module is the ONE place this branch table is written (#5364 §1.2
"history 構築（純関数・1 箇所）") — a second, independently-maintained copy
of this table anywhere else in the history-build path is exactly the
class of defect CLAUDE.md's testing policy singles out ("the same
expression on both sides"). #5506's own finding was this exact claim
failing to hold against ITSELF (a pre-filter in the caller left one cell
unreachable, and a wrong copy of it survived inside this module); #5896
widened the table rather than adding a second resolver for un-spilled
rows, for the same reason.
"""
from __future__ import annotations

from typing import Callable, Literal, NamedTuple

Resolution = Literal["inline", "ref", "lost"]


class HistoryContentEntry(NamedTuple):
    """The three persisted signals :func:`resolve` reads — never re-derived
    from anything else (see module docstring). ``content`` is the inline
    body when the row carries one (``""`` for a ``history.jsonl`` row
    written since #5896, whose body lives only in its file; meaningless
    when ``spilled`` is True — the resolver never reads it in that
    branch). ``ref`` is the backing file's project-relative path — set for
    every SPILLED entry and, since #5896 (#5364 §1.1 "A"), for every
    un-spilled tool result whose write landed; ``""`` for a pre-#5896 row
    or a #5364 §1.5 write-refused row. Used to check existence, to read
    the body back (un-spilled + empty ``content``), and to report back
    when the resolution is ``"ref"`` or ``"lost"``."""
    spilled: bool
    content: str
    ref: str


class Resolved(NamedTuple):
    """``kind`` names which branch fired; ``value`` is the content to show
    (the inline text) for ``"inline"``, or the backing path for ``"ref"``/
    ``"lost"`` (a ``"lost"`` value still names the path that is missing —
    #5364 §1.5's ``gc``/``never_persisted`` reasons are reported
    separately by the caller, this function only says THAT it is lost,
    not WHY — see that section's own two-reason split)."""
    kind: Resolution
    value: str


def resolve(
    entry: HistoryContentEntry,
    file_exists: "Callable[[str], bool]",
    read_text: "Callable[[str], str] | None" = None,
) -> Resolved:
    """Resolve one history entry's tool-result content (#5364 §1.2).

    ``file_exists`` and ``read_text`` are injected (never a bare
    ``Path(ref).exists()`` / ``read_text()`` inline here) so this stays a
    pure function — the caller decides what "exists" and "read" mean (a
    real filesystem check / a ``MediaStore.read_tool_result`` in
    production, a canned answer in a test) without this module importing
    ``pathlib`` or touching disk itself. ``read_text`` is only ever called
    on the ``not spilled, ref, empty`` cell (see the module table); a
    caller that can never reach that cell (a pre-#5896 history, a spilled-
    only path) may leave it ``None`` — reaching the cell with no reader
    is a caller bug, raised as ``TypeError`` rather than silently resolved
    as ``lost``.

    #5506 (architect ruling): ``spilled`` is checked FIRST. An unspilled
    entry that already holds its body is self-sufficient — its file
    existing or not is irrelevant, so ``file_exists`` is never even called
    in that branch, not just ignored after being called.
    """
    if not entry.spilled:
        if not entry.ref or entry.content:
            return Resolved("inline", entry.content)
        if not file_exists(entry.ref):
            return Resolved("lost", entry.ref)
        if read_text is None:
            raise TypeError(
                "resolve(): an un-spilled entry with a ref and no inline body "
                "needs read_text to resolve, and the caller supplied none"
            )
        return Resolved("inline", read_text(entry.ref))
    if not file_exists(entry.ref):
        return Resolved("lost", entry.ref)
    return Resolved("ref", entry.ref)
