"""Tier 2: #3128 (PR-E) — grep-zero completeness gate for the removed
in-process embedding backend.

#3128 removed reyn's in-process local-model embedding backend (reyn now
depends on litellm exclusively for embeddings). This gate is the
"clean-break completeness" enforcement (per project convention: clean-break
completeness = full-repo grep, not a src/tests-only sweep) so a future PR
cannot silently reintroduce a reference to the removed backend without this
test going RED:

  1. **``src`` / ``tests`` / ``pyproject.toml`` / ``uv.lock``**: the ST
     symbol set below must be **bare grep-zero** (0 hits, no exception) —
     this file included (see the self-exclusion note below).
  2. **``docs``**: two-tier — ``docs/deep-dives/decisions/`` and
     ``docs/deep-dives/proposals/`` are historical records and are
     excluded outright; everywhere else in ``docs``, an ST-symbol mention
     must co-occur with a removal-marker in the same **paragraph**
     (blank-line-delimited block), not merely the same line. Paragraph
     scope (not line scope) is load-bearing: a real removal-note in
     ``docs/concepts/tools-integrations/universal-catalog.md`` spans
     multiple lines (the marker sentence and the symbol mention are
     separate sentences in the same paragraph) — a line-scoped check
     false-positives on that exact passage.

Self-exclusion: this file legitimately talks *about* the ST symbols (to
define what the gate scans for). The symbol strings below are split across
a string concatenation so the raw bytes of this file never contain the
literal symbol substring the scanner is searching for — the file therefore
does not need a path-based carve-out in the scanner itself, but the
scanner also skips its own path defensively (belt and suspenders).

No mocks — this test reads real files from the real repo tree (Tier 2: OS
invariant, not a unit-collaborator test), plus synthetic tmp_path fixtures
to prove the scanning functions are load-bearing (would go RED on a real
violation), per testing.md's ban on faked collaborators: there is nothing
to fake here, the "collaborator" is the filesystem itself.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

# Split so this file's own bytes never contain the literal symbol strings
# it is scanning for (see module docstring "Self-exclusion").
ST_SYMBOLS: tuple[str, ...] = (
    "sentence" + "-transformers",
    "sentence" + "_transformers",
    "Sentence" + "Transformer",
    "local" + "-mini",
    "local" + "-e5",
    "local" + "-embed",
    "HF_HUB" + "_OFFLINE",
)

# \b-wrapped so e.g. ``local-embed`` doesn't false-positive-match inside the
# unrelated, still-current phrase "local-embedding" (docs use that phrase
# routinely post-#3128 to describe the *litellm-proxy-fronted* local-model
# path — it must not be caught by the removed-extra symbol scan).
_ST_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"\b" + re.escape(s) + r"\b") for s in ST_SYMBOLS
)

# Removal-marker vocabulary a docs paragraph must contain alongside an ST
# symbol mention to be considered a historical/explanatory reference rather
# than a live stale claim.
REMOVAL_MARKERS: tuple[str, ...] = ("#3128", "removed", "no longer", "exclusively")

DOCS_HISTORICAL_EXCLUDE_DIRS: tuple[Path, ...] = (
    REPO_ROOT / "docs" / "deep-dives" / "decisions",
    REPO_ROOT / "docs" / "deep-dives" / "proposals",
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _matching_symbols(text: str, symbols: tuple[str, ...]) -> list[str]:
    patterns = (
        _ST_SYMBOL_PATTERNS if symbols is ST_SYMBOLS
        else tuple(re.compile(r"\b" + re.escape(s) + r"\b") for s in symbols)
    )
    return [s for s, pat in zip(symbols, patterns) if pat.search(text)]


def _find_bare_hits(paths: list[Path], symbols: tuple[str, ...] = ST_SYMBOLS) -> list[str]:
    """Return ``"path:symbol"`` for every bare (unconditional) symbol hit."""
    hits: list[str] = []
    for path in paths:
        resolved = path.resolve()
        if resolved == THIS_FILE:
            continue
        text = _read_text(path)
        if text is None:
            continue
        for symbol in _matching_symbols(text, symbols):
            hits.append(f"{path}:{symbol}")
    return hits


def _paragraphs(text: str) -> list[str]:
    return re.split(r"\n\s*\n", text)


def _find_unmarked_doc_paragraphs(
    paths: list[Path],
    symbols: tuple[str, ...] = ST_SYMBOLS,
    markers: tuple[str, ...] = REMOVAL_MARKERS,
) -> list[str]:
    """Return ``"path:symbol"`` for every doc paragraph mentioning an ST
    symbol with NO removal-marker anywhere in that same paragraph."""
    violations: list[str] = []
    for path in paths:
        text = _read_text(path)
        if text is None:
            continue
        for paragraph in _paragraphs(text):
            hit_symbols = _matching_symbols(paragraph, symbols)
            if not hit_symbols:
                continue
            para_lower = paragraph.lower()
            has_marker = any(
                (m in paragraph) if m == "#3128" else (m in para_lower)
                for m in markers
            )
            if not has_marker:
                violations.extend(f"{path}:{s}" for s in hit_symbols)
    return violations


def _iter_files(root: Path, suffix: str | None = None) -> list[Path]:
    if not root.exists():
        return []
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts:
            continue
        if suffix is not None and p.suffix != suffix:
            continue
        out.append(p)
    return out


def _docs_scan_paths() -> list[Path]:
    docs_root = REPO_ROOT / "docs"
    paths = []
    for p in _iter_files(docs_root, suffix=".md"):
        if any(
            excluded == p or excluded in p.parents
            for excluded in DOCS_HISTORICAL_EXCLUDE_DIRS
        ):
            continue
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# 1. Real-repo gate: src / tests / pyproject.toml / uv.lock — bare grep-zero
# ---------------------------------------------------------------------------


def test_st_symbols_bare_grep_zero_in_src() -> None:
    """Tier 2: #3128 — no ST symbol reference survives anywhere in ``src``."""
    paths = _iter_files(REPO_ROOT / "src", suffix=".py")
    hits = _find_bare_hits(paths)
    assert not hits, (
        "stale ST-removal reference(s) found in src/ (must be bare "
        f"grep-zero, no exception): {hits}"
    )


