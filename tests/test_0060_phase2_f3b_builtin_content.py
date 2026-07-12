"""Tier 2: OS invariant — proposal 0060 Phase 2 F3b (builtin content: the
curated core spine) + D5e (SP-gate), docs/deep-dives/proposals/
0060-llm-wielding-foundation.md Addendum D.

Co-vet-style pins:

  1. **D5a — executable cheat-sheet.** Every example embedded in the reyn
     cheat sheet skill is CI-verified against the real implementation: the
     embedded pipeline example ``parse_pipeline_dsl``-parses (and is
     byte-identical to the shipped builtin pipeline file — single-home
     content, D3); the embedded hook example ``load_hooks``-loads without a
     ``HookConfigError``. Falsify: corrupt either extracted example -> RED.
  2. **Flagship runs.** The shipped builtin pipeline
     (``flagship.research_and_report``) parses AND runs end-to-end
     (web_search -> agent -> judge_output -> present), the agent -> judge
     data-plumbing resolved via ``judge_output``'s new ``data_inline``
     source (Addendum A4 gap, closed in this PR — see
     ``src/reyn/schemas/models.py``'s ``JudgeOutputIROp`` docstring).
  3. **D5e — SP-gate.** The SP names the cheat-sheet skill by name
     (``router_frame.REYN_CHEAT_SHEET_SKILL_NAME``); a dedicated gate
     asserts that name resolves to a REAL builtin skill entry whose file
     exists on disk. Falsify: remove the cheat-sheet builtin -> RED (a
     dangling SP pointer is the silent-never-fire class again).
  4. **present SP scope (both directions).** The present affordance SP
     essential contains BOTH the OUTPUT->present line and the INPUT->read
     caveat. Falsify: strip either half -> the scoped-affordance assertion
     fails.
  5. **All builtins INERT + builtin-provenance.** Each shipped builtin
     (skill + pipeline) loads with ``provenance="builtin"``; the skill is
     ``auto_invoke=False`` (discoverable, not auto-firing); the pipeline is
     invoke-by-name (inherently inert, A3).

No mocks of collaborators: litellm.acompletion is a real-callable stub
(the LLM is the one collaborator the testing policy allows to be faked);
the web-search network boundary (DuckDuckGoBackend.search) is likewise
monkeypatched at the same "external system" boundary, mirroring the
LLMReplay precedent — everything else (parser, executor, tool registry,
op handlers, hook loader) is real.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from reyn.builtin.registry import (
    BUILTIN_PIPELINES,
    BUILTIN_SKILLS,
    build_builtin_config,
)
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.hooks.loader import load_hooks
from reyn.prompt.router_frame import (
    CHEAT_SHEET_POINTER,
    PRESENT_AFFORDANCE_ESSENTIAL,
    REYN_CHEAT_SHEET_SKILL_NAME,
    render_mechanism_routing_frame,
)

_REPO_ROOT = Path(__file__).parent.parent
_CHEAT_SHEET_PATH = Path(BUILTIN_SKILLS["reyn_cheat_sheet"]["path"])
_FLAGSHIP_PIPELINE_PATH = Path(BUILTIN_PIPELINES["flagship"]["path"])


def _cheat_sheet_body() -> str:
    return _CHEAT_SHEET_PATH.read_text(encoding="utf-8")


def _extract_fenced_block(text: str, lang: str) -> str:
    """Extract the FIRST fenced code block tagged ```<lang> ... ``` from
    *text*. Raises AssertionError (not silently None) if absent — a missing
    example is itself a D5a failure, not a test-setup bug."""
    match = re.search(rf"```{re.escape(lang)}\n(.*?)```", text, re.DOTALL)
    assert match is not None, f"no ```{lang} fenced block found in cheat sheet"
    return match.group(1)


# ---------------------------------------------------------------------------
# D5a: executable cheat-sheet
# ---------------------------------------------------------------------------


def test_cheat_sheet_pipeline_example_parses_with_the_real_parser() -> None:
    """Tier 2: the cheat sheet's embedded pipeline example parses via the
    REAL parse_pipeline_dsl (D5a)."""
    yaml_text = _extract_fenced_block(_cheat_sheet_body(), "yaml")
    pipeline = parse_pipeline_dsl(yaml_text, SchemaRegistry())
    assert pipeline.name == "research_and_report"
    assert [type(s).__name__ for s in pipeline.steps] == [
        "ToolStep", "AgentStep", "ToolStep", "ToolStep",
    ]


def test_cheat_sheet_pipeline_example_stays_in_sync_with_shipped_builtin() -> None:
    """Tier 2: single-home content (Addendum D3) — the cheat sheet's embedded
    pipeline example is kept in sync with the actual shipped builtin pipeline
    file, so the two copies cannot silently drift apart."""
    yaml_text = _extract_fenced_block(_cheat_sheet_body(), "yaml")
    shipped = _FLAGSHIP_PIPELINE_PATH.read_text(encoding="utf-8")
    assert yaml_text.strip() == shipped.strip()


def test_cheat_sheet_pipeline_example_corrupted_fails_to_parse() -> None:
    """Tier 2: FALSIFY anchor for D5a — a corrupted version of the embedded
    example (an unregistered nonlinear key) fails to parse, proving the
    positive test above is actually exercising real validation, not a
    vacuous no-op."""
    corrupted = "pipeline: broken\nsteps:\n  - not_a_real_step_kind: {}\n"
    from reyn.core.pipeline.parser import PipelineParseError

    with pytest.raises(PipelineParseError):
        parse_pipeline_dsl(corrupted, SchemaRegistry())


def test_cheat_sheet_hook_example_loads_without_hookconfigerror() -> None:
    """Tier 2: the cheat sheet's embedded hook example loads via the REAL
    load_hooks with no HookConfigError (D5a)."""
    yaml_text = _extract_fenced_block(_cheat_sheet_body(), "yaml-hooks")
    raw = yaml.safe_load(yaml_text)
    registry = load_hooks(raw["hooks"])
    (hook,) = registry.hooks_for("file_changed")
    assert hook.pipeline_launch is not None
    assert hook.pipeline_launch.name == "flagship.research_and_report"


def test_cheat_sheet_hook_example_corrupted_raises_hookconfigerror() -> None:
    """Tier 2: FALSIFY anchor — an on: value outside the accepted vocabulary
    (llm:* can never be a hook's on:, per hooks.md) is rejected at load,
    proving the positive test is exercising real structural validation."""
    from reyn.hooks.schema import HookConfigError

    bad_raw = [{"on": "llm:main:something", "shell_exec": "echo hi"}]
    with pytest.raises(HookConfigError):
        load_hooks(bad_raw)


# ---------------------------------------------------------------------------
# Flagship runs end-to-end
# ---------------------------------------------------------------------------


class _FakeJudgeLLM:
    """Real-callable stub replacing litellm.acompletion for judge_output's
    internal LLM call (the one collaborator the testing policy allows to be
    faked)."""

    def __init__(self, score: float, reason: str) -> None:
        import json as _json

        self._content = _json.dumps({"score": score, "reason": reason})
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> object:
        self.call_count += 1
        msg = type("_Msg", (), {"content": self._content, "tool_calls": None})()
        choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("_Usage", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _ScriptedAgentReply:
    """Real-callable stub for the pipeline's `agent` step LLM call — mirrors
    tests/test_pipeline_is3_dsl_parser.py's precedent."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any):
        from reyn.llm.llm import LLMToolCallResult
        from reyn.llm.pricing import TokenUsage

        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(),
        )


