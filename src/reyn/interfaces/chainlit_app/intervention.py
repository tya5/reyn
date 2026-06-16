"""Convert a reyn ``kind="intervention"`` outbox payload into the args
needed to render a chainlit Ask* prompt.

The reyn-side ``UserIntervention`` carries one of two shapes:
  - **closed-set choices**: e.g. permission gates with
    ``[A]llow once / [B]lock``. Chainlit's ``AskActionMessage`` is the
    right surface (= clickable buttons, no free-text needed).
  - **free-text**: e.g. ``ask_user`` skill ops with optional
    suggestions. Chainlit's ``AskUserMessage`` (= input box prompt).

This pure helper builds the structured args (= content string +
optional action specs); the caller in ``app.py`` wraps them with the
appropriate ``cl.Ask*Message`` and awaits the user's reply, then
hands the answer back to reyn via
``session.answer_pending_intervention(...)``.

No chainlit import here — unit tests run without the ``[chainlit]``
extra.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _ChoiceSpec:
    """One button-spec for AskActionMessage construction."""
    choice_id: str
    label: str


@dataclass(frozen=True)
class InterventionPrompt:
    """Pre-assembled view of an IV ready for chainlit rendering.

    - ``intervention_id`` is the UUID set on every ``UserIntervention``;
      the caller looks it up via
      ``session._interventions.list_active()`` to get the iv instance,
      then answers via ``session._deliver_answer_to(iv, text,
      choice_id_override=...)``. ``None`` only for malformed meta —
      caller falls back to a plain-text render.
    - ``run_id`` is the skill's run id when the IV originated from a
      running skill (= ``ask_user``); ``None`` for permission gates
      (= ``_prompt`` constructs IVs without a skill context). Not
      used for answer dispatch — keep for diagnostic / future use.
    - ``content`` is the rendered text body (prompt + detail +
      suggestions hint, in that order).
    - ``choices`` is non-empty for closed-set IVs → AskActionMessage;
      empty for free-text IVs → AskUserMessage.
    """
    intervention_id: str | None
    run_id: str | None
    content: str
    choices: tuple[_ChoiceSpec, ...]

    @property
    def is_choice(self) -> bool:
        return bool(self.choices)


def build_intervention_prompt(meta: dict | None, text: str = "") -> InterventionPrompt:
    """Pre-compute the rendering args from an IV outbox message.

    Falls back gracefully on missing meta fields:
    - missing / empty ``prompt`` → use the OutboxMessage ``text`` body
      (= the announce-time multiline string is a reasonable backup)
    - missing ``intervention_id`` → returned as None (caller falls back
      to plain-text render; can't dispatch answer back)
    - missing ``run_id`` → returned as None (informational only;
      permission gates legitimately have no run_id)
    - missing ``choices`` → empty tuple → free-text prompt
    - malformed individual choice (no ``id``) → dropped silently
    """
    meta = meta or {}
    intervention_id = (
        meta.get("intervention_id")
        if isinstance(meta.get("intervention_id"), str) else None
    )
    run_id = meta.get("run_id") if isinstance(meta.get("run_id"), str) else None

    body_parts: list[str] = []
    prompt = meta.get("prompt") if isinstance(meta.get("prompt"), str) else ""
    detail = meta.get("detail") if isinstance(meta.get("detail"), str) else ""

    if prompt:
        body_parts.append(prompt)
    elif text:
        body_parts.append(text)

    if detail:
        body_parts.append(detail)

    suggestions = meta.get("suggestions") or []
    if isinstance(suggestions, list) and suggestions:
        clean_sugg = [str(s) for s in suggestions if s]
        if clean_sugg:
            body_parts.append(
                "Examples: " + " / ".join(clean_sugg),
            )

    raw_choices = meta.get("choices") or []
    choice_specs: list[_ChoiceSpec] = []
    if isinstance(raw_choices, list):
        for c in raw_choices:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            label = c.get("label") or cid
            if not isinstance(cid, str) or not cid:
                continue
            choice_specs.append(_ChoiceSpec(choice_id=cid, label=str(label)))

    content = "\n".join(body_parts) if body_parts else "(empty prompt)"
    return InterventionPrompt(
        intervention_id=intervention_id,
        run_id=run_id,
        content=content,
        choices=tuple(choice_specs),
    )


__all__ = [
    "InterventionPrompt",
    "_ChoiceSpec",
    "build_intervention_prompt",
]
