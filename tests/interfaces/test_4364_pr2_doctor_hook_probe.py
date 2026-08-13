"""Tier 2: #4364 PR-2 (C-1) — ``reyn doctor``'s hook launch probe.

Real CLI invocation (mirrors ``test_4364_pr3a_doctor_cli.py``'s own
established capsys-driven shape) against a REAL configured hook + a REAL
sandbox backend + a REAL subprocess launch — no mocks. Answers "does this
hook's argv[0] run under this sandbox", never "did exec occur" (the
architect-ruled distinction ``probe_argv``'s own module docstring names —
the motivating incident, owner's Mac xcrun shim, WAS an exec that then
died mid-run; a probe asking "did exec happen" would not have caught it).
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


def _this_hosts_default_backend_can_probe() -> bool:
    """Whether ``get_default_backend()`` — the SAME resolution ``doctor.py``
    itself calls — can actually run a differential probe here. False on a
    CI runner with no `sandbox-linux` extra installed (this repo's normal
    pytest job does not install it, ``test.yml``): the resolved backend
    there is `NoopBackend` (no `sandbox-exec`/no Landlock package), whose
    `probe_binary()` is documented `None` by design (#4364 PR-2) — the
    probe correctly reports "cannot probe", not a broken mechanism. The 3
    tests below assert a REAL launch outcome and would otherwise fail on
    such a runner for a reason that has nothing to do with the code under
    test — mirrors ``test_probe_argv_4364.py``'s own
    ``_live_probing_backend()`` skip guard for the identical reason."""
    from reyn.security.sandbox.launcher import resolve_backend

    return resolve_backend(None, None).probe_binary() is not None


def test_no_configured_hooks_reports_that_plainly(tmp_path: Path, capsys):
    """Tier 2: (accept-side, absence) no exec/exec_capture hooks configured
    -> the section says so, never silently absent or a crash."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "Hook launch probe" in out
    assert "no exec/exec_capture hooks configured" in out


def test_a_runnable_hook_argv0_reports_ok(tmp_path: Path, capsys):
    """Tier 2: a hook whose argv[0] is a REAL binary this sandbox can
    launch — driven through the real default backend for this host, real
    subprocess launch, no mock. ``/usr/bin/true`` is the same shared
    positive-control lookup ``probe_binary()`` uses on every POSIX
    backend, so this test exercises the SAME binary the probe would find
    on its own — not a coincidence, a deliberate choice so the test can't
    silently pass by probing a DIFFERENT thing than what production does."""
    if not _this_hosts_default_backend_can_probe():
        pytest.skip("this host's default sandbox backend cannot probe (see helper docstring)")
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "hooks:\n"
            '  - "on": session_start\n'
            "    name: good-hook\n"
            '    exec: ["/usr/bin/true"]\n'
        ),
    )
    if not Path("/usr/bin/true").is_file():
        pytest.skip("this host has no /usr/bin/true — the probe's own control binary is absent")

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "good-hook" in out
    assert "is runnable under this hook's sandbox" in out
    assert "NOT runnable" not in out


def test_a_nonexistent_hook_argv0_reports_target_failed_with_the_false_positive_disclosure(
    tmp_path: Path, capsys,
):
    """Tier 2: LOAD-BEARING falsification — a hook whose argv[0] names a
    binary that does not exist anywhere on this host must be reported as
    NOT runnable, distinguishing it from the accept-path test above (same
    real backend, same real launch mechanism, only the binary differs).
    Also pins D-3's disclosure requirement (architect ruling): the message
    must name the no-arguments caveat rather than asserting "broken" —
    ``probe_argv``'s own module docstring on why ``target_failed`` is not
    resolved further."""
    if not _this_hosts_default_backend_can_probe():
        pytest.skip("this host's default sandbox backend cannot probe (see helper docstring)")
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "hooks:\n"
            '  - "on": session_start\n'
            "    name: bad-hook\n"
            '    exec: ["/definitely/not/a/real/binary-4364-pr2"]\n'
        ),
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "bad-hook" in out
    assert "is NOT runnable under this hook's sandbox" in out
    # D-3: the false-positive caveat must be stated, not just the verdict.
    assert "a program that requires arguments" in out


def test_the_probe_never_passes_the_hooks_own_configured_arguments(
    tmp_path: Path, capsys,
):
    """Tier 2: LOAD-BEARING falsification for D-2 — a hook whose argv is
    ``[<a real binary>, "--flag-that-does-not-exist"]`` must be probed
    with argv[0] ALONE. If the probe passed the configured args, this
    specific binary+flag combination would exit non-zero (the flag is
    real but does not exist for this binary) and report "NOT runnable" —
    the WRONG verdict for a hook whose argv[0] genuinely launches fine.
    Uses this test's own throwaway script as the target so it fully
    controls both branches' exit codes, rather than depending on a real
    system binary's flag-parsing behavior."""
    if not _this_hosts_default_backend_can_probe():
        pytest.skip("this host's default sandbox backend cannot probe (see helper docstring)")
    script = tmp_path / "probe_target.sh"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -eq 0 ]; then exit 0; else exit 1; fi\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "hooks:\n"
            '  - "on": session_start\n'
            "    name: args-sensitive-hook\n"
            f'    exec: ["{script}", "--this-flag-triggers-exit-1"]\n'
        ),
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "args-sensitive-hook" in out
    assert "is runnable under this hook's sandbox" in out, (
        "the probe reported NOT runnable — it must have passed the "
        "configured args (which this script exits 1 on) instead of "
        f"argv[0] alone. Full output:\n{out}"
    )
