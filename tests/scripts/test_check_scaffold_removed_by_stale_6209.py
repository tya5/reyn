"""Tier 2: #6209 — the scaffold-removed-by-stale gate.

Real `tests/scaffold/` is used ONLY for the accept④-style "0 offenders
today" witness (a lightweight, real-`gh`-calling assertion, skipped
without failing the suite when `gh` is unavailable/unauthenticated —
this file's own network-dependence disclosure). Accept①② (a stale file
IS flagged / a prose-condition file is NOT) are proven exclusively via
`tmp_path` fixtures with an INJECTED `is_closed` lookup (never the real
`gh`) — per lead-coder's own explicit instruction (#6209 dispatch): a
real-tree dependence for ① would go quietly vacuous the instant the
real offending file (`test_6184_2b1_compose_truncate_split.py`) was
removed by #6207/#6210, which already happened.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from scripts.check_scaffold_removed_by_stale import (
    extract_removed_by,
    find_stale_scaffold_files,
    referenced_numbers,
)
from tests._support.paths import REPO_ROOT


def _write_scaffold(tmp_path, name: str, *, triggered_by: str, removed_by: str) -> None:
    (tmp_path / name).write_text(
        f'# scaffold: triggered_by="{triggered_by}"\n'
        f'# scaffold: removed_by="{removed_by}"\n'
        '"""Tier scaffold: a fixture file, not a real test."""\n',
        encoding="utf-8",
    )


# ── extraction helpers ────────────────────────────────────────────────────


def test_extract_removed_by_reads_the_real_5620_files_own_condition() -> None:
    """Tier 2: non-vacuity for the parser — reads the REAL, currently
    landed file's own removed_by string, not a hand-typed copy."""
    text = (REPO_ROOT / "tests" / "scaffold" / "test_5620_litellm_proxy_defects.py").read_text(
        encoding="utf-8",
    )
    removed_by = extract_removed_by(text)
    assert removed_by is not None
    assert "upstream" in removed_by


def test_referenced_numbers_empty_for_a_pure_prose_condition() -> None:
    """Tier 2: the real #5620 file's own removed_by names ZERO numbers —
    the exact shape this gate must never flag."""
    text = (REPO_ROOT / "tests" / "scaffold" / "test_5620_litellm_proxy_defects.py").read_text(
        encoding="utf-8",
    )
    removed_by = extract_removed_by(text)
    assert removed_by is not None
    assert referenced_numbers(removed_by) == []


def test_referenced_numbers_dedupes_and_sorts() -> None:
    """Tier 2: a condition naming the same number twice, or out of
    order, still produces a clean, deduplicated list."""
    assert referenced_numbers("#6184 lands, see also #6184 and #6100") == [6100, 6184]


# ── acceptance① — a stale (all-named-numbers-closed) file is flagged ─────


def test_a_file_naming_only_closed_numbers_is_flagged(tmp_path) -> None:
    """Tier 2: acceptance① — the exact #6184-2b1 shape reproduced
    synthetically (a fixture, never the real tree — see module
    docstring): a `removed_by` naming a single closed number."""
    _write_scaffold(
        tmp_path, "test_fake.py",
        triggered_by="#1 -- some refactor",
        removed_by="#1 lands",
    )
    offenders = find_stale_scaffold_files(tmp_path, is_closed=lambda n: True)
    [(path, removed_by, numbers)] = offenders  # exactly this one fixture file
    assert path.name == "test_fake.py"
    assert numbers == [1]


def test_a_file_naming_one_open_number_among_several_is_not_flagged(tmp_path) -> None:
    """Tier 2: accept-side of① — ALL named numbers must be closed; one
    still-open number among several keeps the file green (proves this
    is a genuine "all", not "any")."""
    _write_scaffold(
        tmp_path, "test_fake.py",
        triggered_by="#1 -- some refactor",
        removed_by="#1 and #2 both land",
    )
    offenders = find_stale_scaffold_files(
        tmp_path, is_closed=lambda n: n == 1,  # #2 stays "open"
    )
    assert offenders == []


# ── acceptance② — a prose (no-number) condition is NEVER flagged ─────────


def test_a_pure_prose_condition_is_never_flagged_even_if_is_closed_always_true(
    tmp_path,
) -> None:
    """Tier 2: acceptance② (deny side) — the real #5620 shape,
    synthetically reproduced: a removed_by with NO number. Even an
    `is_closed` that would say "yes" to everything must not flag this
    file, because no number was ever checked — proves the gate reads
    `removed_by`'s own CONTENT, not merely "a removed_by comment
    exists"."""
    _write_scaffold(
        tmp_path, "test_fake.py",
        triggered_by="#5620 -- some patch",
        removed_by="the upstream bug this file reproduces is fixed",
    )
    offenders = find_stale_scaffold_files(tmp_path, is_closed=lambda n: True)
    assert offenders == []


