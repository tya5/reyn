"""Tier 2: OS invariant — proposal 0060 Phase 1 F3a (builtin tier plumbing +
stdlib clean-break abolition), docs/deep-dives/proposals/0060-llm-wielding-foundation.md
Addendum A1/A2/A3/A9.

Co-vet-style pins:

  1. **Builtin provenance is loader-stamped, not install-op-stamped (A9).**
     ``reyn.builtin.registry.build_builtin_config`` stamps
     ``provenance="builtin"`` on every entry it emits — a DIFFERENT seam
     from ``reyn.core.op_runtime.context.provenance_from_ctx`` (the
     install-op seam, which reads ``ctx.turn_origin``). Falsify:
     ``provenance_from_ctx`` must NEVER be able to produce ``"builtin"`` for
     any ``ctx.turn_origin`` value (including unmapped/None) — if it could,
     an LLM-driven install could spoof builtin provenance.
  2. **Inert-by-construction (A3).** A builtin skill entry has ``auto_invoke``
     forced ``False`` regardless of what the source entry declares (so a
     builtin skill can never auto-fire by default); ``enabled`` is left
     whatever the entry declares (default True) — discoverable, not hidden.
     Pipelines/presentations need no such force (invoke-by-name is
     inherently inert).
  3. **F3a ships EMPTY (mechanism only).** ``BUILTIN_SKILLS`` /
     ``BUILTIN_PIPELINES`` / ``BUILTIN_PRESENTATIONS`` are empty in this
     phase; ``build_builtin_config()`` on the shipped registry returns three
     empty ``entries`` dicts — a no-op merge into ``load_config``.
  4. **The builtin tier merges as the LOWEST config tier.** An operator
     entry with the same name as a builtin entry wins (last-tier-wins
     union-merge, same shape ``_merge`` already applies to every other
     config source).
  5. **stdlib clean-break completeness.** No ``stdlib/**/*`` packaging glob,
     no ``docs/reference/stdlib/`` stub pages, no
     ``tests/test_workspace_glob_stdlib_perm.py`` — the packaging glob is
     repurposed to ``builtin/**/*``.

No mocks: real ``build_builtin_config`` / real ``_merge`` / real
``provenance_from_ctx`` / a real ``OpContext`` construction throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import tomllib

import reyn.builtin.registry as builtin_registry
from reyn.builtin.registry import (
    BUILTIN_PIPELINES,
    BUILTIN_PRESENTATIONS,
    BUILTIN_SKILLS,
    build_builtin_config,
)
from reyn.config.loader import _merge
from reyn.core.op_runtime.context import OpContext, provenance_from_ctx

_REPO_ROOT = Path(__file__).parent.parent


class _StubWorkspace:
    """Minimal real-attribute workspace stub — OpContext only reads base_dir."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — passes emit calls through without side effects."""

    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _make_ctx(tmp_path: Path, *, turn_origin: "str | None") -> OpContext:
    """A real OpContext with the field under test set — mirrors
    test_0060_phase1_layer_a.py's construction (no mocks)."""
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

    resolver = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False)
    return OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=_Events(),
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=None,
        turn_origin=turn_origin,
    )

# ---------------------------------------------------------------------------
# F3a's own mechanism-only invariant, updated for F3b (proposal 0060 Phase 2,
# two PRs: #2912 = core spine skill+pipeline; this sibling PR = the remaining
# curated-5 exemplars, draft_judge_revise skill + status_card presentation):
# F3a shipped all three maps EMPTY (mechanism only); F3b populates all three
# — this file no longer asserts any of the three maps stay empty, since that
# was F3a's phase-scoped state, not a permanent one. See
# tests/test_0060_phase2_f3b_builtin_content.py (core spine) and
# tests/test_0060_f3b_sibling_builtins.py (this PR's 2 exemplars) for the
# content-level co-vet pins.
# ---------------------------------------------------------------------------


def test_builtin_presentations_now_populated_by_f3b() -> None:
    """Tier 2: BUILTIN_PRESENTATIONS is populated by this F3b sibling PR (the
    status_card exemplar) — the mechanism-only EMPTY invariant was F3a's
    phase-scoped state, not permanent."""
    assert BUILTIN_PRESENTATIONS != {}
    assert "status_card" in BUILTIN_PRESENTATIONS


def test_build_builtin_config_presentations_entries_populated() -> None:
    """Tier 2: build_builtin_config()'s presentations entries are non-empty
    now that F3b has shipped the status_card exemplar — an operator entry
    still merges through unaffected (non-colliding names coexist)."""
    cfg = build_builtin_config()
    assert cfg["presentations"]["entries"]
    merged = _merge({"skills": {"entries": {"foo": {"path": "x"}}}}, cfg)
    # A distinct, non-colliding operator entry survives the merge unchanged.
    assert merged["skills"]["entries"]["foo"] == {"path": "x"}


# ---------------------------------------------------------------------------
# Builtin provenance stamp (A9) — loader-path seam
# ---------------------------------------------------------------------------


def test_builtin_entry_stamped_provenance_builtin(monkeypatch) -> None:
    """Tier 2: an entry present in the code-shipped BUILTIN_* maps loads with
    provenance="builtin", stamped at the registry-build loader path (A9) —
    exercising the mechanism with a temporary fixture entry (F3a ships the
    real maps empty; this proves the stamping mechanism itself)."""
    monkeypatch.setitem(
        builtin_registry.BUILTIN_SKILLS,
        "fixture_skill",
        {"description": "fixture", "path": "builtin/fixture_skill/SKILL.md"},
    )
    cfg = build_builtin_config()
    entry = cfg["skills"]["entries"]["fixture_skill"]
    assert entry["provenance"] == "builtin"