def test_st_symbols_bare_grep_zero_in_tests() -> None:
    """Tier 2: #3128 — no ST symbol reference survives anywhere in ``tests``
    (this file itself is excluded — see module docstring "Self-exclusion")."""
    paths = _iter_files(REPO_ROOT / "tests", suffix=".py")
    hits = _find_bare_hits(paths)
    assert not hits, (
        "stale ST-removal reference(s) found in tests/ (must be bare "
        f"grep-zero, no exception): {hits}"
    )


def test_st_symbols_bare_grep_zero_in_packaging() -> None:
    """Tier 2: #3128 — ``pyproject.toml`` and ``uv.lock`` carry no ST
    symbol (the ``local-embed`` extra + its pinned deps were removed by
    PR-B; this pins that removal survives packaging drift)."""
    paths = [REPO_ROOT / "pyproject.toml", REPO_ROOT / "uv.lock"]
    hits = _find_bare_hits(paths)
    assert not hits, (
        f"stale ST-removal reference(s) found in packaging files: {hits}"
    )


# ---------------------------------------------------------------------------
# 2. Real-repo gate: docs — paragraph-scoped removal-marker co-occurrence
# ---------------------------------------------------------------------------


def test_st_symbols_in_docs_are_paragraph_scoped_marked() -> None:
    """Tier 2: #3128 — every ST-symbol mention left in docs (outside the
    excluded historical decisions/proposals dirs) co-occurs, in the same
    blank-line-delimited paragraph, with a removal-marker. A bare mention
    with no marker in its paragraph reads as a live (stale) claim, not a
    historical note, and fails this gate."""
    paths = _docs_scan_paths()
    violations = _find_unmarked_doc_paragraphs(paths)
    assert not violations, (
        "ST-symbol mention(s) in docs/ with no removal-marker in the same "
        f"paragraph (see module docstring for the two-tier docs policy): "
        f"{violations}"
    )


