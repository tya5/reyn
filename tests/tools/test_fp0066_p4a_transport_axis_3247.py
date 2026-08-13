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
  - an unregistered cell raises a legible ``ValueError`` rather than being
    silently accepted — the fail-closed guard is load-bearing (strip-falsify: a
    naive permissive lookup, the shape the guard replaces, WOULD silently
    accept it). ★ The witness is ``NOT_A_PRESENTATION``, a name from OUTSIDE
    the presentation axis, not an unregistered cell of a real presentation:
    this module burned through two of the latter in one arc (see below);
  - CodeAct is the (enumerate-all, content_fence) cell's live implementation.
    P4c (#3247) relocated its ``_SCHEMES`` registration off the bare name
    ``"codeact"`` onto ``CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME`` (clean
    break — ``"codeact"`` no longer resolves at all); this module was
    updated in the SAME PR that landed that rename (P4a's own hard rule:
    a doc/test describing a mechanism goes stale the moment the mechanism
    changes).

★ **Why the witness is no longer a cell.** The same rule moved this module's
unregistered witness off ``(category, content_fence)`` when #3376 P2 registered
that cell, and then off ``(retrieval, content_fence)`` when P3 registered THAT
one. Neither pair was ever forbidden — both were legal combinations that had not
arrived yet, in an arc whose stated purpose was to make them arrive. A witness
drawn from the space the system is extending into is therefore dated on the day
it is written. #3376 P3 replaced it with ``NOT_A_PRESENTATION``, which is not a
value of the presentation axis at all and so cannot be registered by any future
cell; ``test_the_negative_example_is_outside_the_namespace`` below turns that
property into a checked claim rather than a comment.

No mocks: ``Transport`` / the registry are pure data + functions, no
collaborators to fake.
"""
from __future__ import annotations

import sys

import pytest

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
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
    CONTENT_FENCE_RETRIEVAL_SCHEME_NAME,
    Transport,
    resolve_scheme_for_transport,
    valid_scheme_transport_pairs,
)
from tests._support.tool_use_negative_examples import NOT_A_PRESENTATION  # noqa: E402

_CENSUS: "dict[tuple[str, Transport], str]" = {
    ("category", Transport.TOOL_CALLS): "universal-category",
    ("category", Transport.CONTENT_FENCE): CONTENT_FENCE_CATEGORY_SCHEME_NAME,
    ("enumerate-all", Transport.TOOL_CALLS): "enumerate-all",
    ("enumerate-all", Transport.CONTENT_FENCE): CONTENT_FENCE_ENUMERATE_ALL_SCHEME_NAME,
    ("retrieval", Transport.TOOL_CALLS): "retrieval",
    ("retrieval", Transport.CONTENT_FENCE): CONTENT_FENCE_RETRIEVAL_SCHEME_NAME,
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


def test_the_negative_example_is_outside_the_namespace() -> None:
    """Tier 1: the witness used below cannot become registered.

    This is the property that makes ``NOT_A_PRESENTATION`` a durable negative
    example rather than a dated one, so it is asserted rather than asserted-by-
    comment. It is also the closest thing to a gate available here: which literal
    in a test is a negative example cannot be told from its syntax (a negative
    example is written exactly like a positive one), so the marker is that the
    name is imported from ``tests._support.tool_use_negative_examples`` — and
    what this arm checks is that the marked name is genuinely off-axis."""
    presentations = {scheme for scheme, _ in valid_scheme_transport_pairs()}
    assert presentations, "no presentation names — this arm would inspect nothing"
    assert NOT_A_PRESENTATION not in presentations, (
        f"{NOT_A_PRESENTATION!r} is now a real presentation name, so every arm "
        "using it as a negative example has silently become an assertion about a "
        "REGISTERED cell. Choose another off-axis name; do not fall back to an "
        "unregistered cell of a real presentation (that is what expired twice in "
        "the #3376 arc)."
    )


@pytest.mark.parametrize("transport", list(Transport))
def test_unregistered_cell_fails_closed(transport: Transport) -> None:
    """Tier 1: an unregistered (scheme,transport) cell raises a legible
    ValueError — NOT a silent fallback to some default scheme. Mirrors #3026
    "enumeration is not resolution": splitting into two axes does not widen
    the resolvable set past what is explicitly registered (firm §2 J1).

    Parametrized over EVERY transport, from the enum: the refusal is a property
    of the presentation name, so it must not depend on which transport happens
    to be paired with it."""
    with pytest.raises(ValueError, match=r"no \(scheme, transport\) registration"):
        resolve_scheme_for_transport(NOT_A_PRESENTATION, transport)


def test_strip_falsify_fail_closed_guard_is_load_bearing() -> None:
    """Tier 1: strip-falsify — a naive permissive lookup — the shape
    ``resolve_scheme_for_transport`` deliberately does NOT use (a plain
    ``dict.get`` with a silent fallback default) — WOULD silently accept an
    unregistered cell and hand back a wrong scheme name instead of raising. This
    demonstrates the real function's ``raise`` is load-bearing: strip it back to
    a ``.get(..., default)`` shape and the failure mode silently degrades from a
    loud error to a wrong-scheme selection — exactly the "configuration doesn't
    do what it says" trap J1 guards against.

    The unregistered pair is built from ``NOT_A_PRESENTATION`` for the reason in
    the module docstring: the two real cells this arm used before were legal
    combinations awaiting implementation, and each stopped being unregistered
    inside the same arc."""
    registry = {pair: resolve_scheme_for_transport(*pair) for pair in _CENSUS}
    unregistered = (NOT_A_PRESENTATION, Transport.CONTENT_FENCE)
    assert unregistered not in registry

    naive_silent_lookup = registry.get(unregistered, "enumerate-all")
    assert naive_silent_lookup == "enumerate-all"  # the un-guarded shape silently "succeeds"

    with pytest.raises(ValueError):
        resolve_scheme_for_transport(*unregistered)  # the real, guarded function does not
