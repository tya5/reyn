"""Claude Code-style interactive input driver for the inline CUI.

A long-lived prompt_toolkit Application that drives input for the interactive
(TTY) inline renderer: a rule-bar sandwiched input plus an animated working row.

Integration: run_repl's `_output_loop` prints conversation output ABOVE this app
via `run_in_terminal` (the app stays a live region at the bottom); user input is
fed to the session via `submit_user_text`, so intervention answers / slash
commands / new turns route through the session exactly as the PromptSession path
did — the app never inspects the text. The navigable status menu + dropdowns are
a follow-up (PR3b); this lands the input-driver swap with a static hint line.

`--cui` / non-TTY keep the existing PromptSession `_input_loop` (plain invariance).
"""
from __future__ import annotations

import time

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout

from reyn.interfaces.repl.renderer import _CC_ACCENT, _CC_DIM, _SPINNER


def working_line(thinking: bool, think_start: float, now: float) -> list:
    """Pure: working-row fragments while a turn runs (empty list when idle).

    The spinner frame derives from `now` so it advances smoothly regardless of
    refresh jitter; elapsed is whole seconds since `think_start`.
    """
    if not thinking:
        return []
    frame = _SPINNER[int(now * 8) % len(_SPINNER)]
    elapsed = max(0, int(now - think_start))
    return [
        (f"fg:{_CC_ACCENT}", f" {frame} "),
        (f"fg:{_CC_DIM}", f"Working… {elapsed}s"),
    ]


async def run_inline_input(registry, renderer) -> None:
    """Run the interactive inline input Application until the user quits.

    Returns on quit (Ctrl-C/D/Q or /quit /exit) so run_repl can tear down (cost
    summary) via its FIRST_COMPLETED wait.
    """
    attached = registry.attached_session()
    history = FileHistory(str(attached.workspace_dir / ".input_history"))
    buf = Buffer(multiline=False, history=history)

    def _frags() -> list:
        return working_line(
            getattr(renderer, "_thinking", False),
            getattr(renderer, "_think_start", 0.0),
            time.monotonic(),
        )

    working = ConditionalContainer(
        Window(FormattedTextControl(_frags), height=1),
        filter=Condition(lambda: getattr(renderer, "_thinking", False)),
    )
    top_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    bottom_rule = Window(height=1, char="─", style=f"fg:{_CC_DIM}")
    prompt_sym = Window(
        FormattedTextControl([(f"fg:{_CC_ACCENT} bold", "> ")]), width=2, height=1
    )
    input_win = Window(BufferControl(buffer=buf), height=1)
    inputrow = VSplit([prompt_sym, input_win])
    hint = Window(
        FormattedTextControl([(f"fg:{_CC_DIM}", " /quit to exit · ↑ history")]),
        height=1,
    )

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event) -> None:
        text = buf.text
        buf.reset(append_to_history=True)
        stripped = text.strip()
        if not stripped:
            return
        # /quit /exit are intercepted here (mirrors _input_loop) so they tear the
        # REPL down rather than dispatching as a no-op slash. Everything else —
        # plain text, intervention answers, other slash commands — flows through
        # submit_user_text, which routes it inside the session unchanged.
        if stripped in ("/quit", "/exit"):
            event.app.create_background_task(_quit(registry, event.app))
        else:
            event.app.create_background_task(_submit(registry, stripped))

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("c-q")
    def _quit_key(event) -> None:
        event.app.create_background_task(_quit(registry, event.app))

    body = HSplit([working, top_rule, inputrow, bottom_rule, hint])
    app: Application = Application(
        layout=Layout(body, focused_element=input_win),
        key_bindings=kb,
        full_screen=False,
        refresh_interval=0.1,
    )
    # patch_stdout so stray stdout/stderr (e.g. library warnings) prints cleanly
    # above the live input region instead of corrupting it — mirrors the
    # PromptSession path. Renderer output (sys.__stdout__ via run_in_terminal in
    # _output_loop) is unaffected.
    with patch_stdout():
        await app.run_async()


async def _submit(registry, text: str) -> None:
    s = registry.attached_session()
    if s is not None:
        await s.submit_user_text(text)


async def _quit(registry, app) -> None:
    await registry.shutdown()
    app.exit()
