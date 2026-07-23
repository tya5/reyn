"""Root pytest configuration: in-process import-identity guard (#3233).

`#3231`'s real incident: a coder ran `pytest` in a git worktree whose ambient
venv had an editable `.pth` pointing at a DIFFERENT worktree. In-process
`import reyn` resolved that OTHER checkout, so the whole suite ran stale code
and reported a false-green ("9150 passed") that hid a real RED an architect's
fresh-worktree run caught.

`scripts/verify_env_identity.py` (#3024) already guards this family of
mismatch, but only for a SPAWNED subprocess's resolution, via two opt-in
`tests/conftest.py` fixtures (`out_of_process_reyn` / `reyn_console_scripts`)
a test requests when it spawns something. Neither fires for the incident
above: no test asked for a spawn-guard, because the divergence was in the
IN-PROCESS `import reyn` every test file performs implicitly, not in anything
a test spawned. This module is the complement: it checks the resolution the
whole process is already relying on, unconditionally, before a single test is
collected.

Lives at the repo root (not `tests/conftest.py`) and hooks `pytest_configure`
rather than an autouse fixture so it fires for ANY bare `pytest` invocation —
including `pytest --collect-only` with zero tests selected — since root
`conftest.py` files load, and `pytest_configure` runs, before collection
starts. A session-scoped autouse fixture would not fire until at least one
test is collected AND run, which is a weaker guarantee than "any pytest
startup in this tree is protected".

Contract grounding for why this is a genuine wrong-worktree signal rather
than a fragile heuristic: `pyproject.toml`'s `[tool.pytest.ini_options]`
declares `pythonpath = ["src"]`, and CI always runs this checkout in
src-mode (no separate `reyn` install) — so in every correctly-configured
environment `reyn.__file__` resolves under `<rootdir>/src` trivially, and
this check passes without ever looking at anything. It can only fire where
an editable `.pth` from ANOTHER checkout wins over that pin, which is
precisely the wrong-worktree case #3231 hit.

NOTE (future installed-mode workflow): if a workflow is ever added that
intentionally runs this test suite against an INSTALLED (site-packages)
`reyn` rather than this checkout's `src/reyn` — i.e. one where
`pythonpath = ["src"]` is deliberately not the operative resolution — this
guard will need a scope exception (e.g. an opt-out env var) for that mode,
since a correctly-resolving installed `reyn` is indistinguishable from this
check's own vantage point from the wrong-worktree case it exists to catch.
No such workflow exists today, so no exception is wired.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent


def _load_env_identity():
    """Load `scripts/verify_env_identity.py` by path (mirrors `tests/conftest.py`).

    `scripts/` carries no `__init__.py` and is not on `sys.path` by default, so
    the module is loaded from its file location rather than imported by name.
    """
    path = _REPO_ROOT / "scripts" / "verify_env_identity.py"
    spec = importlib.util.spec_from_file_location("_reyn_env_identity_root_guard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def pytest_configure(config: pytest.Config) -> None:
    """Fail loudly, before collection, if in-process `reyn` resolves outside this tree.

    `import reyn` here is deliberate and unavoidable: this check's whole subject
    is "what does `import reyn` resolve to in THIS process", and Python caches
    modules by name in `sys.modules` — the module object this import returns is
    the exact same object every collected test file's own `import reyn` will
    receive. There is no reyn-free way to ask this question in-process (contrast
    `scripts/verify_env_identity.py`'s out-of-process checks, which stay
    reyn-free precisely because they ask it of a *different* process).
    """
    import reyn

    env_identity = _load_env_identity()
    finding = env_identity.check_in_process_tree(
        Path(reyn.__file__), Path(config.rootpath)
    )
    if finding is None:
        return
    pytest.exit(
        f"env-identity (in-process, #3233):\n{finding.render()}",
        returncode=1,
    )
