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
import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from reyn import _cooldown

if TYPE_CHECKING:
    from collections.abc import Iterator

    from reyn.core.events.events import EventLog

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

# #4395 axis②/PR-2 (owner-flagged gap in PR-1): PR-1 removed the WITHIN-one-
# call double attempt (a caller's own redundant bare `import litellm` right
# after this chokepoint) but a failure is deliberately NOT cached as
# "ready" — the NEXT call still starts a genuinely fresh attempt, which
# means a PERSISTENTLY broken environment (the owner's own repro: a TLS
# handshake that never completes) gets re-attempted, and re-hangs, on
# EVERY subsequent call, not just once. `_LITELLM_IMPORT_COOLDOWN_SECONDS`
# closes that: a failure starts a cooldown window (`reyn._cooldown`, the
# same primitive #4398 already established for the identical shape in
# `compaction/engine.py`'s `estimate_tokens`) during which every call
# returns `None` immediately — no import attempted, no wait paid — rather
# than a permanent give-up (the underlying cause may clear; the window
# just rate-limits re-probing, it does not stop it).
_LITELLM_IMPORT_COOLDOWN_SECONDS = 60.0
_litellm_import_cooldown_until = 0.0

# #4418: reyn's own name for this egress class in the #3075 audit hook —
# ``import litellm`` triggers a THIRD-PARTY (tiktoken) network call this
# module does not construct the client for, so it is named as its own
# egress class rather than folded into "litellm" (which would misattribute
# a disabled-verify audit event to the LLM call path itself).
_TIKTOKEN_IMPORT_EGRESS_NAME = "tiktoken-import"
# tiktoken's own ``load.py:17`` call carries no timeout at all (#4395's own
# finding, generalized) — 30s matches ``reyn._network``'s other best-effort
# fetch timeouts (none currently sets a *class* default higher), a bound
# generous enough for a small BPE-vocab blob download over a slow link
# without leaving a stalled network call to hang the import indefinitely.
_TIKTOKEN_IMPORT_TIMEOUT_SECONDS = 30.0


def _diagnose_import_failure_for_log() -> str:
    """#4422: turn an ``import litellm`` failure's warn-once log line from
    "it's broken" into "here's why, and what to try" — reusing #4399's own
    judgment (:func:`reyn._tiktoken_diag.diagnose`) rather than re-deriving
    it. Owner + lead-coder spent real hours tracing a real environment's
    ``import litellm`` failure back to a missing/mismatched tiktoken cache
    file before this existed; the fix this issue asks for is making reyn
    say that in ONE line instead of requiring the same multi-hour trace
    again for the next operator who hits it.

    Three outcomes, matching lead-coder's own review correction (do NOT
    assert "tiktoken deleted this" as fact when the file is merely absent
    — absence has more than one cause, and only a DIRECT sha256-mismatch
    reading is confirmed present-tense fact):

    - bundled_file_exists is False: the cache file is not there NOW. This
      does not by itself prove why (never written, or deleted after a past
      mismatch, are both consistent with "not there now") — phrased as a
      possibility, not asserted, with the same remedy (reinstall) that
      also serves as the operator's own diagnostic: if the file exists
      afterward, it was (A) a missing-bundle case; if it goes missing
      again on next use, that is itself evidence of (B).
    - bundled_file_exists is True and sha256_matches is False: this IS a
      directly observed, present-tense mismatch (not inferred) between the
      installed litellm's bundled blob and what the installed tiktoken
      expects — tiktoken will delete and re-fetch this exact file on next
      use, statable as fact because it was just read.
    - Anything else (including "unknown" — the diagnosis itself could not
      read the installed tiktoken_ext source): no cache-specific signal:
      fall back to the #4418 network/cert remedy for a `requests.get`
      failure with no other explanation available.

    Never raises — this augments a WARN, and a broken diagnostic reading
    must not turn one warning into two (or a startup crash) when the
    unaugmented message alone was already good enough for #4395's own
    original fix.
    """
    try:
        from reyn._tiktoken_diag import diagnose

        d = diagnose()
    except Exception:  # noqa: BLE001 — best-effort; the plain WARN above already fired
        return (
            "(tiktoken-cache diagnosis unavailable — check SSL_VERIFY / "
            "SSL_CERT_FILE / REQUESTS_CA_BUNDLE if this looks like a "
            "network/certificate failure.)"
        )

    if d.bundled_file_exists is False:
        return (
            "tiktoken's cache file was not found (litellm=%s, tiktoken=%s) — "
            "possibly never written, or removed after a past version "
            "mismatch; not something this check alone can distinguish. Try: "
            "pip install --force-reinstall --no-deps litellm — if the file "
            "is present afterward, this was a missing-bundle case; if it "
            "disappears again on next use, that itself confirms a version "
            "mismatch." % (d.litellm_version, d.tiktoken_version)
        )
    if d.bundled_file_exists is True and d.sha256_matches is False:
        return (
            "tiktoken's cache file does not match what the installed "
            "tiktoken (%s) expects from litellm's bundle (litellm=%s) — a "
            "version mismatch. tiktoken will delete and re-fetch this file "
            "over the network on next use. Try: pip install "
            "--force-reinstall --no-deps litellm to realign the two "
            "versions." % (d.tiktoken_version, d.litellm_version)
        )
    return (
        "no tiktoken-cache-specific signal found (litellm=%s, tiktoken=%s) "
        "— if this looks like a network/certificate failure, check "
        "SSL_VERIFY / SSL_CERT_FILE / REQUESTS_CA_BUNDLE."
        % (d.litellm_version, d.tiktoken_version)
    )


