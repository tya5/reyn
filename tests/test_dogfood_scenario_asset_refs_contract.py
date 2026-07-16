"""Tier 1: dogfood scenario skill/event references resolve against live registries (#2965).

dogfood does not run in CI (it calls an LLM), so a scenario referencing a
skill or event kind that a bulk deletion later removed stays silently dead
until a human happens to run that exact scenario file — as happened for 13
days after #2434/#2438 deleted the entire stdlib-skill/phase-engine
machinery (13 skills, the ``invoke_skill``/``run_skill`` op path, and the
``skill_run_spawned``/``skill_run_completed``/``lint_completed`` event
kinds) while 9+ live scenario references to that machinery were left
behind. ``reyn.dev.dogfood.verifiers.asset_refs.find_violations`` closes
that gap: it re-derives "what skills/event-kinds actually exist" from the
real registries (``BUILTIN_SKILLS`` + this repo's ``reyn.yaml``; every
``*emit*`` call site under ``src/reyn``, via ``ast``) and cross-references
every scenario's ``expected_skill:`` / artifact ``skill:`` / event
assertions against them — see that module's docstring for why this is
derived rather than a name list some future deletion could just as easily
outrun again.

``KNOWN_PENDING_VIOLATIONS`` is the exception list for references that
were ALREADY dead when this gate was written (#2965 part 1's own
findings) — reconstructing each one's original intent needs a human
judgement call (delete the scenario / re-point to one of the 2 skills
that still exist / drop the assertion) that this gate does not make.
Remove an entry the moment its scenario is fixed; do not add a new entry
here to silence a violation this gate just caught — that defeats the gate.
"""
from __future__ import annotations

from reyn.dev.dogfood.verifiers.asset_refs import (
    ScenarioRefViolation,
    find_violations,
    known_event_types,
    known_skills,
    scanned_scenario_ids,
)

# (file, scenario_id, kind, value) — see module docstring. Every one of
# these pre-dates this gate; #2965 part 1 investigated each but did not
# resolve them (delete/re-point/drop needs an owner call).
KNOWN_PENDING_VIOLATIONS: frozenset[tuple[str, str, str, str]] = frozenset({
    # chat_router_smoke.yaml IS the main dogfood smoke set (#2965).
    ("chat_router_smoke.yaml", "explicit_skill_invocation_word_stats", "event", "skill_run_spawned"),
    ("chat_router_smoke.yaml", "explicit_skill_invocation_word_stats", "event", "skill_run_completed"),
    ("chat_router_smoke.yaml", "explicit_skill_invocation_word_stats", "skill", "word_stats_demo"),
    ("chat_router_smoke.yaml", "catalog_routing_decided_emitted", "skill", "direct_llm"),
    # control_ir_ops.yaml: #2958 removed the judge_output_direct sibling
    # (same class); these two were still there.
    ("control_ir_ops.yaml", "lint_a_skill", "event", "lint_completed"),
    ("control_ir_ops.yaml", "ask_user_round_trip", "event", "skill_run_spawned"),
    ("multi_agent_and_mcp.yaml", "mcp_search_registry", "event", "skill_run_spawned"),
    # permissions_enforcement.yaml: "write_file" was never a real event
    # kind (writes emit tool_executed with op="write_file", a payload
    # field, not a type) — a must_not_emit on it is a vacuous no-op, not
    # a #2434/#2438 casualty, but still a dead reference this gate catches.
    ("permissions_enforcement.yaml", "realignment_approvals_yaml_write_denied", "event", "write_file"),
    ("permissions_enforcement.yaml", "file_write_outside_cwd_denied", "event", "write_file"),
    # fp_0011_narration.yaml / fp_0011_0012_retest.yaml: frozen point-in-
    # time spike/retest records for already-landed FP-0011/FP-0012,
    # driven by scripts/dogfood_g4_spike.py against a since-merged spike
    # branch — not part of the FP-0036 live-rerun corpus, but they still
    # sit in dogfood/scenarios/ referencing the same deleted skills.
    ("fp_0011_narration.yaml", "narr-1-mcp-search", "skill", "mcp_search"),
    ("fp_0011_narration.yaml", "narr-2-eval-numeric", "skill", "eval"),
    ("fp_0011_narration.yaml", "narr-3-skill-builder", "skill", "skill_builder"),
    ("fp_0011_narration.yaml", "narr-4-skill-improver", "skill", "skill_improver"),
    ("fp_0011_0012_retest.yaml", "s-fp11-1-builder-invalid-spec", "skill", "skill_builder"),
    ("fp_0011_0012_retest.yaml", "s-fp11-2-eval-missing-target", "skill", "eval"),
    ("fp_0011_0012_retest.yaml", "s-fp11-3-mcp-search-empty", "skill", "mcp_search"),
    ("fp_0011_0012_retest.yaml", "s-fp12-spawn-1-builder-success-ack", "skill", "skill_builder"),
    ("fp_0011_0012_retest.yaml", "s-fp12-completion-1-mcp-search-narrate", "skill", "mcp_search"),
    ("fp_0011_0012_retest.yaml", "s-fp12-completion-2-error-narrate", "skill", "skill_builder"),
})


