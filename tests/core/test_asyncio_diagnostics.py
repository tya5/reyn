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
import sys
from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import reyn.core.events.asyncio_diagnostics as asyncio_diagnostics_mod
import reyn.interfaces.repl.stream_client as stream_client_mod
from reyn.core.events.asyncio_diagnostics import (
    _durably_capture,
    install_asyncio_exception_handler,
)


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
    installed by `install_asyncio_exception_handler`. The REPL's
    `prompt_session.prompt_async(...)` now passes
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
    """Tier 2b: the REPL prompt_toolkit entry point keeps reyn's asyncio
    exception handler wired by passing ``set_exception_handler=False``.

    The durable-capture test above hardcodes the parameter in its own driver,
    so it would stay green even if a future edit dropped the argument from the
    production call site -- i.e. it verifies the mechanism, not the wiring.
    This pins the wiring itself: if ``interfaces/repl/stream_client.py``'s
    ``prompt_session.prompt_async(...)`` (the ``--cui`` / non-TTY path) loses
    the argument, prompt_toolkit's default (``True``) silently re-masks #2637's
    capture -- exactly the #2786 regression -- and this goes RED. Reads the real
    module source (AST), no mocks.
    """
    stream_client_src = Path(stream_client_mod.__file__).read_text()

    assert _call_passes_set_exception_handler_false(stream_client_src, "prompt_async"), (
        "stream_client.py's prompt_session.prompt_async(...) must pass "
        "set_exception_handler=False (else prompt_toolkit re-masks #2637 capture)"
    )


def _simulate_shutdown_import_failure(module_dotted_name: str):
    """A context manager-free helper pair reproducing the REAL production
    failure (#5951's own traceback) precisely: evicts *module_dotted_name*
    from ``sys.modules`` (forcing the NEXT ``import`` of it to genuinely
    consult ``sys.meta_path`` instead of hitting the cache -- Python's
    import statement checks ``sys.modules`` FIRST, so merely nulling
    ``meta_path`` without this eviction would silently no-op if the module
    happens to already be cached from an earlier import elsewhere in the
    test session, the same technique
    ``test_5059_core_dep_declarations.py``'s own ``_block_httpx_import``
    fixture uses), then sets ``sys.meta_path = None`` -- CPython's own
    interpreter-shutdown signal, the exact condition
    ``importlib._bootstrap._find_spec`` checks to raise ``ImportError:
    sys.meta_path is None, Python is likely shutting down``. Returns the
    saved state for the caller to restore."""
    saved_modules = {
        k: v for k, v in sys.modules.items()
        if k == module_dotted_name or k.startswith(module_dotted_name + ".")
    }
    for k in saved_modules:
        del sys.modules[k]
    saved_meta_path = sys.meta_path
    sys.meta_path = None  # type: ignore[assignment]
    return saved_modules, saved_meta_path


def _restore_after_simulated_shutdown(saved_modules: dict, saved_meta_path) -> None:
    sys.meta_path = saved_meta_path
    sys.modules.update(saved_modules)


@pytest.fixture(autouse=True)
def _reset_asyncio_diagnostics_cache():
    """Every test in this module starts from -- and leaves -- an
    UN-cached ``_emit_cli_event`` (#5951's own module-level cache):
    without this, whichever test happens to run first would silently warm
    it for every test after, hiding the exact "never resolved before
    shutdown" scenario the tests below exist to drive."""
    asyncio_diagnostics_mod._emit_cli_event = None
    try:
        yield
    finally:
        asyncio_diagnostics_mod._emit_cli_event = None