def test_builtin_skill_entry_is_inert_by_construction(monkeypatch) -> None:
    """Tier 2: A3 — a builtin skill is discoverable (enabled, default True)
    but auto_invoke is forced False regardless of the source declaration —
    it never auto-fires by default."""
    monkeypatch.setitem(
        builtin_registry.BUILTIN_SKILLS,
        "fixture_skill",
        {
            "description": "fixture",
            "path": "builtin/fixture_skill/SKILL.md",
            "auto_invoke": True,  # declared true in source — must be overridden
        },
    )
    cfg = build_builtin_config()
    entry = cfg["skills"]["entries"]["fixture_skill"]
    assert entry.get("enabled", True) is True  # discoverable, default-True path
    assert entry["auto_invoke"] is False  # forced inert regardless of source


def test_builtin_pipeline_and_presentation_entries_stamped_too(monkeypatch) -> None:
    """Tier 2: the stamping mechanism applies uniformly to all three
    part-types the builtin tier populates (pipelines/presentations have no
    auto_invoke field to force — invoke-by-name is inherently inert, A3)."""
    monkeypatch.setitem(
        builtin_registry.BUILTIN_PIPELINES,
        "fixture_pipeline",
        {"path": "builtin/pipelines/fixture.yaml"},
    )
    monkeypatch.setitem(
        builtin_registry.BUILTIN_PRESENTATIONS,
        "fixture_view",
        {"blueprint": {"type": "text", "text": {"literal": "hi"}}},
    )
    cfg = build_builtin_config()
    assert cfg["pipelines"]["entries"]["fixture_pipeline"]["provenance"] == "builtin"
    assert cfg["presentations"]["entries"]["fixture_view"]["provenance"] == "builtin"


def test_builtin_tier_merges_as_lowest_tier_operator_entry_wins() -> None:
    """Tier 2: the builtin tier is the LOWEST config tier — an operator
    entry with the same name wins on collision (last-tier-wins union-merge,
    same as every other config source)."""
    builtin_cfg = {
        "skills": {"entries": {"shared_name": {"path": "builtin/path", "provenance": "builtin"}}},
    }
    operator_cfg = {
        "skills": {"entries": {"shared_name": {"path": "operator/path"}}},
    }
    merged = _merge(builtin_cfg, operator_cfg)
    assert merged["skills"]["entries"]["shared_name"]["path"] == "operator/path"


# ---------------------------------------------------------------------------
# Structural non-spoofability: install-op provenance can never be "builtin"
# ---------------------------------------------------------------------------


def test_install_op_provenance_can_never_be_builtin(tmp_path: Path) -> None:
    """Tier 2: provenance_from_ctx (the install-op seam, A7/A9) only ever
    reads ctx.turn_origin (fail-safe collapsing None to "auto_improvement",
    A7's own value-mapping is tested in test_0060_phase1_layer_a.py) — no
    code path can make it produce "builtin". Falsify: if an unset
    ctx.turn_origin produced "builtin" (instead of the strict
    "auto_improvement" fallback), a bridge-fallback install could carry
    builtin-level provenance without ever going through the loader seam."""
    for turn_origin in (None, "user_directed", "auto_improvement"):
        ctx = _make_ctx(tmp_path, turn_origin=turn_origin)
        result = provenance_from_ctx(ctx)
        assert result in ("user_directed", "auto_improvement")
        assert result != "builtin"


# ---------------------------------------------------------------------------
# stdlib clean-break completeness
# ---------------------------------------------------------------------------


def test_pyproject_package_data_repurposed_to_builtin_glob() -> None:
    """Tier 2: the packaging config ships `builtin/**/*` (Addendum A1/A2) —
    the packaging slot the builtin tier (F3) physically ships through —
    and no dead `stdlib/**/*` remnant remains.

    0061 migrated the build backend setuptools -> Hatchling: the
    equivalent of `[tool.setuptools.package-data]` is now
    `[tool.hatch.build.targets.wheel].artifacts` (see `pyproject.toml` +
    `tests/test_0061_repo_self_access_parity.py`, which further gates
    the built wheel actually contains these files)."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    artifacts = data["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert "src/reyn/builtin/**/*" in artifacts
    assert not any("stdlib" in entry for entry in artifacts)


def test_no_docs_reference_stdlib_stub_pages() -> None:
    """Tier 2: the two stdlib doc-stub pages (Addendum A2) are removed."""
    assert not (_REPO_ROOT / "docs" / "reference" / "stdlib").exists()


def test_no_dead_stdlib_dogfood_scenario() -> None:
    """Tier 2: the stale stdlib_skills_core.yaml dogfood scenario (Addendum
    A2) is removed — it exercised skill names removed in #2104/#2434 and
    could never pass."""
    assert not (_REPO_ROOT / "dogfood" / "scenarios" / "stdlib_skills_core.yaml").exists()


def test_no_stdlib_glob_in_source_or_config() -> None:
    """Tier 2: a targeted completeness check — none of the specific
    reyn-feature stdlib remnants enumerated by Addendum A2 remain: the
    packaging glob, the doc stubs, the stale skills-config scan_dirs
    comment, the misleadingly-named permission test, the dead dogfood
    scenario. (Deliberately narrow — "stdlib" as the English word for
    Python's standard library remains legitimate throughout the codebase;
    this is not a repo-wide string ban.)"""
    root_py = (_REPO_ROOT / "src" / "reyn" / "config" / "root.py").read_text(encoding="utf-8")
    assert 'scan_dirs: ["skills"]' not in root_py
    assert not (_REPO_ROOT / "tests" / "test_workspace_glob_stdlib_perm.py").exists()
    assert (_REPO_ROOT / "tests" / "test_workspace_glob_outside_root_perm.py").exists()
