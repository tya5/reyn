"""Tier 1/2: #5059 — ``httpx`` and ``cryptography`` are declared CORE
dependencies (``pyproject.toml``), not transitive riders, and the test
files that used to guard themselves with ``pytest.importorskip("httpx",
...)`` (12 files, folded together with the fastapi half of this same
finding, #5058) now fail LOUD on a genuinely broken install instead of
silently skipping.

Root cause (owner finding, `gh issue view 5059`): ``httpx`` and
``cryptography`` were both present in every real install ONLY because
OTHER core dependencies (``litellm``/``ddgs``/``openai`` for httpx;
``mcp``'s own ``pyjwt[crypto]`` for cryptography) happened to pull them
in transitively — nothing in ``pyproject.toml`` declared them, so a
future upstream release dropping either chain link would silently break
reyn's own direct usage (8 files import ``httpx`` directly;
``interfaces/web/auth/tls.py`` imports ``cryptography`` directly) with
no signal until a runtime failure. ``reyn.interfaces.web.auth.tls``'s
own error message additionally pointed at a REMEDY THAT NEVER WORKED
("install the [web] extra") — ``[web]`` has been an empty back-compat
alias since #5051, and ``cryptography`` was never part of it even
before that.

Fix (this PR): both declared directly in ``pyproject.toml``'s core
``dependencies`` (mirroring the ``anyio`` precedent, #4395 — "make an
existing transitive a direct dependency"), the 12 files' stale
``pytest.importorskip("httpx", ...)`` guards removed (matching #5058's
own already-landed fastapi half, PR #5125), and ``tls.py``'s error
message + module docstring corrected to describe the real remedy (a
broken install needs reinstalling, not an extra that was never real).

Acceptance ③ below is the one that actually matters: declaring a
dependency and removing its guard are only jointly correct if a
GENUINELY missing/broken install now fails collection loudly instead of
being silently skipped — verified live, not merely reasoned about, via a
``sys.meta_path`` finder that raises ``ModuleNotFoundError`` for
``httpx`` (an interception of Python's own import mechanism, not a mock
of any reyn object — the same SPIRIT as
``test_4395_litellm_import_not_recached_on_failure.py``'s
``builtins.__import__`` patch, but a DIFFERENT seam: measured directly
against this exact scenario, ``pytest.importorskip`` calls
``importlib.import_module`` internally, which resolves modules through
``sys.meta_path`` WITHOUT going through the patchable
``builtins.__import__`` wrapper at all — a `builtins.__import__` patch
(this file's own first draft) silently never engages for it, the same
"looks intercepted, isn't" trap the earlier `#4395`-style technique does
not have for a bare `import` STATEMENT specifically, but does have here;
`sys.meta_path` is the layer common to both `import` statements and
`importlib.import_module`, so it is the one seam that actually
distinguishes "guard present" (a caught `ModuleNotFoundError` becomes
`pytest.skip.Exception`) from "guard removed" (the same
`ModuleNotFoundError` propagates uncaught) — proved by strip-verifying
against this file's own git history, see the acceptance test's own
docstring below).
"""
from __future__ import annotations

import importlib
import sys

import pytest


def test_httpx_and_cryptography_import_cleanly() -> None:
    """Tier 1: the 2 packages #5059 moved into core dependencies import
    without raising, in THIS interpreter — a broken/stale install (one
    genuinely missing or incompatible) fails this test loud, mirroring
    ``test_5051_web_core_deps_import.py``'s own established shape for
    the sibling #5051 finding (same class, different PR)."""
    import cryptography  # noqa: F401
    import httpx  # noqa: F401


def test_tls_error_message_no_longer_points_at_the_empty_web_extra() -> None:
    """Tier 1: #5059's own root finding, pinned directly — the FALSE
    remedy ("install the [web] extra") that never worked (``[web]`` has
    been an empty back-compat alias since #5051, and ``cryptography``
    was never part of it in the first place) must not appear in either
    of ``tls.py``'s two dep-gated error messages. Reads the module's
    actual source rather than triggering the (now effectively
    unreachable, since cryptography is guaranteed core) ``ImportError``
    branches directly — the branches exist for a genuinely broken venv,
    which this test does not attempt to simulate; it pins the STATIC
    message text instead."""
    import inspect

    from reyn.interfaces.web.auth import tls

    source = inspect.getsource(tls)
    assert "[web] extra" not in source, (
        "tls.py still tells an operator to install the [web] extra for "
        "cryptography -- that extra has been empty since #5051, and "
        "cryptography was never part of it even before that"
    )
    assert "core dependency" in source, (
        "tls.py's error message should describe cryptography as the "
        "core dependency it now is, not an optional install"
    )