def _key(v: ScenarioRefViolation) -> tuple[str, str, str, str]:
    return (v.file, v.scenario_id, v.kind, v.value)


def test_no_new_dead_scenario_asset_references() -> None:
    """Tier 1: no scenario references a skill/event absent from both registries, beyond the tracked #2965 exceptions."""
    violations = find_violations()
    unexpected = [v for v in violations if _key(v) not in KNOWN_PENDING_VIOLATIONS]
    assert not unexpected, (
        "New dead scenario skill/event reference(s) — not in #2965's tracked "
        f"exception list (add the fix, not a new exception): {unexpected}"
    )


def test_known_pending_violations_list_has_no_stale_entries() -> None:
    """Tier 1: every tracked #2965 exception still reproduces as a real violation.

    Prevents allowlist rot: if a scenario is fixed without removing its
    entry here, a later unrelated regression at the exact same (file,
    scenario_id, kind, value) tuple would be silently swallowed by the
    stale entry instead of failing the gate.
    """
    found_keys = {_key(v) for v in find_violations()}
    stale = KNOWN_PENDING_VIOLATIONS - found_keys
    assert not stale, (
        f"Stale #2965 exception entries no longer reproduce (scenario was "
        f"fixed — remove from KNOWN_PENDING_VIOLATIONS): {stale}"
    )


def test_gate_actually_scans_a_non_empty_corpus_against_real_registries() -> None:
    """Tier 1: the scan reaches real scenarios and real registries — not vacuously green on empty inputs.

    The gate's green signal is only meaningful if it inspected something.
    A wrong scenarios dir would glob zero files and report zero
    violations — green while checking nothing (#2959's artifact verifier
    shipped exactly that shape; #2962's seccomp filter was never loaded
    in production while its mechanism test stayed green). Asserts the
    corpus and both registries are non-trivially populated, and that the
    two skills known to ship are the ones found.
    """
    scanned = set(scanned_scenario_ids())
    # Named real scenarios, not a count: an empty/misdirected scan fails
    # this by membership, and it stays meaningful as the corpus grows.
    assert ("chat_router_smoke.yaml", "explicit_skill_invocation_word_stats") in scanned
    assert ("control_ir_ops.yaml", "file_read_via_chat") in scanned
    assert ("permissions_enforcement.yaml", "file_write_outside_cwd_denied") in scanned

    # BUILTIN_SKILLS is the code-shipped tier; these two are what registry.py declares.
    assert known_skills() >= {"reyn_cheat_sheet", "draft_judge_revise"}
    # A handful of event kinds that demonstrably have live emit call sites.
    assert known_event_types() >= {"routing_decided", "tool_executed", "permission_denied"}


def test_find_violations_detects_a_dead_skill_reference(tmp_path) -> None:
    """Tier 1: find_violations() flags a synthetic reference to a skill that does not exist.

    Mechanism-liveness proof (not a golden fixture): drives the real
    detector against a scenario file naming a skill guaranteed absent
    from BUILTIN_SKILLS, proving the gate's detector actually fires
    rather than being green by construction on any input.
    """
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "synthetic.yaml").write_text(
        "type: dogfood_scenario_set\n"
        "name: synthetic\n"
        "scenarios:\n"
        "  - id: broken\n"
        "    input: hi\n"
        "    expected_skill: totally_fake_skill_xyz\n",
        encoding="utf-8",
    )
    violations = find_violations(scenarios_dir=scenario_dir)
    assert any(v.value == "totally_fake_skill_xyz" and v.kind == "skill" for v in violations)


def test_find_violations_detects_a_dead_event_reference(tmp_path) -> None:
    """Tier 1: find_violations() flags a synthetic must_emit on an event kind no code ever emits."""
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "synthetic.yaml").write_text(
        "type: dogfood_scenario_set\n"
        "name: synthetic\n"
        "scenarios:\n"
        "  - id: broken\n"
        "    input: hi\n"
        "    expected:\n"
        "      events:\n"
        "        must_emit:\n"
        "          - { type: totally_fake_event_kind_xyz, count: '>=1' }\n",
        encoding="utf-8",
    )
    violations = find_violations(scenarios_dir=scenario_dir)
    assert any(v.value == "totally_fake_event_kind_xyz" and v.kind == "event" for v in violations)


def test_find_violations_accepts_a_live_reference(tmp_path) -> None:
    """Tier 1: find_violations() does NOT flag a reference to a skill/event that genuinely exists.

    Sibling to the two detects-a-break tests above: proves the gate is
    not merely permissive-to-the-point-of-flagging-everything — a
    reference to a currently-live skill and a currently-live event kind
    both pass clean.
    """
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "synthetic.yaml").write_text(
        "type: dogfood_scenario_set\n"
        "name: synthetic\n"
        "scenarios:\n"
        "  - id: ok\n"
        "    input: hi\n"
        "    expected_skill: reyn_cheat_sheet\n"
        "    expected:\n"
        "      events:\n"
        "        must_emit:\n"
        "          - { type: routing_decided, count: '>=1' }\n",
        encoding="utf-8",
    )
    violations = find_violations(scenarios_dir=scenario_dir)
    assert violations == []
