"""reyn.hooks.shell_runner — execute a shell HookDef command (#1800 slice C).

Contract
--------
* **Input to the subprocess**: event + context serialised as JSON → subprocess stdin.
* **Output from the subprocess**, by mode (``capture_stdout``, #2069):
  * ``shell_exec`` (``capture_stdout=False``): stdout / stderr are logs only;
    the runner returns ``None`` (pure side-effect). The OS ignores hook output.
  * ``shell_push`` (``capture_stdout=True``): on an exit-0 run the decoded
    **stdout is returned** (the caller parses it as a JSON push-directive);
    stderr stays logs. Any failure returns ``None`` (fail-safe → skip the push).
* **Timeout** (default 60 s, overridable per-hook via ``timeout_seconds``).
  Timeout / non-zero exit → log + return ``None``; the runner NEVER crashes the
  agent.

Sandbox (CRITICAL)
------------------
The hook command runs through the **same** :mod:`reyn.security.sandbox`
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
        "command": "<raw command string>",
        "approved_at": "<ISO-8601>",
        "script_mtime": <float or null>
    }

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_shell_hook(
    command: str,
    event_context: dict,
    *,
    timeout_seconds: int = 60,
    cwd: str | None = None,
    sandbox_backend: "SandboxBackend | None" = None,
    sandbox_config: "SandboxConfig | None" = None,
    sandbox_policy: "SandboxPolicy | None" = None,
    allowlist_path: Path | None = None,
    capture_stdout: bool = False,
    consent_bus: "RequestBus | None" = None,
    hook_name: str | None = None,
    emit_event: "Callable[..., Any] | None" = None,
) -> str | None:
    """Run a shell hook command under the sandbox + consent gate.

    The hook receives event + context as JSON on stdin.  Two output disciplines,
    selected by ``capture_stdout`` (#2069):

    * ``capture_stdout=False`` (``shell_exec``, the default): output is treated as
      logs only and **never parsed** — the runner is a pure side-effect and
      returns ``None``.
    * ``capture_stdout=True`` (``shell_push``): on a successful (exit-0) run the
      decoded **stdout is returned** for the caller to parse as a JSON
      push-directive.  Any failure (consent refusal, invalid command, non-zero
      exit, timeout, exception) returns ``None`` so the caller skips the push
      (fail-safe).  ``stderr`` is always logs.

    Parameters
    ----------
    command:
        The raw shell command string from ``HookDef.shell_exec`` /
        ``HookDef.shell_push``.  Split via ``shlex.split``; executed with
        ``shell=False`` (no shell injection).
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
    allowlist_path:
        Override the allowlist file path (used by tests to point at a tmp
        file).  Defaults to ``~/.reyn/shell-hooks-allowlist.json`` (or the
        ``REYN_SHELL_HOOKS_ALLOWLIST`` env var).
    capture_stdout:
        When ``True`` (``shell_push``) return the decoded stdout on a successful
        run; when ``False`` (``shell_exec``, default) ignore output and return
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

    # --- Split command (shlex; shell=False enforced by the backend) -------
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _log.error("shell-hook: invalid command %r: %s", command, exc)
        return

    if not argv:
        _log.error("shell-hook: empty argv after shlex.split(%r)", command)
        return

    # --- Resolve sandbox backend ------------------------------------------
    # Import here so the module is importable without the sandbox package in
    # contexts where only the schema / allowlist code is needed.
    from reyn.security.sandbox import SandboxPolicy as _SandboxPolicy
    from reyn.security.sandbox import get_default_backend

    backend = sandbox_backend if sandbox_backend is not None else get_default_backend(sandbox_config)

    # Build a safe default policy when none is supplied.
    policy: SandboxPolicy = (
        sandbox_policy
        if sandbox_policy is not None
        else _SandboxPolicy(
            network=False,
            allow_subprocess=False,
            timeout_seconds=timeout_seconds,
        )
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
        if emit_event is not None:
            try:
                emit_event(
                    "hook_shell_executed",
                    command=command,
                    mode=("shell_push" if capture_stdout else "shell_exec"),
                    returncode=result.returncode,
                )
            except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                _log.debug("shell-hook: emit_event failed for %r: %s", command, exc)

        # stderr is ALWAYS logs. stdout is logs for shell_exec; for shell_push
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
            _log.warning(
                "shell-hook %r exited %d (stderr: %s).",
                command,
                result.returncode,
                stderr_snippet or "<empty>",
            )
            return None  # fail-safe: a failed command yields no push-directive

        # Success. capture_stdout (shell_push) → return decoded stdout for the
        # caller to parse; otherwise (shell_exec) output is ignored.
        if capture_stdout:
            return result.stdout.decode("utf-8", errors="replace")
        return None

    except Exception as exc:
        _log.error("shell-hook %r: unexpected error: %s", command, exc)
        return None