def test_triggered_by_being_closed_does_not_flag_a_prose_removed_by(tmp_path) -> None:
    """Tier 2: acceptance② — the SPECIFIC incident #6209 exists to avoid:
    a naive "triggered_by closed -> red" rule would false-positive the
    real #5620 file (triggered_by names #5620, which IS closed) the
    moment it landed. This test pins that this gate does not read
    triggered_by at all -- `is_closed` here would flag #5620 if it were
    ever consulted."""
    _write_scaffold(
        tmp_path, "test_fake.py",
        triggered_by="#5620 -- a patch",
        removed_by="fixed upstream, no ticket tracks this",
    )
    consulted: "list[int]" = []

    def is_closed(n: int) -> bool:
        consulted.append(n)
        return True

    offenders = find_stale_scaffold_files(tmp_path, is_closed=is_closed)
    assert offenders == []
    assert consulted == [], (
        f"gate must never look up a number at all when removed_by names "
        f"none -- consulted {consulted!r}"
    )


# ── acceptance③ — the failure message names the file, condition, and numbers ─


def test_main_failure_message_names_file_condition_and_numbers(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance③ — drives the real `main()` against a
    monkeypatched scaffold dir + injected `is_closed`, reads the real
    printed message."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    import scripts.check_scaffold_removed_by_stale as gate_module

    _write_scaffold(
        tmp_path, "test_fake_stale.py",
        triggered_by="#1 -- x",
        removed_by="#1 lands",
    )
    monkeypatch.setattr(gate_module, "_SCAFFOLD_DIR", tmp_path)
    monkeypatch.setattr(gate_module, "_real_is_closed", lambda n: True)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = gate_module.main()

    assert code == 1
    message = err.getvalue()
    assert "test_fake_stale.py" in message
    assert "[1]" in message
    assert "#1 lands" in message


def test_main_reports_ok_and_exit_0_when_nothing_is_stale(tmp_path, monkeypatch) -> None:
    """Tier 2: accept-side of③ — a clean tmp_path scaffold dir (no
    files at all) reports OK, exit 0."""
    import io
    from contextlib import redirect_stdout

    import scripts.check_scaffold_removed_by_stale as gate_module

    monkeypatch.setattr(gate_module, "_SCAFFOLD_DIR", tmp_path)
    monkeypatch.setattr(gate_module, "_real_is_closed", lambda n: True)

    out = io.StringIO()
    with redirect_stdout(out):
        code = gate_module.main()
    assert code == 0
    assert "OK" in out.getvalue()


# ── real tree — 0 offenders today (network-dependent, disclosed) ─────────


def test_the_real_tree_has_zero_offenders_today() -> None:
    """Tier 2: real-tree witness, NOT accept①'s own proof (see module
    docstring — accept① is proven exclusively via the tmp_path fixture
    above, since the real offending file this issue names was already
    removed by #6207/#6210 by the time this gate landed). This test
    hits the REAL `gh` CLI — skipped (not failed) if `gh` is
    unavailable or unauthenticated in this environment, disclosed here
    rather than presented as unconditional coverage."""
    if shutil.which("gh") is None:
        pytest.skip("gh CLI not available in this environment")
    # cwd=REPO_ROOT: tests/conftest.py's own autouse per-test
    # chdir(tmp_path) isolation means the ambient cwd here is NOT a git
    # checkout at all -- :owner/:repo cannot resolve there.
    probe = subprocess.run(
        ["gh", "api", "repos/:owner/:repo", "--jq", ".id"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    if probe.returncode != 0:
        pytest.skip(f"gh not authenticated/reachable here: {probe.stderr.strip()}")

    from scripts.check_scaffold_removed_by_stale import _SCAFFOLD_DIR, _real_is_closed

    offenders = find_stale_scaffold_files(_SCAFFOLD_DIR, _real_is_closed)
    assert offenders == [], f"real stale scaffold file(s) found: {offenders}"


# ── the gate script runs cleanly as a real subprocess ─────────────────────


def test_the_gate_script_runs_clean_as_a_real_subprocess(out_of_process_reyn: str) -> None:
    """Tier 2: same discipline as the other #6184-arc gates' own test
    files — a real subprocess invocation, since that's how CI actually
    exercises this script. Skipped (not failed) if `gh` is unavailable,
    matching the real-tree test's own disclosure above."""
    if shutil.which("gh") is None:
        pytest.skip("gh CLI not available in this environment")

    gate_script = REPO_ROOT / "scripts" / "check_scaffold_removed_by_stale.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = out_of_process_reyn
    result = subprocess.run(
        [sys.executable, str(gate_script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"gate script failed as a real subprocess: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
