"""Tier 2: litellm import is off the chat startup path (input-box perf, #2929 moved).

``import litellm`` costs ~1.5s (its own huge module tree) and used to run
eagerly inside ``_setup_interactive_logging`` — on the interactive chat
startup path, BEFORE the input box renders. That eager import is removed;
litellm now imports lazily on first real use, via the single chokepoint
``reyn.llm.litellm_bootstrap.ensure_litellm_ready`` (called from
``recorded_acompletion`` — the #1190 single LLM-call funnel — and from the
other lazy litellm call sites that can plausibly run first in a session,
e.g. the session-start cost-warn check).

These tests exercise real subprocesses (not mocks) so the ``sys.modules``
state and the log-routing behavior are the actual process-level facts, not
an in-process assumption that could be contaminated by an earlier test's
``import litellm``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _run(
    src_root: str, script: str, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": src_root}
    if extra_env:
        env.update(extra_env)
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_setup_interactive_logging_does_not_import_litellm(tmp_path, out_of_process_reyn) -> None:
    """Tier 2: `_setup_interactive_logging` — the chat startup-path call — leaves
    litellm out of `sys.modules`.

    FALSIFY: before this change (eager `import litellm` inside the function),
    `"litellm" in sys.modules` would be True immediately after the call.
    """
    script = f"""
        import sys
        from pathlib import Path
        from reyn.interfaces.cli.commands.chat import _setup_interactive_logging

        _setup_interactive_logging(Path({str(tmp_path)!r}))
        assert "litellm" not in sys.modules, "litellm was imported on the startup path"
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_chat_startup_path_clean_of_litellm_at_input_box_point(tmp_path, out_of_process_reyn) -> None:
    """Tier 2: the full chat.py startup-path import (module import + the
    startup-logging call, i.e. everything that runs before `run_repl`/the
    input box) never touches litellm.

    Decisive: imports `reyn.interfaces.cli.commands.chat` itself (module-level
    imports run) AND calls `_setup_interactive_logging`, then asserts
    `sys.modules` is clean. This is the "at input-box render" checkpoint the
    perf fix targets — everything up to (not including) `run_repl` must never
    have pulled litellm in.
    """
    script = f"""
        import sys
        from pathlib import Path
        import reyn.interfaces.cli.commands.chat as chat_mod

        chat_mod._setup_interactive_logging(Path({str(tmp_path)!r}))
        assert "litellm" not in sys.modules, sorted(
            m for m in sys.modules if "litellm" in m
        )
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_ensure_litellm_ready_imports_litellm_and_is_idempotent(tmp_path, out_of_process_reyn) -> None:
    """Tier 2: `ensure_litellm_ready` (the first-real-use chokepoint) imports
    litellm exactly once, is safe to call repeatedly, and sets
    `suppress_debug_info`.
    """
    script = """
        import sys
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready

        assert "litellm" not in sys.modules
        ensure_litellm_ready()
        ensure_litellm_ready()  # idempotent — must not re-import or raise
        import litellm
        assert litellm.suppress_debug_info is True
        print("OK")
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_first_use_quiets_litellm_banners(tmp_path) -> None:
    """Tier 2: #2929 moved — `ensure_litellm_ready`, called at first real
    litellm use, still sets `suppress_debug_info` (litellm's direct-to-stderr
    banners on a provider error are suppressed for the CUI)."""
    import logging

    import litellm

    from reyn.interfaces.cli.commands.chat import _setup_interactive_logging
    from reyn.llm import litellm_bootstrap
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_suppress = getattr(litellm, "suppress_debug_info", False)
    # Reset the process-global one-shot guard so `ensure_litellm_ready` actually
    # runs (an earlier suite test may have tripped it); simulate the pre-first-
    # use state by clearing suppress_debug_info first.
    # #3671 P1: `_ready_registry` (the ownership+Future election dict) is a
    # SECOND piece of process-global one-shot state, alongside `_litellm_ready`
    # — an earlier test's resolved Future left in the registry would make this
    # call return via the "someone else already owns it" branch (a `.result()`
    # on an already-done Future) instead of actually re-running setup, the
    # exact thing this test needs to observe. Reset both together.
    saved_ready = litellm_bootstrap._litellm_ready
    saved_registry = dict(litellm_bootstrap._ready_registry)
    litellm_bootstrap._litellm_ready = False
    litellm_bootstrap._ready_registry.clear()
    litellm.suppress_debug_info = False
    try:
        _setup_interactive_logging(tmp_path)  # startup: file handler in place
        ensure_litellm_ready()  # first real use
        assert litellm.suppress_debug_info is True
    finally:
        # #4362: _setup_interactive_logging also calls
        # logging.captureWarnings(True) now; its own on/off guard
        # (logging._warnings_showwarning) is process-global, same class of
        # leak as root's handlers/level below.
        logging.captureWarnings(False)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        litellm.suppress_debug_info = saved_suppress
        litellm_bootstrap._litellm_ready = saved_ready
        litellm_bootstrap._ready_registry.clear()
        litellm_bootstrap._ready_registry.update(saved_registry)


