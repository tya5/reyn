"""Tier 2: #1953 slice 5a-2 — A2A disposition sweep (RunRegistry-derived webhook).

The periodic sweep notifies the external requester of every cancelled run via
its registered webhook, exactly once, retrying failures, bounded by
construction. Real ``RunRegistry`` + a real recording poster (no mocks). The
webhook channel map is A2A-owned (P7 — never on the internal Task model, and
after #2839 Phase 1, never derived from it either).

#2839 Phase 1: re-based off ``RunRegistry`` instead of the internal Task
backend — every ``RunEntry`` is structurally A2A-external (RunRegistry has no
internal/self-origin concept), so the prior self-vs-external split test is
retired along with the ``origin`` filter it pinned (there is nothing left to
distinguish; every RunEntry would fire). The remaining falsifications:

- (a) a cancelled run with a registered webhook → fires once;
- (b) a second sweep pass → no re-fire (notified-set);
- (c) a failed POST → not notified → retried next sweep;
- (d) the notified-set self-prunes hard-deleted run_ids (bounded);
- (e) the registry persists the map + notified-set across a reload.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.interfaces.web.a2a_webhook_registry import (
    A2AWebhookRegistry,
    sweep_dispositions,
)
from reyn.interfaces.web.run_registry import RunRegistry
from reyn.runtime.a2a_routing import a2a_session_id


class _RecordingPoster:
    """A real injectable webhook poster (not a mock) that records calls."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ok = ok

    async def __call__(self, url: str, payload: dict):
        self.calls.append((url, payload))
        return SimpleNamespace(ok=self._ok)


def _cancelled_run(run_registry: RunRegistry, run_id_suffix: str, ctx: str) -> str:
    """Create a run assigned to the context's session and mark it cancelled.
    Returns the allocated run_id."""
    entry = run_registry.create(
        agent_name=f"agent-{run_id_suffix}", chain_id=f"chain-{run_id_suffix}",
        session_id=a2a_session_id(ctx),
    )
    run_registry.cancel(entry.run_id)
    return entry.run_id


@pytest.mark.asyncio
async def test_sweep_fires_for_cancelled_run_with_webhook():
    """Tier 2: (a): a cancelled run with a registered webhook fires exactly once."""
    run_registry = RunRegistry()
    run_id = _cancelled_run(run_registry, "1", "ctx-a")
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    poster = _RecordingPoster()

    fired = await sweep_dispositions(run_registry, reg, post_fn=poster)

    assert fired == 1
    assert [c[1]["task_id"] for c in poster.calls] == [run_id]
    assert poster.calls[0][0] == "https://client.example/hook"
    assert poster.calls[0][1]["contextId"] == "ctx-a"
    assert reg.is_notified(run_id)


@pytest.mark.asyncio
async def test_sweep_does_not_refire_on_second_pass():
    """Tier 2: (b): a notified run is not re-fired on the next sweep."""
    run_registry = RunRegistry()
    run_id = _cancelled_run(run_registry, "1", "ctx-a")
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    poster = _RecordingPoster()

    await sweep_dispositions(run_registry, reg, post_fn=poster)
    await sweep_dispositions(run_registry, reg, post_fn=poster)

    # RED if the notified-set is dropped: the run would re-fire (the list would
    # carry the run_id twice).
    assert [c[1]["task_id"] for c in poster.calls] == [run_id]


@pytest.mark.asyncio
async def test_sweep_retries_failed_post():
    """Tier 2: (c): a failed POST leaves the run un-notified → retried next sweep
    (§24 forward-progress with retry)."""
    run_registry = RunRegistry()
    run_id = _cancelled_run(run_registry, "1", "ctx-a")
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")

    failing = _RecordingPoster(ok=False)
    await sweep_dispositions(run_registry, reg, post_fn=failing)
    assert not reg.is_notified(run_id)  # not marked on failure

    ok_poster = _RecordingPoster(ok=True)
    await sweep_dispositions(run_registry, reg, post_fn=ok_poster)
    assert ok_poster.calls and reg.is_notified(run_id)  # retried + succeeded


@pytest.mark.asyncio
async def test_sweep_skips_context_without_registered_webhook():
    """Tier 2: a cancelled run whose context has no webhook (e.g. pre-5b) is
    skipped (no channel) — no crash, no notify."""
    run_registry = RunRegistry()
    _cancelled_run(run_registry, "1", "ctx-a")
    reg = A2AWebhookRegistry()  # no webhook registered
    poster = _RecordingPoster()

    fired = await sweep_dispositions(run_registry, reg, post_fn=poster)
    assert fired == 0 and poster.calls == []


@pytest.mark.asyncio
async def test_reconcile_prunes_hard_deleted_notified():
    """Tier 2: (d): the notified-set self-prunes a run_id no longer present
    (hard-deleted / pruned by retention) — bounded by construction."""
    run_registry = RunRegistry()
    run_id = _cancelled_run(run_registry, "1", "ctx-a")
    reg = A2AWebhookRegistry()
    reg.register_webhook("ctx-a", "https://client.example/hook")
    reg.mark_notified("gone-run")  # a run no longer in the registry
    reg.mark_notified(run_id)

    await sweep_dispositions(run_registry, reg, post_fn=_RecordingPoster())

    # RED if reconcile is dropped: the notified-set grows unbounded with stale ids.
    assert not reg.is_notified("gone-run")
    assert reg.is_notified(run_id)  # still present → kept


def test_registry_persists_map_and_notified_across_reload(tmp_path):
    """Tier 2: (e): the contextId→webhook map + notified-set survive a reload (so
    a server restart neither loses a pending webhook nor re-fires a delivered one)."""
    path = tmp_path / "state" / "a2a_webhooks.json"
    reg = A2AWebhookRegistry(persist_path=path)
    reg.register_webhook("ctx-a", "https://client.example/hook")
    reg.mark_notified("ext-1")

    reloaded = A2AWebhookRegistry(persist_path=path)
    assert reloaded.webhook_for("ctx-a") == "https://client.example/hook"
    assert reloaded.is_notified("ext-1")