@contextlib.contextmanager
def _third_party_import_egress_honours_standard_env(
    events: "EventLog | None",
) -> "Iterator[None]":
    """#4418: close the #3075 "zero exceptions" gap ``tiktoken`` opened.

    ``import litellm`` pulls in ``litellm_core_utils/default_encoding.py``,
    which calls ``tiktoken.get_encoding(...)`` — and on a cache miss,
    ``tiktoken/load.py:17`` does a bare ``requests.get(blobpath)`` with no
    ``verify=``/``timeout=`` of its own. This is genuinely reyn-originated
    egress (it only fires because reyn imports litellm), but reyn cannot
    pass ``verify=``/``timeout=`` into a THIRD PARTY's call the way
    ``reyn._network``'s DRY constructors do for reyn's own httpx clients —
    the only lever available is ``requests``' own per-call DEFAULT.

    So this patches ``requests.sessions.Session.request`` — the one method
    every ``requests.get``/``.post``/etc. call funnels through — to inject
    ``verify``/``timeout`` defaults resolved from the SAME standard env
    ``reyn._network.resolve_ssl_verify_from_env`` already reads for every
    other egress class (``SSL_VERIFY`` / ``SSL_CERT_FILE`` /
    ``REQUESTS_CA_BUNDLE``), for the DURATION of this one ``import litellm``
    call only — not patched globally/forever. reyn's #3075 conformance
    obligation covers what reyn itself causes (this one import), not every
    unrelated ``requests`` call a plugin or the host process might make
    elsewhere in the same interpreter; leaving the patch in place past this
    scope would be reyn silently weakening TLS verification for code it has
    no business touching.

    ``kwargs.setdefault`` (not an unconditional override) — an explicit
    ``verify=``/``timeout=`` a caller already passed (not tiktoken's own
    call today, but this patch covers every ``requests`` call made during
    ``import litellm``, not just tiktoken's) is left alone, same "explicit
    wins" precedent as ``resolve_ssl_verify_from_env``'s own callers.

    A verify=False resolution runs the SAME never-silent audit hook
    (:func:`reyn._network.note_ssl_verify_disabled`) every other #3075
    egress class uses — one WARN + one ``network_ssl_verify_disabled`` P6
    audit-event per process, keyed on
    :data:`_TIKTOKEN_IMPORT_EGRESS_NAME` so it is distinguishable from a
    litellm-call-path verify=False in the same log/event stream.
    """
    try:
        import requests.sessions
    except Exception:
        # requests is an optional/transitive dependency from reyn's own
        # perspective (litellm pulls it in) — if it is not importable for
        # any reason, there is nothing to patch and nothing to protect;
        # ``import litellm`` proceeds and either works or fails on its own.
        yield
        return

    from reyn._network import note_ssl_verify_disabled, resolve_ssl_verify_from_env

    verify = resolve_ssl_verify_from_env()
    if verify is False:
        note_ssl_verify_disabled(events, _TIKTOKEN_IMPORT_EGRESS_NAME)

    original_request = requests.sessions.Session.request

    def _patched_request(self: "requests.sessions.Session", method: str, url: str, **kwargs: Any):
        kwargs.setdefault("verify", verify)
        kwargs.setdefault("timeout", _TIKTOKEN_IMPORT_TIMEOUT_SECONDS)
        return original_request(self, method, url, **kwargs)

    # setattr, not a direct attribute assignment: ``_patched_request``'s
    # signature is intentionally NARROWER than ``requests``' own real
    # ``Session.request`` (which carries ~17 named parameters this wrapper
    # only re-exposes via ``**kwargs``) — the whole point of a monkeypatch
    # wrapper. A direct ``Session.request = _patched_request`` makes mypy
    # compare the two signatures structurally and reject the mismatch
    # ([method-assign]/[assignment], version-dependent which code it picks
    # — CI and this reviewer's local run disagreed on the exact code,
    # which is itself evidence a bracketed ``# type: ignore[...]`` is the
    # wrong fix here). ``setattr`` sidesteps that comparison the same way
    # every other Python monkeypatch of a class method does, without
    # hiding the pattern from a reader OR pushing it into the mypy
    # baseline (lead-coder's review point: the baseline is for findings
    # nobody chose, not for erasing the visible fact that THIS PR
    # patches a class method process-wide for an import window — that
    # fact belongs in the code, not the ratchet's ledger).
    setattr(requests.sessions.Session, "request", _patched_request)
    try:
        yield
    finally:
        setattr(requests.sessions.Session, "request", original_request)


