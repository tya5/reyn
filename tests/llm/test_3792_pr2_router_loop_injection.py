"""Tier 2: #3792 PR2 — the RouterLoop-level wire-position witness for mid-turn
``CLIENT_INPUT`` injection.

The Session-level peek/commit/origin-gate/carry-forward/truncate-falsify/
loop-valve witnesses live in ``tests/core/test_3792_pr2_session_injection.py``.
This file covers what ONLY ``RouterLoop.run_loop``'s actual seam code can
witness: the injected message really lands in the outgoing ``messages``
list, at the position the wire format requires, and the commit only fires
once the splice is confirmed safe.
"""
from __future__ import annotations

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeRouterHost, text_result, tool_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


class _InjectingHost(FakeRouterHost):
    """A host with ONE eligible injection candidate queued, real 2-phase
    peek/commit (not PR1's always-None stub) — mirrors
    ``Session._peek_mid_turn_injection`` / ``_commit_mid_turn_injection``'s
    contract closely enough to witness the RouterLoop-side wiring, without
    duplicating the full real Session machinery this file does not own."""

    def __init__(self, *, injection_text: str = "mid-turn note", arrives_after_round: int = 0) -> None:
        super().__init__()
        self._queued: dict | None = {
            "payload": {"text": injection_text, "chain_id": "inj-chain", "meta": {}},
            "msg_id": "inj-1",
        }
        self.committed_msg_ids: list[str] = []
        # Mirrors real production timing: the queue only has something for
        # peek to find once the client's mid-turn message actually arrives —
        # modeled here as "not yet queued" for the first N peeks.
        self._peeks_before_arrival = arrives_after_round
        self._peek_count = 0

    async def peek_mid_turn_injection(self) -> dict | None:
        self.call_order.append("peek_mid_turn_injection")
        self._peek_count += 1
        if self._peek_count <= self._peeks_before_arrival:
            return None
        return self._queued

    async def commit_mid_turn_injection(self, msg_id: str) -> None:
        self.call_order.append("commit_mid_turn_injection")
        self.committed_msg_ids.append(msg_id)
        self._queued = None


def _loop(host: FakeRouterHost, llm, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(
        host=host, chain_id="chain-3792-pr2", max_iterations=max_iterations,
        llm_caller=llm,
    )


@pytest.mark.asyncio
async def test_injected_message_lands_at_the_tail_after_a_completed_round() -> None:
    """Tier 2: #3792 — an eligible injection is appended to ``messages`` as a
    plain ``role="user"`` entry, and the round-2 send actually receives it
    (not silently dropped, not misplaced ahead of the round-1 tool_calls
    pairing).

    Falsification (performed during review): commenting out
    ``messages = [*messages, _injected_msg]`` in ``RouterLoop.run_loop``
    makes this test go RED — the injected text never appears in either
    call's ``messages`` kwarg.
    """
    host = _InjectingHost(
        injection_text="please also check the logs", arrives_after_round=1,
    )
    seen_messages: list[list[dict]] = []

    class _RecordingLLM:
        def __init__(self) -> None:
            self.call_count = 0
            self._script = [
                tool_result([{"name": "read_file", "args": {"path": "x"}}]),
                text_result("done"),
            ]

        async def __call__(self, **kwargs) -> LLMToolCallResult:
            seen_messages.append(list(kwargs["messages"]))
            result = self._script[self.call_count]
            self.call_count += 1
            return result

    loop = _loop(host, _RecordingLLM())
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )

    # Round 1's own send must NOT see the injection (it "arrives" only after
    # round 1, modeling the real timing: the client's mid-turn message is
    # not in the queue yet when round 1 sends).
    round_1_messages = seen_messages[0]
    assert round_1_messages == [{"role": "user", "content": "hi"}]

    # Round 2's send DOES see it, appended after round 1's completed
    # tool_calls/tool_result pair — at the tail, unmoved by wire repair.
    round_2_messages = seen_messages[1]
    assert round_2_messages[-1] == {
        "role": "user", "content": "please also check the logs",
    }
    assert round_2_messages[-2]["role"] == "tool", (
        "sanity: the injection must land AFTER round 1's tool result, not "
        "spliced in ahead of it or between the assistant(tool_calls) message "
        "and its result"
    )
    assert host.committed_msg_ids == ["inj-1"]


@pytest.mark.asyncio
async def test_commit_receives_the_peeked_items_own_msg_id() -> None:
    """Tier 2: #3792 — ``commit_mid_turn_injection`` is called with the
    SAME ``msg_id`` the triggering ``peek_mid_turn_injection()`` call
    returned, after (not before) the append + wire-position assert have
    both already happened in the same code path (structural — an
    ``AssertionError`` raised by the assert would propagate straight out of
    ``run_loop``, and Python's control flow makes the commit call below it
    textually and dynamically unreachable in that case; there is no
    production input that reaches this seam with the injected message
    NOT at the tail — the assert is a permanent by-construction pass, not
    a reachable failure branch, given ``repair_tool_call_pairing`` only
    ever pulls scattered tool-RESULT messages forward to their call and
    never reorders an unrelated trailing message like the injection).

    Falsification (performed during review): passing a hardcoded wrong
    literal (e.g. ``"wrong-id"``) instead of ``_injection["msg_id"]`` to
    ``_commit_injection_fn`` in ``RouterLoop.run_loop`` makes this test go
    RED — ``host.committed_msg_ids`` would read ``["wrong-id"]``.
    """
    host = _InjectingHost()
    llm = _one_round_success_llm()
    loop = _loop(host, llm)
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert host.committed_msg_ids == ["inj-1"]


