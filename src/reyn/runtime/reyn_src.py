"""``reyn_src_*`` resolver — read Reyn's own repository from inside.

Backs the ``reyn_src_list`` / ``reyn_src_read`` / ``reyn_src_glob`` /
``reyn_src_grep`` chat router tools. The resolver scopes paths to the
running Reyn install's repo root, so the agent can answer "how does
Reyn / how does Reyn's X work?" by reading the source / docs that the
user could equivalently view on GitHub.

Why a dedicated resolver instead of the generic ``file_read``:

  * **No permission gating needed.** Reyn's repo is public open-source
    content (= GitHub secret-scanning blocks credentials at push time,
    so nothing in the tree is sensitive). Operators don't configure
    this — it's an OS-internal capability.
  * **Naming clarity.** A generic ``doc/*`` op would collide with the
    user's own project documentation expectations; ``reyn_src_*`` is
    namespaced unambiguously to Reyn-the-project.
  * **Stable resolution.** Anchored deterministically without depending
    on the user's current working directory (see the dual-mode
    resolution below).

**Dual-mode resolution (proposal 0061 §3.2 — dev == wheel parity).**
``resolve_reyn_root()`` resolves in TWO modes:

  1. **Wheel mode** — detected by the presence of a ``_bundled/``
     directory adjacent to ``reyn.__file__`` (this is the PRIMARY
     signal, not "dev walk-up failed": a failure-fallback could
     mis-resolve a weird checkout to an unrelated ``name = "reyn"``
     tree, a confused-deputy risk). ``README.md`` / ``CHANGELOG.md`` /
     ``docs/`` are shipped into the wheel under ``<pkg>/_bundled/`` via
     Hatchling ``force-include`` (``pyproject.toml``
     ``[tool.hatch.build.targets.wheel.force-include]``); the package's
     own Python source already lives directly under ``<pkg>/``. Root =
     the installed package directory.
  2. **Dev mode** (unchanged from the original MVP) — walks up from
     ``reyn.__file__`` for a co-located ``pyproject.toml`` whose content
     references Reyn. Root = the repo root.

**Single logical namespace, two physical layouts (0061 §3.2/§3.3).** Both
modes present the SAME repo-relative logical paths — ``README.md``,
``CHANGELOG.md``, ``docs/<x>``, and (the pinned canonical prefix,
0061 §7) ``src/reyn/<x>`` for source. ``_translate_logical_to_physical``
maps a logical path to its on-disk location under the resolved root; it
is a no-op in dev mode (the dev checkout root already has this exact
shape) and only does work in wheel mode. The **reachable set** is a
single SSoT (``REACHABLE_TOP_LEVEL_ENTRIES`` below) declaring exactly
``{README.md, CHANGELOG.md, docs, src}`` — owner-approved core-only
scope (0061 §3.3 option (A)). Any path outside this set is refused in
BOTH modes: a non-declared dev-only path (``pyproject.toml``, ``tests/``,
``scripts/``, ...) is no longer reachable, matching "equivalent to
absent in the wheel" — the dev==wheel parity invariant this proposal
exists to establish. The wheel-side half of the same SSoT drives
``pyproject.toml``'s ``force-include`` map (kept in literal sync; see
``tests/test_0061_repo_self_access_parity.py``, which fails if the two
drift).

P7-clean: this module is OS infrastructure; it carries no
domain-specific strings.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Maximum file size returned by reyn_src_read. Reyn's docs/source files
# are well under this; the cap keeps a malicious / accidental binary
# from blowing up the LLM context. ~256 KB ≈ 50 K tokens worst case.
_MAX_READ_BYTES = 256 * 1024

# 0061 §3.3 — the single reachable-set SSoT. Every top-level logical
# entry reyn_src_* will resolve into, in EITHER mode. Owner sign-off
# (2026-07-13, proposal 0061 §3.3 option (A) — core set only):
# `pyproject.toml` / `CLAUDE.md` / `tests/` / `scripts/` / `dogfood/` /
# `pipelines/` / `website/` are ACCEPTED AS EXCLUDED from reyn_src in
# BOTH modes going forward — the primary self-explanation surface
# (source + docs + README) is preserved; this is a deliberate narrowing
# of dev's prior "whole repo" reach (not a bug).
REACHABLE_TOP_LEVEL_ENTRIES: tuple[str, ...] = ("README.md", "CHANGELOG.md", "docs", "src")

# The subset of the reachable set that is force-included into the wheel
# via `pyproject.toml`'s `[tool.hatch.build.targets.wheel.force-include]`
# (`src` isn't listed there because Hatchling's `packages = ["src/reyn"]`
# already ships the package tree by its own, separate mechanism — see
# `tests/test_0061_repo_self_access_parity.py`, which cross-checks BOTH
# halves of this SSoT against `pyproject.toml` so they cannot drift).
FORCE_INCLUDE_ENTRIES: tuple[str, ...] = ("README.md", "CHANGELOG.md", "docs")

# The pinned canonical logical prefix for source (0061 §7 / sequencing
# step 2 — fixed BEFORE the parity gate, since its "same logical path"
# assert depends on it): `src/reyn/`. Matches the existing dev checkout
# layout 1:1; in wheel mode this prefix is stripped because the
# installed package directory already IS what "src/reyn/" names in dev.
SOURCE_LOGICAL_PREFIX = "src/reyn"

# The on-disk directory name Hatchling's `force-include` maps
# README/CHANGELOG/docs into inside the installed package (see
# `pyproject.toml`). Wheel-mode detection keys off this directory's
# presence, adjacent to `reyn.__file__` — the PRIMARY signal (0061 §3.2),
# not "walk-up failed".
_BUNDLED_DIR_NAME = "_bundled"


def _is_wheel_root(root: Path) -> bool:
    """True when ``root`` is a wheel install's package directory.

    Detected by the presence of ``_bundled/`` adjacent to
    ``reyn.__file__`` (0061 §3.2) — never by "dev walk-up failed" (a
    failure-fallback could mis-resolve a weird checkout to an unrelated
    ``name = "reyn"``-ish tree, a confused-deputy risk).
    """
    return (root / _BUNDLED_DIR_NAME).is_dir()


@lru_cache(maxsize=1)
def resolve_reyn_root() -> Path:
    """Return the repository (or, in a wheel install, package) root.

    **Wheel mode** (0061 §3.2): if ``<pkg>/_bundled/`` exists adjacent to
    ``reyn.__file__``, the running install is a wheel that force-included
    README/CHANGELOG/docs — return the installed package directory
    itself. This is checked FIRST and is the primary signal (not a
    walk-up-failure fallback).

    **Dev mode** (original MVP, unchanged): walks up from
    ``reyn.__file__`` (= ``src/reyn/__init__.py`` in a dev install) until
    a ``pyproject.toml`` is found AND its content references Reyn (= a
    line containing ``name = "reyn"`` or similar). The ``name`` check is
    what distinguishes a dev install (= our repo, has a Reyn-named
    pyproject) from an unrelated checkout.

    Raises ``RuntimeError`` when neither mode resolves (= a wheel
    install with no ``_bundled/`` — e.g. a pre-0061 wheel — and no
    co-located dev repo). Cached because the answer is process-stable.
    """
    import reyn
    pkg_init = Path(reyn.__file__).resolve()
    pkg_dir = pkg_init.parent

    if _is_wheel_root(pkg_dir):
        return pkg_dir

    # Walk up. Stop at the filesystem root.
    for ancestor in [pkg_dir, *pkg_dir.parents]:
        candidate = ancestor / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        # Heuristic identity check: does the pyproject declare Reyn?
        # Both ``name = "reyn"`` and ``name="reyn"`` (no spaces) match.
        if 'name = "reyn"' in content or 'name="reyn"' in content:
            return ancestor.resolve()
    raise RuntimeError(
        "reyn_src_*: no Reyn repository root found above "
        f"{pkg_init}, and no wheel `_bundled/` directory found "
        "adjacent to it either. This op needs a development install "
        "(= `pip install -e \".[dev]\"` from a clone of "
        "github.com/tya5/reyn) or a 0061-or-later wheel install "
        "(= `pip install reyn`)."
    )


def _reachable_top_level_segment(cleaned: str) -> str:
    """First path segment of a cleaned logical path, or ``""`` for the root."""
    if not cleaned or cleaned == ".":
        return ""
    return cleaned.split("/", 1)[0]


def _translate_logical_to_physical(cleaned: str) -> str:
    """Map a logical repo-relative path to its on-disk path under ``root``.

    No-op in every case that matters for dev mode — the dev checkout
    root already HAS this exact shape (``README.md`` / ``docs/`` / the
    ``SOURCE_LOGICAL_PREFIX`` all live at the paths their logical name
    implies). Only wheel mode needs real translation: README/CHANGELOG/
    docs live under ``_bundled/`` (Hatchling ``force-include``); source
    lives directly under the package root with ``SOURCE_LOGICAL_PREFIX``
    stripped (the installed package directory already IS what
    ``src/reyn/`` names in dev). Callers only invoke this after
    confirming ``_is_wheel_root(root)`` — see ``safe_resolve_inside``.
    """
    if cleaned in ("", "."):
        return ""
    if cleaned in ("README.md", "CHANGELOG.md"):
        return f"{_BUNDLED_DIR_NAME}/{cleaned}"
    if cleaned == "docs" or cleaned.startswith("docs/"):
        return f"{_BUNDLED_DIR_NAME}/{cleaned}"
    if cleaned == SOURCE_LOGICAL_PREFIX or cleaned == "src":
        return ""
    prefix = SOURCE_LOGICAL_PREFIX + "/"
    if cleaned.startswith(prefix):
        return cleaned[len(prefix):]
    # Not a declared logical path. The reachable-set gate in
    # `safe_resolve_inside` already refuses non-declared top segments
    # before this function runs, so this branch is unreachable in
    # practice — a defensive identity fallback, not a silent bypass.
    return cleaned


def safe_resolve_inside(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``root`` and refuse if it escapes.

    Returns the absolute resolved path on success. Raises ``ValueError``
    when: the top-level segment isn't in the declared reachable set
    (0061 §3.3 — the dev/wheel parity gate; a non-declared path like
    ``tests/foo`` is refused in BOTH modes, "equivalent to absent in the
    wheel"); the input contains a path-traversal escape (= ``..``) that
    lands outside ``root``; or the resolved target doesn't exist.

    ``rel_path`` of ``""`` resolves to ``root`` itself (= "list the
    repo top-level"). Leading slashes are stripped so a forgetful LLM
    that calls ``reyn_src_read("/README.md")`` still works.

    In wheel mode (0061 §3.2), ``cleaned`` (the LOGICAL path) is
    translated to its physical on-disk location under ``root`` via
    ``_translate_logical_to_physical`` before the escape/existence
    checks — dev mode is untouched (translation is a no-op there, since
    the dev checkout root already has the logical shape).
    """
    cleaned = (rel_path or "").lstrip("/")
    top = _reachable_top_level_segment(cleaned)
    if top and top not in REACHABLE_TOP_LEVEL_ENTRIES:
        raise ValueError(
            f"reyn_src: path {rel_path!r} is outside the reachable set "
            f"{REACHABLE_TOP_LEVEL_ENTRIES} (proposal 0061 §3.3); refusing."
        )
    physical_rel = _translate_logical_to_physical(cleaned) if _is_wheel_root(root) else cleaned
    candidate = (root / physical_rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValueError(
            f"reyn_src: path {rel_path!r} resolves outside the Reyn "
            "repository root; refusing."
        ) from None
    if not candidate.exists():
        raise ValueError(
            f"reyn_src: path {rel_path!r} does not exist in the Reyn "
            "repository."
        )
    return candidate


def list_entries(root: Path, target: Path, path_arg: str) -> dict:
    """Build the ``reyn_src_list`` result dict for ``target``."""
    if not target.is_dir():
        return {
            "error": (
                f"reyn_src_list: {path_arg!r} is not a directory. "
                "Use reyn_src_read to read a file."
            ),
        }
    if target.resolve() == root.resolve():
        # Top-level listing: present the declared reachable set (0061
        # §3.3) canonically in BOTH modes. A raw `target.iterdir()` here
        # would show dev's whole-repo top level (pyproject.toml, tests/,
        # scripts/, ...) in dev mode, or the wheel's on-disk layout
        # (_bundled/, individual package modules) in wheel mode — neither
        # matches the single logical namespace this op presents.
        entries = []
        for name in REACHABLE_TOP_LEVEL_ENTRIES:
            try:
                resolved = safe_resolve_inside(root, name)
            except ValueError:
                continue
            entries.append({"name": name, "type": "dir" if resolved.is_dir() else "file"})
        return {"path": "", "entries": entries}
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        # Skip hidden entries that aren't relevant (= .git, .reyn,
        # __pycache__, .pytest_cache) plus, in wheel mode, `_bundled`
        # itself when it shows up as a sibling of package modules (e.g.
        # listing the bare "src" logical path) — it is reached
        # explicitly via the README/CHANGELOG/docs logical paths, never
        # as an incidental listing entry.
        if child.name in {
            ".git", ".reyn", ".github", ".claude", ".pytest_cache",
            ".ruff_cache", ".mypy_cache", "__pycache__", "venv", ".venv",
            "site", "build", "dist", "node_modules", _BUNDLED_DIR_NAME,
        }:
            continue
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
        })
    # Echo the caller's own (logical) path argument back — mirrors
    # read_text's convention — so the result is stable across dev/wheel
    # even though the physical `target` may differ (0061 §3.2).
    display_path = "" if path_arg in ("", ".") else path_arg
    return {"path": display_path, "entries": entries}


