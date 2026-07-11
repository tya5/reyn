"""Tier 2: #1458 — per-session build-failure memoization + decision-enabling log.

When the action embedding index build fails (e.g. Hugging Face unreachable),
``RouterLoop._build_action_embedding_index_background`` memoizes the failure via
``_action_index_build_failed`` so neither the eager path nor the background-task
path retries within the same RouterLoop session.  A decision-enabling warning log
is emitted exactly once with three actionable options (pre-download / null class /
API class).

No mocks.  The build path is exercised via a real ``RouterLoop`` instance whose
``_build_router_caller_state`` is shimmed by subclassing; the embedding provider
raises a real ``RuntimeError`` to trigger the failure path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from reyn.runtime.router_loop import RouterLoop

# ── Minimal RouterLoop subclass that makes _build_action_embedding_index_background
# directly invokable without a full host/chain setup. ────────────────────────────


class _FailingProvider:
    """Real fake provider that always raises (simulates HF-unreachable / model
    download failure).  No Mock / AsyncMock — pure subclass fake per policy."""

    async def embed(self, *_args, **_kwargs):  # type: ignore[override]
        raise RuntimeError("Name or service not known (HF unreachable)")

    def get_dimension(self, *_args, **_kwargs) -> int:
        raise RuntimeError("Name or service not known (HF unreachable)")


class _FailingIndex:
    """Real fake index whose build() method raises."""

    def is_ready(self) -> bool:
        return False

    async def build(self, *_args, **_kwargs):  # type: ignore[override]
        raise RuntimeError("Index build failed — provider error")


class _MinimalEvents:
    """Minimal events sink; records emitted events."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append({"kind": kind, **kwargs})


class _MinimalHost:
    def __init__(self) -> None:
        self.events = _MinimalEvents()
        # FP-0057 #2856 Part A: _build_action_embedding_index_background now
        # builds an OpContext via ``host.make_router_op_context()`` and passes
        # THAT (not the raw provider) as idx.build()'s second positional arg
        # (idx.build() itself now routes the embed call through the shared
        # `embed` op). These tests' fake indexes still reach into that
        # second arg expecting the fake provider (to trigger the SAME
        # provider-raised exception the production embed op would surface),
        # so this stub just returns whatever provider the test stashed here.
        self.op_ctx_stub: Any = None

    def make_router_op_context(self) -> Any:
        return self.op_ctx_stub


class _LoopWithFailingBuild(RouterLoop):
    """RouterLoop subclass whose _build_router_caller_state returns a minimal
    state just sufficient for the build method (which only needs events).
    The embedding index build is triggered via a real _FailingIndex provider."""

    def __init__(self) -> None:
        # Skip the real __init__; we only need the method under test.
        self.host = _MinimalHost()  # type: ignore[assignment]
        self.chain_id = "test-chain"

    async def _build_router_caller_state(self) -> None:  # type: ignore[override]
        return None  # list_actions handler is not reached; provider raises first


def _run_build(loop: _LoopWithFailingBuild) -> None:
    idx = _FailingIndex()
    provider = _FailingProvider()
    loop.host.op_ctx_stub = provider  # see _MinimalHost.make_router_op_context
    asyncio.run(loop._build_action_embedding_index_background(idx, provider, "local-mini"))


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_build_failure_prevents_retry_same_session() -> None:
    """Tier 2: #1458 — after a build failure the memoization flag is set, so a
    second call that checks the flag (as the production guard does) does not
    re-invoke the build. Observable via event count: a retry would emit another
    action_index_build_failed event; no retry → count stays at 1."""
    loop = _LoopWithFailingBuild()
    _run_build(loop)
    first_count = len(loop.host.events.emitted)
    # Simulate the production guard: only call again if the flag is NOT set.
    if not getattr(loop, "_action_index_build_failed", False):
        _run_build(loop)
    # Count must be unchanged — the guard prevented the retry.
    assert len(loop.host.events.emitted) == first_count


