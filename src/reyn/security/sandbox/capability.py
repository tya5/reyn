"""CapabilityDeclaration — a backend's declared support for a NAMED-SERVICE
class of capability the axis vocabulary does not cover (#4935, architect
design, owner 2-stage ruling).

## Why this is a SEPARATE registry from ``AxisEnforcementDeclaration``

:class:`~reyn.security.sandbox.backend.AxisEnforcementDeclaration` answers
"does this backend enforce (restrict) SandboxPolicy axis X" — write/network/
subprocess/env, the 7 fields every ``SandboxPolicy`` instance carries. This
module answers a DIFFERENT question that arose from real, measured
production failures (#4932/#4933/#4935): "can this backend be asked to
GRANT reachability to one NAMED external service" — ``com.apple.
SecurityServer``, ``opendirectoryd``, ``com.apple.SystemConfiguration.
configd``, the kind of narrow, named Mach/IPC service a command like ``gh``
or ``dscl``/``scutil`` needs to function, that has no corresponding
``SandboxPolicy`` axis at all (no ``allow_mach_lookup`` field exists, nor
should one — see "why not just add axes" below).

**Root cause this design answers to** (measured, not theorised — #4932's
own root cause, generalised): a capability an operator's shell already had
falls through to the backend's narrower default SILENTLY — no error, no
log line, the command just fails as if broken. #4937 closed ONE instance
of this (Seatbelt's ``com.apple.SecurityServer`` mach-lookup, on by
default) by hand. This module is the DECLARATION half of making the CLASS
of gap askable instead of hand-patched one service at a time forever.

## Why not just add axes to ``AxisEnforcementDeclaration``

An axis is a SandboxPolicy FIELD — a knob every backend must resolve a
concrete allow/deny decision for, every run, from operator-declared policy
data. A named IPC/Mach service has no such structure: there is no bounded,
enumerable "axis" of possible service names (unlike write_paths, which is
a closed policy field with a real default), and #4039's own D2 rejected a
speculative third ``AxisEnforcement`` value for exactly this shape of
"doesn't cleanly fit the existing 2-value contract" pressure. This module
is deliberately a SEPARATE, narrower contract: one boolean per backend, per
NAMED CAPABILITY CLASS (not per specific service name) — "can this backend
grant SOME named-service reachability if the operator asks", not "does it
currently grant every service any operator might need".

## Registry: exactly ONE member today — ``ipc_named_service``

Real evidence only (owner ruling relayed through lead-coder, 2026-08-19:
"don't widen the frame speculatively — leave gaps visible"). 3 concrete
service names are the production evidence this ONE category rests on,
verified through the REAL ``SeatbeltBackend.run()`` path (not a raw
``sandbox-exec`` probe — architect's own #4937 lesson), all under
``SandboxPolicy()``'s bare default:

    com.apple.SecurityServer      — granted (#4937): ``gh auth status`` /
                                     ``security list-keychains`` both
                                     confirmed succeeding
    opendirectoryd                — confirmed FAILING (#4935 measurement,
                                     this PR): ``dscl . -read <user>
                                     RecordName`` → returncode 70,
                                     "eServerError" — NOT yet granted
    com.apple.SystemConfiguration — confirmed FAILING (#4935 measurement,
                                     this PR): ``scutil --dns`` →
                                     returncode 1, "No DNS configuration
                                     available" — NOT yet granted

**Only the FIRST is actually reachable today.** ``SUPPORTED`` below means
"this backend HAS a mechanism it could use to grant a named service" (true
for Seatbelt, proven by #4937's own grant existing), never "every known
named-service need under this category is currently granted" — the other
2 remain open, disclosed gaps, not silently implied closed by this PR. A
future PR that grants ``opendirectoryd``/``com.apple.SystemConfiguration``
does not need a NEW capability category — it extends the SAME
``ipc_named_service`` grant Seatbelt already has a mechanism for.

Adding a SECOND category needs its own real, production-measured evidence
— not a plausible-sounding guess at what else might be missing. Absence
here is a currently-empty slot, not a decision that nothing else exists.

## Landlock structurally cannot express this (research finding, not a gap)

architect's kernel-doc research (docs.kernel.org, Landlock userspace-api,
referenced 2026-08-20): Landlock is RESTRICT-ONLY — "access rights that are
not specifically listed here are not going to be denied by this ruleset
when it is enacted" (verbatim). There is no "grant" operation in Landlock's
model at all; the #4932-class failure (an enumeration gap silently reading
as denial) is STRUCTURALLY IMPOSSIBLE there, because nothing Landlock
doesn't enumerate is ever denied in the first place. ``NOT_SUPPORTED``
below is therefore not "Landlock has a gap" — it is "the question this
module asks does not apply to Landlock's model", the closest available
answer given #4039's own no-3rd-value ruling (D2) still applies here too.

## The CI-witness gap this module MUST NOT let a reader miss (lead-coder,
2026-08-20, same lesson as #4938's "CI-portable" wording correction)

:mod:`~reyn.security.sandbox.axis_contract`'s D4 bridge
(``tests/security/test_sandbox_axis_declaration_witness_4039.py``) keeps an
``AxisEnforcementDeclaration.ENFORCES`` claim from being an unverified
production assertion — but that bridge is CI-conformance-only, and CI runs
on ``ubuntu-latest`` exclusively (0 macOS runners, verified live for #4938).
**There is no CI-runnable analogue for this module's ``SUPPORTED`` claim on
Seatbelt.** The claim is verified exactly once, here, by a human running
the real ``SeatbeltBackend.run()`` path on a real Mac (this PR's own
measurement) — not by a standing gate that re-checks it on every push. A
future change to ``_build_sbpl_profile`` that silently drops the
``com.apple.SecurityServer`` grant would NOT be caught by CI; only a local
run on macOS (or a future macOS-capable CI runner, not requested here)
would catch it. State this to whoever next touches this file — do not let
"there's a declaration" read as "there's a standing guarantee".
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class CapabilitySupport(Enum):
    """Whether a backend can express ONE named-capability class — #4935.

    Two values, mirroring :class:`~reyn.security.sandbox.backend.
    AxisEnforcement`'s own D2 ruling (#4039): no speculative third value.
    """

    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"


@dataclass(frozen=True)
class CapabilityDeclaration:
    """A backend's declared support for every registered capability class —
    #4935 (D1: declaration, never a probe — mirrors
    :class:`~reyn.security.sandbox.backend.AxisEnforcementDeclaration`'s own
    discipline exactly, including "every field required, no defaults" so a
    backend that forgets a capability fails to CONSTRUCT rather than
    silently reading as unsupported).

    See this module's own docstring for why the registry has exactly ONE
    member today (``ipc_named_service``) and why a new member needs its own
    production evidence, not a speculative addition.
    """

    ipc_named_service: CapabilitySupport

    def as_dict(self) -> "dict[str, CapabilitySupport]":
        """The declaration as a plain ``{capability_name: CapabilitySupport}``
        dict — what :func:`~reyn.security.sandbox.policy.
        unsupported_required_capabilities` (the production consumer) and any
        future doctor/CI reader both actually want."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: Every capability name :class:`CapabilityDeclaration` covers — derived
#: from the dataclass's own fields (never a hand-duplicated literal) so this
#: constant cannot drift from the type it describes, same discipline as
#: :data:`~reyn.security.sandbox.backend.SANDBOX_POLICY_AXES`.
SANDBOX_CAPABILITY_NAMES: "frozenset[str]" = frozenset(
    f.name for f in fields(CapabilityDeclaration)
)
