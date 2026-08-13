"""Tier 2: routing_decided P6 event emitted by RouterLoop (FP-0034 Phase 3).

Invariant tests:

1. invoke_action call → routing_decided(source="invoke_action", outcome="success")
2. error tool result → outcome="error"
3. non-catalog tool (invoke_skill) → NO routing_decided event
4. action_name absent in invoke_action args → no event
5. a direct catalog call not surfaced via invoke_action → source="ars_direct"
   (#4552: the hot-list feature that used to produce a second, distinguishable
   "hot_list_alias" source value is retired — every bare catalog dispatch now
   classifies as "ars_direct" unconditionally.)

No MagicMock / AsyncMock.  call_llm_tools is replaced with a real
coroutine function via monkeypatch.  FakeRouterHost and _FakeEventLog are
minimal real collaborators following the pattern in test_replay_skill_router.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop

# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=2)


def _tool_result(calls: list[dict]) -> LLMToolCallResult:
    """Build an LLMToolCallResult that contains one tool_call round."""
    tool_calls = [
        {
            "id": c.get("id", f"tc_{i}"),
            "type": "function",
            "function": {
                "name": c["name"],
                "arguments": (
                    json.dumps(c["args"]) if isinstance(c.get("args"), dict)
                    else c.get("args", "{}")
                ),
            },
        }
        for i, c in enumerate(calls)
    ]
    return LLMToolCallResult(
        content=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


def _text_result(text: str = "done") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


# ---------------------------------------------------------------------------
# _FakeEventLog — minimal real collaborator (records emitted events)
# ---------------------------------------------------------------------------

class _FakeEventLog:
    """Minimal events stub: records emitted events, no subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


# ---------------------------------------------------------------------------
# _FakeRouterHost — minimal real RouterLoopHost with universal wrappers on
# ---------------------------------------------------------------------------

class _FakeRouterHost:
    """Minimal host for routing_decided P6 event tests.

    universal_wrappers_enabled=True by default so routing_decided fires.
    """

    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(
        self,
        *,
        universal_wrappers_enabled: bool = True,
        skills: list[dict] | None = None,
    ) -> None:
        self._universal_wrappers_enabled = universal_wrappers_enabled
        self._skills = skills or []
        self.outbox: list[dict] = []
        self._events = _FakeEventLog()

    @property
    def events(self) -> _FakeEventLog:
        return self._events

    def get_universal_wrappers_enabled(self) -> bool:
        return self._universal_wrappers_enabled

    def get_action_embedding_index(self):  # type: ignore[return]
        return None

    def get_embedding_provider(self):  # type: ignore[return]
        return None

    def get_embedding_model_class(self):  # type: ignore[return]
        return None

    def list_available_skills(self) -> list[dict]:
        return list(self._skills)

    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> dict | None:
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    def resolve_model(self, name: str) -> str:
        return "fake-model"

    async def put_outbox(
        self, *, kind: str, text: str, meta: dict, persist: bool = True,
    ) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def reyn_repo_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_repo_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    async def run_skill_awaitable(
        self, *, skill: str, input: dict, chain_id: str
    ) -> dict:
        """Stub skill runner: always returns success so invoke_action completes."""
        return {"status": "finished", "data": {"result": f"{skill} ran"}}


# ---------------------------------------------------------------------------
# Helper — build loop + run one turn with a pre-scripted LLM sequence
# ---------------------------------------------------------------------------

def _run_with_llm_sequence(
    host: _FakeRouterHost,
    llm_turns: list[LLMToolCallResult],
    monkeypatch: pytest.MonkeyPatch,
    contextual_permission: object | None = None,
) -> None:
    """Drive RouterLoop.run() using a real coroutine sequence as call_llm_tools.

    The stub pops from llm_turns on each call; after exhaustion raises
    StopIteration (should not be reached in well-constructed tests).
    No MagicMock or AsyncMock — only a real coroutine function.
    """
    turns = list(llm_turns)  # copy so caller can reuse

    async def _fake_call_llm_tools(**kwargs: object) -> LLMToolCallResult:
        return turns.pop(0)

    loop = RouterLoop(
        host=host, chain_id="chain-test", max_iterations=5,
        scheme_name="universal-category",  # #1657
        contextual_permission=contextual_permission,
    )
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools)
    asyncio.run(loop.run("hello", []))


