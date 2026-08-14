"""Tier 2: tool-use scheme abstraction (#1593 PR-1).

PR-1 moves universal-category behind the ``ToolUseScheme`` protocol with **zero
behaviour change** — the byte-identical proof is the *existing* tool-use / LLMReplay
suites passing unchanged (incl. the exclude regressions
``test_chat_exclude_tools_187`` / ``test_exclude_execution_block_1406`` /
``test_run_once_187``). These tests pin the new abstraction surface itself: the
registry, the protocol conformance, the per-layer config, the delegation seam, and
the Execute-only invariant of universal-category. Real types, no mocks (a recording
Fake ``SchemeOps`` exercises the delegation).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.config import ToolUseConfig, _build_tool_use_config
from reyn.tools.scheme import (
    DEFAULT_SCHEME_NAME,
    AdvertisedTools,
    ExecContext,
    Execute,
    ExecutionResult,
    Presentation,
    ToolUseScheme,
    advertised_entries,
    get_scheme,
    register_scheme,
    registered_scheme_names,
)
from reyn.tools.schemes.universal_category import UniversalCategoryScheme
from tests._support.tool_use_negative_examples import NOT_A_PRESENTATION


class _RecordingOps:
    """A recording Fake ``SchemeOps`` — lets us exercise the delegating scheme
    without a full router. Real callables, no mock framework."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def present(self, available, layer_ctx) -> Presentation:
        self.calls.append("present")
        return Presentation(tools_channel=AdvertisedTools(entries=[{"t": 1}]))

    def resolve(self, llm_response, tool_catalog: dict) -> list[dict]:
        self.calls.append("resolve")
        tcs = getattr(llm_response, "tool_calls", None) or []
        return [{"tc": tc, "name": tc["name"], "args": {}} for tc in tcs]

    async def dispatch(
        self, actions: list[dict], *, call_id: "str | None" = None,
    ) -> list[dict]:
        self.calls.append("dispatch")
        return [{"status": "ok", "for": a["name"]} for a in actions]

    def feedback(self, result) -> list[dict]:
        # #1608: ops.feedback now receives the enriched ExecutionResult and returns
        # appendable MESSAGES (the relocated assistant+tool-message build). The Fake
        # records the delegated result + returns a representative message sequence.
        self.calls.append("feedback")
        self.last_feedback_result = result
        return [
            {"role": "assistant", "content": result.assistant_content,
             "tool_calls": result.tool_calls},
            *(
                {"role": "tool", "tool_call_id": tc.get("id"), "content": str(r)}
                for tc, r in zip(result.tool_calls, result.tool_results)
            ),
        ]


# ── registry ────────────────────────────────────────────────────────────────


def test_registry_register_get_resolve() -> None:
    """Tier 2: register a scheme by name, look it up, and the default name resolves."""
    register_scheme(UniversalCategoryScheme())
    assert DEFAULT_SCHEME_NAME == "enumerate-all"
    s = get_scheme(DEFAULT_SCHEME_NAME)
    assert s is not None and s.name == "enumerate-all"  # #1657
    assert DEFAULT_SCHEME_NAME in registered_scheme_names()
    assert get_scheme("no-such-scheme") is None


def test_universal_conforms_to_protocol() -> None:
    """Tier 2: UniversalCategoryScheme satisfies the ToolUseScheme protocol."""
    assert isinstance(UniversalCategoryScheme(), ToolUseScheme)


# ── delegation seam + Execute-only invariant ─────────────────────────────────


@pytest.mark.asyncio
async def test_universal_build_presentation_delegates() -> None:
    """Tier 2: build_presentation delegates to ops.present (the router's logic).
    Async seam (#1593 PR-2) but universal's body stays a sync delegation — the
    awaited result equals the unchanged ops.present output (byte-identical)."""
    ops = _RecordingOps()
    pres = await UniversalCategoryScheme().build_presentation({}, {}, ops)
    assert "present" in ops.calls
    assert advertised_entries(pres.tools_channel) == [{"t": 1}]


def test_universal_interpret_execute_with_tool_calls() -> None:
    """Tier 2: with tool calls, universal yields Execute carrying the ops-resolved
    actions — the OS exclude-gates these pre-dispatch. (#1593 loop-unify: the
    no-tool-call → PlainText case is pinned in test_scheme_interpretation_match_1593.)"""
    ops = _RecordingOps()
    resp = SimpleNamespace(content="", tool_calls=[{"name": "a"}, {"name": "b"}])
    interp = UniversalCategoryScheme().interpret(resp, tool_catalog={}, ops=ops)
    assert isinstance(interp, Execute)
    assert [x["name"] for x in interp.actions] == ["a", "b"]
    assert "resolve" in ops.calls


