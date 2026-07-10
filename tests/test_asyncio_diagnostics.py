"""Tier 2b: an unhandled exception in a fire-and-forget asyncio task is
durably captured as an `asyncio_unhandled_exception` P6 event, without
losing the event loop's own default exception handling.

Regression context: reyn installed no `asyncio.set_exception_handler`
anywhere, so a background `asyncio.create_task(...)` whose result nobody
awaits/checks would raise into Python's own
`asyncio.BaseEventLoop.default_exception_handler` (stderr/logging only) and
be gone — the exact "Unhandled exception in event loop" / "exception: None"
class an operator cannot investigate after the fact. This test exercises the
real `install_asyncio_exception_handler` + `emit_cli_event` path (no mocks)
end to end: schedule a raising task on a real loop, let the loop process it,
and assert the event landed durably under `.reyn/events/`.

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real event loop, real filesystem (pytest tmp_path), real EventStore reader.
- Test docstring first line declares Tier.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import reyn.interfaces.inline.app as inline_app_mod
import reyn.interfaces.repl.repl as repl_mod
from reyn.core.events.asyncio_diagnostics import install_asyncio_exception_handler


def _read_events_of_kind(events_dir: Path, kind: str) -> list[dict]:
    """Read every JSONL event of *kind* from anywhere under *events_dir*."""
    found: list[dict] = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


def test_unhandled_task_exception_is_durably_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: a raising fire-and-forget task's exception survives as a P6 event.

    Steps:
    1. Real ``.reyn/`` dir under tmp_path (the durable-emit anchor
       ``emit_cli_event`` walks up from cwd to find).
    2. Real event loop; install the handler; schedule a task that raises
       with NO one awaiting/checking its result (the fire-and-forget class).
    3. Yield control (``asyncio.sleep(0)`` twice) so the loop's own
       call_exception_handler path actually fires for the failed task.
    4. Read back ``.reyn/events/**/*.jsonl`` and assert one
       ``asyncio_unhandled_exception`` event exists with the right fields.
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    async def _boom() -> None:
        raise ValueError("kaboom-from-background-task")

    async def _drive() -> None:
        install_asyncio_exception_handler(asyncio.get_running_loop())
        asyncio.create_task(_boom())  # fire-and-forget: nobody awaits this
        # Give the loop two ticks: one to run _boom() to its raise, one more
        # for the loop to notice the task's exception was never retrieved
        # and invoke the exception handler.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    [event] = events  # exactly one event captured — unpack raises otherwise
    data = event["data"]
    assert data["exception_type"] == "ValueError"
    assert "kaboom-from-background-task" in data["exception_message"]
    assert "ValueError: kaboom-from-background-task" in data["traceback"]
    assert "Task exception was never retrieved" in data["context_message"]


def test_default_handler_still_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2b: installing our handler does not regress existing stderr/log visibility.

    asyncio's own default_exception_handler logs the failure via the
    ``asyncio`` logger at ERROR level. This asserts that log record still
    fires (our handler wraps, never replaces, the default one).
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    async def _boom() -> None:
        raise RuntimeError("still-visible-in-logs")

    async def _drive() -> None:
        install_asyncio_exception_handler(asyncio.get_running_loop())
        asyncio.create_task(_boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio.run(_drive())

    assert any(
        "still-visible-in-logs" in str(record.message)
        or (record.exc_info and "still-visible-in-logs" in str(record.exc_info[1]))
        for record in caplog.records
    )


def test_durable_capture_survives_prompt_toolkit_prompt_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: durable capture is not masked while a prompt_toolkit
    Application (the REPL's prompt-wait, most of its wall-clock time) owns
    the loop's asyncio exception handler.

    Regression context (#2786): `Application.run_async` defaults to
    `set_exception_handler=True`, which swaps the loop's exception handler
    for its own for the whole call -- masking #2637's durable capture
    installed by `install_asyncio_exception_handler`. `interfaces/repl/
    repl.py`'s `prompt_session.prompt_async(...)` (and `interfaces/inline/
    app.py`'s `Application.run_async(...)`) now both pass
    `set_exception_handler=False` so reyn's handler stays wired. This drives
    the exact call shape `repl.py` uses -- a real `PromptSession.prompt_async`
    with `set_exception_handler=False` on a headless pipe input/DummyOutput
    (prompt_toolkit's own sanctioned no-TTY test harness) -- and fires a
    message-only `call_exception_handler` (no `exception` key: the
    "Exception None" class this module's docstring describes) while the
    prompt is still awaiting input, then asserts the event landed durably.
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    async def _drive() -> str:
        install_asyncio_exception_handler(asyncio.get_running_loop())
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=DummyOutput()):
                session: PromptSession[str] = PromptSession()

                async def _fire_then_type() -> None:
                    # Let prompt_async actually start (and install its own
                    # loop bindings) before firing, so this reproduces the
                    # "exception arrives during the prompt wait" window.
                    await asyncio.sleep(0)
                    asyncio.get_running_loop().call_exception_handler(
                        {"message": "message-only-context-during-prompt-wait"}
                    )
                    await asyncio.sleep(0)
                    pipe_input.send_text("hello\r")

                asyncio.create_task(_fire_then_type())
                # The exact parameter repl.py's `_input_loop` now passes.
                return await session.prompt_async(set_exception_handler=False)

    result = asyncio.run(_drive())
    assert result == "hello"  # the prompt itself still completed normally

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    [event] = events  # exactly one event captured — unpack raises otherwise
    assert (
        event["data"]["context_message"]
        == "message-only-context-during-prompt-wait"
    )


def _call_passes_set_exception_handler_false(source: str, method_name: str) -> bool:
    """Whether *source* contains a ``<...>.<method_name>(...)`` call that
    passes ``set_exception_handler=False`` as a keyword.

    AST-level (not a substring grep): tolerant of formatting/whitespace and
    scoped to the actual call, not a comment mentioning the param.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == method_name):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "set_exception_handler"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
            ):
                return True
    return False


def test_repl_prompt_call_sites_disable_prompt_toolkit_exception_handler() -> None:
    """Tier 2b: both REPL prompt_toolkit entry points keep reyn's asyncio
    exception handler wired by passing ``set_exception_handler=False``.

    The durable-capture test above hardcodes the parameter in its own driver,
    so it would stay green even if a future edit dropped the argument from the
    production call sites -- i.e. it verifies the mechanism, not the wiring.
    This pins the wiring itself: if ``interfaces/repl/repl.py``'s
    ``prompt_session.prompt_async(...)`` (the ``--cui`` / non-TTY path) or
    ``interfaces/inline/app.py``'s ``app.run_async(...)`` (the default
    interactive ``reyn chat`` path) loses the argument, prompt_toolkit's
    default (``True``) silently re-masks #2637's capture -- exactly the #2786
    regression -- and this goes RED. Reads the real module source (AST), no
    mocks.
    """
    repl_src = Path(repl_mod.__file__).read_text()
    inline_src = Path(inline_app_mod.__file__).read_text()

    assert _call_passes_set_exception_handler_false(repl_src, "prompt_async"), (
        "repl.py's prompt_session.prompt_async(...) must pass "
        "set_exception_handler=False (else prompt_toolkit re-masks #2637 capture)"
    )
    assert _call_passes_set_exception_handler_false(inline_src, "run_async"), (
        "inline/app.py's app.run_async(...) must pass "
        "set_exception_handler=False (else prompt_toolkit re-masks #2637 capture)"
    )
