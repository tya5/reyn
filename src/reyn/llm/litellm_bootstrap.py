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
first-import work. Callers that need the ``litellm`` module still do their own
``import litellm`` afterward (cheap — Python caches the module), but calling
``ensure_litellm_ready()`` first guarantees the log-routing patch is active
for whichever call site happens to import litellm first in a given process.
"""
from __future__ import annotations

import contextlib
import logging
from concurrent.futures import Future
from typing import TYPE_CHECKING

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


def ensure_litellm_ready() -> None:
    """Idempotent first-touch chokepoint: import litellm + apply its one-time setup.

    Runs at most once per process. Wraps the (possibly first-ever) ``import
    litellm`` in ``_litellm_import_logs_to_file`` (preserving the interactive
    CUI's clean-terminal guarantee — #2929) and sets
    ``litellm.suppress_debug_info = True`` (litellm prints "Give Feedback /
    Get Help" banners straight to stderr on a provider error, NOT via
    ``logging``, so the file redirect above doesn't catch them; this
    suppresses them instead).

    Call this before any bare ``import litellm`` in a lazy call site that
    might be the first one to run in a given process — it costs one boolean
    check on every call after the first, so it is cheap to call defensively.

    #3075 chokepoint coverage: this is the sole place the
    ``litellm.aiohttp_trust_env = True`` flip is applied, and BOTH litellm
    egress families reach it before their first real call — the completion
    path via ``recorded_acompletion`` (``reyn.llm.llm``, the #1190 single
    ``litellm.acompletion`` chokepoint, which calls this before the
    ``acompletion``) and the embedding path via
    ``LiteLLMEmbeddingProvider.embed_batch`` (``reyn.data.embedding.
    litellm_provider``, which calls this before ``_aembedding_bounded`` →
    ``litellm.aembedding``). So the proxy-trust flip covers every
    litellm-originated request, not just chat.
    """
    global _litellm_ready
    # Unlocked fast-path read keeps the "cheap on every call after the
    # first" property this docstring promises — after the owner's Future
    # resolves this flag is the only thing subsequent calls ever touch.
    if _litellm_ready:
        return

    my_future: "Future[None]" = Future()
    winning_future = _ready_registry.setdefault("ready", my_future)
    if winning_future is not my_future:
        # Someone else already owns setup — wait for THEM to finish, not a
        # lock, so we block only as long as the real work actually takes,
        # never past it (the "started, not finished" bug this replaces).
        winning_future.result()
        return

    # We won ownership. Do the real work, then release every waiter.
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
            except Exception:  # noqa: BLE001 — best-effort; never block the caller on this
                pass
    finally:
        _litellm_ready = True
        my_future.set_result(None)
