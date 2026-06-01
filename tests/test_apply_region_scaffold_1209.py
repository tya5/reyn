"""Tier 2: OS/skill invariant — #1209 PR-B apply deterministic edit-region scaffolding.

The apply phase preprocessor places each edit's target region into context BEFORE
the model edits, by grepping the plan's verbatim ``anchor`` (the apply-starvation
fix: the model must never edit a file it cannot see — astropy-13236 fabricated
old_strings for an offloaded 150KB file). This pins:

  - ``escape_anchors`` regex-escapes each anchor (grep compiles a regex);
  - the apply preprocessor (escape → iterate grep) binds per-edit regions at
    ``data._edit_regions``, one entry per edit in plan order, with the grepped
    context; one-match → region present, not-found → count 0 (graceful, no blind
    edit), multi-match → count > 1 (ambiguity surfaced).

Real Workspace + real skill loaded from disk + real op_runtime grep via
PreprocessorExecutor; no collaborator mocks. ``escape_anchors`` is a pure
data-transform tested directly.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.events.events import EventLog
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.workspace.workspace import Workspace

SWE_BENCH_DIR = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


# ── escape_anchors: pure data transform ─────────────────────────────────────

def test_escape_anchors_adds_regex_escaped_field() -> None:
    """Tier 2: each edit gains anchor_re = re.escape(anchor); regex chars neutralized."""
    import re
    import sys

    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from escape_anchors import escape_anchors
    finally:
        sys.path.pop(0)

    anchor = "def f(x): return col.formats[idx](x)  # special (.)*+"
    out = escape_anchors({"edits": [{"file": "a.py", "description": "d", "anchor": anchor}]})
    assert out[0]["anchor_re"] == re.escape(anchor)
    # the escaped form compiles + matches the literal text (regex chars neutralized)
    assert re.compile(out[0]["anchor_re"]).search(f"prefix {anchor} suffix")


def test_escape_anchors_empty_anchor_is_never_match_sentinel() -> None:
    """Tier 2: a missing/empty anchor → the never-match sentinel `(?!)`, NOT "".

    An empty regex matches every line (`re.search("", line)` → pos 0), which would
    wrongly land in the multi-match path; the sentinel yields zero matches so the
    edit is treated as not-locatable (#1214 review).
    """
    import re
    import sys

    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from escape_anchors import escape_anchors
    finally:
        sys.path.pop(0)
    out = escape_anchors({"edits": [{"file": "a.py", "description": "d", "anchor": ""}]})
    sentinel = out[0]["anchor_re"]
    assert sentinel != ""
    # the sentinel compiles and matches NOTHING (so grep returns count 0)
    assert re.search(sentinel, "any code line at all") is None


def test_skill_md_registers_escape_anchors_python_permission() -> None:
    """Tier 2: skill.md declares escape_anchors as a safe python step.

    The apply preprocessor's python step requires an explicit permission entry in
    skill.md frontmatter; without it the OS (with a real PermissionResolver)
    rejects the step at runtime and the apply phase never executes. A
    permission_resolver=None unit harness bypasses this check, so this structural
    pin guards the declared-and-enforced path (#1214 faithful-run gap).
    """
    skill_md = (SWE_BENCH_DIR / "skill.md").read_text(encoding="utf-8")
    assert "escape_anchors.py" in skill_md
    assert "function: escape_anchors" in skill_md
    assert "mode: safe" in skill_md


# ── apply preprocessor: deterministic edit-region scaffolding ───────────────

def _run_apply_preprocessor(tmp_path: Path, file_body: str, edits: list[dict]) -> dict:
    from reyn.sandbox import NoopBackend

    skill = load_dsl_skill(SWE_BENCH_DIR / "skill.md")
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
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
        permission_resolver=None,
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


def test_apply_preprocessor_populates_region_for_unique_anchor(tmp_path: Path) -> None:
    """Tier 2: a unique anchor in a large file → one match + region context in _edit_regions."""
    anchor = "    has_fill_values = hasattr(col, 'fill_values')  # UNIQUE-ANCHOR-XYZ"
    data = _run_apply_preprocessor(
        tmp_path,
        _big_body(anchor),
        [{"file": "pkg/mod.py", "description": "apply col formats", "anchor": anchor}],
    )

    region = data["_edit_regions"][0]  # one entry per edit (empty → IndexError)
    assert region["status"] == "ok"
    assert region["count"] == 1
    # the grepped region carries the anchored line (deterministically in-context)
    blob = str(region.get("matches"))
    assert "UNIQUE-ANCHOR-XYZ" in blob


def test_apply_preprocessor_anchor_not_found_is_graceful(tmp_path: Path) -> None:
    """Tier 2: an anchor absent from the file → count 0, no crash (apply must not blind-edit)."""
    data = _run_apply_preprocessor(
        tmp_path,
        _big_body("    real_line = 1  # ACTUAL"),
        [{"file": "pkg/mod.py", "description": "x", "anchor": "NONEXISTENT-ANCHOR-QQQ"}],
    )
    assert data["_edit_regions"][0]["count"] == 0  # one entry per edit (empty → IndexError)


def test_apply_preprocessor_empty_anchor_is_not_locatable(tmp_path: Path) -> None:
    """Tier 2: an empty anchor → count 0 (NOT match-all), so the edit is not-locatable.

    Guards the #1214 review finding: an empty regex would match every line and
    wrongly look like a multi-match; the never-match sentinel keeps count at 0.
    """
    data = _run_apply_preprocessor(
        tmp_path,
        _big_body("    real_line = 1  # ACTUAL"),
        [{"file": "pkg/mod.py", "description": "x", "anchor": ""}],
    )
    assert data["_edit_regions"][0]["count"] == 0


def test_apply_preprocessor_multi_match_surfaces_count(tmp_path: Path) -> None:
    """Tier 2: a non-unique anchor → count > 1 (ambiguity surfaced for the apply model)."""
    dup = "    x = compute()  # DUP-ANCHOR"
    body = _big_body(dup) + dup + "\n"  # the anchor appears twice
    data = _run_apply_preprocessor(
        tmp_path, body,
        [{"file": "pkg/mod.py", "description": "x", "anchor": dup}],
    )
    assert data["_edit_regions"][0]["count"] >= 2