def _routing_decided_events(host: _FakeRouterHost) -> list[dict]:
    return [e for e in host.events.emitted if e["type"] == "routing_decided"]


# ---------------------------------------------------------------------------
# Test 1: invoke_action → routing_decided(source="invoke_action", outcome="success")
# ---------------------------------------------------------------------------


def test_routing_decided_emitted_for_invoke_action(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: invoke_action call emits routing_decided with source='invoke_action' and outcome='success'.

    #3450: was ``action_name="skill__foo"`` — a name #3429 (removal of the
    qualified-spelling convention) left non-resolvable, so this actually
    exercised ``invoke_action``'s ``UnknownActionError`` branch (a bare
    ``{"error": ...}`` handler return) the whole time, silently miscounted
    as outcome="success" by the very envelope bug #3450 fixes. Switched to
    ``list_agents`` — a real, always-resolvable action given this host's
    ``list_available_agents`` stub — so this test exercises an actual
    success path, not an error path mislabeled by the pre-#3450 defect.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # Turn 1: LLM calls invoke_action(action_name="list_agents")
    # Turn 2: LLM emits text reply (stop)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "invoke_action", "args": {"action_name": "list_agents", "args": {}}}]),
            _text_result("ok"),
        ],
        monkeypatch,
    )

    events = _routing_decided_events(host)
    (ev,) = events
    assert ev["action_name"] == "list_agents"
    assert ev["source"] == "invoke_action"
    assert ev["outcome"] == "success"
    assert ev["chain_id"] == "chain-test"


# ---------------------------------------------------------------------------
# Test 3: error result → outcome="error"
# ---------------------------------------------------------------------------


