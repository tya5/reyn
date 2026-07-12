"""Tier 2: OS invariant — proposal 0060 F3b sibling PR: the 2 remaining
curated-5 builtins (Addendum D9.5 #3/#4) that did not ship with the core
spine (#2912) — the `draft_judge_revise` workflow SKILL and the
`status_card` present-view.

Co-vet-style pins:

  1. **Both builtins load with provenance="builtin" and are inert.** The
     skill is `auto_invoke=False` (discoverable, not auto-firing); the
     presentation is invoke-by-name (inherently inert, A3).
  2. **The skill's SKILL.md is well-formed** (parseable YAML frontmatter
     with `name`/`description`) and its body is wheel-reachable via the same
     `read_builtin_body_bytes` bypass #2913/#2914 established for
     `reyn_cheat_sheet` (mirrors `test_2913_builtin_body_wheel_reachable.py`'s
     wheel-layout scenario for this second skill).
  3. **The skill's embedded worked example is D5a-executable**: the fenced
     ```json``` `judge_output` argument set parses AND validates against the
     REAL `JudgeOutputIROp` schema. Falsify: corrupt it (drop `rubric`) ->
     schema validation fails, proving the positive test exercises real
     validation.
  4. **The status_card blueprint passes `validate_blueprint`** (the real
     structural gate) and registers through the real
     `build_presentation_registry` config-entry path. Falsify: corrupt the
     blueprint (unknown component) -> `PresentBlueprintError`.

No mocks: real `BUILTIN_SKILLS`/`BUILTIN_PRESENTATIONS` maps, the real
`JudgeOutputIROp` pydantic model, the real `validate_blueprint` +
`build_presentation_registry`, the real `read_file` op + `OpContext`
(mirrors `test_2913_builtin_body_wheel_reachable.py`'s harness).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml

from reyn.builtin.docs import read_builtin_body_bytes
from reyn.builtin.registry import (
    BUILTIN_PRESENTATIONS,
    BUILTIN_SKILLS,
    build_builtin_config,
)
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.core.present.catalog import PresentBlueprintError, validate_blueprint
from reyn.data.presentations.registry import build_presentation_registry
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp, JudgeOutputIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_SKILL_PATH = Path(BUILTIN_SKILLS["draft_judge_revise"]["path"])


def _skill_body() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def _extract_fenced_block(text: str, lang: str) -> str:
    match = re.search(rf"```{re.escape(lang)}\n(.*?)```", text, re.DOTALL)
    assert match is not None, f"no ```{lang} fenced block found in draft_judge_revise skill"
    return match.group(1)


# ---------------------------------------------------------------------------
# Both builtins: provenance + inert
# ---------------------------------------------------------------------------


def test_draft_judge_revise_skill_ships_builtin_provenance_and_inert() -> None:
    """Tier 2: the draft_judge_revise skill loads with provenance="builtin",
    auto_invoke=False (discoverable, not auto-firing), enabled=True
    (discoverable)."""
    cfg = build_builtin_config()
    entry = cfg["skills"]["entries"]["draft_judge_revise"]
    assert entry["provenance"] == "builtin"
    assert entry["auto_invoke"] is False
    assert entry.get("enabled", True) is True


def test_status_card_presentation_ships_builtin_provenance() -> None:
    """Tier 2: the status_card presentation loads with provenance="builtin"
    -- invoke-by-name is inherently inert (A3), no auto_invoke-shaped field
    exists on a presentation entry to force."""
    cfg = build_builtin_config()
    entry = cfg["presentations"]["entries"]["status_card"]
    assert entry["provenance"] == "builtin"


def test_status_card_discoverable_not_auto_enabled() -> None:
    """Tier 2: the status_card entry is discoverable (enabled defaults True)
    but requires an explicit `present(view="status_card", ...)` call --
    registering a template never self-triggers a render."""
    assert BUILTIN_PRESENTATIONS["status_card"].get("enabled", True) is True


# ---------------------------------------------------------------------------
# draft_judge_revise: well-formed SKILL.md + wheel-reachable body
# ---------------------------------------------------------------------------


def test_skill_frontmatter_is_well_formed() -> None:
    """Tier 2: the SKILL.md frontmatter (between the two `---` fences) is
    valid YAML with the required `name`/`description` keys, matching the
    registered BUILTIN_SKILLS entry name."""
    body = _skill_body()
    match = re.match(r"^---\n(.*?)\n---\n", body, re.DOTALL)
    assert match is not None, "SKILL.md must open with a YAML frontmatter block"
    frontmatter = yaml.safe_load(match.group(1))
    assert frontmatter["name"] == "draft_judge_revise"
    assert isinstance(frontmatter["description"], str) and frontmatter["description"]


def test_skill_body_readable_with_project_root_elsewhere(tmp_path, monkeypatch):
    """Tier 2: simulated wheel layout (mirrors
    test_2913_builtin_body_wheel_reachable.py for the reyn_cheat_sheet skill)
    -- the draft_judge_revise body reads successfully through the real
    read_file op even when project_root has nothing to do with the package's
    on-disk location."""
    monkeypatch.chdir(tmp_path)
    unrelated_root = tmp_path / "unrelated_project"
    unrelated_root.mkdir()
    resolver = PermissionResolver(
        config_permissions={}, project_root=unrelated_root, interactive=False,
    )
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test_skill",
    )

    assert not str(_SKILL_PATH.resolve()).startswith(str(unrelated_root.resolve())), (
        "fixture invariant: the builtin path must genuinely be outside project_root"
    )

    op = FileIROp(kind="file", op="read", path=str(_SKILL_PATH))
    result = asyncio.run(handle(op, ctx))

    assert result["status"] == "ok", result
    assert "draft_judge_revise" in result["content"]
    assert "description:" in result["content"]


def test_body_read_dir_bypass_reaches_the_second_skill_too() -> None:
    """Tier 2: read_builtin_body_bytes (the #2913/#2914 wheel-reachable
    bypass) is not hardcoded to the cheat-sheet path -- it generalizes to
    ANY path under skills/, including this second builtin skill."""
    raw = read_builtin_body_bytes(str(_SKILL_PATH))
    assert raw is not None
    assert b"draft_judge_revise" in raw


# ---------------------------------------------------------------------------
# D5a: the embedded judge_output worked example is executable
# ---------------------------------------------------------------------------


def test_skill_embedded_judge_output_example_validates_against_real_schema() -> None:
    """Tier 2: the fenced ```json``` judge_output argument block embedded in
    the skill parses as JSON AND validates against the REAL JudgeOutputIROp
    pydantic model (D5a: every cheat-sheet-style example is CI-verified
    against the real implementation, not just prose)."""
    json_text = _extract_fenced_block(_skill_body(), "json")
    args = json.loads(json_text)
    op = JudgeOutputIROp(kind="judge_output", **args)
    assert op.data_inline == args["data_inline"]
    assert op.rubric == args["rubric"]
    assert op.threshold == pytest.approx(0.8)
    assert op.on_fail == "continue"


def test_skill_embedded_example_corrupted_fails_schema_validation() -> None:
    """Tier 2: FALSIFY anchor for D5a -- dropping the required `rubric` field
    from the embedded example fails JudgeOutputIROp validation, proving the
    positive test above is exercising real schema validation, not a vacuous
    pass-through."""
    json_text = _extract_fenced_block(_skill_body(), "json")
    args = json.loads(json_text)
    del args["rubric"]
    with pytest.raises(Exception):  # pydantic ValidationError
        JudgeOutputIROp(kind="judge_output", **args)


def test_skill_embedded_example_both_target_and_data_inline_would_fail() -> None:
    """Tier 2: FALSIFY anchor -- supplying BOTH target and data_inline (the
    XOR the real model enforces) is rejected, confirming the embedded
    example's single-source shape is load-bearing, not incidental."""
    json_text = _extract_fenced_block(_skill_body(), "json")
    args = json.loads(json_text)
    args["target"] = "some.dotted.path"
    with pytest.raises(Exception):  # pydantic ValidationError (XOR violated)
        JudgeOutputIROp(kind="judge_output", **args)


