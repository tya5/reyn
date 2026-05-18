"""``reyn_src_*`` resolver — read Reyn's own repository from inside.

Backs the ``reyn_src_list`` / ``reyn_src_read`` chat router tools. The
resolver scopes paths to the running Reyn install's repo root, so the
agent can answer "how does Reyn / how does Reyn's X work?" by reading
the source / docs that the user could equivalently view on GitHub.

Why a dedicated resolver instead of the generic ``file_read``:

  * **No permission gating needed.** Reyn's repo is public open-source
    content (= GitHub secret-scanning blocks credentials at push time,
    so nothing in the tree is sensitive). Operators don't configure
    this — it's an OS-internal capability.
  * **Naming clarity.** A generic ``doc/*`` op would collide with the
    user's own project documentation expectations; ``reyn_src_*`` is
    namespaced unambiguously to Reyn-the-project.
  * **Stable resolution.** Walks up from the running ``reyn`` package
    until ``pyproject.toml`` is found, anchoring to the repo
    deterministically without depending on the user's current working
    directory.

MVP scope: development install (= ``pip install -e .`` from a clone or
the source itself). Wheel install (= post-PyPI) doesn't bundle README /
docs / cookbook outside ``src/reyn/`` by default; resolving from the
package install dir would only see the Python source. That extension is
tracked separately as a packaging-side change (``MANIFEST.in`` /
``package-data``) — not part of this MVP.

P7-clean: this module is OS infrastructure; it carries no
skill-specific strings.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Maximum file size returned by reyn_src_read. Reyn's docs/source files
# are well under this; the cap keeps a malicious / accidental binary
# from blowing up the LLM context. ~256 KB ≈ 50 K tokens worst case.
_MAX_READ_BYTES = 256 * 1024


@lru_cache(maxsize=1)
def resolve_reyn_root() -> Path:
    """Return the repository root of the running Reyn install.

    Walks up from ``reyn.__file__`` (= ``src/reyn/__init__.py`` in dev
    install, ``site-packages/reyn/__init__.py`` in wheel install) until
    a ``pyproject.toml`` is found AND its content references Reyn (= a
    line containing ``name = "reyn"`` or similar). The ``name`` check
    is what distinguishes a dev install (= our repo, has Reyn-named
    pyproject) from a wheel install in someone else's checkout (= their
    pyproject, no name match).

    Raises ``RuntimeError`` when no Reyn repo root can be resolved
    (= wheel install with no co-located source). Cached because the
    answer is process-stable.
    """
    import reyn
    pkg_init = Path(reyn.__file__).resolve()
    # Walk up. Stop at the filesystem root.
    for ancestor in [pkg_init.parent, *pkg_init.parents]:
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
        f"{pkg_init}. This op needs a development install "
        "(= `pip install -e \".[dev]\"` from a clone of "
        "github.com/tya5/reyn). Wheel-install support is tracked as a "
        "packaging follow-up."
    )


def safe_resolve_inside(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``root`` and refuse if it escapes.

    Returns the absolute resolved path on success. Raises ``ValueError``
    when the input contains a path-traversal escape (= ``..``) that
    lands outside ``root``, when the input is an absolute path, or when
    the resolved target doesn't exist.

    ``rel_path`` of ``""`` resolves to ``root`` itself (= "list the
    repo top-level"). Leading slashes are stripped so a forgetful LLM
    that calls ``reyn_src_read("/README.md")`` still works.
    """
    cleaned = (rel_path or "").lstrip("/")
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
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
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        # Skip hidden entries that aren't relevant (= .git, .reyn,
        # __pycache__, .pytest_cache). The user can list them explicitly
        # by descending into the parent path; this default keeps the
        # top-level listing readable.
        if child.name in {
            ".git", ".reyn", ".github", ".claude", ".pytest_cache",
            ".ruff_cache", ".mypy_cache", "__pycache__", "venv", ".venv",
            "site", "build", "dist", "node_modules",
        }:
            continue
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
        })
    # Show path relative to root (= what the user passed) so the LLM
    # can compose follow-up calls without re-deriving paths.
    try:
        rel = str(target.relative_to(root))
    except ValueError:
        rel = path_arg
    return {"path": rel if rel != "." else "", "entries": entries}


def read_text(target: Path, path_arg: str) -> dict:
    """Build the ``reyn_src_read`` result dict for ``target``."""
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
    if size > _MAX_READ_BYTES:
        return {
            "error": (
                f"reyn_src_read: {path_arg!r} is {size} bytes, "
                f"larger than the {_MAX_READ_BYTES}-byte cap. Read a "
                "smaller file or list its directory first."
            ),
        }
    try:
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
    things a `reyn.source__list` browse wouldn't.
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
]
