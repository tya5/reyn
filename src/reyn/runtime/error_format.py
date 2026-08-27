"""Friendly classification of router / LLM-call failure exceptions.

The chat layer catches a broad ``except Exception`` around the router
loop because failures arrive from many providers via litellm and the
exact import path is provider-specific. Without classification, every
failure surfaces as ``router failed: <RawExceptionRepr>`` which leaks
provider internals (multi-line JSON blobs, raw exception class names)
and gives the user no path forward.

This module maps the common patterns to a short, actionable prefix:

    [rate limit]      provider 429 (per-request) — wait a moment and retry
    [usage limit]     provider 429 (usage-window/plan quota exhausted,
                      #5256) — a wait, not a retry-worthy failure
    [provider error]  provider 5xx / connection failure — retry shortly
    [auth error]      bad / missing API key
    [timeout]         client- or server-side timeout
    [bad request]     malformed prompt or oversized context
    [budget exceeded] reyn-side budget cap (BudgetExceeded)

The classifier inspects ``type(exc).__name__`` and falls back to a
``status_code`` attribute when present. It deliberately does NOT
import litellm or httpx — keeping this layer free of provider deps
means new provider exceptions don't have to be re-imported here as
long as their class names follow common conventions.

#5256: a 429 is not one failure — a per-request rate limit (shrinking
the input can genuinely help avoid it) and a usage-window/plan quota
exhaustion (shrinking cannot: the window resets on a clock, not on
input size) both surface as ``RateLimitError`` today. ``is_quota_
exhausted_error``/``quota_reset_seconds`` below read the STRUCTURED
provider body litellm exposes on ``exc.body`` (a dict) to tell the two
apart and surface a wait time — never the provider's own free-text
message (#5257: a provider/SDK changing its own wording is not reyn's
bug to catch or reyn's string to own).
"""
from __future__ import annotations

from reyn.runtime.budget.budget import BudgetExceeded

#: #5256: the ONE provider ``type`` value observed to mean "usage-window /
#: plan quota exhausted, waiting is the only remedy" (reyn-self, litellm/
#: Anthropic-proxy, 2026-08-24/25/27 — see issue #5256's own real
#: transcripts). A provider that expresses the same failure class under a
#: DIFFERENT ``type`` string is a disclosed gap, not a silently-assumed
#: absence — this is a positive allowlist, not an attempt to enumerate
#: every provider's vocabulary.
_QUOTA_EXHAUSTED_BODY_TYPE = "usage_limit_reached"


def is_quota_exhausted_error(exc: BaseException) -> bool:
    """True when *exc* is a provider usage-window/plan quota exhaustion —
    genuinely time-based and input-size-independent (#5256), never
    something shrinking the request can fix. Distinguishes this from an
    ordinary per-request rate limit, which a ``RateLimitError`` ALSO
    reports and which shrinking the input CAN help avoid.

    Checked via the structured field litellm exposes on ``exc.body``
    (the provider's own parsed error dict), never ``str(exc)`` — matching
    the provider's own free-text message is a responsibility that belongs
    to the provider, not reyn (#5257's own precedent for this exact
    class of mistake)."""
    body = getattr(exc, "body", None)
    return isinstance(body, dict) and body.get("type") == _QUOTA_EXHAUSTED_BODY_TYPE


def quota_reset_seconds(exc: BaseException) -> "int | None":
    """The provider's own ``resets_in_seconds`` field, if *exc* carries one
    — how long until the exhausted quota window resets. ``None`` if absent
    or not an int.

    Deliberately does NOT read ``resets_at`` (an absolute epoch timestamp
    the SAME provider also returns): a real occurrence (#5256 issue
    thread) computed a wait time from ``resets_at`` that was wrong by
    ~4 days against the account's actual recovery — the field does not
    reliably predict recovery time, so no promise is derived from it here.
    ``resets_in_seconds`` is used as reported (a duration, not a promise);
    the caller frames it as "reported by the provider", not a guarantee."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    val = body.get("resets_in_seconds")
    return val if isinstance(val, int) else None


def classify_router_error(exc: BaseException) -> str:
    """Return a one-line user-facing description of *exc*.

    Format: ``"router failed: [<bucket>] <detail> • <hint>"``. Falls back
    to ``"router failed: <repr>"`` when no bucket matches.
    """
    if isinstance(exc, BudgetExceeded):
        return (
            f"router failed: [budget exceeded] {exc.dimension}: {exc.detail} "
            "• try /budget reset or wait for the next period"
        )

    name = type(exc).__name__
    msg = str(exc) or name
    code = getattr(exc, "status_code", None)

    bucket = _bucket_for(name, code, exc)
    if bucket is None:
        return f"router failed: {msg}"
    label, hint = bucket
    # Trim the verbose part — keep just the first sentence / line so the
    # user-facing version doesn't carry multi-line JSON from the provider.
    short = msg.splitlines()[0]
    if len(short) > 200:
        short = short[:200] + "…"
    return f"router failed: [{label}] {short} • {hint}"


def _bucket_for(name: str, code: "int | None", exc: BaseException) -> "tuple[str, str] | None":
    """Return a ``(label, hint)`` pair for the matched bucket, or None.

    Class-name matching is intentionally substring-based so subclasses
    (``RateLimitError``, ``OpenAIRateLimitError``, …) all fall into the
    same bucket. ``status_code`` is the secondary signal for providers
    that wrap everything in a single ``APIError`` class — and it's
    checked BEFORE the generic-name buckets so a ``WrappedAPIError``
    with ``status_code=400`` lands in [bad request], not [provider error].
    """
    # Rate limit — 429
    if "RateLimit" in name or code == 429:
        # #5256: a quota exhaustion (waiting is the only remedy) gets a
        # distinct, decision-enabling hint from a plain per-request rate
        # limit — "wait a moment" understates a multi-hour usage window.
        if is_quota_exhausted_error(exc):
            resets = quota_reset_seconds(exc)
            if resets is not None:
                return (
                    "usage limit",
                    f"provider reports it resets in {resets}s — no need to "
                    "reply, it will recover on its own",
                )
            return (
                "usage limit",
                "provider usage limit reached — it will recover on its own, "
                "no fixed wait reported",
            )
        return "rate limit", "wait a moment then retry"
    # Auth — 401 / 403
    if "Authentication" in name or "PermissionDenied" in name or code in (401, 403):
        return "auth error", "check your API key for the active provider"
    # Timeout (client or server)
    if "Timeout" in name or "APITimeoutError" in name or code == 408:
        return "timeout", "retry or check your network"
    # Connection — DNS / TCP / TLS
    if "Connection" in name or "ConnectError" in name:
        return "connection error", "check network connectivity and retry"
    # Status-code-driven precedence: 4xx (other than auth/rate) → bad request;
    # 5xx → provider error. This runs BEFORE the class-name fallback so a
    # wrapper class named ``WrappedAPIError`` with status_code=400 lands in
    # the right bucket.
    if isinstance(code, int):
        if code == 400 or 410 <= code < 500:
            return "bad request", "check the prompt / model name / context size"
        if 500 <= code < 600:
            return "provider error", "retry or check provider status"
    # Class-name fallback for providers without a status_code attribute.
    if "BadRequest" in name or "InvalidRequest" in name:
        return "bad request", "check the prompt / model name / context size"
    if (
        "ServiceUnavailable" in name
        or "InternalServerError" in name
        or "APIError" in name
    ):
        return "provider error", "retry or check provider status"
    return None


__all__ = ["classify_router_error", "is_quota_exhausted_error", "quota_reset_seconds"]
