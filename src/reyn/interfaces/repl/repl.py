"""prompt_toolkit-based REPL for AgentRegistry-managed multi-agent chat.

The REPL drains the registry-owned `repl_outbox` (always present, regardless
of which agent is attached) and forwards user input to whichever agent is
currently attached. Agent switching (`/attach <name>`) flips the registry's
attached pointer; the REPL doesn't need to re-bind anything because both
the input and output sides funnel through the registry.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from reyn.runtime.registry import AgentRegistry  # #312 PR-A: registry stays in the runtime pkg

from ._clipboard import copy_to_clipboard_async
from .renderer import ChatRenderer

logger = logging.getLogger(__name__)

# How many recent agent replies `/copy` can target (1 = newest).
_COPY_BUFFER_MAX = 20


def _copy_target(recent_replies, arg: str) -> tuple[str | None, str]:
    """Pure: resolve a ``/copy`` arg against the newest-first reply buffer.

    Returns ``(text_to_copy, status)``. ``text_to_copy`` is None when there is
    nothing to copy — ``status`` then explains why (list view / empty buffer /
    bad arg / out of range). ``recent_replies[0]`` is the newest reply.
    """
    arg = (arg or "").strip()
    n_buf = len(recent_replies)

    def _plural(n: int) -> str:
        return "reply" if n == 1 else "replies"

    if arg == "list":
        if not n_buf:
            return None, "no replies buffered yet"
        return None, f"{n_buf} {_plural(n_buf)} buffered (/copy N — 1 = newest)"
    n = 1
    if arg:
        if not arg.isdigit() or int(arg) < 1:
            return None, f"bad /copy arg {arg!r}; use a number (1 = newest) or 'list'"
        n = int(arg)
    if not n_buf:
        return None, "no agent reply to copy yet"
    if n > n_buf:
        return None, f"only {n_buf} {_plural(n_buf)} buffered"
    return recent_replies[n - 1], ""


async def _handle_copy_sentinel(recent_replies, arg: str):
    """Resolve a ``/copy`` request and return a status OutboxMessage to render.

    Replaces the unhandled ``__copy_last_reply__`` sentinel (a silent no-op
    before this) with a real clipboard copy + a visible result line.
    """
    text, status = _copy_target(recent_replies, arg)
    if text is not None:
        ok, tool = await copy_to_clipboard_async(text)
        status = (
            f"copied reply to clipboard ({tool})" if ok
            else "no clipboard tool found — install pbcopy / xclip / wl-copy / xsel"
        )
    return _simple_status(status)


async def _input_loop(
    registry: AgentRegistry,
    prompt_session: PromptSession,
    renderer: ChatRenderer,
    reply_seen: asyncio.Event | None = None,
) -> None:
    is_tty = sys.stdin.isatty()
    while True:
        # Piped / scripted mode: pace input by reply availability. Without
        # this gate, readline pulls every buffered line before the output
        # loop renders the first reply — the per-turn `reply_seen.clear()`
        # races with `set()`, and any later `wait_for(reply_seen)` may be
        # satisfied by an earlier turn's reply instead of the current one.
        # The gate serialises turns: read line N+1 only after turn N's
        # reply has been rendered (or there is no pending turn at all).
        # TTY mode is unaffected — interactive users may type ahead.
        if not is_tty and reply_seen is not None:
            await reply_seen.wait()

        try:
            if is_tty:
                with patch_stdout():
                    text = await prompt_session.prompt_async(
                        renderer.prompt_text(),
                        # Animated working indicator while a turn runs. None when
                        # idle (default base renderer) → no toolbar shown.
                        bottom_toolbar=renderer.bottom_toolbar,
                        refresh_interval=0.1,
                    )
            else:
                # Piped / scripted stdin: skip prompt_toolkit entirely. It
                # otherwise emits cursor-movement escapes (`\x1b[1A\x1b[K`)
                # that clutter logs and confuse line-buffered drivers.
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline,
                )
                if not line:
                    raise EOFError
                text = line
        except (EOFError, KeyboardInterrupt):
            # The pacing gate above guarantees any in-flight reply has
            # already been rendered before we read the next line, so we
            # can shut down immediately without a drain timeout.
            await registry.shutdown()
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            await registry.shutdown()
            return
        attached = registry.attached_session()
        if attached is None:
            renderer.message(_simple_status("no agent attached; try :agents"))
            continue
        # Mark a reply as in flight before submit so the pacing gate on
        # the next iteration blocks until the output loop signals it.
        # `/`-prefixed slash commands (`/list`, `/attach`, `/answer`, ...)
        # bypass the router and emit only `status` (no router reply),
        # so clearing the gate for them would deadlock the next pipe
        # iteration. `/quit` and `/exit` are handled above and never
        # reach this branch. Intervention answers (plain text that
        # happens to answer a pending ask_user) can still deadlock the
        # pipe; that path is rare in scripted use and tracked as a
        # known limitation.
        if reply_seen is not None and not text.startswith("/"):
            reply_seen.clear()
        await attached.submit_user_text(text)


async def _output_loop(
    registry: AgentRegistry,
    renderer: ChatRenderer,
    reply_seen: asyncio.Event | None = None,
) -> None:
    is_tty = sys.stdout.isatty()
    # Newest-first ring of recent agent replies so `/copy [N]` can grab the
    # latest (or an older) reply and pipe it to the system clipboard.
    recent_replies: deque[str] = deque(maxlen=_COPY_BUFFER_MAX)
    while True:
        msg = await registry.repl_outbox.get()
        if msg.kind == "__end__":
            return
        if msg.kind == "agent":
            recent_replies.appendleft(msg.text)
        elif msg.kind == "__copy_last_reply__":
            # /copy sentinel: resolve + copy, then render the result as a status
            # line instead of the (unhandled) sentinel — no more silent no-op.
            msg = await _handle_copy_sentinel(recent_replies, msg.text)
        # On a real terminal: wrap in run_in_terminal so the prompt is cleared
        # before output and redrawn after — required for ANSI/Rich to render
        # cleanly without corrupting the prompt.
        # On a pipe: print plainly, no prompt redraw, no cursor escapes.
        #
        # Contain a single message's render failure: this loop is the sole
        # consumer of repl_outbox, so an uncaught exception here would end the
        # loop, trip run_repl's FIRST_COMPLETED wait, and tear down the whole
        # REPL for one bad message. Log and continue instead (CancelledError is
        # BaseException, so shutdown cancellation still propagates). reply_seen
        # is still signalled below so the input pacing gate never hangs.
        try:
            if is_tty and get_app_or_none() is not None:
                await run_in_terminal(lambda m=msg: renderer.message(m))
            else:
                renderer.message(msg)
        except Exception:
            logger.exception("output loop: render failed for message kind=%r", msg.kind)
        # Signal end-of-turn for the input loop's pacing gate. "agent" is
        # the canonical reply kind; "skill_done" / "error" also count as
        # turn-terminal so a skill-launch chat or a failed router round
        # doesn't deadlock the next iteration.
        if reply_seen is not None and msg.kind in {"agent", "skill_done", "error"}:
            reply_seen.set()


def _simple_status(text: str):
    """Build a status OutboxMessage for inline rendering (no async needed)."""
    from reyn.runtime.outbox import OutboxMessage
    return OutboxMessage(kind="status", text=text)


async def run_repl(registry: AgentRegistry, renderer: ChatRenderer) -> None:
    """Attach to the default agent (or pre-attached one) and run the REPL.

    Caller is expected to have called `await registry.attach(name)` before
    invoking this function so the user lands on a known agent.
    """
    attached = registry.attached_session()
    if attached is None:
        raise RuntimeError("run_repl requires an attached agent; call registry.attach() first")

    history_path = attached.workspace_dir / ".input_history"

    # Bind the front-end listeners that must follow the FOCUSED session across
    # agent switches, via the registry (it re-wires them on every /attach):
    #  - the renderer's working-indicator chat-event callback (turn_started →
    #    spinner, turn_settled → idle), used by both input paths, and
    #  - the intervention listener channel so ask_user / cost-warn confirm /
    #    permission prompts surface and can be answered. The session is built with
    #    enforce_listener_presence=True, so without a registered listener every
    #    intervention short-circuits to an empty answer (a silent auto-refuse);
    #    DEFAULT_CHAT_CHANNEL_ID ("tui") mirrors the Textual TUI / chainlit mount.
    # Binding here (not a direct subscribe to `attached`) is what makes both
    # follow a `/attach <other>` instead of stranding on the initial session.
    from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
    registry.bind_focus_listeners(
        on_chat_event=renderer.on_chat_event,
        intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )

    renderer.banner(attached.agent_name)

    # `set` = "no reply pending" (the input loop's pacing gate is open).
    # `clear` = "a turn is in flight" (the gate blocks until the output
    # loop renders the reply). Start opened so the first read isn't gated.
    reply_seen: asyncio.Event = asyncio.Event()
    reply_seen.set()

    # Interactive TTY inline renderer → its own rule-bar Application input
    # driver. --cui / non-TTY (pipe / script) keep the PromptSession `_input_loop`
    # (plain invariance + the piped reply_seen pacing). _output_loop is shared:
    # run_in_terminal prints above whichever input is live.
    if renderer.uses_app_input() and sys.stdin.isatty():
        from reyn.interfaces.inline.app import run_inline_input
        inputs = asyncio.create_task(run_inline_input(registry, renderer))
    else:
        from prompt_toolkit.styles import Style
        prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            # Working-indicator toolbar as a dim status line, not the default
            # heavy reversed bar.
            style=Style.from_dict({"bottom-toolbar": "noreverse bg:default"}),
        )
        inputs = asyncio.create_task(
            _input_loop(registry, prompt_session, renderer, reply_seen)
        )
    outputs = asyncio.create_task(
        _output_loop(registry, renderer, reply_seen)
    )

    try:
        # Wait until one of the loops returns (user `/quit` or EOF).
        await asyncio.wait(
            {inputs, outputs}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Unwire from the LIVE attached session (handles a switch before quit),
        # then clear the binding.
        registry.unbind_focus_listeners()
        inputs.cancel()
        outputs.cancel()
        await asyncio.gather(inputs, outputs, return_exceptions=True)
        # Aggregate cost from all loaded agents
        from reyn.llm.pricing import TokenUsage
        total_usage = TokenUsage()
        total_cost = 0.0
        for name in registry.loaded_names():
            session = registry.get_session(name)
            if session is None:
                continue
            total_usage += session.total_usage
            total_cost += session.total_cost_usd
        renderer.cost_summary(total_usage, total_cost if total_cost > 0 else None)