def test_first_use_routes_litellm_logger_to_file_not_console(tmp_path) -> None:
    """Tier 2: #2929 moved — after `_setup_interactive_logging` (startup, file
    handler installed) followed by `ensure_litellm_ready` (first real use),
    the "LiteLLM" logger has no console handler and routes to the file.

    FALSIFY: without `ensure_litellm_ready`'s exit-time strip, the "LiteLLM"
    logger keeps its own StreamHandler (→ stderr) instead of reaching the file.
    """
    import logging

    import litellm  # noqa: F401 — ensures the "LiteLLM" logger + its handler exist

    from reyn.interfaces.cli.commands.chat import _setup_interactive_logging
    from reyn.llm import litellm_bootstrap
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready

    root = logging.getLogger()
    litellm_logger = logging.getLogger("LiteLLM")
    saved_root_handlers, saved_root_level = root.handlers[:], root.level
    saved_litellm_handlers = litellm_logger.handlers[:]
    saved_litellm_propagate = litellm_logger.propagate
    # `ensure_litellm_ready` is process-global idempotent — an earlier suite
    # test may have already tripped its one-shot guard, which would make the
    # call below a no-op and skip the handler-strip we are asserting on. Reset
    # the guard (a process singleton, not private assertion state) + re-attach
    # a bare console StreamHandler to litellm's logger to reconstruct the
    # pre-first-use state this test exercises.
    # #3671 P1: `_ready_registry` (the ownership+Future election dict) is a
    # SECOND piece of process-global one-shot state alongside `_litellm_ready`
    # — see the analogous reset in `test_first_use_quiets_litellm_banners`
    # above for why both must be reset together.
    saved_ready = litellm_bootstrap._litellm_ready
    saved_registry = dict(litellm_bootstrap._ready_registry)
    litellm_bootstrap._litellm_ready = False
    litellm_bootstrap._ready_registry.clear()
    litellm_logger.addHandler(logging.StreamHandler())
    try:
        _setup_interactive_logging(tmp_path)
        ensure_litellm_ready()
        log_file = tmp_path / ".reyn" / "logs" / "reyn.log"

        assert not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in litellm_logger.handlers
        )
        assert litellm_logger.propagate is True

        litellm_logger.warning("runtime-marker-9a1b-not-a-mock")
        for h in root.handlers:
            h.flush()
        assert "runtime-marker-9a1b-not-a-mock" in log_file.read_text()
    finally:
        # #4362: same process-global captureWarnings guard leak as
        # test_first_use_quiets_litellm_banners above.
        logging.captureWarnings(False)
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        litellm_logger.handlers[:] = saved_litellm_handlers
        litellm_logger.propagate = saved_litellm_propagate
        litellm_bootstrap._litellm_ready = saved_ready
        litellm_bootstrap._ready_registry.clear()
        litellm_bootstrap._ready_registry.update(saved_registry)


