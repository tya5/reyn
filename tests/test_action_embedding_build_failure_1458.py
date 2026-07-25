"""Tier 2: #1458 — per-session build-failure memoization + decision-enabling log.

P2-convergence PR2 (#3270 §3, migrating the PR2 commitment carried from
PR1's co-vet): this file used to drive
``RouterLoop._build_action_embedding_index_background`` directly — a
primitive PR1 (#3270 §2) made production-dead (its only caller after PR1's
Coordinator-routing was this test). That primitive is now DELETED; this
file is rewritten to drive the SAME retry-semantics invariant against the
PRODUCTION path instead — ``RouterLoop._ensure_action_index_built``, which
registers a ``BuildFn`` with the real ``IndexCoordinator`` and calls
``ensure_built`` (exactly what a live chat turn does). The invariant is
UNCHANGED: a build failure is memoized so a fresh coordinator/session
attempts the build exactly ONCE, and the production retry-guard (checked
by the caller BEFORE invoking the build again, mirroring
``RouterLoop.run()``'s own gate) suppresses any further attempt within the
same session. Failure-state now lives SOLELY in
``IndexCoordinator.build_failed(source_id)`` (#3270 §3 single-owner
collapse) — there is no more RouterLoop-instance-scoped flag to read.

No mocks. The build path is exercised via a real ``RouterLoop`` subclass
(minimal-subclass shim for ``_build_router_caller_state`` /
``_get_index_coordinator`` — same convention as
``tests/test_index_coordinator_3247_p2b.py`` /
``tests/test_index_coordinator_3247_p2d.py``) driving a REAL
``IndexCoordinator`` + a REAL ``ActionEmbeddingIndex`` against a real
(monkeypatched-provider) ``embed`` op; the fake embedding provider raises a
real ``RuntimeError`` to trigger the failure path — non-vacuity: since the
failure now flows through ``embed_verify_write`` (not a bespoke
``idx.build()`` call), these tests would go RED if the Coordinator's
same-session suppression (``build_failed``) were neutered, because the
retry-guarded second call would re-invoke the provider and the call-count
assertion would fail.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.coordinator import IndexCoordinator
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.permissions import PermissionDecl
from reyn.tools.action_index import ActionEmbeddingIndex


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FailingProvider:
    """Real fake provider that always raises (simulates the embedding API
    being unreachable) and counts its own calls — the non-vacuity witness:
    a retry would show up as a second call. No Mock/AsyncMock per policy."""

    def __init__(self) -> None:
        self.embed_calls = 0

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        self.embed_calls += 1
        raise RuntimeError("Name or service not known (embedding API unreachable)")


class _UnsupportedParamsError(Exception):
    """Real fake mirroring litellm's UnsupportedParamsError TYPENAME (the
    helper keys on the type name, not the class identity). No Mock per
    policy."""


class _UnsupportedParamProvider:
    """Real fake provider whose embed() raises the proxy-rejects-param
    error — the #1616 gemini-via-LiteLLM-proxy case (encoding_format
    rejected)."""

    async def embed(self, texts: list[str], model: str) -> dict[str, Any]:
        raise _UnsupportedParamsError(
            "litellm.UnsupportedParamsError: gemini-embedding-001 does not "
            "support parameter: encoding_format"
        )


def _op_ctx_for(provider: Any, monkeypatch: pytest.MonkeyPatch, events: EventLog) -> OpContext:
    """Real OpContext whose `embed` op resolves to ``provider`` (mirrors
    ``tests/test_index_coordinator_3247_p2b.py``/``_p2d.py``'s
    ``_op_ctx_for``)."""
    import reyn.core.op_runtime.embed as _embed_mod

    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)
    ws = Workspace(events=events)
    return OpContext(workspace=ws, events=events, permission_decl=PermissionDecl())


class _StubEvents(EventLog):
    """A real EventLog — production audit-emit is exercised; nothing
    special is asserted about its contents here (that is P2d's job)."""


class _StubHost:
    """Minimal host — only what ``RouterLoop._ensure_action_index_built`` /
    ``_fetch_action_catalog_items`` touch."""

    def __init__(self, op_ctx: Any, events: EventLog) -> None:
        self.events = events
        self.op_ctx_stub = op_ctx

    def make_router_op_context(self) -> Any:
        return self.op_ctx_stub


class _LoopWithFailingBuild(RouterLoop):
    """RouterLoop subclass exercising the production
    ``_ensure_action_index_built`` orchestration without a full
    host/chain/session setup — same minimal-subclass pattern as
    ``tests/test_index_coordinator_3247_p2b.py``'s ``_LoopForP2b``."""

    def __init__(self, workspace_root: Path, op_ctx: Any, events: EventLog) -> None:
        self.host = _StubHost(op_ctx, events)  # type: ignore[assignment]
        self.chain_id = "test-chain"
        self._workspace_root_for_test = workspace_root

    async def _build_router_caller_state(self) -> None:  # type: ignore[override]
        return None  # list_actions handler is not reached; provider raises first

    def _get_index_coordinator(self) -> IndexCoordinator:
        # Deterministic per-test coordinator instance (bypasses the module
        # singleton so tests don't leak state across each other) — same
        # convention as ``_LoopForP2b``/``_LoopForP2d``.
        if not hasattr(self, "_test_coordinator"):
            self._test_coordinator = IndexCoordinator(self._workspace_root_for_test)
        return self._test_coordinator


def _build_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Any,
) -> tuple[_LoopWithFailingBuild, ActionEmbeddingIndex, IndexCoordinator]:
    events = _StubEvents()
    op_ctx = _op_ctx_for(provider, monkeypatch, events)
    idx = ActionEmbeddingIndex(workspace_root=tmp_path)
    loop = _LoopWithFailingBuild(tmp_path, op_ctx, events)
    coordinator = loop._get_index_coordinator()
    _run(loop._ensure_action_index_built(idx, provider, "standard", await_completion=True))
    return loop, idx, coordinator


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_fresh_session_attempts_build_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #1458 — a fresh coordinator/session attempts the build
    exactly once on a failure, and the Coordinator's failure-memo
    (single-owner, P2-convergence PR2 #3270 §3) is set as a result.
    Observable via ``provider.embed_calls`` (the real production embed op
    call, driven through ``ensure_built``'s ``embed_verify_write``) — a
    silent double-attempt would show up as ``embed_calls == 2``."""
    provider = _FailingProvider()
    loop, idx, coordinator = _build_once(tmp_path, monkeypatch, provider)

    assert provider.embed_calls == 1
    assert idx.is_ready() is False
    assert coordinator.build_failed("actions") is True
    assert not hasattr(loop, "_action_index_build_failed"), (
        "the retired twin RouterLoop-side flag must not exist"
    )


def test_same_session_retry_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #1458 — same-session retry suppression is DRIVEN, not
    mirrored: this test calls ``_ensure_action_index_built``
    UNCONDITIONALLY a second time (no caller-side "check
    ``build_failed`` first" guard) and asserts the retry is suppressed —
    the suppression itself is enforced by ``IndexCoordinator.ensure_built``
    (the ``_failure_memo`` owner, P2-convergence PR2 #3270 §3), not by
    this test's own logic. Observable via ``provider.embed_calls`` staying
    at 1 after the second call — a retry would drive a second real
    ``embed`` op call. Non-vacuity: strip ``ensure_built``'s
    ``build_failed(source_id)`` early-return (coordinator.py) and THIS
    assertion goes RED (``embed_calls == 2``), because the second call
    would then run ``build_fn`` again — see the PR body for the recorded
    RED-when-neutered proof."""
    provider = _FailingProvider()
    loop, idx, coordinator = _build_once(tmp_path, monkeypatch, provider)
    assert provider.embed_calls == 1
    assert coordinator.build_failed("actions") is True

    # Call again UNCONDITIONALLY — no caller-side guard. The suppression
    # must come from ``ensure_built`` itself for this to be a genuine
    # (non-vacuous) drive of the production mechanism.
    _run(loop._ensure_action_index_built(
        idx, provider, "standard", await_completion=True,
    ))

    assert provider.embed_calls == 1, "memoized failure must prevent a retry"
    assert idx.is_ready() is False


def test_build_failure_search_stays_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #1458 — after a build failure, ``is_ready()`` on the real
    index stays False, which is the gate that keeps ``_search_visible``
    False in ``RouterLoop.run()``. Regression pin: the failure must not
    accidentally flip search to visible."""
    provider = _FailingProvider()
    _loop, idx, _coordinator = _build_once(tmp_path, monkeypatch, provider)
    assert idx.is_ready() is False


def test_warning_log_emitted_once_with_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """Tier 2: #1458 — a decision-enabling warning log is emitted exactly
    once on failure; it mentions the three actionable options."""
    provider = _FailingProvider()
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.router_loop"):
        _build_once(tmp_path, monkeypatch, provider)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING log on build failure"
    text = " ".join(r.getMessage() for r in warnings).lower()
    # All three options mentioned.
    # FP-0066 §7 / #3218: option 2 (opt out) moved from `embedding_class: null`
    # to `embedding.enabled: false` (clean-break).
    assert "embedding.enabled" in text, "option 2 (set embedding.enabled: false) must be named"
    assert "standard" in text or "api" in text, "option 3 (api class) must be named"
    assert "embedding" in text and ("unreachable" in text or "provider" in text), (
        "cause (embedding provider failure) must be named"
    )


def test_build_failure_unsupported_param_warns_proxy_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """Tier 2: #1616 — driving the real production build path with a
    provider that raises the proxy-rejects-param error logs the proxy
    drop_params guidance (the operator is NOT left with a silent empty
    index nor the misleading HF message)."""
    provider = _UnsupportedParamProvider()
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.router_loop"):
        _build_once(tmp_path, monkeypatch, provider)

    text = " ".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ).lower()
    assert "drop_params" in text and "proxy" in text, (
        f"expected proxy drop_params guidance; got: {text!r}"
    )


# ── #1616: cause-aware guidance — UnsupportedParamsError vs HF-download ──────────


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


def test_helper_generic_failure_keeps_config_guidance() -> None:
    """Tier 2: #1616 — a non-param failure (e.g. network/credentials) still
    returns the generic embedding-provider-failure guidance (regression pin
    for the #1458 branch; #3128 removed the in-process-local-model-specific
    HF-download branch since litellm is now the sole embedding backend)."""
    from reyn.runtime.router_loop import _action_index_build_failure_warning

    exc = RuntimeError("Name or service not known (embedding API unreachable)")
    msg = _action_index_build_failure_warning(exc, "standard").lower()
    assert "embedding.classes" in msg or "provider config" in msg
    assert "drop_params" not in msg
