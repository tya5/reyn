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

#: #1676: env vars whose values are API secrets — scrubbed from any
#: freeform provider text (error body, exception message) so a captured
#: 4xx/401 never leaks a key, regardless of whether the key reached
#: litellm via an explicit kwarg or an environment variable it read
#: itself.
SECRET_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "LITELLM_API_KEY", "AZURE_API_KEY",
)


def collect_secret_values(base_kwargs: dict) -> list[str]:
    """The concrete secret VALUES to scrub — the secret-keyed kwargs (e.g.
    the proxy-injected ``api_key``) + known API-key env vars, unconditionally
    (a provider call may read its key straight from the environment without
    it ever passing through *base_kwargs*). Scrubbing the actual value is
    precise (vs. guessing patterns in arbitrary provider output)."""
    vals: list[str] = []
    for k, v in base_kwargs.items():
        if isinstance(v, str) and v and any(h in k.lower() for h in SECRET_KWARG_HINTS):
            vals.append(v)
    for env in SECRET_ENV_VARS:
        val = os.environ.get(env)
        if val:
            vals.append(val)
    return vals


def scrub_secrets(value: object, secrets: list[str]) -> object:
    """Replace any known secret VALUE occurring in a string with a marker,
    recursing through dict/list structure to reach every string leaf — same
    contract as the function #3830/#4259 established in ``llm.py`` (moved
    here, not duplicated, so both call sites share one implementation)."""
    if not secrets:
        return value
    if isinstance(value, str):
        for s in secrets:
            if s:
                value = value.replace(s, "***REDACTED***")
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
