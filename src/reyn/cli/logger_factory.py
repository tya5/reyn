"""Logger selection.

PR-cli-2: default chat mode is the Textual TUI (ReynTUIApp). When --cui flag
is passed or stdin is not a tty, the plain ConsoleChatRenderer is used.
make_chat_renderer() always returns the plain renderer (used by --cui path).
"""
from __future__ import annotations


def make_logger(**opts):
    from reyn.reporters import ConsoleLogger
    return ConsoleLogger(**opts)


def make_chat_renderer():
    """Return the plain console renderer used by --cui mode."""
    from reyn.chat.renderer import ConsoleChatRenderer
    return ConsoleChatRenderer()
