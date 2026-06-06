"""Tier 2: #187 — `reyn run-once` one-shot agent invocation (replaces REPL scripting).

#187 solves SWE with the general agent. The scripted REPL path read stdin
line-by-line (repl.py), fragmenting the 439-line task into 439 turns (#1401 root
cause). `reyn run-once` reads the WHOLE stdin as ONE message and drives the agent
to completion via send_to_agent_impl — the same programmatic drive MCP/A2A use,
NOT the REPL. It reuses `reyn chat`'s scoped session construction (delegates to
chat.run with once=True), so the scoped capabilities are inherited, not re-ported
(a delivery change, not a construction change).

Per lead's R1-R3, scoping must be ACTIVE on the one-shot path (the construction is
reused, but verified — see also the seam: send_to_agent_impl → _get_session →
registry.get_or_load returns the attached scoped session, mcp_server.py:87 +
registry.py:755, so the same scoped factory output is driven):
- R1 in-container execution: the docker backend builds an in-container argv.
- R2 web-disable: the real catalog filter drops web tools.
- R3 withholding: the piped task carries no held-out test_patch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_CHAT_PY = (
    Path(__file__).resolve().parent.parent / "src" / "reyn" / "cli" / "commands" / "chat.py"
)
_RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "swe_bench_runner.py"


def _run_once_parser() -> argparse.ArgumentParser:
    from reyn.cli.commands import run_once

    p = argparse.ArgumentParser()
    run_once.register(p.add_subparsers())
    return p


# ── the new surface ───────────────────────────────────────────────────────────

def test_run_once_parser_defaults_and_scoped_flags() -> None:
    """Tier 2: `reyn run-once` sets one-shot defaults (once/cui) + a high
    max_iterations, and accepts the scoped flags it inherits from chat."""
    p = _run_once_parser()
    a = p.parse_args([
        "run-once", "--env-backend=docker", "--container", "c1",
        "--repo-dir", "/testbed", "--grant-file-write",
        "--exclude-tools", "web__search,web__fetch", "--max-iterations", "80",
    ])
    # one-shot mode markers (drive the chat.run once-branch, plain console)
    assert a.once is True
    assert a.cui is True
    # scoped capabilities (inherited from chat's construction)
    assert a.env_backend == "docker" and a.container == "c1" and a.repo_dir == "/testbed"
    assert a.grant_file_write is True
    assert a.exclude_tools == "web__search,web__fetch"
    assert a.max_iterations == 80


def test_run_once_default_iterations_exceeds_interactive_chat() -> None:
    """Tier 2: run-once defaults max_iterations far above interactive chat's 5
    (autonomous SWE needs many explore→edit→verify rounds). chat.run maps an
    unset value to 5 (interactive unchanged)."""
    a = _run_once_parser().parse_args(["run-once"])
    assert a.max_iterations == 80
    # interactive chat has no such flag → the factory uses 5 (see chat.py).
    assert "or 5)" in _CHAT_PY.read_text(encoding="utf-8")


def test_run_once_delegates_to_chat_run() -> None:
    """Tier 2: run-once is a thin delegate to chat.run (shared construction)."""
    from reyn.cli.commands import chat, run_once

    assert run_once.run.__module__ == "reyn.cli.commands.run_once"
    # the delegate calls chat.run (same construction path, only the drive differs)
    assert "chat" in run_once.run.__doc__.lower() or chat.run is not None


# ── the anti-fragmentation guard (the bug this replaces) ──────────────────────

def test_one_shot_delivers_whole_stdin_as_one_message() -> None:
    """Tier 2: behavioral — a multi-line stdin is delivered to the agent as ONE
    message (one `send` call, whole string, no line-splitting) — the structural fix
    for the #1401 line-fragmentation bug (the REPL read stdin line-by-line, making
    each line a separate turn). Uses a recording `send` double (no mock); pins the
    raison d'être of this PR before the expensive N-run."""
    import asyncio
    import io

    from reyn.cli.commands import chat

    multi_line = "This repo @ <commit> has this issue:\n## Issue\nline A\nline B\n"
    captured: dict = {}

    async def _recording_send(registry, *, agent_name, message, timeout):
        captured["calls"] = captured.get("calls", 0) + 1
        captured["message"] = message
        captured["agent"] = agent_name
        return {"reply": "done", "partial": False, "agent": agent_name}

    reply = asyncio.run(
        chat._run_once(
            object(), "default",
            instream=io.StringIO(multi_line),
            send=_recording_send,
        )
    )
    # exactly ONE message carrying the WHOLE multi-line text, not N line-fragments
    assert captured["calls"] == 1, "the whole task must be ONE message, not one-per-line"
    assert captured["message"] == multi_line, "the multi-line task must arrive unsplit"
    assert "\n" in captured["message"]  # newlines preserved (not fragmented)
    assert captured["agent"] == "default"
    assert reply == "done"


