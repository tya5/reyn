"""Render reyn's ``session.history`` into a list of Chainlit-ready entries.

When the operator switches to a different agent via the chat-profile
picker (or just opens a new browser tab on the same agent), chainlit
spins up a fresh per-session UI thread — but the reyn-side
``ChatSession.history`` already holds every prior turn from disk
(``load_history`` reads ``history.jsonl``). Without replay, the
operator sees an empty conversation while the agent still
"remembers" everything from the LLM side, which is confusing.

This module is the pure conversion layer. ``app._on_chat_start``
calls ``history_to_chainlit(session.history)`` and pushes each
returned tuple via ``cl.Message`` before starting the live drain.

Filter policy (= mirrors the CUI / TUI renderer's visible set):
- ``user`` / ``assistant`` → kept, author label same as live outbox
- ``tool`` / ``system`` / ``summary`` / ``skill_event`` → dropped
  (= LLM-wire entries or Reyn-internal markers, not chat-thread turns)

Multimodal content (= list-of-dict parts on user / tool turns) is
flattened to its text parts only — image / file parts are shown as
``[image: <name>]`` markers because chainlit can't re-display a
file that lives only as a path-ref on the operator's disk without
re-uploading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class _MessageLike(Protocol):
    """The minimal surface ``history_to_chainlit`` reads on each entry.

    Defined as a Protocol so tests can pass a tiny fake without
    instantiating the real ``ChatMessage`` (= avoids dragging in
    ChatSession's full import graph).
    """
    role: str
    content: "str | list[dict]"


@dataclass(frozen=True)
class HistoryEntry:
    """One Chainlit-ready replay frame.

    The drain-loop callers convert this to
    ``cl.Message(author=author, content=content).send()``.
    """
    author: str
    content: str


# Authors mirror ``adapter.outbox_to_chainlit`` for the same role so a
# replayed turn looks identical to a live one in the chat thread.
_AUTHOR_BY_ROLE = {
    "user": "user",
    "assistant": "agent",
}

# Roles intentionally dropped from the chat thread. Kept as an
# explicit set so future ChatMessage role additions surface as a
# silent drop (= caller can grep for this set when adding a new
# role) instead of accidentally rendering as a fallback.
_DROPPED_ROLES = frozenset({
    "tool", "system", "summary", "skill_event",
})


def _flatten_content(content: "str | list[dict]") -> str:
    """Pull a renderable text string out of ChatMessage's content union.

    String content → returned verbatim. List content (= multimodal)
    → text parts concatenated by newline, image / file parts replaced
    by ``[image: <name>]`` markers so the operator sees something was
    there without breaking the layout.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = part.get("text") or ""
            if text:
                chunks.append(text)
        elif kind == "image":
            path = part.get("path") or ""
            name = path.rsplit("/", 1)[-1] if path else "image"
            chunks.append(f"[image: {name}]")
        elif kind == "image_url":
            chunks.append("[image]")
        # other / unknown part shapes — drop silently; future
        # ChatMessage extensions land here as no-op until explicitly
        # wired (= same conservative posture as _DROPPED_ROLES).
    return "\n".join(chunks)


DEFAULT_REPLAY_CAP = 50


def _truncation_marker(omitted: int) -> HistoryEntry:
    """One-line system entry rendered at the top of a capped replay."""
    return HistoryEntry(
        author="system",
        content=(
            f"_({omitted} earlier turns omitted to keep the chat snappy. "
            "Set `REYN_CHAINLIT_HISTORY_CAP=0` to show all.)_"
        ),
    )


def history_to_chainlit(
    history: Iterable[_MessageLike], *, cap: int | None = None,
) -> list[HistoryEntry]:
    """Convert ``ChatSession.history`` into a list of replay-ready entries.

    Order preserved (= chronological), drops applied per
    ``_DROPPED_ROLES``. Empty-text turns (= ``content`` flattens to ``""``)
    are also dropped so the replay doesn't emit blank cells.

    ``cap``:
        - ``None`` (default): no cap, full history rendered.
        - positive int: keep at most this many *visible* entries; if the
          full history would exceed the cap, slice to the last ``cap``
          entries and prepend a single ``author="system"`` marker that
          tells the operator how many entries were skipped + how to
          opt back into full replay.
        - ``0`` or negative: treated the same as ``None`` (= unlimited)
          so the env-var path can use ``REYN_CHAINLIT_HISTORY_CAP=0``
          as the "show all" sentinel without a separate flag.

    Capping happens after filtering so that ``cap=50`` always shows the
    last 50 *visible* turns regardless of how many internal entries
    (tool / system / summary) sit between them on disk.
    """
    visible: list[HistoryEntry] = []
    for msg in history:
        role = getattr(msg, "role", "")
        if role in _DROPPED_ROLES:
            continue
        author = _AUTHOR_BY_ROLE.get(role)
        if author is None:
            continue
        text = _flatten_content(getattr(msg, "content", ""))
        if not text:
            continue
        visible.append(HistoryEntry(author=author, content=text))

    if cap is None or cap <= 0:
        return visible
    if len(visible) <= cap:
        return visible

    omitted = len(visible) - cap
    return [_truncation_marker(omitted)] + visible[-cap:]


__all__ = [
    "DEFAULT_REPLAY_CAP",
    "HistoryEntry",
    "history_to_chainlit",
]