def test_shutdown_time_call_reuses_the_cache_and_still_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: #5951's actual fix, driven end to end. A process that
    resolved ``emit_cli_event`` once (a normal, pre-shutdown call) durably
    records a LATER call made during simulated interpreter shutdown
    (``sys.meta_path = None``, the exact production condition) -- it
    reuses the cached reference and never touches ``sys.meta_path`` again.
    Checks a RECORDED EVENT (lead-coder's own review note: "no exception"
    alone is also true of a silent swallow, which is the FAILURE mode,
    not the fix) -- not merely the absence of a raise.

    Strip: reverting ``_durably_capture`` to its pre-#5951 shape (a fresh,
    unprotected ``from reyn.core.events.events import emit_cli_event``
    inside the function body, every call) makes this go RED -- the SECOND
    call's fresh import genuinely consults the nulled ``sys.meta_path``
    (the module was evicted from ``sys.modules`` first, see
    ``_simulate_shutdown_import_failure``'s own docstring) and raises
    ``ImportError`` uncaught, exactly reproducing #5951's own traceback.
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    _durably_capture({"message": "warm-up-before-shutdown"})

    saved_modules, saved_meta_path = _simulate_shutdown_import_failure(
        "reyn.core.events.events"
    )
    try:
        _durably_capture({"message": "during-simulated-shutdown"})
    finally:
        _restore_after_simulated_shutdown(saved_modules, saved_meta_path)

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    messages = [e["data"]["context_message"] for e in events]
    assert "warm-up-before-shutdown" in messages, (
        "the normal, pre-shutdown call must still record -- the fix must "
        "not have fallen to the 'give up quietly' side for the ordinary case"
    )
    assert "during-simulated-shutdown" in messages, (
        "the call made during simulated shutdown must ALSO record -- this "
        "is #5951's actual claim: caching means shutdown no longer loses "
        "the event"
    )


def test_never_resolved_shutdown_call_gives_up_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: #5951's other acceptance branch (architect: "黙って諦める")
    -- a process whose VERY FIRST call to the durable-capture path happens
    during simulated shutdown (nothing cached yet) raises no exception.
    This is the genuinely-unrecoverable case (there is no earlier success
    to have cached) -- the assertion is that it fails SILENTLY, not that
    it still somehow records (it structurally cannot, by construction: the
    import itself is what failed).
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    # Never warmed -- the autouse `_reset_asyncio_diagnostics_cache` fixture
    # (this module) resets the cache before every test; this test relies on
    # that reset, not a direct read of the private attribute itself.

    saved_modules, saved_meta_path = _simulate_shutdown_import_failure(
        "reyn.core.events.events"
    )
    try:
        _durably_capture({"message": "first-ever-call-during-shutdown"})
    finally:
        _restore_after_simulated_shutdown(saved_modules, saved_meta_path)

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    assert events == [], (
        "a call that never resolved emit_cli_event before simulated "
        "shutdown cannot genuinely record -- any event here would mean "
        "this test's own simulation did not actually reproduce the "
        "failure it claims to"
    )


def test_install_warms_the_cache_so_a_first_ever_shutdown_only_exception_still_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2b: #5952 BLOCKING (lead-coder, measured) -- the exact residual
    gap the earlier fix left open: ``_resolve_emit_cli_event`` was only
    ever reachable from INSIDE the handler, so a process whose
    FIRST-EVER unhandled exception is a genuinely shutdown-only one
    (``Task was destroyed but it is pending!`` -- #5951's own reported
    symptom, fired from asyncio's OWN teardown machinery, never from
    ordinary running code) still lost it: nothing earlier had warmed the
    cache, so that first (and only) call resolved for the first time
    AFTER simulated shutdown and gave up quietly.

    Drives the real, unmodified sequence a live entrypoint follows:
    ``install_asyncio_exception_handler(loop)`` (now resolves
    ``emit_cli_event`` at install time, per the fix) THEN -- with nothing
    else having fired the handler in between, matching "first-ever
    exception" exactly -- a call landing during simulated shutdown.

    Strip: remove the ``_resolve_emit_cli_event()`` call this fix added to
    ``install_asyncio_exception_handler`` -- this goes red (no event
    recorded), reproducing #5952's own report exactly.
    """
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    loop = asyncio.new_event_loop()
    try:
        install_asyncio_exception_handler(loop)  # warms the cache, per the fix
    finally:
        loop.close()

    saved_modules, saved_meta_path = _simulate_shutdown_import_failure(
        "reyn.core.events.events"
    )
    try:
        _durably_capture({"message": "first-ever-exception-and-its-shutdown-only"})
    finally:
        _restore_after_simulated_shutdown(saved_modules, saved_meta_path)

    events = _read_events_of_kind(reyn_dir / "events", "asyncio_unhandled_exception")
    messages = [e["data"]["context_message"] for e in events]
    assert "first-ever-exception-and-its-shutdown-only" in messages, (
        "installing the handler must warm the emit_cli_event cache -- a "
        "process whose only-ever unhandled exception is a shutdown-only "
        "one must still get it durably recorded"
    )