def read_text(
    target: Path,
    path_arg: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> dict:
    """Build the ``reyn_src_read`` result dict for ``target``.

    When ``offset`` or ``limit`` is provided, the file is line-streamed
    so the 256-KB byte cap is bypassed: only the requested slice is
    materialised, and a giant file can be partially read without an
    error. Without a slice, the cap applies as before to keep an
    accidental binary / generated artifact from blowing up LLM context.
    """
    if target.is_dir():
        return {
            "error": (
                f"reyn_src_read: {path_arg!r} is a directory. Use "
                "reyn_src_list to list its entries."
            ),
        }
    try:
        size = target.stat().st_size
    except OSError as exc:
        return {"error": f"reyn_src_read: stat failed: {exc}"}
    sliced = offset is not None or limit is not None
    if not sliced and size > _MAX_READ_BYTES:
        return {
            "error": (
                f"reyn_src_read: {path_arg!r} is {size} bytes, "
                f"larger than the {_MAX_READ_BYTES}-byte cap. Read a "
                "smaller file, pass `offset` / `limit` to slice it, "
                "or list its directory first."
            ),
        }
    try:
        if sliced:
            start = max(0, offset or 0)
            end = (start + limit) if limit is not None else None
            collected: list[str] = []
            with target.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < start:
                        continue
                    if end is not None and i >= end:
                        break
                    collected.append(line)
            content = "".join(collected)
        else:
            content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "error": (
                f"reyn_src_read: {path_arg!r} is not UTF-8 text. "
                "Only text files are supported."
            ),
        }
    except OSError as exc:
        return {"error": f"reyn_src_read: read failed: {exc}"}
    return {"path": path_arg, "content": content}


