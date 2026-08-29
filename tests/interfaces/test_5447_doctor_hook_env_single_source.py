"""Tier 2: #5447 — ``reyn doctor``'s hook-env section prints
``HookProcessContext.as_env()`` verbatim, replacing #5428's original
shape (``Session.hook_env_snapshot()``, a public method doctor never
actually called — architect finding: the #4866 shape #5442 already spent
a PR closing, plus a second, independent copy of the 4 ``REYN_*`` names
living in ``doctor.py``'s own literal ``print(f"...")`` lines, breaking
``HookProcessContext``'s own docstring guarantee "so the four names stay
defined in exactly one place").

Real CLI invocation (mirrors ``test_4364_pr2_doctor_hook_probe.py``'s own
established capsys-driven shape) against a REAL ``.reyn/agents/<name>/``
tree — no mocks, no live ``Session`` (doctor constructs none).

Witnesses:
    1. All 4 REYN_* keys appear in doctor's real printed output for a
       configured agent.
    2. An agent-profile ``base_dir`` override changes the printed value
       on the NEXT ``doctor`` run.
    3. Single-source, not a count coincidence: monkeypatching
       ``HookProcessContext.as_env`` to return a 5TH key makes that key
       appear in doctor's output with ZERO changes to ``doctor.py`` —
       proving doctor iterates the returned mapping rather than printing
       4 hardcoded literal lines.
    4. Gate: ``git grep -nE 'def hook_env_snapshot' -- src/`` is empty
       (the method itself does not exist — pinning NON-EXISTENCE, not
       "no call site": the latter would read GREEN the moment the
       method is reintroduced with zero callers, which IS #5447's
       defect, and would push a fixer toward deleting a legitimate
       future caller instead of the method — architect finding on this
       PR's first revision) and
       ``git grep 'context\\.as_env(' -- src/reyn/interfaces/cli/commands/doctor.py``
       is non-empty (doctor's own call-site, not merely a docstring
       mention of the name).
"""
from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

from reyn.hooks.shell_runner import HookProcessContext
from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.paths import REPO_ROOT

_REYN_KEYS = {
    "REYN_PROJECT_DIR", "REYN_AGENT_BASE_DIR", "REYN_AGENT_NAME",
    "REYN_AGENT_STATE_DIR",
}


def _write_agent(tmp_path: Path, agent_name: str, base_dir: "str | None" = None) -> None:
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    agent_dir = tmp_path / ".reyn" / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    if base_dir is not None:
        (agent_dir / "profile.yaml").write_text(f"base_dir: {base_dir}\n", encoding="utf-8")


# ── witness 1: all 4 keys reach doctor's real printed output ──────────────


def test_doctor_prints_all_four_reyn_keys(tmp_path: Path, capsys):
    """Tier 2: witness ① — the 4 REYN_* keys ``HookProcessContext.as_env()``
    defines all appear in ``reyn doctor``'s real printed output for a
    configured agent, sourced with no live Session."""
    _write_agent(tmp_path, "alpha")

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "alpha:" in out
    for key in _REYN_KEYS:
        assert f"    {key}=" in out, f"{key} missing from doctor output:\n{out}"


# ── witness 2: live per-run read, not frozen ───────────────────────────────


def test_doctor_reflects_a_profile_base_dir_override_on_the_next_run(
    tmp_path: Path, capsys,
):
    """Tier 2: witness ② — writing an agent-profile ``base_dir`` override
    changes ``REYN_AGENT_BASE_DIR`` on the NEXT ``reyn doctor`` run (doctor
    re-reads ``profile.yaml`` fresh every call; nothing is cached across
    runs since doctor holds no live Session to cache on)."""
    _write_agent(tmp_path, "alpha")
    run(Namespace(project_root=str(tmp_path)))
    before_out = capsys.readouterr().out
    before = next(
        line for line in before_out.splitlines() if "REYN_AGENT_BASE_DIR=" in line
    )

    narrowed = tmp_path / "narrowed"
    narrowed.mkdir()
    _write_agent(tmp_path, "alpha", base_dir=str(narrowed))

    run(Namespace(project_root=str(tmp_path)))
    after_out = capsys.readouterr().out
    after = next(
        line for line in after_out.splitlines() if "REYN_AGENT_BASE_DIR=" in line
    )

    assert after != before, (
        "a base_dir override written between two doctor runs must change "
        f"the SECOND run's printed value — got the same line twice: {after!r}"
    )
    assert str(narrowed.resolve()) in after


# ── witness 3: single-source, not a count coincidence ──────────────────────


def test_a_fifth_as_env_key_reaches_doctor_output_with_no_doctor_py_change(
    tmp_path: Path, capsys, monkeypatch,
):
    """Tier 2: witness ③ — the essential one. Monkeypatching
    ``HookProcessContext.as_env`` to return a 5TH key and asserting that
    key appears in doctor's printed output proves doctor iterates the
    returned mapping (``for key, value in context.as_env().items()``)
    rather than 4 hardcoded ``print(f"REYN_...")`` literals — the
    reproduction of #5447's original defect. A doctor.py still hardcoding
    4 literal prints would leave this 5th key silently absent; this test
    would catch that regression without any change to this test file."""
    _write_agent(tmp_path, "alpha")

    real_as_env = HookProcessContext.as_env

    def patched_as_env(self: HookProcessContext) -> "dict[str, str]":
        env = real_as_env(self)
        env["REYN_FUTURE_FIELD"] = "sentinel-value"
        return env

    monkeypatch.setattr(HookProcessContext, "as_env", patched_as_env)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "REYN_FUTURE_FIELD=sentinel-value" in out, (
        "doctor's hook-env section did not propagate a 5th as_env() key — "
        "it is hardcoding the 4 REYN_* prints again instead of iterating "
        f"context.as_env().items():\n{out}"
    )


# ── witness 4: the observable, grep-able gate itself ───────────────────────


def test_hook_env_snapshot_does_not_exist(tmp_path: Path) -> None:
    """Tier 2: gate — pins ABSENCE of the method itself, not absence of a
    call site to it (architect finding on this PR's first revision: a
    "no call site" gate reads GREEN the moment the method is
    reintroduced with zero callers — which IS #5447's defect, so that
    phrasing pushed a fixer toward deleting a legitimate future caller
    rather than the method). ``def hook_env_snapshot`` in ``src/`` is 0
    hits today (removed, #5447); reintroducing the method — called or
    not — must turn this RED."""
    result = subprocess.run(
        ["git", "grep", "-nE", r"def hook_env_snapshot", "--", "src/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "Session.hook_env_snapshot() was reintroduced — this is the "
        "#4866/#5442 shape (a public method with no guaranteed real "
        f"consumer) #5447 removed it to close:\n{result.stdout}"
    )


def test_doctor_calls_as_env_not_a_docstring_mention(tmp_path: Path) -> None:
    """Tier 2: gate — ``doctor.py`` has a REAL call site
    ``context.as_env()`` / ``.as_env().items()``, not merely a docstring
    or comment naming the method (the exact ambiguity architect's
    original #5428 phrasing left open)."""
    result = subprocess.run(
        ["git", "grep", "-n", r"as_env()\.items\|context\.as_env(",
         "--", "src/reyn/interfaces/cli/commands/doctor.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, "no real .as_env() call site found in doctor.py"
    assert result.stdout.strip(), "grep produced no output despite rc==0"
