"""Tier 2: ADR-0006 (schema-version refuse policy, Accepted 2026-05-03) has
a live decision and a live implementation, but no test that ever pins it
(#5962). `grep -rl "SchemaVersionError\\|SNAPSHOT_VERSION" tests/`'s only
pre-existing hit, `test_snapshot_applied_seq_coerce.py`, uses
``SNAPSHOT_VERSION`` as a VALID value on every write — it never constructs
a mismatch and never touches ``SchemaVersionError`` at all.

ADR-0006's Decision, quoted verbatim:

    - `SNAPSHOT_VERSION` and `SKILL_SNAPSHOT_VERSION` constants in
      `agent_snapshot.py` and `skill_snapshot.py`.
    - `SchemaVersionError` exception, raised by `load()` when the file's
      `version` field doesn't match.
    - `reyn chat` CLI catches `SchemaVersionError` from `restore_all` and
      exits cleanly with the message (no stack trace).

(``skill_snapshot.py``'s half was deleted by #2434 and is out of scope —
see #5900's own disposition, recorded on ADR-0006 itself.)

Two separate claims, two separate tests below, per this issue's own
explicit warning: "see REFUSED, not see NO EXCEPTION" — a silently
swallowed mismatch would ALSO show "no exception propagated", so the
first test pins the raise itself (real `AgentRegistry.restore_all`, no
mocks — mirrors `test_registry_restore_all_only_names_3671_p4c1.py`'s own
on-disk-snapshot seeding technique), and the second drives the REAL
production `reyn chat` entry point (`chat.run(args)`, the same technique
`test_chat_cli_flags.py::_run_chat_once` already established) end to end,
asserting the exception that reaches the top is `SystemExit(1)` — never
`SchemaVersionError` itself (which would mean it went unhandled) and
never a silent zero exit (which would mean it was swallowed) — plus that
the printed message is the friendly one, not a Python traceback.

## `mcp.py:519-522`'s structurally identical `except SchemaVersionError:
## print(...); sys.exit(1)` — deliberately OUT OF SCOPE here

ADR-0006's own Decision paragraph (quoted above) names only the `reyn
chat` CLI. `reyn mcp serve`'s occurrence is the same defensive shape but
is not the ADR's own stated subject — a test whose job is to pin exactly
what the ADR decided should not silently expand to a target the ADR never
named. If `reyn mcp serve` needs its own witness, that is a separate,
new decision (or a fresh ADR amendment), not something this issue's own
narrow "witness the existing decision" scope covers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import SchemaVersionError
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


def _seed_version_mismatched_snapshot(tmp_path: Path, *, agent_name: str = "default") -> None:
    """Writes a real on-disk snapshot whose ``version`` field does not match
    ``SNAPSHOT_VERSION`` — bypassing ``AgentSnapshot.save()`` (which always
    writes the CURRENT version) since the whole point is a file the
    running code's OWN version does not recognise, the same "an operator
    upgraded reyn and its schema moved on" shape ADR-0006 exists for."""
    agent_dir = tmp_path / ".reyn" / "agents" / agent_name
    state_dir = agent_dir / "state"
    state_dir.mkdir(parents=True)
    AgentProfile.new(agent_name, role="").save(agent_dir)
    (state_dir / "snapshot.json").write_text(
        json.dumps({"version": 999, "applied_seq": 0}), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Claim 1 (ADR-0006 line 2): SchemaVersionError actually raises through
# restore_all — real AgentRegistry, real on-disk snapshot, no mocks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_mismatched_snapshot_raises_schema_version_error(tmp_path, monkeypatch):
    """Tier 2: a version-mismatched snapshot on disk makes `restore_all`
    raise `SchemaVersionError` — the REFUSAL itself, not merely "no crash
    occurred" (a swallowed mismatch would also show no crash). strip: remove
    the version check from `AgentSnapshot.load` (or make it accept anything)
    and this test goes red with no exception raised at all."""
    monkeypatch.chdir(tmp_path)
    _seed_version_mismatched_snapshot(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def _factory(profile: AgentProfile):
        return make_session(agent_name=profile.name, state_log=state_log)

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )

    with pytest.raises(SchemaVersionError) as exc_info:
        await registry.restore_all()

    assert "999" in str(exc_info.value), (
        "the raised error must name the actual mismatched version found on "
        "disk, not a generic message -- an operator needs this to know what "
        "they're looking at"
    )


@pytest.mark.asyncio
async def test_a_version_matched_snapshot_does_not_raise(tmp_path, monkeypatch):
    """Tier 2: accept-side control for the test above — without this, a
    change that made EVERY snapshot raise (not just mismatched ones) would
    still pass the deny-side test. Same seeding path, current
    SNAPSHOT_VERSION instead of a mismatched one."""
    from reyn.core.events.agent_snapshot import SNAPSHOT_VERSION

    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / ".reyn" / "agents" / "default"
    state_dir = agent_dir / "state"
    state_dir.mkdir(parents=True)
    AgentProfile.new("default", role="").save(agent_dir)
    (state_dir / "snapshot.json").write_text(
        json.dumps({"version": SNAPSHOT_VERSION, "applied_seq": 0}), encoding="utf-8",
    )
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def _factory(profile: AgentProfile):
        return make_session(agent_name=profile.name, state_log=state_log)

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )

    await registry.restore_all()  # must not raise


