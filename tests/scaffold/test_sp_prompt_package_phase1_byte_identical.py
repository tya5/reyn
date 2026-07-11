# scaffold: triggered_by="reyn.prompt package Phase 1 relocation lands (SP agent-facing A-D: router_frame/universal_slots/codeact/retrieval)"
# scaffold: removed_by="The same PR that lands the relocation, once this test is green"
"""Tier 1: byte-identical characterization gate for the SP Phase-1 (agent-facing
A-D) relocation into ``reyn.prompt``.

``tests/scaffold/_sp_phase1_baseline_pre_refactor.json`` was captured by
mechanically calling ``build_universal_tool_use_slots`` (EXHAUSTIVE 64-way
cross-product of its 6 gating booleans), ``build_system_prompt`` (a curated
set toggling each of the cwd/env/memory/project/output-language/
non_interactive/reasoning_continuity/context-size axes at least once, plus
scheme-bool variants), ``_render_code_api``, and ``_search_sp`` — run against
the pre-relocation source tree (the commit this refactor branched from), with
NO manual transcription (the JSON is a raw ``json.dump`` of the live call
results). This test re-runs the identical calls against the CURRENT
(post-relocation) source and asserts byte-for-byte equality against that
captured baseline: any wrong ``"\\n".join`` seam, dropped blank line, or
misrouted gated variant fails this test.

This is scaffolding, not a permanent test: per the extracted-refactor idiom in
``docs/deep-dives/contributing/testing.md`` (Annex: Scaffolding tests), it is
added and removed in the SAME PR that lands the relocation, once green — the
post-relocation code has no independent behavior to keep re-verifying past
that point (the relocation is a one-time mechanical move, not an area that will
keep changing shape).
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.schemes.codeact import _build_actions_map, _render_code_api
from reyn.tools.schemes.retrieval import _search_sp

_BASELINE_PATH = Path(__file__).parent / "_sp_phase1_baseline_pre_refactor.json"
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


def _capture_current() -> dict:
    out: dict[str, str] = {}

    # EXHAUSTIVE 64-way cross-product of the 6 scheme-slot gating booleans.
    for combo in itertools.product([False, True], repeat=len(_BOOL_NAMES)):
        kwargs = dict(zip(_BOOL_NAMES, combo))
        key = "slots:" + ",".join(f"{k}={v}" for k, v in kwargs.items())
        slots = build_universal_tool_use_slots(**kwargs, available_skills=None)
        out[key] = json.dumps(slots, sort_keys=True)

    skills = [_Skill("deploy", "Deploys the app", "skills/deploy/SKILL.md")]
    slots_with_skills = build_universal_tool_use_slots(
        universal_wrappers_enabled=True, search_actions_enabled=True,
        discovery_mandate=True, has_hot_list_aliases=True,
        non_interactive=False, non_claude=False, available_skills=skills,
    )
    out["slots:with_skills"] = json.dumps(slots_with_skills, sort_keys=True)

    base_kwargs = dict(
        universal_wrappers_enabled=True, search_actions_enabled=True,
        discovery_mandate=True, has_hot_list_aliases=True,
        non_interactive=False, non_claude=False,
    )
    env_info = {"date": "2026-07-12", "platform": "Darwin", "os_version": "25.3.0",
                "shell": "/bin/zsh", "is_git_repo": True}
    memory_ok = {"status": "ok", "content": "# Memory Index (shared)\n- [Fact](user_1.md) — a fact\n"}
    memory_none = {"status": "not_found", "content": ""}

    axis_variants = [
        ("default", {}),
        ("cwd_none", {"cwd": None}),
        ("env_info", {"environment_info": env_info}),
        ("memory_ok", {"memory_index": memory_ok}),
        ("project_context", {"project_context": "Some AGENTS.md content."}),
        ("output_language", {"output_language": "ja"}),
        ("non_interactive", {"non_interactive": True}),
        ("reasoning_continuity", {"reasoning_continuity_section": "━━━ prior_reasoning ━━━\n- note"}),
        ("context_size", {"context_size_signal": "[context: 12000/128000 tokens]"}),
        ("wrappers_off", {"_scheme": dict(base_kwargs, universal_wrappers_enabled=False, search_actions_enabled=False)}),
        ("no_discovery_no_hotlist", {"_scheme": dict(base_kwargs, discovery_mandate=False, has_hot_list_aliases=False)}),
        ("non_claude", {"_scheme": dict(base_kwargs, non_claude=True)}),
    ]

    for label, overrides in axis_variants:
        overrides = dict(overrides)
        scheme_kwargs = overrides.pop("_scheme", base_kwargs)
        slots = build_universal_tool_use_slots(**scheme_kwargs, available_skills=None)
        call_kwargs = dict(
            agent_name="chat",
            agent_role="general assistant",
            available_agents=[{"name": "peer1", "role": "peer role", "cluster": "default"}],
            memory_index=memory_none,
            tool_use_sp=slots,
            non_interactive=scheme_kwargs["non_interactive"],
            cwd="/tmp/project",
        )
        call_kwargs.update(overrides)
        out[f"sp:{label}"] = build_system_prompt(**call_kwargs)

    out["sp:bare"] = build_system_prompt(
        agent_name="chat", agent_role="general assistant",
        available_agents=[], memory_index={"status": "not_found", "content": ""},
    )
    out["sp:str_shim"] = build_system_prompt(
        agent_name="chat", agent_role="general assistant",
        available_agents=[], memory_index={"status": "not_found", "content": ""},
        tool_use_sp="a bare replacement string",
    )

    sample_entries = [
        {"qualified_name": "file__read", "name": "file__read", "description": "Read a file",
         "parameters": {"properties": {"path": {}}}},
        {"qualified_name": "exec__run", "name": "exec__run", "description": "Run a shell command",
         "parameters": {"properties": {"argv": {}}}},
    ]
    ident_by_qn = _build_actions_map([e["qualified_name"] for e in sample_entries])
    out["codeact:render"] = _render_code_api(sample_entries, ident_by_qn)
    out["codeact:render_empty"] = _render_code_api([], {})

    out["retrieval:terminal"] = _search_sp(terminal=True)
    out["retrieval:non_terminal"] = _search_sp(terminal=False)

    return out


class TestSPPhase1ByteIdentical:
    def test_current_output_matches_pre_refactor_baseline(self):
        """Tier 1: every fixture's current output equals the captured
        pre-relocation baseline byte-for-byte. Covers the EXHAUSTIVE 6-bool
        slot cross-product (the concatenation seams for R1-R4 + Skills) plus
        curated build_system_prompt/codeact/retrieval axis coverage."""
        baseline = json.loads(_BASELINE_PATH.read_text())
        current = _capture_current()
        assert set(current) == set(baseline), (
            f"fixture key set changed: added={set(current) - set(baseline)!r} "
            f"removed={set(baseline) - set(current)!r}"
        )
        mismatches = [k for k in baseline if baseline[k] != current[k]]
        assert mismatches == [], (
            f"byte-identical relocation VIOLATED for fixtures: {mismatches!r}"
        )

    def test_strip_falsify_one_char_change_is_detected(self):
        """Tier 1: mutating one captured baseline string by 1 char must make
        the equality check fail — proves the comparison is not vacuously true."""
        baseline = json.loads(_BASELINE_PATH.read_text())
        some_key = next(iter(baseline))
        poisoned = dict(baseline)
        poisoned[some_key] = poisoned[some_key] + "X"
        assert poisoned[some_key] != baseline[some_key], (
            "strip-falsify: a 1-char mutation was not detected by direct "
            "string inequality — the fixture harness is not live"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
