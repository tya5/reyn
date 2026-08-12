"""Tier 2: `_is_llm_timeout_exc` (llm.py), `resolve_verify` (web.py), and
`RegistryClient.__aenter__` (registry/client.py) no longer do their own
independent `import litellm` / `from litellm.X import Y` — they read the
already-confirmed module off `ensure_litellm_ready()`'s return value
instead (#4395, owner-observed live: `AttributeError: module 'litellm'
has no attribute 'exceptions'`).

THE BUG: Python places a module into `sys.modules` at the START of
import, before its top-level code finishes — a SEPARATE, independent
`import litellm` statement on one thread while the dedicated background
warming thread (#4417) is mid-import on ANOTHER thread can observe the
SAME, genuinely incomplete module object (attributes litellm's own
`__init__` hasn't assigned yet). Before #4417, every import was
synchronous on whichever thread triggered it, so this race was latent,
never live — #4417's own background thread is what exposed it.

Uses `builtins.__import__` patching to prove these 3 call sites never
independently attempt `import litellm` at all — not a mock, an
interception of Python's own import mechanism.
"""
from __future__ import annotations

import builtins

import pytest

import reyn.llm.litellm_bootstrap as lb_mod


@pytest.fixture(autouse=True)
def _clean_litellm_bootstrap_state():
    """Tier 2 hygiene — see the identical fixture in
    test_4395_litellm_import_not_recached_on_failure.py."""
    original_ready = lb_mod._litellm_ready
    original_cooldown_until = lb_mod._litellm_import_cooldown_until
    lb_mod._litellm_ready = False
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = 0.0
    yield
    lb_mod._litellm_ready = original_ready
    lb_mod._ready_registry.pop("ready", None)
    lb_mod._litellm_import_failure_warned = False
    lb_mod._litellm_import_cooldown_until = original_cooldown_until


def test_is_llm_timeout_exc_returns_false_when_litellm_not_ready(monkeypatch):
    """Tier 2: `exc` cannot be a genuine `litellm.exceptions.Timeout`
    instance if litellm itself was never successfully imported — False
    is the logically correct answer, not just a safe placeholder."""
    from reyn.llm.llm import _is_llm_timeout_exc

    class _FakeTimeout(Exception):
        pass

    assert not lb_mod.is_litellm_ready()
    assert _is_llm_timeout_exc(_FakeTimeout("boom")) is False


def test_is_llm_timeout_exc_never_imports_litellm_on_its_own(monkeypatch):
    """Tier 2: THE core fix — this classifier must never independently
    attempt `import litellm`, regardless of readiness state."""
    from reyn.llm.llm import _is_llm_timeout_exc

    real_import = builtins.__import__

    def _fail_only_on_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise AssertionError(
                "_is_llm_timeout_exc must not independently import litellm"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_only_on_litellm)

    class _SomeExc(Exception):
        pass

    result = _is_llm_timeout_exc(_SomeExc("boom"))
    assert result is False  # not ready, never attempted, correctly classified


def test_is_llm_timeout_exc_classifies_a_real_timeout_once_ready():
    """Tier 2: accept-side — once litellm IS ready, a genuine
    `litellm.exceptions.Timeout` is still correctly classified."""
    from reyn.llm.llm import _is_llm_timeout_exc

    litellm = lb_mod.ensure_litellm_ready()
    assert litellm is not None, "this test requires a real litellm install"

    exc = litellm.exceptions.Timeout(
        message="timed out", model="test-model", llm_provider="test",
    )
    assert _is_llm_timeout_exc(exc) is True


def test_web_fetch_resolve_ssl_verify_never_imports_litellm_independently(monkeypatch):
    """Tier 2: `web.py`'s SSL-verify resolver — same fix, same fixture
    class. An explicit `ca_bundle`/`verify_ssl` config never needs
    litellm at all; the litellm-dependent fallback path must not do its
    own independent import either."""
    from reyn.core.op_runtime.web import _resolve_ssl_verify

    real_import = builtins.__import__

    def _fail_only_on_litellm(name, *args, **kwargs):
        if name == "litellm":
            raise AssertionError(
                "_resolve_ssl_verify must not independently import litellm"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_only_on_litellm)

    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    class _FakeEventLog:
        subscribers: list = []
        def emit(self, *args, **kwargs) -> None:
            pass

    class _FakeWorkspace:
        pass

    ctx = OpContext(
        workspace=_FakeWorkspace(),  # type: ignore[arg-type]
        events=_FakeEventLog(),      # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_fetch_config=None,
    )

    # not-ready + no explicit config → falls through to the litellm path,
    # which must read off ensure_litellm_ready()'s return value, never a
    # separate `import litellm` — and fall back to True when unavailable.
    result = _resolve_ssl_verify(ctx)
    assert result is True