def test_first_use_routes_litellm_import_time_warning_to_file(tmp_path, out_of_process_reyn) -> None:
    """Tier 2: litellm's cost-map-fetch-failure warning, emitted synchronously
    *during* ``import litellm`` (not a runtime call reyn controls, so exercised
    via a real subprocess), lands in reyn.log and NOT on stderr — now via the
    first-real-use chokepoint instead of the (removed) startup-path import.

    Forces the fetch-failure path explicitly (overrides the #2928
    ``LITELLM_LOCAL_MODEL_COST_MAP`` setdefault so the remote fetch is
    attempted) against an unreachable URL, so the warning is guaranteed to
    fire. FALSIFY: without the patch, litellm's own StreamHandler prints this
    warning straight to stderr.
    """
    project_root = tmp_path
    script = f"""
        import os
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "False"
        os.environ["LITELLM_MODEL_COST_MAP_URL"] = "http://127.0.0.1:1/unreachable.json"

        from pathlib import Path
        from reyn.interfaces.cli.commands.chat import _setup_interactive_logging
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready

        _setup_interactive_logging(Path({str(project_root)!r}))
        assert "litellm" not in __import__("sys").modules
        ensure_litellm_ready()  # first real litellm use -> import happens here
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    log_file = project_root / ".reyn" / "logs" / "reyn.log"
    log_text = log_file.read_text()
    assert "Failed to fetch remote model cost map" in log_text
    assert "Failed to fetch remote model cost map" not in result.stderr


def test_sibling_first_import_routes_import_time_warning_to_file(tmp_path, out_of_process_reyn) -> None:
    """Tier 2: chokepoint-completeness — when a NON-recorded_acompletion litellm
    call site is the FIRST to import litellm, the import-time cost-map-fetch
    warning still lands in reyn.log, NOT stderr.

    Exercises a genuine *sibling* first-import path: ``compaction.engine.
    estimate_tokens`` (per-turn token sizing runs before the first completion,
    so it can be the process's first ``import litellm``) — one of the four
    sibling sites now wired to ``ensure_litellm_ready``. It never calls
    ``recorded_acompletion``, so it proves the routing does not depend on the
    LLM-call funnel.

    FALSIFY: without the ``ensure_litellm_ready()`` call newly added at the
    sibling site, this path's bare ``import litellm`` attaches litellm's own
    stderr StreamHandler and the import-time warning leaks to the console —
    the assertion `not in result.stderr` would fail. (Confirmed by removing
    the wire and re-running: the warning appears on stderr.)

    #4395 PR-2: ``estimate_tokens`` now calls the NON-blocking
    ``ensure_litellm_ready_or_defer()`` — the FIRST call in a process with
    litellm not yet ready no longer imports inline; it kicks off the one
    dedicated background thread and falls back to chars//4 immediately.
    That thread does the exact same ``ensure_litellm_ready()`` call this
    test cares about (so the #2929 routing still applies), just off the
    calling thread — waits for it to actually finish (unbounded wait on the
    condition itself, no wait-budget constant of this test's own; CI's own
    kill switch is the backstop) before asserting, instead of assuming the
    single ``estimate_tokens`` call already did the import synchronously.
    """
    project_root = tmp_path
    script = f"""
        import os, sys, time
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "False"
        os.environ["LITELLM_MODEL_COST_MAP_URL"] = "http://127.0.0.1:1/unreachable.json"

        from pathlib import Path
        from reyn.interfaces.cli.commands.chat import _setup_interactive_logging
        from reyn.llm.litellm_bootstrap import is_litellm_ready
        from reyn.services.compaction.engine import estimate_tokens

        _setup_interactive_logging(Path({str(project_root)!r}))
        assert "litellm" not in sys.modules, "litellm imported before the sibling call"
        # SIBLING-FIRST: the very first litellm import in this process happens
        # inside estimate_tokens (via its ensure_litellm_ready_or_defer wire),
        # NOT via recorded_acompletion / ensure_litellm_ready called directly.
        # #4395 PR-2: this defers to the background warming thread rather
        # than importing inline — wait for that thread to finish.
        estimate_tokens("some text to size", "gpt-3.5-turbo")
        # Unbounded wait on the condition itself — no wait-budget constant
        # of this test's own (CI's own --timeout=120 kill switch is the
        # backstop; a script needing longer than that to warm should be
        # investigated, not silently capped here).
        while not is_litellm_ready():
            time.sleep(0.05)
        assert "litellm" in sys.modules, "sibling call's background warm did not import litellm"
        """
    result = _run(out_of_process_reyn, script)
    assert result.returncode == 0, result.stderr
    log_file = project_root / ".reyn" / "logs" / "reyn.log"
    log_text = log_file.read_text()
    assert "Failed to fetch remote model cost map" in log_text
    assert "Failed to fetch remote model cost map" not in result.stderr


def test_measured_startup_latency_delta_from_deferring_litellm(out_of_process_reyn) -> None:
    """Tier 2: measures the actual latency delta the perf fix targets — the
    startup-path work (importing `reyn.interfaces.cli.commands.chat` +
    calling `_setup_interactive_logging`) is fast, while a bare
    `import litellm` remains the ~1.5s cost, now off that path.

    Not a strict pinned-timing assertion (no algorithm-level behavior is
    pinned) — reports the measured delta and asserts only the qualitative,
    load-bearing claim: the startup path is dramatically cheaper than a raw
    litellm import, which is the whole point of deferring it.
    """
    startup_script = """
        import time
        t = time.perf_counter()
        from pathlib import Path
        import reyn.interfaces.cli.commands.chat as chat_mod
        chat_mod._setup_interactive_logging(Path("/tmp"))
        print(time.perf_counter() - t)
        """
    litellm_script = """
        import time
        t = time.perf_counter()
        import litellm
        print(time.perf_counter() - t)
        """
    startup_result = _run(out_of_process_reyn, startup_script)
    litellm_result = _run(out_of_process_reyn, litellm_script)
    assert startup_result.returncode == 0, startup_result.stderr
    assert litellm_result.returncode == 0, litellm_result.stderr

    startup_s = float(startup_result.stdout.strip().splitlines()[-1])
    litellm_s = float(litellm_result.stdout.strip().splitlines()[-1])

    # The startup path must be substantially cheaper than a bare litellm
    # import (this is the whole perf claim) — a generous 3x margin avoids
    # flaking on a loaded CI box while still falsifying a regression that
    # re-adds the eager import (which would make startup_s >= litellm_s).
    assert startup_s * 3 < litellm_s, (
        f"startup path ({startup_s:.3f}s) is not substantially cheaper than "
        f"a bare litellm import ({litellm_s:.3f}s) — litellm may be back on "
        f"the startup path"
    )


# ── #3671: structural guard for the ensure_litellm_ready-first assumption ────
#
# run_async's #3434 close-scoping now depends on ensure_litellm_ready()
# having run before ANY litellm async client is created THIS call (see
# reyn.llm.llm.run_async's own docstring). That was verified against today's
# call sites by NAME (grepping for get_async_httpx_client/acompletion/
# aembedding) — a name-shaped search, the exact blind-spot class this
# session hit three times already: a 5th call site creating a client under a
# DIFFERENT name would be invisible to a name grep and would silently make
# the assumption false, breaking in the direction that does not turn CI red
# (close silently skips). These three gates turn "verified once by grep"
# into "enforced by construction" the same way #1190's AST guard already
# does for litellm.acompletion specifically — walking every file's AST
# rather than matching an expected name shape.
#
# RESIDUAL RISK, deliberately accepted rather than closed (lead-coder
# review): these gates still enumerate litellm's client-creating APIs BY
# NAME (acompletion / aembedding / get_async_httpx_client) — routing every
# lazy litellm call site through ensure_litellm_ready() unconditionally
# (closing this for good) would also force it onto sites that only ever
# touch STATIC data (model_budget.py / model_cost_rate.py / pricing.py /
# core/registry/client.py / core/op_runtime/web.py — cost maps, token
# counting, ssl verify — none create an async client), which risks pulling
# the import back toward startup for paths that never needed it, the exact
# #3671 regression this arc closes. If litellm ever grows a NEW
# client-creating API under a different name, THESE GATES WILL NOT CATCH
# IT — add it here alongside the three above.

import ast


def _repo_src_root() -> "Path":
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor / "src" / "reyn"
    raise RuntimeError("repo root not found from " + str(here))


def _call_span(py: "Path", func_name: str) -> "tuple[int, int] | None":
    """The (start, end) line span of the first ``def``/``async def`` named
    ``func_name`` in ``py``, or ``None`` if not found."""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node.lineno, (node.end_lineno or node.lineno)
    return None


def test_litellm_aembedding_only_inside_the_bounded_helper() -> None:
    """Tier 2: OS invariant — ``litellm.aembedding`` may be called ONLY
    inside ``LiteLLMEmbeddingProvider._aembedding_bounded``
    (``reyn.data.embedding.litellm_provider``), the one place that already
    calls ``ensure_litellm_ready()`` (via its caller, ``embed_batch``)
    before reaching it. A new embedding call site elsewhere would create a
    litellm async client without that guarantee, silently breaking
    run_async's #3434 close-scoping baseline in the direction that never
    turns CI red on its own (the close step just skips)."""
    src = _repo_src_root()
    provider_py = (src / "data" / "embedding" / "litellm_provider.py").resolve()
    span = _call_span(provider_py, "_aembedding_bounded")
    assert span is not None, "_aembedding_bounded not found — chokepoint moved?"
    start, end = span

    offenders: "list[str]" = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "aembedding"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "litellm"
            ):
                continue
            inside = py.resolve() == provider_py and start <= node.lineno <= end
            if not inside:
                offenders.append(f"{py.relative_to(src.parent.parent)}:{node.lineno}")

    assert not offenders, (
        "litellm.aembedding called outside _aembedding_bounded (bypasses "
        "the ensure_litellm_ready-first guarantee run_async's #3434 "
        f"close-scoping depends on). Offending sites: {offenders}"
    )


def test_no_production_call_site_creates_a_litellm_client_below_acompletion_aembedding() -> None:
    """Tier 2: OS invariant — nothing in ``src/reyn/`` calls litellm's
    lower-level async-client constructors directly (``get_async_httpx_client``
    and siblings), which would create a real cached client WITHOUT going
    through ``litellm.acompletion``/``aembedding`` — and therefore without
    the ``ensure_litellm_ready()`` guarantee those two chokepoints carry
    (the #1190 AST guard for ``acompletion``; the sibling gate above for
    ``aembedding``). Today this is true because nothing needs to reach that
    low-level; this test is what makes a FUTURE such site fail here instead
    of silently widening the #3434 close-scoping gap.

    Scoped to ``get_async_httpx_client`` by name deliberately paired with a
    Tier-1 comment, not a Tier-2 enforcement claim: litellm exposes more
    than one low-level constructor family, and this guard does not claim to
    enumerate all of them — it closes the ONE bypass this arc's own
    investigation found and used in test setup (see
    ``test_llm_run_async_shutdown_litellm_clients_2787.py``), so a
    regression on THAT specific bypass is caught."""
    src = _repo_src_root()

    offenders: "list[str]" = []
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "get_async_httpx_client" in [
                a.name for a in node.names
            ]:
                offenders.append(f"{py.relative_to(src.parent.parent)}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_async_httpx_client"
            ):
                offenders.append(f"{py.relative_to(src.parent.parent)}:{node.lineno}")

    assert not offenders, (
        "get_async_httpx_client used outside a test — this creates a "
        "litellm async client WITHOUT the ensure_litellm_ready-first "
        f"guarantee. Offending sites: {offenders}"
    )
