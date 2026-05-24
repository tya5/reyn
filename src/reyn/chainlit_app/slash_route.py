"""Pure helpers for routing slash commands from the chainlit surface.

``ChatSession.submit_user_text`` bypasses slash dispatch (= slash
handling lives on the TUI / CUI wrappers via
``session._maybe_handle_slash``). The chainlit ``@cl.on_message`` glue
mirrors the same pattern: if the typed content starts with ``/``,
route to the session's slash dispatcher; otherwise hand it to
``submit_user_text`` as a normal user turn.

This module also catalogs a small set of "quick action" slash
commands surfaced as ``cl.Action`` buttons on the welcome message so
operators don't have to memorize the slash vocabulary, and builds the
typing-time slash completion list consumed by chainlit's
``emitter.set_commands`` API (= the ``/`` palette that pops up while
the operator types).

No chainlit import here — kept pure so tests run without the
``[chainlit]`` extra.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


def action_name_for(action: QuickAction) -> str:
    """Return the ``cl.Action.name`` (= callback key) for a QuickAction.

    Centralised so the welcome message builder and the
    ``@cl.action_callback`` decorator agree on the same namespaced key.
    """
    return f"slash_{action.name}"


# ── slash typing palette (chainlit emitter.set_commands) ─────────────────


class _SlashCommandLike(Protocol):
    """Minimal surface ``build_command_dicts`` reads on each entry.

    Defined as a Protocol so tests can pass a tiny fake without
    constructing the real ``reyn.chat.slash.SlashCommand`` dataclass.
    """
    name: str
    summary: str
    hidden: bool


# Per-command lucide icon picks. Lucide icon names — see
# https://lucide.dev — chainlit forwards the string verbatim to the
# frontend, which renders the matching SVG. Anything not in this
# map falls back to ``_FALLBACK_ICON``.
_ICON_BY_NAME: dict[str, str] = {
    "agents":      "users",
    "skills":      "wrench",
    "list":        "list",
    "cost":        "dollar-sign",
    "tasks":       "list-checks",
    "expand":      "maximize-2",
    "help":        "help-circle",
    "cost-inline": "badge-dollar-sign",
    "image":       "image",
    "img":         "image",
    "find":        "search",
    "plan":        "map",
    "attach":      "link",
    "save":        "save",
    "copy":        "copy",
    "reset":       "rotate-ccw",
    "quit":        "log-out",
    "exit":        "log-out",
}

_FALLBACK_ICON = "slash"


def icon_for_slash_name(name: str) -> str:
    """Return the lucide icon name for ``name`` (with sensible fallback)."""
    return _ICON_BY_NAME.get(name, _FALLBACK_ICON)


def build_command_dicts(commands: list[_SlashCommandLike]) -> list[dict]:
    """Convert a list of reyn ``SlashCommand`` entries → chainlit CommandDicts.

    The chainlit ``emitter.set_commands`` API consumes a
    ``List[CommandDict]`` (= ``TypedDict`` with ``id`` / ``description``
    / ``icon`` required). This helper:

    - Drops ``hidden=True`` entries (= ``matrix`` / ``donut`` / ``zen``
      etc., which already don't appear in /help or the TUI palette)
    - Builds ``id = "/<name>"`` (= chainlit displays this verbatim in
      the popup palette; matches what gets dispatched on submit)
    - Carries the canonical ``summary`` through as ``description``
    - Picks a lucide icon via ``icon_for_slash_name`` so each row has
      a visual hint
    - Sorts by name for stable ordering across reloads
    """
    out: list[dict] = []
    for cmd in commands:
        if getattr(cmd, "hidden", False):
            continue
        name = getattr(cmd, "name", "")
        summary = getattr(cmd, "summary", "") or ""
        if not name:
            continue
        out.append({
            "id": f"/{name}",
            "description": summary,
            "icon": icon_for_slash_name(name),
        })
    out.sort(key=lambda d: d["id"])
    return out


__all__ = [
    "QUICK_ACTIONS",
    "QuickAction",
    "action_name_for",
    "build_command_dicts",
    "icon_for_slash_name",
    "is_slash",
]
