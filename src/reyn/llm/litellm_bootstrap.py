"""Single chokepoint for the first real ``import litellm`` in a process.

``litellm``'s own package init pulls in a huge module tree (~1.5s cold-import
cost). Every reyn call site that touches litellm does so lazily, inside a
function — see ``reyn/__init__.py`` for the full inventory + the #2928
``LITELLM_LOCAL_*`` env-var defaults that must be set before ANY of them run.
This module adds the SECOND piece: routing litellm's own console log output
(StreamHandlers it attaches to its "LiteLLM" / "LiteLLM Router" / "LiteLLM
Proxy" loggers, unconditionally, at import time) to reyn's log file instead of
stderr, so an interactive CUI session's terminal is never corrupted by a
litellm banner or warning.

``ensure_litellm_ready()`` is the ONE place that should perform this
first-import work — and (#4395 PR-1) the ONE place the actual ``import
litellm`` statement should live. Callers use the module it RETURNS
(``None`` on failure) rather than doing their own separate ``import
litellm`` afterward. An earlier version of this paragraph claimed a
caller's own subsequent bare ``import litellm`` was "cheap — Python
caches the module" — true only when the import SUCCEEDED. Python does
NOT cache a FAILED import (a module whose top-level code raises is
evicted from ``sys.modules``), so a caller doing its own redundant bare
``import litellm`` right after this function would re-attempt — and
re-fail — the exact same slow, unbounded network-touching import this
function had JUST attempted, silently doubling the cost of every
failure. See ``ensure_litellm_ready()``'s own docstring for the full
success/failure contract this fixed.

#3671: this module also owns the #3434 client-cache "pre-existing keys"
baseline (:func:`reset_client_cache_baseline`/:func:`client_cache_baseline`)
— NOT because it is about logging, but because ``ensure_litellm_ready`` is
already the one place every REAL litellm use passes through, which is
exactly where the baseline must be captured for a session that never calls
the LLM to never import litellm at all. ``llm.py``'s ``run_async`` used to
call ``import litellm`` unconditionally at its own top (via a now-removed
eager snapshot helper) purely to compute this baseline — costing every
session litellm's own ~1.5-5s cold-import before the UI could even mount,
whether or not that session ever used the LLM. Moving the capture here
means it only happens on the SAME call path that was going to pay the
import cost anyway.
"""
from __future__ import annotations

import contextlib
import logging
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_LITELLM_LOGGER_NAMES = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")

_litellm_ready = False
# #3671 P1: ownership + Future, not a lock (owner directive: prefer a
# non-lock exclusion where one exists). NOTE: a re-entrant call from the
# owner's OWN thread would deadlock on `.result()` just as badly as a plain
# `threading.Lock` would — a Future is NOT reentrancy-safe, so that is not
# the real advantage here (lead-coder finding, corrected 2026-08-03).
# The real advantage: `dict.setdefault` is a SINGLE dict operation, atomic
# under the GIL by construction — the first caller's `Future` wins and is
# stored; every later caller's `setdefault` sees that SAME Future already
# present and gets it back instead of its own throwaway one. Whoever's
# Future came back as the winner OWNS the setup body; everyone else just
# calls `.result()` and blocks until the owner calls `.set_result(...)` —
# which happens only AFTER the real work finishes. A Future literally
# cannot be waited-past before `set_result` runs, so "ready" cannot mean
# *started* instead of *finished* — the bug lead-coder found is closed by
# construction, not by discipline. #3671 P2/P3 (a startup-warming thread,
# not added by this PR) will need this exact "await the owner's finish"
# primitive anyway, so this is not new machinery introduced just for P1.
_ready_registry: "dict[str, Future]" = {}

# #3671: the litellm async-client cache's key set as it stood at the START
# of the CURRENT `run_async` call (#3434's "pre-existing" baseline) — a
# DIFFERENT lifetime than `_litellm_ready` above. `_litellm_ready` is
# process-wide, set once ever; this is per-`run_async`-CALL, reset at each
# call's start by `reset_client_cache_baseline` and captured lazily by
# whichever real litellm use (`ensure_litellm_ready`) happens first WITHIN
# that call — regardless of whether litellm's own process-wide first-import
# setup already ran for an earlier call. Same ownership shape as
# `_ready_registry` above (a Future per "generation", `setdefault`-raced)
# rather than a lock, and for the same reason: the loop-owning caller in
# `run_async` is the only intended re-entry point, but nothing stops a
# concurrent asyncio task within one call from racing another into
# `ensure_litellm_ready` first.
#
# A SINGLE module-global generation counter (not one scoped per-call some
# other way) is safe only because `run_async` (`llm.py`) is `asyncio.run`
# underneath: `asyncio.run` raises if called while a loop is already
# running on the same thread, so two `run_async` calls cannot be nested or
# concurrent on one thread — not by convention, by asyncio's own
# construction. `run_async`'s only callers are the CLI's own outermost
# entry points (`chat.py`, `mcp.py`), one `asyncio.run(_wrapped())` per
# process lifetime of a call. If `run_async` is ever invoked from more
# than one OS thread concurrently, this assumption is the first thing that
# breaks — a second thread's `reset_client_cache_baseline` would advance
# the SAME generation counter the first thread's in-flight call is still
# reading, invalidating its baseline mid-call.
_baseline_generation = 0
_baseline_registry: "dict[int, Future]" = {}