def ensure_litellm_ready(
    events: "EventLog | None" = None, *, ignore_cooldown: bool = False
) -> "Any | None":
    """Idempotent first-touch chokepoint: import litellm + apply its
    one-time setup, returning the imported module — or ``None`` if the
    import itself failed.

    ``ignore_cooldown`` (#4395 PR-2 follow-up, lead-coder review): the
    axis② cooldown below exists to protect callers WITH a safe fallback
    (``ensure_litellm_ready_or_defer()``'s own callers — a conservative
    default, an unknown-cost ``None``, chars//4) from repeatedly re-
    attempting, and re-hanging on, the same broken import. A caller with
    NO fallback (a real completion/embedding call, or a structured-output
    precheck that genuinely needs an answer) was ALREADY a "must wait,
    there is nowhere else to go" site before axis② existed — for THAT
    caller, the cooldown is not a protection, it is a regression: a
    transient failure that would have cleared and succeeded on retry
    instead hard-fails immediately, without even attempting, for the
    rest of the cooldown window. Pass ``ignore_cooldown=True`` from a
    no-fallback call site to skip the axis② gate below and always make a
    genuine attempt — this does NOT reintroduce axis①'s original bug
    (within-call double-attempt) or spawn a duplicate concurrent import:
    the `_ready_registry` ownership dance immediately below still applies
    unchanged, so a genuine attempt already in flight (from the
    background warming thread, or another no-fallback caller) is still
    joined via the SAME `Future`, never independently repeated.

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
    clean-terminal guarantee — #2929) AND
    ``_third_party_import_egress_honours_standard_env`` (#4418 — the
    SAME import triggers a bare, unauthenticated ``tiktoken`` blob
    download with no ``verify=``/``timeout=`` of its own; see that
    context manager's own docstring) and sets
    ``litellm.suppress_debug_info = True`` (litellm prints "Give Feedback
    / Get Help" banners straight to stderr on a provider error, NOT via
    ``logging``, so the file redirect above doesn't catch them; this
    suppresses them instead).

    ``events`` (#4418, optional — same posture as ``reyn._network``'s DRY
    httpx constructors): an ``EventLog`` a caller happens to have in scope,
    fed straight through to the ``tiktoken-import`` egress's
    ``verify=False`` audit hook. ``None`` (every call site except
    ``RegistryClient.__aenter__`` today) still emits the one-time WARN;
    only the P6 ``network_ssl_verify_disabled`` audit-event is skipped —
    this function's own idempotent "first caller wins" ownership means a
    LATER call passing ``events`` after an earlier ``events=None`` call
    already won ownership does NOT get a second chance at the audit-event
    for this process (the underlying ``import litellm`` only ever runs
    once) — an accepted gap, not a promise this parameter breaks; nothing
    currently depends on being the FIRST call to also be the one with an
    ``EventLog`` in scope.

    #3075 chokepoint coverage: this is the sole place the
    ``litellm.aiohttp_trust_env = True`` flip is applied, and BOTH
    litellm egress families reach it before their first real call — the
    completion path via ``recorded_acompletion`` (``reyn.llm.llm``, the
    #1190 single ``litellm.acompletion`` chokepoint) and the embedding
    path via ``LiteLLMEmbeddingProvider.embed_batch`` (``reyn.data.
    embedding.litellm_provider``). So the proxy-trust flip covers every
    litellm-originated request, not just chat.
    """
    global _litellm_ready, _litellm_import_failure_warned, _litellm_import_cooldown_until
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

    # #4395 axis②: a PERSISTENT failure (e.g. a TLS handshake that never
    # completes) must not be re-attempted, and re-hung-on, by every single
    # call across the whole cooldown window — see this module's own
    # `_LITELLM_IMPORT_COOLDOWN_SECONDS` comment above. Checked BEFORE the
    # ownership dance below so a call inside the window never even
    # contests for it. Unlocked read, same GIL-atomic-single-variable
    # reasoning as the fast path above; the worst race is one caller
    # re-probing slightly early or late, never a wrong result.
    # `ignore_cooldown` (see this function's own docstring): a no-fallback
    # caller opts out of this gate entirely — it was already a "must
    # wait" site before axis② existed, and the cooldown protects
    # fallback-having callers, not this one.
    if not ignore_cooldown and _cooldown.in_cooldown(_litellm_import_cooldown_until):
        return None

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
            with (
                _litellm_import_logs_to_file(),
                _third_party_import_egress_honours_standard_env(events),
            ):
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
                    # #4395/#4421 (seam alignment): `litellm.litellm_core_
                    # utils.logging_worker` is NOT one of the submodules
                    # litellm's own `__init__` eagerly populates as an
                    # attribute (unlike e.g. `litellm.types.utils` or
                    # `litellm.llms.custom_httpx.http_handler`, verified
                    # empirically) — imported here, once, inside the ONE
                    # place `import litellm` itself is allowed, so
                    # `llm.py`'s `shutdown_logging()` can read it via plain
                    # attribute access afterward instead of needing its
                    # own submodule import statement outside the seam.
                    import litellm.litellm_core_utils.logging_worker  # noqa: F401

                    # #5603: reyn's own local litellm patches, applied ONCE
                    # inside this same seam (never a `site-packages`-direct
                    # `.pth` file — see `_litellm_compat_patches`'s own
                    # module docstring for the incident that replaces). The
                    # correctness-critical patch (A) is UNCAUGHT here — its
                    # own failure falls into the SAME `except Exception:
                    # result = None` this `import litellm` failure already
                    # uses, so a no-fallback caller sees "litellm unusable"
                    # rather than silently running with a known-broken
                    # bridge; the diagnostic-only patch (B) catches its own
                    # failures internally and only warns (see
                    # `_litellm_compat_patches.apply_all`'s own docstring).
                    from reyn.llm._litellm_compat_patches import apply_all as _apply_litellm_patches
                    _apply_litellm_patches(events)
                    result = litellm
                except Exception:  # noqa: BLE001 — best-effort; never block the caller on this
                    result = None
        finally:
            if result is not None:
                _litellm_ready = True
                _litellm_import_cooldown_until = 0.0  # healthy — clear any cooldown
            else:
                # NOT cached — clear ownership so the NEXT call gets a
                # fresh attempt instead of being permanently stuck on
                # this one failure (see docstring: callers with no
                # fallback must keep retrying across turns). #4395 axis②:
                # that "fresh attempt" is now rate-limited by the cooldown
                # above rather than happening on literally every call —
                # ownership is still cleared (so the FIRST call after the
                # cooldown elapses can win it), just gated by the cooldown
                # check before it's ever contested.
                _ready_registry.pop("ready", None)
                _litellm_import_cooldown_until = _cooldown.new_cooldown_deadline(
                    _LITELLM_IMPORT_COOLDOWN_SECONDS,
                )
                if not _litellm_import_failure_warned:
                    _litellm_import_failure_warned = True
                    logging.getLogger(__name__).warning(
                        "import litellm failed — falling back where a "
                        "fallback exists, retrying on the next call "
                        "where it doesn't. This is a warn-once notice, "
                        "not a record of a permanent failure. %s",
                        _diagnose_import_failure_for_log(),
                    )
            my_future.set_result(result)

    # #4418②: only when `result is not None` — `_capture_client_cache_
    # baseline` does its OWN bare `import litellm` (not wrapped in
    # `_third_party_import_egress_honours_standard_env`, since it isn't
    # the seam's "first real import" call), which is a harmless no-op
    # re-import when litellm already imported successfully but a SECOND,
    # UNPROTECTED full re-import attempt when the FIRST one (just above,
    # inside the protected `with` block) failed and Python evicted the
    # partially-initialized module from `sys.modules` — reopening the
    # exact tiktoken-fetch-without-SSL_VERIFY/timeout gap #4419 closed,
    # through a second, unguarded `import litellm` statement this
    # module's own docstring says should not exist ("ensure_litellm_
    # ready() is the ONE place... the actual import litellm statement
    # should live"). Measured directly (2026-08-13, forced tiktoken cache
    # miss + blocked socket probe): a failed import here produced 5
    # PROTECTED fetch attempts (verify=/timeout= present) followed by 5
    # MORE, unprotected ones (bare `{"params": None}`, no verify/timeout)
    # from `_capture_client_cache_baseline`'s own `import litellm` — not
    # hypothetical. There is nothing to baseline when the import failed
    # (litellm's `in_memory_llm_clients_cache` doesn't exist), so skipping
    # entirely on failure is strictly correct, not just a workaround.
    if result is not None:
        _capture_client_cache_baseline()
    return result


