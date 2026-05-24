"""Tier 1: ``reyn.chainlit_app.intervention.build_intervention_prompt``.

The chainlit drain loop intercepts ``kind="intervention"`` outbox
messages and feeds the meta payload to this helper before deciding
which ``cl.Ask*Message`` to send. Pins:

1. closed-set IVs (= ``meta.choices`` present) build
   ``InterventionPrompt(choices=non-empty)`` so the caller renders
   an AskActionMessage.
2. free-text IVs build empty-choices prompts → AskUserMessage.
3. ``run_id`` round-trips so the caller can call
   ``session.answer_pending_intervention(run_id, ...)``.
4. body assembly: ``prompt`` + ``detail`` + ``suggestions`` are
   concatenated in fixed order; ``text`` (= OutboxMessage body) is
   the fallback when ``prompt`` meta is missing.
5. Malformed shapes (= non-string run_id, choices without id,
   missing meta entirely) degrade gracefully — no exceptions reach
   the drain loop.
"""
from __future__ import annotations

from reyn.chainlit_app.intervention import (
    InterventionPrompt,
    _ChoiceSpec,
    build_intervention_prompt,
)


def test_free_text_meta_builds_empty_choices_prompt():
    """Tier 1: meta with no choices → InterventionPrompt with empty
    choices tuple (= caller picks AskUserMessage)."""
    out = build_intervention_prompt(
        {"run_id": "r1", "prompt": "Pick a name?"},
    )
    assert out.run_id == "r1"
    assert out.content == "Pick a name?"
    assert out.choices == ()
    assert out.is_choice is False


def test_choice_meta_builds_non_empty_choices_prompt():
    """Tier 1: meta with choices → choices tuple populated (= caller
    picks AskActionMessage)."""
    out = build_intervention_prompt(
        {
            "run_id": "r2",
            "prompt": "Allow this?",
            "choices": [
                {"id": "allow", "label": "[A]llow"},
                {"id": "block", "label": "[B]lock"},
            ],
        },
    )
    assert out.run_id == "r2"
    assert out.is_choice is True
    assert out.choices == (
        _ChoiceSpec(choice_id="allow", label="[A]llow"),
        _ChoiceSpec(choice_id="block", label="[B]lock"),
    )


def test_choice_missing_id_dropped_silently():
    """Tier 1: malformed choice entries (no id) skip — don't crash."""
    out = build_intervention_prompt(
        {
            "run_id": "r3",
            "prompt": "x?",
            "choices": [
                {"id": "yes", "label": "Yes"},
                {"label": "Missing id"},  # dropped
                "not a dict",  # dropped
                {"id": "", "label": "Empty id"},  # dropped (empty id)
            ],
        },
    )
    assert [c.choice_id for c in out.choices] == ["yes"]


def test_choice_label_falls_back_to_id():
    """Tier 1: a choice without ``label`` uses the id as the button text
    (= avoids a blank button)."""
    out = build_intervention_prompt(
        {
            "run_id": "r4",
            "prompt": "?",
            "choices": [{"id": "ok"}],
        },
    )
    assert out.choices[0].label == "ok"


def test_body_assembly_prompt_then_detail_then_suggestions():
    """Tier 1: rendered content concatenates prompt + detail +
    suggestions in stable order."""
    out = build_intervention_prompt(
        {
            "run_id": "r5",
            "prompt": "Pick a file",
            "detail": "stored under ./drafts/",
            "suggestions": ["a.md", "b.md", "c.md"],
        },
    )
    assert out.content == (
        "Pick a file\n"
        "stored under ./drafts/\n"
        "Examples: a.md / b.md / c.md"
    )


def test_text_used_as_body_fallback_when_prompt_meta_missing():
    """Tier 1: missing ``meta.prompt`` → fall back to OutboxMessage
    text body (= the announce-side multiline string)."""
    out = build_intervention_prompt(
        {"run_id": "r6"},
        text="Fallback body line",
    )
    assert out.content == "Fallback body line"


def test_empty_meta_returns_placeholder_content():
    """Tier 1: completely empty meta + empty text → visible placeholder
    so the renderer doesn't try to draw an empty bubble."""
    out = build_intervention_prompt({}, text="")
    assert out.run_id is None
    assert out.content == "(empty prompt)"
    assert out.choices == ()


def test_none_meta_handled_safely():
    """Tier 1: defensive — None meta passes through without exception."""
    out = build_intervention_prompt(None, text="hello")
    assert out.run_id is None
    assert out.content == "hello"


def test_run_id_non_string_dropped():
    """Tier 1: malformed run_id (= not a string) → None, caller skips
    answer-back."""
    out = build_intervention_prompt(
        {"run_id": 12345, "prompt": "p"},
    )
    assert out.run_id is None


def test_suggestions_non_list_ignored():
    """Tier 1: ``suggestions`` of unexpected shape doesn't blow up."""
    out = build_intervention_prompt(
        {"run_id": "r7", "prompt": "p", "suggestions": "not-a-list"},
    )
    # "Examples:" line should NOT appear.
    assert "Examples:" not in out.content
    assert out.content == "p"


def test_returns_intervention_prompt_dataclass():
    """Tier 1: return value is the public dataclass (= caller can read
    .run_id / .content / .choices / .is_choice without dict gymnastics)."""
    out = build_intervention_prompt({"run_id": "r", "prompt": "x"})
    assert isinstance(out, InterventionPrompt)