def test_routing_decided_outcome_error_on_tool_error(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: routing_decided outcome='error' when the tool result is an error.

    Driven through the PRE-DISPATCH exclude gate: a contextual narrowing that
    denies ``read_file`` makes the call return the raw ``tool_excluded`` error
    row that ``_excluded_result`` produces, which is the shape the outcome
    discriminator reads.

    #3429 changed the name this test uses. It was ``bogus_category__action`` —
    an UNRESOLVABLE qualified name, whose bare ``{"error": ...}`` came from
    ``dispatch_tool``'s unknown-tool rejection, and which reached the audit arm
    at all only because that arm fired on any ``__``-containing call. The arm
    now keys on catalog membership, so an unresolvable name emits nothing
    (correctly: that is a rejected call, not a routing decision).

    ★ A HANDLER-level failure was ONCE a different, unfixed story (pre-#3450):
    ``dispatch_tool`` wrapped it as ``{"status": "ok", "data": {..., "error":
    ...}}``, which this discriminator read as a success. #3450 fixed that
    class at the envelope (``dispatch_tool`` now promotes a handler-declared
    error to its OWN outer ``status``) — see
    ``test_routing_decided_outcome_error_on_handler_declared_error`` below for
    the now-reachable HANDLER-level case, driven through the same real
    dispatch_tool + RouterLoop chokepoints as this test.
    """
    from reyn.security.permissions.effective import ContextualPermission

    host = _FakeRouterHost(universal_wrappers_enabled=True)

    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "read_file", "args": {"path": "/nonexistent/x"}}]),
            _text_result("done"),
        ],
        monkeypatch,
        contextual_permission=ContextualPermission(tool_deny=frozenset({"read_file"})),
    )

    events = _routing_decided_events(host)
    (ev,) = events
    assert ev["action_name"] == "read_file"
    assert ev["outcome"] == "error", (
        f"Expected outcome='error' for an OS-rejected action, got {ev['outcome']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3b (#3450): HANDLER-level failure → outcome="error" AND the SAME
# turn's role:tool body shows the failure (intermediate-cut witness — both
# the audit log and the LLM-visible text, not either alone).
# ---------------------------------------------------------------------------


def test_routing_decided_outcome_error_on_handler_declared_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: #3450 — a handler that returns ``{"error": ...}`` WITHOUT
    raising (``list_mcp_servers``'s MCP-failure sentinel, #3441/#3429) drives
    routing_decided outcome="error" AND an "Error (...): ..." role:tool body
    — neither read is fooled by dispatch_tool's success wrap anymore.

    Pre-#3450: dispatch_tool wrapped the handler's return as ``{"status":
    "ok", "data": {"error": "...", ...}}``. The LLM read the outer "ok" and
    never opened ``data`` to find the failure (#3450's original report), and
    routing_decided's outcome discriminator — which reads this SAME outer
    envelope, not the rendered text — recorded outcome="success" for a call
    whose handler plainly failed (the #3429 arc's measurement that broadened
    this issue's scope). Test 3 above could only reach the OS-level
    ``tool_excluded`` shape (already a top-level ``{"status": "error", ...}``
    before #3450); THIS test drives the previously-unreachable HANDLER-level
    shape through the real dispatch_tool + invoke_action + RouterLoop.feedback()
    chokepoints — no fabricated envelope.

    Strip-falsify: reverting ``_handler_declared_error``'s promotion in
    ``core/dispatch/dispatcher.py`` (or the ``routing_decided`` outcome
    derivation in ``router_loop.py`` back to its pre-#3450 form) turns BOTH
    assertions below RED — the outer envelope's ``status`` goes back to "ok"
    and the role:tool body falls back to the generic (non-"Error (...)"...)
    canonical rendering of a one-entry ``{"servers": [{"error": ...}]}`` list.
    """
    class _McpFailureHost(_FakeRouterHost):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.history_entries: list[dict] = []

        async def mcp_list_servers(self) -> list[dict]:
            # The real Session-layer sentinel #3441 documents (a Cancelled /
            # MCPFault / unresolved-config failure surfaced without raising).
            return [{"error": "boom, mcp handshake failed"}]

        def append_history_entry(self, *, role, content, meta=None, **kw) -> None:
            self.history_entries.append(
                {"role": role, "content": content, "meta": meta or {}},
            )

    host = _McpFailureHost(universal_wrappers_enabled=True)

    _run_with_llm_sequence(
        host,
        [
            _tool_result([{
                "name": "invoke_action",
                "args": {"action_name": "list_mcp_servers", "args": {}},
            }]),
            _text_result("done"),
        ],
        monkeypatch,
    )

    # (1) The audit log: routing_decided must record the handler's failure.
    events = _routing_decided_events(host)
    (ev,) = events
    assert ev["action_name"] == "list_mcp_servers"
    assert ev["outcome"] == "error", (
        f"Expected outcome='error' for a handler-declared failure, got {ev['outcome']!r}"
    )

    # (2) The LLM-visible body from the SAME turn must ALSO show the failure —
    # the intermediate-cut witness #3450 requires both, not the audit log alone.
    tool_entries = [e for e in host.history_entries if e["role"] == "tool"]
    (entry,) = tool_entries
    assert entry["content"].startswith("Error ("), entry["content"]
    assert "boom, mcp handshake failed" in entry["content"]


# ---------------------------------------------------------------------------
# Test 4: non-catalog tool → NO routing_decided event
# ---------------------------------------------------------------------------


def test_routing_decided_not_emitted_for_non_catalog_tool(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: plain tool without '__' and not invoke_action emits no routing_decided."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # Turn 1: LLM calls list_skills (plain OS tool, no '__')
    # The handler will succeed (returns a list) and the loop continues.
    # Turn 2: text reply.
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "list_skills", "args": {}}]),
            _text_result("ok"),
        ],
        monkeypatch,
    )

    events = _routing_decided_events(host)
    assert events == [], (
        f"routing_decided must NOT fire for non-catalog tool 'list_skills', "
        f"but got: {events}"
    )


