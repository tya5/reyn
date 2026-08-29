"""The ONE tool-result history-content resolver — #5364 §1.2.

``resolve()`` decides what a persisted history entry's tool-result content
actually is, from ``spilled`` (checked FIRST) and, only when spilled,
whether the backing file still exists (#5506, architect ruling — this
module's own two descriptions of the unspilled+missing-file cell used to
disagree with each other: the prose below said an unspilled entry's file
existing or not is IRRELEVANT, self-sufficient inline content; the table
said the opposite, "lost". The CODE implemented the table (checked
``file_exists`` before ``spilled``), so the prose was the one nobody
enforced. Ruled correct: the prose. ``spilled`` is now checked first, and
an unspilled entry never even calls ``file_exists``)::

               │ file present   │ file lost
    ───────────┼─────────────────┼──────────
    not spilled│ inline (content)│ inline (content)
    spilled    │ ref (path)      │ lost

``spilled`` is never re-derived from content shape (e.g. sniffing for a
``read_file(path=...)`` marker in the text) — it is read from the entry's
own persisted field, set once at write time and never re-guessed, so a
restart never flips the answer (#5364 §1.2 "D"). An UNSPILLED entry's file
existing or not is irrelevant (its content is already self-sufficient) —
whether that file exists is never even checked. A SPILLED entry's file can
be GC'd or fail to have ever been written at all — see :func:`resolve`'s
own branch table.

This module is the ONE place this 3-way branch is written (#5364 §1.2
"history 構築（純関数・1 箇所）") — a second, independently-maintained copy
of this table anywhere else in the history-build path is exactly the
class of defect CLAUDE.md's testing policy singles out ("the same
expression on both sides"). #5506's own finding was this exact claim
failing to hold against ITSELF: the unspilled+missing-file cell was never
reachable through any real caller (``router_history_buffer.py`` already
short-circuits an unspilled entry before calling this module at all), so
a second, wrong copy of that one cell survived inside this "ONE place"
undetected. Production behavior is unchanged by this fix (verified,
#5506) — this closes the self-contradiction defensively, for the next
caller that reaches this function without pre-filtering.
"""
from __future__ import annotations

from typing import Callable, Literal, NamedTuple

Resolution = Literal["inline", "ref", "lost"]


class HistoryContentEntry(NamedTuple):
    """The two persisted signals :func:`resolve` reads — never re-derived
    from anything else (see module docstring). ``content`` is the
    UNSPILLED inline body (meaningless when ``spilled`` is True — the
    caller need not have kept it around, and the resolver never reads it
    in that branch). ``ref`` is the backing file's path — set for every
    SPILLED entry (matches ``chat_message.py``'s own ``CONTENT_REF_META_KEY``
    comment; #5506 corrected this module's earlier "regardless of
    spilled" claim, which #5364 §1.1 "A" never actually delivered — see
    that same comment's own #5364 §1.5 caveat for the one documented
    exception). Meaningless/unread when ``spilled`` is False —
    :func:`resolve` never calls ``file_exists`` on it in that branch.
    Used only to check existence and to report back when the resolution
    is ``"ref"`` or ``"lost"``."""
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
    entry: HistoryContentEntry, file_exists: "Callable[[str], bool]",
) -> Resolved:
    """Resolve one history entry's tool-result content (#5364 §1.2).

    ``file_exists`` is injected (never a bare ``Path(ref).exists()``
    call inline here) so this stays a pure function — the caller decides
    what "exists" means (a real filesystem check in production, a
    canned answer in a test) without this module importing ``pathlib``
    or touching disk itself.

    #5506 (architect ruling): ``spilled`` is checked FIRST. An unspilled
    entry's content is already self-sufficient — its file existing or
    not is irrelevant, so ``file_exists`` is never even called in that
    branch, not just ignored after being called.
    """
    if not entry.spilled:
        return Resolved("inline", entry.content)
    if not file_exists(entry.ref):
        return Resolved("lost", entry.ref)
    return Resolved("ref", entry.ref)
