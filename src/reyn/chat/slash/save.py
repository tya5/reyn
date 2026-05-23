"""/save — write the conv pane to a text file.

Categorical UX gap fill (= "I want to share / archive this
conversation"). Snapshots the live RichLog buffer (= what's
currently in the scrollback) to a plain-text file. Pre-trim
history past ``_RICHLOG_MAX_LINES`` is out of scope — for full
agent history the user has the right-panel Events tab + the
``.reyn/events/agents/<name>/`` event log directories.

Pattern: same shape as ``/copy`` and ``/find`` — the slash
command emits a sentinel ``__save__`` OutboxMessage with the
raw path argument in ``text``; the TUI app intercepts via
``_on_save`` in app_outbox, walks
``ConversationView.dump_buffer_text()``, and writes the result
to disk.

Usage::

    /save                  # auto-generate ./reyn-conv-YYYYMMDD-HHMMSS.txt
    /save notes.txt        # write to ./notes.txt (overwrites if exists)
    /save ~/dump.txt       # write to home (~ is expanded)
    /save /tmp/sess.txt    # absolute path
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash(
    "save",
    summary="Save the conv pane to a file",
    usage="/save [path]",
)
async def save_cmd(session: "object", args: str) -> None:
    # Forward the raw arg; the TUI handler resolves the path
    # (expanduser / auto-name / overwrite) and surfaces errors via
    # the sticky status so we don't duplicate I/O logic across the
    # slash + outbox layers. Matches the ``/copy`` and ``/find``
    # pattern.
    await session._put_outbox(OutboxMessage(
        kind="__save__", text=(args or "").strip(),
    ))
