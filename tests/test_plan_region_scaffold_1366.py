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
    for p in pairs:
        assert p["symbol_re"] == re.escape(p["symbol"])
    # each file is paired with the same symbol set
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
