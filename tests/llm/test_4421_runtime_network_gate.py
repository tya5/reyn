"""Tier 2: #4421 step ③ — one full router turn, under reyn's DEFAULT config,
reaches zero real sockets.

#4428 closed literal ``import litellm`` outside the seam via a STATIC (AST)
gate — blind by construction to a ``sys.modules``/``getattr`` reach with no
import statement for it to see. This is the second, RUNTIME sheet architect
named as still required: a mechanism gate over the OUTCOME (a real socket
opened) rather than the syntax that could produce it, so it covers every
spelling that reaches litellm, not just the ones #4428 can parse.

Real subprocess (not an in-process patch): a pytest worker may already have
``litellm`` in ``sys.modules`` from an earlier test, which would make
re-importing it here a silent no-op and prove nothing about import-time
network reach. Same shape ``tests/llm/test_litellm_lazy_load.py`` already
established for this exact class of "process-level fact" test.

Real router loop, no live provider: the LLM's own completion is served by a
hand-authored ``litellm.ModelResponse`` (a
``reyn.dev.testing.replay.LLMReplay`` subclass with key-matching bypassed —
no genuine recorded fixture exists for this scenario and no API credits are
spent recording one) — everything downstream of that one call (router loop,
tool dispatch, permission gate) is real, unmocked reyn code. ``permissions.
file.write: deny`` makes the one scripted ``write_file`` call resolve
without any interactive JIT prompt, so the whole turn completes in one
round trip suitable for a non-interactive subprocess.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run(
    src_root: str, script: str, cwd: Path, *, unset_env: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Every embedded script below is written flush-left (column 0) —
    deliberately, so nothing here needs to reconcile different fragments'
    indentation (``textwrap.dedent`` over a concatenation of differently
    indented triple-quoted strings is fragile; an earlier draft of this
    file hit exactly that as a real ``IndentationError``, not a hypothetical).

    ``unset_env``: this PARENT pytest process has almost certainly already
    imported ``reyn`` itself (an earlier test, or pytest's own collection),
    which sets ``LITELLM_LOCAL_MODEL_COST_MAP`` in the parent's own
    ``os.environ`` — inherited by every child by default. The falsify test
    needs a child that genuinely lacks it (matching a real cold process
    that hasn't run reyn's bootstrap yet), so it strips it explicitly
    rather than relying on child-process import order alone, which the
    inherited env var would silently defeat."""
    env = {k: v for k, v in os.environ.items() if k not in unset_env}
    env["PYTHONPATH"] = src_root
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(cwd),
    )


# Flush-left on purpose (see `_run`'s docstring).
_SCRIPTED_TURN_SETUP = """
import itertools
import sys
from pathlib import Path

import litellm
from reyn.dev.testing.replay import LLMReplay

_TOOL_CALL_RESPONSE = {
    "id": "gen-1", "created": 1700000000, "model": "fake/4421-gate",
    "object": "chat.completion", "system_fingerprint": None,
    "choices": [{
        "finish_reason": "tool_calls", "index": 0,
        "message": {
            "content": None, "role": "assistant",
            "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path": "/etc/reyn_4421_gate.txt", "content": "x"}',
                },
            }],
            "function_call": None,
        },
    }],
    "usage": {"completion_tokens": 8, "prompt_tokens": 20, "total_tokens": 28,
              "completion_tokens_details": None, "prompt_tokens_details": None},
}


class _ScriptedReplay(LLMReplay):
    # No genuine fixture exists for this scenario (see module docstring) —
    # _replay is fully overridden to always serve the same canned tool
    # call, regardless of what was actually asked.
    def __init__(self):
        super().__init__(Path("/dev/null"), mode="replay")
        self._plan_iter = itertools.repeat(_TOOL_CALL_RESPONSE)

    def _replay(self, key, model, messages, observed, request):
        return litellm.ModelResponse(**next(self._plan_iter))


def run_one_turn():
    from reyn.interfaces.cli import main

    replay = _ScriptedReplay()
    replay.install()
    try:
        sys.argv = ["reyn", "run-once", "--model", "fake/4421-gate"]
        try:
            main()
        except SystemExit:
            pass
    finally:
        replay.restore()
"""


@pytest.fixture
def _default_project(tmp_path: Path) -> Path:
    """A project whose ``reyn.yaml`` carries only what makes the scripted
    turn resolve without an interactive prompt (``permissions.file.write:
    deny`` denies the one scripted ``write_file`` call outright) — no
    tiktoken-cache / cost-map tampering, so the probe measures reyn's real
    DEFAULT behaviour, not an artificially narrowed one."""
    (tmp_path / "reyn.yaml").write_text(
        "llm:\n  models:\n    standard: fake/4421-gate\n"
        "permissions:\n  file:\n    write: deny\n",
        encoding="utf-8",
    )
    return tmp_path


