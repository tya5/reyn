#!/usr/bin/env python3
"""CodeAct oracle verifier — the #1618 design-review (d) bar, codified.

Runs a CodeAct task over N FRESH agents (no cross-run history — the confound gate)
and reports RATES, not a single pass (weak models are noisy):

  - fence-compliance: the model's act turn(s) emit a recognized fenced code block
    (```python / ```py / ```tool_code) — the ② SP-replace success metric.
  - clean-dispatch:   an in-code tool() call actually dispatched (a [codeact result]
    observation appears) with no ToolError / SyntaxError / MalformedResponse before it.
  - success:          the task's objective end-state holds (the expected sentinel is in
    the final reply) AND MalformedResponse count == 0.

Transient flake (a 200 / empty-choices response from the proxy) is EXCLUDED from the
rates (retry-transient, not a real failure) and reported separately.

This is the behavioural oracle for the holistic CodeAct re-design (#1618): the design
is correct when these rates hold on the holistic build WITHOUT #1617's point-patches.
Primary evidence is the per-run REYN_LLM_TRACE_DUMP (no impression-based judging).

Usage:
  PYTHONPATH=$(pwd)/src python scripts/codeact_oracle_verify.py \
      --n 8 --task "Read the file codeact_lv_test.txt and report its exact contents." \
      --sentinel PURPLE-OTTER-42 --model light --agent-prefix oracle

Requires reyn.local.yaml tool_use.chat=codeact (the scheme under test) + the sentinel
file present in cwd. Run against the build under test (PYTHONPATH=<that tree>/src).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

# Recognized CodeAct fences (mirrors codeact._FENCE_RE intent; ```json is NOT a code
# fence — it is the Gemini-native tool-call envelope leak, deliberately not counted).
_FENCE_RE = re.compile(r"```(?:python|py|tool_code)?[ \t]*\n.*?```", re.DOTALL)
_FLAKE_MARK = "empty choices"
_DISPATCH_ERRORS = ("not in catalog", "[codeact ToolError]", "[codeact SyntaxError]",
                    "[codeact MalformedResponse]")


@dataclass
class RunOutcome:
    agent: str
    flake: bool = False
    fenced: bool = False
    clean_dispatch: bool = False
    success: bool = False
    malformed: int = 0
    note: str = ""


@dataclass
class Report:
    outcomes: list[RunOutcome] = field(default_factory=list)

    @property
    def scored(self) -> list[RunOutcome]:
        return [o for o in self.outcomes if not o.flake]

    def _rate(self, attr: str) -> str:
        s = self.scored
        if not s:
            return "n/a (0 scored runs)"
        hit = sum(getattr(o, attr) for o in s)
        return f"{hit}/{len(s)} = {100*hit/len(s):.0f}%"

    def render(self) -> str:
        flakes = sum(o.flake for o in self.outcomes)
        lines = [
            "=== CodeAct oracle report ===",
            f"runs total      : {len(self.outcomes)}  (flake-excluded: {flakes})",
            f"scored runs     : {len(self.scored)}",
            f"fence-compliance: {self._rate('fenced')}",
            f"clean-dispatch  : {self._rate('clean_dispatch')}",
            f"success         : {self._rate('success')}",
            f"MalformedResp=0 : {sum(o.malformed == 0 for o in self.scored)}/{len(self.scored)}",
            "--- per run ---",
        ]
        for o in self.outcomes:
            if o.flake:
                lines.append(f"  {o.agent}: FLAKE (transient empty-choices)")
            else:
                lines.append(
                    f"  {o.agent}: fenced={o.fenced} dispatch={o.clean_dispatch} "
                    f"success={o.success} malformed={o.malformed} {o.note}"
                )
        return "\n".join(lines)


def _classify(agent: str, stdout: str, dump_path: str, sentinel: str) -> RunOutcome:
    o = RunOutcome(agent=agent)
    if _FLAKE_MARK in stdout:
        o.flake = True
        return o
    o.malformed = stdout.count("[codeact MalformedResponse]")
    # success = objective end-state: the sentinel is in the run output + no malformed.
    o.success = (sentinel in stdout) and o.malformed == 0
    # parse the LLM trace dump for fence-compliance (assistant act turns).
    assistant_turns: list[str] = []
    observations: list[str] = []
    try:
        with open(dump_path, encoding="utf-8") as f:
            seen: set[str] = set()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if "messages" not in d:
                    continue
                for m in d["messages"]:
                    c = m.get("content")
                    c = c if isinstance(c, str) else json.dumps(c)
                    if m.get("role") == "assistant" and c not in seen:
                        seen.add(c)
                        assistant_turns.append(c)
                    elif m.get("role") == "user" and c.lstrip().startswith("[codeact"):
                        observations.append(c)
    except FileNotFoundError:
        o.note = "(no dump)"
        return o
    # fence-compliance: at least one act turn emitted a recognized fenced block.
    o.fenced = any(_FENCE_RE.search(t) for t in assistant_turns)
    # clean-dispatch: a [codeact result] observation with no dispatch-error observation.
    got_result = any("[codeact result]" in ob for ob in observations)
    any_error = any(any(e in ob for e in _DISPATCH_ERRORS) for ob in observations)
    o.clean_dispatch = got_result and not any_error
    return o


def _run_one(agent: str, task: str, model: str, sentinel: str, src: str,
             timeout: float) -> RunOutcome:
    subprocess.run([sys.executable, "-m", "reyn._cli", "agent", "new", agent],
                   capture_output=True, text=True)
    dump = os.path.join(tempfile.gettempdir(), f"oracle_{agent}_dump")
    try:
        os.remove(dump)
    except OSError:
        pass
    env = dict(os.environ, REYN_LLM_TRACE_DUMP=dump, PYTHONPATH=src)
    # start_new_session=True puts the chat run in its own process GROUP so a timeout
    # kills the WHOLE group (incl. the CodeAct sandbox grandchild) — a plain
    # subprocess timeout kills only the direct child, and a grandchild holding the
    # captured stdout pipe open then hangs the post-kill drain (the bug that stalled
    # the first oracle run on a partially-fixed branch where runs loop to the cap).
    proc = subprocess.Popen(
        [sys.executable, "-m", "reyn._cli", "chat", agent, "--cui", "--model", model],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(input=task, timeout=timeout)
    except subprocess.TimeoutExpired:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, _ = proc.communicate()
        o = _classify(agent, out or "", dump, sentinel)
        o.note = (o.note + " (timeout)").strip()
        return o
    return _classify(agent, out or "", dump, sentinel)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8, help="fresh-agent runs (flake retried)")
    ap.add_argument("--task", required=True)
    ap.add_argument("--sentinel", required=True, help="objective end-state token")
    ap.add_argument("--model", default="light")
    ap.add_argument("--agent-prefix", default="oracle")
    ap.add_argument("--max-attempts", type=int, default=0,
                    help="cap total attempts incl. flakes (0 → n + n//2)")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="per-run wall cap (s); the whole process group is killed on "
                         "expiry (a looping run on a partially-fixed build hits this)")
    args = ap.parse_args()

    src = os.path.join(os.getcwd(), "src")
    max_attempts = args.max_attempts or (args.n + args.n // 2 + 2)
    report = Report()
    scored = 0
    attempt = 0
    while scored < args.n and attempt < max_attempts:
        agent = f"{args.agent_prefix}_{attempt}"
        attempt += 1
        print(f"  [{attempt}] {agent}: running…", flush=True)
        o = _run_one(agent, args.task, args.model, args.sentinel, src, args.timeout)
        report.outcomes.append(o)
        if not o.flake:
            scored += 1
        print(f"  [{attempt}] {agent}: "
              f"{'FLAKE' if o.flake else f'fenced={o.fenced} dispatch={o.clean_dispatch} success={o.success}'}"
              f"{(' ' + o.note) if o.note else ''}",
              flush=True)

    print()
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
