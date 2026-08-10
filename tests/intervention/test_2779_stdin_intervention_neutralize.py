"""Tier 2a: #2779 — StdinInterventionBus prompt render neutralizes LLM-derived content.

Sibling follow-up to #2776 (which unified the inline-CUI intervention display
with the ``present`` renderer discipline / FP-0054 terminal neutralizer, but
was scoped to the inline-CUI surface only). ``StdinInterventionBus`` is the
`reyn run` / cron / scripted-stdin surface — a DIFFERENT entry point with a
DIFFERENT rendering path (a prompt_toolkit prompt string, not the Rich
Console scrollback) — so it needed its own wiring of the same neutralizer.

Intervention content is LLM-derived / untrusted: ``ask_user`` ``prompt`` /
``suggestions`` come straight from a model tool-call, and permission prompts
interpolate a model-controlled ``path``. This suite pins the Security
invariant: a terminal control/ESC/BEL/NUL sequence embedded in ``iv.prompt``
/ ``iv.detail`` / ``iv.suggestions`` / a choice ``label`` must not reach the
``_render_prompt`` output (the string handed to prompt_toolkit's
``_read_line``) — the payload text around the control bytes must still be
present, only the control bytes stripped. Falsify: drop the neutralizer call
in ``StdinInterventionBus._render_prompt`` → these go RED (verified below by
cp-scratch removal, restored via cp — never ``git checkout``).

Real ``UserIntervention`` / ``InterventionChoice`` instances throughout; no
mocks. Behavioral asserts only (control bytes absent / payload substring
present) — no whitespace/format pins on the rest of the rendered string.
"""
from __future__ import annotations

from reyn.user_intervention import (
    InterventionChoice,
    StdinInterventionBus,
    UserIntervention,
)

# A terminal control/ESC injection payload: ESC + CSI red SGR, a bell, and a NUL.
ESC = "\x1b[31mINJECT\x1b[0m\x07\x00"
CONTROL_BYTES = ("\x1b", "\x07", "\x00")


def test_render_prompt_neutralizes_prompt_and_detail() -> None:
    """Tier 2a: iv.prompt / iv.detail control bytes are stripped; payload text survives."""
    iv = UserIntervention(
        kind="ask_user",
        prompt=f"question {ESC} here",
        detail=f"detail {ESC} context",
    )
    rendered = StdinInterventionBus._render_prompt(iv)

    for byte in CONTROL_BYTES:
        assert byte not in rendered
    assert "question" in rendered and "here" in rendered
    assert "detail" in rendered and "context" in rendered
    assert "INJECT" in rendered  # payload text (non-control) survives


def test_render_prompt_neutralizes_suggestions() -> None:
    """Tier 2a: iv.suggestions entries are neutralized individually."""
    iv = UserIntervention(
        kind="ask_user",
        prompt="pick one",
        suggestions=[f"opt-a {ESC}", "opt-b"],
    )
    rendered = StdinInterventionBus._render_prompt(iv)

    for byte in CONTROL_BYTES:
        assert byte not in rendered
    assert "opt-a" in rendered
    assert "opt-b" in rendered


def test_render_prompt_neutralizes_choice_labels() -> None:
    """Tier 2a: Choice ``label`` text is neutralized; ``hotkey``/``id`` (match
    keys, never rendered) are untouched by the neutralizer — only the
    displayed label is."""
    choices = [
        InterventionChoice(id="yes", label=f"[Y]es {ESC}", hotkey="Y"),
        InterventionChoice(id="no", label="[N]o", hotkey="N"),
    ]
    iv = UserIntervention(kind="permission.file_write", prompt="allow?", choices=choices)
    rendered = StdinInterventionBus._render_prompt(iv)

    for byte in CONTROL_BYTES:
        assert byte not in rendered
    assert "[Y]es" in rendered
    assert "[N]o" in rendered
    # match keys are untouched (not part of the rendered guard's concern)
    assert choices[0].hotkey == "Y"
    assert choices[0].id == "yes"


def test_render_prompt_actor_prefix_and_affordance_unchanged() -> None:
    """Tier 2a: The OS-controlled ``[actor]`` prefix and ``> `` affordance need
    no guard and are not corrupted by the neutralization of the untrusted
    leaves."""
    iv = UserIntervention(kind="ask_user", prompt=f"q {ESC}", actor="agent-1")
    rendered = StdinInterventionBus._render_prompt(iv)

    assert rendered.startswith("[agent-1] q ")
    assert rendered.rstrip("\n").endswith("> ")
