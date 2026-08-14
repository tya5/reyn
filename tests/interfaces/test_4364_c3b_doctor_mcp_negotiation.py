"""Tier 2: #4364 C-3(b) — ``reyn doctor``'s MCP negotiated
version/capabilities check.

architect's motivating case: a protocol-version mismatch between reyn and a
connected server had already fallen back silently to an older shared
version — nothing raised, and the only way to learn it happened was
digging through the audit log after the fact. This check surfaces the
same fact in one line, per declared ``mcp.servers`` entry.

Reuses the SAME windowed evidence-based scan shape C-2's
``mcp_resource_updated``/``webhook_received`` checks already use
(``_mcp_initialized_evidence``, sharing ``_MCP_EVENT_SCAN_MAX_FILES`` and
#4624's empty-history branch) — never a live probe (C-3(a), a real
``tools/list`` connect, was ruled unnecessary: this evidence already
exists from connections ``reyn`` itself made).

Real CLI invocation (mirrors ``test_4364_pr2b_doctor_external_pairing.py``'s
own established capsys-driven shape) against real config files + a real
``.reyn/events`` tree — no mocks.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_mcp_initialized_event(
    events_dir: Path, filename: str, *, server: str, version: str, capabilities: list,
) -> None:
    import json

    events_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "mcp_initialized",
        "timestamp": "2026-08-14T00:00:00+00:00",
        "data": {
            "server": server, "negotiated_version": version, "capabilities": capabilities,
        },
    }
    (events_dir / filename).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _mcp_yaml(*servers: str) -> str:
    body = "mcp:\n  servers:\n"
    for name in servers:
        body += f'    {name}:\n      command: npx\n      args: ["-y", "@mcp/{name}"]\n'
    return body


def test_no_servers_declared_prints_the_absence_not_a_finding(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: accept-side — no ``mcp.servers`` declared prints a plain
    "no MCP servers declared" line, not a per-server "?" finding (nothing
    to report on)."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "MCP servers — last negotiated version/capabilities" in out
    assert "no MCP servers declared" in out


def test_declared_server_with_evidence_shows_negotiated_version_and_capabilities(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: the core witness — a declared server with a real
    ``mcp_initialized`` record in ``.reyn/events`` surfaces its
    ``negotiated_version``/``capabilities`` verbatim."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML + _mcp_yaml("filesystem"))
    _write_mcp_initialized_event(
        tmp_path / ".reyn" / "events", "2026-08-14T000000.jsonl",
        server="filesystem", version="2025-11-25", capabilities=["resources", "tools"],
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ filesystem: last negotiated '2025-11-25'" in out
    assert "'resources', 'tools'" in out


def test_declared_server_with_no_event_history_says_the_plain_fact(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: #4624-shaped fresh-install case — no ``.reyn/events`` files
    at all (scanned == 0) prints "no event history yet", never the
    windowed caveat (which would be a true but empty statement with
    nothing to scan)."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML + _mcp_yaml("filesystem"))

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "? filesystem: no event history yet" in out
    assert "not seen in the newest" not in out


def test_declared_server_evidence_outside_the_scan_window_is_disclosed_not_silent(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: real evidence exists but predates the scan window — must
    surface the #4614-style '?' disclosure (NOT proof the server was
    never reached), not silence and not a false '✓'."""
    from reyn.interfaces.cli.commands import doctor as _doctor_mod

    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML + _mcp_yaml("filesystem"))
    events_dir = tmp_path / ".reyn" / "events"
    _write_mcp_initialized_event(
        events_dir, "2026-01-01T000000.jsonl",
        server="filesystem", version="2025-11-25", capabilities=["tools"],
    )
    for day in range(1, _doctor_mod._MCP_EVENT_SCAN_MAX_FILES + 1):
        import json
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / f"2026-02-{day:02d}T000000.jsonl").write_text(
            json.dumps({
                "type": "session_started", "timestamp": "2026-02-01T00:00:00+00:00", "data": {},
            }) + "\n",
            encoding="utf-8",
        )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "? filesystem: not seen in the newest" in out
    assert "NOT proof the server was never reached" in out
    assert "✓ filesystem" not in out


def test_a_server_with_evidence_and_one_without_are_reported_independently(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: two declared servers, only one with evidence — each line
    reflects ONLY its own server's evidence, not a shared verdict."""
    _write_yaml(
        tmp_path / "reyn.yaml", MINIMAL_REYN_YAML + _mcp_yaml("filesystem", "brave"),
    )
    _write_mcp_initialized_event(
        tmp_path / ".reyn" / "events", "2026-08-14T000000.jsonl",
        server="filesystem", version="2025-11-25", capabilities=["tools"],
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ filesystem: last negotiated" in out
    assert "? brave: not seen in the newest" in out


def test_misplaced_mcp_name_explains_why_nothing_is_declared(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: #4631's own shape defect (``mcp.<name>`` written where
    ``mcp.servers.<name>`` belongs) loads without error and without a
    warning — ``mcp.servers`` stays empty. Before this fix doctor printed
    the plain "no MCP servers declared" line here, which reads as "you
    never wrote anything", even though the operator DID write an entry —
    just at the wrong nesting. Doctor must name the misplaced entry
    instead of the bare absence."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML
        + 'mcp:\n  filesystem:\n    command: npx\n    args: ["-y", "@mcp/filesystem"]\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "no MCP servers declared under mcp.servers" in out
    assert "filesystem" in out
    assert "misplaced server entries" in out
    assert "reyn config validate" in out


def test_misplaced_mcp_name_alongside_a_real_server_is_noted_not_hidden(
    tmp_path: Path, capsys,
) -> None:
    """Tier 2: a real ``mcp.servers.<name>`` entry AND a misplaced
    ``mcp.<name>`` entry can coexist — the misplaced one must still surface
    as a note, not be swallowed because the section wasn't empty."""
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML
        + 'mcp:\n  servers:\n    filesystem:\n      command: npx\n'
        + '      args: ["-y", "@mcp/filesystem"]\n'
        + '  brave:\n    command: npx\n    args: ["-y", "@mcp/brave"]\n',
    )

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    assert "✓ filesystem" in out or "? filesystem" in out
    assert "misplaced server entries" in out
    assert "brave" in out