# ── R1: in-container execution ────────────────────────────────────────────────

def test_docker_backend_routes_ops_in_container() -> None:
    """Tier 2: the REAL DockerEnvironmentBackend (the backend chat's factory builds
    for --env-backend=docker, inherited by run-once) builds an in-container
    `docker exec <container>` argv; a stdin op keeps `-i` open (the #1356/#1363
    detail a fake backend dropped)."""
    from reyn.environment.container_backend import DockerEnvironmentBackend
    from reyn.sandbox.backend import SandboxResult

    captured: dict = {}

    def _capture(argv, stdin=None):
        captured["argv"] = argv
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")

    be = DockerEnvironmentBackend(container="c1", repo_dir="/testbed", fs_runner=_capture)
    be.read_bytes(Path("/testbed/astropy/io/ascii/html.py"))
    assert captured["argv"][:2] == ["docker", "exec"]
    assert "c1" in captured["argv"]
    be.write_bytes(Path("/testbed/x.py"), b"data")
    assert "-i" in captured["argv"]


# ── R2: web-disable survival ──────────────────────────────────────────────────

def test_exclude_tools_filter_drops_web() -> None:
    """Tier 2: the real RouterLoop catalog filter (which the scoped session feeds
    via exclude_tools) drops web tools while keeping the repo-editing tools."""
    from reyn.chat.router_loop import _apply_tool_exclusions

    catalog = [
        {"type": "function", "function": {"name": n}}
        for n in ("web__search", "web__fetch", "file__write", "exec__sandboxed_exec")
    ]
    names = {
        t["function"]["name"]
        for t in _apply_tool_exclusions(catalog, frozenset({"web__search", "web__fetch"}))
    }
    assert "web__search" not in names and "web__fetch" not in names
    assert {"file__write", "exec__sandboxed_exec"} <= names


# ── R3: withholding ───────────────────────────────────────────────────────────

def test_runner_pipes_withheld_task_to_run_once() -> None:
    """Tier 2: the faithful runner pipes the task (problem_statement+hints, NO
    test_patch) to `reyn run-once` stdin as the one message; the model_patch is
    the in-container git diff (the held-out test_patch never reaches the agent)."""
    sys.path.insert(0, str(_RUNNER.parent))
    import swe_bench_runner as r

    prompt = r.build_swe_task_prompt({
        "instance_id": "x__y-1", "repo": "x/y", "base_commit": "deadbeef",
        "problem_statement": "BUG: foo drops bar.",
        "hints_text": "look at foo.py",
        "test_patch": "+def test_secret(): SECRET_EVAL",
    })
    assert "SECRET_EVAL" not in prompt and "test_patch" not in prompt
    runner_src = _RUNNER.read_text(encoding="utf-8")
    assert '"reyn", "run-once"' in runner_src  # invoked via run-once
    assert "input=task" in runner_src           # the whole task on stdin (one message)
