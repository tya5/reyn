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


def make_renderer(is_interactive: bool):
    """Single renderer-selection seam shared by the LOCAL and REMOTE chat paths.

    ADR-0039 P3: ``reyn chat`` and ``reyn chat --connect`` must choose the SAME
    renderer for the SAME terminal condition (D2, local ≡ remote). ``is_interactive``
    is the ``_inline_interactive`` predicate (TTY stdin AND stdout, no ``--cui``):
    True → the Claude Code-style inline CUI; False → the plain console renderer
    (piped / scripted / ``--cui``). Both paths call THIS so the choice can never
    diverge again (the remote path previously hard-coded the plain renderer)."""
    return make_inline_renderer() if is_interactive else make_chat_renderer()
