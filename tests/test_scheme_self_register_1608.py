"""Tier 2: #1608 ④ — built-in tool-use schemes self-register on import.

The OS scheme resolver no longer names any scheme class; each scheme module calls
``register_scheme`` at import time and the ``schemes`` package ``__init__`` imports
them all. The load-bearing invariant (sandbox_2's completeness axis): **all built-in
names resolve after importing only the package, with NO prior explicit scheme import
by the caller.** This MUST be checked in a FRESH interpreter — an in-process test
would false-pass because sibling tests already populate the global registry.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _fresh_interpreter(src_root: str, code: str) -> subprocess.CompletedProcess:
    # `out_of_process_reyn` propagates this test tree's src root to the subprocess
    # so it imports the SAME reyn (the #1609 worktree-drift lesson: sys.executable's
    # default reyn may resolve to a different worktree's venv).
    env = {**os.environ, "PYTHONPATH": src_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, env=env,
    )


def test_all_builtins_resolve_in_fresh_interpreter(out_of_process_reyn) -> None:
    """Tier 2: #1608 ④ — a fresh interpreter that imports ONLY the schemes package
    (no explicit scheme-class import) finds all 4 built-ins registered + resolvable,
    and the default is unchanged. This is the completeness gate."""
    result = _fresh_interpreter(
        out_of_process_reyn,
        """
        # The ONLY scheme-related import — must self-register the full built-in set.
        import reyn.tools.schemes  # noqa: F401
        from reyn.tools.scheme import (
            DEFAULT_SCHEME_NAME, get_scheme, registered_scheme_names,
        )
        # FP-0066 P4c (#3247): the CodeAct implementation self-registers under the
        # (enumerate-all, content_fence) cell's resolved name, not the bare "codeact".
        from reyn.tools.transport import CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME
        expected = {
            "universal-category", "enumerate-all", "retrieval",
            CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
        }
        names = set(registered_scheme_names())
        assert "codeact" not in names
        assert expected <= names, f"missing built-ins: {expected - names}"
        for n in expected:
            s = get_scheme(n)
            assert s is not None and s.name == n, n
        assert DEFAULT_SCHEME_NAME == "enumerate-all"
        assert get_scheme(DEFAULT_SCHEME_NAME) is not None
        print("RESOLVE_OK")
        """
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "RESOLVE_OK" in result.stdout


def test_resolver_finds_all_builtins_without_naming_them(out_of_process_reyn) -> None:
    """Tier 2: #1608 ④ — _resolve_tool_use_scheme (the OS resolver, which names NO
    scheme class) resolves each built-in name from a fresh interpreter, and an
    unknown name falls back to the default. Behaviour-invariant vs the old lazy loop."""
    result = _fresh_interpreter(
        out_of_process_reyn,
        """
        from reyn.runtime.router_loop import _resolve_tool_use_scheme
        # FP-0066 P4c (#3247): CodeAct resolves under its (enumerate-all,
        # content_fence)-relocated name, not the bare "codeact".
        from reyn.tools.transport import CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME
        for n in (
            "universal-category", "enumerate-all", "retrieval",
            CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
        ):
            s = _resolve_tool_use_scheme(n)
            assert s is not None and s.name == n, n
        # Unknown / None → default. "codeact" is now an unknown name (P4c
        # clean-break) so it falls back to the default too, same as any
        # other unregistered string.
        assert _resolve_tool_use_scheme("no-such").name == "enumerate-all"
        assert _resolve_tool_use_scheme("codeact").name == "enumerate-all"
        assert _resolve_tool_use_scheme(None).name == "enumerate-all"
        print("RESOLVE_OK")
        """
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "RESOLVE_OK" in result.stdout
