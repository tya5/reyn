"""reyn.hooks.shell_runner — execute an exec/exec_capture HookDef argv (#1800 slice C).

#3226 Phase 4 (naming honesty, NOT security): the ``HookDef`` fields this
module runs were renamed ``shell_exec``/``shell_push`` → ``exec``/
``exec_capture`` and their payload from a shell-command STRING to an
**argv list** (``tuple[str, ...]``). Neither rename changes what this module
does at runtime — it never ran ``/bin/sh -c <string>``; it always executed a
tokenized argv with ``shell=False``. The ``shell_`` prefix was a misnomer
this Phase removes; the module/function names below (``shell_runner.py``,
``run_shell_hook``) are unchanged (out of Phase-4 scope — only the
config-facing action names + payload shape were the misnomer).

Contract
--------
* **Input to the subprocess**: event + context serialised as JSON → subprocess stdin.
* **Output from the subprocess**, by mode (``capture_stdout``, #2069):
  * ``exec`` (``capture_stdout=False``): stdout / stderr are logs only;
    the runner returns ``None`` (pure side-effect). The OS ignores hook output.
  * ``exec_capture`` (``capture_stdout=True``): on an exit-0 run the decoded
    **stdout is returned** (the caller parses it as a JSON push-directive);
    stderr stays logs. Any failure returns ``None`` (fail-safe → skip the push).
* **Timeout** (default 60 s, overridable per-hook via ``timeout_seconds``).
  Timeout / non-zero exit → log + return ``None``; the runner NEVER crashes the
  agent.

Sandbox (CRITICAL)
------------------
The hook argv runs through the **same** :mod:`reyn.security.sandbox`
backend that the ``sandboxed_exec`` op uses::

    backend = sandbox_backend or get_default_backend(sandbox_config)
    result = await backend.run(argv, policy, stdin=..., cwd=...)

No new subprocess machinery is introduced.  When the caller passes
``sandbox_backend=None`` and ``sandbox_config=None``, the factory auto-selects
``SeatbeltBackend`` (macOS), ``LandlockBackend`` (Linux), or ``NoopBackend``
as a last-resort fallback with a loud warning — same as ``sandboxed_exec``.

Consent + allowlist (Hermes-style)
------------------------------------
Allowlist lives at ``~/.reyn/shell-hooks-allowlist.json`` (env-var override:
``REYN_SHELL_HOOKS_ALLOWLIST``).  Each entry records:

    {
        "command": "<argv, shlex-joined into a display/allowlist-key string>",
        "approved_at": "<ISO-8601>",
        "script_mtime": <float or null>
    }

``command`` here is a DISPLAY/allowlist-key string derived from the argv via
``shlex.join`` — never re-interpreted as a shell string; execution always
uses the original argv list, not a re-split of this string (``shlex.join``
followed by ``shlex.split`` round-trips exactly for the mtime-drift check
below, which is the only place the joined form is re-split).

Rules:

* **TTY** (``sys.stdin.isatty()``): if the command is not in the allowlist (or
  its script mtime has changed), prompt the operator; record approval.
* **Non-TTY without REYN_ACCEPT_HOOKS=1**: refuse to run + log (fail-closed).
* **REYN_ACCEPT_HOOKS=1**: bypass the TTY check (for CI); record approval.
* Mtime drift: if the command's first token resolves to an existing file AND its
  mtime differs from the stored ``script_mtime``, treat as un-approved.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from reyn.config import SandboxConfig
    from reyn.security.sandbox import SandboxBackend
    from reyn.security.sandbox.policy import SandboxPolicy
    from reyn.user_intervention import RequestBus

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / env-var paths
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWLIST_PATH = Path.home() / ".reyn" / "shell-hooks-allowlist.json"


def _allowlist_path() -> Path:
    """Return the allowlist path, consulting REYN_SHELL_HOOKS_ALLOWLIST first."""
    env = os.environ.get("REYN_SHELL_HOOKS_ALLOWLIST")
    if env:
        return Path(env)
    return _DEFAULT_ALLOWLIST_PATH


# ---------------------------------------------------------------------------
# Allowlist helpers
# ---------------------------------------------------------------------------


def _load_allowlist(path: Path) -> list[dict]:
    """Load the allowlist JSON, returning an empty list on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def _save_allowlist(path: Path, entries: list[dict]) -> None:
    """Persist the allowlist, creating parent dirs as needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("shell-hook allowlist: could not save %s: %s", path, exc)


def _script_mtime(command: str) -> float | None:
    """Return the mtime of the first token of *command* if it is an existing file."""
    try:
        first_token = shlex.split(command)[0]
        p = Path(first_token).expanduser()
        if p.exists() and p.is_file():
            return p.stat().st_mtime
    except (ValueError, OSError):
        pass
    return None


def _is_approved(command: str, entries: list[dict]) -> bool:
    """Return True iff *command* has a current (no mtime drift) allowlist entry."""
    current_mtime = _script_mtime(command)
    for entry in entries:
        if entry.get("command") != command:
            continue
        stored_mtime = entry.get("script_mtime")
        # Mtime drift: stored mtime differs from current file mtime → un-approved.
        if current_mtime is not None and stored_mtime is not None:
            if abs(float(stored_mtime) - current_mtime) > 0.01:
                _log.warning(
                    "shell-hook: script mtime changed for %r (was %.3f, now %.3f) — "
                    "re-approval required.",
                    command,
                    stored_mtime,
                    current_mtime,
                )
                return False
        return True
    return False


def _record_approval(command: str, path: Path) -> None:
    """Add / update an allowlist entry for *command*."""
    entries = _load_allowlist(path)
    current_mtime = _script_mtime(command)
    # Remove any existing entry for the same command.
    entries = [e for e in entries if e.get("command") != command]
    entries.append({
        "command": command,
        "approved_at": datetime.now(tz=timezone.utc).isoformat(),
        "script_mtime": current_mtime,
    })
    _save_allowlist(path, entries)


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------


async def _check_consent(
    command: str,
    allowlist_path: Path,
    *,
    consent_bus: "RequestBus | None" = None,
    hook_name: str | None = None,
) -> bool:
    """Return True if *command* is approved to run.

    Approval order (#2095):
      1. allowlist hit → approved.
      2. ``REYN_ACCEPT_HOOKS=1`` → record + approve (CI / non-TTY accept).
      3. ``consent_bus`` set → prompt through the SAME ``RequestBus`` that
         ungated permission-prompts use, so it lands in the TUI Pending tab and
         is answerable there (instead of the stdin ``print``/``input`` below,
         which is invisible / unanswerable under a Textual app). The dispatcher
         passes a non-None ``consent_bus`` ONLY when the session has a live
         intervention listener (= a surface that will actually answer), so plain
         ``mcp-serve`` / headless (no listener) and ``reyn run`` on a TTY (no
         listener) both arrive here with ``consent_bus=None`` and take step 4.
      4. **no consent bus** → the pre-#2095 behavior, byte-for-byte: TTY → stdin
         prompt; non-TTY → fail-closed.
    """
    entries = _load_allowlist(allowlist_path)

    if _is_approved(command, entries):
        return True

    # Not approved — decide based on environment.
    accept_env = os.environ.get("REYN_ACCEPT_HOOKS", "").strip() == "1"

    if accept_env:
        # CI / non-TTY accept path: record and proceed.
        _log.info("shell-hook: REYN_ACCEPT_HOOKS=1 — auto-approving %r", command)
        _record_approval(command, allowlist_path)
        return True

    # An answerable surface is attached → route the consent through the unified
    # intervention bus (#2095). The allowlist remains the "always" persistence.
    if consent_bus is not None:
        return await _prompt_consent_via_bus(
            command, allowlist_path, consent_bus, hook_name,
        )

    # No consent bus → preserve the exact pre-#2095 behavior below.
    is_tty = sys.stdin.isatty()

    if is_tty:
        # Interactive prompt.
        print(
            f"\nReyn shell hook: the following command has not been approved:\n\n"
            f"  {command}\n\n"
            "Allow this command to run under the Reyn sandbox? [y/N] ",
            end="",
            flush=True,
        )
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", "yes"):
            _record_approval(command, allowlist_path)
            _log.info("shell-hook: operator approved %r", command)
            return True
        _log.warning("shell-hook: operator declined %r — skipping.", command)
        return False

    # Non-TTY, no accept flag, not pre-approved → fail-closed.
    _log.warning(
        "shell-hook REFUSED (fail-closed): %r is not in the allowlist and "
        "REYN_ACCEPT_HOOKS=1 is not set. To allow in non-interactive / CI "
        "environments, set REYN_ACCEPT_HOOKS=1 or pre-approve the command "
        "interactively first.",
        command,
    )
    return False


async def _prompt_consent_via_bus(
    command: str, allowlist_path: Path, bus: "RequestBus", hook_name: str | None = None,
) -> bool:
    """Prompt for shell-hook consent through the unified intervention bus (#2095).

    Reuses the SAME ``UserIntervention`` / ``RequestBus`` mechanism that ungated
    permission-prompts use, so the prompt surfaces wherever interventions do
    (the TUI Pending tab, stdin for ``reyn run``, etc.) — not the stdin
    ``print``/``input`` that is invisible under a Textual app.

    ``hook_name`` (#2095 P2): the operator's ``HookDef.name`` when set, so the
    prompt identifies WHICH configured hook is asking (vs a generic "a shell
    hook"). Shell hooks are always operator-config (``hooks_add`` can only write
    ``template_push``), so no agent-vs-operator source label is shown.

    Choice mapping (``shell_hook_choices``): ``ALWAYS`` records to the allowlist
    (the "always" persistence); ``YES`` allows this run only; ``NO`` / unknown /
    an empty answer (e.g. the iv was parked stalled because the origin channel
    closed) → deny + skip the hook (fail-safe).
    """
    from reyn.intervention_choices import ALWAYS, YES, shell_hook_choices
    from reyn.user_intervention import UserIntervention

    who = f"Shell hook {hook_name!r}" if hook_name else "A shell hook"
    iv = UserIntervention(
        kind="permission.shell_hook",
        prompt=f"{who} wants to run a command",
        detail=f"$ {command}",
        choices=shell_hook_choices(),
    )
    answer = await bus.request(iv)
    choice = answer.choice_id
    if choice == ALWAYS:
        _record_approval(command, allowlist_path)
        _log.info("shell-hook: approved (always) via intervention bus %r", command)
        return True
    if choice == YES:
        _log.info("shell-hook: approved (once) via intervention bus %r", command)
        return True
    _log.warning("shell-hook: declined via intervention bus %r — skipping.", command)
    return False


def _report_unapplied_agent_policy(
    *,
    sandbox_config: "SandboxConfig | None",
    policy: "SandboxPolicy",
    hook_label: str,
    declared: dict,
    emit_event: "Callable[..., Any] | None",
) -> None:
    """Speak every agent-level ``sandbox.policy`` field this hook shell did not
    honour — as a WARNING + a ``sandbox_policy_not_applied`` audit-event (#3005).

    The agent-level policy is op-scoped by construction, so a hook shell IGNORING
    it is correct; a hook shell ignoring it *silently* is not. An operator who
    writes ``sandbox.policy: {network: true}`` and gets a hook with no network
    has had their expressed will neither applied nor refused — and no signal
    exists anywhere from which they could learn that, or learn that the per-hook
    key is the surface that would work. Both directions matter: an ignored
    ``network``/``write_paths`` grant fails safe (the hook gets less than asked)
    while an ignored ``allow_subprocess`` would fail loose, and neither is
    discoverable while the drop is mute.

    Mirrors the ``sandbox_policy_narrowed`` shape (#2978/#2986) rather than
    inventing one: a policy decision the operator did not write is emitted where
    it is taken, so ``reyn events`` can reconstruct which policy a hook actually
    ran under. Best-effort throughout — reporting must never break the hook run.
    """
    from reyn.hooks.sandbox_scope import (  # noqa: PLC0415 — keep import cost off the no-policy path
        unapplied_policy_fields,
        unapplied_policy_message,
    )

    config_policy = getattr(sandbox_config, "policy", None)
    unapplied = unapplied_policy_fields(config_policy, declared)
    if not unapplied:
        return

    for policy_field, hook_key in unapplied:
        message = unapplied_policy_message(
            hook_label=hook_label,
            policy_field=policy_field,
            hook_key=hook_key,
            configured=config_policy[policy_field],
            effective=getattr(policy, policy_field),
        )
        _log.warning("shell-hook: %s", message)
        if emit_event is None:
            continue
        try:
            emit_event(
                "sandbox_policy_not_applied",
                hook=hook_label,
                policy_field=policy_field,
                hook_key=hook_key,
                configured=config_policy[policy_field],
                effective=getattr(policy, policy_field),
            )
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            _log.debug("shell-hook: emit_event failed for %r: %s", hook_label, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_shell_hook(
    argv: "list[str] | tuple[str, ...]",
    event_context: dict,
    *,
    timeout_seconds: int = 60,
    cwd: str | None = None,
    sandbox_backend: "SandboxBackend | None" = None,
    sandbox_config: "SandboxConfig | None" = None,
    sandbox_policy: "SandboxPolicy | None" = None,
    allow_subprocess: bool | None = None,
    network: bool | None = None,
    write_paths: "tuple[str, ...] | list[str] | None" = None,
    allowlist_path: Path | None = None,
    capture_stdout: bool = False,
    consent_bus: "RequestBus | None" = None,
    hook_name: str | None = None,
    emit_event: "Callable[..., Any] | None" = None,
) -> str | None:
    """Run an exec/exec_capture HookDef argv under the sandbox + consent gate.

    The hook receives event + context as JSON on stdin.  Two output disciplines,
    selected by ``capture_stdout`` (#2069):

    * ``capture_stdout=False`` (``exec``, the default): output is treated as
      logs only and **never parsed** — the runner is a pure side-effect and
      returns ``None``.
    * ``capture_stdout=True`` (``exec_capture``): on a successful (exit-0) run the
      decoded **stdout is returned** for the caller to parse as a JSON
      push-directive.  Any failure (consent refusal, invalid argv, non-zero
      exit, timeout, exception) returns ``None`` so the caller skips the push
      (fail-safe).  ``stderr`` is always logs.

    Parameters
    ----------
    argv:
        The argv list from ``HookDef.exec`` / ``HookDef.exec_capture``
        (#3226 Phase 4 — a clean break from the pre-Phase-4 shell-command
        STRING shape). Executed directly with ``shell=False`` — no shell
        interpretation, no ``shlex.split`` of operator input (the runner
        never ran ``/bin/sh -c <string>``, even pre-Phase-4; only the
        payload SHAPE changed, not the execution mechanism).
    event_context:
        Dict serialised as JSON and passed to the subprocess on stdin.
    timeout_seconds:
        Wall-clock cap; default 60 s.
    cwd:
        Working directory for the subprocess.  Defaults to None (inherit).
    sandbox_backend:
        A pre-constructed :class:`~reyn.security.sandbox.SandboxBackend`
        instance.  When ``None``, ``get_default_backend(sandbox_config)`` is
        called to select the platform backend.
    sandbox_config:
        :class:`~reyn.config.SandboxConfig` forwarded to
        ``get_default_backend``.  Used only when *sandbox_backend* is None.
    sandbox_policy:
        The :class:`~reyn.security.sandbox.policy.SandboxPolicy` to enforce.
        When ``None``, a default policy (no network + no subprocess) is built.
        A full override — when supplied, *allow_subprocess* is not consulted
        (the caller already expressed the whole policy).
    allow_subprocess:
        #2827 — the operator's per-hook ``subprocess:`` knob (``HookDef``),
        applied to the DEFAULT policy built here. ``None`` = omitted = keep the
        floor (``False``); an explicit bool is the operator's expressed will.
        Only consulted when *sandbox_policy* is None.
    network:
        #3005 — the operator's per-hook ``network:`` knob (``HookDef``). Same
        ``None`` = omitted = floor (``False``) semantics as *allow_subprocess*,
        and likewise only consulted when *sandbox_policy* is None.
    write_paths:
        #3005 — the operator's per-hook ``write_paths:`` knob (``HookDef``).
        ``None`` = omitted = the floor, which grants no write paths; an explicit
        sequence (including an empty one) is the operator's expressed will. Only
        consulted when *sandbox_policy* is None.
    allowlist_path:
        Override the allowlist file path (used by tests to point at a tmp
        file).  Defaults to ``~/.reyn/shell-hooks-allowlist.json`` (or the
        ``REYN_SHELL_HOOKS_ALLOWLIST`` env var).
    capture_stdout:
        When ``True`` (``exec_capture``) return the decoded stdout on a successful
        run; when ``False`` (``exec``, default) ignore output and return
        ``None``.
    consent_bus:
        The session ``RequestBus`` (#2095), or ``None``. When set, a
        not-yet-allowlisted command's consent prompt is routed through it (→ the
        TUI Pending tab / the answering surface) instead of the stdin prompt. The
        caller (``HookDispatcher``) passes a non-None bus ONLY when the session
        has a live intervention listener; ``None`` (incl. headless / CI /
        plain mcp-serve / ``reyn run`` with no listener) preserves the pre-#2095
        stdin / fail-closed gate.
    hook_name:
        The hook's ``HookDef.name`` (#2095 P2), surfaced in the consent prompt
        so the user sees WHICH configured hook is asking. ``None`` → a generic
        "a shell hook" prompt. Only used on the ``consent_bus`` path.
    emit_event:
        Optional ``(event_type, **data)`` sink (#2095 P3), wired to the session
        event log. Called once with ``hook_shell_executed`` immediately after the
        command actually runs (consent passed + executed) — so an auto-run
        (allowlisted / accepted) hook, otherwise a silent side-effect, surfaces in
        the TUI events tab. NOT called when consent is refused or the command is
        skipped (then nothing ran). Best-effort: a sink error never breaks the run.

    Returns
    -------
    str | None
        The decoded stdout when ``capture_stdout=True`` and the run succeeded;
        otherwise ``None`` (always ``None`` for ``capture_stdout=False``, and on
        any failure in either mode).

    Notes
    -----
    **Never raises** — all errors (timeout, non-zero exit, consent refusal)
    are logged and the function returns so the agent is never blocked by a
    hook failure.
    """
    # #3226 Phase 4: argv is already tokenized (the loader validated a
    # non-empty list of non-empty strings) — no shlex.split of operator
    # input here. ``command`` is a DISPLAY/allowlist-key string derived via
    # ``shlex.join``, used only for the consent prompt, the allowlist file,
    # and logging; it is never re-parsed to decide what runs.
    argv = list(argv)
    if not argv:
        _log.error("shell-hook: empty argv")
        return
    command = shlex.join(argv)

    resolved_allowlist = allowlist_path if allowlist_path is not None else _allowlist_path()

    # --- Consent gate (fail-closed in non-TTY without accept flag) --------
    try:
        approved = await _check_consent(
            command,
            resolved_allowlist,
            consent_bus=consent_bus,
            hook_name=hook_name,
        )
    except Exception as exc:
        _log.error("shell-hook: consent check error for %r: %s", command, exc)
        return

    if not approved:
        return

    # --- Resolve sandbox backend ------------------------------------------
    # Import here so the module is importable without the sandbox package in
    # contexts where only the schema / allowlist code is needed.
    from reyn.security.sandbox import SandboxPolicy as _SandboxPolicy
    from reyn.security.sandbox import get_default_backend

    backend = sandbox_backend if sandbox_backend is not None else get_default_backend(sandbox_config)

    # Build a safe default policy when none is supplied.
    # #2827/#3005: allow_subprocess / network / write_paths are the operator's
    # per-hook knobs (``subprocess:`` / ``network:`` / ``write_paths:``). None
    # (omitted) keeps the floor — today's behaviour, byte-identical for every
    # hook that predates the knobs; only an explicit operator value moves an
    # axis. (read_deny_paths is NOT set here on purpose: SandboxPolicy's own
    # default_factory already supplies DEFAULT_SENSITIVE_READ_DENY, so the
    # sensitive-file deny-list applies to hook shells too — verified, not
    # assumed.)
    policy: SandboxPolicy
    if sandbox_policy is not None:
        policy = sandbox_policy
    else:
        policy = _SandboxPolicy(
            network=bool(network) if network is not None else False,
            allow_subprocess=bool(allow_subprocess) if allow_subprocess is not None else False,
            write_paths=list(write_paths) if write_paths is not None else [],
            timeout_seconds=timeout_seconds,
        )
        # #3005: the agent-level ``reyn.yaml sandbox.policy`` never reaches this
        # policy — it is resolved on the op path only. That scoping is
        # deliberate (a hook's floor should not move because a run's *ops* are
        # unsandboxed), but dropping the operator's declaration in SILENCE is
        # not: their expressed will must be applied or refused, never ignored.
        # This is the only place that holds both the declaration and the policy
        # it did not become, so it is where the refusal has to be spoken.
        _report_unapplied_agent_policy(
            sandbox_config=sandbox_config,
            policy=policy,
            hook_label=hook_name or command,
            declared={
                "subprocess": allow_subprocess,
                "network": network,
                "write_paths": write_paths,
            },
            emit_event=emit_event,
        )

    # --- Run via backend (same abstraction as sandboxed_exec.py) ----------
    try:
        stdin_bytes = json.dumps(event_context, default=str).encode("utf-8")

        result = await backend.run(
            argv,
            policy,
            stdin=stdin_bytes,
            cwd=cwd,
        )

        # #2095 P3: the command actually ran (consent passed) — surface it as a
        # P6 event so an auto-run (allowlisted) shell hook isn't a silent
        # side-effect. Best-effort: a sink error must not break the run.
        # #2827: classify a sandbox fork-denial the SAME way the op path does
        # (op_runtime/sandboxed_exec.py, #2820 part B). Without this the hook
        # path's only signal was an opaque `fork: Operation not permitted`
        # warning, so an operator could not tell an environment/PATH problem
        # from a genuine command failure — and therefore could not know the
        # ``subprocess:`` knob above is what fixes it. Pure function of
        # (returncode, stderr); no I/O.
        from reyn.security.sandbox.denial import DENIAL_FORK, classify_denial  # noqa: PLC0415
        denial_class = classify_denial(result.returncode, result.stderr)

        if emit_event is not None:
            try:
                emit_event(
                    "hook_shell_executed",
                    command=command,
                    mode=("exec_capture" if capture_stdout else "exec"),
                    returncode=result.returncode,
                    denial_class=denial_class,
                )
            except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                _log.debug("shell-hook: emit_event failed for %r: %s", command, exc)

        # stderr is ALWAYS logs. stdout is logs for exec; for exec_capture
        # (capture_stdout) it is the JSON push-directive the caller parses — so
        # don't log it as a side-effect line, return it below.
        if not capture_stdout and result.stdout.strip():
            _log.debug(
                "shell-hook %r stdout (logged, not parsed): %s",
                command,
                result.stdout[:200].decode("utf-8", errors="replace"),
            )
        if result.stderr.strip():
            _log.debug(
                "shell-hook %r stderr (logs): %s",
                command,
                result.stderr[:200].decode("utf-8", errors="replace"),
            )

        if result.returncode not in (0,):
            stderr_snippet = result.stderr[:200].decode("utf-8", errors="replace").strip()
            if denial_class == DENIAL_FORK:
                # #2827/#2820-B: name the class and point at the fix. The raw
                # stderr ("fork: Operation not permitted") reads as a broken
                # command; it is actually the sandbox denying a launcher's fork,
                # which the operator's per-hook ``subprocess: true`` resolves.
                _log.warning(
                    "shell-hook %r exited %d: the sandbox denied fork() "
                    "(denial_class=%s) — an environment/config problem, not a "
                    "command failure. A bare command resolving to a version-manager "
                    "shim (pyenv/asdf/mise) or a spawn-based launcher (npx/uvx) forks "
                    "internally. Set `subprocess: true` on this hook to permit it, or "
                    "use an absolute path to the real binary. (stderr: %s)",
                    command,
                    result.returncode,
                    denial_class,
                    stderr_snippet or "<empty>",
                )
            else:
                _log.warning(
                    "shell-hook %r exited %d (stderr: %s).",
                    command,
                    result.returncode,
                    stderr_snippet or "<empty>",
                )
            return None  # fail-safe: a failed command yields no push-directive

        # Success. capture_stdout (exec_capture) → return decoded stdout for the
        # caller to parse; otherwise (exec) output is ignored.
        if capture_stdout:
            return result.stdout.decode("utf-8", errors="replace")
        return None

    except Exception as exc:
        _log.error("shell-hook %r: unexpected error: %s", command, exc)
        return None
