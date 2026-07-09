"""Tier 1/2: #2686 — CONDITIONAL LLM credential pre-check for ``reyn pipe run``.

``reyn pipe run`` gains ``reyn chat``'s early "no API key set" pre-check, but
fires it ONLY when the resolved pipeline actually uses an LLM (``pipeline_uses_llm``)
and only AFTER pipeline resolution. A PRIOR attempt (#2685) added the check
UNCONDITIONALLY and regressed: it ``SystemExit``-ed legitimate transform/tool-only
pipelines whose provider env var was merely unset, and preempted the "pipeline not
registered" error. That was reverted; #2686 re-adds it conditionally.

Structure of this file:

- Tier 1 completeness gate: every ``_STEP_KINDS`` step type is classified into
  exactly one LLM bucket (llm-leaf / non-llm-leaf / container). A new kind is RED
  until classified — a silent non-LLM default would unsoundly disable the check.
- Tier 1 per-kind behaviour falsify: the gate catches MISSING classification but
  not WRONG classification, so each container kind (fold/for_each/parallel/call/
  match) gets a minimal pipeline reaching an ``AgentStep`` ONLY through it →
  predicate True; transform/tool/shell-only → False (false-positive-zero).
- Tier 2 integration/core: the extracted config-level core
  (``verify_model_credentials_or_exit``) exits/short-circuits correctly, and
  ``run_run`` drives it — a non-LLM pipeline runs WITHOUT SystemExit even with
  every provider key unset (the #2685 regression, pinned dead); an agent pipeline
  with no key + no proxy exits early; "not registered" surfaces its own error.

Real dataclasses / real ``PipelineRegistry`` / real ``load_config`` / real
``run_run`` — no mocks. The credential pre-check for the agent-pipeline case
exits BEFORE any LLM spawn, so no LLM replay is needed here.
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
from reyn.interfaces.cli.credentials_check import verify_model_credentials_or_exit

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
# Tier 2 — the extracted config-level credential core
# ---------------------------------------------------------------------------


def _load_config_at(tmp_path: Path, monkeypatch, *, api_base: str | None):
    """Write a minimal reyn.yaml (optionally with api_base) and load it."""
    from reyn.config import load_config
    data: dict = {"model": "standard", "models": {"standard": "openai/gpt-4o-mini"}}
    if api_base is not None:
        data["api_base"] = api_base
    (tmp_path / "reyn.yaml").write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return load_config()


@pytest.fixture
def _keys_unset(monkeypatch):
    """Every provider env var (and the proxy switch) unset — reproduces the CI
    environment the #2685 regression was masked away from locally.

    LITELLM_API_BASE is cleared via direct ``os.environ.pop`` (the autouse
    ``_restore_litellm_api_base`` fixture owns its restore) rather than
    ``monkeypatch.delenv`` — mixing monkeypatch's undo with ``load_config()``'s
    own set of the same var confuses monkeypatch's bookkeeping (the exact
    hazard ``test_pipe_proxy_bootstrap_2682.py`` documents)."""
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    os.environ.pop("LITELLM_API_BASE", None)


def test_core_exits_when_key_unset_and_no_proxy(tmp_path, monkeypatch, _keys_unset) -> None:
    """Tier 2: no api_base + known provider prefix + env var unset → SystemExit(1)
    with an actionable message. This is the ONLY exit case."""
    config = _load_config_at(tmp_path, monkeypatch, api_base=None)
    with pytest.raises(SystemExit) as exc:
        verify_model_credentials_or_exit(config, "openai/gpt-4o-mini")
    assert exc.value.code == 1


def test_core_no_exit_when_key_present(tmp_path, monkeypatch, _keys_unset) -> None:
    """Tier 2: env var set → no exit (returns None)."""
    config = _load_config_at(tmp_path, monkeypatch, api_base=None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert verify_model_credentials_or_exit(config, "openai/gpt-4o-mini") is None


def test_core_proxy_api_base_is_a_no_op(tmp_path, monkeypatch, _keys_unset) -> None:
    """Tier 2: api_base (proxy) set → no-op even with the provider key unset —
    the proxy handles auth. This is the branch that makes an agent pipeline under
    a proxy config proceed without an early exit (the user's real dogfood setup)."""
    config = _load_config_at(tmp_path, monkeypatch, api_base="http://localhost:4000")
    assert verify_model_credentials_or_exit(config, "openai/gpt-4o-mini") is None


def test_core_none_bare_and_unknown_provider_are_no_ops(tmp_path, monkeypatch, _keys_unset) -> None:
    """Tier 2: resolver-failure (None), a bare model name (no '/'), and an
    unknown provider prefix all early-return — the check is deliberately narrow
    (false positives are worse than a late litellm error)."""
    config = _load_config_at(tmp_path, monkeypatch, api_base=None)
    assert verify_model_credentials_or_exit(config, None) is None
    assert verify_model_credentials_or_exit(config, "gpt-4o-mini") is None
    assert verify_model_credentials_or_exit(config, "cohere/command-r") is None


# ---------------------------------------------------------------------------
# Tier 2 — run_run integration (the conditional gate, end to end)
# ---------------------------------------------------------------------------


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
    """Tier 2: THE #2685 regression, pinned dead. A transform-only pipeline runs
    to completion via ``run_run`` even with EVERY provider key unset and no proxy
    — the conditional check must NOT fire for a non-LLM pipeline."""
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


def test_run_agent_pipeline_exits_early_when_key_unset_no_proxy(
    tmp_path, monkeypatch, capsys, _keys_unset,
) -> None:
    """Tier 2: an agent (LLM) pipeline with the provider key unset AND no proxy
    exits early (code 1) with the credential message — AFTER pipeline resolution,
    BEFORE any LLM spawn."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agent.yaml").write_text(
        "pipeline: agent_cli\n"
        "steps:\n"
        "  - agent: {prompt: \"summarize {pipe}\", identity: default}\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(tmp_path, {"agent_cli": {"path": "agent.yaml"}})

    args = _ns(name="agent_cli", input="{}", project=str(tmp_path), async_=False)
    with pytest.raises(SystemExit) as exc:
        run_run(args)
    assert exc.value.code == 1
    assert "no api key" in capsys.readouterr().err.lower()


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