# ── #4395 PR-2: a background warming thread for call sites with a fallback ──
#
# PR-1 (#4413) closed the "one attempt per call, forever, while litellm
# keeps failing" defect above, and made the two call sites with NO fallback
# (`llm.py`'s `recorded_acompletion`, `litellm_provider.py`'s embed-retry
# loop) genuinely awaitable (`asyncio.to_thread(ensure_litellm_ready)`) so
# the wait for a real answer no longer blocks the whole event loop — only
# the one coroutine that needs the answer. Neither of those two sites is
# touched by this section: they have no safe fallback, so they must keep
# waiting for a real result regardless of how that wait is implemented.
#
# What PR-1 left: the FIRST-ever `import litellm` in a process can still
# take however long litellm's own (upstream, un-timed-out) tiktoken fetch
# takes — owner-observed longer than the local ~7.4s case. `model_budget.py`
# / `model_cost_rate.py` / `compaction/engine.py`'s two litellm-touching
# functions all ALREADY have a safe, cheap fallback for "no answer yet"
# (a conservative token-budget default, an unknown-cost `None`, a chars//4
# estimate) — for these, there is no reason to wait for litellm at all, let
# alone on the calling thread. This section gives them a NON-blocking
# variant: kick off exactly ONE persistent background thread that keeps
# retrying (with a cooldown between attempts — a failed import isn't cached,
# so retrying on every single call while litellm stays broken would mean
# one slow attempt per call, same shape PR-1 already closed for the
# no-fallback sites) until it succeeds, and have every caller with a
# fallback fail fast to its own fallback instead of waiting on that thread
# at all. #3671 P1 (3b113e597) already made the 6 shared-state items a
# concurrent background thread would touch (this module's own
# `_ready_registry`/`_litellm_ready`, `llm.py`'s
# `_RETRYABLE_LITELLM_EXCEPTIONS` / `_HTTPX_EXC_TYPES`,
# `compaction/engine.py`'s `_token_cache` / `_token_counter_fallback_warned`
# / `_token_counter_cooldown_until`) thread-safe in preparation for exactly
# this — verified still true of the current code before writing this
# section, not assumed from that PR's own description.
_LITELLM_WARM_POLL_SECONDS = 0.05
_litellm_warm_thread: "threading.Thread | None" = None