def test_decisions_and_proposals_dirs_are_excluded_from_the_docs_gate() -> None:
    """Tier 2: sanity — the two historical dirs are actually excluded from
    the scan set (not merely intended to be), so the exclusion in
    ``_docs_scan_paths`` is exercised, not vacuous."""
    scanned = set(_docs_scan_paths())
    for excluded_dir in DOCS_HISTORICAL_EXCLUDE_DIRS:
        for p in scanned:
            assert excluded_dir not in p.parents, (
                f"{p} should have been excluded via {excluded_dir}"
            )
    # And the exclusion isn't vacuous — at least one of the two dirs
    # actually contains an ST-symbol mention in the real repo, i.e. the
    # exclusion is doing live work, not skipping an empty directory.
    excluded_hits = 0
    for excluded_dir in DOCS_HISTORICAL_EXCLUDE_DIRS:
        for p in _iter_files(excluded_dir, suffix=".md"):
            text = _read_text(p) or ""
            if any(s in text for s in ST_SYMBOLS):
                excluded_hits += 1
    assert excluded_hits > 0, (
        "expected at least one ST-symbol mention under the excluded "
        "historical dirs in the real repo — if this is now 0, the "
        "exclusion may no longer be doing live work; re-verify before "
        "assuming it's still needed"
    )


# ---------------------------------------------------------------------------
# 3. Load-bearing proof: the scanners actually detect injected violations
# ---------------------------------------------------------------------------


def test_bare_scanner_is_load_bearing(tmp_path: Path) -> None:
    """Tier 2: strip/injection proof for ``_find_bare_hits`` — a synthetic
    file containing an ST symbol with NO marker at all must be caught
    (RED), and a clean synthetic file must not be (GREEN). Proves the
    real-repo gate above isn't vacuously passing because the scanner
    itself never matches anything."""
    dirty = tmp_path / "dirty.py"
    dirty.write_text(
        "# uses the " + "sentence" + "-transformers" + " package\n",
        encoding="utf-8",
    )
    clean = tmp_path / "clean.py"
    clean.write_text("# litellm-only embeddings\n", encoding="utf-8")

    assert _find_bare_hits([dirty]), "injected violation was not detected"
    assert not _find_bare_hits([clean]), "clean file falsely flagged"


def test_docs_paragraph_scanner_accepts_same_paragraph_marker(tmp_path: Path) -> None:
    """Tier 2: strip/injection proof — a multi-line paragraph where the
    removal-marker and the ST symbol are in DIFFERENT SENTENCES of the
    SAME paragraph (no blank line between them) must be accepted (GREEN).
    This is the exact shape of the real false-positive the architect
    found in universal-catalog.md: marker and symbol on different lines,
    same paragraph — a line-scoped checker would wrongly flag it."""
    doc = tmp_path / "note.md"
    symbol = "local" + "-mini"
    doc.write_text(
        "Some intro line about the default.\n"
        f"`{symbol}` was the old default (= a since-removed backend,\n"
        "see below).\n"
        "\n"
        "#3128 removed reyn's in-process backend entirely.\n",
        encoding="utf-8",
    )
    violations = _find_unmarked_doc_paragraphs([doc])
    assert not violations, (
        f"same-paragraph, different-line marker should have been accepted: {violations}"
    )


def test_docs_paragraph_scanner_rejects_different_paragraph_marker(tmp_path: Path) -> None:
    """Tier 2: strip/injection proof — the mirror case: when the marker is
    in a DIFFERENT paragraph (separated by a blank line) from the ST
    symbol mention, the mention must be flagged (RED). This proves the
    scanner is genuinely paragraph-scoped, not accidentally file-scoped
    (which would silently accept anything as long as a marker exists
    ANYWHERE in the document)."""
    doc = tmp_path / "note.md"
    symbol = "local" + "-mini"
    doc.write_text(
        f"`{symbol}` is still mentioned here with no nearby marker.\n"
        "\n"
        "Much later, in an unrelated paragraph, #3128 removed something else.\n",
        encoding="utf-8",
    )
    violations = _find_unmarked_doc_paragraphs([doc])
    assert violations, (
        "a marker in a different paragraph must not launder an unmarked "
        "mention — file-scoped matching would wrongly pass this"
    )


def test_docs_paragraph_scanner_rejects_bare_mention_with_no_marker_anywhere(
    tmp_path: Path,
) -> None:
    """Tier 2: strip/injection proof — the plain case: an ST symbol with no
    removal-marker anywhere in the file at all must be flagged (RED)."""
    doc = tmp_path / "note.md"
    symbol = "Sentence" + "Transformer"
    doc.write_text(f"Uses `{symbol}` for embeddings.\n", encoding="utf-8")
    violations = _find_unmarked_doc_paragraphs([doc])
    assert violations, "a bare unmarked mention must be flagged"
