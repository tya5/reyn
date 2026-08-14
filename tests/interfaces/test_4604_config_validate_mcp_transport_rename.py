"""Tier 2: #4604 — ``reyn config validate``'s MCP transport-type check.

reyn's own MCP transport-type vocabulary renamed ``"http"`` to
``"streamable-http"``, aligning with the Agent Plugins 1.0 canonical
``mcp.schema.json``. ``MCPClient.__init__`` already rejects the old value
with a clear error naming the rename, but that only fires the next time the
server is actually connected — the exact #4401 shape (a real failure
discovered only much later) lead-coder flagged when assigning this issue,
citing the owner's own real ``.reyn/config/mcp.yaml`` (still ``type: http``
at assignment time). This check finds a stale ``type: http`` entry
proactively, without connecting to anything.

Checked PER SOURCE FILE (the SAME scan #4631's own placement check already
does — this module's ``static_mcp_sources``/dynamic-file loop runs both
detectors off one loaded raw dict per source, not two separate reads), so
the finding can name which file to fix.

Real CLI invocation against real config files (mirrors
``test_4631_config_validate_mcp_placement.py``'s own established project
fixture) — no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.setattr(
        "reyn.config.loader._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_streamable_http_entry_produces_no_finding(project, capsys):
    """Tier 2: accept-side — the current, correct value must NOT be
    flagged."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    myserver:\n"
            "      type: streamable-http\n      url: \"https://example.com/mcp\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


def test_a_stdio_entry_produces_no_finding(project, capsys):
    """Tier 2: accept-side — an unrelated transport type (never renamed)
    must not false-positive."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    myserver:\n"
            "      type: stdio\n      command: python\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out
    assert "MCP transport type" not in out


def test_a_server_entry_with_type_http_in_reyn_yaml_is_flagged(project, capsys):
    """Tier 2: the core witness (#4604's own motivating case — the
    owner's real .reyn/config/mcp.yaml had this exact shape at
    assignment time) — a stale ``type: http`` entry is caught and the
    file is named."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    myserver:\n"
            "      type: http\n      url: \"https://example.com/mcp\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "MCP transport type" in out
    assert "[reyn.yaml] mcp.servers.myserver.type: http" in out
    assert "streamable-http" in out


def test_a_reyn_yaml_finding_says_it_self_heals_with_no_reload_or_restart(
    project, capsys,
):
    """Tier 2: #4658 — a stale entry in a policy-tier file (reyn.yaml /
    reyn.local.yaml / ~/.reyn/config.yaml) is watched by
    RouterHostAdapter.maybe_refresh_mcp_tools_from_yaml's own independent
    per-turn mtime-check (measured directly, not assumed) — the fix must
    say it applies automatically on the next message, so an agent that
    just fixed the line doesn't retry-and-loop waiting for something that
    already happened."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    myserver:\n"
            "      type: http\n      url: \"https://example.com/mcp\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "reyn.yaml self-heal automatically" in out
    assert "no /reload and no restart needed" in out
    assert "does not self-heal automatically" not in out


def test_a_server_entry_in_dot_reyn_config_mcp_yaml_is_flagged_with_its_own_file_name(
    project, capsys,
):
    """Tier 2: the SAME stale value in the dynamic file — mirrors #4631's
    own placement check, one detector per source, both correctly scoped."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  servers:\n    myserver:\n      type: http\n      url: \"https://example.com/mcp\"\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "MCP transport type" in out
    assert "[.reyn/config/mcp.yaml] mcp.servers.myserver.type: http" in out
    assert "[reyn.yaml]" not in out.split("MCP transport type")[1]