class LitellmWarmingInBackgroundError(Exception):
    """Raised by :func:`ensure_litellm_ready_or_defer` when litellm is not
    yet importable and the caller has a fallback — the ONE dedicated
    background thread (:func:`warm_litellm_in_background`) is (or already
    was) started to keep retrying, with a cooldown between attempts, until
    it succeeds. A LATER call from any thread will see litellm become
    ready once that thread succeeds. Callers are expected to catch this
    (an ordinary ``except Exception`` already does, unmodified) and use
    their own existing fallback — not an error to surface to an operator.
    """


def is_litellm_ready() -> bool:
    """Non-blocking read: has the first-ever ``import litellm`` (and its
    one-time setup) already finished, successfully, in this process?

    Unlike :func:`ensure_litellm_ready`, this never imports litellm and
    never blocks — the same unlocked-read argument that function's own
    fast path already relies on (GIL-atomic single-variable read) applies
    here; this just exposes that same read publicly for the warming
    thread below to poll.
    """
    return _litellm_ready


def _litellm_warm_worker() -> None:
    """Body of the ONE dedicated background thread: retries
    :func:`ensure_litellm_ready` — unmodified, so it participates in the
    SAME `_ready_registry` ownership every other caller uses; if some
    other call (e.g. a no-fallback site's blocking wait) wins ownership of
    a given attempt first, this thread just observes `_litellm_ready` flip
    via that attempt instead of redundantly repeating it — until litellm
    becomes ready, then exits.

    No cooldown/sleep logic of its OWN between real attempts: axis②
    (`_LITELLM_IMPORT_COOLDOWN_SECONDS`, this module's own chokepoint-level
    fix above) already makes `ensure_litellm_ready()` self-regulate its own
    retry cadence — a call during the cooldown returns `None` immediately
    without attempting a real import, so polling it here every
    `_LITELLM_WARM_POLL_SECONDS` costs nothing while in cooldown and only
    triggers a genuine re-attempt once the cooldown naturally elapses.
    Duplicating that cooldown here would just be the same rate limit
    enforced twice.
    """
    import time
    while not _litellm_ready:
        ensure_litellm_ready()  # self-regulates via its own cooldown (see above)
        if _litellm_ready:
            return
        time.sleep(_LITELLM_WARM_POLL_SECONDS)


