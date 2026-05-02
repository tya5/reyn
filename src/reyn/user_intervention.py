"""UserIntervention — unified abstraction for skill→user prompts.

Both `ask_user` Control IR ops (free-text questions) and PermissionResolver
prompts (yes/no/always choice) flow through a single `InterventionBus`. The
bus is wired by the call site:

  - chat session   → `ChatInterventionBus` routes via outbox/inbox + Future
  - CLI / one-shot → `StdinInterventionBus` reads stdin synchronously

PR6 migrates `ask_user`. PR7 layers permission prompts onto the same bus.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InterventionChoice:
    """One option in a closed-set prompt (e.g. yes / always / no)."""
    id: str             # stable id consumed by the producer (e.g. "yes", "always")
    label: str          # human-facing label, e.g. "[A]lways"
    hotkey: str | None  # single-character shortcut; case-sensitive


@dataclass
class UserIntervention:
    """One pending question from a skill to the user.

    `choices` empty → free-text prompt (current ask_user behavior).
    `choices` non-empty → closed-set selection; consumer matches user input
    against `choice.hotkey` and resolves the future with the matching id.
    """
    kind: str                                                          # "ask_user" | "permission.*"
    prompt: str                                                        # main user-visible text
    detail: str = ""                                                   # optional second-line context
    choices: list[InterventionChoice] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)               # ask_user only
    run_id: str | None = None
    skill_name: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.future is None:
            try:
                self.future = asyncio.get_running_loop().create_future()
            except RuntimeError:
                # No running loop (e.g. tests). Caller will set a future explicitly.
                self.future = asyncio.Future()


@dataclass(frozen=True)
class InterventionAnswer:
    """Resolved answer for a UserIntervention.

    For free-text prompts only `text` is set. For choice prompts `choice_id`
    is the id of the selected `InterventionChoice` (or None when the user
    typed something that didn't match any hotkey — producer decides whether
    to re-issue the prompt).
    """
    text: str = ""
    choice_id: str | None = None


@runtime_checkable
class InterventionBus(Protocol):
    """Producer interface. Skills emit interventions; consumers (chat REPL,
    stdin, etc.) deliver them to the user and resolve the answer."""

    async def request(self, iv: UserIntervention) -> InterventionAnswer: ...


def match_choice(text: str, choices: list[InterventionChoice]) -> InterventionChoice | None:
    """Return the choice whose hotkey matches `text` exactly (case-sensitive),
    or None when no hotkey matches. Whitespace is stripped.
    """
    stripped = text.strip()
    if not stripped:
        return None
    for choice in choices:
        if choice.hotkey is not None and choice.hotkey == stripped:
            return choice
    return None


class StdinInterventionBus:
    """Synchronous-stdin implementation for non-chat contexts (`reyn run`).

    Reads via prompt_toolkit when a TTY is available; falls back to blocking
    `input()` in a worker thread. Both paths cooperate with the surrounding
    asyncio loop (no concurrent reader competes for stdin in the CLI).
    """

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        text = self._render_prompt(iv)
        raw = await self._read_line(text)
        if iv.choices:
            choice = match_choice(raw, iv.choices)
            return InterventionAnswer(text=raw, choice_id=choice.id if choice else None)
        return InterventionAnswer(text=raw)

    @staticmethod
    def _render_prompt(iv: UserIntervention) -> str:
        lines: list[str] = []
        prefix = f"[{iv.skill_name}] " if iv.skill_name else ""
        lines.append(f"{prefix}{iv.prompt}")
        if iv.detail:
            lines.append(f"  {iv.detail}")
        if iv.suggestions:
            lines.append(f"  options: {' / '.join(iv.suggestions)}")
        if iv.choices:
            labels = " / ".join(c.label for c in iv.choices)
            lines.append(f"  {labels}")
        lines.append("  > ")
        return "\n".join(lines)

    @staticmethod
    async def _read_line(prompt_text: str) -> str:
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.patch_stdout import patch_stdout
            session: PromptSession[str] = PromptSession()
            with patch_stdout():
                text = await session.prompt_async(prompt_text)
            return (text or "").strip()
        except Exception:
            def _blocking() -> str:
                print(prompt_text, end="", flush=True)
                return input()
            return (await asyncio.to_thread(_blocking)).strip()


__all__ = [
    "InterventionAnswer",
    "InterventionBus",
    "InterventionChoice",
    "StdinInterventionBus",
    "UserIntervention",
    "match_choice",
]