def _one_round_success_llm():
    class _LLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def __call__(self, **kwargs) -> LLMToolCallResult:
            self.call_count += 1
            return text_result("done")
    return _LLM()


@pytest.mark.asyncio
async def test_no_injection_when_queue_is_empty() -> None:
    """Tier 2: #3792 — a host whose peek legitimately returns ``None`` (no
    queued candidate) never calls commit and never appends anything to
    ``messages`` — the ordinary, overwhelmingly common case stays
    byte-identical to PR1."""

    class _EmptyInjectingHost(_InjectingHost):
        async def peek_mid_turn_injection(self) -> dict | None:
            self.call_order.append("peek_mid_turn_injection")
            return None

    host = _EmptyInjectingHost()
    seen_messages: list[list[dict]] = []

    class _RecordingLLM:
        async def __call__(self, **kwargs) -> LLMToolCallResult:
            seen_messages.append(list(kwargs["messages"]))
            return text_result("done")

    loop = _loop(host, _RecordingLLM())
    await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert seen_messages[0] == [{"role": "user", "content": "hi"}]
    assert host.committed_msg_ids == []


# ---------------------------------------------------------------------------
# Production-name witness — closes the gap the tests above CANNOT close.
#
# lead-coder's blocking review comment on PR1 (#3802) / restated for PR2:
# ``_InjectingHost`` above is a fake this SAME file defines; renaming
# ``RouterHostAdapter.peek_mid_turn_injection`` to anything else leaves every
# test above green (the seam's ``getattr(host, "peek_mid_turn_injection",
# None)`` would silently resolve to ``None`` against a real production host
# and go permanently inert, but the fake's own identically-named method still
# answers). A test that only exercises a fake with a name it chose to match
# is "a mirror", not a witness that PRODUCTION's name matches.
#
# This test reads router_loop.py's ACTUAL source (not a hardcoded copy of the
# string literal) to extract the two getattr(...) attribute names, and checks
# them against a REAL ``RouterHostAdapter`` (``tests/_support/router_host_adapter
# .make_adapter``, the same real-collaborator builder ``test_router_host_adapter
# _invariants.py`` uses) — so a rename on EITHER side (the adapter's method, or
# router_loop.py's getattr string) breaks this test.
# ---------------------------------------------------------------------------

import re

from tests._support.paths import REPO_ROOT
from tests._support.router_host_adapter import make_adapter

_ROUTER_LOOP_SRC = (
    REPO_ROOT / "src" / "reyn" / "runtime" / "router_loop.py"
).read_text()


def _extract_mid_turn_injection_attr_names() -> "dict[str, str]":
    """Regex-extract the attribute-name string literals router_loop.py's
    ``getattr(host, "...", None)`` calls actually use for the #3792 seam —
    reading the real source, not a copy of the literal, so a rename in
    router_loop.py alone (without touching this test) is caught too."""
    names = re.findall(
        r'getattr\(\s*(?:self\.)?host,\s*"(\w*mid_turn_injection\w*)"',
        _ROUTER_LOOP_SRC, re.DOTALL,
    )
    by_role = {n: n for n in names}
    assert "peek_mid_turn_injection" in by_role.values(), (
        "sanity: router_loop.py's own source must still name a "
        "peek_mid_turn_injection getattr — if this fails, the extraction "
        "regex itself broke, not the production code"
    )
    assert "commit_mid_turn_injection" in by_role.values()
    return by_role


@pytest.mark.asyncio
async def test_router_loop_peek_and_commit_names_resolve_on_the_real_adapter() -> None:
    """Tier 2: #3792 — the EXACT attribute-name strings ``RouterLoop.run_loop``
    reads via ``getattr`` really exist on a REAL ``RouterHostAdapter``
    (``make_adapter``, real collaborators — no ``FakeRouterHost``), and
    really forward to the callbacks the adapter was constructed with.

    Falsification (performed during review): renaming
    ``RouterHostAdapter.peek_mid_turn_injection`` (the PRODUCTION method, in
    ``router_host_adapter.py`` — not the test's own fake) to any other name
    makes this test go RED with ``AttributeError`` — while every OTHER test
    in this file (built on ``_InjectingHost``, this file's own fake) stays
    green, exactly the gap this test exists to close.
    """
    attr_names = _extract_mid_turn_injection_attr_names()
    peek_calls: list[None] = []
    commit_calls: list[str] = []

    async def _real_peek() -> "dict | None":
        peek_calls.append(None)
        return {"payload": {"text": "hi from real adapter"}, "msg_id": "real-1"}

    async def _real_commit(msg_id: str) -> None:
        commit_calls.append(msg_id)

    adapter = make_adapter(universal_wrappers_enabled=False,  # #4159: not exercised by this test
        peek_mid_turn_injection=_real_peek,
        commit_mid_turn_injection=_real_commit,
    )

    peek_fn = getattr(adapter, attr_names["peek_mid_turn_injection"], None)
    assert peek_fn is not None, (
        f"RouterHostAdapter has no attribute {attr_names['peek_mid_turn_injection']!r} "
        "— the exact name router_loop.py's getattr call reads"
    )
    result = await peek_fn()
    assert result == {"payload": {"text": "hi from real adapter"}, "msg_id": "real-1"}
    assert peek_calls == [None]

    commit_fn = getattr(adapter, attr_names["commit_mid_turn_injection"], None)
    assert commit_fn is not None, (
        f"RouterHostAdapter has no attribute {attr_names['commit_mid_turn_injection']!r} "
        "— the exact name router_loop.py's getattr call reads"
    )
    await commit_fn("real-1")
    assert commit_calls == ["real-1"]
