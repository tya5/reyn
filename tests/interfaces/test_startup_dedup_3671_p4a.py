"""Tier 2: #3671 P4 item A-4 — `SessionFactoryConfig.from_config` no longer
runs twice per `reyn chat` startup for the identical `(config, project_root)`
pair (a real disk-scanning registry build — pipelines/presentations/skills —
not a cheap mapping): once for `_session_factory`'s own default-agent build
and again for `AgentRegistry`'s constructor. Now computed ONCE in `chat.py`
and captured by both consumers — a single value one owner produces, two
consumers share, with no second call site left that could independently
recompute it.

Witnessed on the REAL `reyn chat` one-shot entry point (`chat.run(args)`,
driven exactly like `test_chat_cli_flags.py`'s `_run_chat_once` — the
one-shot path shares chat.py's ENTIRE startup prologue with the interactive
path; only what happens after it differs) with a call-through spy (real
behavior still runs; only the count is observed — not a private-state read).

#3671 P4 item A also listed `_find_project_root` (3 independent walks) and
`load_project_context`. `load_project_context` was investigated and found
already single-call — no fix, no test here. The `_find_project_root` fix
was DROPPED from this PR after lead-coder's review (#3678): threading it
through optional `project_root=` kwargs (default `None`, preserving every
untouched caller) makes the duplication merely AVOIDABLE, not removed — a
caller that doesn't thread it (including a future one) still walks, so the
count silently regresses to 3 (or 4) with no test catching it structurally.
A real fix needs a single OWNER of the resolution (e.g. a cache the walk
itself can't bypass) — deferred to its own PR/issue since a process-wide
cache needs to be checked against `cwd` truly changing mid-process (tests
routinely `monkeypatch.chdir` and/or create `reyn.yaml` mid-test) before
landing, not applied reflexively.
"""
from __future__ import annotations

import argparse
import io


def _run_chat_once(tmp_path, monkeypatch):
    """Drive the REAL `reyn chat` entry point (`chat.run(args)`) through the
    fast one-shot branch — mirrors `test_chat_cli_flags.py`'s
    `_run_chat_once` exactly (same substitutions, same rationale). The
    one-shot branch shares chat.py's ENTIRE startup prologue (project_root
    computation, config load, env backend, factory_config, AgentRegistry
    construction) with the interactive path; only what happens AFTER that
    prologue differs (`_run_once` vs `run_repl`), so a duplication fixed in
    the shared prologue is witnessed identically by either branch."""
    from reyn.interfaces.cli.commands.chat import register as chat_register

    monkeypatch.chdir(tmp_path)
    # #4349: reyn ships no built-in model catalog — a minimal reyn.yaml is
    # needed for the real config-load path this helper drives (mirrors
    # test_chat_cli_flags.py's own copy of this fix).
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: openai/test-standard-model\n",
        encoding="utf-8",
    )
    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    chat_register(sub)
    args = top.parse_args(["chat"])
    args.once = True

    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None,
                          inbox_kind="user") -> dict:
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    from reyn.interfaces.cli.commands import chat as chat_mod
    chat_mod.run(args)


def _spy(monkeypatch, target_module, attr_name):
    """Wrap `target_module.attr_name` with a call-through counter; returns
    the shared mutable count dict (`{"n": ...}`)."""
    orig = getattr(target_module, attr_name)
    calls = {"n": 0}

    def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(target_module, attr_name, _wrapped)
    return calls


def test_session_factory_config_from_config_computed_once(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item A-4 — `SessionFactoryConfig.from_config`
    (a real disk-scanning registry build: pipelines/presentations/skills,
    not a cheap mapping) runs exactly ONCE per `reyn chat` startup, not once
    for `_session_factory`'s own default-agent build AND again for
    `AgentRegistry`'s constructor with the identical `(config, project_root)`
    pair."""
    from reyn.runtime.factory_config import SessionFactoryConfig

    calls = _spy(monkeypatch, SessionFactoryConfig, "from_config")

    _run_chat_once(tmp_path, monkeypatch)

    assert calls["n"] == 1, (
        f"SessionFactoryConfig.from_config was called {calls['n']} time(s) — "
        "expected exactly 1 (computed once, reused by both _session_factory "
        "and AgentRegistry)"
    )