class _BlockHttpxFinder:
    """A real ``sys.meta_path`` entry, inserted ahead of the genuine
    finders, that raises for ``httpx``/``httpx.*`` — this is the layer
    ``importlib.import_module`` (what ``pytest.importorskip`` calls
    internally) and a plain ``import`` statement BOTH route through, so
    it is the one seam that faithfully distinguishes "the guard caught
    this and skipped" from "there was no guard and it propagated" — see
    this module's own docstring for why a ``builtins.__import__`` patch
    (tried first) does not reach ``importlib.import_module`` calls at
    all."""

    def find_spec(self, name, path, target=None):
        if name == "httpx" or name.startswith("httpx."):
            raise ModuleNotFoundError("simulated: httpx not installed")
        return None


@pytest.fixture()
def _block_httpx_import():
    """Evicts every already-cached ``httpx``/``httpx.*`` entry from
    ``sys.modules`` (by the time this fixture runs, httpx has already
    been imported successfully by other tests collected earlier in the
    SAME pytest session — leaving it cached would let a fresh import
    just return the cached module without a real resolution attempt),
    then installs :class:`_BlockHttpxFinder`. Both undone afterward."""
    saved = {k: v for k, v in sys.modules.items() if k == "httpx" or k.startswith("httpx.")}
    for k in saved:
        del sys.modules[k]
    sys.meta_path.insert(0, _BlockHttpxFinder())
    try:
        yield
    finally:
        sys.meta_path.pop(0)
        sys.modules.update(saved)


def test_a_missing_httpx_fails_collection_of_a_fixed_file_not_a_silent_skip(
    _block_httpx_import,
) -> None:
    """Tier 2: #5059 acceptance ③ — the actual claim this PR makes:
    removing the ``importorskip`` guard converts "httpx missing" from a
    SILENT SKIP into a LOUD collection failure. Verified against a REAL
    fixed file (``tests/runtime/test_webhook_delivery_ack.py`` — this PR
    removed its bare ``pytest.importorskip("httpx", ...)`` guard, leaving
    a plain module-level ``import httpx``), not reasoned about.

    Explicitly distinguishes the 3 possible outcomes rather than a bare
    ``pytest.raises`` (which would let a resurrected guard's
    ``pytest.skip.Exception`` propagate uncaught out of THIS test —
    pytest's own machinery marks a test that does that SKIPPED, not
    FAILED, silently losing the very signal this test exists to give):
    strip-verified locally by temporarily reintroducing the removed
    guard — the middle branch below fired (``pytest.fail``), proving
    this test does NOT just pass-through a resurrected skip as green."""
    target = "tests.runtime.test_webhook_delivery_ack"
    sys.modules.pop(target, None)
    try:
        importlib.import_module(target)
    except ModuleNotFoundError as exc:
        assert "simulated: httpx not installed" in str(exc), (
            f"a ModuleNotFoundError was raised, but not the simulated one "
            f"-- something else broke: {exc!r}"
        )
    except pytest.skip.Exception:
        pytest.fail(
            "the fixed file SKIPPED instead of failing loud when httpx "
            "was genuinely missing -- an importorskip-shaped guard must "
            "have been reintroduced somewhere in its import chain"
        )
    else:
        pytest.fail(
            "expected the import to fail (httpx was genuinely blocked) "
            "-- it succeeded instead, meaning the block itself did not "
            "engage"
        )
    finally:
        # Leave no trace for whatever collects this module next in the
        # SAME pytest session: a failed partial import can leave a
        # broken entry in sys.modules that a later real import of the
        # same dotted path would incorrectly reuse.
        sys.modules.pop(target, None)


def test_the_fixed_file_imports_fine_once_httpx_is_not_blocked() -> None:
    """Tier 2: the negative-control half of the acceptance above — the
    SAME target module, freshly imported with NOTHING blocked, must
    succeed. Without this, the previous test's "raises" result would be
    equally consistent with the target module being broken for an
    unrelated reason (a green here that the strip-falsify below did not
    also exercise would be a false witness — CLAUDE.md's six-questions
    Q4 shape)."""
    target = "tests.runtime.test_webhook_delivery_ack"
    sys.modules.pop(target, None)
    try:
        importlib.import_module(target)
    finally:
        sys.modules.pop(target, None)
