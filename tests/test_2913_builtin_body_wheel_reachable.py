"""Tier 2: OS invariant — proposal 0060 F3 follow-up #2913.

Co-vet pins:

  1. **Wheel-layout reachability.** A builtin skill's body (``BUILTIN_SKILLS``'s
     ``reyn_cheat_sheet`` entry, ``reyn.builtin.registry``) is readable through
     the real ``read_file`` op (``reyn.core.op_runtime.file.handle``) even when
     the ``PermissionResolver``'s ``project_root`` is a directory that does
     NOT contain the builtin package (= the fact of a wheel install: the
     package lives in site-packages, physically outside any given project's
     ``project_root``). Prior to #2913 this hard-failed with a
     ``PermissionError`` from ``_in_default_read_zone`` (confirmed defect,
     issue #2913 / architect co-vet on #2912).
  2. **Falsify.** Stripping the ``importlib.resources`` routing (reverting to
     the plain ``ctx.workspace.read_file_bytes`` path for a builtin body, by
     monkeypatching ``read_builtin_body_bytes`` back to always return
     ``None``) makes the SAME read hit the out-of-root gate and raise
     ``PermissionError`` — proving the wheel-layout test above is exercising
     the new routing, not some other path that "just works".
  3. **No security regression.** An operator (non-builtin) file outside the
     read zone is still denied by the unmodified ``_in_default_read_zone``
     gate — the builtin short-circuit does not widen the gate for anything
     else.
  4. **Least-privilege scoping (#2914 co-vet Ruling 1).** A path that resolves
     INSIDE the ``reyn.builtin`` package but OUTSIDE the body dirs
     (``skills/``/``pipelines/``) — e.g. ``reyn/builtin/registry.py`` — returns
     ``None`` from ``read_builtin_body_bytes`` (NOT bytes), so it goes through
     the normal read gate, not the bypass. Falsify: widen the check back to
     "any path under the package" and this test shows bytes returned = the
     over-broad bypass.

No mocks: real ``PermissionResolver`` + real ``OpContext`` + the real
``handle()`` op dispatch, exactly the harness ``test_op_runtime_file_permissions.py``
already established for this module.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.builtin import registry as builtin_registry
from reyn.builtin.docs import read_builtin_body_bytes
from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.events.events import EventLog
from reyn.core.op_runtime import file as file_mod
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_CHEAT_SHEET_PATH = BUILTIN_SKILLS["reyn_cheat_sheet"]["path"]


def _make_ctx(*, permission_resolver: PermissionResolver | None) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=permission_resolver,
        actor="test_skill",
    )


def _resolver(project_root: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={},
        project_root=project_root,
        interactive=False,
    )


def _run(coro):
    return asyncio.run(coro)


def _read_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def test_builtin_skill_body_readable_with_project_root_elsewhere(tmp_path, monkeypatch):
    """Tier 2: simulated wheel layout — project_root has NOTHING to do with the
    installed package location (the real fact in a pip-installed deploy: the
    package lives in site-packages, not under the user's project). The
    cheat-sheet body still reads successfully through the real read_file op."""
    monkeypatch.chdir(tmp_path)
    unrelated_root = tmp_path / "unrelated_project"
    unrelated_root.mkdir()
    resolver = _resolver(unrelated_root)
    ctx = _make_ctx(permission_resolver=resolver)

    assert not str(Path(_CHEAT_SHEET_PATH).resolve()).startswith(
        str(unrelated_root.resolve())
    ), "fixture invariant: the builtin path must genuinely be outside project_root"

    result = _run(handle(_read_op(_CHEAT_SHEET_PATH), ctx))

    assert result["status"] == "ok", result
    # Behavioral assertion (not a golden pin): the REAL SKILL.md content, via a
    # distinctive marker from its own front-matter — not exact length/formatting.
    assert "reyn_cheat_sheet" in result["content"]
    assert "description:" in result["content"]


def test_falsify_stripped_routing_hits_out_of_root_permission_error(tmp_path, monkeypatch):
    """Tier 2: (falsify) with the importlib.resources routing stripped — simulating
    a revert to the plain `ctx.workspace.read_file_bytes` path — the SAME builtin
    body read now hits the out-of-project-root gate and raises PermissionError.
    Proves the GREEN test above is actually exercising the new routing."""
    monkeypatch.chdir(tmp_path)
    unrelated_root = tmp_path / "unrelated_project"
    unrelated_root.mkdir()
    resolver = _resolver(unrelated_root)
    ctx = _make_ctx(permission_resolver=resolver)

    monkeypatch.setattr(file_mod, "read_builtin_body_bytes", lambda path_str: None)

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_read_op(_CHEAT_SHEET_PATH), ctx))


def test_non_body_package_path_returns_none_not_bypassed(tmp_path, monkeypatch):
    """Tier 2: least-privilege scoping — a path INSIDE the reyn.builtin package
    but OUTSIDE the body dirs (skills/ + pipelines/), e.g. the package's own
    registry.py module, returns None from read_builtin_body_bytes (so the read
    would go through the NORMAL gate, not the bypass). Guards against the
    over-broad "any path under the package" bypass the co-vet flagged."""
    non_body_path = builtin_registry.__file__  # reyn/builtin/registry.py — a .py, not a body
    assert Path(non_body_path).is_file(), "fixture: the package module must exist on disk"

    # It IS inside the package dir (so the containment check alone would pass) ...
    import importlib.resources as _res
    pkg_dir = Path(str(_res.files("reyn.builtin"))).resolve()
    assert str(Path(non_body_path).resolve()).startswith(str(pkg_dir)), (
        "fixture: registry.py must live inside the reyn.builtin package dir"
    )
    assert Path(non_body_path).resolve().parent == pkg_dir, (
        "fixture: registry.py must sit at the package root, outside skills/ or pipelines/"
    )

    # ... but it is NOT a body file → the helper returns None (falls through to the gate).
    assert read_builtin_body_bytes(non_body_path) is None
    # Sanity contrast: a genuine body path DOES return bytes (the bypass still works
    # for what it's scoped to — this is not a vacuous None-for-everything result).
    assert read_builtin_body_bytes(_CHEAT_SHEET_PATH) is not None


def test_operator_file_outside_zone_still_denied(tmp_path, monkeypatch):
    """Tier 2: no security regression — an ordinary (non-builtin) file outside
    project_root is still denied by the unmodified _in_default_read_zone gate.
    The builtin short-circuit only ever fires for a path under reyn.builtin;
    an operator file living elsewhere on disk never matches it."""
    monkeypatch.chdir(tmp_path)
    resolver = _resolver(tmp_path)
    ctx = _make_ctx(permission_resolver=resolver)

    operator_file = tmp_path.parent / "operator_secret.txt"
    operator_file.write_text("not a builtin body")

    with pytest.raises(PermissionError, match="read from"):
        _run(handle(_read_op(str(operator_file)), ctx))