# ---------------------------------------------------------------------------
# Test 5: action_name absent in invoke_action args → no event
# ---------------------------------------------------------------------------


def test_routing_decided_skipped_when_action_name_empty(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: invoke_action call with missing action_name does not emit routing_decided."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # invoke_action with empty args (no action_name key)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "invoke_action", "args": {}}]),
            _text_result("ok"),
        ],
        monkeypatch,
    )

    events = _routing_decided_events(host)
    assert events == [], (
        f"routing_decided must NOT fire when action_name is absent/empty, "
        f"but got: {events}"
    )


# ---------------------------------------------------------------------------
# Test 6 (issue #241): qualified-name direct call NOT in tools[] → "ars_direct"
# ---------------------------------------------------------------------------


def test_routing_decided_source_ars_direct_for_action_not_in_catalog(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: an action name not in tools[] tags ``source="ars_direct"``.

    Issue #241 originally distinguished "the alias was a real hot-list entry
    the LLM used correctly" (= name actually surfaced in tools[]) from "the
    LLM picked a name from the ARS text and called it directly" (= name
    appeared only in invoke_action.description's ARS block). #4552 retired
    the hot-list feature entirely — a bare catalog name can structurally
    never land in tools[]/self._catalog anymore, so ``source`` is now always
    ``"ars_direct"`` for this shape (see ``_emit_routing_decided`` in
    router_loop.py). This test now pins that constant classification rather
    than a two-way split.

    #3429: this used ``bogus_category__action``, which qualified as a "direct
    catalog call" only because the arm tested for ``__``. The label under test
    is about CATALOG LANDING, so the name has to be a real action — which is
    what ``read_file`` is here. The error outcome is incidental.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "read_file", "args": {"path": "/nonexistent/x"}}]),
            _text_result("done"),
        ],
        monkeypatch,
    )
    events = _routing_decided_events(host)
    (ev,) = events
    assert ev["action_name"] == "read_file"
    assert ev["source"] == "ars_direct", (
        f"Expected source='ars_direct' for an action not in the catalog, "
        f"got {ev['source']!r}"
    )


# ---------------------------------------------------------------------------
# Removal-invariant: invoke_action.description carries no action enumeration
# ---------------------------------------------------------------------------


