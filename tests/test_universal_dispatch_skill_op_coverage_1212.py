"""Tier 2: universal_dispatch coverage guard for the D6 op-shape codemod (#1212 PR3).

Guard invariant (ADR-0035 §"Open items"):
  The ``universal_dispatch`` op-kind→tool-call mapping MUST be total over
  the real op kinds that stdlib skills actually reference.  This test fails
  loudly when a skill uses a real op kind that has no ``{name, arguments}``
  tool_call route in ``universal_dispatch``, so the PR3 codemod cannot
  silently miss an op.

Scope (per D6 correction, 2026-06-02):
  - IN: real op kinds found in ``allowed_ops`` frontmatter and
    preprocessor/postprocessor ``run_op`` step ``op.kind`` literals.
  - OUT: DSL step types ``iterate`` / ``validate`` / ``python`` /
    ``lint_plan`` — these are OS-deterministic, never LLM-emitted,
    and are explicitly out of scope for the D6 codemod.

Coverage contract:
  For every real op kind k used by any stdlib skill, there must exist at
  least one qualified name in ``universal_dispatch._OPERATION_RULES`` (or
  ``_RESOURCE_RULES``) whose category unambiguously covers k.  The explicit
  op_kind → category correspondence is defined in ``_OP_KIND_CATEGORY`` below
  and IS the codemod contract: PR3 will rewrite each op kind to the
  corresponding qualified name(s).

Load strategy: reads skill files from disk via YAML + frontmatter parsing
(real data, no hardcoded copies).

No mocks. No private-state assertions. No magic-number length pins.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# ── Paths ──────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_STDLIB_SKILLS_DIR = _REPO_ROOT / "src" / "reyn" / "stdlib" / "skills"

# ── DSL step types excluded from D6 scope ──────────────────────────────────

_DSL_STEP_TYPES_OUT_OF_SCOPE: frozenset[str] = frozenset(
    {"iterate", "validate", "python", "lint_plan"}
)

# ── Op-kind → category correspondence (the D6 codemod contract) ──────────
#
# Maps each real op kind → the category key in ``universal_dispatch`` that
# provides the ``{name, arguments}`` tool_call route for it.
#
#   - Categories whose key exists in ``_RESOURCE_RULES`` (= resource
#     categories): the value is the resource-rule category key.
#   - Categories whose key exists in ``_OPERATION_RULES`` (= operation
#     categories): the value is the operation-category prefix
#     (= the part before ``__`` in the qualified name).
#
# This dict is the authoritative contract for coverage.  If an op kind
# is missing here, add it when the category is wired (= that wiring IS
# the PR3 / PR4 work).  The test asserts every skill-used op kind has an
# entry here AND that universal_dispatch has at least one qualified name
# for that category.
_OP_KIND_CATEGORY: dict[str, str] = {
    # file ops → "file" category (_OPERATION_RULES: file__read/write/edit/…)
    "file":          "file",
    # run_skill → "skill" resource category (_RESOURCE_RULES: skill → invoke_skill)
    "run_skill":     "skill",
    # web ops → "web" category (_OPERATION_RULES: web__fetch, web__search)
    "web_fetch":     "web",
    "web_search":    "web",
    # lint → "validation" category (_OPERATION_RULES: validation__lint)
    "lint":          "validation",
    # sandboxed_exec → "exec" category (_OPERATION_RULES: exec__sandboxed_exec)
    "sandboxed_exec": "exec",
    # recall → "rag.operation" category (_OPERATION_RULES: rag.operation__recall)
    "recall":        "rag.operation",
    # mcp tool call → "mcp" category (_OPERATION_RULES: mcp__call_tool)
    "mcp":           "mcp",
    # mcp install ops → "mcp" category (_OPERATION_RULES: mcp__install_*)
    "mcp_install":   "mcp",
    # ask_user → NOT YET COVERED (no qualified name in universal_dispatch)
    # embed     → NOT YET COVERED (no qualified name in universal_dispatch)
    # index_write → NOT YET COVERED (no qualified name in universal_dispatch)
    # skill_resolve → NOT YET COVERED (no qualified name in universal_dispatch)
    # (entries above are intentionally absent — see expected-failure comment below)
}


# ── Frontmatter parser (mirrors compiler/parser._split_frontmatter) ───────

def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into (frontmatter dict, body string)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    if end is None:
        return {}, text
    fm: dict[str, Any] = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


# ── Op-kind collectors ─────────────────────────────────────────────────────

def _collect_run_op_kinds_from_steps(steps: list[Any]) -> set[str]:
    """Walk a preprocessor/postprocessor step list and collect run_op op.kind values.

    Handles both flat step lists and nested ``iterate``/``apply`` sub-step
    lists.  Excludes DSL step types that are out of scope (iterate / validate
    / python / lint_plan).  Only ``type: run_op`` steps are considered; the
    real op kind lives in the step's ``op.kind`` field.
    """
    kinds: set[str] = set()
    if not isinstance(steps, list):
        return kinds
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        if step_type in _DSL_STEP_TYPES_OUT_OF_SCOPE:
            # May contain nested apply.steps — recurse into them.
            apply_block = step.get("apply") or {}
            if isinstance(apply_block, dict):
                nested = apply_block.get("steps") or []
                kinds.update(_collect_run_op_kinds_from_steps(nested))
            continue
        if step_type == "run_op":
            op_block = step.get("op")
            if isinstance(op_block, dict):
                kind = op_block.get("kind")
                if isinstance(kind, str) and kind.strip():
                    kinds.add(kind.strip())
    return kinds


def _skill_real_op_kinds(skill_dir: Path) -> dict[str, set[str]]:
    """Return {op_kind: {source_label, ...}} for all real op kinds a skill references.

    Sources:
      1. ``phases/*.md`` → ``allowed_ops`` frontmatter list (real op kinds only,
         validated against ALL_OP_KINDS).
      2. ``phases/*.md`` preprocessor step lists for ``type: run_op`` / ``op.kind``.
      3. ``skill.md`` postprocessor step lists for ``type: run_op`` / ``op.kind``.

    The returned dict maps each found op kind to the set of source labels
    (= short paths relative to skill_dir) where it was found.
    """
    # Import here so the test can be collected without a full reyn install; the
    # import is deferred to function body so collection errors surface as test errors.
    from reyn.op_runtime.registry import ALL_OP_KINDS  # noqa: PLC0415

    op_to_sources: dict[str, set[str]] = {}

    def _add(kind: str, label: str) -> None:
        op_to_sources.setdefault(kind, set()).add(label)

    # ── skill.md postprocessor ──────────────────────────────────────────────
    skill_md = skill_dir / "skill.md"
    if skill_md.exists():
        fm, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        post = fm.get("postprocessor") or {}
        if isinstance(post, dict):
            steps = post.get("steps") or []
            for kind in _collect_run_op_kinds_from_steps(steps):
                _add(kind, "skill.md[postprocessor]")

    # ── phases/*.md ─────────────────────────────────────────────────────────
    phases_dir = skill_dir / "phases"
    if phases_dir.is_dir():
        for phase_path in sorted(phases_dir.glob("*.md")):
            fm, _ = _split_frontmatter(phase_path.read_text(encoding="utf-8"))
            label = f"phases/{phase_path.name}"

            # allowed_ops: include only op kinds present in ALL_OP_KINDS.
            # Unknown tokens (e.g. the legacy 'grep' sub-op alias seen in
            # swe_bench/phases/explore.md) are sub-ops of a coarser kind and
            # are not tracked independently here — the parent kind (file) is
            # already covered by other phases.
            ao_raw = fm.get("allowed_ops")
            if isinstance(ao_raw, list):
                for val in ao_raw:
                    if isinstance(val, str) and val.strip() in ALL_OP_KINDS:
                        _add(val.strip(), f"{label}[allowed_ops]")

            # preprocessor run_op steps
            pre_raw = fm.get("preprocessor") or []
            for kind in _collect_run_op_kinds_from_steps(pre_raw):
                _add(kind, f"{label}[preprocessor]")

    return op_to_sources


# ── Test ───────────────────────────────────────────────────────────────────


def test_universal_dispatch_covers_all_stdlib_skill_real_op_kinds() -> None:
    """Tier 2: universal_dispatch map is total over stdlib skills' real op kinds.

    Guard for the ADR-0035 D6 op-shape codemod (#1212 PR3).

    Invariant: every real op kind used in any stdlib skill's ``allowed_ops``
    or preprocessor/postprocessor ``run_op`` step MUST have a ``{name,
    arguments}`` tool_call route in ``universal_dispatch`` (i.e. must appear
    in ``_OP_KIND_CATEGORY`` AND the corresponding category must be wired in
    ``universal_dispatch._OPERATION_RULES`` or ``_RESOURCE_RULES``).

    When this test FAILS it surfaces the exact set of uncovered op kinds and
    which skills use them — the finding is the input for the PR3 codemod.
    """
    from reyn.tools.universal_dispatch import (  # noqa: PLC0415
        _OPERATION_RULES,  # type: ignore[attr-defined]  # tested public-enough: PR3 target
        _RESOURCE_RULES,  # type: ignore[attr-defined]  # tested public-enough: PR3 target
        KNOWN_STATIC_QUALIFIED_NAMES,
    )

    # ── 1. Enumerate real op kinds across all stdlib skills ─────────────────
    skill_op_kinds: dict[str, dict[str, set[str]]] = {}  # skill → {kind: {sources}}
    for skill_dir in sorted(_STDLIB_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        op_to_sources = _skill_real_op_kinds(skill_dir)
        if op_to_sources:
            skill_op_kinds[skill_dir.name] = op_to_sources

    # Aggregate: kind → {skill/source label} across all skills
    all_real_op_kinds: dict[str, list[str]] = {}
    for skill_name, op_map in skill_op_kinds.items():
        for kind, sources in op_map.items():
            label_list = all_real_op_kinds.setdefault(kind, [])
            for src in sorted(sources):
                label_list.append(f"{skill_name}/{src}")

    assert all_real_op_kinds, (
        "No real op kinds found across any stdlib skill — "
        "the scanner may be broken or STDLIB_SKILLS_DIR is wrong"
    )

    # ── 2. Build the set of categories covered by universal_dispatch ────────
    # Categories present in _OPERATION_RULES (= operation categories with explicit routes)
    _OP_RULE_CATEGORIES: set[str] = set()
    for qname in _OPERATION_RULES:
        sep = qname.find("__")
        if sep >= 0:
            _OP_RULE_CATEGORIES.add(qname[:sep])

    # Categories present in _RESOURCE_RULES (= resource categories)
    _RES_RULE_CATEGORIES: set[str] = set(_RESOURCE_RULES.keys())

    _ALL_COVERED_CATEGORIES = _OP_RULE_CATEGORIES | _RES_RULE_CATEGORIES

    # ── 3. Check coverage for each real op kind ─────────────────────────────
    uncovered: dict[str, list[str]] = {}  # kind → [source labels]
    for kind, sources in sorted(all_real_op_kinds.items()):
        category = _OP_KIND_CATEGORY.get(kind)
        if category is None:
            # Op kind has no entry in the codemod contract table at all
            uncovered[kind] = sources
            continue
        if category not in _ALL_COVERED_CATEGORIES:
            # The contract table declares a category but it's not wired yet
            uncovered[kind] = sources

    # ── 4. Assert totality — fail loudly with actionable detail ────────────
    assert uncovered == {}, (
        "universal_dispatch op-kind coverage is NOT total over stdlib skills "
        "(ADR-0035 D6 invariant violated — PR3 codemod would miss these ops).\n\n"
        "Uncovered op kinds (kind → [skill/source]):\n"
        + "\n".join(
            f"  {kind!r}: {sources}"
            for kind, sources in sorted(uncovered.items())
        )
        + "\n\nTo fix: add a qualified-name route for each uncovered kind in "
        "universal_dispatch._OPERATION_RULES (or _RESOURCE_RULES), then add "
        "the kind to _OP_KIND_CATEGORY in this test."
    )


def test_op_kind_category_map_references_known_skills_only() -> None:
    """Tier 2: every op kind in _OP_KIND_CATEGORY is in ALL_OP_KINDS.

    Cross-check: the codemod contract table must not declare a category
    for an op kind that doesn't exist in the OS registry — that would be
    a stale entry silently hiding a missing op kind.
    """
    from reyn.op_runtime.registry import ALL_OP_KINDS  # noqa: PLC0415

    stale = {k for k in _OP_KIND_CATEGORY if k not in ALL_OP_KINDS}
    assert stale == set(), (
        f"_OP_KIND_CATEGORY references op kinds not in ALL_OP_KINDS "
        f"(= stale / mistyped entries): {sorted(stale)}"
    )


def test_skill_real_op_kind_scanner_finds_known_ops() -> None:
    """Tier 2: the scanner returns at least the op kinds confirmed by direct inspection.

    Spot-checks a small subset of op kinds that manual recon confirmed are
    present in the stdlib skills, to guard against silent scanner regressions
    (e.g. YAML parse failure that produces an empty result).

    The specific values are chosen because they come from different sources
    (allowed_ops vs run_op preprocessor) and different skills, so a scanner
    regression would have to break multiple paths to silence them all.
    """
    # Collect across all skills
    all_kinds: set[str] = set()
    for skill_dir in sorted(_STDLIB_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        for kind in _skill_real_op_kinds(skill_dir):
            all_kinds.add(kind)

    # Spot-checks confirmed by direct recon (source in parentheses):
    spot_checks = {
        "file",          # many skills, allowed_ops
        "sandboxed_exec", # swe_bench, allowed_ops + run_op preprocessor
        "embed",         # index_docs + index_events, run_op postprocessor
        "index_write",   # index_docs + index_events, run_op postprocessor
        "recall",        # ops_report + skill_improver, run_op preprocessor
        "skill_resolve", # eval_builder + skill_improver, run_op preprocessor
    }
    missing_from_scan = spot_checks - all_kinds
    assert missing_from_scan == set(), (
        f"Scanner did not find expected op kinds (scanner may be broken): "
        f"{sorted(missing_from_scan)}"
    )
