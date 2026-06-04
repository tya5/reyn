"""Tier 2: OS/skill invariant — swe_bench apply DETERMINISTICALLY drops a
not-locatable edit (region count 0) from the actionable plan and records it in
``not_locatable`` (#1216, #1209 follow-up).

Primary evidence backing (#1216 run2): #1209 PR-B scaffolds each edit's region
(``_edit_regions``, count 0 = anchor not found). The apply instruction told the
model to *skip* a count-0 edit — but that is compliance-dependent: the model
ignored it and blind-edited from the non-existent anchor (0/4, empty patch). The
fix makes the drop **deterministic** (preprocessor-side) so the model has no
anchored region to blind-edit from — structural close, not model instruction-
following (the #1209-family ``deterministic_split`` care boundary).

Two layers are pinned:
  (a) **behavioral-deterministic** — running the REAL apply preprocessor (the
      enforced permission path, real PermissionResolver, no LLM) over a mix of a
      locatable and a not-locatable edit drops the not-locatable one from
      ``edits`` and records it in ``not_locatable``; the locatable one stays.
      This exercises the count-0 partition end-to-end and confirms the
      ``into: data`` write of both outputs.
  (b) **text-presence (non-vacuous)** — apply.md Step 1 reflects the
      deterministic drop and skill.md declares the drop_not_locatable step.

No mocks. No private-state assertions. (a) drives the real executor over a
throwaway workspace; (b) reads the on-disk skill files.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.events.events import EventLog
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.workspace.workspace import Workspace

SWE_BENCH_DIR = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)
_APPLY_MD = SWE_BENCH_DIR / "phases" / "apply.md"
_SKILL_MD = SWE_BENCH_DIR / "skill.md"


def _run_apply_preprocessor(tmp_path: Path, file_body: str, edits: list[dict]) -> dict:
    """Run the apply preprocessor through the REAL enforced permission path.

    Real PermissionResolver (config-approving, non-interactive) — NOT
    permission_resolver=None — so the python steps go through ``require_python``;
    the test fails if a step's skill.md declaration is removed.
    """
    from reyn.sandbox import NoopBackend

    skill = load_dsl_skill(SWE_BENCH_DIR / "skill.md")
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"python.safe": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    ws = Workspace(events=events, base_dir=tmp_path, permission_resolver=resolver)
    ws.write_file("pkg/mod.py", file_body)

    artifact = {
        "type": "plan",
        "data": {
            "instance_id": "x__y-1",
            "edits": edits,
            "rationale": "r",
            "attempt": 1,
        },
    }
    executor = PreprocessorExecutor(
        skill=skill,
        workspace=ws,
        model="standard",
        events=events,
        subscribers=[],
        resolver=None,
        permission_resolver=resolver,
        sandbox_backend=NoopBackend(),
    )
    result, _usage = asyncio.run(
        executor.run(skill.phases["apply"], artifact, output_language=None)
    )
    return result["data"]


def _big_body(unique_line: str) -> str:
    head = "".join(f"# filler line {i}\n" for i in range(300))
    tail = "".join(f"# trailer line {i}\n" for i in range(300))
    return head + unique_line + "\n" + tail


# ── (a) behavioral-deterministic: count-0 dropped+recorded, count≥1 kept ─────


def test_apply_preprocessor_drops_and_records_not_locatable(tmp_path: Path) -> None:
    """Tier 2: a count-0 edit is dropped from ``edits`` + recorded in ``not_locatable``."""
    data = _run_apply_preprocessor(
        tmp_path,
        _big_body("    value = compute()  # REAL-ANCHOR-XYZ"),
        [
            {"file": "pkg/mod.py", "description": "locatable", "anchor": "REAL-ANCHOR-XYZ"},
            {"file": "pkg/mod.py", "description": "missing", "anchor": "NONEXISTENT-ANCHOR-QQQ"},
        ],
    )

    # The locatable edit (count >= 1) stays in the actionable plan.
    actionable = data["edits"]
    assert [e["anchor"] for e in actionable] == ["REAL-ANCHOR-XYZ"], (
        f"only the locatable edit should remain actionable; got {actionable}"
    )

    # The not-locatable edit (count 0) is dropped from edits AND recorded.
    not_locatable = data.get("not_locatable") or []
    assert [e["anchor"] for e in not_locatable] == ["NONEXISTENT-ANCHOR-QQQ"], (
        f"the count-0 edit must be recorded in not_locatable; got {not_locatable}"
    )

    # Regions are preserved (the drop partitions edits, it does not discard the
    # grep evidence): both entries still present, count 1 then count 0.
    regions = data["_edit_regions"]
    assert regions[0]["count"] >= 1 and regions[1]["count"] == 0


def test_apply_preprocessor_all_locatable_keeps_all(tmp_path: Path) -> None:
    """Tier 2: when every edit is locatable, none is dropped and not_locatable is empty."""
    data = _run_apply_preprocessor(
        tmp_path,
        _big_body("    value = compute()  # REAL-ANCHOR-XYZ"),
        [{"file": "pkg/mod.py", "description": "locatable", "anchor": "REAL-ANCHOR-XYZ"}],
    )
    assert [e["anchor"] for e in data["edits"]] == ["REAL-ANCHOR-XYZ"]
    assert (data.get("not_locatable") or []) == []


# ── (b) text-presence invariant (non-vacuous) ────────────────────────────────


def test_apply_md_reflects_deterministic_drop() -> None:
    """Tier 2: apply.md Step 1 states not-locatable edits are already removed."""
    text = _APPLY_MD.read_text(encoding="utf-8").lower()
    assert "not_locatable" in text, "apply.md must reference the not_locatable record."
    assert "removed" in text and "count" in text, (
        "apply.md Step 1 must state that count-0 (not-locatable) edits are already "
        "removed by the OS (deterministic), not left to the model to skip."
    )


def test_skill_md_declares_drop_not_locatable_step() -> None:
    """Tier 2: skill.md python permissions declare the drop_not_locatable preprocessor."""
    text = _SKILL_MD.read_text(encoding="utf-8")
    assert "drop_not_locatable" in text, (
        "skill.md must declare ./drop_not_locatable.py (a real PermissionResolver "
        "would reject the undeclared safe-mode python step at apply preprocessing)."
    )
