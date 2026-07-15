"""Tier 2: FP-0063 PC — the LIVE `embed` tool path actually records embedding
cost (the production wiring, not the mechanism).

Why this file exists, separately from `test_op_embed.py`: those tests build an
`OpContext(...)` BY HAND, so they pin the MECHANISM (given a wired ctx, the op
records) but not the WIRING (that a real Session hands the op a wired ctx).
Both production call-sites could be deleted and every hand-built-ctx test would
stay green — which is exactly how the gap this PR fixes came to exist in the
first place: `ctx.budget_tracker` was a real field, with a real consumer
(`judge_output`, since removed — a clean-break op deletion), that no
router-dispatch host ever populated. `budget_tracker` itself was deleted from
`OpContext` once that consumer went away (0 readers / 0 writers verified by
grep); `embed`'s recording path below uses `ctx.budget_gateway` only.

So these tests drive the REAL chain the `embed` TOOL resolves at runtime:

    Session.__init__ -> RouterHostAdapter(budget_gateway=)
      -> build_resource_caller_state(host)
      -> RouterCallerState.op_context_factory = host.make_router_op_context
      -> tools/embed.py `_handle_embed` calls that factory
      -> execute_op(EmbedIROp, ctx) -> ctx.budget_gateway

Verified RED by stripping the production call-site (reported in the PR body):
removing `budget_gateway=self._budget` from Session's `RouterHostAdapter(...)`
construction turns all three of these tests RED, while every hand-built-ctx
test in `test_op_embed.py` stays green.

Note which host: the `embed` tool resolves `RouterHostAdapter.make_router_op_context`
(via `RouterCallerState.op_context_factory`), NOT `Session._make_router_op_context`
(which serves file/MCP ops that no embed op reaches). That distinction is the
whole point of testing through the real factory rather than asserting on a
builder chosen by reading.

Policy: no mocks — a real `Session` / `RouterHostAdapter` / `BudgetTracker` /
`BudgetGateway` and the real `embed` tool handler. Only the embedding PROVIDER
is a real-instance Fake (the network egress boundary), same
`FakeEmbeddingProvider` pattern `test_op_embed.py` uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.data.embedding.provider import EmbedBatchResult
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.session import Session
from reyn.tools.types import ToolContext, build_resource_caller_state

# A real litellm embedding-mode model, so the recorded figure is a real
# `litellm.model_cost` lookup rather than a fabricated rate.
_REAL_EMBEDDING_MODEL = "text-embedding-3-small"


class _RealModelFakeProvider:
    """Real EmbeddingProvider-protocol instance (not a mock) that reports a
    REAL, litellm-priceable model name so the cost path resolves an actual
    rate."""

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        return EmbedBatchResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts],
            model=_REAL_EMBEDDING_MODEL,
            total_tokens=1000,
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


def _session(tmp_path: Path, tracker: BudgetTracker) -> Session:
    agent_dir = tmp_path / ".reyn" / "agents" / "embedder"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return Session(
        agent_name="embedder",
        agent_role="r",
        output_language="en",
        budget_tracker=tracker,
        snapshot_path=agent_dir / "state" / "snapshot.json",
    )


async def _run_embed_tool_via_live_path(session: Session) -> dict:
    """Drive the real `embed` tool through the real router chain — the same
    `op_context_factory` resolution a live chat turn performs."""
    from reyn.tools.embed import _handle_embed

    router_state = await build_resource_caller_state(session._router_host)
    tool_ctx = ToolContext(
        events=session._chat_events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=router_state,
    )
    return await _handle_embed({"texts": ["hello", "world"]}, tool_ctx)


@pytest.mark.asyncio
async def test_live_embed_tool_records_agent_scope_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: driving the REAL `embed` tool through the REAL router chain
    records agent-scope embedding cost on the Session's BudgetTracker.

    RED if Session stops passing `budget_tracker` into RouterHostAdapter —
    the wiring, not the mechanism."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(
        _embed_mod, "get_provider", lambda *a, **kw: _RealModelFakeProvider(),
    )

    tracker = BudgetTracker(CostConfig())
    session = _session(tmp_path, tracker)

    result = await _run_embed_tool_via_live_path(session)
    assert result.get("status") != "error", result

    # Keyed by agent NAME — the key `Registry.agent_embedding_cost` reads.
    agg = tracker.agent_embedding_cost("embedder")
    assert agg.calls == 1, (
        "the live embed tool call must reach the agent-scope aggregate — "
        "0 calls means Session is not wiring budget_gateway into the host "
        "whose make_router_op_context the embed tool actually resolves"
    )
    assert agg.tokens == 1000
    assert agg.cost_usd > 0.0
    assert agg.cost_usd == result["cost_usd"]

    # The chat aggregate stays untouched by embedding activity, on the live
    # path too (not just in the hand-built-ctx unit tests).
    assert tracker.agent_cost_usd("embedder") == 0.0
    assert tracker.agent_cost_breakdown("embedder").total_cost == 0.0


@pytest.mark.asyncio
async def test_live_embed_tool_records_session_scope_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the same live chain records session-scope embedding cost on the
    Session's own BudgetGateway.

    RED if Session stops passing `budget_gateway` into RouterHostAdapter."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(
        _embed_mod, "get_provider", lambda *a, **kw: _RealModelFakeProvider(),
    )

    tracker = BudgetTracker(CostConfig())
    session = _session(tmp_path, tracker)

    result = await _run_embed_tool_via_live_path(session)
    assert result.get("status") != "error", result

    agg = session.embedding_cost  # the public Session-scope reader
    assert agg.calls == 1, (
        "the live embed tool call must reach the session-scope aggregate — "
        "0 calls means Session is not wiring budget_gateway into the host "
        "whose make_router_op_context the embed tool actually resolves"
    )
    assert agg.tokens == 1000
    assert agg.cost_usd == result["cost_usd"]
    # The chat session-scope figures stay untouched by embedding activity.
    assert session.total_cost_usd == 0.0
    assert session.total_cost_breakdown.total_cost == 0.0


@pytest.mark.asyncio
async def test_live_embed_tool_reaches_project_scope_via_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the live-path recording is what the project-scope reader sums —
    `project_embedding_cost` is non-zero after a real embed tool call, closing
    the loop from the interactive tool to the per-scope surface this PR adds."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(
        _embed_mod, "get_provider", lambda *a, **kw: _RealModelFakeProvider(),
    )

    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry

    tracker = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=tracker,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("embedder")
    session = reg.get_or_load("embedder")

    assert reg.project_embedding_cost().cost_usd == 0.0

    result = await _run_embed_tool_via_live_path(session)
    assert result.get("status") != "error", result

    project = reg.project_embedding_cost()
    assert project.calls == 1
    assert project.cost_usd > 0.0
    assert project.cost_usd == result["cost_usd"]