@pytest.mark.asyncio
async def test_universal_execute_and_feedback_round_trip() -> None:
    """Tier 2: execute delegates dispatch; format_feedback delegates to ops.feedback
    with the ENRICHED result and returns appendable MESSAGES (#1608 unified contract,
    not the former tool_results passthrough)."""
    ops = _RecordingOps()
    scheme = UniversalCategoryScheme()
    res = await scheme.execute(Execute(actions=[{"name": "a"}]), ExecContext(), ops)
    assert res.tool_results == [{"status": "ok", "for": "a"}]
    enriched = ExecutionResult(
        tool_results=res.tool_results, tool_calls=[{"id": "c1"}], assistant_content="hi",
    )
    fb = scheme.format_feedback(enriched, ops)
    # the full enriched result is delegated (not just tool_results) ...
    assert ops.last_feedback_result is enriched
    # ... and the return is appendable messages: assistant turn + one tool message.
    assert fb[0]["role"] == "assistant" and fb[0]["tool_calls"] == [{"id": "c1"}]
    assert fb[1]["role"] == "tool" and fb[1]["tool_call_id"] == "c1"
    assert ops.calls.count("dispatch") == 1 and ops.calls.count("feedback") == 1


# ── per-layer config ─────────────────────────────────────────────────────────


def test_tool_use_config_chat_scheme_and_defaults() -> None:
    """Tier 2: chat-layer scheme x transport selection parses + defaults to
    enumerate-all / tool_calls; a non-string value is a loud error. (#2768
    removed the dead step/phase layers; FP-0066 P4b split ``chat`` into
    ``scheme`` x ``transport``.)"""
    assert _build_tool_use_config(None) == ToolUseConfig()
    assert ToolUseConfig().scheme == "enumerate-all"
    assert ToolUseConfig().transport == "tool_calls"
    # "category" is the presentation-axis name (P4a firm §1 census); the
    # concrete registered scheme it resolves to is "universal-category".
    cfg = _build_tool_use_config({"scheme": "category"})
    assert cfg.scheme == "category"
    assert cfg.transport == "tool_calls"
    with pytest.raises(ValueError):
        _build_tool_use_config({"scheme": 123})


def test_tool_use_config_nondefault_roundtrip() -> None:
    """Tier 2: FP-0066 P4b — a non-default (scheme, transport) pair round-trips
    through parsing. Defaults-only round-trip proves nothing about the 2-key
    surface actually wiring both fields independently."""
    cfg = _build_tool_use_config(
        {"scheme": "category", "transport": "tool_calls"}
    )
    assert cfg.scheme == "category"
    assert cfg.transport == "tool_calls"


def test_tool_use_config_old_chat_key_fails_loud() -> None:
    """Tier 2: FP-0066 P4b ★ J2 — a reyn.yaml ``tool_use:`` block still
    carrying the removed ``chat`` key must raise a legible error naming the
    2-key migration, NOT silently ignore it (a silently-dropped old key is a
    "config that doesn't take effect" trap — clean-break means detect+error,
    not remove+ignore)."""
    with pytest.raises(ValueError) as excinfo:
        _build_tool_use_config({"chat": "enumerate-all"})
    message = str(excinfo.value)
    assert "tool_use.chat" in message
    assert "tool_use.scheme" in message
    assert "tool_use.transport" in message


def test_tool_use_config_invalid_pair_fails_loud_at_parse_time() -> None:
    """Tier 2: FP-0066 P4b — an unregistered (scheme, transport) cell (P4a's
    valid-pair registry) raises at CONFIG PARSE time, not deep in a running
    session.

    ★ The witness is off-axis, and it took two expiries to get here. This arm
    named ``category`` x ``content_fence`` until #3376 P2 implemented that cell,
    then ``retrieval`` x ``content_fence`` until P3 implemented that one — both
    times the arm stopped testing fail-closedness and started failing for the
    opposite reason. Neither pair was ever forbidden; each was a legal
    combination that had not arrived yet, in an arc whose purpose was to make
    them arrive. ``NOT_A_PRESENTATION`` is not a name on the presentation axis at
    all, so it cannot be registered by any future cell.

    Paired with the arm below, which is the same axis from the other side."""
    with pytest.raises(ValueError):
        _build_tool_use_config(
            {"scheme": NOT_A_PRESENTATION, "transport": "content_fence"}
        )


@pytest.mark.parametrize("scheme", ["category", "retrieval"])
def test_tool_use_config_accepts_the_content_fence_cells(scheme: str) -> None:
    """Tier 2: #3376 P2/P3 — the config surface actually admits the new cells.

    Registering a cell in ``_VALID_SCHEME_TRANSPORT_PAIRS`` and having an
    operator's ``reyn.yaml`` accept it are two facts: parse-time validation reads
    that registry, so this is the arm that says the documented yaml works. Paired
    with the refusal above, which is the same axis from the other side."""
    cfg = _build_tool_use_config({"scheme": scheme, "transport": "content_fence"})
    assert (cfg.scheme, cfg.transport) == (scheme, "content_fence")


def test_chat_default_matches_runtime_fallback_default() -> None:
    """Tier 1: the single enforced source of truth for the tool-use default (#2768).

    Two literals declare the default and are kept in sync by convention only: the
    config-schema default (``ToolUseConfig().scheme``) and the runtime fallback
    (``scheme.DEFAULT_SCHEME_NAME``). ``config/execution.py`` is a declarative
    dataclass, not a runtime source, so this contract test is the binding that
    prevents the two from silently drifting — edit one literal without the other
    and this fails."""
    assert ToolUseConfig().scheme == DEFAULT_SCHEME_NAME
