"""Pure helpers for routing slash commands from the chainlit surface.

``Session.submit_user_text`` bypasses slash dispatch (= slash
handling lives on the TUI / CUI wrappers via
``session._maybe_handle_slash``). The chainlit ``@cl.on_message`` glue
mirrors the same pattern: if the typed content starts with ``/``,
route to the session's slash dispatcher; otherwise hand it to
``submit_user_text`` as a normal user turn.

This module also catalogs a small set of "quick action" slash
commands surfaced as ``cl.Action`` buttons on the welcome message so
operators don't have to memorize the slash vocabulary.

No chainlit import here — kept pure so tests run without the
``[chainlit]`` extra.
"""
from __future__ import annotations

from dataclasses import dataclass


def is_slash(text: str) -> bool:
    """Return True when ``text`` looks like a slash command.

    Empty / None / leading-whitespace strings are NOT considered slash
    (= chainlit's input box trims most whitespace already; this keeps
    behavior predictable for the rare leading-space paste).
    """
    if not text:
        return False
    return text.startswith("/")


@dataclass(frozen=True)
class QuickAction:
    """A welcome-message button entry.

    - ``name``: stable id used by ``@cl.action_callback("slash_<name>")``.
      Prefixed at the call site to namespace away from other actions.
    - ``label``: button label the operator clicks (e.g. ``"/agents"``).
    - ``slash_text``: full slash command sent to
      ``session._maybe_handle_slash`` when clicked.
    """
    name: str
    label: str
    slash_text: str


# Quick-action buttons rendered on the welcome message. Curated to
# read-only / quick-info commands so a stray click never has
# side-effects. Adding entries here is the single edit point for the
# chainlit welcome button row.
QUICK_ACTIONS: tuple[QuickAction, ...] = (
    QuickAction(name="agents", label="/agents", slash_text="/agents"),
    QuickAction(name="skills", label="/skills", slash_text="/skills"),
    QuickAction(name="list", label="/list", slash_text="/list"),
    QuickAction(name="cost", label="/cost", slash_text="/cost"),
)


def is_chainlit_history_wipe(text: str) -> bool:
    """Return True when ``text`` should also trigger the chainlit-side
    chat-thread clear after the reyn slash dispatch.

    Today the only entry that requires UI cleanup is
    ``/clear-history confirm`` — the slash wipes ``session.history`` +
    the per-agent ``history.jsonl`` reyn-side, but chainlit's
    browser-rendered ``cl.Message`` widgets stay on screen unless we
    explicitly remove them. The check tolerates extra whitespace +
    case variations so ``"  /CLEAR-HISTORY CONFIRM "`` still triggers.
    """
    if not text:
        return False
    normalized = " ".join(text.lower().split())
    return normalized == "/clear-history confirm"


def action_name_for(action: QuickAction) -> str:
    """Return the ``cl.Action.name`` (= callback key) for a QuickAction.

    Centralised so the welcome message builder and the
    ``@cl.action_callback`` decorator agree on the same namespaced key.
    """
    return f"slash_{action.name}"


__all__ = [
    "QUICK_ACTIONS",
    "QuickAction",
    "action_name_for",
    "is_chainlit_history_wipe",
    "is_slash",
]