def test_a_dot_reyn_mcp_yaml_finding_says_it_needs_reload_not_restart(
    project, capsys,
):
    """Tier 2: #4658 — the IN-set file is NOT covered by
    maybe_refresh_mcp_tools_from_yaml's mtime-watch (measured: that watch
    only stats the 3 policy-tier paths) — a fix there needs an explicit
    `/reload`, and the finding must say so rather than silently matching
    the policy-tier "self-heals" wording (which would be false here) or
    telling the operator to restart (which overstates what's needed)."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  servers:\n    myserver:\n      type: http\n      url: \"https://example.com/mcp\"\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "does not self-heal automatically" in out
    assert "/reload" in out
    assert "reyn mcp refresh" in out
    assert "self-heal automatically once fixed" not in out


def test_findings_in_both_kinds_of_file_get_both_kinds_of_guidance(project, capsys):
    """Tier 2: #4658 — a policy-tier finding and an IN-set finding present
    at once must each get their OWN correct guidance, not one overwriting
    or hiding the other."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n    policyserver:\n"
            "      type: http\n      url: \"https://a.example.com/mcp\"\n"
        ),
    )
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  servers:\n    dynamicserver:\n"
        "      type: http\n      url: \"https://b.example.com/mcp\"\n",
    )
    _validate()
    out = capsys.readouterr().out

    assert "reyn.yaml self-heal automatically" in out
    assert "does not self-heal automatically" in out


def test_two_renamed_servers_in_the_same_file_are_both_named(project, capsys):
    """Tier 2: multiple stale entries in one file are each named
    individually, not collapsed into a count."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  servers:\n"
            "    serverA:\n      type: http\n      url: \"https://a.example.com/mcp\"\n"
            "    serverB:\n      type: http\n      url: \"https://b.example.com/mcp\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "[reyn.yaml] mcp.servers.serverA.type: http" in out
    assert "[reyn.yaml] mcp.servers.serverB.type: http" in out


def test_a_misplaced_and_renamed_entry_is_flagged_by_both_checks(project, capsys):
    """Tier 2: an entry that is BOTH misplaced (#4631, mcp.<name> instead
    of mcp.servers.<name>) AND still says type: http (#4604) is caught
    by #4631's placement check, not this one — this check only reads
    ``mcp.servers.<name>``, so a misplaced entry is invisible to it
    (the placement check's own fix — add the missing 'servers:' key —
    is the prerequisite before this check can even see the entry)."""
    from reyn.interfaces.cli.commands.config import _validate

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + (
            "mcp:\n  myserver:\n    type: http\n    url: \"https://example.com/mcp\"\n"
        ),
    )
    _validate()
    out = capsys.readouterr().out

    assert "MCP server placement" in out
    assert "[reyn.yaml] mcp.myserver" in out
    assert "MCP transport type" not in out


def test_real_client_construction_rejects_the_renamed_value_by_name():
    """Tier 1: the OTHER half of #4604's fix — the actual config-validate
    proactive check above is a convenience; the real enforcement is
    MCPClient itself refusing to construct with the old value, naming
    the rename explicitly (not a generic "not one of {...}" the operator
    has to decode)."""
    from reyn.mcp.client import MCPClient

    with pytest.raises(ValueError, match="renamed to 'streamable-http'"):
        MCPClient({"type": "http", "url": "https://example.com/mcp"})


def test_client_construction_error_names_both_when_it_could_apply():
    """Tier 1: #4658 — MCPClient never knows which file its config dict
    came from, so unlike `reyn config validate` (which DOES know the
    source file and can give one definitive answer) it cannot say a
    single unconditional "when does this apply". It must give a safe
    answer that covers BOTH real cases instead of guessing one — an
    agent fixing this from the raw error alone (no validate run first)
    still gets a correct next step regardless of which file it edits."""
    from reyn.mcp.client import MCPClient

    with pytest.raises(ValueError) as exc_info:
        MCPClient({"type": "http", "url": "https://example.com/mcp"})

    message = str(exc_info.value)
    assert "reyn.yaml" in message
    assert "picked up automatically on your next message" in message
    assert ".reyn/config/mcp.yaml" in message
    assert "/reload" in message
