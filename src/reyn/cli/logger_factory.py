"""Logger selection — always plain CUI (rich reporter removed in PR-cli-1)."""
from __future__ import annotations


def make_logger(**opts):
    from reyn.reporters.console import ConsoleLogger
    return ConsoleLogger(**opts)


def make_chat_renderer():
    from reyn.chat.renderer import ConsoleChatRenderer
    return ConsoleChatRenderer()
