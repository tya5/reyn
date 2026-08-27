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
* **Output size** (``exec_capture`` only, #5210, corrected #5244): the
  returned/parsed stdout is unbounded by default (``output_token_cap=None``,
  byte-identical to every pre-#5210 caller) — the LOGGED copy has always
  been capped at 200 bytes (see ``:200`` below) but the RETURNED value,
  which ends up as an inbox message and ultimately a prompt, was not. A
  caller that supplies ``output_token_cap`` gets the parsed directive's
  ``message`` field bounded IN PLACE (structure preserved, marked with a
  visible elision note, still delivered) when it exceeds the cap — #5210's
  own "never truncate" ruling governs the SERIALIZED byte stream (cutting
  JSON mid-stream breaks the parse); it does not forbid bounding a
  structured field's VALUE after a successful parse, which #5244 found
  #5210's original form conflated (see ``run_shell_hook``'s own docstring).

Sandbox (CRITICAL)
------------------
The hook argv runs through the **same** :mod:`reyn.security.sandbox`
backend that the ``sandboxed_exec`` op uses, via the shared resolve/run/
classify slice both callers do identically (:mod:`reyn.security.sandbox.launcher`,
#3823 ①)::

    backend = resolve_backend(sandbox_backend, sandbox_config)
    launched = await run_and_classify(backend, argv, policy, stdin=..., cwd=...)

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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class HookProcessContext:
    """The CLOSED, fixed set of ``REYN_*`` values a hook's ``exec``/
    ``exec_capture`` child process receives via its own environment (#5084
    ④, mechanism "B" — see :mod:`reyn.runtime.workspace_paths`'s own module
    docstring for the full A/B split: "A" is reyn's own in-process
    ``${REYN_PROJECT_DIR}`` token expansion, ``expand_reyn_tokens``/
    ``expand_with_map``, never touching ``os.environ``; "B" is this class,
    a REAL environment variable a spawned CHILD process reads because it
    has no way to run reyn's own in-process expander itself).

    Exactly THREE named fields, never a free-form ``dict[str, str]`` —
    architect's own ruling (#5084, owner's standing directive "don't break
    the Sandbox abstraction"): a general ``run(env=Mapping[str, str])``
    would let a caller inject ARBITRARY env into a sandboxed subprocess
    (``PATH``/``LD_PRELOAD``/``PYTHONPATH`` are all ways to change WHAT
    actually runs, not just what it can read) — silently routing around
    the sandbox boundary's whole point. This type is a closed envelope
    (Tier-1 lens 2: typed, never free-formed): a caller cannot add a
    fourth variable, and the three names below are the ONLY ones any
    backend's :meth:`~reyn.security.sandbox.backend.SandboxBackend.run`
    implementation is asked to set.

    ``project_dir``/``agent_base_dir`` are PATHS, resolved on the reyn
    host — meaningless (or actively wrong) inside a container backend
    whose repo lives at a different in-container path (the SAME asymmetry
    ``SandboxBackend.run``'s own ``cwd`` docstring already draws for a
    workspace-coupled backend); ``agent_name`` is a bare identity string,
    equally true on either side of a container boundary, so it is passed
    through unconditionally. A backend that cannot translate the two path
    values omits them rather than passing a host-side path that would
    silently resolve to nothing (or someone else's directory) inside the
    container — never a silent full drop of all three."""

    project_dir: Path
    agent_base_dir: Path
    agent_name: str

    def as_env(self) -> "dict[str, str]":
        """The literal ``os.environ`` additions a host-process backend
        applies verbatim. A container backend calls this too but MAY strip
        the two path keys first (see the class docstring) before merging
        the rest into the container's own env — never call ``os.environ``
        directly from a backend; go through this method so the three
        names stay defined in exactly one place."""
        return {
            "REYN_PROJECT_DIR": str(self.project_dir),
            "REYN_AGENT_BASE_DIR": str(self.agent_base_dir),
            "REYN_AGENT_NAME": self.agent_name,
        }


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
        effective_policy_value,
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
            effective=effective_policy_value(policy, policy_field),
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
                effective=effective_policy_value(policy, policy_field),
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
    temp_dir: str | None = None,
    hook_process_context: "HookProcessContext | None" = None,
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
    output_token_cap: "tuple[int, str] | None" = None,
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
    output_token_cap:
        #5210, corrected #5244 — ``(cap_tokens, model)`` for
        ``capture_stdout=True`` runs only; ``None`` (default) applies NO cap,
        matching every pre-#5210 caller's behavior unchanged. When set, this
        function parses the decoded stdout as the JSON push-directive itself
        (a preview parse — the caller/dispatcher still does its own,
        independent parse of whatever this returns) and, IF that parse
        succeeds and yields a string ``message`` field whose estimated token
        count (:func:`~reyn.services.compaction.engine.estimate_tokens`,
        against *model*) exceeds *cap_tokens*: bounds that field's text
        (:func:`~reyn.services.compaction.engine.hard_truncate_summary`,
        deterministic char-ratio truncation) to leave room for a visible
        elision marker WITHIN *cap_tokens* — the marker's own estimated
        token cost is reserved from the body's budget first (architect's
        TESTS-READ catch, #5343: appending the marker to a body already
        truncated to the FULL cap would let the combined message exceed
        *cap_tokens*, the exact thing this cap exists to prevent) — then
        re-serializes the SAME JSON object with the bounded ``message``,
        and returns that. A *cap_tokens* smaller than the marker's own
        token floor (~20 tokens for its fixed English text) cannot be
        honored exactly no matter how much the body is truncated — a
        real, live context-budget-derived cap is never this small in
        practice, so this is a theoretical floor, not a handled case with
        its own fallback. The directive's other fields, and its
        structure, are untouched, and it still reaches the caller as a
        normal push. Recorded via *emit_event* with
        ``denial_class="exec_capture_message_bounded"``. #5210's original
        form checked the RAW stdout byte/token count BEFORE any parse and
        discarded the whole directive on overflow (a `#5244 finding
        <https://github.com/tya5/reyn/issues/5244>`_: #5210's own "never
        truncate" ruling is about not cutting the SERIALIZED byte stream —
        a truncated JSON push-directive fails to parse and is
        indistinguishable from a clean no-push run at the dispatcher, the
        exact "two silences" shape #5041 already closed once for a
        different cause — it says nothing about bounding a structured
        field's VALUE after a successful parse, which is what this branch
        now does). If the preview parse fails, or yields no usable
        ``message`` string, this function does not attempt to bound
        anything — the caller's own (separate) parse handles that shape the
        same fail-safe way it always has. *cap_tokens* is deliberately not
        invented here — the caller derives it from a real, live
        context-budget source (``HookDispatcher``'s own
        ``resolve_exec_capture_output_cap``, wired from ``Session``'s
        ``TurnBudgetEngine.budget.output_reserve``) and passes ``None`` when
        no such source is available, rather than supplying an arbitrary
        fallback number.

    Returns
    -------
    str | None
        The decoded stdout when ``capture_stdout=True`` and the run
        succeeded — with its ``message`` field bounded in place if
        *output_token_cap* was set and exceeded (see above); otherwise
        ``None`` (always ``None`` for ``capture_stdout=False``, and on any
        run failure — non-zero exit, timeout, consent refusal, exception —
        in either mode).

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
    from reyn.security.sandbox.launcher import resolve_backend

    backend = resolve_backend(sandbox_backend, sandbox_config)

    # Build a safe default policy when none is supplied.
    # #2827/#3005: allow_subprocess / network / write_paths are the operator's
    # per-hook knobs (``subprocess:`` / ``network:`` / ``write_paths:``). None
    # (omitted) keeps the floor — today's behaviour, byte-identical for every
    # hook that predates the knobs; only an explicit operator value moves an
    # axis. (#3901 PR-B ④: read_deny_paths is NOT set here — SandboxPolicy's
    # own dataclass default is now an empty list (owner ruling B, full
    # compat), so this floor no longer carries the sensitive-file deny-list
    # by default; an operator who wants it back sets it explicitly, same as
    # everywhere else post-#3901.)
    policy: SandboxPolicy
    if sandbox_policy is not None:
        policy = sandbox_policy
    else:
        policy = _SandboxPolicy(
            network=bool(network) if network is not None else False,
            deny_subprocess=not allow_subprocess if allow_subprocess is not None else True,
            write_paths=list(write_paths) if write_paths is not None else [],
            timeout_seconds=timeout_seconds,
            temp_dir=temp_dir or "",
            temp_source="session",
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

    # --- Run via backend (same abstraction as tools/exec.py, #3226 Phase 3
    # renamed from sandboxed_exec.py) ----------
    try:
        stdin_bytes = json.dumps(event_context, default=str).encode("utf-8")

        from reyn.security.sandbox.denial import DENIAL_FORK  # noqa: PLC0415
        from reyn.security.sandbox.launcher import run_and_classify  # noqa: PLC0415

        launched = await run_and_classify(
            backend,
            argv,
            policy,
            stdin=stdin_bytes,
            cwd=cwd,
            hook_process_context=hook_process_context,
        )
        result = launched.result

        # #2095 P3: the command actually ran (consent passed) — surface it as a
        # P6 event so an auto-run (allowlisted) shell hook isn't a silent
        # side-effect. Best-effort: a sink error must not break the run.
        # #2827: a sandbox fork-denial the SAME way the op path does
        # (op_runtime/sandboxed_exec.py, #2820 part B). Without this the hook
        # path's only signal was an opaque `fork: Operation not permitted`
        # warning, so an operator could not tell an environment/PATH problem
        # from a genuine command failure — and therefore could not know the
        # ``subprocess:`` knob above is what fixes it. Classified inside
        # run_and_classify (#3823 ①) — reused here, not re-derived.
        denial_class = launched.denial_class

        # #5210, corrected #5244: computed HERE — before the single
        # unconditional emit_event call below — so a bounded-message
        # verdict rides that SAME event (architect's own prescription:
        # reuse denial_class, no new event surface) rather than firing a
        # second, separate one. Only relevant for a would-be-successful
        # capture_stdout run; an already-failed run (non-zero exit) is
        # unaffected — that path's own denial_class (e.g. DENIAL_FORK)
        # takes priority below, unchanged.
        #
        # #5244 (architect ruling, correcting #5210): #5210's own "never
        # truncate" reasoning is about the SERIALIZED byte stream — cutting
        # JSON mid-stream produces a directive that fails to parse and is
        # indistinguishable from a clean, deliberate no-push run (the exact
        # "two silences" shape #5041 already closed once). That reasoning
        # does NOT extend to bounding a single structured field's VALUE
        # after a successful parse — the structure survives, the caller's
        # own parse still succeeds, and the push still reaches its target.
        # #5210's original form conflated the two (checked raw stdout
        # BEFORE any parse, discarded the whole directive on overflow) —
        # coder-brown's census (28 output-capping call sites repo-wide)
        # found this was the only one of the 28 that discarded wholesale
        # rather than marking and delivering a bounded result; #5244 brings
        # it in line with the other 27.
        bounded_stdout: str | None = None
        if (
            capture_stdout
            and result.returncode == 0
            and output_token_cap is not None
        ):
            cap_tokens, cap_model = output_token_cap
            from reyn.services.compaction.engine import (  # noqa: PLC0415
                estimate_tokens,
                hard_truncate_summary,
            )

            decoded_stdout_for_cap = result.stdout.decode("utf-8", errors="replace")
            # A preview parse, purely to reach the `message` field for
            # bounding — the caller/dispatcher still does its own,
            # independent parse of whatever this function returns
            # (`_parse_exec_push`, unchanged). If this preview parse fails,
            # or the directive has no usable string `message`, nothing is
            # bounded here — the caller's own fail-safe parse handles that
            # shape the same way it always has.
            try:
                directive = json.loads(decoded_stdout_for_cap)
            except (json.JSONDecodeError, ValueError):
                directive = None
            message = directive.get("message") if isinstance(directive, dict) else None
            if isinstance(message, str):
                message_tokens = estimate_tokens(message, cap_model)
                if message_tokens > cap_tokens:
                    denial_class = "exec_capture_message_bounded"
                    elided = message_tokens - cap_tokens
                    # architect's TESTS-READ catch (#5343): the marker
                    # itself costs tokens too. Appending it to a body
                    # already truncated to the FULL cap would let the
                    # combined message exceed cap_tokens — the exact
                    # thing this cap exists to prevent. Reserve the
                    # marker's own token cost from the body's budget FIRST
                    # so the combined result stays within cap_tokens (the
                    # marker is counted IN the cap, not appended on top of
                    # it).
                    marker = (
                        f"\n\n… ({elided} estimated tokens elided by the "
                        "context-budget-derived hook-output cap)"
                    )
                    marker_tokens = estimate_tokens(marker, cap_model)
                    body_budget = max(0, cap_tokens - marker_tokens)
                    bounded_message = hard_truncate_summary(message, body_budget, cap_model) + marker
                    directive["message"] = bounded_message
                    bounded_stdout = json.dumps(directive)
                    _log.warning(
                        "shell-hook %r exec_capture message (%d estimated tokens) "
                        "exceeds the context-budget-derived cap (%d tokens) — "
                        "message bounded in place (%d tokens elided, structure "
                        "and other fields preserved, push still delivered).",
                        command, message_tokens, cap_tokens, elided,
                    )

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

        # Success. capture_stdout (exec_capture) → return decoded stdout for
        # the caller to parse; otherwise (exec) output is ignored. #5244: a
        # bounded-message verdict (computed above, before the emit_event
        # call, so it rides that SAME event) still returns the directive —
        # with its `message` field bounded — never None; only a genuine run
        # failure above returns None.
        if not capture_stdout:
            return None
        if bounded_stdout is not None:
            return bounded_stdout
        return result.stdout.decode("utf-8", errors="replace")

    except Exception as exc:
        _log.error("shell-hook %r: unexpected error: %s", command, exc)
        return None
