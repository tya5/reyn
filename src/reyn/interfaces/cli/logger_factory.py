"""Logger / chat-renderer selection.

Default interactive chat (TTY, no --cui) uses the Claude Code-style
InlineChatRenderer. --cui or a non-TTY (pipe / script) uses the plain
ConsoleChatRenderer. make_chat_renderer() returns the plain renderer;
make_inline_renderer() returns the inline one.
"""
from __future__ import annotations


def make_logger(**opts):
    from reyn.reporters import ConsoleLogger
    return ConsoleLogger(**opts)


def make_chat_renderer():
    """Return the plain console renderer used by --cui / non-TTY mode."""
    from reyn.interfaces.repl.renderer import ConsoleChatRenderer
    return ConsoleChatRenderer()


def make_inline_renderer():
    """Return the Claude Code-style inline renderer (default interactive TTY)."""
    from reyn.interfaces.repl.renderer import InlineChatRenderer
    return InlineChatRenderer()
