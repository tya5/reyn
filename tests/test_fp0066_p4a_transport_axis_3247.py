"""Tier 1: FP-0066 P4a — Transport axis + valid-(scheme,transport) registry (#3247).

Internal, behavior-preserving (per the P4 firm, issue #3247): introduces
``reyn.tools.transport.Transport`` (the two implemented values,
``tool_calls`` / ``content_fence`` — ``structured_output`` deliberately absent,
deferred to #3249) + the explicit valid-(scheme,transport) registry (firm §2 J1)
that maps a (presentation-scheme, transport) pair to the ``_SCHEMES`` name that
implements it TODAY, fail-closed on any unregistered cell.

Pins:
  - the registry's census is exactly the known-implemented cells (vacuity
    guard: non-empty + no more/less than the census);
  - an unregistered cell (e.g. ``retrieval`` x ``content_fence``) raises a
    legible ``ValueError`` rather than being silently accepted — the
    fail-closed guard is load-bearing (strip-falsify: a naive permissive
    lookup, the shape the guard replaces, WOULD silently accept it);
  - CodeAct is the (enumerate-all, content_fence) cell's live implementation.
    P4c (#3247) relocated its ``_SCHEMES`` registration off the bare name
    ``"codeact"`` onto ``CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`` (clean
    break — ``"codeact"`` no longer resolves at all); this module was
    updated in the SAME PR that landed that rename (P4a's own hard rule:
    a doc/test describing a mechanism goes stale the moment the mechanism
    changes). The same rule moved this module's unregistered witness off
    ``(category, content_fence)`` when #3376 P2 registered that cell — the
    witness has to be a cell that is actually unregistered, or the arm
    stops testing fail-closedness and starts failing for the opposite
    reason.

No mocks: ``Transport`` / the registry are pure data + functions, no
collaborators to fake.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Ensure the built-in schemes (codeact / enumerate-all / retrieval /
# universal-category) are registered — importing the package runs each
# module's self-register-on-import side effect (mirrors test_scheme_self_register_1608.py).
import reyn.tools.schemes  # noqa: E402,F401
from reyn.tools.scheme import get_scheme  # noqa: E402
from reyn.tools.transport import (  # noqa: E402
    CONTENT_FENCE_CATEGORY_SCHEME_NAME,
    CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
    Transport,
    resolve_scheme_for_transport,
    valid_scheme_transport_pairs,
)

_CENSUS: "dict[tuple[str, Transport], str]" = {
    ("category", Transport.TOOL_CALLS): "universal-category",
    ("category", Transport.CONTENT_FENCE): CONTENT_FENCE_CATEGORY_SCHEME_NAME,
    ("enumerate-all", Transport.TOOL_CALLS): "enumerate-all",
    ("enumerate-all", Transport.CONTENT_FENCE): CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
    ("retrieval", Transport.TOOL_CALLS): "retrieval",
}


def test_transport_has_only_the_two_implemented_values() -> None:
    """Tier 1: Transport carries exactly tool_calls/content_fence — no reserved
    structured_output slot (firm §3 P4d — YAGNI, deferred to #3249)."""
    assert {t.value for t in Transport} == {"tool_calls", "content_fence"}
    assert not hasattr(Transport, "STRUCTURED_OUTPUT")


def test_valid_pair_registry_census_matches_the_known_cells() -> None:
    """Tier 1: the valid-pair registry enumerates exactly the census-verified
    cells (FP-0066 issue #3247, P4 firm §1; ``(category, content_fence)`` added
    by #3376 P2) — vacuity guard (non-empty) folded into the exact-match
    assertion (a registry with 0 or with an unlisted entry fails this)."""
    pairs = valid_scheme_transport_pairs()
    assert pairs, "valid-pair registry must not be vacuous"
    assert set(pairs) == set(_CENSUS)


@pytest.mark.parametrize(
    ("scheme", "transport", "expected"),
    [(s, t, expected) for (s, t), expected in _CENSUS.items()],
)
def test_valid_pairs_resolve_to_the_census_scheme(
    scheme: str, transport: Transport, expected: str,
) -> None:
    """Tier 1: each valid cell resolves to its census-verified _SCHEMES name,
    and that name is actually a registered, live scheme."""
    resolved = resolve_scheme_for_transport(scheme, transport)
    assert resolved == expected
    assert get_scheme(resolved) is not None


def test_codeact_is_the_enumerate_all_content_fence_cell_and_codeact_name_is_gone() -> None:
    """Tier 1: (enumerate-all, content_fence) resolves to the registered
    CodeAct-implementing scheme instance under its P4c-relocated name — and
    the bare ``"codeact"`` name no longer resolves at all (clean-break, #3247
    P4c): it is reachable ONLY via this (scheme, transport) pair, never as an
    independent ``_SCHEMES`` name."""
    resolved_name = resolve_scheme_for_transport("enumerate-all", Transport.CONTENT_FENCE)
    assert resolved_name == CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME
    assert get_scheme(resolved_name) is not None
    assert get_scheme("codeact") is None


@pytest.mark.parametrize(
    ("scheme", "transport"),
    [
        ("retrieval", Transport.CONTENT_FENCE),
        ("no-such-presentation", Transport.TOOL_CALLS),
        ("no-such-presentation", Transport.CONTENT_FENCE),
    ],
)
def test_unregistered_cell_fails_closed(scheme: str, transport: Transport) -> None:
    """Tier 1: an unregistered (scheme,transport) cell raises a legible
    ValueError — NOT a silent fallback to some default scheme. Mirrors #3026
    "enumeration is not resolution": splitting into two axes does not widen
    the resolvable set past what is explicitly registered (firm §2 J1)."""
    with pytest.raises(ValueError, match=r"no \(scheme, transport\) registration"):
        resolve_scheme_for_transport(scheme, transport)


def test_strip_falsify_fail_closed_guard_is_load_bearing() -> None:
    """Tier 1: strip-falsify — a naive permissive lookup — the shape
    ``resolve_scheme_for_transport`` deliberately does NOT use (a plain
    ``dict.get`` with a silent fallback default) — WOULD silently accept the
    unregistered ``(retrieval, content_fence)`` cell and hand back a wrong
    scheme name instead of raising. This demonstrates the real function's
    ``raise`` is load-bearing: strip it back to a ``.get(..., default)`` shape
    and the failure mode silently degrades from a loud error to a wrong-scheme
    selection — exactly the "configuration doesn't do what it says" trap J1
    guards against."""
    registry = {pair: resolve_scheme_for_transport(*pair) for pair in _CENSUS}
    unregistered = ("retrieval", Transport.CONTENT_FENCE)
    assert unregistered not in registry

    naive_silent_lookup = registry.get(unregistered, "enumerate-all")
    assert naive_silent_lookup == "enumerate-all"  # the un-guarded shape silently "succeeds"

    with pytest.raises(ValueError):
        resolve_scheme_for_transport(*unregistered)  # the real, guarded function does not