class LitellmUnavailableError(Exception):
    """#4395 PR-1: raised by a call site with NO safe fallback (a real
    completion or embedding call — `llm.py`'s `recorded_acompletion`,
    `litellm_provider.py`'s `embed_batch`) when `ensure_litellm_ready()`
    returns `None`. A clear, explicit failure instead of letting a
    redundant, un-gated bare `import litellm` raise whatever exception
    litellm's own failed import happened to produce — same underlying
    cause, a legible error instead of an incidental one. Retriable: the
    NEXT call gets a fresh attempt (see `ensure_litellm_ready()`'s own
    docstring — a failure is not permanently cached)."""


@contextlib.contextmanager
def _litellm_import_logs_to_file() -> "Iterator[None]":
    """Route litellm's own loggers to reyn's log file instead of stderr.

    litellm's ``_logging.py`` module attaches a fresh ``logging.StreamHandler()``
    (→ stderr, by construction) to each of ``"LiteLLM"`` / ``"LiteLLM Router"`` /
    ``"LiteLLM Proxy"`` **unconditionally at import time** (module-level code,
    not gated on whether the logger already has a handler) — the first
    ``import litellm`` anywhere in the process attaches it, which happens
    inside this context manager's ``with`` body when routed through
    ``ensure_litellm_ready``. Because the attach is unconditional, merely
    pre-configuring these loggers *before* import does not stop litellm from
    ALSO adding its own console handler — it would just add a second one. So
    the console redirect has to intercept handler *construction* itself: for
    the duration of the ``import litellm`` this swaps in a
    ``logging.StreamHandler`` subclass whose default stream is reyn's log file
    instead of stderr, so every StreamHandler litellm builds at import time —
    including the one behind the cost-map-fetch-failure warning
    ``litellm.litellm_core_utils.get_model_cost_map`` emits synchronously
    during import — writes to the file, not the console.

    On exit, the real ``StreamHandler`` class is restored and the three
    loggers are stripped down to file-routed only: their handler lists are
    cleared and ``propagate`` is set ``True``, so every *runtime* litellm log
    (not just the import-time one) flows to the root logger's file handler
    exactly once, with no leftover console sink. This also makes the
    context manager safe to use when litellm was already imported earlier in
    the process (e.g. by an unrelated call site's own lazy import racing ahead
    of ``ensure_litellm_ready``) — the patch during ``import litellm`` becomes
    a no-op (module cache hit, nothing re-runs), but the handler-strip on exit
    still redirects it.
    """
    # Find the reyn.log FileHandler the interactive startup installed. Unlike
    # the pre-lazy-load version — which ran only immediately after
    # ``basicConfig(filename=...)`` so ``handlers[0]`` was guaranteed a
    # FileHandler — this chokepoint is now reached from ANY first-litellm-use
    # call site (interactive CUI, non-interactive run, tests under pytest's
    # live-logging null handler). So scan for a FileHandler explicitly rather
    # than assuming ``handlers[0]`` is stream-backed (a non-stream handler at
    # [0] would otherwise ``AttributeError`` on ``.stream``).
    file_stream = None
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            file_stream = handler.stream
            break
    if file_stream is None:
        # No file handler in place (non-interactive / --cui / no prior
        # basicConfig call) — do not patch anything, so litellm's normal
        # stderr behavior applies.
        yield
        return

    original_stream_handler = logging.StreamHandler

    class _FileRoutedStreamHandler(logging.StreamHandler):
        """``StreamHandler`` that defaults to reyn's log file, not stderr."""

        def __init__(self, stream: object = None) -> None:
            super().__init__(stream=file_stream if stream is None else stream)

    logging.StreamHandler = _FileRoutedStreamHandler  # type: ignore[misc]
    try:
        yield
    finally:
        logging.StreamHandler = original_stream_handler  # type: ignore[misc]
        for name in _LITELLM_LOGGER_NAMES:
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True


