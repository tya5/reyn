"""Tier 1/2: #2686/#2708 — ``pipeline_uses_llm`` classification + LLM-less
``reyn pipe run`` never rejected for missing creds.

Since #2708 P3.2b the missing-cred pre-check moved OFF ``reyn pipe run``'s
per-surface startup gate and ONTO the single LLM funnel (``recorded_acompletion``,
tested in ``test_2708_cred_check_chokepoint.py``). Two things survive here:

- ``pipeline_uses_llm`` remains an OPTIONAL early-UX predicate (no longer
  load-bearing for correctness). Its Tier-1 classification contract still holds:
  every ``_STEP_KINDS`` kind is classified into exactly one LLM bucket, and each
  container kind (fold/for_each/parallel/call/match) reaching an ``AgentStep``
  ONLY through it → predicate True; transform/tool/shell-only → False.
- The #2686 false-positive-zero property is now STRUCTURAL: an LLM-less
  (transform/tool-only) pipeline never reaches the funnel, so it can never be
  rejected for a missing provider key. ``run_run`` on a transform-only pipeline
  runs to completion even with every provider key unset (the reverted #2685
  regression, pinned dead — now by construction, not a hand-maintained guard).
  A "not registered" name still surfaces its own resolution error.

Real dataclasses / real ``PipelineRegistry`` / real ``load_config`` / real
``run_run`` — no mocks.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest
import yaml

from reyn.core.pipeline.executor import (
    _CONTAINER_CHILDREN,
    _LLM_LEAF_KINDS,
    _NON_LLM_LEAF_KINDS,
    _STEP_KINDS,
    AgentStep,
    CallStep,
    FoldStep,
    ForEachStep,
    MatchCase,
    MatchStep,
    ParallelStep,
    Pipeline,
    ToolStep,
    TransformStep,
    pipeline_uses_llm,
)
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.interfaces.cli.commands.pipe import register, run_run

_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY")


@pytest.fixture(autouse=True)
def _restore_litellm_api_base():
    """Snapshot + restore LITELLM_API_BASE around every test in this file.

    ``load_config()`` legitimately EXPORTS LITELLM_API_BASE process-wide when a
    config carries ``api_base`` (#2683's single-writer chokepoint) — and the
    ``api_base``-proxy fixtures here drive exactly that path. Without this owning
    the real restore, the export would leak into the rest of the suite and
    perturb litellm-routing tests (same guard as
    ``test_pipe_proxy_bootstrap_2682.py``)."""
    saved = os.environ.get("LITELLM_API_BASE")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("LITELLM_API_BASE", None)
        else:
            os.environ["LITELLM_API_BASE"] = saved


# ---------------------------------------------------------------------------
# Tier 1 — completeness gate (classification coverage)
# ---------------------------------------------------------------------------


def test_every_step_kind_is_classified_into_exactly_one_llm_bucket() -> None:
    """Tier 1: every executor step kind (``_STEP_KINDS``) is classified into
    EXACTLY ONE of llm-leaf / non-llm-leaf / container. A new step kind added to
    the union without a classification here → RED (forces a soundness decision;
    a silent non-LLM default would disable the credential check for that kind)."""
    llm_leaf = set(_LLM_LEAF_KINDS)
    non_llm_leaf = set(_NON_LLM_LEAF_KINDS)
    container = set(_CONTAINER_CHILDREN)

    # Coverage: the three buckets exactly partition the executor step universe.
    assert (llm_leaf | non_llm_leaf | container) == set(_STEP_KINDS)

    # Disjointness: no kind lands in two buckets (a kind is a leaf XOR a container).
    assert not (llm_leaf & non_llm_leaf)
    assert not (llm_leaf & container)
    assert not (non_llm_leaf & container)


# ---------------------------------------------------------------------------
# Tier 1 — per-kind behaviour falsify (WRONG classification, not just missing)
# ---------------------------------------------------------------------------


def _agent() -> AgentStep:
    return AgentStep(prompt="do the thing", identity="default")


def _transform() -> TransformStep:
    return TransformStep(value="pipe")


def test_agent_leaf_uses_llm() -> None:
    """Tier 1: a bare ``AgentStep`` is the True leaf — the predicate fires."""
    assert pipeline_uses_llm(Pipeline(steps=[_agent()])) is True


def test_transform_tool_shell_only_pipeline_does_not_use_llm() -> None:
    """Tier 1: transform / tool / ``shell`` (a ``ToolStep(name='shell')``, NOT a
    ShellStep) only → predicate is False. This is the false-positive-zero pin: a
    legitimate non-LLM pipeline must NEVER be classified as LLM-using (the
    reverted #2685 SystemExit-a-valid-pipeline regression)."""
    non_llm = Pipeline(steps=[
        _transform(),
        ToolStep(name="write_file", args={}),
        ToolStep(name="shell", args={}),
    ])
    assert pipeline_uses_llm(non_llm) is False


def test_fold_over_agent_uses_llm() -> None:
    """Tier 1: an ``AgentStep`` reachable ONLY through a ``fold`` container →
    True (container recursion into ``fold.do``)."""
    pl = Pipeline(steps=[FoldStep(init="0", do=_agent(), output="acc")])
    assert pipeline_uses_llm(pl) is True


def test_for_each_agent_in_do_and_in_collect_use_llm() -> None:
    """Tier 1: ``for_each`` recurses into BOTH ``do`` and ``collect`` — an
    ``AgentStep`` in either reaches the predicate."""
    via_do = Pipeline(steps=[
        ForEachStep(do=_agent(), collect=_transform(), on_error="abort"),
    ])
    via_collect = Pipeline(steps=[
        ForEachStep(do=_transform(), collect=_agent(), on_error="abort"),
    ])
    assert pipeline_uses_llm(via_do) is True
    assert pipeline_uses_llm(via_collect) is True


def test_for_each_transform_only_does_not_use_llm() -> None:
    """Tier 1: a ``for_each`` with non-LLM ``do`` AND ``collect`` → False."""
    pl = Pipeline(steps=[
        ForEachStep(do=_transform(), collect=_transform(), on_error="abort"),
    ])
    assert pipeline_uses_llm(pl) is False


def test_parallel_over_agent_branch_uses_llm() -> None:
    """Tier 1: an ``AgentStep`` in a ``parallel`` branch → True (recursion into
    every ``branches`` value + ``collect``)."""
    pl = Pipeline(steps=[
        ParallelStep(
            branches={"a": _transform(), "b": _agent()},
            collect=_transform(),
        ),
    ])
    assert pipeline_uses_llm(pl) is True


def test_call_resolves_callee_via_registry_and_recurses() -> None:
    """Tier 1: a ``call`` step's static sub-pipeline name is resolved through the
    ``PipelineRegistry`` and recursed into — an agent-using callee → True, a
    transform-only callee → False (registry resolution is REQUIRED for a
    ``CallStep`` to be classifiable; it has no in-line ``Step`` field)."""
    reg = PipelineRegistry()
    reg.register("sub_agent", Pipeline(steps=[_agent()], name="sub_agent"))
    reg.register("sub_plain", Pipeline(steps=[_transform()], name="sub_plain"))

    caller_llm = Pipeline(steps=[CallStep(pipeline="sub_agent")])
    caller_plain = Pipeline(steps=[CallStep(pipeline="sub_plain")])
    assert pipeline_uses_llm(caller_llm, reg) is True
    assert pipeline_uses_llm(caller_plain, reg) is False


def test_match_resolves_each_case_and_default_via_registry() -> None:
    """Tier 1: a ``match`` step resolves EACH case AND ``default`` via the
    registry and ORs the results — an ``AgentStep`` reachable through any case OR
    the default → True. A MatchStep has NO direct ``Step`` field, so without
    per-case registry resolution it would be misclassified as a leaf (silently
    disabling the check for match-routed agent pipelines)."""
    reg = PipelineRegistry()
    reg.register("sub_agent", Pipeline(steps=[_agent()], name="sub_agent"))
    reg.register("sub_plain", Pipeline(steps=[_transform()], name="sub_plain"))

    # Agent reachable only via a case.
    via_case = Pipeline(steps=[MatchStep(
        on="pipe",
        cases={"x": MatchCase(pipeline="sub_plain"), "y": MatchCase(pipeline="sub_agent")},
    )])
    # Agent reachable only via default.
    via_default = Pipeline(steps=[MatchStep(
        on="pipe",
        cases={"x": MatchCase(pipeline="sub_plain")},
        default=MatchCase(pipeline="sub_agent"),
    )])
    # All targets non-LLM → False.
    all_plain = Pipeline(steps=[MatchStep(
        on="pipe",
        cases={"x": MatchCase(pipeline="sub_plain")},
        default=MatchCase(pipeline="sub_plain"),
    )])
    assert pipeline_uses_llm(via_case, reg) is True
    assert pipeline_uses_llm(via_default, reg) is True
    assert pipeline_uses_llm(all_plain, reg) is False


def test_unresolvable_callee_does_not_fire() -> None:
    """Tier 1: an unresolvable ``call`` target (no registry, or name not
    registered) yields NO children → predicate does NOT fire (False) — consistent
    with 'let the executor surface the resolution error first', and keeping
    false-positive-zero (we never guess True for an unknown callee)."""
    caller = Pipeline(steps=[CallStep(pipeline="does_not_exist")])
    empty_reg = PipelineRegistry()
    assert pipeline_uses_llm(caller, None) is False
    assert pipeline_uses_llm(caller, empty_reg) is False


def test_call_cycle_is_guarded() -> None:
    """Tier 1: a self-referential ``call`` (or a mutual cycle) with no agent step
    terminates and returns False — the cycle guard prevents infinite recursion."""
    reg = PipelineRegistry()
    reg.register("loop", Pipeline(steps=[CallStep(pipeline="loop")], name="loop"))
    assert pipeline_uses_llm(Pipeline(steps=[CallStep(pipeline="loop")]), reg) is False


def test_nested_container_over_call_to_agent_uses_llm() -> None:
    """Tier 1: composition — a ``for_each`` whose ``do`` is a ``call`` to an
    agent-using sub-pipeline reaches the LLM through two container layers."""
    reg = PipelineRegistry()
    reg.register("sub_agent", Pipeline(steps=[_agent()], name="sub_agent"))
    pl = Pipeline(steps=[ForEachStep(
        do=CallStep(pipeline="sub_agent"), collect=_transform(), on_error="abort",
    )])
    assert pipeline_uses_llm(pl, reg) is True


# ---------------------------------------------------------------------------
# Tier 2 — run_run integration (LLM-less pipeline is never cred-rejected)
# ---------------------------------------------------------------------------


@pytest.fixture
def _keys_unset(monkeypatch, _provider_credentials_present):
    """Every provider env var (and the proxy switch) unset — reproduces the CI
    environment the #2685 regression was masked away from locally. Depends on
    ``_provider_credentials_present`` (the conftest autouse dummy-cred fixture)
    so this ``delenv`` runs AFTER its ``setenv`` and wins.

    LITELLM_API_BASE is cleared via direct ``os.environ.pop`` (the autouse
    ``_restore_litellm_api_base`` fixture owns its restore) rather than
    ``monkeypatch.delenv`` — mixing monkeypatch's undo with ``load_config()``'s
    own set of the same var confuses monkeypatch's bookkeeping (the exact
    hazard ``test_pipe_proxy_bootstrap_2682.py`` documents)."""
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    os.environ.pop("LITELLM_API_BASE", None)


def _write_reyn_yaml(root: Path, entries: dict) -> None:
    data = {
        "model": "standard",
        "models": {"standard": "openai/gpt-4o-mini"},
        "pipelines": {"entries": entries},
    }
    (root / "reyn.yaml").write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8",
    )


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_register_parses_run_subcommand() -> None:
    """Tier 2: the ``pipe`` subparser registers (guards the import surface this
    file's integration tests drive)."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register(sub)
    args = parser.parse_args(["pipe", "run", "somepipe"])
    assert args.name == "somepipe"


def test_run_non_llm_pipeline_does_not_exit_with_keys_unset(
    tmp_path, monkeypatch, capsys, _keys_unset,
) -> None:
    """Tier 2: THE #2685 regression, pinned dead — now STRUCTURAL. A transform-only
    pipeline runs to completion via ``run_run`` even with EVERY provider key unset
    and no proxy: it never reaches the LLM funnel (``recorded_acompletion``), so it
    is by construction immune to a missing-cred rejection (#2708 P3.2b)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.yaml").write_text(
        "pipeline: hello_cli\n"
        "steps:\n"
        "  - transform: {value: \"'hello ' + ctx.name\", output: greeting}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"hello_cli": {"path": "hello.yaml"}})

    args = _ns(
        name="hello_cli", input=json.dumps({"name": "world"}),
        project=str(tmp_path), async_=False,
    )
    run_run(args)  # must NOT raise SystemExit

    result = json.loads(capsys.readouterr().out)
    assert result["pipe_data"] == "hello world"


def test_run_unregistered_pipeline_surfaces_its_own_error_not_cred(
    tmp_path, monkeypatch, capsys, _keys_unset,
) -> None:
    """Tier 2: a NAME with no matching pipeline exits with the 'not registered'
    error, NOT the credential error — the check runs AFTER resolution, so
    resolution failures win (the #2685 preemption lesson)."""
    monkeypatch.chdir(tmp_path)
    _write_reyn_yaml(tmp_path, {})

    args = _ns(name="nope", input="{}", project=str(tmp_path), async_=False)
    with pytest.raises(SystemExit) as exc:
        run_run(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err.lower()
    assert "not registered" in err
    assert "no api key" not in err
