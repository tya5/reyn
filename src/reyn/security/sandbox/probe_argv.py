"""probe_argv — a doctor-only differential launch probe (#4364 PR-2, C-1).

**What C-1 is actually asking.** The motivating incident (owner's Mac,
#4364) was an ``xcrun`` PATH shim that ran, then died mid-exec on a
``TMPDIR`` write the sandbox denied — exec genuinely happened. A probe
that asks "did exec occur" would not have caught it: the question that
matters is "does THIS argv[0] run to a clean exit under THIS hook's
sandbox", not whether the OS-level ``execve`` syscall fired.

**Why differential measurement, not a contract.** ``sandbox-exec``'s own
man page has no EXIT STATUS section and opens with DEPRECATED (measured
directly, #4364) — there is no documented return-code contract this
module could read against. So it never interprets a return code in
isolation: it runs a KNOWN-GOOD, args-free binary
(:meth:`~reyn.security.sandbox.backend.SandboxBackend.probe_binary`)
under the SAME backend + SAME policy as the argv[0] being probed, and
reports only the DIFFERENCE. "``/usr/bin/true`` runs here; ``<argv[0]>``
does not" is true regardless of what any particular exit code means on
this platform — architect's own ruling on why the method is named
``probe_argv``, not ``did_exec_fail`` (a name that would assert a
syscall-level claim this measurement never makes).

**Three real outcomes, plus a `None` for "cannot measure here"** — the
same 3-value-plus-None shape ``self_test()``/``probe_binary()`` already
use:

- ``"ok"``       — target ran to exit 0.
- ``"target_failed"`` — the KNOWN-GOOD control ran clean, but the target
  did not: the failure is attributable to the target under this sandbox
  (never to the sandbox mechanism itself, since the control just proved
  it works).
- ``"sandbox_failed"`` — the control itself did not exit 0: something is
  wrong with the sandbox/backend, independent of the target argv[0]
  entirely. Reported so a caller does not misattribute a broken sandbox
  to a broken hook.
- ``None`` — this backend cannot support a probe at all
  (``probe_binary()`` returned ``None``: NoopBackend has nothing to
  differentiate, DockerEnvironmentBackend cannot assume anything about
  the configured image's own filesystem).

**A known false-positive, disclosed rather than hidden (D-3's own
"disclose what was not measured" discipline).** ``target_failed`` covers
BOTH "genuinely broken under this sandbox" and "this program requires
arguments and legitimately exits non-zero with none" — argv[0] is probed
WITHOUT its configured args (a launch probe, not a run: giving it real
args would mean actually EXECUTING the hook, the one thing a read-only
doctor (D-2) must never do). The caller (``doctor.py``) states this
ambiguity in its own output rather than trying to resolve it — resolving
it would require running the hook for real, which this module refuses to
do.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from reyn.security.sandbox.backend import SandboxBackend
    from reyn.security.sandbox.policy import SandboxPolicy

ProbeResult = Literal["ok", "target_failed", "sandbox_failed"]


async def probe_argv(
    backend: "SandboxBackend", argv: "list[str] | tuple[str, ...]", policy: "SandboxPolicy",
) -> "ProbeResult | None":
    """Differentially probe ``argv[0]`` (never the configured args — see
    the module docstring's D-2 note) under *backend* + *policy*.

    Returns ``None`` immediately, without launching anything, when
    ``backend.probe_binary()`` has nothing to offer (this backend cannot
    support a probe) or *argv* is empty (nothing to probe)."""
    if not argv:
        return None
    good = backend.probe_binary()
    if good is None:
        return None
    good_result = await backend.run(good, policy)
    if good_result.returncode != 0:
        return "sandbox_failed"
    target_result = await backend.run([argv[0]], policy)
    if target_result.returncode == 0:
        return "ok"
    return "target_failed"


__all__ = ["ProbeResult", "probe_argv"]
