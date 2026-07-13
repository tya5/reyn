"""Production-reachable read path for builtin-tier BODY content.

Proposal 0060 Addendum D (+ #2913 follow-up). This module used to also carry
a reference-*doc* reader (``read_builtin_doc``, Addendum D5b) that worked
around setuptools' "package data must live inside the package dir" limitation
via a build-time copytree mirror (``scripts/mirror_reference_docs.py`` into
the git-ignored ``src/reyn/builtin/reference/``). Proposal 0061 supersedes
that mechanism entirely: the Hatchling build backend's
``force-include`` (``pyproject.toml``
``[tool.hatch.build.targets.wheel.force-include]``) ships README/CHANGELOG/
all of ``docs/`` into every wheel directly, and ``reyn.runtime.reyn_repo``'s
dual-mode ``resolve_reyn_root()`` reaches them in both dev and wheel installs
— ``read_builtin_doc`` had zero production callers (only test/smoke) and is
retired along with the mirror it depended on. See
``docs/deep-dives/proposals/0061-repo-self-access-and-packaging-standardization.md``
§3.5.

**#2913 — builtin skill/pipeline BODY reads (kept, LIVE, untouched by 0061).**
``reyn.builtin.registry``'s ``BUILTIN_SKILLS``/``BUILTIN_PIPELINES`` entries
carry a ``path`` computed relative to THIS package's own on-disk location
(``builtin/**/*``, F3a) — outside ``project_root`` in every deploy, not just a
wheel. The generic ``read_file`` op's ``_in_default_read_zone`` gate
(``reyn.security.permissions.permissions``) treats any out-of-project-root
path as "requires approval", which hard-fails non-interactively in
production (there is no operator to approve). :func:`read_builtin_body_bytes`
reads via :mod:`importlib.resources`, generalized to any path under this
package directory (not just ``reference/``) — it lets the ``read_file`` op
handler (``reyn.core.op_runtime.file``) short-circuit the read-zone gate for
builtin-provenance body reads specifically, while leaving every operator
(non-builtin) file read on the unmodified ``_in_default_read_zone`` path.
This is the ``read`` op's LIVE production path (``file.py:214``) — 0061 does
not touch it.
"""
from __future__ import annotations

import importlib.resources as _resources
from pathlib import Path

# The ONLY subdirectories of ``reyn.builtin`` whose files are legitimate L2
# body reads: ``reyn.builtin.registry``'s ``BUILTIN_SKILLS`` paths point at
# ``skills/<name>/SKILL.md`` and ``BUILTIN_PIPELINES`` paths at
# ``pipelines/<name>.yaml``. Least-privilege (#2914 co-vet Ruling 1): a
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
    package but outside the body dirs (a ``.py`` module, etc.). In every
    ``None`` case the caller (``reyn.core.op_runtime.file.handle``) falls
    through to the normal ``_in_default_read_zone``-gated file read,
    unchanged — the permission bypass is scoped to exactly the shipped body
    content, nothing else.
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
