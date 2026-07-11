"""Tier 2: OS invariant — #2683 removal-safety regression for the proxy seam.

#2683 deleted the inline ``LITELLM_API_BASE`` exports from
``InvocationContext.from_args`` (chat/run/mcp-serve) and
``web/deps._get_registry`` on the basis that ``load_config()`` — which both call
before their first LLM call — is now the single canonical writer. This test
falsifies the "the removal silently re-broke proxy routing" failure mode: it
drives the real ``InvocationContext.from_args`` seam (no mock) and asserts that a
configured ``api_base`` still lands in ``LITELLM_API_BASE`` purely via the
``load_config()`` fold.

The web path (``_get_registry`` → ``_load_config`` → ``load_config``) shares the
exact same seam and is covered transitively by the loader-level guarantee; a full
web-registry build pulls in process-singleton state (budget/state-log/registry)
that does not fit a focused Tier-2 seam test, so it is asserted here only at the
shared ``load_config()`` level, not by standing up the FastAPI registry.
"""
from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from reyn.interfaces.cli.invocation_context import InvocationContext

_KEY = "LITELLM_API_BASE"


@contextmanager
def _clean_env_and_cwd(project_dir: Path):
    """Save/restore ``LITELLM_API_BASE`` and cwd; enter with the key unset.

    Env-mutation tests are isolation-sensitive: ``setdefault`` is a no-op when
    the key is already present, so the seam can only be observed from a clean
    slate. Everything is restored in ``finally`` so parallel workers and later
    tests see the pre-test environment.
    """
    prev_val = os.environ.get(_KEY)
    prev_cwd = os.getcwd()
    os.environ.pop(_KEY, None)
    os.chdir(project_dir)
    try:
        yield
    finally:
        os.chdir(prev_cwd)
        if prev_val is None:
            os.environ.pop(_KEY, None)
        else:
            os.environ[_KEY] = prev_val


def test_invocation_context_seam_still_exports_api_base() -> None:
    """Tier 2: chat/run path sets LITELLM_API_BASE via the load_config fold."""
    api_base = "http://localhost:4000/reyn-2683-seam"
    with TemporaryDirectory() as td:
        project = Path(td)
        (project / "reyn.yaml").write_text(f"api_base: {api_base}\n", encoding="utf-8")
        with _clean_env_and_cwd(project):
            # Real seam, no mock: from_args → load_config() → the single writer.
            ctx = InvocationContext.from_args(argparse.Namespace())
            # Config carries the value AND the env var was materialized — proving
            # the removed inline copy's job is now done by load_config().
            assert ctx.config.api_base == api_base
            assert os.environ.get(_KEY) == api_base


def test_absent_api_base_leaves_env_unset() -> None:
    """Tier 2: no api_base configured → the seam leaves LITELLM_API_BASE unset."""
    with TemporaryDirectory() as td:
        project = Path(td)
        (project / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
        with _clean_env_and_cwd(project):
            ctx = InvocationContext.from_args(argparse.Namespace())
            assert not ctx.config.api_base
            # The ``if _api_base`` guard means no empty/spurious export.
            assert _KEY not in os.environ