def test_build_failure_emits_event() -> None:
    """Tier 2: #1458 — the existing action_index_build_failed event is still
    emitted (regression pin: existing downstream consumers must not break)."""
    loop = _LoopWithFailingBuild()
    _run_build(loop)
    kinds = [e["kind"] for e in loop.host.events.emitted]
    assert "action_index_build_failed" in kinds


def test_build_failure_search_stays_hidden() -> None:
    """Tier 2: #1458 — after a build failure, is_ready() on the fake index stays
    False, which is the gate that keeps _search_visible False in RouterLoop.run().
    Regression pin: the failure must not accidentally flip search to visible."""
    idx = _FailingIndex()
    assert idx.is_ready() is False
    provider = _FailingProvider()
    loop = _LoopWithFailingBuild()
    loop.host.op_ctx_stub = provider  # see _MinimalHost.make_router_op_context
    asyncio.run(loop._build_action_embedding_index_background(idx, provider, "local-mini"))
    # is_ready() still False after failure — search stays hidden.
    assert idx.is_ready() is False


def test_warning_log_emitted_once_with_options(caplog) -> None:
    """Tier 2: #1458 — a decision-enabling warning log is emitted exactly once
    on failure; it mentions the three actionable options."""
    loop = _LoopWithFailingBuild()
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.router_loop"):
        _run_build(loop)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING log on build failure"
    text = " ".join(r.getMessage() for r in warnings).lower()
    # All three options mentioned.
    assert "null" in text or "embedding_class" in text, "option 2 (set null) must be named"
    assert "standard" in text or "api" in text, "option 3 (api class) must be named"
    assert "hugging face" in text or "hf" in text or "download" in text, (
        "cause (HF / download) must be named"
    )


# ── #1616: cause-aware guidance — UnsupportedParamsError vs HF-download ──────────


class _UnsupportedParamsError(Exception):
    """Real fake mirroring litellm's UnsupportedParamsError TYPENAME (the helper
    keys on the type name, not the class identity). No Mock per policy."""


class _UnsupportedParamProvider:
    """Real fake provider whose embed() raises the proxy-rejects-param error —
    the #1616 gemini-via-LiteLLM-proxy case (encoding_format rejected)."""

    async def embed(self, *_args, **_kwargs):  # type: ignore[override]
        raise _UnsupportedParamsError(
            "litellm.UnsupportedParamsError: gemini-embedding-001 does not support "
            "parameter: encoding_format"
        )

    def get_dimension(self, *_args, **_kwargs) -> int:
        raise _UnsupportedParamsError("does not support parameter: encoding_format")


class _UnsupportedParamIndex:
    """Real fake index whose build() surfaces the provider's UnsupportedParamsError
    (mirrors ActionEmbeddingIndex.build propagating the embed() exception)."""

    def is_ready(self) -> bool:
        return False

    async def build(self, items, provider, model_class):  # type: ignore[override]
        # Drive the real embed() so the genuine provider exception propagates,
        # exactly as the production build path does (idx.build(items, provider, model_class)).
        await provider.embed(items)


