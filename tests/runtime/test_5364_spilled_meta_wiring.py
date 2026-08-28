"""Tier 2: #5364 §1.2 — the write-time cap chokepoint (RouterLoop.feedback,
mirroring test_2425_step1c_chat_chokepoint.py's own real harness) stamps
``SPILLED_META_KEY``/``CONTENT_REF_META_KEY`` on the persisted history entry
exactly when an offload actually happened — never guessed from the
rendered content string's own shape (this repo's typed-over-form-sniffed
convention). This is the resolver's own write-side counterpart: without
this wiring, ``reyn.core.offload.history_content_resolve.resolve()`` has
no real signal to read (#5364, lead-coder: "resolver は呼び口と同じ PR で").
"""
from __future__ import annotations

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.config.chat import OffloadConfig
from reyn.data.workspace.media_store import MediaStore
from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.tool_result_cap import TRIGGER_CAP, cap_tool_result_content
from reyn.tools.scheme import ExecutionResult
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"
_BIG = "\n".join(f"line {i}: " + "z" * 60 for i in range(400))  # well over the offload trigger
_SMALL = "tiny result, stays inline"


class _RecordingHost:
    """Mirrors test_2425_step1c_chat_chokepoint.py's own ``_CapHost`` +
    captures every ``append_history_entry`` call so this test can assert
    on the persisted ``meta`` dict — the ONE thing ``RouterLoop.feedback``
    actually hands a real Session in production.

    architect (#5372): a fast unit-level stand-in, not a substitute for the
    real production chain — since
    ``test_the_real_production_chain_stamps_spilled_meta_end_to_end`` below
    closes the one risk a fake host posed here (wiring silently dropped
    between ``RouterHostAdapter``/``Session``/``ContextBudgetAdvisor``),
    this class stays legitimate for the fast per-branch coverage the 3
    tests above want."""

    offload_enabled = True

    def __init__(self, store: "MediaStore | None") -> None:
        self.media_store = store
        self.appended: list[dict] = []

    def cap_tool_result(
        self, content_str: str, *, content_type: "str | None" = None, on_offload=None,
        on_write_unavailable=None,
    ) -> str:
        if self.media_store is None:
            return content_str
        return cap_tool_result_content(
            content_str, cap_tokens=100, model=_MODEL,
            save_fn=self.media_store.save_tool_result, trigger=TRIGGER_CAP,
            use_chars4=True, content_type=content_type, on_offload=on_offload,
            on_write_unavailable=on_write_unavailable,
        )

    def media_followup_budget(self, _content_str: str) -> int:
        return 500

    def append_history_entry(self, **kwargs) -> None:
        self.appended.append(kwargs)


def _mcp_env(**data_extra) -> dict:
    data = {"kind": "mcp", "status": "ok", "server": "s", "tool": "t", "content": "", "media_blocks": []}
    data.update(data_extra)
    return {"status": "ok", "data": data, "_canonical_source": "mcp"}


def _feedback(env: dict, host: "_RecordingHost"):
    loop = RouterLoop(host=host, chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[env],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    return loop.feedback(result)


def test_offloaded_tool_result_is_stamped_spilled_with_a_content_ref(tmp_path) -> None:
    """Tier 2: content over the cap → the persisted entry's meta carries
    SPILLED_META_KEY=True and CONTENT_REF_META_KEY naming a file that
    actually exists and holds the ORIGINAL (pre-offload) content."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    host = _RecordingHost(store)

    _feedback(_mcp_env(content=_BIG), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert meta.get(SPILLED_META_KEY) is True
    ref = meta.get(CONTENT_REF_META_KEY)
    assert ref, f"expected a content ref in meta, got: {meta!r}"
    full = tmp_path / ref
    assert full.is_file(), f"the ref must name a file that actually exists: {full}"
    assert full.read_text(encoding="utf-8") == _BIG


def test_unoffloaded_tool_result_is_never_stamped_spilled(tmp_path) -> None:
    """Tier 2: content under the cap → no offload happens → meta carries
    neither key at all (never False/None as a placeholder — see
    SPILLED_META_KEY's own docstring: absence means "never spilled")."""
    store = MediaStore(project_root=tmp_path, session_id="test-session")
    host = _RecordingHost(store)

    _feedback(_mcp_env(content=_SMALL), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert SPILLED_META_KEY not in meta
    assert CONTENT_REF_META_KEY not in meta


def test_the_real_production_chain_stamps_spilled_meta_end_to_end(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect/lead-coder BLOCKING (#5372, head 8803bc020) — the 3
    tests above all drive ``_RecordingHost``, a fake that REPLACES the real
    ``RouterHostAdapter -> Session._cap_tool_result ->
    ContextBudgetAdvisor.cap_tool_result`` forwarding chain (#5364 §1.2's
    actual production path for ``on_offload``). Deleting any of those 3
    forwarding sites' own ``on_offload=on_offload`` left every test above
    green — the fake never exercised them.

    This test drives the REAL chain instead: a real ``Session`` (via
    ``make_session`` — no mocks, per ``tests/_support/router_host_adapter.py``'s
    own "real collaborators" contract) built with ``offload.enabled: true``,
    handing ``RouterLoop`` its real ``session.router_host`` (the actual
    ``RouterHostAdapter`` production wires — same object ``_build_router_waist``
    constructs, same as every real chat turn), and reading the persisted
    ``ChatMessage`` back off ``session.history`` (not the wire-rendered
    ``build_history()``, which the resolver already transforms — this test
    wants the raw stamped meta the resolver's OWN write-side counterpart)."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="real-chain-agent",
        multimodal_config=MultimodalConfig(),
        offload_config=OffloadConfig(enabled=True),
        compaction_config=CompactionConfig(use_chars4_estimate=True),
    )

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    result = ExecutionResult(
        tool_results=[_mcp_env(content=_BIG)],
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "mcp"}}],
        assistant_content="",
    )
    loop.feedback(result)

    (tool_msg,) = [m for m in session.history if m.role == "tool"]
    assert tool_msg.meta.get(SPILLED_META_KEY) is True, (
        f"real production chain never stamped SPILLED_META_KEY, meta={tool_msg.meta!r}"
    )
    ref = tool_msg.meta.get(CONTENT_REF_META_KEY)
    assert ref, f"expected a content ref in meta, got: {tool_msg.meta!r}"
    full = tmp_path / ref
    assert full.is_file(), f"the ref must name a file that actually exists: {full}"
    assert full.read_text(encoding="utf-8") == _BIG


def test_no_media_store_never_stamps_spilled(tmp_path) -> None:
    """Tier 2: accept-side — a host with no media_store configured (cap
    is identity, per _RecordingHost.cap_tool_result's own no-op branch)
    never stamps SPILLED_META_KEY even for content that WOULD have been
    offloaded had a store existed — proves the stamp is tied to an actual
    offload, not just "content is large"."""
    host = _RecordingHost(store=None)

    _feedback(_mcp_env(content=_BIG), host)

    (tool_entry,) = [e for e in host.appended if e["role"] == "tool"]
    meta = tool_entry["meta"]
    assert SPILLED_META_KEY not in meta
    assert CONTENT_REF_META_KEY not in meta