def warm_litellm_in_background() -> None:
    """Idempotently ensure the ONE dedicated background thread
    (:func:`_litellm_warm_worker`) is (or already is) running, without
    blocking the calling thread even briefly.

    Safe to call from anywhere, any number of times, on any thread: once
    litellm is ready this is a single cheap attribute read; before that,
    `Thread.is_alive()` makes every call after the first a no-op. The only
    possible race is two threads BOTH seeing no thread alive yet and both
    starting one — harmless (both do the exact same idempotent work, and
    they still rendezvous on the SAME `_ready_registry` Future inside
    `ensure_litellm_ready`), never an incorrect outcome, and the thread
    exits on its own once litellm becomes ready — nothing lingers for the
    rest of the process.
    """
    global _litellm_warm_thread
    if _litellm_ready:
        return
    if _litellm_warm_thread is not None and _litellm_warm_thread.is_alive():
        return
    _litellm_warm_thread = threading.Thread(
        target=_litellm_warm_worker, name="reyn-litellm-warm", daemon=True,
    )
    _litellm_warm_thread.start()


def ensure_litellm_ready_or_defer() -> "Any":
    """Non-blocking alternative to :func:`ensure_litellm_ready`, for call
    sites with a safe, cheap fallback for "no answer yet" — see this
    module's own PR-2 section comment above for which sites qualify and
    why (`llm.py` / `litellm_provider.py` do NOT — they keep calling the
    blocking :func:`ensure_litellm_ready` and are unaffected by this
    function's existence).

    Already-ready case: returns the module immediately (cheap — delegates
    to :func:`ensure_litellm_ready`'s own fast path). NOT-yet-ready case:
    never imports litellm on the calling thread — ensures the one
    dedicated background thread is (or already is) retrying, then raises
    :class:`LitellmWarmingInBackgroundError` immediately so the caller's
    own pre-existing ``except Exception`` reaches its fallback with no
    wait paid, regardless of which thread called this.
    """
    if _litellm_ready:
        return ensure_litellm_ready()
    warm_litellm_in_background()
    raise LitellmWarmingInBackgroundError(
        "litellm is not yet importable in this process; a background "
        "thread is retrying it (or a recent attempt failed and is "
        "cooling down) — use the fallback for this call.",
    )


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
