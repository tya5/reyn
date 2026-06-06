"""Tier 2: OS/skill invariant — #1366 plan deterministic region scaffolding.

The plan phase must emit a verbatim ``anchor`` per edit, but a large
``relevant_file`` is read-truncated, so the model never sees the target region
and fabricates an anchor the apply grep (#1209) cannot find. This is the
plan-layer analogue of apply-starvation. The plan preprocessor closes it
deterministically: extract code-symbols from the ``problem_statement`` (the
legitimate task input — NOT test_patch, which would deepen leakage) and grep them
against the explore ``relevant_files``, placing the problem-relevant regions into
``_plan_regions`` BEFORE the plan model runs.

This pins:
  - ``extract_problem_symbols`` returns valid code identifiers from a
    representative problem statement (it is load-bearing that the extraction is
    code-fence-aware, not junk — a junk-only result would surface no region);
  - the plan preprocessor (extract → iterate grep) places a target region that
    sits PAST the read-truncation window into ``_plan_regions`` — i.e. the
    grounding is truncation-independent, not reliant on model navigation;
  - falsification: a symbol absent from the file yields no fabricated region
    (the model then falls back to its own reads).

Real Workspace + real skill loaded from disk + real op_runtime grep via
PreprocessorExecutor with the OS-injected ``_skill_input`` binding (the #1115
Stage 0 mechanism); no collaborator mocks. ``extract_problem_symbols`` is a pure
data-transform tested directly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.events.events import EventLog
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.sandbox import NoopBackend
from reyn.workspace.workspace import Workspace

SWE_BENCH_DIR = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


# ── extract_problem_symbols: pure data transform (extraction validity) ───────


def _extract():
    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from extract_problem_symbols import _rank_symbols, extract_problem_symbols
    finally:
        sys.path.pop(0)
    return _rank_symbols, extract_problem_symbols


def _prune():
    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from prune_plan_regions import _MAX_TOTAL_CHARS, prune_plan_regions
    finally:
        sys.path.pop(0)
    return prune_plan_regions, _MAX_TOTAL_CHARS


def test_prune_is_bounded_by_construction_total_size() -> None:
    """Tier 2: the surfaced _plan_regions size is bounded by construction — the
    total never exceeds the size budget, whatever the input grep counts.

    This is the #1366 regression the original synthetic single-match tests missed:
    on astropy-13236 `Column` matched 228 lines → raw _plan_regions reached ~6MB →
    the plan model aborted. The size budget (primary bound), not a magic count
    threshold, guarantees a small context.
    """
    import json as _json

    prune_plan_regions, max_total = _prune()
    # a pathological input: one huge high-count region + many large low-count ones
    regions = [{"count": 228, "matches": [{"content": "Column " * 200}] * 228}]
    regions += [{"count": 2, "matches": [{"content": "x" * 4000}]} for _ in range(50)]
    out = prune_plan_regions({"data": {"_plan_regions": regions}})
    total = len(_json.dumps(out["_plan_regions"]))
    assert total <= max_total, f"total _plan_regions must be <= budget {max_total}, got {total}"


def test_prune_drops_no_match_and_prioritizes_specific_over_generic() -> None:
    """Tier 2: count is a SECONDARY ranking signal — a no-match region (count 0) is
    dropped (validity), and precise low-count locators are surfaced ahead of a
    non-specific high-count symbol when the budget is contended.

    Realistic mixed distribution (lead-coder guidance #3): a common symbol
    (high match-count, e.g. `Column`) + specific symbols (low count, the gold
    targets). The specific ones must survive; the generic one is crowded out by the
    size budget, not a hard threshold. Mirrors drop_not_locatable's spirit.
    """
    prune_plan_regions, max_total = _prune()
    # Size the precise locators so the two of them ~fill the budget; the generic
    # high-count region (sorted last) is then crowded out. This mirrors the real
    # astropy-13236 distribution where ±context regions are ~13KB each so only the
    # first couple by ascending count fit.
    big = int(max_total * 0.45)  # each specific ~45% of budget → two ~fill it
    specific_a = {"count": 1, "matches": [{"content": "N" * big}]}      # gold
    specific_b = {"count": 2, "matches": [{"content": "d" * big}]}      # gold
    absent = {"count": 0, "matches": []}
    generic = {"count": 228, "matches": [{"content": "Column " * 400}] * 228}
    out = prune_plan_regions(
        {"data": {"_plan_regions": [generic, absent, specific_a, specific_b]}}
    )
    kept_counts = [r["count"] for r in out["_plan_regions"]]
    assert 0 not in kept_counts, "a no-match region must be dropped (validity filter)"
    # the precise locators are surfaced; the generic high-count symbol is crowded out
    assert 1 in kept_counts and 2 in kept_counts, (
        f"specific (gold) locators must survive, got {kept_counts}"
    )
    assert 228 not in kept_counts, (
        f"the non-specific high-count symbol must be crowded out by the budget, got {kept_counts}"
    )


def test_prune_keeps_proximal_gold_match_over_early_isolated() -> None:
    """Tier 2: #1375 D1 — within a region the prune keeps matches by PROXIMITY to
    other symbols' matches, not first-N — so a LATE gold match co-located with
    another symbol survives while early isolated junk is dropped.

    This is the astropy-13453 failure: the plain `write` symbol matched early
    lines [2,5,15] (module docstring/imports) AND the gold `write()` method @349;
    first-N kept [2,5,15] and dropped @349. Here another symbol matches @348
    (co-located with the gold @349), so proximity-rank must keep @349.
    """
    prune_plan_regions, _ = _prune()
    f = "astropy/io/ascii/html.py"
    # the gold symbol's region: 3 early isolated junk matches + the gold @349
    gold_region = {
        "count": 4,
        "matches": [
            {"path": f, "line_number": "2", "content": "module docstring"},
            {"path": f, "line_number": "5", "content": "import"},
            {"path": f, "line_number": "15", "content": "import"},
            {"path": f, "line_number": "349", "content": "self.data._set_col_formats()"},
        ],
    }
    # a DIFFERENT symbol matches @348 — adjacent to the gold @349 (the cluster)
    neighbor_region = {
        "count": 1,
        "matches": [{"path": f, "line_number": "348", "content": "cols = ..."}],
    }
    out = prune_plan_regions({"data": {"_plan_regions": [gold_region, neighbor_region]}})
    kept_gold = [r for r in out["_plan_regions"] if r["count"] == 4][0]
    kept_lines = {int(m["line_number"]) for m in kept_gold["matches"]}
    # _MAX_MATCHES_PER_REGION caps to 3; the gold @349 (dist 1 to @348) must be kept
    assert 349 in kept_lines, (
        f"the proximal gold match @349 must survive (got {sorted(kept_lines)}); "
        "first-N selection would have dropped it for the early isolated lines"
    )


def test_extract_yields_valid_code_symbols_not_junk() -> None:
    """Tier 2: code-fence-aware extraction returns real identifiers, not prose junk.

    Load-bearing per the design review: a naive backtick-only / bare-word
    extraction returns junk (``the``, a filename) and the grep then locates no
    region, so the fix is a no-op. The extractor must surface the API symbol the
    problem statement names — here ``write_table`` and the kwarg ``formats`` — and
    must NOT return prose stopwords.
    """
    _rank_symbols, _ = _extract()
    problem_statement = (
        "When calling `Table.write` with the `formats` keyword the HTML output is\n"
        "wrong. Reproduction:\n\n"
        "```python\n"
        "from astropy.table import Table\n"
        "t = Table()\n"
        "t.write(sp, format='html', formats={'a': lambda x: f'{x:.2e}'})\n"
        "```\n\n"
        "Please see CONTRIBUTING.md. The write_table path ignores formats.\n"
    )
    symbols = _rank_symbols(problem_statement)
    assert symbols, "extraction returned no symbols for a code-bearing problem statement"
    # the API symbol the issue names is surfaced (the kwarg that IS the bug)
    assert "formats" in symbols, f"gold kwarg 'formats' missing from {symbols}"
    # the dotted API path is surfaced
    assert "Table.write" in symbols, f"dotted API path missing from {symbols}"
    # prose stopwords are NOT treated as symbols
    assert "the" not in symbols and "Please" not in symbols
    # a doc filename mentioned in prose is NOT a code symbol
    assert "CONTRIBUTING.md" not in symbols, f"doc filename leaked as symbol: {symbols}"


def test_extract_surfaces_snake_case_symbol() -> None:
    """Tier 2: a snake_case identifier the issue names is extracted as a symbol."""
    _rank_symbols, _ = _extract()
    symbols = _rank_symbols(
        "The `compute_fill_values` helper returns the wrong result; "
        "compute_fill_values is called from the writer. Fix compute_fill_values."
    )
    assert "compute_fill_values" in symbols, f"snake_case symbol missing from {symbols}"


def test_extract_empty_when_no_problem_statement() -> None:
    """Tier 2: no problem_statement / no relevant_files → empty pair list (graceful)."""
    _, extract_problem_symbols = _extract()
    assert extract_problem_symbols({"data": {"relevant_files": ["a.py"]}}) == []
    assert extract_problem_symbols(
        {"_skill_input": {"data": {"problem_statement": "use `foo_bar`"}}, "data": {}}
    ) == []


def test_extract_pairs_are_cartesian_files_x_symbols() -> None:
    """Tier 2: returns {file, symbol, symbol_re} for each relevant_file x symbol.

    ``symbol_re`` is re.escape-d (the iterate grep compiles a regex), and every
    relevant_file is paired with every extracted symbol so the iterate step can
    grep each combination.
    """
    import re

    _, extract_problem_symbols = _extract()
    frame = {
        "_skill_input": {"data": {"problem_statement": "fix `alpha_beta` in `Gamma.delta`"}},
        "data": {"relevant_files": ["pkg/x.py", "pkg/y.py"]},
    }
    pairs = extract_problem_symbols(frame)
    assert pairs, "expected non-empty pairs"
    files = {p["file"] for p in pairs}
    assert files == {"pkg/x.py", "pkg/y.py"}
    # plain-symbol pairs carry the re.escape'd pattern; #1375 D1 also adds a
    # method-definition pair per non-dotted symbol (`<sym> (def)` -> `def ...`).
    for p in pairs:
        if p["symbol"].endswith(" (def)"):
            assert p["symbol_re"].startswith(r"def\s+"), f"def-pair pattern: {p}"
        else:
            assert p["symbol_re"] == re.escape(p["symbol"])
    # a def-pair exists for the non-dotted symbol, not for the dotted one
    defs = {p["symbol"] for p in pairs if p["symbol"].endswith(" (def)")}
    assert "alpha_beta (def)" in defs and "Gamma.delta (def)" not in defs
    # each file is paired with the same pair set
    by_file = {f: sorted(p["symbol"] for p in pairs if p["file"] == f) for f in files}
    assert by_file["pkg/x.py"] == by_file["pkg/y.py"]


# ── plan preprocessor: deterministic region scaffolding (truncation-indep) ───


def _run_plan_preprocessor(
    tmp_path: Path, file_path: str, file_body: str, problem_statement: str
) -> dict:
    """Run the plan preprocessor through the REAL enforced permission path with
    the OS-injected ``_skill_input`` binding (mirrors the apply #1209 harness +
    the #1115 Stage 0 skill_input passthrough)."""
    skill = load_dsl_skill(SWE_BENCH_DIR / "skill.md")
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"python.safe": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    ws = Workspace(events=events, base_dir=tmp_path, permission_resolver=resolver)
    ws.write_file(file_path, file_body)

    artifact = {
        "type": "exploration",
        "data": {
            "instance_id": "x__y-1",
            "relevant_files": [file_path],
            "summary": "s",
            "hints_used": False,
        },
    }
    skill_input = {
        "type": "swe_bench_input",
        "data": {"instance_id": "x__y-1", "problem_statement": problem_statement},
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
        executor.run(
            skill.phases["plan"], artifact, output_language=None, skill_input=skill_input
        )
    )
    return result["data"]


def _big_body(unique_line: str) -> str:
    """A file large enough that ``unique_line`` sits far past any head-window read
    truncation (so model navigation, not the preprocessor grep, would be needed
    to see it)."""
    head = "".join(f"# filler line {i}\n" for i in range(400))
    tail = "".join(f"# trailer line {i}\n" for i in range(400))
    return head + unique_line + "\n" + tail


def test_plan_preprocessor_surfaces_target_region_past_truncation(tmp_path: Path) -> None:
    """Tier 2: a problem-named symbol deep in a large file is placed into
    ``_plan_regions`` deterministically — truncation-independent grounding.

    The target line sits past 400 filler lines (a read would truncate before
    reaching it); the preprocessor greps the symbol named in the problem
    statement and surfaces the region regardless of where it sits in the file.
    """
    target = "    def render_formats(self, col):  # PLAN-TARGET-REGION-ZZZ"
    data = _run_plan_preprocessor(
        tmp_path,
        "pkg/mod.py",
        _big_body(target),
        problem_statement="The `render_formats` method drops the formatting. Fix it.",
    )

    assert "_plan_regions" in data, "preprocessor did not produce _plan_regions"
    blob = str(data["_plan_regions"])
    assert "PLAN-TARGET-REGION-ZZZ" in blob, (
        "the problem-named symbol's region was not surfaced into _plan_regions "
        "(model navigation would otherwise be required to see it)"
    )


def test_plan_preprocessor_bounds_volume_for_common_symbol(tmp_path: Path) -> None:
    """Tier 2: a problem-named symbol that matches MANY lines does NOT bloat
    _plan_regions — the prune step caps the volume (the #1366 regression: on
    astropy-13236 `Column` matched 228 lines → ~6MB _plan_regions → plan aborted).

    Builds a file where `Column` appears on hundreds of lines, names it in the
    problem statement, runs the full real preprocessor, and asserts the resulting
    _plan_regions stays small (the generic high-count region is dropped).
    """
    import json as _json

    body = "".join(f"class Column{i}:  # Column usage line {i}\n" for i in range(300))
    data = _run_plan_preprocessor(
        tmp_path,
        "pkg/mod.py",
        body,
        problem_statement="The `Column` class is broken in the writer.",
    )
    regions = data.get("_plan_regions") or []
    total = len(_json.dumps(regions))
    assert total < 60000, (
        f"_plan_regions must stay bounded for a high-count symbol (got {total} chars); "
        "the prune step should have dropped the too-generic region"
    )


def test_plan_preprocessor_absent_symbol_yields_no_fabricated_region(tmp_path: Path) -> None:
    """Tier 2: falsification — a symbol that the file does not contain produces no
    region (grep count 0), so nothing is fabricated; the plan model falls back to
    its own reads. Pairs with the surface test above."""
    data = _run_plan_preprocessor(
        tmp_path,
        "pkg/mod.py",
        _big_body("    real_line = 1  # ACTUAL-ONLY"),
        problem_statement="The `nonexistent_symbol_qqq` is broken.",
    )
    # the preprocessor ran (key present) but no region carries a real match
    regions = data.get("_plan_regions") or []
    for r in regions:
        assert r.get("count", 0) == 0 or not r.get("matches"), (
            "a symbol absent from the file must not yield a fabricated region"
        )


def _run_plan_preprocessor_replan(
    tmp_path: Path, file_path: str, file_body: str, problem_statement: str
) -> dict:
    """Run the plan preprocessor on a RE-PLAN input (``verify_state``, which has
    NO ``relevant_files``) — D8 must re-derive candidate files from the repo."""
    skill = load_dsl_skill(SWE_BENCH_DIR / "skill.md")
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"python.safe": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    ws = Workspace(events=events, base_dir=tmp_path, permission_resolver=resolver)
    ws.write_file(file_path, file_body)
    # verify_state: the re-plan input — note NO relevant_files
    artifact = {
        "type": "verify_state",
        "data": {
            "instance_id": "x__y-1",
            "tests_passed": False,
            "attempt": 2,
            "failure_summary": "the fix did not work",
        },
    }
    skill_input = {
        "type": "swe_bench_input",
        "data": {"instance_id": "x__y-1", "problem_statement": problem_statement},
    }
    executor = PreprocessorExecutor(
        skill=skill, workspace=ws, model="standard", events=events, subscribers=[],
        resolver=None, permission_resolver=resolver, sandbox_backend=NoopBackend(),
    )
    result, _usage = asyncio.run(
        executor.run(skill.phases["plan"], artifact, output_language=None, skill_input=skill_input)
    )
    return result["data"]


def test_replan_rederives_regions_without_relevant_files(tmp_path: Path) -> None:
    """Tier 2: #1375 D8 — on a re-plan (verify_state, no relevant_files) the plan
    preprocessor RE-DERIVES candidate files from the problem_statement (repo grep)
    and still surfaces the target region into _plan_regions.

    Before D8, a verify_state input gave extract_problem_symbols 0 files -> 0
    regions on EVERY re-plan (astropy-13453: 13/14 plan iterations blind). The
    re-derive (D2 mechanism) restores the scaffolding.
    """
    target = "    def render_special(self, col):  # REPLAN-TARGET-XYZ"
    data = _run_plan_preprocessor_replan(
        tmp_path, "pkg/mod.py", _big_body(target),
        problem_statement="The `render_special` method drops formatting. Fix it.",
    )
    # candidate files were re-derived (no relevant_files on the verify_state input)
    assert data.get("_candidate_files"), "D8 must re-derive _candidate_files on re-plan"
    blob = str(data.get("_plan_regions") or [])
    assert "REPLAN-TARGET-XYZ" in blob, (
        "on a re-plan, the target region must be re-derived + surfaced (was 0 before D8)"
    )


def test_replan_no_symbol_yields_empty_graceful(tmp_path: Path) -> None:
    """Tier 2: D8 falsification — a re-plan whose problem_statement names no
    greppable symbol re-derives no candidates and surfaces no region (graceful;
    the model falls back to its own reads), never an error."""
    data = _run_plan_preprocessor_replan(
        tmp_path, "pkg/mod.py", _big_body("    x = 1  # ACTUAL"),
        problem_statement="the behavior is wrong",  # no code symbols
    )
    regions = data.get("_plan_regions") or []
    for r in regions:
        assert r.get("count", 0) == 0 or not r.get("matches")
