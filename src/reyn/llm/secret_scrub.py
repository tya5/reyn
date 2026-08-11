"""#1676/#3830: the ONE shared boundary for scrubbing secrets out of a
litellm-constructed exception, applied where reyn first touches it.

## The class this closes

litellm — not reyn — constructs the exception a failed `acompletion`/
`aembedding` call raises: its `.args`/`.message` are built FROM the
provider's own HTTP response text, so a 401 that quotes the credential it
rejected puts that credential straight into the exception's own `str()`/
`repr()`. #3830 measured 3 independent consumers of that unscrubbed
`str(exc)`/`repr(exc)` reachable from one `except Exception as exc:` in
`Session._run_router_loop` (a TUI-visible outbox message, a second P6
audit-event kind, and `.reyn/logs/reyn.log` via `logger.exception`) — and
#3830's own history is a cautionary tale about scrubbing at the SINK
instead of the SOURCE: an earlier fix (#4259) scrubbed exactly one sink
(`llm_request_error`'s own `detail` dict) and the other 3 were still
open the same night. Scrubbing every future consumer individually has no
natural end — the same "deny-list needs a new entry every time" shape
CLAUDE.md's `#4327` band already rejected for a doc gate.

**The fix scrubs the exception OBJECT itself, once, at the boundary
where reyn first receives it from litellm** — `scrub_exception_in_place`
below. Every consumer downstream (`str()`, `repr()`, `logger.exception`'s
traceback formatting, a sink nobody has written yet) reads already-safe
text automatically, because there is nothing left to scrub by the time
any of them see it. This is the reason it is a function in its own
module rather than a private helper duplicated in `llm.py` and
`data/embedding/litellm_provider.py`: **one shared implementation, two
call sites** (lead-coder's #3830 ruling) — writing the same scrub logic
twice means the next fix only reaches the copy someone remembers to
touch.

## What this does NOT scrub

`exc.response` (an `httpx.Response`, when litellm attaches one) is left
alone — `.text` is a read-only property computed from a private
`_content` attribute, and mutating a third-party object's private state
is a worse trade than the problem it would solve. The 2 sinks this
module exists for (`str(exc)`/`repr(exc)`, which drive the exception's
own message/args) never read `.response.text` directly; the ONE place
that does (`llm.py`'s `_emit_llm_request_error`, building the
`provider_response` audit field) already scrubs it locally, unaffected
by this module.
"""
from __future__ import annotations

import os

#: #1669/#1676: top-level kwarg keys whose VALUE is secret-like — the
#: proxy path injects ``api_key`` (see ``llm.py``'s ``proxy_kwargs``).
#: Substring match, case-insensitive. Single source for both the
#: llm_request-params redaction (``llm.py::_redact_llm_request_params``)
#: and this module's own secret-value collection.
SECRET_KWARG_HINTS: tuple[str, ...] = (
    "api_key", "api-key", "authorization", "secret", "token",
)

#: A candidate secret value shorter than this, or made of digits only, is
#: excluded from the scrub list — lead-coder's #3830 review catch: an env
#: var like ``TOKEN_LIMIT=4096`` matches ``SECRET_KWARG_HINTS`` on its NAME
#: (contains "token") but its VALUE is an ordinary short numeric setting,
#: not a credential. Blindly ``str.replace()``-ing every occurrence of
#: ``"4096"`` in a provider error message would corrupt legitimate
#: diagnostic text (a token count, a byte limit, a status code) that
#: happens to share those digits — a real correctness risk, not a
#: hypothetical. Real provider API keys (`sk-...`, `AIza...`, proxy
#: tokens, …) are uniformly well over this length and never purely
#: numeric, so this filter costs no real-secret coverage. Chosen by
#: reasoning about key-format lengths, not by inspecting any value this
#: process has ever held — see module docstring's own rule about not
#: outputting secret values, which extends to not using them to tune this
#: threshold either.
_MIN_SECRET_VALUE_LENGTH = 12

#: #4343: how many LEADING characters of a known secret value are also
#: tried as a standalone match — see :func:`scrub_secrets`'s "already
#: partially masked" fallback. Derived from :data:`_MIN_SECRET_VALUE_LENGTH`
#: itself (two-thirds of it, floor-divided: 12 * 2 // 3 == 8), the SAME
#: no-value-inspection discipline that constant's own docstring states —
#: chosen by reasoning about the trade-off shape, not by looking at what
#: any real provider's masking actually reveals:
#:   - too SHORT (close to a bare provider prefix like ``sk-``, 3-4 chars)
#:     risks matching ordinary text that happens to start the same way —
#:     the over-redaction failure mode, the false-positive twin of the
#:     ``TOKEN_LIMIT=4096`` problem the length filter above already guards.
#:   - too LONG (close to the FULL ``_MIN_SECRET_VALUE_LENGTH``) stops
#:     matching once an upstream masker (litellm's own exception
#:     construction, observed structurally in #4343 — not by value —
#:     to sometimes reveal only a short leading span before masking the
#:     rest) has already replaced the tail with its own marker characters.
#: Deriving it as a fraction of the existing threshold ties the two
#: constants together instead of adding an unrelated magic number, and
#: keeps both adjustable from one place if real-world masking behavior
#: (still not inspected here) is measured later and this needs revisiting.
_SECRET_PREFIX_LENGTH = _MIN_SECRET_VALUE_LENGTH * 2 // 3


