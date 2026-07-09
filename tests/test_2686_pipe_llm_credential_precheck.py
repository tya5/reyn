"""Tier 2: #2686/#2708 — an LLM-less ``reyn pipe run`` is never rejected for
missing creds.

Since #2708 P3.2b the missing-cred pre-check moved OFF ``reyn pipe run``'s
per-surface startup gate and ONTO the single LLM funnel (``recorded_acompletion``,
tested in ``test_2708_cred_check_chokepoint.py``). The #2686 false-positive-zero
property is therefore now STRUCTURAL: an LLM-less (transform/tool-only) pipeline
never reaches the funnel, so it can never be rejected for a missing provider
key. ``run_run`` on a transform-only pipeline runs to completion even with every
provider key unset (the reverted #2685 regression, pinned dead — now by
construction, not a hand-maintained guard). A "not registered" name still
surfaces its own resolution error.

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