def test_invoke_action_description_has_no_ars_block(monkeypatch: pytest.MonkeyPatch):
    """Tier 2: #187 STEP 1c — invoke_action's description carries NO action
    enumeration. Owner principle: actions are listed ONLY by list_actions and
    their schemas ONLY by describe_action. The former ARS block (B37/B38), which
    inlined the whole session action catalog into invoke_action.description, is
    removed — this is the load-bearing guard against re-adding a second
    enumeration surface to the wrapper description.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    captured: dict = {}

    async def _capture_tools(**kwargs: object) -> LLMToolCallResult:
        captured["tools"] = kwargs.get("tools")
        return _text_result("ok")  # finish immediately

    loop = RouterLoop(host=host, chain_id="chain-test", max_iterations=5, scheme_name="universal-category")  # #1657
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _capture_tools)
    asyncio.run(loop.run("hello", []))

    tools = captured["tools"]
    invoke = next(t for t in tools if t["function"]["name"] == "invoke_action")
    desc = invoke["function"]["description"]
    # The ARS block's headers (public surface the LLM saw) must be absent.
    assert "ACTION ARG SCHEMAS" not in desc
    assert "canonical keys for all session-visible actions" not in desc


# ---------------------------------------------------------------------------
# #3455: coverage-hole fix — routing_decided emitted at the dispatch
# chokepoint (_dispatch_resolved) regardless of which entry surface the
# model used, INCLUDING the flat/default bare-name dispatch shape that the
# prior ``if _univ_enabled:``-gated emit never covered.
# ---------------------------------------------------------------------------


def test_routing_decided_emitted_for_bare_direct_call_when_universal_wrappers_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: #3455 — bare-name direct catalog dispatch emits routing_decided
    even with ``universal_wrappers_enabled=False`` (the opt-out shape an
    operator gets from ``tool_use.universal_wrappers_enabled: false``
    in reyn.yaml — #4552 PR-3 moved this from
    ``action_retrieval.universal_wrappers_enabled``: flat bare-name
    ``tools=``, no ``invoke_action`` wrapper at
    all — the production default, since PR-3b-iv, is actually ``True``).

    This is the actual coverage hole #3455 reports. Before the fix, the
    emit lived in ``run_loop`` inside ``if _univ_enabled:`` — with wrappers
    off that whole block was skipped unconditionally, so EVERY catalog
    dispatch through the flat bare-name tool shape produced NO
    ``routing_decided`` event, even though ``dispatch_tool`` ran the exact
    same real catalog dispatch it always does. The fix relocates the emit
    into ``_dispatch_resolved`` — the single chokepoint every dispatch
    (wrapped or bare) funnels through — so coverage no longer depends on
    which entry surface the model used, or on the ``_univ_enabled`` flag.

    Strip-falsify: commenting out the ``self._emit_routing_decided(...)``
    call in ``RouterLoop._dispatch_resolved`` turns this RED (0 events)
    while every other routing_decided test that uses
    ``universal_wrappers_enabled=True`` stays unaffected — proving this
    test is the one pinned on the new (previously-uncovered) path.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=False)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "list_agents", "args": {"path": "."}}]),
            _text_result("done"),
        ],
        monkeypatch,
    )

    events = _routing_decided_events(host)
    (ev,) = events
    assert ev["action_name"] == "list_agents"
    assert ev["outcome"] == "success"
    assert ev["chain_id"] == "chain-test"


def test_routing_decided_both_entry_surfaces_no_double_emit(
    monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: #3455 intermediate-cut witness — a bare direct call AND an
    ``invoke_action``-wrapped call in the SAME round both drive
    ``routing_decided`` through the real dispatch chokepoint, with the
    correct per-call outcome, and with NO double-emit (2 dispatched tool
    calls → exactly 2 routing_decided events, not 4 and not 0).

    ``universal_wrappers_enabled=True`` here so both entry surfaces are
    simultaneously available to the (simulated) model in one round —
    the flat/default (wrappers-off) coverage is pinned separately above.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([
                {"name": "list_agents", "args": {"path": "."}},
                {
                    "name": "invoke_action",
                    "args": {"action_name": "skill_list", "args": {}},
                },
            ]),
            _text_result("done"),
        ],
        monkeypatch,
    )

    events = _routing_decided_events(host)
    # Behavioral (not a size pin): the MULTISET of action_names emitted must
    # be exactly one "list_agents" + one "skill_list" — no double-emit (a
    # dup would surface as a repeated name in this sorted list) and no
    # missing emit (a drop would surface as a missing name).
    assert sorted(e["action_name"] for e in events) == ["list_agents", "skill_list"]
    by_action = {e["action_name"]: e for e in events}
    # A bare direct call not routed via invoke_action always classifies as
    # "ars_direct" (#4552: the hot-list feature retired) — the #229/#3429
    # salvage path.
    assert by_action["list_agents"]["source"] == "ars_direct"
    assert by_action["list_agents"]["outcome"] == "success"
    assert by_action["skill_list"]["source"] == "invoke_action"
    assert by_action["skill_list"]["outcome"] == "success"
