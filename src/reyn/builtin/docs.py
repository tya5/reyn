"""Production-reachable read path for builtin-tier content.

Proposal 0060 Addendum D, D5b (+ #2913 follow-up). ``resolve_reyn_root()``
(``reyn.runtime.reyn_src.resolve_reyn_root``) raises ``RuntimeError`` in a
wheel install — there is no co-located ``pyproject.toml`` to anchor the walk,
so every ``docs/`` path built on top of it is unreachable in production. This
module is the production-safe alternative: it reads from the build-time
mirror at ``reyn.builtin.reference`` (``[tool.setuptools.package-data]``
``"builtin/**/*"``, F3a) via :mod:`importlib.resources`, which works
identically whether the package is an editable/dev checkout or an installed
wheel — no repo-root walk required.

**Dev-checkout fallback**: a plain (non-editable-build-hook) dev checkout may
not have run the build hook that populates ``src/reyn/builtin/reference/``
(``scripts/mirror_reference_docs.py``, wired into ``setup.py``'s custom
``build_py``) — the mirror directory is git-ignored, generated only at build
time. When the mirror is absent, this function falls back to
``resolve_reyn_root()`` + ``docs/reference/`` directly, which works in any
dev checkout (this is the ONLY caller allowed to treat that RuntimeError as
"docs unavailable" rather than letting it propagate — every other caller
should prefer this function over calling ``resolve_reyn_root()`` for docs
access).

**#2913 — builtin skill/pipeline BODY reads.** ``reyn.builtin.registry``'s
``BUILTIN_SKILLS``/``BUILTIN_PIPELINES`` entries carry a ``path`` computed
relative to THIS package's own on-disk location (``builtin/**/*``, F3a) —
outside ``project_root`` in every deploy, not just a wheel. The generic
``read_file`` op's ``_in_default_read_zone`` gate
(``reyn.security.permissions.permissions``) treats any out-of-project-root
path as "requires approval", which hard-fails non-interactively in
production (there is no operator to approve). :func:`read_builtin_body_bytes`
is the same ``importlib.resources`` idiom as :func:`read_builtin_doc`,
generalized to any path under this package directory (not just
``reference/``) — it lets the ``read_file`` op handler
(``reyn.core.op_runtime.file``) short-circuit the read-zone gate for
builtin-provenance body reads specifically, while leaving every operator
(non-builtin) file read on the unmodified ``_in_default_read_zone`` path.
"""
from __future__ import annotations

import importlib.resources as _resources
from pathlib import Path


class DocNotFoundError(FileNotFoundError):
    """The requested reference doc is not reachable via the builtin mirror
    NOR via a dev-checkout ``docs/reference/`` fallback."""


def read_builtin_doc(rel_path: str) -> str:
    """Read a reference doc by its ``docs/reference/``-relative path.

    Tries the wheel-packaged builtin mirror first (production-reachable, no
    repo root needed); falls back to the live ``docs/reference/`` tree via
    ``resolve_reyn_root()`` for a dev checkout that hasn't run the build hook.
    Raises :class:`DocNotFoundError` if neither resolves.
    """
    try:
        mirror_root = _resources.files("reyn.builtin") / "reference"
        candidate = mirror_root / rel_path
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        pass

    try:
        from reyn.runtime.reyn_src import resolve_reyn_root

        root = resolve_reyn_root()
    except RuntimeError as exc:
        raise DocNotFoundError(
            f"reference doc {rel_path!r} not reachable: no builtin mirror "
            "and no dev-checkout repo root"
        ) from exc

    fallback = root / "docs" / "reference" / rel_path
    if not fallback.is_file():
        raise DocNotFoundError(f"reference doc {rel_path!r} not found at {fallback}")
    return fallback.read_text(encoding="utf-8")


# The ONLY subdirectories of ``reyn.builtin`` whose files are legitimate L2
# body reads: ``reyn.builtin.registry``'s ``BUILTIN_SKILLS`` paths point at
# ``skills/<name>/SKILL.md`` and ``BUILTIN_PIPELINES`` paths at
# ``pipelines/<name>.yaml``. The ``reference/`` mirror is NOT here: it is read
# via :func:`read_builtin_doc` directly (never through the ``read_file`` op),
# so it needs no body-read bypass. Least-privilege (#2914 co-vet Ruling 1): a
# path resolving INSIDE the package but OUTSIDE these body dirs (e.g. a
# ``.py`` module) returns ``None`` and falls through to the normal read-zone
# gate — the bypass cannot be repurposed as an arbitrary-builtin-source read.
_BODY_READ_DIRS = frozenset({"skills", "pipelines"})


def read_builtin_body_bytes(path_str: str) -> "bytes | None":
    """Wheel-safe read of a builtin skill/pipeline BODY file (#2913).

    Returns the file's raw bytes via :mod:`importlib.resources` when
    *path_str* resolves to a file INSIDE one of the ``reyn.builtin`` package's
    BODY directories (``skills/`` or ``pipelines/`` — see ``_BODY_READ_DIRS``):
    it IS a builtin-provenance body (``reyn.builtin.registry`` is the only
    place that stamps such absolute paths). Returns ``None`` otherwise — a
    path NOT under ``reyn.builtin`` at all (an operator path), OR under the
    package but outside the body dirs (a ``.py`` module, the ``reference/``
    mirror, etc.). In every ``None`` case the caller
    (``reyn.core.op_runtime.file.handle``) falls through to the normal
    ``_in_default_read_zone``-gated file read, unchanged — the permission
    bypass is scoped to exactly the shipped body content, nothing else.
    """
    try:
        builtin_root = _resources.files("reyn.builtin")
    except ModuleNotFoundError:
        return None

    try:
        builtin_dir = Path(str(builtin_root)).resolve()
    except (OSError, ValueError):
        return None

    try:
        candidate = Path(path_str).expanduser().resolve()
    except OSError:
        return None

    try:
        rel = candidate.relative_to(builtin_dir)
    except ValueError:
        return None  # not under reyn.builtin — not a builtin body, let the normal gate handle it

    # Least-privilege scoping: inside the package but outside a body dir → gated.
    if not rel.parts or rel.parts[0] not in _BODY_READ_DIRS:
        return None

    resource = builtin_root
    for part in rel.parts:
        resource = resource / part
    try:
        if not resource.is_file():
            return None
        return resource.read_bytes()
    except (OSError, NotADirectoryError):
        return None
