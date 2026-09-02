"""ProcessLauncher — the shared backend-resolve/run/classify slice every
agent-reachable command-level launch route already duplicates (#3823 Phase 1).

Scope, measured not assumed: ``sandboxed_exec`` (op_runtime) and the shell-hook
runner both do the SAME three steps in the SAME order — resolve a
:class:`~reyn.security.sandbox.backend.SandboxBackend` (an injected instance
wins over name-based platform auto-selection), call its ``run()``, and
classify a launcher-fork denial from the result — then diverge (each emits
its own, differently-shaped audit-event; only ``sandboxed_exec`` additionally
does argv0 launcher-shim resolution and a pre-exec threat scan). This module
extracts exactly the shared triple, not the divergent parts — folding
argv0-resolution or threat-scanning in here would silently change shell
hooks' behavior (they do neither today), which is a scope decision for a
later, explicit PR, not something to bundle into a route-unification pass.

This is deliberately the NARROW slice of the #3823 proposal's ``ProcessLauncher``
(cwd/env/PATH/launcher-resolution/threat-scan/timeout/cancel-teardown/audit/
diagnostics) — the rest either already lives at the right layer (timeout and
output-cap are ``SandboxPolicy``-driven inside each backend's own ``run()``;
cancel-teardown is likewise backend-internal via ``kill_process_tree``) or is
genuinely new work gated on the ``sandbox.mode`` design (#3823 ②③, unresolved
as of this module's introduction). Mode-independent: this module makes no
behavior decision — it resolves and runs with whatever ``SandboxPolicy`` the
caller already built, byte-identical to what each caller did inline before.

#5084 ④ adds ``hook_process_context`` — this is NOT the general "env" slot
#3823's own unresolved ``sandbox.mode`` design still gates: it is a single,
CLOSED 3-field envelope
(:class:`~reyn.hooks.shell_runner.HookProcessContext`), the shell-hook
runner's own sole caller of it, threaded straight through to
``backend.run()`` with no interpretation here. A general, caller-chosen
``env: Mapping[str, str]`` remains exactly as unresolved/deferred as before
this addition (owner's own standing directive: the Sandbox abstraction
must not gain a caller-controlled arbitrary-env escape hatch).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .denial import classify_denial

if TYPE_CHECKING:
    from reyn.hooks.shell_runner import HookProcessContext

    from .backend import SandboxBackend, SandboxResult
    from .policy import SandboxPolicy


def resolve_backend(
    backend: "SandboxBackend | None" = None,
    sandbox_config: "object | None" = None,
) -> "SandboxBackend":
    """Resolve the backend to launch under — an injected instance wins over
    name-based platform auto-selection (the "an injected instance beats the
    factory" precedent ``sandboxed_exec`` already establishes, letting a
    caller route into a stateful backend, e.g. Docker, that the name-based
    factory cannot build). ``sandbox_config`` is only consulted when
    ``backend`` is ``None``.

    Split from :func:`run_and_classify` rather than folded into one
    resolve-then-run call: both real callers (``sandboxed_exec``, the
    shell-hook runner) need the backend's ``.name`` for a "started" audit-
    event BEFORE the run happens — collapsing resolution into the run call
    would force callers back to re-deriving the backend a second time just
    to log it, which is the duplication this module exists to remove."""
    from . import get_default_backend  # noqa: PLC0415 — matches call sites' own deferred import

    return backend or get_default_backend(sandbox_config)


@dataclass
class LaunchResult:
    """The raw backend result and the classified denial (if any) — the two
    things every caller re-derives from a finished run before building its
    own event/response shape."""

    result: "SandboxResult"
    denial_class: "str | None"


async def run_and_classify(
    backend: "SandboxBackend",
    argv: list[str],
    policy: "SandboxPolicy",
    *,
    cwd: str | None = None,
    stdin: bytes | None = None,
    cancel_event: "asyncio.Event | None" = None,
    hook_process_context: "HookProcessContext | None" = None,
    sink: "Callable[[int, bytes], None] | None" = None,
) -> LaunchResult:
    """Run *argv* under *policy* on the already-resolved *backend*, classify
    the result. The shared tail every agent-reachable launch route already
    does identically, after :func:`resolve_backend`.

    ``cancel_event`` is passed straight through to ``backend.run()`` — not
    every backend accepts meaningful cancellation the same way; Docker's
    ``run()`` simply doesn't take the parameter at all (a pre-existing gap,
    #3822's own measurement, not something this module papers over — a
    caller that needs cancel support on Docker still doesn't have it after
    this).

    ``hook_process_context`` (#5084 ④): the CLOSED, 3-field ``REYN_*`` env
    struct (:class:`~reyn.hooks.shell_runner.HookProcessContext`) a hook's
    ``exec``/``exec_capture`` child process reads — ``None`` for every
    OTHER caller of this shared function (the ``sandboxed_exec`` op path
    has no such context and never passes one, byte-identical to before
    this parameter existed). Passed straight through to ``backend.run()``,
    same as ``cwd``/``cancel_event`` — this function does not interpret it
    itself; each backend decides how (or whether) to translate it, per
    that Protocol method's own docstring.

    ``sink`` (#4733 §3-a, architect ruling 2026-09-02): forwarded
    verbatim to ``backend.run()`` — see ``SandboxBackend.run``'s own
    docstring for the full contract. ``None`` for every caller before
    #4733 (byte-identical)."""
    result = await backend.run(
        argv, policy, cwd=cwd, stdin=stdin, cancel_event=cancel_event,
        hook_process_context=hook_process_context, sink=sink,
    )
    denial_class = classify_denial(result.returncode, result.stderr)
    return LaunchResult(result=result, denial_class=denial_class)