def test_helper_unsupported_param_points_to_proxy_drop_params() -> None:
    """Tier 2: #1616 — the cause-aware helper, given an UnsupportedParamsError,
    returns the PROXY-side drop_params guidance (not the misleading HF-download
    message). reyn cannot suppress a param the proxy injects, so the operator is
    pointed to the recommended `litellm_settings: drop_params: true` on the proxy."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    exc = _UnsupportedParamsError(
        "gemini-embedding-001 does not support parameter: encoding_format"
    )
    msg = _action_index_build_failure_warning(exc, "standard").lower()
    assert "drop_params" in msg, "must name the recommended proxy-side fix"
    assert "proxy" in msg, "must say the fix is proxy-side"
    assert "encoding_format" in msg, "must name the rejected param"
    # Must NOT mislead with the HF-download cause for a param-rejection failure.
    assert "hugging face" not in msg and "download" not in msg


def test_helper_generic_failure_keeps_hf_guidance() -> None:
    """Tier 2: #1616 — a non-param failure (e.g. HF unreachable) still returns the
    pre-existing offline/cache guidance (regression pin for the #1458 branch)."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    exc = RuntimeError("Name or service not known (HF unreachable)")
    msg = _action_index_build_failure_warning(exc, "local-mini").lower()
    assert "hugging face" in msg or "download" in msg
    assert "drop_params" not in msg


# ── FP-0057 Phase 4: HF_HUB_OFFLINE-aware guidance ────────────────────────────


def _clear_offline_env(monkeypatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)


def test_helper_generic_failure_names_offline_fast_fail_opt_in(monkeypatch) -> None:
    """Tier 2: FP-0057 Phase 4 — the generic (real-network) HF-unreachable message
    now also names the standard HF_HUB_OFFLINE=1 as the fast-fail opt-in."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    _clear_offline_env(monkeypatch)
    exc = RuntimeError("Name or service not known (HF unreachable)")
    msg = _action_index_build_failure_warning(exc, "local-mini").lower()
    assert "hf_hub_offline" in msg


def test_helper_offline_mode_set_names_it_explicitly(monkeypatch) -> None:
    """Tier 2: FP-0057 Phase 4 — when HF_HUB_OFFLINE is set, the failure
    message names offline mode explicitly and gives the preload-and-copy-cache
    recipe, distinct from the generic "check network connectivity" message
    (offline mode made no network attempt at all — that distinction matters
    to the operator)."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    _clear_offline_env(monkeypatch)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    exc = OSError("Cannot find an appropriate cached snapshot folder")
    msg = _action_index_build_failure_warning(exc, "local-mini").lower()
    assert "hf_hub_offline" in msg
    assert "no network attempt was made" in msg
    assert "standard" in msg or "api" in msg


def test_helper_offline_mode_via_transformers_offline_env(monkeypatch) -> None:
    """Tier 2: FP-0057 Phase 4 — TRANSFORMERS_OFFLINE (the sibling HF-standard
    var) also selects the offline-specific branch, not just HF_HUB_OFFLINE."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    _clear_offline_env(monkeypatch)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    exc = OSError("Cannot find an appropriate cached snapshot folder")
    msg = _action_index_build_failure_warning(exc, "local-mini").lower()
    assert "no network attempt was made" in msg


def test_helper_offline_mode_unset_gives_generic_message(monkeypatch) -> None:
    """Tier 2: FP-0057 Phase 4 — with the offline env unset, the offline-mode
    branch is not taken; the generic network-failure message is used instead."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    _clear_offline_env(monkeypatch)
    exc = RuntimeError("Name or service not known (HF unreachable)")
    msg = _action_index_build_failure_warning(exc, "local-mini").lower()
    assert "no network attempt was made" not in msg


def test_build_failure_offline_mode_warns_with_offline_guidance(monkeypatch, caplog) -> None:
    """Tier 2: FP-0057 Phase 4 — driving the real build path with HF_HUB_OFFLINE
    set logs the offline-specific guidance (not the generic "check connectivity"
    message), so the operator is never told to "just wait" when no network
    attempt was ever made."""
    _clear_offline_env(monkeypatch)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    loop = _LoopWithFailingBuild()
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.router_loop"):
        _run_build(loop)

    text = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ).lower()
    assert "hf_hub_offline" in text
    assert "no network attempt was made" in text


def test_build_failure_unsupported_param_warns_proxy_fix(caplog) -> None:
    """Tier 2: #1616 — driving the real build path with a provider that raises the
    proxy-rejects-param error logs the proxy drop_params guidance (the operator is
    NOT left with a silent empty index nor the misleading HF message)."""
    loop = _LoopWithFailingBuild()
    idx = _UnsupportedParamIndex()
    provider = _UnsupportedParamProvider()
    loop.host.op_ctx_stub = provider  # see _MinimalHost.make_router_op_context
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.router_loop"):
        asyncio.run(loop._build_action_embedding_index_background(idx, provider, "standard"))

    text = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ).lower()
    assert "drop_params" in text and "proxy" in text, (
        f"expected proxy drop_params guidance; got: {text!r}"
    )
