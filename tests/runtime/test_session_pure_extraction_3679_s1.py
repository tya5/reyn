"""Tier 2: #3679 stage 1 — `new_chain_id` / `render_summary_for_storage` /
`merge_memory_indexes` promoted from `reyn.runtime.session`'s private
(`_`-prefixed) names to a new, dependency-light public module,
`reyn.runtime.session_pure`.

Zero behavior change — every existing test that exercised these functions
through `session.py` still passes UNMODIFIED except for the import line
itself switching to the new module (the tier's own acceptance condition:
"only the rename differs" — see `tests/runtime/test_retry_loop_chat_wiring_1125.py`,
updated in this same PR to import from the new module, no other diff).
A second such site existed at the time this was written (a force-close②
test file, removed later by #4381 PR-4 along with the mechanism it
exercised — not a regression in this promotion).

Three things pinned here, matching the stage-1 acceptance conditions
(architect-specified):

1. "Pure" is a CHECK, not an assertion in prose — each promoted function's
   signature is inspected to confirm it takes no `self`/`cls` (genuinely a
   module-level function, not a method masquerading as one).
2. No backward-compat alias: `reyn.runtime.session` no longer HAS the old
   private names at all (not even as a re-export) — all 15+ call sites
   across the repo were moved to the new module in this same PR, so nothing
   should still need one.
3. The new module is importable at module top-level WITHOUT first importing
   `reyn.runtime.session` — proving it is genuinely independent (this is
   what makes the lazy-import discipline the two real circular-import call
   sites (`router_host_adapter.py`, `router_loop_driver.py`) used to need
   actually unnecessary now, not just inconvenient).
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

from reyn.runtime.session_pure import (
    merge_memory_indexes,
    new_chain_id,
    render_summary_for_storage,
)
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT


def test_new_chain_id_is_a_pure_function_no_self():
    """Tier 2: acceptance condition ① — a genuine module-level function, not
    a method: no `self`/`cls` in its signature."""
    params = inspect.signature(new_chain_id).parameters
    assert "self" not in params and "cls" not in params


def test_render_summary_for_storage_is_a_pure_function_no_self():
    """Tier 2: acceptance condition ①."""
    params = inspect.signature(render_summary_for_storage).parameters
    assert "self" not in params and "cls" not in params


def test_merge_memory_indexes_is_a_pure_function_no_self():
    """Tier 2: acceptance condition ①."""
    params = inspect.signature(merge_memory_indexes).parameters
    assert "self" not in params and "cls" not in params


def test_session_module_no_longer_has_the_old_private_names():
    """Tier 2: acceptance condition ② — no backward-compat alias left in
    `session.py`. All 15+ call sites moved to the new module's public name
    in this same PR; if any were missed, THAT call site breaks loudly
    (ImportError/AttributeError) rather than this test silently permitting
    a straggler to keep using the old name."""
    import reyn.runtime.session as session_mod

    assert not hasattr(session_mod, "_new_chain_id")
    assert not hasattr(session_mod, "_render_summary_for_storage")
    assert not hasattr(session_mod, "_merge_memory_indexes")


def test_session_pure_importable_without_first_importing_session(out_of_process_reyn):
    """Tier 2: acceptance condition ③ — `reyn.runtime.session_pure` is
    importable standalone, in a FRESH subprocess, without
    `reyn.runtime.session` (or anything that imports it) already loaded
    first. This is the direct proof the lazy-import discipline the two real
    circular-import call sites used to need is genuinely gone: if
    `session_pure` still depended on anything that imports `session.py`
    back, importing it FIRST (before `session.py` has a chance to partially
    initialize) would raise the same
    "cannot import name ... from partially initialized module" this stage
    is fixing.

    #4446: was missing `out_of_process_reyn` (and its PYTHONPATH pin) — a
    spawned subprocess re-resolves `reyn` from the ambient venv, not from
    this checkout's in-process `sys.path` (`pythonpath = ["src"]` only
    affects THIS process). Without the pin, a machine whose ambient venv
    has an editable install pointing at a DIFFERENT checkout silently reads
    that one instead — exactly the #3231/#3024 incident `out_of_process_reyn`
    exists to close, which every sibling subprocess-spawning test in this
    repo already requests. This one just never had.
    """
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    proc = subprocess.run(
        [sys.executable, "-c", "import reyn.runtime.session_pure; print('OK')"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


def test_router_host_adapter_and_router_loop_driver_import_it_at_top_level():
    """Tier 2: the two real circular-import call sites (`router_host_adapter
    .py`, `router_loop_driver.py` — the ONLY two where a reproduced
    `ImportError` justified the original lazy-import) now import
    `session_pure` at their OWN module top, not lazily inside a function —
    the concrete "lazy import no longer needed" proof for the sites that
    actually needed it (`mcp/server.py`/`a2a.py`'s lazy imports were a local
    convention, not cycle-avoidance — see `session_pure`'s own module
    docstring)."""
    import ast

    for rel in (
        "src/reyn/runtime/services/router_host_adapter.py",
        "src/reyn/runtime/services/router_loop_driver.py",
    ):
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        top_level_modules = {
            node.module
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "reyn.runtime.session_pure" in top_level_modules, (
            f"{rel} must import session_pure at module top level, not lazily"
        )