def _looks_like_a_real_secret(value: str) -> bool:
    """False for a short and/or purely-numeric string — see
    :data:`_MIN_SECRET_VALUE_LENGTH`'s docstring for why this filter
    exists and how the threshold was chosen."""
    return len(value) >= _MIN_SECRET_VALUE_LENGTH and not value.isdigit()


def collect_secret_values(base_kwargs: dict) -> list[str]:
    """The concrete secret VALUES to scrub — every kwarg AND env var whose
    NAME matches :data:`SECRET_KWARG_HINTS`, filtered by
    :func:`_looks_like_a_real_secret`.

    The env-var side used to be a fixed 6-name enumeration
    (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, …) — the exact "deny-list
    needs a new entry every time a provider is added" shape this module's
    own docstring names as the class #3830 rejects for SINKS; applying it
    here, to the SOURCE side, was the same mistake once removed (lead-coder
    review, #4341). Scanning ``os.environ`` with the SAME name-predicate the
    kwargs side already uses means a provider this list never named
    (``MISTRAL_API_KEY``, a self-hosted proxy's own env var name, …) is
    covered by construction, not by remembering to add a line here.
    """
    vals: list[str] = []
    for k, v in base_kwargs.items():
        if (
            isinstance(v, str) and v
            and any(h in k.lower() for h in SECRET_KWARG_HINTS)
            and _looks_like_a_real_secret(v)
        ):
            vals.append(v)
    for k, v in os.environ.items():
        if (
            v
            and any(h in k.lower() for h in SECRET_KWARG_HINTS)
            and _looks_like_a_real_secret(v)
        ):
            vals.append(v)
    return vals


def scrub_secrets(value: object, secrets: list[str]) -> object:
    """Replace any known secret VALUE (or, failing that, its known PREFIX)
    occurring in a string with a marker, recursing through dict/list
    structure to reach every string leaf — same contract as the function
    #3830/#4259 established in ``llm.py`` (moved here, not duplicated, so
    both call sites share one implementation).

    #4343: the full-value pass alone misses a secret that an upstream
    intermediary (litellm's own exception construction, for one measured
    case) has ALREADY partially masked by the time reyn's exact-match
    ``str.replace`` runs — the full known value is no longer a literal
    substring of the text once its tail has been replaced by someone
    else's mask characters, so nothing matches and the marker never
    appears. The prefix pass is still "the actual value at hand", not
    pattern-guessing: it derives from the SAME known secret this function
    was already given, just a leading slice of it — never a guessed
    format like "anything starting with sk-" (this module's own docstring
    rejects that class of scrubbing).
    """
    if not secrets:
        return value
    if isinstance(value, str):
        for s in secrets:
            if s:
                value = value.replace(s, "***REDACTED***")
        for s in secrets:
            if s and len(s) >= _SECRET_PREFIX_LENGTH:
                value = value.replace(s[:_SECRET_PREFIX_LENGTH], "***REDACTED***")
        return value
    if isinstance(value, dict):
        return {k: scrub_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_secrets(v, secrets) for v in value]
    return value


def scrub_exception_in_place(exc: BaseException, base_kwargs: "dict | None" = None) -> BaseException:
    """Scrub *exc* — the object itself, not a sink's copy of its text — so
    every consumer of ``str(exc)``/``repr(exc)`` downstream sees already-safe
    output. Returns *exc* (mutated, same identity) for a fluent call at a
    ``raise scrub_exception_in_place(exc, ...)`` or ``except ... as exc:
    scrub_exception_in_place(exc, ...)`` site.

    Mutates ``.args`` (what ``BaseException.__str__``/``__repr__`` both
    read — this is what makes ``logger.exception``'s traceback formatting,
    an f-string ``f"{exc}"``, and a future sink nobody has written yet all
    automatically safe, with no per-sink scrub call to remember) plus the
    ``message``/``body`` attributes litellm's own exception classes commonly
    carry alongside ``.args`` (present on some provider exception types,
    read defensively via ``hasattr`` — absent on stdlib exceptions and on
    litellm classes that don't set them, a no-op there).

    A no-op (returns *exc* unchanged) when no secret values are configured
    — the common case outside a proxy/credential setup — so this costs
    nothing when there's nothing to scrub.
    """
    secrets = collect_secret_values(base_kwargs or {})
    if not secrets:
        return exc
    if exc.args:
        exc.args = tuple(
            scrub_secrets(a, secrets) if isinstance(a, (str, dict, list)) else a
            for a in exc.args
        )
    for attr in ("message", "body"):
        if hasattr(exc, attr):
            try:
                setattr(exc, attr, scrub_secrets(getattr(exc, attr), secrets))
            except Exception:  # noqa: BLE001 — a read-only/exotic attribute must never break error handling
                pass
    return exc
