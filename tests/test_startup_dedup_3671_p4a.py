"""Tier 2: #3671 P4 item A — `reyn chat` startup no longer redundantly
recomputes the SAME value multiple times (structural duplication, not a
performance fix — each individual duplicate call was cheap; the point is
that "compute once, reuse" is now actually true, so the NEXT thing added to
startup doesn't inherit a 2nd/3rd copy of the same read by following the
existing pattern).

Three independent duplications, all witnessed on the REAL `reyn chat`
one-shot entry point (`chat.run(args)`, driven exactly like
`test_chat_cli_flags.py`'s `_run_chat_once` — the one-shot path shares the
startup prologue with the interactive path; only the branch AFTER it
differs) with call-through spies (real behavior still runs; only the count
is observed — not a private-state read):

1. `_find_project_root` used to run 3 times during startup (`chat.py`'s own
   compute, then AGAIN inside `load_config()`, then AGAIN inside
   `build_environment_backend()`) — all 3 for the SAME `Path.cwd()`. Now the
   one value `chat.py` computes first is threaded through the other two via
   new optional `project_root=` params (default `None`, preserving every
   OTHER caller's behavior byte-identically — `load_config()`/
   `build_environment_backend()` are shared across every CLI surface, most
   of which never had this duplication to begin with).
2. `SessionFactoryConfig.from_config` — a real disk-scanning registry build
   (pipelines/presentations/skills), not a cheap mapping — used to run once
   for `_session_factory`'s own first (default-agent) build and AGAIN for
   `AgentRegistry`'s constructor, with the exact same `(config, project_root)`
   pair both times. Now computed once and captured by both.

Not covered here: `load_project_context` (P4 dispatch item A #2) was
investigated and found to already be a single call on the `reyn chat`
path — no fix needed, so no regression test either (nothing to pin).
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


def test_load_config_no_longer_calls_find_project_root_itself(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item A #3, site 2 — `load_config()`'s OWN internal
    `_find_project_root(cwd)` call (its module-global name, resolved inside
    `reyn.config.loader` regardless of how a caller imported the function)
    must NOT fire when the caller (chat.py, via `InvocationContext.from_args`)
    already threaded a `project_root` through."""
    import reyn.config.loader as loader_mod

    calls = _spy(monkeypatch, loader_mod, "_find_project_root")

    _run_chat_once(tmp_path, monkeypatch)

    assert calls["n"] == 0, (
        f"load_config() called its own _find_project_root {calls['n']} time(s) "
        "despite chat.py passing project_root= — the walk should be skipped entirely"
    )


def test_find_project_root_runs_exactly_once_across_startup(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item A #3, sites 1+3 — patched at `reyn.config`, the
    package-level name BOTH `chat.py`'s own initial computation (line ~431)
    AND `env_backend.py`'s (locally-imported, so dynamically resolved here
    each call) `_find_project_root` calls go through. Before the fix this was
    3 (chat.py once, `load_config()` once — covered by the test above via
    its own module global — and `build_environment_backend()` once); after
    the fix, only chat.py's own single computation remains — a regression in
    EITHER site's threading (chat.py calling twice, or env_backend calling
    despite `project_root` being passed) would push this above 1."""
    import reyn.config as config_pkg

    calls = _spy(monkeypatch, config_pkg, "_find_project_root")

    _run_chat_once(tmp_path, monkeypatch)

    assert calls["n"] == 1, (
        f"expected exactly 1 call (chat.py's own initial computation), got {calls['n']}"
    )


def test_session_factory_config_from_config_computed_once(tmp_path, monkeypatch):
    """Tier 2: #3671 P4 item A #4 — `SessionFactoryConfig.from_config`
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


def test_load_config_project_root_param_skips_the_internal_walk(tmp_path):
    """Tier 2: unit-level confirmation of the mechanism `load_config()`
    itself now offers — passing a pre-computed `project_root` is honored
    (the merged config still resolves `reyn.yaml` from it) without
    `load_config` needing to independently re-derive it from `cwd`."""
    from reyn.config.loader import load_config

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "reyn.yaml").write_text("model: strong\n", encoding="utf-8")

    config = load_config(cwd=project_root, project_root=project_root)
    assert config.model == "strong"


def test_build_environment_backend_project_root_param_is_honored(tmp_path):
    """Tier 2: unit-level confirmation for `build_environment_backend`'s
    new `project_root=` param — the returned workspace base dir is the
    PASSED value, not a freshly re-derived one (proving the param actually
    short-circuits the internal `_find_project_root` call, not just accepts
    and ignores it)."""
    from reyn.interfaces.cli.env_backend import build_environment_backend

    args = argparse.Namespace(env_backend="host")
    passed_root = tmp_path / "explicit-root"
    passed_root.mkdir()

    _backend, ws_base_dir, ws_state_dir, _cleanup = build_environment_backend(
        args, project_root=passed_root
    )

    assert ws_base_dir == passed_root
    assert ws_state_dir == passed_root / ".reyn"