_MAX_GLOB_MATCHES = 200
_MAX_GREP_RESULTS = 50
_GREP_SNIPPET_CHARS = 200
# Same skip-set the listing path applies (= canonical exclusion for
# noise / build artifacts). Used by both glob and grep so the surfaces
# are uniformly "Reyn source as a human reader would see it".
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git", ".reyn", ".github", ".claude", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "__pycache__", "venv", ".venv",
    "site", "build", "dist", "node_modules",
})


def _iter_files_under(root: Path):
    """Yield files under ``root``, skipping noise dirs.

    Walks ``root`` recursively with ``Path.rglob`` then filters out any
    file whose ancestry includes a name in ``_SKIP_DIR_NAMES``. Matches
    `list_entries`'s skip discipline so glob / grep results don't include
    things a `reyn_source__list` browse wouldn't.
    """
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip if any ancestor dir name is in the skip set.
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIR_NAMES for part in rel_parts):
            continue
        yield p


def glob_entries(root: Path, pattern: str) -> dict:
    """Build the ``reyn_src_glob`` result dict.

    Returns ``{pattern, matches: [str, ...], count: int}`` where each
    match is a repo-root-relative path. Capped at ``_MAX_GLOB_MATCHES``
    so a careless ``**`` doesn't blow up the LLM context.
    """
    cleaned = (pattern or "").strip()
    if not cleaned:
        return {"error": "reyn_src_glob: pattern must be non-empty."}
    matches: list[str] = []
    try:
        for p in root.glob(cleaned):
            if not p.is_file():
                continue
            try:
                rel_parts = p.relative_to(root).parts
            except ValueError:
                continue
            if any(part in _SKIP_DIR_NAMES for part in rel_parts):
                continue
            matches.append(str(p.relative_to(root)))
            if len(matches) >= _MAX_GLOB_MATCHES:
                break
    except (ValueError, OSError) as exc:
        return {"error": f"reyn_src_glob: pattern {pattern!r} failed: {exc}"}
    matches.sort()
    return {"pattern": pattern, "matches": matches, "count": len(matches)}


