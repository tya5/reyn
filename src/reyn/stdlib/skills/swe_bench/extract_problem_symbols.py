"""Phase preprocessor (#1366): deterministic plan-time region scaffolding.

The plan phase must emit, for each edit, a verbatim ``anchor`` copied from the
current file — but a large ``relevant_file`` is read-truncated, so the model
never sees the target region and fabricates an anchor that the apply grep (#1209)
cannot find (region count 0 → #1216 drops the edit → no fix). This is the
plan-layer analogue of the apply-starvation root cause.

apply solves it by grepping the model's ``anchor``; at plan there is no anchor
yet (plan is *producing* it). The deterministic region-locator available at plan
time is the **problem statement**: it is the legitimate task input the real
solver reads, and for SWE-bench it names the affected symbols (repro code-fences,
tracebacks, backtick-quoted API names). We extract those code-identifiers and
grep them against the explore phase's ``relevant_files`` so the OS places the
problem-relevant regions into context BEFORE the plan model runs — the model then
copies a real anchor from a region it actually sees.

We deliberately do NOT grep ``test_patch``: using it at plan would deepen the
test leakage beyond the verify-only level (it would show the model the
tested functions' code), changing the internal-signal property. ``problem_statement``
keeps the leakage where it already is — reading the issue is exactly what a real
solver does.

This step returns the cartesian product (relevant_file x problem-symbol) as a
list of ``{file, symbol, symbol_re}`` dicts; the iterate step then greps each
``symbol_re`` (regex-escaped, since grep compiles its pattern as a regex) in each
``file`` and collects the regions into ``_plan_regions`` — mirroring the apply
preprocessor shape. Pure data transform (no file access; the grep happens in the
iterate step), deterministic, P5-correct (OS-run before the plan LLM).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

# Tokens that look like code but are language/English noise — dropped so the
# grep is not flooded with non-locating patterns. Kept deliberately small: a
# stray junk symbol only wastes one grep (it locates nothing), whereas dropping a
# real symbol loses a region, so we err toward keeping candidates.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "not", "you", "with", "this", "that", "from",
        "import", "print", "return", "def", "class", "self", "true", "false",
        "none", "please", "out", "output", "input", "your", "are", "can",
        "but", "has", "have", "will", "all", "any", "use", "using", "when",
        "should", "would", "https", "http", "com", "org", "www", "github",
        # common builtins / generic calls that match many lines (low locating value)
        "isinstance", "len", "str", "int", "list", "dict", "type", "super",
        "version", "__version__",
    }
)

# Doc / config file extensions — a dotted token ending in one of these is a
# filename mentioned in prose (CONTRIBUTING.md), not a code symbol to grep.
_DOC_EXTENSIONS = frozenset(
    {"md", "rst", "txt", "cfg", "ini", "toml", "yaml", "yml", "json", "lock"}
)

# How many distinct symbols to keep (ranked by frequency in the problem
# statement). The relevant_files set is already narrow (usually 1-3), so this
# bounds the total grep count and the region volume placed into context.
_MAX_SYMBOLS = 6

# Identifier shapes, most-specific first:
#   - backtick-quoted spans:        `Table.write`, `formats`
#   - dotted attribute paths:       astropy.io.ascii, Table.write
#   - snake_case:                   write_table, fill_values
#   - CamelCase:                    HtmlWriter, Table
#   - fenced call targets / kwargs: ``.write(`` -> write, ``formats=`` -> formats
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```.*?\n(.*?)```", re.S)
_DOTTED_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_SNAKE_RE = re.compile(r"\b([a-z_]+_[a-z_0-9]+)\b")
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+[A-Z]\w*|[A-Z]{2,}[a-z]\w*)\b")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w+)\s*\(")
_KWARG_RE = re.compile(r"\b([A-Za-z_]\w+)\s*=")
# A bare identifier (used only to harvest call/kwarg targets from code fences,
# where a plain lowercase word like ``formats`` is a real symbol, not prose).
_IDENT_RE = re.compile(r"[A-Za-z_]\w+")


def _problem_statement(data: Mapping[str, Any]) -> str:
    """Read ``problem_statement`` (P5 entry-input passthrough, mirrors
    ``sanitize_test_patch``): ``_skill_input.data.problem_statement`` first (the
    OS-injected original entry artifact, never LLM-mutated), then the legacy
    ``_input_raw`` file.read shape, then ``data.problem_statement`` /
    top-level — the last two for unit tests injecting the input directly."""
    skill_input = data.get("_skill_input")
    if isinstance(skill_input, dict):
        si_data = skill_input.get("data")
        if isinstance(si_data, dict):
            ps = si_data.get("problem_statement")
            if isinstance(ps, str) and ps:
                return ps

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    input_raw = inner.get("_input_raw") if isinstance(inner, dict) else None
    if isinstance(input_raw, dict):
        content = input_raw.get("content")
        if isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content)
                ps = (parsed.get("data") or {}).get("problem_statement")
                if isinstance(ps, str) and ps:
                    return ps
            except (json.JSONDecodeError, AttributeError):
                pass

    for src in (inner, data):
        if isinstance(src, dict):
            ps = src.get("problem_statement")
            if isinstance(ps, str) and ps:
                return ps
    return ""


def _relevant_files(data: Mapping[str, Any]) -> list[str]:
    """Read the files to region-surface: ``relevant_files`` from the exploration
    input, else (#1375 D8) the re-derived ``_candidate_files``.

    On a re-plan the plan input is ``verify_state``, which has NO
    ``relevant_files`` (only the explore-phase output carries them). Without a
    fallback, the region-surfacing produces 0 regions on EVERY re-plan — the model
    flies blind through the whole verify→plan revision loop (astropy-13453: 13 of
    14 plan iterations had no regions). D8 re-derives candidate files
    deterministically from the problem_statement (the D2 explore-scaffolding repo
    grep, run as preprocessor steps before this) into ``_candidate_files``, so the
    re-plan gets the same gold-region scaffolding as the first plan."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return []
    files = inner.get("relevant_files")
    if not isinstance(files, list) or not files:
        files = inner.get("_candidate_files")  # D8: re-derived fallback (re-plan)
    if isinstance(files, list):
        return [f for f in files if isinstance(f, str) and f]
    return []


def _rank_symbols(problem_statement: str) -> list[str]:
    """Extract candidate code-identifiers from the problem statement, ranked by
    frequency, capped to ``_MAX_SYMBOLS``. Deterministic (fixed regexes + stable
    sort). Returns [] when nothing code-like is present (the caller then yields
    no pairs → the plan model falls back to its own targeted reads)."""
    counts: dict[str, int] = {}

    def _add(tok: str) -> None:
        tok = tok.strip()
        if len(tok) < 3 or tok.lower() in _STOPWORDS:
            return
        # a pure integer or a token starting with a digit is not a symbol
        if tok[0].isdigit():
            return
        # a dotted token whose every piece is a stopword (e.g. github.com) is noise
        if "." in tok and all(p.lower() in _STOPWORDS for p in tok.split(".") if p):
            return
        # a doc/config filename (CONTRIBUTING.md, setup.cfg) is not a code symbol
        if "." in tok and tok.rsplit(".", 1)[-1].lower() in _DOC_EXTENSIONS:
            return
        counts[tok] = counts.get(tok, 0) + 1

    fences = "\n".join(_FENCE_RE.findall(problem_statement))

    for span in _BACKTICK_RE.findall(problem_statement):
        # split a backtick span into identifier pieces, keeping dotted paths
        for piece in _DOTTED_RE.findall(span):
            _add(piece)
        for piece in _IDENT_RE.findall(span):
            _add(piece)

    for rx in (_DOTTED_RE, _SNAKE_RE, _CAMEL_RE):
        for tok in rx.findall(problem_statement):
            _add(tok)

    # call targets / kwargs from code fences capture plain lowercase symbols
    # (e.g. ``formats=`` -> formats) that the structural regexes above skip.
    for rx in (_CALL_RE, _KWARG_RE):
        for tok in rx.findall(fences):
            _add(tok)

    # rank: frequency desc, then longer (more specific) first, then lexical for
    # determinism.
    ranked = sorted(counts, key=lambda t: (-counts[t], -len(t), t))
    return ranked[:_MAX_SYMBOLS]


def extract_problem_symbols(data: Mapping[str, Any]) -> list[dict]:
    """Return ``[{file, symbol, symbol_re}, ...]`` = cartesian product of the
    explore ``relevant_files`` and the problem-statement code-symbols.

    ``symbol_re`` is ``re.escape``-d because the iterate grep compiles its
    pattern as a regex. Returns an empty list when there are no relevant files or
    no extractable symbols (the iterate step then produces no regions and the
    plan model uses its own reads — no failure)."""
    files = _relevant_files(data)
    symbols = _rank_symbols(_problem_statement(data))
    out: list[dict] = []
    for f in files:
        for sym in symbols:
            out.append({"file": f, "symbol": sym, "symbol_re": re.escape(sym)})
            # #1375 D1: also grep the METHOD DEFINITION whose name contains the
            # symbol (e.g. `formats` -> `def _set_col_formats`, `write` -> `def
            # write`). The plain symbol grep matches many incidental lines (early
            # docstrings/imports) and the gold fix often lives inside a method the
            # problem statement names only obliquely; the def-grep surfaces that
            # method's body precisely (astropy-13453: `def write` @~340 carries
            # the gold `_set_col_formats()` site, which the plain `write` matches
            # missed). Skip dotted symbols (a method name has no dot).
            if "." not in sym:
                out.append({
                    "file": f,
                    "symbol": f"{sym} (def)",
                    "symbol_re": r"def\s+\w*" + re.escape(sym) + r"\w*\s*\(",
                })
    return out


# ── #1375 D2: explore-layer file-candidate scaffolding ──────────────────────
# The explore phase (weak model) greps/reads to find which files to edit, but
# misses the gold files (astropy-13398: gold in builtin_frames/*, explore looked
# at transformations.py / baseframe.py). These two steps pre-grep the problem
# statement's code-symbols across the repo and surface the strongest candidate
# files so explore SEES the gold files in context — the explore-layer analogue
# of the plan region-surfacing (#1366 / D1). Deterministic, P5; explore stays
# LLM-driven but no longer blind. Uses problem_statement only (NOT test_patch),
# same legitimate-input basis as D1.

#: how many candidate files to surface (bounded — explore reads its own beyond).
_MAX_CANDIDATE_FILES = 8
#: #1375 D9 — the repo-wide grep matches the WHOLE tree including .git/ (binary
#: pack files) and other non-source paths; surfacing those as candidates is noise
#: and an unreadable .git pack as a "candidate" can make the explore model abort
#: ("input artifact could not be read"). Drop non-source paths from candidates.
#: excluded by EXACT path-component match (not substring) so a real source path
#: like ``astropy/_build_utils/x.py`` is NOT false-dropped by ``build``.
_NON_SOURCE_DIRS = frozenset({
    ".git", ".tox", ".eggs", "node_modules", "build", "dist",
    ".mypy_cache", "__pycache__", ".pytest_cache",
})
_NON_SOURCE_SUFFIXES = (".pack", ".idx", ".pyc", ".pyo", ".so", ".o", ".png",
                        ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz")


def _is_source_candidate(path: str) -> bool:
    """True when ``path`` is a plausible source file (not .git/binary/cache).

    Directory exclusion is by EXACT path component (``build`` matches a ``build/``
    dir but not ``_build_utils/``), so real source files are never false-dropped.
    """
    if any(part in _NON_SOURCE_DIRS for part in path.split("/")):
        return False
    return not path.lower().endswith(_NON_SOURCE_SUFFIXES)
#: a symbol matching this few files is "specific" — a strong signal even alone
#: (e.g. an exact method name) — so its match outranks incidental co-occurrence.
_SPECIFIC_FILE_THRESHOLD = 2
_SPECIFIC_BONUS = 3.0


def extract_explore_symbols(data: Mapping[str, Any]) -> list[dict]:
    """Return ``[{symbol, symbol_re}, ...]`` for a repo-wide grep — the symbols
    the iterate step greps across the repo (path is the repo root, a constant in
    the op, so no per-symbol file here). Empty when no symbol is extractable."""
    return [
        {"symbol": s, "symbol_re": re.escape(s)}
        for s in _rank_symbols(_problem_statement(data))
    ]


def rank_candidate_files(data: Mapping[str, Any]) -> dict:
    """Rank the repo-grepped files by symbol co-occurrence + specificity into
    ``_candidate_files`` (write back with ``into: data``).

    Each entry of ``_symbol_files`` is one symbol's ``files_with_matches`` grep
    result (``{"files": [...]}``). A file scores 1 per matching symbol, plus a
    bonus when the matching symbol is *specific* (matches <= ``_SPECIFIC_FILE_THRESHOLD``
    files repo-wide) — so a gold file named by a single exact symbol (e.g.
    ``itrs_to_observed_mat``) outranks an incidental file matched by several
    common symbols (lead-coder's co-occurrence + specificity refinement)."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    results = inner.get("_symbol_files") or []

    scores: dict[str, float] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        files = [
            f for f in (r.get("files") or [])
            if isinstance(f, str) and f and _is_source_candidate(f)  # D9: drop .git/binary
        ]
        weight = 1.0 + (_SPECIFIC_BONUS if 1 <= len(files) <= _SPECIFIC_FILE_THRESHOLD else 0.0)
        for f in files:
            scores[f] = scores.get(f, 0.0) + weight

    ranked = sorted(scores, key=lambda f: (-scores[f], f))[:_MAX_CANDIDATE_FILES]
    return {**inner, "_candidate_files": ranked}
