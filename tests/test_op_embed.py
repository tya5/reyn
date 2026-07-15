"""Tier 1/2: `embed` typed op — FP-0057 Phase 1 (the raw embed primitive).

Tests use a real class implementing the `EmbeddingProvider` protocol
(`FakeEmbeddingProvider`, same pattern as `tests/test_op_recall.py`'s
`FakeEmbeddingProvider`) monkeypatched into `op_runtime.embed`'s
module-level `get_provider` — NOT `unittest.mock` (per
`docs/deep-dives/contributing/testing.ja.md`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import available_kinds, execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, EmbedIROp, Op
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Fake provider (real EmbeddingProvider-protocol instance, not a mock)
# ---------------------------------------------------------------------------

class FakeEmbeddingProvider:
    """Deterministic real EmbeddingProvider: a fixed 3-dim vector per text,
    scaled by input length so distinct texts get distinct (but reproducible)
    vectors — enough to assert shape/order without pinning provider internals."""

    def __init__(self) -> None:
        self._batch_size = 10
        self.received_texts: list[str] = []

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        self.received_texts.extend(texts)
        vectors = [[float(len(t)), 0.0, 1.0] for t in texts]
        return EmbedBatchResult(vectors=vectors, model=f"fake/{model}", total_tokens=len(texts))

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


def _make_ctx(tmp_path: Path, *, embedding_event_sink=None) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        embedding_event_sink=embedding_event_sink,
    )


# ---------------------------------------------------------------------------
# Tier 1: registration + control-ir round-trip
# ---------------------------------------------------------------------------

def test_embed_registered_in_op_kind_model_map() -> None:
    """Tier 1: `embed` is a first-class Control IR op kind (hard-rule sync:
    OP_KIND_MODEL_MAP <-> control-ir.md, #1983)."""
    assert "embed" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["embed"] is EmbedIROp
    assert "embed" in ALL_OP_KINDS
    assert "embed" in available_kinds()


def test_embed_op_round_trips_through_the_op_union() -> None:
    """Tier 1: an embed op dict validates against the discriminated `Op` union
    (the same envelope shape an LLM would emit)."""
    from pydantic import TypeAdapter

    adapter: TypeAdapter = TypeAdapter(Op)
    parsed = adapter.validate_python(
        {"kind": "embed", "texts": ["hello", "world"], "embedding_model": "standard"}
    )
    assert isinstance(parsed, EmbedIROp)
    assert parsed.texts == ["hello", "world"]
    assert parsed.embedding_model == "standard"


def test_embed_op_defaults_embedding_model_to_standard() -> None:
    """Tier 1: `embedding_model` defaults to "standard" when omitted."""
    op = EmbedIROp(kind="embed", texts=["x"])
    assert op.embedding_model == "standard"


# ---------------------------------------------------------------------------
# Tier 2: op-runtime dispatch behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_batch_returns_one_vector_per_text_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: embed is batch-granular (list -> list); vector order matches
    input text order and dimension matches the (fake) model's dimension."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=["a", "bb", "ccc"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["kind"] == "embed"
    # unpack: exactly one vector per input text, order-preserving
    vec_a, vec_bb, vec_ccc = result["vectors"]
    # each vector is 3-dim (matches the fake provider's declared dimension) and
    # its first component encodes the source text's length (fake's deterministic
    # scheme) -> confirms per-text correspondence, not just count
    x_a, y_a, z_a = vec_a
    x_bb, y_bb, z_bb = vec_bb
    x_ccc, y_ccc, z_ccc = vec_ccc
    assert (x_a, x_bb, x_ccc) == (1.0, 2.0, 3.0)
    assert result["total_tokens"] == 3


@pytest.mark.asyncio
async def test_embed_empty_texts_returns_empty_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: an empty `texts` list is a no-op (empty vectors, no provider
    call) rather than an error."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=[], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["vectors"] == []
    assert fake.received_texts == []


@pytest.mark.asyncio
async def test_embed_default_gate_allows_dispatch_without_permission_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: embed is default-ALLOW (a compute op) — it dispatches and
    succeeds with no `permission_resolver` wired into the OpContext, mirroring
    recall/index_query's no-gate posture."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    assert ctx.permission_resolver is None
    op = EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # unpack: exactly one vector for the single input text
    (only_vector,) = result["vectors"]
    assert only_vector


# ---------------------------------------------------------------------------
# Tier 2: redaction-egress seam (co-vet #3) reachability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_pre_embed_redaction_seam_fires_on_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a secret-shaped text is redacted BEFORE it reaches the
    provider (the PRE-call egress seam, co-vet #3) — the provider never sees
    the raw credential value, and the seam firing is recorded as an
    `embed_secret_redacted` audit-event."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    secret_text = 'api_key = "abcdefghijklmnopqrstuvwxyz123456"'
    op = EmbedIROp(kind="embed", texts=[secret_text, "harmless text"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # the provider (= the egress boundary to an external embedding API) never
    # receives the raw secret value
    assert "abcdefghijklmnopqrstuvwxyz123456" not in fake.received_texts[0]
    assert "REDACTED" in fake.received_texts[0]
    # the harmless text is untouched
    assert fake.received_texts[1] == "harmless text"
    # the seam firing is observable (P6 audit-event trace)
    assert any(e.type == "embed_secret_redacted" for e in ctx.events.all())


# ---------------------------------------------------------------------------
# Tier 1: EMBED ToolDefinition — registered, default-allow
# ---------------------------------------------------------------------------

def test_embed_tool_registered_default_allow() -> None:
    """Tier 1: the `embed` ToolDefinition is registered in the default tool
    registry with gates.router=allow / gates.phase=allow (default-ALLOW per
    the FP-0057 design — a compute op, individually name-gateable via
    contextual_gate rather than requiring an ask-gate by default)."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    tool = registry.lookup("embed")
    assert tool is not None
    assert tool.gates.router == "allow"
    assert tool.gates.phase == "allow"


def test_embed_op_kind_has_a_contextual_gate_entry() -> None:
    """Tier 1: `embed` is registered in the contextual-gate op-kind table (so a
    per-session capability narrowing can name-gate it individually, even
    though its default posture is allow) — same shape as index_query/recall."""
    from reyn.core.op_runtime.contextual_gate import op_kind_tool_names

    names = op_kind_tool_names("embed")
    assert "embed" in names


@pytest.mark.asyncio
async def test_embed_no_redaction_event_when_no_secret_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the redaction seam is a no-op (no event, text unchanged) when
    no secret pattern is present — it must not false-positive on ordinary text."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=["just some ordinary sentence"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert fake.received_texts == ["just some ordinary sentence"]
    assert not any(e.type == "embed_secret_redacted" for e in ctx.events.all())


# ---------------------------------------------------------------------------
# Tier 2: FP-0057 #2856 Part A — ctx.embedding_event_sink forwarding seam
# ---------------------------------------------------------------------------
#
# ActionEmbeddingIndex used to call `provider.embed(...)` PROVIDER-DIRECT so
# it could carry a session-scoped provider's TUI model-download-status
# event_sink — bypassing this op's PRE-embed redaction-egress seam above.
# Part A closes that bypass by having ActionEmbeddingIndex route through this
# op instead; the seam that makes that possible WITHOUT losing the TUI
# status rows is this op forwarding `ctx.embedding_event_sink` into its own
# per-call `_resolve_provider(event_sink=...)` — verified here directly
# (independent of ActionEmbeddingIndex, which has its own coverage in
# tests/test_action_embedding_index.py).

@pytest.mark.asyncio
async def test_embed_forwards_ctx_embedding_event_sink_to_provider_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when ctx carries an embedding_event_sink, the op forwards THAT
    exact callable into get_provider(event_sink=...) — the seam that keeps
    the TUI model-download status rows alive for a caller (ActionEmbeddingIndex)
    that no longer holds its own provider instance."""
    fake = FakeEmbeddingProvider()
    received_event_sinks: list[object] = []

    def _spy_get_provider(*_args, event_sink=None, **_kwargs):
        received_event_sinks.append(event_sink)
        return fake

    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", _spy_get_provider)

    def _tui_sink(kind: str, text: str, meta: dict) -> None:
        pass

    ctx = _make_ctx(tmp_path, embedding_event_sink=_tui_sink)
    op = EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # unpack: exactly one get_provider() call was made for this single embed
    (forwarded_sink,) = received_event_sinks
    assert forwarded_sink is _tui_sink, (
        "ctx.embedding_event_sink must reach get_provider(event_sink=...) "
        "UNCHANGED (identity), not wrapped/dropped"
    )


@pytest.mark.asyncio
async def test_embed_forwards_none_when_ctx_embedding_event_sink_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsify — stripping ctx.embedding_event_sink (force None — the
    OpContext default) means get_provider() receives event_sink=None — the
    RED counterpart of the test above, confirming the forwarding is actually
    load-bearing (not a no-op wiring that would "pass" either way)."""
    fake = FakeEmbeddingProvider()
    received_event_sinks: list[object] = []

    def _spy_get_provider(*_args, event_sink=None, **_kwargs):
        received_event_sinks.append(event_sink)
        return fake

    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", _spy_get_provider)

    ctx = _make_ctx(tmp_path)  # embedding_event_sink defaults to None
    assert ctx.embedding_event_sink is None
    op = EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    (forwarded_sink,) = received_event_sinks
    assert forwarded_sink is None, (
        "with no sink on ctx, get_provider() must receive event_sink=None "
        "(no TUI status forwarded) — the RED case for the test above"
    )


# ---------------------------------------------------------------------------
# Tier 2: FP-0063 PC -- embedding cost in the op's output metadata + recording
# ---------------------------------------------------------------------------
#
# `embed`'s result envelope already carried `total_tokens`/`model`; this PR
# adds `cost_usd`/`priced` (X2c) and records the call into whichever of
# `ctx.budget_tracker` (agent/project scope) / `ctx.budget_gateway` (session
# scope) are wired -- an INDEPENDENT aggregate, never the chat `CostBreakdown`.

# A real litellm embedding-mode model (verified present, mode="embedding") so
# these tests exercise the actual litellm.model_cost lookup, not a fake rate.
_REAL_EMBEDDING_MODEL = "text-embedding-3-small"


@pytest.mark.asyncio
async def test_embed_output_carries_cost_usd_and_priced_for_a_real_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the op's output metadata carries `cost_usd` (a positive dollar
    figure) and `priced=True` for a model litellm can price -- `total_tokens`/
    `model` alone (the pre-existing shape) no longer suffice."""
    class _RealModelProvider(FakeEmbeddingProvider):
        async def embed(self, texts, model):
            return EmbedBatchResult(
                vectors=[[1.0, 0.0, 0.0] for _ in texts],
                model=_REAL_EMBEDDING_MODEL,
                total_tokens=1000,
            )

    fake = _RealModelProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=["a", "b"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["priced"] is True
    assert result["cost_usd"] is not None
    assert result["cost_usd"] > 0.0


@pytest.mark.asyncio
async def test_embed_output_unpriced_model_reports_none_cost_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsify the silent-$0.00 failure mode -- a model litellm cannot
    price yields `cost_usd=None` / `priced=False`, NOT `cost_usd=0.0` (which
    would be indistinguishable from a real free call)."""
    fake = FakeEmbeddingProvider()  # returns model="fake/standard" -- unpriced
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=["a", "bb"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["priced"] is False
    assert result["cost_usd"] is None


@pytest.mark.asyncio
async def test_embed_empty_texts_reports_zero_priced_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the empty-texts no-op path (no provider call at all) reports a
    real, priced $0.00 -- distinct from the unpriced-model None case above."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(kind="embed", texts=[], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["cost_usd"] == 0.0
    assert result["priced"] is True


@pytest.mark.asyncio
async def test_embed_records_into_budget_tracker_agent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when `ctx.budget_tracker` is wired, the op records the call
    into the INDEPENDENT per-agent embedding-cost aggregate (agent/project
    scope) -- via `ctx.agent_id`, mirroring `judge_output.py`'s existing
    `ctx.budget_tracker` recording precedent."""
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig

    class _RealModelProvider(FakeEmbeddingProvider):
        async def embed(self, texts, model):
            return EmbedBatchResult(
                vectors=[[1.0, 0.0, 0.0] for _ in texts],
                model=_REAL_EMBEDDING_MODEL,
                total_tokens=2000,
            )

    fake = _RealModelProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    tracker = BudgetTracker(CostConfig())
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        budget_tracker=tracker,
        agent_id="agent-x",
    )
    op = EmbedIROp(kind="embed", texts=["a", "b"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    agg = tracker.agent_embedding_cost("agent-x")
    assert agg.calls == 1
    assert agg.tokens == 2000
    assert agg.cost_usd == result["cost_usd"]
    # The chat aggregate is untouched by this embed call.
    assert tracker.agent_cost_usd("agent-x") == 0.0


@pytest.mark.asyncio
async def test_embed_records_into_budget_gateway_session_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: when `ctx.budget_gateway` is wired (the Session-scope adapter),
    the op ALSO records the call into its session-scope embedding-cost
    aggregate, independent of the agent-scope tracker above."""
    from reyn.runtime.services.budget_gateway import BudgetGateway

    class _RealModelProvider(FakeEmbeddingProvider):
        async def embed(self, texts, model):
            return EmbedBatchResult(
                vectors=[[1.0, 0.0, 0.0] for _ in texts],
                model=_REAL_EMBEDDING_MODEL,
                total_tokens=500,
            )

    fake = _RealModelProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    events = EventLog()
    ws = Workspace(events=events)
    gateway = BudgetGateway(budget_tracker=None, events=events, agent_name="agent-y")
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        budget_gateway=gateway,
    )
    op = EmbedIROp(kind="embed", texts=["a"], embedding_model="standard")
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert gateway.embedding_cost.calls == 1
    assert gateway.embedding_cost.tokens == 500
    assert gateway.embedding_cost.cost_usd == result["cost_usd"]
    # Session-scope chat total is untouched by this embed call.
    assert gateway.total_cost_usd == 0.0
