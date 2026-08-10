"""Tier 2: #2682 — load_config() bootstraps the LiteLLM proxy switch.

Root cause: the LiteLLM proxy switch env var ``LITELLM_API_BASE`` was exported
only from ``InvocationContext.from_args`` (chat/run) and ``web/deps.py`` (web).
``reyn pipe run`` reaches its ``AgentRegistry`` without building an
``InvocationContext`` — so with a LiteLLM-proxy config (``api_base`` + pseudo
``openai/<model>`` + only a proxy ``OPENAI_API_KEY``) the pseudo-model + proxy
key were sent to the REAL upstream endpoint and rejected.

The fix folds the export into ``load_config()`` itself — the one universal
chokepoint every LLM entry point passes before its first LLM call (pipe /
dogfood / embeddings direct; chat/run/mcp via ``InvocationContext``; web via
``_get_registry``).

No mocks: real ``load_config`` over a real on-disk ``reyn.yaml``. The autouse
fixture handles LITELLM_API_BASE save/restore so the process env is never
leaked into the rest of the suite.
"""
from __future__ import annotations

import os

import pytest

from reyn.config import load_config

_PROXY = "http://localhost:4000"
_PSEUDO_MODEL = "openai/gemini-2.5-flash-lite"


@pytest.fixture(autouse=True)
def _restore_litellm_api_base():
    """Snapshot + restore LITELLM_API_BASE around every test in this file.

    load_config() legitimately EXPORTS LITELLM_API_BASE process-wide (that IS
    the fix under test), and ``monkeypatch.delenv(raising=False)`` records no
    undo entry when the var starts absent — so the code-under-test's own set
    would leak into the rest of the suite and perturb litellm-routing tests.
    This fixture owns the real restore; the tests use monkeypatch only to set
    up the pre-condition.
    """
    saved = os.environ.get("LITELLM_API_BASE")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("LITELLM_API_BASE", None)
        else:
            os.environ["LITELLM_API_BASE"] = saved


def _write_project(root, *, api_base: str | None) -> None:
    lines = ["model: standard", "models:", f"  standard: {_PSEUDO_MODEL}"]
    if api_base is not None:
        lines.insert(0, f"api_base: {api_base}")
    (root / "reyn.yaml").write_text("\n".join(lines) + "\n")


# ── the load_config() chokepoint (the pipe-run path passes here) ─────────────


def test_load_config_exports_litellm_api_base(tmp_path, monkeypatch) -> None:
    """Tier 2: load_config() with api_base exports LITELLM_API_BASE.

    Every LLM entry point (incl. ``reyn pipe run``, which builds NO
    InvocationContext) passes through load_config() before its first LLM call,
    so this is the universal seam the proxy switch must be wired at.

    LITELLM_API_BASE is manipulated via direct os.environ (the autouse fixture
    owns restore); mixing monkeypatch.delenv with load_config()'s own set of
    the same var confuses monkeypatch's undo bookkeeping.
    """
    os.environ.pop("LITELLM_API_BASE", None)
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, api_base=_PROXY)

    config = load_config()

    assert config.api_base == _PROXY
    assert os.environ.get("LITELLM_API_BASE") == _PROXY


def test_load_config_no_api_base_leaves_env_unset(tmp_path, monkeypatch) -> None:
    """Tier 2: falsify — no api_base → LITELLM_API_BASE stays unset (no-op)."""
    os.environ.pop("LITELLM_API_BASE", None)
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, api_base=None)

    config = load_config()

    assert config.api_base == ""
    assert "LITELLM_API_BASE" not in os.environ


def test_load_config_setdefault_does_not_clobber(tmp_path, monkeypatch) -> None:
    """Tier 2: idempotent setdefault — a pre-existing value is preserved.

    Explicit operator-set LITELLM_API_BASE wins over config (same principle as
    the sibling REYN_* env exports load_config() already performs).
    """
    os.environ["LITELLM_API_BASE"] = "http://preset:9999"
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, api_base=_PROXY)

    load_config()

    assert os.environ["LITELLM_API_BASE"] == "http://preset:9999"


def test_load_config_export_does_not_leak_across_isolation(tmp_path, monkeypatch) -> None:
    """Tier 2: the export is confined to the process env under test control.

    Drives the api_base export, then (simulating the next isolated invocation)
    clears the env and re-loads a NON-proxy config — the stale proxy value must
    not survive. The autouse fixture owns the real restore; this asserts the
    export has no hidden global stickiness beyond os.environ.
    """
    os.environ.pop("LITELLM_API_BASE", None)
    monkeypatch.chdir(tmp_path)
    _write_project(tmp_path, api_base=_PROXY)
    load_config()
    assert os.environ.get("LITELLM_API_BASE") == _PROXY

    # Next isolated invocation: env cleared, non-proxy config → no re-export.
    os.environ.pop("LITELLM_API_BASE", None)
    _write_project(tmp_path, api_base=None)
    load_config()
    assert "LITELLM_API_BASE" not in os.environ
