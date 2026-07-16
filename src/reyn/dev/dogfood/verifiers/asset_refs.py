"""Dogfood scenario asset-reference contract check (#2965).

``dogfood/scenarios/*.yaml`` names skills (``expected_skill:`` / an
artifact assertion's ``skill:`` key) and event kinds (``expected.events.
must_emit`` / ``must_not_emit`` / ``must_emit_any`` / ``sequence``) as bare
strings. Nothing at scenario-load time checks those strings against
anything real — dogfood does not run in CI (it calls an LLM), so a
reference that goes stale between one bulk deletion and the next stays
silently dead until a human happens to run that scenario file by hand.
This module is the structural check that closes that gap in CI, without
needing to run dogfood itself.

**Ground truth is derived, never grep-listed.** Two registries:

- ``known_skills()`` — ``reyn.builtin.registry.BUILTIN_SKILLS`` (the
  code-shipped skill tier) union any ``skills.entries`` declared in this
  repo's own ``reyn.yaml`` (the operator-declared tier for THIS repo's
  own dogfood runs). Whatever skill names exist for a real run of this
  repo's dogfood suite, live here.
- ``known_event_types()`` — every string literal passed as the ``type``
  (positional or ``type=``/``kind=`` keyword) argument to any method
  whose name contains ``emit`` anywhere under ``src/reyn``, found via an
  ``ast`` walk of every module (not a text grep, and not sampled: a
  literal event-kind string cannot be missed by mis-spelling or by
  reading only the first few hits — see #2965's postmortem on why a
  spelling-sensitive text grep is not a safe substitute for this). There
  is no single closed "all event kinds" enum in this codebase (the
  ``EVENT_AUDIT_REQUIREMENTS`` dict in ``core/events/event_schema.py`` is
  a curated subset for audit-completeness tests, not exhaustive), so the
  AST walk over every actual emit call site IS the registry — a scenario
  referencing an event kind absent from this set is asserting on
  something the current runtime provably never produces.

``find_violations()`` walks every ``dogfood/scenarios/*.yaml`` file and
returns one :class:`ScenarioRefViolation` per reference that resolves to
neither registry. The corpus had known violations at the time this check
was written (#2965 part 1 — reconstructing each one's *intent* needs a
human judgement call between delete / re-point / drop, which this module
does not make); those are tracked in ``KNOWN_PENDING_VIOLATIONS`` in
``tests/test_dogfood_scenario_asset_refs_contract.py`` so the gate is
green today while still hard-failing on any *new* dead reference.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SRC_ROOT = _REPO_ROOT / "src" / "reyn"
_SCENARIOS_DIR = _REPO_ROOT / "dogfood" / "scenarios"
_REYN_YAML = _REPO_ROOT / "reyn.yaml"


@dataclass(frozen=True)
class ScenarioRefViolation:
    """One scenario reference that resolves to neither registry.

    ``kind`` is ``"skill"`` or ``"event"``; ``value`` is the referenced
    name; ``file``/``scenario_id`` locate it in the corpus.
    """
    file: str
    scenario_id: str
    kind: str
    value: str


# ---------------------------------------------------------------------------
# Registries (ground truth, derived — not grep-listed)
# ---------------------------------------------------------------------------


def known_skills() -> frozenset[str]:
    """Every skill name resolvable for a real run of this repo's dogfood suite.

    ``BUILTIN_SKILLS`` (code-shipped tier, always present) union this
    repo's own ``reyn.yaml`` ``skills.entries`` (operator tier), mirroring
    ``reyn.config.loader.load_config``'s two lowest tiers without
    depending on the full loader (which wants a live project context this
    static check does not have).
    """
    from reyn.builtin.registry import BUILTIN_SKILLS

    names = set(BUILTIN_SKILLS.keys())
    if _REYN_YAML.exists():
        doc = yaml.safe_load(_REYN_YAML.read_text(encoding="utf-8")) or {}
        entries = ((doc.get("skills") or {}).get("entries")) or {}
        if isinstance(entries, dict):
            names.update(entries.keys())
    return frozenset(names)


def known_op_kinds() -> frozenset[str]:
    """Every Control IR op kind — the ``OP_KIND_MODEL_MAP`` union.

    Not currently cross-referenced by :func:`find_violations` (no
    scenario field carries a bare op-kind string today — ``covers:``
    tags use a separate free-form taxonomy, see module docstring), but
    exposed for a future field that does, and so this module has a
    single answer to "what op kinds exist" rather than each caller
    re-deriving it.
    """
    from reyn.schemas.models import ALL_OP_KINDS

    return ALL_OP_KINDS


def known_event_types() -> frozenset[str]:
    """Every event-kind string literal passed to an ``*emit*`` call in ``src/reyn``.

    AST walk (``ast.parse`` + ``ast.walk``), not a text grep: every ``.py``
    file under ``src/reyn`` is parsed, and every ``Call`` node whose
    callee name contains ``"emit"`` contributes its string-literal
    positional args and its ``type=``/``kind=`` keyword args. This over-
    includes a handful of non-event string literals that happen to share
    a call site with an ``emit*``-named method (harmless: it only makes
    the registry more permissive, it can never hide a genuinely dead
    reference), and under-includes nothing reachable by static string
    literal — the two failure modes are asymmetric on purpose given this
    is a "is X reachable at all" gate, not an exhaustive event catalogue.
    """
    found: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if not name or "emit" not in name.lower():
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
            for kw in node.keywords:
                if kw.arg in ("type", "kind") and isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    found.add(kw.value.value)
    return frozenset(found)


# ---------------------------------------------------------------------------
# Scenario reference extraction
# ---------------------------------------------------------------------------


def _iter_event_type_refs(expected: dict[str, Any]) -> list[str]:
    events = expected.get("events")
    if not isinstance(events, dict):
        return []
    refs: list[str] = []
    for bucket in ("must_emit", "must_not_emit", "must_emit_any"):
        for item in events.get(bucket) or []:
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                refs.append(item["type"])
    for item in events.get("sequence") or []:
        if isinstance(item, str):
            refs.append(item)
    return refs


def _iter_skill_refs(raw_scenario: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    expected_skill = raw_scenario.get("expected_skill")
    if isinstance(expected_skill, str):
        refs.append(expected_skill)
    artifacts = expected.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict) and isinstance(item.get("skill"), str):
                refs.append(item["skill"])
    return refs


def _iter_scenario_files(scenarios_dir: Path) -> list[Path]:
    return sorted(scenarios_dir.glob("*.yaml"))


def scanned_scenario_ids(scenarios_dir: Path | None = None) -> list[tuple[str, str]]:
    """Return ``(file, scenario_id)`` for every scenario :func:`find_violations` inspects.

    Exists so a test can assert the scan actually reached a non-empty
    corpus. Without it, a wrong ``_SCENARIOS_DIR`` would make
    ``find_violations`` glob zero files, report zero violations, and go
    GREEN while checking nothing — the same always-verifies-empty shape
    as #2959's artifact verifier. The registry-side failure mode is
    fail-loud by contrast (an empty registry flags EVERY reference), so
    only the corpus side needs this witness.
    """
    ids: list[tuple[str, str]] = []
    for path in _iter_scenario_files(scenarios_dir or _SCENARIOS_DIR):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for raw in doc.get("scenarios") or []:
            if isinstance(raw, dict):
                ids.append((path.name, str(raw.get("id", "<unknown>"))))
    return ids


def find_violations(scenarios_dir: Path | None = None) -> list[ScenarioRefViolation]:
    """Return every scenario skill/event reference absent from both registries.

    Scans every ``<scenarios_dir>/*.yaml`` file (default: this repo's
    ``dogfood/scenarios/``) directly via ``yaml.safe_load`` (not through
    ``load_scenario_set``) because the legacy G4-spike scenario format
    (``expected_skill:``, no ``type:`` key) and the ``skill:`` key on an
    artifact assertion (accepted but silently dropped by
    ``ArtifactAssertion``, since it has no ``skill`` field) both carry
    references this check needs that the typed loader does not preserve.

    ``scenarios_dir`` is overridable so a test can point this at a
    synthetic fixture directory and prove the detector fires, without
    monkeypatching module internals.
    """
    skills = known_skills()
    events = known_event_types()
    violations: list[ScenarioRefViolation] = []

    for path in _iter_scenario_files(scenarios_dir or _SCENARIOS_DIR):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_scenarios = doc.get("scenarios")
        if not isinstance(raw_scenarios, list):
            continue
        for raw in raw_scenarios:
            if not isinstance(raw, dict):
                continue
            scenario_id = str(raw.get("id", "<unknown>"))
            expected = raw.get("expected")
            expected = expected if isinstance(expected, dict) else {}

            for event_type in _iter_event_type_refs(expected):
                if event_type not in events:
                    violations.append(
                        ScenarioRefViolation(path.name, scenario_id, "event", event_type)
                    )
            for skill_name in _iter_skill_refs(raw, expected):
                if skill_name not in skills:
                    violations.append(
                        ScenarioRefViolation(path.name, scenario_id, "skill", skill_name)
                    )
    return violations
