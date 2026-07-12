#!/usr/bin/env python3
"""In-venv probe for the dev==wheel repo-self-access parity gate (0061 §3.4).

Run by ``scripts/wheel_reachability_smoke.py`` INSIDE a throwaway venv that has
ONLY the built wheel installed (no ``-e``, no dev-source tree on ``sys.path``),
via ``<venv>/bin/python scripts/wheel_parity_probe.py``. It is a normal
committed ``.py`` file — NOT a generated / string-templated sub-script and NOT
handed to ``python -c``. **No file content is ever embedded in this source:**
both sides of every byte-identity comparison are read at RUNTIME —

  * the WHEEL side through the venv-installed ``reyn.runtime.reyn_src`` resolver,
  * the DEV side by reading the dev-checkout file directly, from the path passed
    in via the ``REYN_DEV_REPO_ROOT`` env var.

That runtime-read design (vs. baking ``repr(README_bytes)`` into a script) is
deliberate: README/docs contain quotes / parens / em-dashes that break a
generated-source literal with a ``SyntaxError`` in a clean CI env.

``import reyn`` here resolves to the venv's installed wheel (this script's own
directory — ``scripts/`` — is ``sys.path[0]`` and contains no ``reyn`` package),
and the dev-mask guard below hard-asserts that fact so a source-tree leak
false-passes nothing.

Exits 0 iff every check passes; exits 1 (with a ``[PASS]``/``[FAIL]`` line per
check) on any assertion failure — reporting every failure, not stopping early.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEV_ROOT = Path(os.environ["REYN_DEV_REPO_ROOT"]).resolve()

# Logical paths compared byte-for-byte between the wheel-mode reyn_src read and
# a direct read of the dev checkout. Each is a declared reachable-set path
# (README at root, a docs/ file, a src/reyn/ source file).
_BYTE_IDENTITY_LOGICAL_PATHS = [
    ("README.md", _DEV_ROOT / "README.md"),
    ("docs/index.md", _DEV_ROOT / "docs" / "index.md"),
    ("src/reyn/runtime/reyn_src.py", _DEV_ROOT / "src" / "reyn" / "runtime" / "reyn_src.py"),
]

_CHECK_NAMES = [
    "dev-mask guard (reyn.__file__ under venv, not source tree)",
    "resolve_reyn_root: wheel mode detected (_bundled/ present)",
    "reyn_src read: README.md byte-identical to dev checkout",
    "reyn_src read: docs/index.md byte-identical to dev checkout",
    "reyn_src read: src/reyn/runtime/reyn_src.py byte-identical to dev checkout",
    "reyn_src read: non-declared path (pyproject.toml) refused",
    "read_builtin_body_bytes: reyn_cheat_sheet SKILL.md reachable",
    "read_builtin_body_bytes: flagship pipeline yaml reachable",
    "read_builtin_body_bytes: non-body .py path returns None (least-privilege)",
]

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((name, True, ""))
    except Exception as exc:  # noqa: BLE001 - report every failure, don't stop early
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


import reyn  # noqa: E402


def _dev_mask_guard() -> None:
    reyn_file = Path(reyn.__file__).resolve()
    venv_root = Path(sys.prefix).resolve()
    assert str(reyn_file).startswith(str(venv_root)), (
        f"reyn.__file__ ({reyn_file}) is NOT under the venv ({venv_root}) -- "
        "this probe is reading the source checkout, not the installed wheel; "
        "every check below would be meaningless"
    )
    assert not str(reyn_file).startswith(str(_DEV_ROOT)), (
        f"reyn.__file__ ({reyn_file}) resolves under the repo checkout "
        f"({_DEV_ROOT}) -- dev-tree leakage into the venv"
    )


check(_CHECK_NAMES[0], _dev_mask_guard)

from reyn.runtime.reyn_src import (  # noqa: E402
    read_text,
    resolve_reyn_root,
    safe_resolve_inside,
)


def _check_wheel_mode_detected() -> None:
    resolve_reyn_root.cache_clear()
    root = resolve_reyn_root()
    assert (root / "_bundled").is_dir(), (
        f"resolve_reyn_root() returned {root}, expected a wheel package dir "
        "with an adjacent _bundled/ directory (0061 §3.2 wheel-mode signal)"
    )


check(_CHECK_NAMES[1], _check_wheel_mode_detected)


def _read_logical_via_wheel(logical_path: str) -> bytes:
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, logical_path)
    result = read_text(target, logical_path)
    assert "content" in result, f"read_text({logical_path!r}) returned no content: {result}"
    return result["content"].encode("utf-8")


def _make_byte_identity_check(logical_path: str, dev_path: Path):
    def _check() -> None:
        got = _read_logical_via_wheel(logical_path)
        expected = dev_path.read_bytes()  # dev side read at RUNTIME (no templating)
        assert got == expected, (
            f"{logical_path} via wheel-mode reyn_src ({len(got)} bytes) does not "
            f"match the dev checkout {dev_path} ({len(expected)} bytes)"
        )

    return _check


for _i, (_logical, _dev_path) in enumerate(_BYTE_IDENTITY_LOGICAL_PATHS):
    check(_CHECK_NAMES[2 + _i], _make_byte_identity_check(_logical, _dev_path))


def _check_non_declared_path_refused() -> None:
    root = resolve_reyn_root()
    try:
        safe_resolve_inside(root, "pyproject.toml")
    except ValueError as exc:
        assert "reachable set" in str(exc), f"wrong refusal reason: {exc}"
        return
    raise AssertionError("pyproject.toml resolved instead of being refused")


check(_CHECK_NAMES[5], _check_non_declared_path_refused)

from reyn.builtin.docs import read_builtin_body_bytes  # noqa: E402
from reyn.builtin.registry import BUILTIN_PIPELINES, BUILTIN_SKILLS  # noqa: E402


def _check_cheat_sheet() -> None:
    path = BUILTIN_SKILLS["reyn_cheat_sheet"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "reyn_cheat_sheet SKILL.md body is empty"
    assert b"reyn_cheat_sheet" in body, "reyn_cheat_sheet SKILL.md missing its own name marker"


check(_CHECK_NAMES[6], _check_cheat_sheet)


def _check_flagship_pipeline() -> None:
    path = BUILTIN_PIPELINES["flagship"]["path"]
    body = read_builtin_body_bytes(path)
    assert body is not None, f"read_builtin_body_bytes({path!r}) returned None"
    assert len(body) > 0, "flagship pipeline yaml body is empty"


check(_CHECK_NAMES[7], _check_flagship_pipeline)


def _check_negative_non_body_py() -> None:
    import reyn.builtin.registry as registry_mod

    non_body_path = registry_mod.__file__
    result = read_builtin_body_bytes(non_body_path)
    assert result is None, (
        f"read_builtin_body_bytes({non_body_path!r}) returned {len(result) if result else 0} "
        "bytes -- expected None (in-package .py is NOT a legitimate body read)"
    )


check(_CHECK_NAMES[8], _check_negative_non_body_py)

any_fail = False
for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    if not ok:
        any_fail = True
    line = f"[{status}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)

sys.exit(1 if any_fail else 0)