_litellm_import_failure_warned = False


def ensure_litellm_ready() -> "Any | None":
    """Idempotent first-touch chokepoint: import litellm + apply its
    one-time setup, returning the imported module — or ``None`` if the
    import itself failed.

    #4395 (PR-1, the minimal fix): this function's OWN internal ``import
    litellm`` is the single place that attempt happens; call sites should
    use the RETURNED module rather than performing their own separate
    ``import litellm`` afterward. Before this fix, the docstring here
    claimed "callers still do their own ``import litellm`` afterward
    (cheap — Python caches the module)" — true only when the import
    SUCCEEDED. Python does NOT cache a FAILED import (a module whose
    top-level code raises is evicted from ``sys.modules``), so 4 call
    sites doing their own redundant bare ``import litellm`` right after
    calling this function (``model_budget.py``, ``model_cost_rate.py``,
    ``llm.py``, ``litellm_provider.py``) were each independently
    re-attempting — and re-failing — the exact same slow, unbounded
    network-touching import this function had JUST attempted, turning
    one slow attempt per call into two. A live owner repro (py-spy
    stack) caught the event-loop/UI thread blocked inside exactly this
    redundant second attempt. A separate 3 sites in ``pricing.py``
    bypassed this chokepoint entirely — a different failure mode (never
    calling this function at all, not a redundant second attempt after
    calling it).

    THE UNDERLYING MISTAKE (not just the stale prose above): the old
    docstring's false premise came from conflating two DIFFERENT
    guarantees — "this chokepoint's one-time setup (log routing,
    ``suppress_debug_info``, ``aiohttp_trust_env``) has run" is NOT the
    same guarantee as "``import litellm`` will now succeed for you too".
    A call site may only assume the SECOND from this function's actual
    RETURN VALUE (the module, or ``None``) — never from the fact that
    this function was merely called, or that it didn't raise, or from
    any other side effect of having reached this chokepoint once before.
    This is the general shape to watch for at any chokepoint: "setup ran"
    and "the resource is usable" are separate claims, and only the
    second is what a caller with no fallback actually needs.

    On SUCCESS: returns the module, and stays ``True``-flagged for the
    rest of the process — a successfully imported module is real,
    permanent state (``sys.modules`` itself never re-runs it), so caching
    "ready" forever is correct. On FAILURE: returns ``None``, and does
    NOT cache the failure — the next call gets a fresh ownership round
    and genuinely retries, because an environmental failure (a proxy
    down) can clear, and callers with no fallback (a real completion or
    embedding call) must keep trying on the next turn, not be
    permanently locked out after one bad attempt. Emits a WARNED-ONCE
    (not every failure — #3368's own "warn once, not every call" lesson)
    log line the first time this happens in the process, so a
    permanently-unusable environment is visible rather than silently
    degrading every downstream fallback with no signal at all.

    Wraps the (possibly first-ever) ``import litellm`` in
    ``_litellm_import_logs_to_file`` (preserving the interactive CUI's
    clean-terminal guarantee — #2929) and sets
    ``litellm.suppress_debug_info = True`` (litellm prints "Give Feedback
    / Get Help" banners straight to stderr on a provider error, NOT via
    ``logging``, so the file redirect above doesn't catch them; this
    suppresses them instead).

    #3075 chokepoint coverage: this is the sole place the
    ``litellm.aiohttp_trust_env = True`` flip is applied, and BOTH
    litellm egress families reach it before their first real call — the
    completion path via ``recorded_acompletion`` (``reyn.llm.llm``, the
    #1190 single ``litellm.acompletion`` chokepoint) and the embedding
    path via ``LiteLLMEmbeddingProvider.embed_batch`` (``reyn.data.
    embedding.litellm_provider``). So the proxy-trust flip covers every
    litellm-originated request, not just chat.
    """
    global _litellm_ready, _litellm_import_failure_warned
    # Unlocked fast-path read keeps the "cheap on every call after the
    # first success" property this docstring promises. NOTE: still falls
    # through to `_capture_client_cache_baseline` below — the
    # process-wide-once setup and the per-`run_async`-call baseline
    # (#3671/#3434) are different lifetimes, so this early return must
    # not skip the second one.
    if _litellm_ready:
        _capture_client_cache_baseline()
        import sys
        return sys.modules.get("litellm")

    my_future: "Future[Any | None]" = Future()
    winning_future = _ready_registry.setdefault("ready", my_future)
    if winning_future is not my_future:
        # Someone else already owns this attempt — wait for THEM to
        # finish, not a lock, so we block only as long as the real work
        # actually takes, never past it (the "started, not finished" bug
        # this replaces).
        result = winning_future.result()
    else:
        # We won ownership. Do the real work, then release every waiter.
        result = None
        try:
            with _litellm_import_logs_to_file():
                try:
                    import litellm
                    litellm.suppress_debug_info = True
                    # #3075 fix 1: litellm's aiohttp transport defaults
                    # aiohttp_trust_env=False, so it is proxy-blind even when the
                    # operator's standard HTTP(S)_PROXY/NO_PROXY env is set — the
                    # highest-volume egress reyn originates (every LLM/embedding call)
                    # was the sharpest non-conformer in the #3075 enumeration. Flipping
                    # this makes litellm read the standard proxy env like every other
                    # conforming egress; it already honours SSL_CERT_FILE/
                    # REQUESTS_CA_BUNDLE via get_ssl_verify(), so this is the one
                    # missing piece for full conformance.
                    litellm.aiohttp_trust_env = True
                    result = litellm
                except Exception:  # noqa: BLE001 — best-effort; never block the caller on this
                    result = None
        finally:
            if result is not None:
                _litellm_ready = True
            else:
                # NOT cached — clear ownership so the NEXT call gets a
                # fresh attempt instead of being permanently stuck on
                # this one failure (see docstring: callers with no
                # fallback must keep retrying across turns).
                _ready_registry.pop("ready", None)
                if not _litellm_import_failure_warned:
                    _litellm_import_failure_warned = True
                    logging.getLogger(__name__).warning(
                        "import litellm failed — falling back where a "
                        "fallback exists, retrying on the next call "
                        "where it doesn't. This is a warn-once notice, "
                        "not a record of a permanent failure.",
                    )
            my_future.set_result(result)

    _capture_client_cache_baseline()
    return result


