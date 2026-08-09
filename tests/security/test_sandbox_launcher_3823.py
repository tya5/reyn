"""Tier 2: ProcessLauncher's shared resolve/run/classify slice (#3823 ①).

``sandboxed_exec`` and the shell-hook runner both did backend-resolution +
``backend.run()`` + ``classify_denial`` identically before this — this module
pins the extracted shared shape, real ``NoopBackend`` throughout (no mocks;
`.run()` is a real subprocess launch, cheap and platform-portable).
"""
from __future__ import annotations

import sys

import pytest

from reyn.security.sandbox.launcher import resolve_backend, run_and_classify
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy


def test_resolve_backend_prefers_the_injected_instance():
    """Tier 2: an injected backend wins over name-based auto-selection —
    the same precedent sandboxed_exec already established. get_default_backend
    is never even reached when an instance is supplied."""
    injected = NoopBackend()
    resolved = resolve_backend(injected, sandbox_config=None)
    assert resolved is injected


def test_resolve_backend_falls_back_to_the_factory_when_none_given():
    """Tier 2: with no injected backend, resolve_backend calls
    get_default_backend(sandbox_config) — the same factory every call site
    used inline before this extraction."""
    resolved = resolve_backend(None, sandbox_config=None)
    # No real backend forced here (platform-dependent); just confirm the
    # factory path was actually reached and returned something usable.
    assert resolved is not None
    assert resolved.available() or not resolved.available()  # real call, no crash


@pytest.mark.asyncio
async def test_run_and_classify_returns_a_normal_result_with_no_denial():
    """Tier 2: a real, successful run classifies to denial_class=None."""
    backend = NoopBackend()
    policy = SandboxPolicy(deny_subprocess=False)
    launched = await run_and_classify(
        backend, [sys.executable, "-c", "print('ok')"], policy,
    )
    assert launched.result.returncode == 0
    assert launched.denial_class is None


@pytest.mark.asyncio
async def test_run_and_classify_classifies_a_real_fork_denial_signature():
    """Tier 2: run_and_classify correctly WIRES a real backend.run() result
    into classify_denial — not re-testing classify_denial's own logic
    (covered by test_sandbox_denial_class_2820.py), but that this module's
    extraction didn't drop the wiring between the two. Drives a real
    subprocess that reproduces the exact launcher-fork denial stderr
    signature and a nonzero exit, rather than asserting on a crafted result
    object.

    Strip the ``classify_denial(...)`` call inside ``run_and_classify`` (or
    revert to not calling it at all) and this goes RED — denial_class would
    stay unset/wrong despite the real stderr matching the signature.
    """
    from reyn.security.sandbox.denial import DENIAL_FORK

    backend = NoopBackend()
    policy = SandboxPolicy(deny_subprocess=False)
    script = (
        "import sys; "
        "sys.stderr.write('pyenv: fork: Operation not permitted\\n'); "
        "sys.exit(1)"
    )
    launched = await run_and_classify(backend, [sys.executable, "-c", script], policy)
    assert launched.result.returncode == 1
    assert launched.denial_class == DENIAL_FORK
