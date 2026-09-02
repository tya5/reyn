"""Tier 2: #4364 (lead-coder assignment, tui-coder's candidate ① — part of
#4364) — ``reyn doctor`` gains an ``external_transports:`` inert-key row.

Real defect: ``config/loader.py``'s ``_build_external_transports_config``
accepts any transport name (``for name, raw_entry in raw.items()``), but
the only 2 real consumers are ``interfaces/web/deps.py``'s outbox-
interceptor wiring and ``interfaces/web/server.py``'s cron-failure
notifier — both reachable only through the web/AGUI server runner, never
through ``reyn chat``'s own run-loop. Same third-state shape
``config.py``'s AgentProfile-unknown-key report already established
("it is read, kept in no in-memory state, and does nothing"), applied
here to a section that IS recognized but whose live effect depends on
which runner the operator actually uses (a fact doctor cannot observe,
D-2).

No mocks — drives the real ``run`` against a real parsed config under
``tmp_path``, matching this command family's own established shape
(``test_4364_storage_cap_doctor_row.py``).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_EXTERNAL_TRANSPORTS_YAML = (
    "external_transports:\n"
    "  broker:\n"
    "    mcp_tool: broker__post_message\n"
    "    args_template:\n"
    "      text: \"{text}\"\n"
)


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _external_transports_section(out: str) -> str:
    """The doctor output's ``external_transports:`` section body — from
    its own header line up to (not including) the blank line that
    separates it from the NEXT section (every section in this command
    is separated by a bare ``print()``, doctor.py's own established
    shape — see ``run()``'s own section-by-section structure)."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("external_transports:"))
    end = start + 1
    while end < len(lines) and lines[end] != "":
        end += 1
    return "\n".join(lines[start:end])


def test_configured_transport_reports_inert_and_names_real_consumers(
    tmp_path: Path, capsys,
):
    """Tier 2: accept-side — a declared ``external_transports.broker``
    entry produces a row naming it BY NAME as inert under ``reyn chat``,
    and names the 2 real consumer files."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML + _EXTERNAL_TRANSPORTS_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    section = _external_transports_section(out)
    assert "broker" in section
    assert "1 configured" in section
    assert "interfaces/web/deps.py" in section
    assert "interfaces/web/server.py" in section
    assert "reyn chat" in section


def test_unconfigured_external_transports_says_so_and_names_nothing(
    tmp_path: Path, capsys,
):
    """Tier 2: deny-side — with no ``external_transports:`` block at all,
    the row must say "unconfigured" and must NOT print the "configured"
    finding form at all (falsify pair with the test above — an
    always-inert-warning implementation would fail THIS test; an
    always-unconfigured implementation would fail the test above)."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    section = _external_transports_section(out)
    assert "unconfigured" in section
    assert "configured (" not in section
    assert "interfaces/web/deps.py" not in section
