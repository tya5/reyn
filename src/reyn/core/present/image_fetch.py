"""Fetch resolution for `present`'s `image` component's `src` URL (#3846 ①).

Owner ruling (#3846, 2026-08-09): image delivery is C — `src` is not restricted
to reyn-issued URIs by default (unrestricted, neutralize-only); an operator can
opt into a scheme allowlist via `chat.image_url_schemes` (empty/unset = no
restriction). "Even without the bytes, the record of what was presented is
enough" is the owner's stated rationale — the value is the audit record, not
reyn proxying the bytes.

Deliberately client-side (this module), not server-side (`core/op_runtime`):
architect's measurement (#3846) found AG-UI has no byte-serving route (its 3
endpoints are all JSON/SSE), so a remote client resolves `src` itself, the same
way the local TUI does — this module is that one resolution path both share
(`present`'s `src` slot is written by the model, per `binding.py`'s
`_render_text_slot`, so it is exactly the LLM-supplied class
`build_async_http_client(pin_ssrf=True)` names itself for).

Kept OUTSIDE `interfaces/repl/present_renderer.py` deliberately: that module's
own docstring declares it "Pure: ... No I/O" (FP-0054 PR-B invariant) — a
render function must never itself decide to fetch. Callers resolve `src` here
FIRST (async, bounded, cacheable) and hand the *result* into the pure render
path as an already-decided value.

V1 does not follow redirects across a scheme downgrade/upgrade beyond
`httpx`'s own default handling, and does not implement web_fetch's manual
per-hop `_gate_hop` beyond what `PinnedAsyncHTTPTransport` itself does per
request — its own docstring states every httpx-internal redirect hop calls the
transport again independently, so `follow_redirects=True` stays SSRF-safe here
without reimplementing web_fetch's hop loop; the difference from web_fetch is
scope (present's image fetch is display-only — no manual redirect gate for a
concern web_fetch has, like MIME/robots policy, that doesn't apply here) not
resolution.
"""
from __future__ import annotations

from dataclasses import dataclass

# Practical fetchable schemes — the only two an httpx client can dial at all.
# `allowed_schemes` (the opt-in restriction) can only narrow WITHIN this set,
# never widen it: passing e.g. "file" in the allowlist still errors here.
_FETCHABLE_SCHEMES = frozenset({"http", "https"})

#: Bounded so `present`'s image fetch can never itself become a hang — the
#: caller (a render pass) is on a UX-visible clock, unlike an agent-driven
#: `web_fetch` call the user has already accepted waiting on.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: A display image, not an LLM context payload — smaller than web_fetch's own
#: cap is reasonable, but this is a starting number, not a measured one.
DEFAULT_MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class ImageResolution:
    """The settled outcome of one `src` resolution — cached by the presenter
    layer (keyed by `src`) so a render pass is a pure dict lookup, never a
    fetch. Exactly one of ok=True (with `body`/`content_type`) or ok=False
    (with `error`) is meaningful at a time; the unused fields carry their
    empty default rather than `None` so callers doing `f"{r.error}"`-style
    formatting need no extra `or ""` guard."""

    ok: bool
    body: bytes = b""
    content_type: str = ""
    error: str = ""


class ImageFetchError(Exception):
    """Raised for any resolution failure — bad scheme, timeout, oversize
    response, or the underlying HTTP request itself failing. The caller
    (presenter-layer) is expected to catch this and render a failure state,
    never let it propagate as an unhandled render crash."""


async def fetch_image_bytes(
    src: str,
    *,
    allowed_schemes: "list[str] | None" = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> "tuple[bytes, str]":
    """Fetch `src`, returning `(body_bytes, content_type)`.

    Raises :class:`ImageFetchError` on any failure. `allowed_schemes` is the
    opt-in `chat.image_url_schemes` narrowing (owner ruling C): `None` or an
    empty list means no restriction beyond `_FETCHABLE_SCHEMES` itself (both
    http and https allowed); a non-empty list restricts to exactly those
    schemes (e.g. `["https"]` to reject plain http).

    Always routes through `build_async_http_client(pin_ssrf=True)` —
    unconditional, not itself an opt-in: `src` is written by the model
    (`present`'s blueprint is LLM-authored), the same trust class
    `_network.py` names `build_async_http_client`'s `pin_ssrf` for.
    """
    from urllib.parse import urlparse

    parsed = urlparse(src)
    scheme = parsed.scheme.lower()
    if scheme not in _FETCHABLE_SCHEMES:
        raise ImageFetchError(
            f"cannot fetch {src!r}: scheme {scheme!r} is not http/https"
        )
    if allowed_schemes and scheme not in allowed_schemes:
        raise ImageFetchError(
            f"cannot fetch {src!r}: scheme {scheme!r} is not in the "
            f"configured chat.image_url_schemes allowlist {list(allowed_schemes)!r}"
        )

    import httpx

    from reyn._network import build_async_http_client
    from reyn._ssrf_guard import SSRFBlocked

    try:
        async with build_async_http_client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "reyn/1.0"},
            pin_ssrf=True,
            egress="present_image",
        ) as client:
            async with client.stream("GET", src) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                chunks: "list[bytes]" = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageFetchError(
                            f"cannot fetch {src!r}: response exceeds the "
                            f"{max_bytes}-byte cap"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    except SSRFBlocked as exc:
        # The pin's own denial (loopback / link-local / cloud-metadata /
        # RFC1918 without operator opt-in) — a policy reject, not a network
        # failure, but the presenter layer needs exactly ONE exception type
        # to catch, not this plus httpx's hierarchy separately.
        raise ImageFetchError(f"cannot fetch {src!r}: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise ImageFetchError(
            f"cannot fetch {src!r}: request timed out after {timeout}s"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ImageFetchError(
            f"cannot fetch {src!r}: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise ImageFetchError(f"cannot fetch {src!r}: {exc}") from exc