# ---------------------------------------------------------------------------
# Claim 2 (ADR-0006 line 3): the REAL `reyn chat` CLI entry point turns that
# raise into a clean, stack-trace-free exit -- not a silent pass, not an
# unhandled crash.
# ---------------------------------------------------------------------------


def _run_chat_once_against(tmp_path: Path, monkeypatch) -> None:
    """Drives the REAL production `chat.run(args)` entry point (mirrors
    `test_chat_cli_flags.py::_run_chat_once`'s established technique) against
    a project whose on-disk `default` agent snapshot has a mismatched
    version. Under the CORRECT (unstripped) behavior, the schema check
    fires during startup, before any message is ever sent — but this helper
    still wires the SAME stdin/send_to_agent_impl substitution
    `_run_chat_once` uses, so that IF the version check is ever stripped
    (in-file, for strip-verification) the run falls through to a genuine,
    observable completion instead of an unrelated stdin-capture OSError
    that would falsely look like a falsification of THIS test."""
    import io

    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: openai/test-standard-model\n",
        encoding="utf-8",
    )
    _seed_version_mismatched_snapshot(tmp_path)

    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None,
                          inbox_kind="user") -> dict:
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    from reyn.interfaces.cli.commands.chat import register as chat_register

    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    chat_register(sub)
    args = top.parse_args(["chat"])
    args.once = True  # the fast one-shot branch -- same as `reyn run-once`

    from reyn.interfaces.cli.commands import chat as chat_mod

    chat_mod.run(args)


def test_reyn_chat_exits_cleanly_with_code_1_on_schema_mismatch(tmp_path, monkeypatch, capsys):
    """Tier 2: LOAD-BEARING — ADR-0006's own decision line 3, driven through
    the real `reyn chat` entry point end to end. Asserts `SystemExit`
    SPECIFICALLY (not `SchemaVersionError` propagating unhandled, which
    would mean the catch in `chat.py` regressed) with exit code 1.

    strip: comment out the `except SchemaVersionError` branch in
    `chat.py`'s `_safe_restore` (letting it propagate) -- this test then
    fails with `SchemaVersionError` raised instead of `SystemExit`, not
    merely a different message, proving the assertion actually
    distinguishes "caught and converted" from "went unhandled"."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        _run_chat_once_against(tmp_path, monkeypatch)

    assert exc_info.value.code == 1, (
        f"a schema mismatch must exit non-zero (specifically 1, the "
        f"documented one-shot-path contract) -- got {exc_info.value.code!r}"
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Schema version mismatch" in combined, (
        "the operator must see WHY it stopped -- the friendly message from "
        "chat.py's own SchemaVersionError catch"
    )
    assert "Traceback" not in combined, (
        "ADR-0006 decision line 3 promises 'exits cleanly ... (no stack "
        "trace)' -- a Python traceback in the output means this degraded "
        "back into an ordinary unhandled-exception crash"
    )


def test_a_healthy_project_does_not_exit_on_schema_grounds(tmp_path, monkeypatch, capsys):
    """Tier 2: accept-side control -- without this, a change that made
    `reyn chat --once` ALWAYS exit 1 (regardless of any schema mismatch)
    would still pass the deny-side test above. Same real CLI path, no
    mismatched snapshot seeded; the run still needs a real message to send
    (no state to restore, so `send_to_agent_impl` IS reached this time,
    hence the same LLM-call substitution `_run_chat_once`
    (test_chat_cli_flags.py) already established)."""
    import io

    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: openai/test-standard-model\n",
        encoding="utf-8",
    )

    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None,
                          inbox_kind="user") -> dict:
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    from reyn.interfaces.cli.commands.chat import register as chat_register

    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    chat_register(sub)
    args = top.parse_args(["chat"])
    args.once = True

    from reyn.interfaces.cli.commands import chat as chat_mod

    chat_mod.run(args)  # must not raise SystemExit

    captured = capsys.readouterr()
    assert "Schema version mismatch" not in (captured.out + captured.err)
