"""Tier 2: #5351 (B-2) — ``reyn doctor``'s hook launch probe (#4364 PR-2 /
C-1) now covers ALL hook layers (startup/runtime/per-agent) and names
which layer declared each hook, not just the startup one.

Before this fix, ``_configured_exec_hooks`` built its own single-layer
``load_hooks(config.hooks)`` registry — a ``per-agent``-only
``exec``/``exec_capture`` hook was never probed at all (not "probed with
the wrong origin" — silently ABSENT from the probe section entirely).
That's the concrete instance of #5244③'s general shape ("declared,
accepted, effect not visible") this PR closes for doctor's own probe
section specifically.

Real CLI invocation (mirrors ``test_4364_pr2_doctor_hook_probe.py``'s own
established capsys-driven shape) — no mocks."""
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
    """Same guard as ``test_4364_pr2_doctor_hook_probe.py`` — see that
    file's own docstring for why (a CI runner with no sandbox extra
    installed resolves `NoopBackend`, whose `probe_binary()` is
    documented `None` by design)."""
    from reyn.security.sandbox.launcher import resolve_backend

    return resolve_backend(None, None).probe_binary() is not None


def test_a_per_agent_only_hook_is_now_probed_and_labeled_per_agent(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: LOAD-BEARING — before this fix, a hook declared ONLY under
    ``.reyn/agents/<name>/hooks.yaml`` never appeared in the probe section
    at all (the pre-fix registry was startup-only). It must now appear,
    labeled ``(per-agent)``."""
    if not _this_hosts_default_backend_can_probe():
        pytest.skip("this host's default sandbox backend cannot probe (see helper docstring)")
    if not Path("/usr/bin/true").is_file():
        pytest.skip("this host has no /usr/bin/true — the probe's own control binary is absent")
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        tmp_path / ".reyn" / "agents" / "myagent" / "hooks.yaml",
        "hooks:\n"
        '  - "on": session_start\n'
        "    name: per-agent-exec-hook\n"
        '    exec: ["/usr/bin/true"]\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "per-agent-exec-hook (per-agent)" in out, (
        f"a per-agent-only exec hook must now be probed AND labeled with "
        f"its real origin -- full output:\n{out}"
    )
    assert "is runnable under this hook's sandbox" in out


def test_a_startup_hook_is_labeled_startup_not_unknown(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: accept-side witness -- a startup-layer hook (the ONLY layer
    the pre-fix code ever probed) must keep showing up, now correctly
    labeled ``(startup)`` rather than the #5213 ``"unknown"`` default a
    registry built without threading ``origin=`` through would silently
    fall back to."""
    if not _this_hosts_default_backend_can_probe():
        pytest.skip("this host's default sandbox backend cannot probe (see helper docstring)")
    if not Path("/usr/bin/true").is_file():
        pytest.skip("this host has no /usr/bin/true — the probe's own control binary is absent")
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "hooks:\n"
            '  - "on": session_start\n'
            "    name: startup-exec-hook\n"
            '    exec: ["/usr/bin/true"]\n'
        ),
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "startup-exec-hook (startup)" in out, (
        f"a startup-layer hook must be labeled (startup), never the "
        f"'unknown' fallback a stripped origin= would silently produce -- "
        f"full output:\n{out}"
    )
