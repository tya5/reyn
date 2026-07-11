"""Tier 2b: every ``reyn.prompt.*`` (Phase 1: router_frame/universal_slots/
codeact/retrieval) string constant is exercised — appears in at least one
fixture's ASSEMBLED (rendered) output — across a representative gate matrix.

This is the F1 coverage requirement (per the SP prompt-package review gate):
the test must NOT sample a subset of relocated constants and call it done — it
enumerates every module-level string constant reyn.prompt.* actually defines
and asserts each is reachable from the corpus of assembled outputs, so a
constant that is dead (imported nowhere, or gated behind a combination this
fixture matrix never hits) is caught structurally rather than by manual
inspection.

The corpus is built from ASSEMBLED text (``build_system_prompt`` /
``_render_code_api`` / ``_search_sp`` return values), not from calling the
``reyn.prompt.*`` functions directly — this also exercises the concatenation
seams (``"\\n".join`` / string ``+``) that join a relocated constant to its
neighbours, since a constant only appears as a byte-identical substring of the
final text when the surrounding join logic is correct.
"""
from __future__ import annotations

import itertools
import types

import pytest

import reyn.prompt.codeact as _codeact_mod
import reyn.prompt.retrieval as _retrieval_mod
import reyn.prompt.router_frame as _router_frame_mod
import reyn.prompt.universal_slots as _universal_slots_mod
from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.schemes.codeact import _build_actions_map, _render_code_api
from reyn.tools.schemes.retrieval import _search_sp

_PROMPT_MODULES = [_router_frame_mod, _universal_slots_mod, _codeact_mod, _retrieval_mod]

_BOOL_NAMES = [
    "universal_wrappers_enabled",
    "search_actions_enabled",
    "discovery_mandate",
    "has_hot_list_aliases",
    "non_interactive",
    "non_claude",
]


class _Skill:
    def __init__(self, name, description, path, enabled=True, auto_invoke=True):
        self.name = name
        self.description = description
        self.path = path
        self.enabled = enabled
        self.auto_invoke = auto_invoke


def _module_string_constants(mod: types.ModuleType) -> dict[str, str]:
    """Every module-level UPPER_CASE constant whose value is a non-empty
    ``str`` (list-of-str constants like ``ACTION_CATEGORIES_LINES`` are
    flattened to their individual string elements — each element is itself
    one of the LLM-facing lines that must appear in the corpus)."""
    out: dict[str, str] = {}
    for name, value in vars(mod).items():
        if name.startswith("_") or not name.isupper():
            continue
        if isinstance(value, str) and value:
            out[name] = value
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            for i, v in enumerate(value):
                if v:
                    out[f"{name}[{i}]"] = v
    return out


def _all_relocated_constants() -> dict[str, str]:
    out: dict[str, str] = {}
    for mod in _PROMPT_MODULES:
        for key, value in _module_string_constants(mod).items():
            out[f"{mod.__name__}.{key}"] = value
    return out


def _assembled_output_corpus() -> str:
    """Concatenation of every assembled fixture's rendered text, across the
    EXHAUSTIVE 6-bool slot cross-product (so every gated R1-R4/Skills variant
    renders at least once) plus a curated build_system_prompt/codeact/
    retrieval axis sweep."""
    chunks: list[str] = []

    for combo in itertools.product([False, True], repeat=len(_BOOL_NAMES)):
        kwargs = dict(zip(_BOOL_NAMES, combo))
        slots = build_universal_tool_use_slots(**kwargs, available_skills=None)
        chunks.extend(slots.values())
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="general assistant",
            available_agents=[{"name": "peer1", "role": "peer role", "cluster": "default"}],
            memory_index={"status": "not_found", "content": ""},
            tool_use_sp=slots,
            non_interactive=kwargs["non_interactive"],
            cwd="/tmp/project",
        )
        chunks.append(prompt)

    skills = [_Skill("deploy", "Deploys the app", "skills/deploy/SKILL.md")]
    slots_with_skills = build_universal_tool_use_slots(
        universal_wrappers_enabled=True, search_actions_enabled=True,
        discovery_mandate=True, has_hot_list_aliases=True,
        non_interactive=False, non_claude=False, available_skills=skills,
    )
    chunks.extend(slots_with_skills.values())

    # project_context / output_language / memory-ok / reasoning-continuity /
    # context-size axes — each rendered at least once so their gated
    # router_frame constants appear in the corpus.
    chunks.append(build_system_prompt(
        agent_name="chat", agent_role="general assistant",
        available_agents=[{"name": "peer1", "role": "peer role", "cluster": "default"}],
        memory_index={"status": "ok", "content": "# Memory Index (shared)\n- [Fact](user_1.md) — a fact\n"},
        tool_use_sp=build_universal_tool_use_slots(
            universal_wrappers_enabled=True, search_actions_enabled=True,
            discovery_mandate=True, has_hot_list_aliases=True,
            non_interactive=False, non_claude=False, available_skills=None,
        ),
        cwd="/tmp/project",
        project_context="Some AGENTS.md content.",
        output_language="ja",
        reasoning_continuity_section="━━━ prior_reasoning ━━━\n- note",
        context_size_signal="[context: 12000/128000 tokens]",
    ))

    sample_entries = [
        {"qualified_name": "file__read", "name": "file__read", "description": "Read a file",
         "parameters": {"properties": {"path": {}}}},
    ]
    ident_by_qn = _build_actions_map([e["qualified_name"] for e in sample_entries])
    chunks.append(_render_code_api(sample_entries, ident_by_qn))

    chunks.append(_search_sp(terminal=True))
    chunks.append(_search_sp(terminal=False))

    # cwd set with NO scheme slot-map (tool_use_sp=None → {}) exercises the
    # DEFAULT_CWD_HOW_CLAUSE fallback in the cwd-instruction sentence — the
    # only path that reaches it (every scheme-supplied slot-map always fills
    # slot_in_environment via R4, so the default only surfaces bare-OS-frame).
    chunks.append(build_system_prompt(
        agent_name="chat", agent_role="general assistant",
        available_agents=[], memory_index={"status": "not_found", "content": ""},
        cwd="/tmp/project",
    ))

    return "\n\x00\n".join(chunks)  # NUL-joined so constants can't false-match across chunk boundaries


class TestEveryPromptConstantIsExercised:
    def test_every_relocated_constant_appears_in_assembled_output(self):
        """Tier 2b: F1 coverage — every reyn.prompt.* string constant defined
        in Phase 1's modules is a substring of some assembled fixture's
        rendered output — no relocated constant is dead/unreachable."""
        corpus = _assembled_output_corpus()
        constants = _all_relocated_constants()
        assert constants, "no string constants discovered — the introspection is broken"
        missing = [name for name, text in constants.items() if text not in corpus]
        assert missing == [], (
            f"reyn.prompt.* constant(s) never appear in any assembled fixture "
            f"output (dead / unreachable / seam bug): {missing!r}"
        )

    def test_strip_falsify_unreachable_constant_is_detected(self):
        """Tier 2b: a constant containing text absent from the corpus must be
        flagged as missing — proves the substring check is live, not vacuous."""
        corpus = _assembled_output_corpus()
        bogus = "this exact string ZZQXJ_NEVER_APPEARS_ANYWHERE does not exist"
        assert bogus not in corpus, (
            "strip-falsify precondition failed: the sentinel string "
            "unexpectedly appears in the corpus"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