# ---------------------------------------------------------------------------
# status_card: validate_blueprint + registry round-trip
# ---------------------------------------------------------------------------


def test_status_card_blueprint_passes_validate_blueprint() -> None:
    """Tier 2: the shipped status_card blueprint passes the REAL structural
    gate (validate_blueprint) -- every component is in the display-only
    catalog and every binding is a JSON Pointer string."""
    blueprint = BUILTIN_PRESENTATIONS["status_card"]["blueprint"]
    nodes = validate_blueprint(blueprint)
    assert [n["component"] for n in nodes] == ["markdown", "keyvalue"]


def test_status_card_blueprint_falsify_corrupted_component_rejected() -> None:
    """Tier 2: FALSIFY anchor -- swapping in a non-catalog component name
    raises PresentBlueprintError, proving the positive test above is
    exercising the real structural gate, not a vacuous pass-through."""
    import copy

    corrupted = copy.deepcopy(BUILTIN_PRESENTATIONS["status_card"]["blueprint"])
    corrupted[0]["component"] = "not_a_real_component"
    with pytest.raises(PresentBlueprintError):
        validate_blueprint(corrupted)


def test_status_card_registers_through_the_real_presentation_registry() -> None:
    """Tier 2: the builtin-tier config shape (build_builtin_config's
    "presentations" block) round-trips through the REAL
    build_presentation_registry config-entry loader (the production
    population path), not just validate_blueprint in isolation."""
    cfg = build_builtin_config()
    registry = build_presentation_registry(cfg["presentations"], strict=True)
    assert registry.has("status_card")
    nodes = registry.get("status_card")
    assert nodes is not None
    assert [n["component"] for n in nodes] == ["markdown", "keyvalue"]
