"""#3869: the CodeAct child harness names itself, same as reyn's own main
process (#3870).

MCP / sandboxed_exec / docker exec's own child-naming are separate PRs
(#3869's own thread: split by path so one failing doesn't block the rest).
This is one path.

Real subprocess spawn through ``CodeActRunner.run`` (``allow_unsandboxed=True``,
the same test-only escape ``test_codeact_runner_1593.py`` uses to exercise
the transport/proxy core without a real OS sandbox) — the sandbox layer
itself was already measured separately (#3869: neither Seatbelt nor
Landlock+seccomp deny the argv/PR_SET_NAME rewrite this depends on), so
this test's own job is narrower: does the harness actually CALL
``set_process_title`` on the real, running child.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner
from reyn.runtime.proctitle import format_title


@pytest.mark.asyncio
async def test_codeact_child_process_is_named_reyn_codeact() -> None:
    """Tier 2: the real, running CodeAct child process is visible via
    ``ps -o args=`` as ``reyn:codeact`` — not the interpreter's own name.

    The snippet reports its own pid back over the real tool() control
    channel (mid-execution, while the child is genuinely alive and blocked
    on the dispatch round-trip) so the test can ``ps`` it at exactly that
    moment — the harness sets its title BEFORE reading the request, so by
    the time any tool() call reaches here the rename has already happened
    on the real process."""
    observed_ps: list[str] = []

    async def dispatch(name: str, args: dict) -> dict:
        # #3869's own falsification concern: ps alone cannot tell "was
        # renamed" from "was never running" — the process must still exist
        # at read time. This callback runs WHILE the real child is blocked
        # on this exact tool() round-trip (the harness's control channel is
        # synchronous — the snippet does not resume until dispatch returns),
        # so the pid is guaranteed alive here, unlike checking ps AFTER
        # `runner.run()` returns (by then the snippet has already finished
        # and the process may already have exited — measured: it does).
        child_pid = int(args["pid"])
        ps_out = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "args="],
            capture_output=True, text=True,
        ).stdout.strip()
        observed_ps.append(ps_out)
        return {"status": "ok", "data": {}}

    runner = CodeActRunner()
    code = "import os\ntool('report_pid', pid=os.getpid())\nresult = 'done'"
    out = await runner.run(
        code=code, dispatch=dispatch, allow_unsandboxed=True,
        allowed_modules=["os"],
    )

    assert out["ok"] is True, out
    assert observed_ps, "the snippet never reported its pid — dispatch was never called"
    ps_out = observed_ps[0]
    assert ps_out == format_title("codeact"), (
        f"expected ps to show {format_title('codeact')!r}, got {ps_out!r}"
    )
    assert sys.executable not in ps_out, (
        f"the interpreter's own path is still visible in ps output: {ps_out!r}"
    )
