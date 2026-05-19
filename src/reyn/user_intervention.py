"""UserIntervention — unified abstraction for skill→user prompts.

Both `ask_user` Control IR ops (free-text questions) and PermissionResolver
prompts (yes/no/always choice) flow through the intervention plumbing. The
plumbing splits into two contracts (issue #254 Phase 2):

  - ``RequestBus`` (= [A] OS↔upper-layer contract): callers in the OS
    layer (limit_handler / permission gate / etc.) emit a request via
    ``bus.request(iv)`` and await an answer.  They know NOTHING about
    where the response comes from — that's the responsibility of the
    subscriber on the other end of the bus (eventually Agent in Phase 3).
  - ``UserChannel`` (= [B] Agent↔User contract): the physical delivery
    path to a user surface (TUI / stdin / a2a webhook).  Implementations
    expose ``channel.deliver(iv)``; this is what the Agent calls
    internally when it decides to forward an intervention to the user
    (vs self-answering or delegating to a parent agent).

In Phase 2 the existing ``ChatInterventionBus`` / ``StdinInterventionBus``
/ ``A2AInterventionBus`` satisfy BOTH protocols simultaneously
(``request == deliver``), so the runtime behaviour is unchanged.  The
type-level split is the foundation for Phase 3 (= Agent becomes the
``RequestBus`` subscriber and routes to a ``UserChannel`` internally).

The legacy ``InterventionBus`` name is retained as an alias of
``RequestBus`` so callers that import it keep working unchanged.

PR6 migrated `ask_user`. PR7 layered permission prompts onto the same
bus.  Phase 2 (issue #254) splits the bus into RequestBus / UserChannel.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


def _make_placeholder_future() -> asyncio.Future:
    """Create a Future that the intervention can hold until awaited.

    Prefers the running loop's future. Falls back to a fresh event loop
    when none is running (tests / sync construction paths). The placeholder
    is replaced on first await; the loop affinity matters only when the
    Future is actually awaited.
    """
    try:
        return asyncio.get_running_loop().create_future()
    except RuntimeError:
        pass
    # No running loop. Use the thread's existing or new loop.
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.create_future()


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
    # issue #268: identifies the channel that initiated this intervention
    # (= "tui:<session>" / "a2a:<run_id>" / etc.). When the origin channel
    # closes while the iv is unresolved, the iv becomes **stalled** in
    # the agent layer — other channels can observe / discard / claim it
    # via ``ChatSession.list_stalled_interventions`` /
    # ``discard_pending_intervention`` / ``claim_pending_intervention``.
    # ``None`` (= legacy default) skips the origin-pin routing entirely
    # so existing in-tree callers + tests see no change in behaviour.
    origin_channel_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.future is None:
            self.future = _make_placeholder_future()

    def to_dict(self) -> dict:
        """Serialize persistent fields for crash-recovery storage.

        ``future`` is excluded — it's volatile and the restored intervention
        gets a fresh future on from_dict (the original waiter has gone away
        with the crashed process). The output is JSON-safe so it flows
        unchanged through ``AgentSnapshot.outstanding_interventions`` and
        the WAL ``intervention_dispatched.iv_dict`` field.
        """
        out: dict = {
            "kind": self.kind,
            "prompt": self.prompt,
            "detail": self.detail,
            "choices": [
                {"id": c.id, "label": c.label, "hotkey": c.hotkey}
                for c in self.choices
            ],
            "suggestions": list(self.suggestions),
            "run_id": self.run_id,
            "skill_name": self.skill_name,
            "id": self.id,
        }
        if self.origin_channel_id is not None:
            out["origin_channel_id"] = self.origin_channel_id
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "UserIntervention":
        """Deserialize an intervention from a snapshot / WAL record.

        Resilient to missing optional fields (forward compat — older snapshots
        may pre-date newer fields). The ``future`` is reset to a fresh
        unresolved Future ready to await again.
        """
        choices = [
            InterventionChoice(
                id=c["id"], label=c["label"], hotkey=c.get("hotkey"),
            )
            for c in data.get("choices") or []
        ]
        return cls(
            kind=data["kind"],
            prompt=data["prompt"],
            detail=data.get("detail", ""),
            choices=choices,
            suggestions=list(data.get("suggestions") or []),
            run_id=data.get("run_id"),
            skill_name=data.get("skill_name"),
            origin_channel_id=data.get("origin_channel_id"),
            id=data.get("id") or uuid.uuid4().hex,
        )


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
class RequestBus(Protocol):
    """[A] OS↔upper-layer contract — emit an intervention request and
    await a response.

    Callers in the OS layer (= ``handle_limit_exceeded``, permission
    gates, skill ``ask_user`` op) know only this interface; they do NOT
    know whether the responder is a TUI listener, an A2A peer, or an
    Agent making a self-decision.  The subscriber on the other end of
    the bus decides routing (Phase 3 onward: Agent inspects context and
    chooses to ``self_answer`` / forward to ``parent_agent`` / forward
    to a ``UserChannel``).

    issue #254 Phase 2.
    """

    async def request(self, iv: UserIntervention) -> InterventionAnswer: ...


@runtime_checkable
class UserChannel(Protocol):
    """[B] Agent↔User contract — physical delivery path to a user
    surface (TUI, stdin, a2a webhook).

    Only the Agent layer (and during Phase 2 backward-compat, the OS
    layer that has not yet been migrated) calls ``deliver``.  Each
    concrete UserChannel routes the prompt to one specific surface.
    The semantics match ``RequestBus.request`` (= emit + await answer);
    the type split is conceptual rather than behavioural — it pins
    which layer is responsible for choosing a channel (Agent) versus
    which layer is responsible for using the bus (OS).

    issue #254 Phase 2.
    """

    async def deliver(self, iv: UserIntervention) -> InterventionAnswer: ...


# issue #254 Phase 5 (deprecated alias):
#
# ``InterventionBus`` was the original name for what is now ``RequestBus``
# (= the OS↔upper-layer "send a request, await an answer" contract).
# All in-tree production callers have migrated to ``RequestBus`` directly
# (verified at test time by
# ``tests/test_intervention_legacy_alias_deprecated.py``);
# the module-level binding is retained so external code (= third-party
# skills, plugins, restored snapshots) that imports the legacy name
# keeps working.
#
# **Do not add new usage** — type new code as ``RequestBus``. The alias
# is intentionally absent from ``__all__`` so ``from reyn.user_intervention
# import *`` no longer pulls it.
InterventionBus = RequestBus


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
    """``UserChannel`` implementation for non-chat contexts (`reyn run`,
    cron, scripted invocations with a terminal attached).

    Reads via prompt_toolkit when a TTY is available; falls back to blocking
    `input()` in a worker thread. Both paths cooperate with the surrounding
    asyncio loop (no concurrent reader competes for stdin in the CLI).

    Phase 2 (issue #254): also satisfies ``RequestBus`` via ``request``,
    which today is an alias for ``deliver`` so existing callers that
    receive this class typed as ``InterventionBus`` (= ``RequestBus``)
    keep working.  In Phase 3 the Agent becomes the ``RequestBus``
    subscriber and only invokes ``deliver`` on this class.
    """

    async def deliver(self, iv: UserIntervention) -> InterventionAnswer:
        """``UserChannel.deliver`` — route the prompt to stdin."""
        text = self._render_prompt(iv)
        raw = await self._read_line(text)
        if iv.choices:
            choice = match_choice(raw, iv.choices)
            return InterventionAnswer(text=raw, choice_id=choice.id if choice else None)
        return InterventionAnswer(text=raw)

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        """``RequestBus.request`` — backwards-compat entry point.

        In Phase 2 we deliberately keep this as an alias of ``deliver``
        so callers that were typed against ``InterventionBus`` continue
        to work without changes.  Phase 3 will route OS-level requests
        through the Agent, which will then call ``deliver`` on this
        channel — at that point this method becomes unused for top-level
        OS callers (= a candidate for Phase 5 removal).
        """
        return await self.deliver(iv)

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
    # NB: ``InterventionBus`` (= legacy alias of ``RequestBus``) is
    # intentionally absent from ``__all__`` as of issue #254 Phase 5.
    # The module-level binding still exists for backwards-compat; new
    # code should import ``RequestBus`` directly.
    "InterventionAnswer",
    "InterventionChoice",
    "RequestBus",
    "StdinInterventionBus",
    "UserChannel",
    "UserIntervention",
    "match_choice",
]
