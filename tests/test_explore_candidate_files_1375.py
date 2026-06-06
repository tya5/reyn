"""Tier 2: OS/skill invariant — #1375 D2 explore file-candidate scaffolding.

The explore phase (weak model) misses the gold files (astropy-13398: gold in
builtin_frames/* that explore overlooked). The D2 preprocessor pre-greps the
problem statement's code-symbols across the repo and surfaces the strongest
candidate files into ``_candidate_files`` (ranked by symbol co-occurrence +
specificity), so explore SEES the gold files. Explore-layer analogue of the plan
region-surfacing (#1366).

Pins:
  - ``extract_explore_symbols`` yields the problem-statement code-symbols;
  - ``rank_candidate_files`` ranks by co-occurrence AND specificity — a file named
    by a single RARE symbol (an exact method name) outranks an incidental file
    matched by several COMMON symbols (lead-coder's refinement);
  - the real explore preprocessor (real Workspace + skill + op_runtime grep)
    surfaces the gold file into ``_candidate_files``.

Real Workspace + real skill loaded from disk + real grep; no mocks.
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


def _fns():
    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from extract_problem_symbols import (
            extract_explore_symbols,
            rank_candidate_files,
        )
    finally:
        sys.path.pop(0)
    return extract_explore_symbols, rank_candidate_files


def test_extract_explore_symbols_yields_problem_symbols() -> None:
    """Tier 2: extract_explore_symbols returns the problem-statement code-symbols."""
    extract_explore_symbols, _ = _fns()
    syms = extract_explore_symbols(
        {"data": {"problem_statement": "the `rotation_matrix` and `itrs_to_observed_mat` are wrong"}}
    )
    names = {s["symbol"] for s in syms}
    assert "rotation_matrix" in names and "itrs_to_observed_mat" in names
    for s in syms:  # each carries a regex-escaped pattern for the grep
        assert "symbol_re" in s


def test_rank_specificity_beats_incidental_cooccurrence() -> None:
    """Tier 2: a gold file named by a single RARE symbol outranks an incidental
    file matched by several COMMON symbols (co-occurrence + specificity).

    This is the astropy-13398 shape: `itrs_to_observed_mat` (exact gold method)
    matches only the gold file, while common symbols match many decoys.
    """
    _, rank_candidate_files = _fns()
    gold = "astropy/coordinates/builtin_frames/itrs_observed_transforms.py"
    symbol_files = [
        {"files": [gold]},                         # a SPECIFIC symbol → 1 file (gold)
        {"files": ["x.py", "y.py", "z.py", "w.py"]},  # a COMMON symbol → 4 decoys
        {"files": ["x.py", "y.py", "z.py", "w.py"]},  # another COMMON symbol → same decoys
    ]
    out = rank_candidate_files({"data": {"_symbol_files": symbol_files}})
    assert out["_candidate_files"][0] == gold, (
        f"the specific-symbol gold file must rank first, got {out['_candidate_files']}"
    )


def test_rank_cooccurrence_orders_multi_symbol_files() -> None:
    """Tier 2: among equally-(non)specific files, more symbol co-occurrence ranks higher."""
    _, rank_candidate_files = _fns()
    # all symbols are common (match 3 files) so no specificity bonus; the file
    # matched by MORE symbols (co-occurrence) wins.
    symbol_files = [
        {"files": ["a.py", "b.py", "c.py"]},
        {"files": ["a.py", "d.py", "e.py"]},
        {"files": ["a.py", "f.py", "g.py"]},
    ]
    out = rank_candidate_files({"data": {"_symbol_files": symbol_files}})
    assert out["_candidate_files"][0] == "a.py", out["_candidate_files"]


def test_rank_empty_when_no_symbol_files() -> None:
    """Tier 2: no _symbol_files → empty _candidate_files (graceful; explore greps)."""
    _, rank_candidate_files = _fns()
    assert rank_candidate_files({"data": {}})["_candidate_files"] == []


def test_explore_preprocessor_surfaces_gold_candidate(tmp_path: Path) -> None:
    """Tier 2: the REAL explore preprocessor (real grep over a tmp repo) surfaces
    the gold file — named by a specific symbol — at the top of _candidate_files,
    above decoys named by common symbols.
    """
    skill = load_dsl_skill(SWE_BENCH_DIR / "skill.md")
    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"python.safe": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    ws = Workspace(events=events, base_dir=tmp_path, permission_resolver=resolver)
    # gold file: contains the rare/specific method name; decoys: only a common word
    ws.write_file("pkg/builtin/gold.py", "def itrs_to_observed_mat(self):\n    return rotation_matrix\n")
    ws.write_file("pkg/decoy1.py", "rotation_matrix = 1\n")
    ws.write_file("pkg/decoy2.py", "rotation_matrix = 2\n")
    ws.write_file("pkg/decoy3.py", "rotation_matrix = 3\n")

    artifact = {
        "type": "swe_bench_input",
        "data": {
            "instance_id": "x__y-1",
            "problem_statement": "the `itrs_to_observed_mat` transform using `rotation_matrix` is wrong",
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
        executor.run(skill.phases["explore"], artifact, output_language=None)
    )
    candidates = result["data"].get("_candidate_files") or []
    assert candidates, "explore preprocessor produced no _candidate_files"
    assert candidates[0].endswith("gold.py"), (
        f"the gold file (specific symbol itrs_to_observed_mat) must rank first; got {candidates}"
    )


def test_rank_excludes_git_and_binary_candidates() -> None:
    """Tier 2: #1375 D9 — the repo-wide grep matches .git/ (binary pack files) and
    other non-source paths; those must be dropped from _candidate_files (an
    unreadable .git pack as a candidate made the explore model abort)."""
    _, rank_candidate_files = _fns()
    symbol_files = [
        {"files": [
            ".git/objects/pack/pack-abc.pack",   # binary git pack — drop
            "astropy/io/ascii/html.py",          # source — keep
            "astropy/_build_utils/helper.py",     # source ('build' is a SUBSTRING,
                                                  # not a path component) — KEEP
            "build/lib/x.py",                     # build/ dir component — drop
            "docs/_static/logo.png",              # binary — drop
            "__pycache__/mod.cpython-311.pyc",    # cache — drop
        ]},
    ]
    out = rank_candidate_files({"data": {"_symbol_files": symbol_files}})
    cands = set(out["_candidate_files"])
    assert cands == {"astropy/io/ascii/html.py", "astropy/_build_utils/helper.py"}, (
        f"only source survives, and a 'build' SUBSTRING (not component) is not "
        f"false-dropped: {cands}"
    )


def _fns_d7():
    sys.path.insert(0, str(SWE_BENCH_DIR))
    try:
        from extract_problem_symbols import (
            extract_filename_tokens,
            rank_candidate_files,
        )
    finally:
        sys.path.pop(0)
    return extract_filename_tokens, rank_candidate_files


def test_extract_filename_tokens_splits_meaningfully() -> None:
    """Tier 2: #1375 D7 — symbols split into meaningful lowercase tokens for the
    filename glob (itrs_to_observed_mat -> itrs/observed/mat; AltAz -> alt; short
    fragments dropped)."""
    extract_filename_tokens, _ = _fns_d7()
    toks = {t["token"] for t in extract_filename_tokens(
        {"data": {"problem_statement": "`itrs_to_observed_mat` and `AltAz` and `rotation_matrix`"}}
    )}
    assert {"itrs", "observed", "rotation", "matrix", "alt"} <= toks
    assert "to" not in toks and "az" not in toks  # short fragments dropped
    for t in extract_filename_tokens({"data": {"problem_statement": "`itrs`"}}):
        assert t["glob"] == "**/*itrs*"  # the glob pattern for the iterate step


def test_merge_surfaces_filename_gold_with_zero_content() -> None:
    """Tier 2: #1375 D7 — a fix-site file named by a problem token but with ZERO
    content match (a patch-ADDED method) surfaces via the filename signal, merged
    (interleaved) with the content candidates. This is the astropy-13398 case: the
    gold `itrs_observed_transforms.py` (0 content match) must appear alongside the
    `cirs/icrs` siblings D2's content-grep found.
    """
    _, rank_candidate_files = _fns_d7()
    base = "astropy/coordinates/builtin_frames/"
    out = rank_candidate_files({"data": {
        # D2 content-grep found only the siblings (gold has 0 content match)
        "_symbol_files": [{"files": [base + "cirs_observed_transforms.py",
                                     base + "icrs_observed_transforms.py"]}],
        # D7 filename-glob found the gold by its name tokens (itrs, observed)
        "_filename_files": [
            {"matches": [base + "itrs.py", base + "itrs_observed_transforms.py"]},  # itrs
            {"matches": [base + "itrs_observed_transforms.py",
                         base + "cirs_observed_transforms.py"]},  # observed
        ],
    }})
    cands = out["_candidate_files"]
    assert base + "itrs_observed_transforms.py" in cands, (
        f"the filename-gold (0 content match) must surface via D7: {cands}"
    )
    # the content candidate is NOT crowded out by the filename merge
    assert base + "cirs_observed_transforms.py" in cands


def test_merge_empty_filename_falls_back_to_content() -> None:
    """Tier 2: D7 falsification — no filename matches → _candidate_files is just the
    content (D2) candidates (graceful; D7 adds nothing when no filename overlaps)."""
    _, rank_candidate_files = _fns_d7()
    out = rank_candidate_files({"data": {
        "_symbol_files": [{"files": ["pkg/a.py", "pkg/b.py"]}],
        "_filename_files": [],
    }})
    assert set(out["_candidate_files"]) == {"pkg/a.py", "pkg/b.py"}
