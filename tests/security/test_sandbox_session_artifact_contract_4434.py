"""Tier 2: SandboxBackend.session_artifact_outside_write_scope contract (#4434
stage 1) — every concrete backend bears this contract (owner ruling: the
sandbox abstraction means every backend needs the abstract contract, not just
the one implementation that currently has real content to protect).

This file's job is the CROSS-backend piece two single-backend test files
(test_sandbox_seatbelt.py, test_landlock_exec_shim_1344e.py) cannot cover on
their own: that the full concrete-backend set is actually exercised, and that
a backend answering "vacuously True" (nothing on disk to protect) does so
self-consciously rather than by accident (e.g. the enumeration silently
shrinking to fewer backends, or a backend inheriting an unimplemented default
that happens to return something truthy).

#4439 CI (this arc's own PR) caught the census hand-typed here missing a 4th
implementer, ``DockerEnvironmentBackend`` — it lives under ``environment/``,
a DIFFERENT directory from the other 3 (``security/sandbox/``), so a
directory-scoped hand list silently excluded it. The fix is structural, not
"add the 4th name": the census below is derived from an AST scan of the
WHOLE ``src/reyn`` tree for any class defining every ``SandboxBackend``
method by name — a real, repo-wide source, not a hand-maintained list a 5th
implementer could miss the same way.
"""
from __future__ import annotations

import ast
import pathlib

from reyn.security.sandbox.backend import SandboxBackend
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy

_REPO_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "reyn"

# The exact SandboxBackend Protocol method names (backend.py) — a class
# defining every one of these, structurally, is a real implementer. This is
# an AST scan (no import/construction needed), so it also finds classes like
# DockerEnvironmentBackend whose __init__ requires real arguments.
_PROTOCOL_METHOD_NAMES = frozenset(
    {"available", "self_test", "wrap_command", "run", "session_artifact_outside_write_scope"},
)


def _scan_backend_class_locations() -> "list[tuple[str, str]]":
    """Return ``(relative_file_path, class_name)`` for every class under
    ``src/reyn`` that defines ALL of ``_PROTOCOL_METHOD_NAMES`` — excludes
    the Protocol definition itself (``SandboxBackend``, whose bases include
    ``Protocol``)."""
    found: "list[tuple[str, str]]" = []
    for path in sorted(_REPO_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            is_protocol_def = any("Protocol" in ast.dump(b) for b in node.bases)
            if is_protocol_def:
                continue
            method_names = {
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if _PROTOCOL_METHOD_NAMES.issubset(method_names):
                found.append((str(path.relative_to(_REPO_SRC)), node.name))
    return found


def test_the_concrete_backend_enumeration_has_not_silently_shrunk():
    """Tier 2: vacuity guard — the AST-derived census of every class under
    ``src/reyn`` structurally implementing the full SandboxBackend Protocol
    names exactly the 4 real implementers. If a future backend is added (or
    one is accidentally removed / fails to implement a method), this
    assertion is what goes RED — not a hand-typed list a 5th implementer in
    a new directory could miss the same way #4439's CI caught the 4th."""
    found = _scan_backend_class_locations()
    assert {cls_name for _path, cls_name in found} == {
        "SeatbeltBackend", "LandlockBackend", "NoopBackend", "DockerEnvironmentBackend",
    }


def test_every_scanned_backend_class_is_importable_and_conforms_to_the_protocol():
    """Tier 2: every class the AST scan found is actually importable from
    the module the scan found it in, and — where constructible with no
    arguments — satisfies the runtime-checkable SandboxBackend Protocol.
    Closes the gap a pure AST scan alone leaves open: a class could define
    all 5 method NAMES without their signatures actually matching the
    Protocol (isinstance() checks that structurally, AST scanning doesn't).
    ``DockerEnvironmentBackend`` requires real constructor args (container,
    repo_dir) — it's exempted from the isinstance leg here and covered
    directly by its own dedicated test below instead."""
    import importlib

    found = _scan_backend_class_locations()
    assert found, "AST scan found zero backend classes — collection itself is broken"

    for rel_path, cls_name in found:
        module_name = "reyn." + rel_path[:-3].replace("/", ".")
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
        if cls_name == "DockerEnvironmentBackend":
            continue  # covered by test_docker_backend_implements_the_contract below
        assert isinstance(cls(), SandboxBackend), (
            f"{module_name}.{cls_name} defines every Protocol method NAME but "
            "does not structurally satisfy SandboxBackend"
        )


def test_docker_backend_implements_the_contract():
    """Tier 2: DockerEnvironmentBackend (#4439 CI's own catch — see module
    docstring) answers session_artifact_outside_write_scope, vacuously True
    (docker exec never writes a policy-derived file to the host)."""
    from reyn.environment.container_backend import DockerEnvironmentBackend

    backend = DockerEnvironmentBackend(container="reyn-test-container", repo_dir="/repo")
    assert isinstance(backend, SandboxBackend)
    result = backend.session_artifact_outside_write_scope(SandboxPolicy(write_paths=["/repo"]))
    assert result is True


def test_every_backend_implements_the_contract_and_conforms_to_the_protocol():
    """Tier 2: every concrete backend (a) satisfies the runtime-checkable
    SandboxBackend Protocol (which now includes
    session_artifact_outside_write_scope) and (b) returns a real bool, not
    None/NotImplemented, for a representative policy — a backend that
    forgot to override the Protocol method would fail (a). Scoped to the 3
    no-arg-constructible backends; DockerEnvironmentBackend has its own
    dedicated test above."""
    policy = SandboxPolicy(write_paths=["/some/workspace"])
    for cls in (SeatbeltBackend, LandlockBackend, NoopBackend):
        backend = cls()
        assert isinstance(backend, SandboxBackend), f"{cls.__name__} breaks Protocol conformance"
        result = backend.session_artifact_outside_write_scope(policy)
        assert isinstance(result, bool), (
            f"{cls.__name__}.session_artifact_outside_write_scope() returned "
            f"{result!r}, not a bool"
        )


def test_only_seatbelt_answers_non_vacuously_today():
    """Tier 2: names which backend's answer is a REAL derivation from
    *policy* (Seatbelt: materialises a file, so relocating write_paths onto
    the cache dir must flip its answer) versus VACUOUSLY True regardless of
    *policy* (Landlock, Noop, Docker: nothing is ever written to disk, so no
    write_paths value can make their answer False today)."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_dir

    adversarial_policy = SandboxPolicy(write_paths=[str(_seatbelt_cache_dir())])
    assert SeatbeltBackend().session_artifact_outside_write_scope(adversarial_policy) is False

    # Landlock/Noop never touch disk, so even this adversarial policy (which
    # only means anything relative to SEATBELT's own cache dir) can't flip
    # them — their vacuous True is unconditional, which is exactly what
    # "vacuous" means here, not a gap in the check.
    assert LandlockBackend().session_artifact_outside_write_scope(adversarial_policy) is True
    assert NoopBackend().session_artifact_outside_write_scope(adversarial_policy) is True
