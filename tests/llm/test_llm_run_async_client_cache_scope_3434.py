"""Tier 2b: `run_async`'s client-close step is scoped to clients created
during its own call — not litellm's entire process-wide async-client cache
(#3434).
"""
import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from reyn.llm.llm import run_async


def test_run_async_does_not_close_a_client_created_outside_its_own_call() -> None:
    """Tier 2b: an unrelated, LLM-free `run_async` call must not close a
    litellm async client another (already-completed) call created and left
    open.

    Regression guard for #3434: litellm's own `close_litellm_async_clients()`
    iterates its ENTIRE process-wide client cache, unconditionally — not
    just entries the calling `run_async` invocation created. `LLMClientCache`
    never evicts on close (its own docstring: an in-flight request may still
    hold the client), so under `pytest-xdist` any later LLM-free `run_async`
    call (e.g. an mcp.py CLI command test) in the same worker process closed
    every OTHER test's still-open, still-cached litellm client — including
    ones for providers that test never touched. A real (unmocked) network
    LLM call running afterward in that worker then failed with "Cannot send
    a request, as the client has been closed", with the specific failing
    test varying run to run because it depends on xdist worker assignment
    and intra-worker test order, not on any one test's own defect.

    Uses litellm's real async-client cache + real aiohttp objects (no
    mocks — `unittest.mock` is forbidden by testing policy). Asserts on the
    client session's own public `closed` attribute, not any reyn-private
    state.
    """
    async def _create_client_outside_run_async():
        # Same call litellm's own vertex_ai/gemini `make_call` makes
        # internally (`get_async_httpx_client(llm_provider=VERTEX_AI)`,
        # no params) — the exact cache key a real streaming LLM call hits.
        from litellm.llms.custom_httpx.http_handler import get_async_httpx_client

        handler = get_async_httpx_client(llm_provider="vertex_ai")
        return handler.client._transport._get_valid_client_session()

    # Simulates a prior, already-finished test/call that created a real
    # litellm async client on its OWN event loop (not through `run_async`)
    # and left it open — exactly what a real (unmocked) LLM call does.
    session = asyncio.run(_create_client_outside_run_async())
    assert session.closed is False

    async def _unrelated_llm_free_work() -> str:
        return "done"

    # Simulates a later, totally unrelated `run_async` call in the same
    # worker process (e.g. an LLM-free mcp.py CLI command).
    assert run_async(_unrelated_llm_free_work()) == "done"

    assert session.closed is False, (
        "run_async closed a litellm async client it did not create — "
        "this is the #3434 shared-cache poisoning bug"
    )


def test_run_async_never_imports_litellm_without_an_llm_call(out_of_process_reyn) -> None:
    """Tier 2b: #3671 — the required gate. `run_async` given an LLM-free
    coroutine must leave `"litellm" not in sys.modules` after it returns.

    This is the ONE witness that actually pins the startup-latency win: the
    other tests in this file (and #3434's own regression guard above) stay
    green whether litellm is imported eagerly or lazily — they test SCOPING
    of the close step, not WHETHER an import happened at all. Someone could
    silently reinstate an eager `import litellm` at `run_async`'s own top
    (the exact #3671 defect this arc fixed) and every other test would stay
    green; only this one would turn red that day.

    Subprocess, not in-process (same reasoning as `test_litellm_lazy_load
    .py`'s own tests): almost every OTHER test in this suite imports litellm
    somewhere, so `sys.modules` is contaminated by test order in-process —
    only a fresh interpreter answers "did THIS call import it" honestly.
    """
    script = """
        import sys
        from reyn.llm.llm import run_async

        assert "litellm" not in sys.modules, (
            "litellm was already imported before run_async even ran — "
            "broken test setup, not what this test means to check"
        )

        async def _llm_free() -> str:
            return "done"

        assert run_async(_llm_free()) == "done"
        assert "litellm" not in sys.modules, sorted(
            m for m in sys.modules if "litellm" in m
        )
        print("OK")
        """
    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_constructing_a_session_never_imports_litellm(tmp_path, out_of_process_reyn) -> None:
    """Tier 2b: #3671 follow-up — widens the gate above, which the owner's
    real-machine re-measurement caught as too narrow (issue #3671:
    ``tui-boot`` 2.27s -> 6.49s after #3780 landed).

    The gate above drives `run_async` with a synthetic `_llm_free()`
    coroutine that never constructs a `Session` — so it could not see
    litellm being imported at SESSION CONSTRUCTION time (`Session.__init__`
    unconditionally builds a `TurnBudgetEngine`, which resolves
    `get_max_input_tokens` against litellm's model catalog — see
    `services/turn_budget/engine.py`). A real interactive TUI session always
    constructs a `Session` during startup (`chat.py`'s `_background_attach`
    -> `registry.attach` -> the session factory), so this — not the
    synthetic no-op — is the shape #3671's win actually needs to hold for.

    Real `Session` (via `tests._support.agent_session.make_session`, the
    same helper 196+ other test files use — no mocks), constructed but
    never run (`.run()` is a separate, later step; this isolates
    CONSTRUCTION specifically). Subprocess, same reasoning as the gate
    above: `sys.modules` must be virgin for the assertion to mean anything.
    """
    script = f"""
        import sys
        sys.path.insert(0, {str(Path(out_of_process_reyn).parent)!r})
        from pathlib import Path
        from tests._support.agent_session import make_session

        assert "litellm" not in sys.modules, (
            "litellm was already imported before construction even ran — "
            "broken test setup, not what this test means to check"
        )

        make_session(
            agent_name="probe",
            agent_role="test",
            output_language="en",
            model="claude-sonnet",
            snapshot_path=Path({str(tmp_path)!r}) / "snap.json",
        )
        assert "litellm" not in sys.modules, sorted(
            m for m in sys.modules if "litellm" in m
        )
        print("OK")
        """
    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