def reset_client_cache_baseline() -> None:
    """Arm a FRESH per-call baseline capture (#3434's "pre-existing" key
    set) for the `run_async` invocation about to start.

    Call this at `run_async`'s own entry — it touches only plain module
    globals, never imports litellm, so it costs nothing for a session that
    never calls the LLM (the #3671 property: `"litellm" not in sys.modules`
    must still hold after this runs). The NEXT real litellm use within the
    call (`ensure_litellm_ready`, from `recorded_acompletion` or the
    embedding provider) captures the actual baseline lazily; if no such use
    ever happens, no baseline is ever captured and
    :func:`client_cache_baseline` stays `None` for the whole call.
    """
    global _baseline_generation
    _baseline_generation += 1


def client_cache_baseline() -> "frozenset | None":
    """This `run_async` call's pre-existing litellm client-cache key set
    (#3434), or `None` if the call never touched litellm at all.

    `None` and `frozenset()` mean DIFFERENT things to the caller
    (`_close_litellm_async_clients`): `None` means "nothing was ever
    imported this call, there is nothing of ours to close, do not even
    import litellm to check" — treating it as an empty baseline instead
    would tell the close step "everything currently cached is new, close
    all of it", which is the #3434 bug (closing another call's still-open
    client) reintroduced one layer up.
    """
    future = _baseline_registry.get(_baseline_generation)
    if future is None or not future.done():
        return None
    return future.result()


def _capture_client_cache_baseline() -> None:
    """Lazily capture :func:`client_cache_baseline`'s value for the CURRENT
    generation (see :func:`reset_client_cache_baseline`), once, the first
    time this is reached within a given `run_async` call — regardless of
    whether litellm's own process-wide first-import setup (above) ran just
    now or ran for an earlier call. Same ownership shape as
    ``ensure_litellm_ready``'s Future/registry, scoped per-generation
    instead of process-wide, for the same reentrancy-safety reason: nothing
    stops two concurrent asyncio tasks within one call from both reaching
    ``ensure_litellm_ready`` before either has captured the baseline."""
    generation = _baseline_generation
    my_future: "Future[frozenset]" = Future()
    winning_future = _baseline_registry.setdefault(generation, my_future)
    if winning_future is not my_future:
        return  # someone else already owns (or has finished) this capture
    try:
        import litellm

        cache = getattr(litellm, "in_memory_llm_clients_cache", None)
        cache_dict = getattr(cache, "cache_dict", None)
        baseline = frozenset(cache_dict.keys()) if cache_dict else frozenset()
    except Exception:  # noqa: BLE001 — best-effort, matches the module's posture elsewhere
        baseline = frozenset()
    winning_future.set_result(baseline)
