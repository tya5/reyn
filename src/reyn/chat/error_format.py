"""Friendly classification of router / LLM-call failure exceptions.

The chat layer catches a broad ``except Exception`` around the router
loop because failures arrive from many providers via litellm and the
exact import path is provider-specific. Without classification, every
failure surfaces as ``router failed: <RawExceptionRepr>`` which leaks
provider internals (multi-line JSON blobs, raw exception class names)
and gives the user no path forward.

This module maps the common patterns to a short, actionable prefix:

    [rate limit]      provider 429 — wait a moment and retry
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
"""
from __future__ import annotations

from reyn.budget.budget import BudgetExceeded


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

    bucket = _bucket_for(name, code)
    if bucket is None:
        return f"router failed: {msg}"
    label, hint = bucket
    # Trim the verbose part — keep just the first sentence / line so the
    # user-facing version doesn't carry multi-line JSON from the provider.
    short = msg.splitlines()[0]
    if len(short) > 200:
        short = short[:200] + "…"
    return f"router failed: [{label}] {short} • {hint}"


def _bucket_for(name: str, code: int | None) -> tuple[str, str] | None:
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


__all__ = ["classify_router_error"]