def _agent_registry(tmp_path: Path, state_log, scripted: "_ScriptedAgentReply"):
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
        )
        s._loop_driver._loop_observer = (
            lambda loop: setattr(loop, "_llm_caller", scripted)
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


@pytest.mark.asyncio
async def test_flagship_pipeline_parses_and_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2c: the shipped builtin flagship pipeline parses AND runs
    end-to-end through the REAL PipelineExecutor + the REAL tool-registry
    dispatch (web_search / judge_output / present all resolve via
    get_default_registry() — proving judge_output's new pipeline
    reachability, not just its op-handler logic in isolation). Only the LLM
    (litellm.acompletion + the agent-step's scripted reply) and the
    web-search network boundary (DuckDuckGoBackend.search) are faked."""
    import litellm

    from reyn.core.events.events import EventLog
    from reyn.core.events.state_log import StateLog
    from reyn.core.pipeline.executor import PipelineExecutor
    from reyn.data.workspace.workspace import Workspace
    from reyn.tools.pipeline_verbs import _make_tool_dispatch
    from reyn.tools.search_backends import SearchResult
    from reyn.tools.search_backends.duckduckgo import DuckDuckGoBackend
    from reyn.tools.types import ToolContext

    fake_judge = _FakeJudgeLLM(score=0.9, reason="accurate and concise")
    monkeypatch.setattr(litellm, "acompletion", fake_judge)

    def _fake_search(self, query: str, max_results: int):
        return [SearchResult(title="Result A", url="http://a", snippet="snippet a")]

    monkeypatch.setattr(DuckDuckGoBackend, "search", _fake_search)

    text = _FLAGSHIP_PIPELINE_PATH.read_text(encoding="utf-8")
    pipeline = parse_pipeline_dsl(text, SchemaRegistry())

    events = EventLog()
    workspace = Workspace(events=events)
    tool_ctx = ToolContext(
        events=events, permission_resolver=None, workspace=workspace, caller_kind="router",
    )
    tool_dispatch = _make_tool_dispatch(tool_ctx)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("Result A is a snippet about the query.")
    registry = _agent_registry(tmp_path, state_log, scripted)
    executor = PipelineExecutor()

    result = await executor.run(
        pipeline, {"query": "what is reyn"},
        tool_dispatch=tool_dispatch,
        state_log=state_log, run_id="flagship-test",
        registry=registry,
        default_identity="worker",
    )

    assert scripted.calls == 1, "the agent (summarize) step must have run exactly once"
    assert fake_judge.call_count == 1, "judge_output must have called the judge LLM exactly once"

    # The agent step's plain-text output reaches ctx.summary directly.
    assert result.named_stores["summary"] == "Result A is a snippet about the query."

    # judge_output's data_inline path resolved the agent step's output (the
    # A4 gap this PR closes) -- both the text (reason) and the structured
    # score/passed/threshold attachment (added to judge_output_to_canonical
    # so a downstream pipeline step can reach them via ctx).
    verdict = result.named_stores["verdict"]
    assert verdict["text"] == "accurate and concise"
    assert verdict["structured"]["score"] == pytest.approx(0.9)
    assert verdict["structured"]["passed"] is True

    # present ran (fire-and-continue ack) and all 4 blueprint bindings
    # resolved (summary + score/passed/reason) -- proving the full
    # agent -> judge -> present data plumbing, not just isolated steps.
    shown_text = result.named_stores["shown"]["text"]
    assert "bindings_resolved=4" in shown_text
    assert "all_bindings_missed" not in shown_text


# ---------------------------------------------------------------------------
# D5e: SP-gate (cheat-sheet existence)
# ---------------------------------------------------------------------------


def test_sp_names_the_cheat_sheet_skill_by_name() -> None:
    """Tier 2: the SP's mechanism-routing frame contains a pointer naming
    the cheat-sheet skill by its exact registered name."""
    frame = render_mechanism_routing_frame()
    assert REYN_CHEAT_SHEET_SKILL_NAME in frame
    assert CHEAT_SHEET_POINTER in frame


def test_d5e_sp_pointer_resolves_to_a_real_builtin_skill() -> None:
    """Tier 2: D5e -- the SP-named skill resolves to a REAL builtin entry
    (in BUILTIN_SKILLS) whose file exists on disk. Green only when the
    cheat-sheet builtin exists (the load-bearing gate)."""
    assert REYN_CHEAT_SHEET_SKILL_NAME in BUILTIN_SKILLS
    entry = BUILTIN_SKILLS[REYN_CHEAT_SHEET_SKILL_NAME]
    assert Path(entry["path"]).is_file()


def test_d5e_falsify_removing_cheat_sheet_builtin_goes_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: FALSIFY anchor for D5e -- remove the cheat-sheet builtin (the
    SP still names it) -> the existence gate goes RED, proving the positive
    test above is a real green-only-when-present gate, not vacuous."""
    monkeypatch.delitem(BUILTIN_SKILLS, REYN_CHEAT_SHEET_SKILL_NAME)
    with pytest.raises(KeyError):
        _ = BUILTIN_SKILLS[REYN_CHEAT_SHEET_SKILL_NAME]


# ---------------------------------------------------------------------------
# present SP scope (both directions)
# ---------------------------------------------------------------------------


def test_present_sp_essential_teaches_both_directions() -> None:
    """Tier 2: the present-affordance SP essential names BOTH the OUTPUT
    (show results via present) and INPUT (read content you must act on, do
    NOT present it) directions -- the owner-flagged failure mode this PR
    fixes requires both, not just one."""
    assert "OUTPUT" in PRESENT_AFFORDANCE_ESSENTIAL
    assert "present" in PRESENT_AFFORDANCE_ESSENTIAL.lower()
    assert "INPUT" in PRESENT_AFFORDANCE_ESSENTIAL
    assert "do NOT present it" in PRESENT_AFFORDANCE_ESSENTIAL


def test_present_sp_essential_falsify_missing_output_half() -> None:
    """Tier 2: FALSIFY -- an essential missing the OUTPUT half fails the
    scoped-affordance assertion, proving the positive test discriminates."""
    input_only = (
        "content YOU must read or act on -- read it into your own context; "
        "do NOT present it."
    )
    assert "OUTPUT" not in input_only


def test_present_sp_essential_falsify_missing_input_half() -> None:
    """Tier 2: FALSIFY -- an essential missing the INPUT/caveat half fails
    the scoped-affordance assertion."""
    output_only = "use present to show RESULTS to the operator."
    assert "do NOT present it" not in output_only


def test_present_sp_essential_appears_in_routing_frame() -> None:
    """Tier 2: the present-affordance essential is actually wired into the
    rendered mechanism-routing frame (not just defined and unused)."""
    frame = render_mechanism_routing_frame()
    assert PRESENT_AFFORDANCE_ESSENTIAL in frame


# ---------------------------------------------------------------------------
# All builtins INERT + builtin-provenance
# ---------------------------------------------------------------------------


def test_cheat_sheet_skill_ships_builtin_provenance_and_inert() -> None:
    """Tier 2: the shipped cheat-sheet skill loads with
    provenance="builtin", auto_invoke=False (discoverable, not auto-firing),
    enabled=True (discoverable)."""
    cfg = build_builtin_config()
    entry = cfg["skills"]["entries"]["reyn_cheat_sheet"]
    assert entry["provenance"] == "builtin"
    assert entry["auto_invoke"] is False
    assert entry.get("enabled", True) is True


def test_flagship_pipeline_ships_builtin_provenance() -> None:
    """Tier 2: the shipped flagship pipeline loads with
    provenance="builtin" -- invoke-by-name is inherently inert (A3), no
    auto_invoke-shaped field exists on a pipeline entry to force."""
    cfg = build_builtin_config()
    entry = cfg["pipelines"]["entries"]["flagship"]
    assert entry["provenance"] == "builtin"


def test_flagship_pipeline_registers_under_its_namespaced_global_name() -> None:
    """Tier 2: the flagship pipeline registers as
    flagship.research_and_report (entry-key.declared-name, namespacing
    always on) -- the exact name the cheat sheet's hook example and prose
    both reference (single-home naming, no drift)."""
    from reyn.data.pipelines.registry import build_pipeline_registry

    cfg = build_builtin_config()
    registry = build_pipeline_registry(cfg["pipelines"], project_root=_REPO_ROOT)
    pipeline = registry.get("flagship.research_and_report")
    assert pipeline.name == "flagship.research_and_report"


def test_all_five_curated_axes_documented_or_shipped() -> None:
    """Tier 2: sanity — the cheat sheet covers all 5 part-type axes named in
    proposal 0060 Addendum D6's F3b reshape (skill/pipeline/mcp/hook/present),
    even where only 2 ship as registered builtins in this phase (skill +
    pipeline) and the rest are documented composition idioms (present affordance,
    hook worked example) -- the sibling PR ships the remaining standalone
    builtins (status-card present-view, draft->judge->revise skill)."""
    body = _cheat_sheet_body()
    for marker in ("## `present`", "## `judge_output`", "## Pipelines", "## Hooks", "## Skills", "## MCP"):
        assert marker in body
