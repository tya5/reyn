"""Axis contract — the "1 bit -> 3-tuple" enforcement claim (#2983 continuation
of stage 1's ``self_test.py``).

**The gap this closes.** The stage-1 contract (``self_test.py``) is "a backend
that names an axis must actually fire a deny on it" — one bit per axis. #3060
(the ``network=False`` NULL-addr ``sendto``/``recvfrom`` allowance for the
async self-pipe) produced two more claims that one bit cannot express, both
built bespoke for the network axis and inherited by nothing else:

1. **A narrow exception's boundary.** Once an axis carries a deliberate hole
   (network's NULL-addr allowance), "the deny fired" no longer distinguishes
   "the gate is intact" from "the gate is intact but the exception reopened
   egress". Only a probe aimed AT the exception's boundary — an ADDRESSED
   ``sendto`` under the same policy — can tell those apart.
2. **Reachable-for-purpose.** #3060's production failure was "every syscall
   probe is green and the server still hangs" — a real chunker FastMCP server
   that could not reach ``serving`` under ``network=False`` even though every
   individual deny/allow fired exactly as documented. "Did the deny fire" is
   structurally blind to that failure; only running the actual workload closes
   it.

So the full claim for an axis is three independent legs, not one bit:

    (a) DENY   — the axis's core deny actually fires
    (b) BOUNDARY — each declared exception's own probe proves it did not
                   reopen the axis (absent when the axis declares none)
    (c) WORKLOAD — the real workload the axis exists to gate reaches its
                   intended state under the axis's restriction

**Why this contract lives here, split from where it is enforced — the
two-layer design (firmed by architect co-vet, #2983).** The obvious move is
"every axis's ``enforcement_self_test`` call now runs all three legs for
every axis". That would be wrong: ``self_test.py`` documents, in
:func:`self_test.probe_network_enforcement`'s and
:func:`self_test.enforcement_self_test`'s own docstrings, that the network
probe is DELIBERATELY excluded from the cached suite every real backend
resolution calls, because that suite's blast radius is "every sandboxed op on
every host" — a probe bug there (a timeout, a host where bare ``socket()``
creation is itself blocked by something unrelated) silently falls EVERY op
back to :class:`NoopBackend`, not just that one axis. Folding this module's
richer contract into that production gate would widen that blast radius to
every future axis's three legs, which is the opposite of what #2983 is for
(a sandbox that was found "not sandboxing" must not gain a new way to
silently disable itself).

The firm: the CONTRACT (this module — what a fully-witnessed axis must
supply) is decoupled from WHERE it runs.

    | Layer                                    | What runs           | Blast radius |
    |-------------------------------------------|----------------------|--------------|
    | production gate (``self_test.enforcement_self_test``) | DENY leg only, write + spawn axes only, unchanged by this module | every sandboxed op, every host |
    | CI conformance (``tests/test_sandbox_axis_contract_2983.py``, Linux-only, same gating as ``sandbox_landlock_deny_gate.py``) | all 3 legs, all migrated axes, all backends | CI only |

This is not a new pattern: ``scripts/sandbox_landlock_deny_gate.py`` (#2983
stage 3) already runs real deny arms as a CI-only gate, never a production
one, for exactly this reason (a probe bug there fails a CI job, not a user's
sandbox). This module generalises that split to a typed per-axis contract
instead of a fixed arm list.

**Type-enforced continuation, not opt-in discipline.** Two fields have no
default value anywhere in this module:

- :attr:`AxisException.boundary_probe` — an ``AxisException`` cannot be
  constructed without naming the probe that proves it did not reopen the
  axis. An exception without a boundary probe is a ``TypeError`` at the call
  site, not a silently-unwitnessed hole.
- :attr:`AxisContract.exceptions` — a new axis registered here must say
  EXPLICITLY whether it has exceptions (an empty ``()``) or not; there is no
  default that would let a forgotten field read as "none".

Axes not yet migrated onto this contract (write, spawn, as of this PR) pass
:data:`NOT_MIGRATED` for every leg explicitly — a marker, not an absence — so
"not yet migrated" and "migrated but reads as empty" cannot be confused. See
:data:`AXIS_REGISTRY`'s own docstring for how the migration count is checked
so a partially-migrated axis cannot read as fully migrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .backend import SandboxBackend

# A probe's shape, shared by the deny leg and every exception's boundary
# probe: given a live backend, return None when the claim held (deny fired /
# boundary intact) or an operator-readable reason when it did not. Matches
# self_test.py's probe_enforcement / probe_subprocess_enforcement /
# probe_network_enforcement signature exactly, because this contract's deny
# leg IS meant to be satisfied by reusing those, not reimplementing them.
AxisProbe = Callable[["SandboxBackend"], "str | None"]


class WitnessStrength(Enum):
    """How strong a leg's evidence is for a given backend — a type-level place
    to WRITE DOWN an asymmetry, rather than leave it implicit.

    Concrete instance this exists for: the network axis's deny leg is
    ``BEHAVIORAL`` on seccomp (a real connect() attempt, refused, plus a
    negative witness that the exception did not reopen egress — #3060) but
    only ``PROFILE_TEXT`` on Seatbelt (the SBPL profile text is inspected for
    the expected rule; no real deny is attempted). That gap is not
    necessarily wrong — the SBPL-text -> ``sandbox-exec`` trust chain is far
    shorter than a seccomp-BPF filter's, so text inspection may be a
    defensible substitute — but until this field existed, the asymmetry was
    simply unwritten. Adding real behavioral witnessing on Seatbelt is
    explicitly OUT of this PR's scope (a separate issue): this field's job
    here is only to make the current asymmetry a recorded decision instead
    of an unnoticed gap.
    """

    BEHAVIORAL = "behavioral"
    PROFILE_TEXT = "profile_text"


class _NotMigratedMarker:
    """Sentinel meaning: this leg has not yet been migrated onto the 3-tuple
    contract. Explicit, never a default — every :class:`AxisContract` /
    :class:`AxisException` field must say what IT is, so a forgotten field
    cannot silently read the same as a deliberately-declared one."""

    def __repr__(self) -> str:  # pragma: no cover - repr only, no behavior
        return "NOT_MIGRATED"


#: The one instance of :class:`_NotMigratedMarker` — compare with ``is``,
#: never ``==`` (there is deliberately no ``__eq__``; identity is the only
#: correct comparison for a sentinel).
NOT_MIGRATED = _NotMigratedMarker()


@dataclass(frozen=True)
class AxisException:
    """A deliberately narrow hole in an axis's deny — #3060's NULL-addr
    ``sendto``/``recvfrom`` allowance (for the async-runtime self-pipe) is the
    founding instance this type generalises.

    ``boundary_probe`` has NO default value. Omitting it at construction is a
    ``TypeError``, not a silently-empty exception: an exception this contract
    cannot express without also naming the probe that proves it did not
    reopen the axis (in #3060's case: an ADDRESSED ``sendto`` — real UDP
    egress — must STILL be denied even though a connected, NULL-addr
    socketpair send/recv is allowed).
    """

    name: str
    boundary_probe: AxisProbe


@dataclass(frozen=True)
class AxisContract:
    """The full enforcement claim for one sandbox axis — three independent
    legs, not the one bit ``self_test.py``'s stage-1 contract checks. See the
    module docstring for what each leg means and why this type is deliberately
    NOT wired into the production gate.

    Every field is required (no defaults anywhere in this class), including
    ``exceptions``: a newly-registered axis that forgets to state its
    exceptions fails to construct at all, rather than reading as "this axis
    has none" by accident.

    An axis not yet migrated passes :data:`NOT_MIGRATED` for every one of the
    three legs (``deny_probe``, ``exceptions``, ``workload_test_id``) plus
    ``witness_strength`` — explicitly, so "not yet migrated" cannot be
    confused with "migrated but happens to be a no-op axis".
    """

    name: str
    deny_probe: "AxisProbe | _NotMigratedMarker"
    exceptions: "tuple[AxisException, ...] | _NotMigratedMarker"
    # A pytest node id ("path/to/test_file.py::test_name") rather than a live
    # callable, deliberately: the workload leg's real instance (#3060's
    # `test_chunker_server_reaches_serving_under_network_false`) is an async
    # pytest test wired to real fixtures (tmp_path, a real chonkie-backed
    # FastMCP server) and lives in tests/, which src/ must not import — this
    # module stays importable from production code without pulling tests/ in.
    # CI conformance resolves and asserts the id exists (see
    # tests/test_sandbox_axis_contract_2983.py) rather than invoking it
    # out of pytest's own fixture machinery.
    workload_test_id: "str | _NotMigratedMarker"
    # Per-backend name (e.g. "seccomp", "seatbelt") -> WitnessStrength for
    # this axis's deny leg. A dict, because strength can differ by backend
    # (see WitnessStrength's docstring for the network/seccomp-vs-seatbelt
    # instance this exists for).
    witness_strength: "dict[str, WitnessStrength] | _NotMigratedMarker"

    @property
    def is_migrated(self) -> bool:
        """True once every leg names something real — none of the four
        fields below is still :data:`NOT_MIGRATED`. Deliberately all-or-
        nothing: a partially-migrated axis (say, a deny_probe but
        NOT_MIGRATED exceptions) is exactly as unwitnessed on its missing
        legs as one with none migrated, so it must not read as "done"."""
        return NOT_MIGRATED not in (
            self.deny_probe,
            self.exceptions,
            self.workload_test_id,
            self.witness_strength,
        )


def _network_deny_probe(backend: "SandboxBackend") -> "str | None":
    """Deny leg for the network axis — reuses
    :func:`self_test.probe_network_enforcement` (#3030/#3060) rather than a
    new implementation. That probe already performs the axis's core deny
    check (a loopback ``connect()`` under ``network=False`` must be refused,
    arms 1-3) as well as this axis's declared exception's boundary (arms 4-5,
    reused again as :data:`_NETWORK_EXCEPTIONS`'s ``boundary_probe`` below) —
    it is one probe function doing two legs' worth of witnessing because the
    #3060 work that built it already interleaved them; splitting it into two
    separate call sequences would be a rewrite this PR deliberately avoids
    (see the module docstring's reuse rule)."""
    from .self_test import probe_network_enforcement

    return probe_network_enforcement(backend)


_NETWORK_EXCEPTIONS: "tuple[AxisException, ...]" = (
    AxisException(
        name="null_addr_socketpair_selfpipe",
        # Same underlying probe as the deny leg — see _network_deny_probe's
        # docstring: probe_network_enforcement's arms 4-5 ARE this boundary
        # witness (the connected-socketpair self-pipe must survive; an
        # ADDRESSED sendto must still be denied). Reused, not duplicated.
        boundary_probe=_network_deny_probe,
    ),
)


_NETWORK_CONTRACT = AxisContract(
    name="network",
    deny_probe=_network_deny_probe,
    exceptions=_NETWORK_EXCEPTIONS,
    workload_test_id=(
        "tests/test_sandbox_seccomp_network_3030.py::"
        "test_chunker_server_reaches_serving_under_network_false"
    ),
    witness_strength={
        "seccomp": WitnessStrength.BEHAVIORAL,
        "seatbelt": WitnessStrength.PROFILE_TEXT,
    },
)

_WRITE_CONTRACT = AxisContract(
    name="write",
    deny_probe=NOT_MIGRATED,
    exceptions=NOT_MIGRATED,
    workload_test_id=NOT_MIGRATED,
    witness_strength=NOT_MIGRATED,
)

_SPAWN_CONTRACT = AxisContract(
    name="spawn",
    deny_probe=NOT_MIGRATED,
    exceptions=NOT_MIGRATED,
    workload_test_id=NOT_MIGRATED,
    witness_strength=NOT_MIGRATED,
)

#: Every sandbox axis this contract knows about — migrated or not. #2983
#: migrates axes incrementally, starting with ``network`` (the only axis with
#: all three legs' worth of existing, real evidence: deny = #3030, boundary =
#: #3060, workload = #3060's chunker-serving probe). ``write`` and ``spawn``
#: are registered with every leg EXPLICITLY :data:`NOT_MIGRATED` rather than
#: omitted, so a future axis added here without stating its legs fails to
#: construct (TypeError) instead of silently joining this tuple as if
#: migrated.
#:
#: CI conformance (``tests/test_sandbox_axis_contract_2983.py``) asserts the
#: migrated COUNT directly against ``_EXPECTED_MIGRATED_AXES`` below — so
#: shrinking a migrated axis back to NOT_MIGRATED, or a new axis silently
#: reading as migrated when it is not, both fail that count check rather than
#: passing by construction alone.
AXIS_REGISTRY: "tuple[AxisContract, ...]" = (
    _WRITE_CONTRACT,
    _SPAWN_CONTRACT,
    _NETWORK_CONTRACT,
)

#: Axis names AXIS_REGISTRY currently reports as fully migrated
#: (``is_migrated``). A constant, not a derived count, so the conformance
#: test can name what SHOULD be true and fail loudly if the registry drifts
#: from it in either direction (an axis silently un-migrated, or a new axis
#: migrated without updating this expectation).
_EXPECTED_MIGRATED_AXES: "frozenset[str]" = frozenset({"network"})
