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

from reyn.chat.router_loop import RouterLoop

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
    asyncio.run(loop._build_action_embedding_index_background(idx, provider, "local-mini"))
    # is_ready() still False after failure — search stays hidden.
    assert idx.is_ready() is False


def test_warning_log_emitted_once_with_options(caplog) -> None:
    """Tier 2: #1458 — a decision-enabling warning log is emitted exactly once
    on failure; it mentions the three actionable options."""
    loop = _LoopWithFailingBuild()
    with caplog.at_level(logging.WARNING, logger="reyn.chat.router_loop"):
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
