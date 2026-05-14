"""Tier 2 invariant tests for FP-0024 Component A — BM25 skill pre-filter.

Tests verify OS-level invariants:
1. Below threshold → full enum (no pre-filter).
2. Above threshold + keyword match → top_k only passed to build_tools.
3. BM25 returns 0 results → full enum fall-through (no skill invisibility).
4. BM25 dispatch emits ``skill_search_invoked`` event (P6 audit).

No unittest.mock.MagicMock / AsyncMock / patch. Real BM25Backend and
real RouterLoop with FakeRouterHost used throughout.
"""
from __future__ import annotations

from typing import Any

from reyn.chat.router_loop import RouterLoop
from reyn.config import SkillSearchConfig

# ---------------------------------------------------------------------------
# Minimal stubs (duplicated pattern from test_router_loop.py — no sharing
# across test modules to keep tests independent, per testing policy).
# ---------------------------------------------------------------------------


class _FakeEventLog:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data: Any) -> None:
        self.emitted.append({"type": type, **data})


class _FakeRouterHost:
    """Minimal RouterLoopHost for skill_search invariant tests."""

    chat_id: str = "test-chat"
    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(self, skills: list[dict]) -> None:
        self._skills = skills
        self.outbox: list[dict] = []
        self._events = _FakeEventLog()

    @property
    def events(self) -> _FakeEventLog:
        return self._events

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

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        return {"status": "ok", "skill": skill}

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None:
        pass

    async def file_read(self, path: str) -> str:
        raise FileNotFoundError(path)

    async def file_write(self, path: str, content: str) -> dict:
        return {"status": "ok"}

    async def file_delete(self, path: str) -> dict:
        return {"status": "ok"}

    async def file_list_directory(self, path: str) -> list[dict]:
        return []

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        return {"status": "ok"}

    async def mcp_list_servers(self) -> list[dict]:
        return []

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return []

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return {"status": "ok"}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"status": "ok", "content": ""}

    async def reyn_src_list(self, *, path: str) -> dict:
        return {"entries": []}

    async def reyn_src_read(self, *, path: str) -> dict:
        return {"content": ""}

    def memory_path(self, layer: str, slug: str) -> str:
        return f"/memory/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f"/memory/{layer}"

    def resolve_model(self, name: str) -> str:
        return f"fake-{name}"


# ---------------------------------------------------------------------------
# Helper — build N synthetic skills with distinct keywords
# ---------------------------------------------------------------------------

def _make_skills(n: int) -> list[dict]:
    """Return n skills whose names + descriptions are keyword-distinct."""
    return [
        {
            "name": f"skill_{i:03d}",
            "description": f"Handles task_{i:03d} operations for category_{i % 5}",
            "category": f"cat_{i % 5}",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Test 1 — below threshold: full enum reaches build_tools
# ---------------------------------------------------------------------------


def test_below_threshold_uses_full_enum() -> None:
    """Tier 2: below threshold → _apply_skill_search returns all skills unchanged."""
    # threshold=20; give 10 skills → pre-filter must NOT activate.
    skills = _make_skills(10)
    cfg = SkillSearchConfig(threshold=20, top_k=5)
    host = _FakeRouterHost(skills)
    loop = RouterLoop(host=host, chain_id="c1", skill_search_config=cfg)

    result = loop._apply_skill_search(skills, query="task_000 operations")

    assert len(result) == len(skills), (
        "below threshold: full skill list must pass through unchanged"
    )
    # No event should have been emitted (pre-filter did not run).
    event_types = [e["type"] for e in host.events.emitted]
    assert "skill_search_invoked" not in event_types, (
        "below threshold: no skill_search_invoked event expected"
    )


# ---------------------------------------------------------------------------
# Test 2 — above threshold: only top_k candidates reach build_tools
# ---------------------------------------------------------------------------


def test_above_threshold_uses_bm25_top_k() -> None:
    """Tier 2: above threshold + keyword match → only top_k skills returned."""
    # 25 skills; threshold=20 → BM25 activates. top_k=3 → at most 3 returned.
    skills = _make_skills(25)
    cfg = SkillSearchConfig(threshold=20, top_k=3)
    host = _FakeRouterHost(skills)
    loop = RouterLoop(host=host, chain_id="c1", skill_search_config=cfg)

    # Query keyword "task_000" should match skill_000 strongly.
    result = loop._apply_skill_search(skills, query="task_000 operations")

    assert 1 <= len(result) <= 3, (
        f"above threshold: expected 1–3 skills, got {len(result)}"
    )
    # skill_000 must be among the results (it has the exact keyword "task_000").
    result_names = {s["name"] for s in result}
    assert "skill_000" in result_names, (
        "keyword 'task_000' must rank skill_000 in BM25 results"
    )
    # All returned names must have been in the original catalogue.
    original_names = {s["name"] for s in skills}
    assert result_names <= original_names, (
        "BM25 results must be a subset of the original catalogue"
    )


# ---------------------------------------------------------------------------
# Test 3 — BM25 0 results → full enum fall-through
# ---------------------------------------------------------------------------


def test_bm25_zero_results_falls_through() -> None:
    """Tier 2: BM25 returns 0 matches → full catalogue passed to build_tools."""
    # 25 skills; threshold=20 → BM25 activates.
    # Query has no overlap with any skill name or description token.
    skills = _make_skills(25)
    cfg = SkillSearchConfig(threshold=20, top_k=5)
    host = _FakeRouterHost(skills)
    loop = RouterLoop(host=host, chain_id="c1", skill_search_config=cfg)

    # Token "xyzzy_nonexistent" is not in any skill's name or description.
    result = loop._apply_skill_search(skills, query="xyzzy_nonexistent")

    assert len(result) == len(skills), (
        "BM25 zero results must fall through to full catalogue "
        f"(got {len(result)}, expected {len(skills)})"
    )


# ---------------------------------------------------------------------------
# Test 4 — BM25 dispatch emits skill_search_invoked event (P6)
# ---------------------------------------------------------------------------


def test_skill_search_emits_invoked_event() -> None:
    """Tier 2: BM25 dispatch (above threshold, non-zero results) emits skill_search_invoked."""
    skills = _make_skills(25)
    cfg = SkillSearchConfig(threshold=20, top_k=5)
    host = _FakeRouterHost(skills)
    loop = RouterLoop(host=host, chain_id="c1", skill_search_config=cfg)

    loop._apply_skill_search(skills, query="task_001 operations")

    invoked_events = [e for e in host.events.emitted if e["type"] == "skill_search_invoked"]
    assert len(invoked_events) == 1, (
        f"expected exactly 1 skill_search_invoked event, got {len(invoked_events)}"
    )
    ev = invoked_events[0]
    # Verify mandatory fields (P6: enough info to reconstruct what happened).
    assert "query" in ev, "event must carry query"
    assert "candidates_count" in ev, "event must carry candidates_count"
    assert "top_k" in ev, "event must carry top_k"
    assert ev["top_k"] == 5
    assert ev["candidates_count"] >= 1, (
        "query 'task_001 operations' must match at least 1 skill"
    )