def grep_entries(
    root: Path,
    pattern: str,
    path: str = "",
    glob: str | None = None,
    case_sensitive: bool = False,
    max_results: int = _MAX_GREP_RESULTS,
) -> dict:
    """Build the ``reyn_src_grep`` result dict.

    Returns ``{pattern, matches: [{path, line, snippet}, ...], count: int,
    truncated: bool}``. ``path`` scopes the search to a sub-tree (default
    repo root). ``glob`` filters which files are searched (default = all
    text files under scope).
    """
    import re

    if not pattern:
        return {"error": "reyn_src_grep: pattern must be non-empty."}
    try:
        compiled = re.compile(
            pattern,
            flags=0 if case_sensitive else re.IGNORECASE,
        )
    except re.error as exc:
        return {"error": f"reyn_src_grep: invalid regex {pattern!r}: {exc}"}

    try:
        scope_root = safe_resolve_inside(root, path)
    except ValueError as exc:
        return {"error": str(exc)}

    # Resolve which files to scan
    if scope_root.is_file():
        candidates = [scope_root]
    elif glob:
        try:
            candidates = [
                p for p in scope_root.glob(glob)
                if p.is_file()
                and not any(part in _SKIP_DIR_NAMES for part in p.relative_to(root).parts)
            ]
        except (ValueError, OSError) as exc:
            return {"error": f"reyn_src_grep: glob {glob!r} failed: {exc}"}
    else:
        candidates = list(_iter_files_under(scope_root))

    matches: list[dict] = []
    truncated = False
    for fp in candidates:
        if len(matches) >= max_results:
            truncated = True
            break
        # Skip files larger than the read cap — same discipline as read_text.
        try:
            if fp.stat().st_size > _MAX_READ_BYTES:
                continue
        except OSError:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                snippet = line.strip()[:_GREP_SNIPPET_CHARS]
                matches.append({
                    "path": str(fp.relative_to(root)),
                    "line": line_no,
                    "snippet": snippet,
                })
                if len(matches) >= max_results:
                    truncated = True
                    break

    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
    }


__all__ = [
    "resolve_reyn_root",
    "safe_resolve_inside",
    "list_entries",
    "read_text",
    "glob_entries",
    "grep_entries",
    "REACHABLE_TOP_LEVEL_ENTRIES",
    "FORCE_INCLUDE_ENTRIES",
    "SOURCE_LOGICAL_PREFIX",
]
