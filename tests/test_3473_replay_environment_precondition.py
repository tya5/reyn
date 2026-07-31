"""Tier 1/2: a replay key is the SCENARIO; the environment is a CHECKED precondition (#3473).

#3473 face 3 was a `MissingFixture` on turn 1 of the fp0063 arc that
reproduced only when other tests shared the machine. The mechanism is
generic, and it is the reason this file exists rather than a one-off guard:
an environment-derived value (the MCP tool catalog, whose probe has a
deadline) was hashed INTO the replay key, so an environment wobble was
indistinguishable from a different conversation and the report said only
"No fixture entry for model=…".

The pins below are the two halves the fix must keep:

  - The key is invariant to the environment (so a wobble cannot masquerade
    as a different scenario) but still sensitive to the scenario — and
    byte-stable for payloads carrying no environment imprint, so committed
    fixtures recorded before #3473 keep their keys.
  - Nothing left the key silently: the imprint is recorded and compared, a
    difference is reported NAMING the server and the tools that differ, and
    an entry with no recorded imprint is refused rather than served under an
    environment it was never captured against.

The last test is the "deterministic without waiting" pin: #3473 rules out
sleeps, longer deadlines and retries, so the recorded catalog is injected
directly and the probe is off the replay path entirely — witnessed with a
probe that never answers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.dev.testing.replay import LLMReplay, MissingFixture, PreconditionMismatch
from reyn.dev.testing.replay_preconditions import (
    MCPCatalogPrecondition,
    ReplayRequest,
    default_preconditions,
)

_MODEL = "gemini/gemini-2.5-flash-lite"
_MESSAGES = [{"role": "user", "content": "Install the rag plugin."}]


def _mcp_tool_schema(servers: list[str], qualified_tools: list[str]) -> dict:
    """A `call_mcp_tool` entry shaped as `_enrich_router_schema` leaves it.

    `server` / `mcp_tool_name` carry an `enum` only when the adapter has an
    ANSWER for the server; a probe that did not answer leaves the property
    enum-less, which is exactly what an empty ``qualified_tools`` produces here.
    """
    server_prop: dict = {"type": "string", "description": "server"}
    tool_prop: dict = {"type": "string", "description": "tool"}
    if servers:
        server_prop["enum"] = list(servers)
    if qualified_tools:
        tool_prop["enum"] = list(qualified_tools)
    return {
        "type": "function",
        "function": {
            "name": "call_mcp_tool",
            "description": "Call an MCP tool",
            "parameters": {
                "type": "object",
                "properties": {"server": server_prop, "mcp_tool_name": tool_prop},
            },
        },
    }


_TOOLS_FULL_CATALOG = [_mcp_tool_schema(["reyn_markitdown"], ["reyn_markitdown.convert_to_markdown"])]
_TOOLS_UNANSWERED = [_mcp_tool_schema(["reyn_markitdown"], [])]
_TOOLS_NO_MCP = [
    {
        "type": "function",
        "function": {"name": "list_skills", "description": "List skills", "parameters": {}},
    },
]


# ── The key is the scenario ──────────────────────────────────────────────────


def test_key_is_invariant_to_the_mcp_catalog_but_not_to_the_scenario() -> None:
    """Tier 1: the same conversation keys identically under two catalogs.

    The catalog is what the MACHINE offered, not what the conversation was.
    The second half of the assertion is what keeps this from being satisfied
    by a key that ignores ``tools`` altogether: a genuinely different tool
    payload must still key differently.
    """
    answered = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_FULL_CATALOG)
    unanswered = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_UNANSWERED)
    assert answered == unanswered, (
        "an MCP probe that did not answer changed the replay key — the "
        "environment is still a key component"
    )

    scenario_change = LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_NO_MCP)
    assert scenario_change != answered, (
        "a different tools= payload must still produce a different key — the "
        "scrub must remove the environment imprint, not the whole payload"
    )


def test_key_is_unchanged_for_a_payload_with_no_environment_imprint() -> None:
    """Tier 1: scrubbing is a no-op where the environment left no imprint.

    Committed fixtures recorded before #3473 on a machine with no MCP servers
    must keep matching, so the scrub may not perturb a payload it has nothing
    to remove from. Compared against the key computed with preconditions
    explicitly disabled — the pre-#3473 computation.
    """
    assert LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_NO_MCP) == LLMReplay.key(
        _MODEL, _MESSAGES, tools=_TOOLS_NO_MCP, preconditions=(),
    )
    assert LLMReplay.key(_MODEL, _MESSAGES) == LLMReplay.key(
        _MODEL, _MESSAGES, preconditions=(),
    )


def test_scrub_does_not_mutate_the_caller_s_tools() -> None:
    """Tier 1: the payload handed to the real provider must survive key computation.

    ``key`` runs on the SAME list the caller is about to send; mutating it in
    place would strip the enums the model is actually meant to see.
    """
    before = json.dumps(_TOOLS_FULL_CATALOG, sort_keys=True)
    LLMReplay.key(_MODEL, _MESSAGES, tools=_TOOLS_FULL_CATALOG)
    assert json.dumps(_TOOLS_FULL_CATALOG, sort_keys=True) == before


# ── The environment is checked, and the mismatch speaks ──────────────────────


def _write_fixture(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8",
    )


def _recorded_entry(tools: list[dict], *, with_preconditions: bool = True) -> dict:
    """A completion entry as record mode writes it for ``tools``."""
    precondition = MCPCatalogPrecondition()
    request = ReplayRequest(model=_MODEL, messages=_MESSAGES, tools=tools)
    entry = {
        "key": LLMReplay.key(_MODEL, _MESSAGES, tools=tools),
        "kind": "completion",
        "model": _MODEL,
        "prompt_preview": "Install the rag plugin.",
        "response": {
            "id": "rec-1",
            "created": 0,
            "model": _MODEL,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }],
        },
    }
    if with_preconditions:
        entry["preconditions"] = {precondition.name: precondition.observe(request)}
    return entry


@pytest.mark.asyncio
async def test_a_different_catalog_fails_naming_the_server_and_its_missing_tools(
    tmp_path: Path,
) -> None:
    """Tier 1: the report NAMES the difference instead of saying "no fixture entry".

    This is the whole diagnostic half of #3473: three sessions attributed this
    failure exactly once because the message described the LOOKUP, never the
    environment. The assertions are on what a reader can learn from the text —
    the server, the tools it was captured with, and the fact that the
    conversation itself matched.
    """
    fixture = tmp_path / "f.jsonl"
    _write_fixture(fixture, [_recorded_entry(_TOOLS_FULL_CATALOG)])
    replay = LLMReplay(fixture, mode="replay")

    with pytest.raises(PreconditionMismatch) as excinfo:
        await replay._handle(_MODEL, _MESSAGES, tools=_TOOLS_UNANSWERED)

    message = str(excinfo.value)
    assert "mcp_catalog" in message
    assert "reyn_markitdown" in message, "the report must name the server that differed"
    assert "convert_to_markdown" in message, (
        "the report must name the tools the fixture was captured with"
    )
    assert "No fixture entry" not in message, (
        "a matched conversation under a different environment must NOT be "
        "reported as a missing recording — that conflation is the defect"
    )


@pytest.mark.asyncio
async def test_the_matching_catalog_replays(tmp_path: Path) -> None:
    """Tier 1: a run whose environment matches the capture is served normally.

    The non-vacuity companion to the test above: the check must reject a
    DIFFERENT environment, not every environment.
    """
    fixture = tmp_path / "f.jsonl"
    _write_fixture(fixture, [_recorded_entry(_TOOLS_FULL_CATALOG)])
    replay = LLMReplay(fixture, mode="replay")

    response = await replay._handle(_MODEL, _MESSAGES, tools=_TOOLS_FULL_CATALOG)
    assert response.choices[0].message.content == "ok"


@pytest.mark.asyncio
async def test_an_entry_with_no_recorded_imprint_is_refused_under_a_live_environment(
    tmp_path: Path,
) -> None:
    """Tier 1: taking the environment out of the key may not silently widen matching.

    A pre-#3473 entry records no imprint, so nothing can be checked against it.
    Serving it under a non-empty environment is the rejected "drop the catalog
    from the key" design wearing a different hat — it would replay a response
    recorded against different tooling. It stays servable where the imprint is
    empty, because there the key is byte-identical to the one it was recorded
    under.
    """
    fixture = tmp_path / "legacy.jsonl"
    _write_fixture(
        fixture, [_recorded_entry(_TOOLS_NO_MCP, with_preconditions=False)],
    )
    replay = LLMReplay(fixture, mode="replay")

    response = await replay._handle(_MODEL, _MESSAGES, tools=_TOOLS_NO_MCP)
    assert response.choices[0].message.content == "ok"

    legacy_with_catalog = _recorded_entry(_TOOLS_FULL_CATALOG, with_preconditions=False)
    _write_fixture(fixture, [legacy_with_catalog])
    replay = LLMReplay(fixture, mode="replay")
    with pytest.raises(MissingFixture) as excinfo:
        await replay._handle(_MODEL, _MESSAGES, tools=_TOOLS_FULL_CATALOG)
    assert "mcp_catalog" in str(excinfo.value)


# ── Determinism without waiting ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_recorded_catalog_is_injected_so_a_probe_that_never_answers_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: injection puts the captured catalog on the real warm-start path.

    The load-sensitive failure is a probe missing its deadline. #3473 rules
    out every fix that merely widens the window (sleep / longer timeout /
    retry), so the recorded snapshot is written into the persistent cache file
    `ensure_mcp_tools_cached` warm-starts from BEFORE it probes anything — the
    same file `reyn mcp refresh` writes, through the same production writer.

    The witness is a probe that raises every time it is called: if injection
    were absent or reached a file the adapter does not read, the adapter would
    fall through to that probe and the router-visible catalog would carry no
    tools at all. Constructing a real unreachable MCP server would witness the
    same thing more slowly and less certainly; this is the same real-async-
    callable probe seam `test_mcp_cache_warm_start.py` already drives.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_mcp_cache_warm_start import _CountingProbe, _make_adapter  # noqa: E402

    state_dir = tmp_path / "state"
    snapshot = {"reyn_markitdown": [{"name": "convert_to_markdown", "description": "d"}]}

    fixture = tmp_path / "f.jsonl"
    _write_fixture(
        fixture,
        [
            {"kind": "environment", "name": "mcp_catalog", "value": snapshot},
            _recorded_entry(_TOOLS_FULL_CATALOG),
        ],
    )
    replay = LLMReplay(
        fixture, mode="replay", preconditions=(MCPCatalogPrecondition(state_dir),),
    )
    replay.install()
    try:
        pass
    finally:
        replay.restore()

    class _NeverAnswers(_CountingProbe):
        async def __call__(self, server_name: str) -> list[dict]:
            self.calls.append(server_name)
            raise TimeoutError(f"probe for {server_name} did not answer")

    probe = _NeverAnswers()
    adapter = _make_adapter(
        tmp_path=tmp_path,
        mcp_servers={"servers": {"reyn_markitdown": {"type": "stdio"}}},
        probe=probe,
        state_dir=state_dir,
    )
    await adapter.ensure_mcp_tools_cached()

    catalog = {
        server["name"]: [t["name"] for t in server.get("tools", [])]
        for server in adapter._get_mcp_servers_for_router()
    }
    assert catalog == {"reyn_markitdown": ["convert_to_markdown"]}, (
        "the injected catalog did not reach the router payload — the adapter "
        f"fell through to the probe (calls: {probe.calls})"
    )


def test_capture_reads_back_what_inject_wrote(tmp_path: Path) -> None:
    """Tier 1: capture/inject round-trip through the production cache file.

    Record mode captures with the same pair replay mode injects with, so a
    snapshot that cannot be read back is a fixture that silently records
    nothing. Uses a NON-DEFAULT value (two servers, one of them with two
    tools) so a round-trip that quietly returns a default would fail.
    """
    precondition = MCPCatalogPrecondition(tmp_path / "state")
    assert precondition.capture() is None, "no cache file yet — nothing to capture"

    snapshot = {
        "reyn_chunker": [{"name": "chunk", "description": "c"}],
        "reyn_vector_store": [
            {"name": "upsert", "description": "u"}, {"name": "query", "description": "q"},
        ],
    }
    precondition.inject(snapshot)
    assert precondition.capture() == snapshot


def test_default_preconditions_are_not_shared_between_replays() -> None:
    """Tier 1: each LLMReplay gets its own precondition instances.

    A precondition carries resolution state (a state dir); a module-level
    singleton would let one test's injection target leak into another's.
    """
    first, second = default_preconditions(), default_preconditions()
    assert [p.name for p in first] == [p.name for p in second]
    assert all(a is not b for a, b in zip(first, second, strict=True))
