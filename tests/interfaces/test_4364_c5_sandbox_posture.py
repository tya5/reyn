"""Tier 2: #4364 C-5 — ``reyn doctor``'s resolved sandbox posture section.

Architect's ruling (issue #4364): declared ``sandbox.backend``/
``sandbox.on_unsupported``/``sandbox.policy`` next to the ACTUALLY RESOLVED
backend — production's own resolution (:func:`reyn.security.sandbox.
launcher.resolve_backend`, the SAME call C-1's hook probe already makes),
never a second doctor-invented probe. Motivating real case: an operator read
"no sandbox.policy declared" as "unrestricted", but the resolved backend was
actually enforcing (SeatbeltBackend, write_paths=[]) — declaration and
resolution silently disagreed.

No mocks — drives the real ``run`` against real on-disk state under
``tmp_path`` and the REAL backend-resolution machinery (whatever backend
this host's platform actually resolves to), matching this file family's
established shape (``test_4364_pr3a_doctor_cli.py``).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    return tmp_path


def _unavailable_backend_name() -> str:
    """Whichever of the two platform-specific backends this host does NOT
    have — real ``.available()`` checks, no mock. Both modules import
    cleanly on every platform (``available()`` itself gates on
    ``platform.system()``), so this is portable across CI hosts."""
    from reyn.security.sandbox.backends.landlock import LandlockBackend
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    if not LandlockBackend().available():
        return "landlock"
    if not SeatbeltBackend().available():
        return "seatbelt"
    pytest.skip("both seatbelt and landlock report available on this host")


# ── declared vs. resolved — the section exists and shows both ─────────────


def test_sandbox_section_prints_declared_and_a_real_resolved_backend(project, capsys):
    """Tier 2: the section header and both a declared line and a resolved
    line are present — the resolved backend name is one of the real,
    concrete backend names production can hand back, never a doctor-
    invented placeholder."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "Sandbox posture" in out
    declared_line = next(line for line in out.splitlines() if line.strip().startswith("declared: sandbox.backend"))
    assert "sandbox.backend='auto'" in declared_line
    assert "sandbox.on_unsupported='warn'" in declared_line
    resolved_line = next(line for line in out.splitlines() if line.strip().startswith("resolved:"))
    assert any(name in resolved_line for name in ("'seatbelt'", "'landlock'", "'noop'"))


def test_no_sandbox_policy_states_it_is_not_the_same_as_unrestricted(project, capsys):
    """Tier 2: THE core C-5 value — the exact real-world confusion the
    check was written for (#4364 architect note: an operator read "no
    sandbox.policy declared" as "unrestricted", but the resolved backend
    was enforcing all along). Absence of a declaration must never be
    reported as if it meant no restriction."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "no sandbox.policy" in out
    assert "NOT the same as unrestricted" in out


def test_declared_write_scope_is_shown_verbatim(project, capsys):
    """Tier 2: an operator-declared allow_write_paths/deny_write_paths is
    echoed back — the declared side of the declared-vs-resolved pair,
    never merged with any doctor-invented op-context floor (architect/
    lead-coder ruling: doctor has no op context)."""
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + "\nsandbox:\n  policy:\n    allow_write_paths: ['/tmp/some-declared-path']\n",
    )
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    scope_line = next(line for line in out.splitlines() if "declared write scope" in line)
    assert "/tmp/some-declared-path" in scope_line
    assert "allow_write_paths" in scope_line


def test_declared_policy_with_no_write_scope_keys_says_so(project, capsys):
    """Tier 2: accept-side — a sandbox.policy block that sets something
    OTHER than write scope (e.g. network) still reports honestly that
    neither write-scope key is present, rather than fabricating an empty
    scope dict that would read as "declared: no write access"."""
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + "\nsandbox:\n  policy:\n    network: false\n",
    )
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "neither allow_write_paths nor deny_write_paths appears" in out


def test_doctor_never_writes_anything_while_reporting_sandbox_posture(project, capsys):
    """Tier 2: D-2 falsify — resolving a real backend must not itself
    perform (or leave evidence of) a write doctor didn't declare it was
    doing; the project directory's only file after the run is still
    exactly reyn.yaml (+ whatever the OTHER sections legitimately wrote,
    none of which touch this fixture's sandbox posture path)."""
    before = {p for p in project.rglob("*") if p.is_file()}
    run(Namespace(project_root=str(project)))
    capsys.readouterr()
    after = {p for p in project.rglob("*") if p.is_file()}
    assert after == before, "doctor's sandbox-posture check must never write to the project"


# ── forced-unavailable backend — downgrade is flagged, not silent ─────────


def test_forced_unavailable_backend_downgrades_and_is_flagged(project, capsys):
    """Tier 2: THE downgrade case — an operator FORCES a specific backend
    name that this host cannot actually provide, with the default
    on_unsupported='warn'. Production silently falls back to NoopBackend
    (by design — #2983's own on_unsupported contract); doctor must NOT
    silently agree with that silence — the declared/resolved mismatch is
    named explicitly as a downgrade, never printed as if 'noop' were what
    was asked for."""
    forced = _unavailable_backend_name()
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + f"\nsandbox:\n  backend: {forced}\n  on_unsupported: warn\n",
    )
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    resolved_line = next(line for line in out.splitlines() if line.strip().startswith("resolved:"))
    assert "DOWNGRADED" in resolved_line
    assert f"declared {forced!r}" in resolved_line
    assert "'noop'" in resolved_line


def test_forced_unavailable_backend_with_on_unsupported_error_refuses(project, capsys):
    """Tier 2: on_unsupported='error' makes RESOLUTION ITSELF fail-closed
    (RuntimeError) rather than silently falling back — doctor must report
    that refusal (D-1: measure, don't paper over a raise) instead of
    crashing the whole command or silently swallowing it."""
    forced = _unavailable_backend_name()
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + f"\nsandbox:\n  backend: {forced}\n  on_unsupported: error\n",
    )
    run(Namespace(project_root=str(project)))  # must not raise
    out = capsys.readouterr().out
    assert "resolved: refuses to run" in out


def test_auto_backend_that_resolves_to_noop_is_not_flagged_as_a_downgrade(project, capsys):
    """Tier 2: accept-side — when the DECLARED backend is 'auto' (the
    default; no operator forced anything) and this host has no real
    backend, the resolution to 'noop' is legitimate auto-selection, not a
    downgrade from an operator's explicit request — must not print
    'DOWNGRADED' in that case."""
    from reyn.security.sandbox.launcher import resolve_backend

    resolved = resolve_backend(None, None)
    if resolved.name != "noop":
        pytest.skip("this host resolves 'auto' to a real backend — downgrade-vs-auto distinction not exercised")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    resolved_line = next(line for line in out.splitlines() if line.strip().startswith("resolved:"))
    assert "DOWNGRADED" not in resolved_line