def test_one_turn_under_default_config_reaches_zero_real_sockets(
    _default_project: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: import reyn (production bootstrap order) -> import litellm ->
    drive one full router turn (scripted completion, real everything else)
    -> assert zero real ``socket.connect``/``connect_ex`` attempts.

    Decisive, not just descriptive: a failure here means litellm (or reyn's
    own call into it) reached a real socket during a turn reyn's contract
    says stays fully local — caught via
    :class:`reyn.dev.testing.runtime_network_probe.NetworkReachProbe`, which
    raises naming reyn's own known suppression points (architect's landing
    condition: the message must say what REYN should do, never just that a
    network reach happened)."""
    script = (
        "import sys\n"
        "sys.stdin = open('/dev/null')\n"
        # `reyn` FIRST — production ordering. `reyn/__init__.py` sets
        # LITELLM_LOCAL_MODEL_COST_MAP=True (setdefault) before litellm is
        # ever imported; importing litellm first would skip that and
        # produce a FALSE positive (litellm's own remote cost-map fetch)
        # rather than a real reyn gap — measured directly (2026-08-13): 8
        # real connect attempts to raw.githubusercontent.com when this
        # ordering is skipped.
        "import reyn\n"
        "from reyn.dev.testing.runtime_network_probe import NetworkReachProbe\n"
        + _SCRIPTED_TURN_SETUP
        + "\n"
        "with NetworkReachProbe() as probe:\n"
        "    run_one_turn()\n"
        "\n"
        "probe.assert_clean(context='one router turn, default config')\n"
        "print('OK')\n"
    )
    result = _run(out_of_process_reyn, script, cwd=_default_project)
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_skipping_reyns_bootstrap_order_really_does_reach_the_network(
    _default_project: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: falsify-verify half 1/2 — importing litellm BEFORE reyn
    (skipping ``reyn/__init__.py``'s ``LITELLM_LOCAL_MODEL_COST_MAP``
    ``setdefault`` on purpose, the exact ordering mistake this gate exists
    to catch) really does reach a real socket. Uses a RAW inline socket
    patch here, not :class:`NetworkReachProbe` — that class lives inside
    the ``reyn`` package, so importing IT would import ``reyn`` first and
    silently defeat the "litellm before reyn" ordering this test exists to
    falsify against (measured: an earlier draft of this file did exactly
    that and the probe never fired)."""
    script = """
import socket
attempts = []
def _rec(self, addr):
    attempts.append(addr)
    raise OSError("blocked")
socket.socket.connect = _rec
socket.socket.connect_ex = _rec

import litellm  # BEFORE reyn — no LITELLM_LOCAL_MODEL_COST_MAP set yet
import reyn  # noqa: F401 — too late; the fetch already happened above

assert attempts, "expected import litellm (with no reyn bootstrap yet) to reach the network"
print("REACHED", len(attempts))
"""
    result = _run(
        out_of_process_reyn, script, cwd=_default_project,
        unset_env=("LITELLM_LOCAL_MODEL_COST_MAP",),
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "REACHED" in result.stdout, (
        f"litellm-before-reyn must actually reach the network, or this whole "
        f"gate is unfalsifiable: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_assert_clean_names_reyns_own_remedy() -> None:
    """Tier 1: falsify-verify half 2/2 — :meth:`NetworkReachProbe.
    assert_clean`'s failure message names reyn's own known suppression
    points (architect's landing condition, #4421), not just "a network
    call happened". In-process (no subprocess needed): this checks the
    MESSAGE CONTRACT of a plain method, not process-level import-order
    facts — the other half of this pair checks the fact, this half checks
    the contract, and neither needs the other's setup."""
    from reyn.dev.testing.runtime_network_probe import (
        NetworkReachProbe,
        RealNetworkReachDuringOneTurn,
    )

    probe = NetworkReachProbe()
    probe.attempts.append(("connect", ("185.199.108.133", 443)))

    with pytest.raises(RealNetworkReachDuringOneTurn) as excinfo:
        probe.assert_clean(context="test context")

    msg = str(excinfo.value)
    assert "test context" in msg
    assert "remedy" in msg.lower(), (
        f"message must name reyn's own remedy, not just report the reach: {msg}"
    )
    assert "LITELLM_LOCAL_MODEL_COST_MAP" in msg, (
        f"message must name the specific known suppression point: {msg}"
    )
