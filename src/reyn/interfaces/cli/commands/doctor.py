"""`reyn doctor` — health checks that report what was MEASURED, never what
was declared (#4364 PR-3a: skeleton + D-3's coverage-disclosure framework +
C-7's disk visibility).

## D-1 (owner/lead-coder ruling, #4364): measure, don't assert

Every line this command prints is the result of actually reading a live
effect — a file on disk, a resolved value — never a restatement of what
``reyn.yaml`` (or any other config source) DECLARES. "A hook is registered"
is ``reyn config show``'s job; "a hook's argv actually launched" would be
doctor's (a later slice — see #4364's own C-1/C-2). This module's own slice
(C-7, disk) follows the same rule: every number below comes from
``os.stat``/``Path.rglob``, never from re-reading a config object.

## D-2 (owner/lead-coder ruling): report-only, never mutate

``reyn`` gives ``doctor`` reach into sandbox / MCP / hook internals — wider
than any other CLI surface. Every prior local-CLI precedent surveyed in
#4364's own investigation (``npm doctor``, ``claude doctor``, oh-my-opencode
``doctor``) either reports-only or requires explicit confirmation before
acting; none auto-repairs from a CLI invocation. This module never deletes,
never writes, never calls ``apply_auto_purge``/``purge_files`` — only the
read-only ``select_purge_targets`` (a query, not an action).

## D-3 (architect ruling, #4364, generalized from C-5/C-6): disclose what
was NOT measured, and how "measurable" was decided

A doctor that only ever prints what it happens to check reads, on a clean
run, identical to a doctor that checked everything — the #4480/#4478/#4479
pattern this repo hit four times in one night (a declaration outrunning
what was actually verified). This module's own summary line is therefore
not "N checks passed" but:

    <N> config leaves total, <M> have a measurable effective surface,
    <N-M> uncovered (no live-effect reader exists for them yet)

"Measurable effective surface" is a genuinely hand-picked judgment call —
architect's own words, not avoidable — so the CRITERION is printed
alongside the count, not just the number: :data:`_MEASURABLE_LEAF_KEYS`
below is the literal, auditable list, and a future PR widening it changes
this file's own diff, not a silent number. Do not print an unqualified "N
items checked" anywhere in this module or its own docs — see this issue's
own owner note: any claim about coverage must be DERIVABLE from this
disclosure line, never asserted independently of it.

## Scope of this slice (PR-3a + PR-2)

C-7 (disk: declared retention ↔ actual bytes/age for ``.reyn/events/``, plus
policy-independent visibility for ``.reyn/media/`` / ``.reyn/tool-results/``
/ every ``history.jsonl`` — none of which has a declared retention policy
yet, #4478/#4476 Phase 2 unimplemented). C-1 (hook argv[0] launch probe,
#4364 PR-2, architect ruling): every configured ``exec``/``exec_capture``
hook's argv[0] is probed under the SAME sandbox backend + the SAME per-hook
policy a real dispatch would use (:mod:`reyn.security.sandbox.probe_argv`),
NEVER with the hook's own configured arguments (D-2: a real run, even a
partial one, is the one thing this command must never do). C-5 (sandbox
posture) and C-6's other named pairs (listen port, model-name acceptance)
are PR-3b — each needs its own NEW measurement code (introspecting a live
bound socket, a real litellm probe call), not reuse of an existing function
the way C-1/C-7 are. C-2 (zero-responder subscription) remains dispatched
separately, not implemented in this module.
"""
from __future__ import annotations

import argparse
import shutil
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from reyn.hooks.schema import HookDef


def register(sub) -> None:
    p = sub.add_parser(
        "doctor",
        help="Report measured (not declared) health — disk retention, storage footprint",
    )
    p.add_argument(
        "--project-root",
        default=".",
        help="Project root containing .reyn/ (default: current directory).",
    )
    p.set_defaults(func=run)


# #4364 D-3: the literal, auditable set of config leaves this doctor slice
# actually measures a live effect for — printed alongside the N/M/N-M
# summary so "measurable" stays a disclosed judgment call, not a hidden one.
# Widening this set is a diff to THIS list, never a silent count change.
_MEASURABLE_LEAF_KEYS: Final[tuple[str, ...]] = (
    "audit_events.cleanup_period_days",
    "audit_events.max_disk_usage_percent",
)
_MEASURABILITY_CRITERION: Final[str] = (
    "a leaf counts as measurable here iff this module reads its LIVE EFFECT "
    "(a file's actual age/size on disk), not merely the config value itself "
    "-- re-reading the config object back is not a measurement (#4364 "
    "architect note: an assignment can succeed while never being used)"
)


def _events_dir_stats(root: Path) -> "tuple[int, int, date | None]":
    """(file_count, total_bytes, oldest_start_date) for every dated
    ``.jsonl`` under ``root`` — read-only, mirrors
    ``event_purge.collect_dated_files``'s own file discovery so this
    reports on exactly the population a real purge would consider."""
    from reyn.core.events.event_purge import collect_dated_files

    files = collect_dated_files(root)
    count = len(files)
    total_bytes = 0
    oldest: "date | None" = None
    for path, start_date in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue
        if start_date is not None and (oldest is None or start_date < oldest):
            oldest = start_date
    return count, total_bytes, oldest


def run(args: argparse.Namespace) -> None:
    from reyn.config.config_schema import walk_config_schema
    from reyn.config.loader import _find_project_root, load_config
    from reyn.core.events.event_purge import select_purge_targets
    from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
    from reyn.runtime.history_tail_reader import aggregate_history_stats

    project_root = Path(args.project_root).resolve()
    resolved_root = _find_project_root(project_root) or project_root

    # ── D-3: coverage disclosure — printed FIRST, before any check result,
    # so a reader sees the scope claim before the findings (#4364 owner note:
    # a scope claim buried after results reads as an afterthought).
    total_leaves = len(walk_config_schema())
    measured = len(_MEASURABLE_LEAF_KEYS)
    uncovered = total_leaves - measured
    print(
        f"{total_leaves} config leaves total, {measured} have a measurable "
        f"effective surface (checked below), {uncovered} uncovered.",
    )
    print(f"  Measurable means: {_MEASURABILITY_CRITERION}")
    print()

    # ── C-7: .reyn/events/ — declared retention vs actual state.
    config = load_config(project_root)
    cleanup_period_days = config.audit_events.cleanup_period_days
    max_disk_usage_percent = config.audit_events.max_disk_usage_percent
    events_dir = resolved_root / ".reyn" / "events"

    print("Disk usage — declared retention vs. actual (.reyn/events/):")
    if not events_dir.is_dir():
        print("  no .reyn/events/ directory yet (nothing written)")
    else:
        count, total_bytes, oldest = _events_dir_stats(events_dir)
        print(
            f"  declared: cleanup_period_days={cleanup_period_days} "
            f"(0=disabled), max_disk_usage_percent={max_disk_usage_percent} "
            f"(0=disabled)",
        )
        oldest_desc = f"{(date.today() - oldest).days} day(s) old" if oldest else "n/a (no dated files)"
        print(f"  actual:   {count} file(s), {total_bytes:,} bytes, oldest = {oldest_desc}")
        # Read-only query — never deletes (D-2). A non-empty result here
        # means the declared policy is NOT currently being honored: files
        # exist RIGHT NOW that the policy's own axes would purge, but
        # haven't been (the automatic trigger only fires on write/rotation,
        # not continuously — see event_store.py's own submit_auto_purge).
        pending = select_purge_targets(
            events_dir,
            max_age_days=cleanup_period_days,
            max_disk_usage_percent=max_disk_usage_percent,
        )
        if pending:
            print(
                f"  ⚠ {len(pending)} file(s) currently exceed the declared "
                f"policy but have not been purged yet (purge fires on the "
                f"next write/rotation, not continuously) — run 'reyn events "
                f"purge' to apply the policy now if you don't want to wait.",
            )
        else:
            print("  ✓ no file currently exceeds the declared policy")

    # ── C-7: media/ / tool-results/ / history.jsonl — no declared retention
    # policy exists for any of these yet (#4478/#4476 Phase 2 unimplemented)
    # — the visibility itself IS the finding (#4480: "no one owns this
    # resource" made visible, not asserted).
    print()
    print("Disk usage — no declared retention policy (visibility only):")
    store = MediaStore(MediaStoreConfig(), project_root=resolved_root)
    media_stats = store.storage_stats()
    hist = aggregate_history_stats(resolved_root)
    print(
        f"  media/:         {media_stats.media_file_count} file(s), "
        f"{media_stats.media_bytes:,} bytes",
    )
    print(
        f"  tool-results/:  {media_stats.tool_result_file_count} file(s), "
        f"{media_stats.tool_result_bytes:,} bytes",
    )
    print(
        f"  history.jsonl:  {hist.file_count} file(s), {hist.total_bytes:,} "
        f"bytes, {hist.total_lines:,} turn(s)",
    )
    total_bytes_all = (
        media_stats.media_bytes + media_stats.tool_result_bytes + hist.total_bytes
    )
    try:
        free_bytes = shutil.disk_usage(resolved_root).free
    except OSError:
        free_bytes = None
    print(f"  total (media+tool-results+history): {total_bytes_all:,} bytes")
    if free_bytes is not None:
        print(f"  (filesystem free space: {free_bytes:,} bytes)")

    # ── C-1: hook argv[0] launch probe (#4364 PR-2, architect ruling) ──────
    print()
    print("Hook launch probe (argv[0] only, no configured args — a launch")
    print("probe, not a run; D-2: doctor never executes a hook for real):")
    _print_hook_probe_results(config)


def _configured_exec_hooks(config: object) -> "list[HookDef]":
    """Every configured ``exec``/``exec_capture`` ``HookDef`` — the caller
    reads ``.name``/``.on``/``.exec``/``.exec_capture``/``.subprocess``/
    ``.network``/``.write_paths`` directly off it, so the per-hook sandbox
    knobs travel with the argv rather than being stripped here."""
    from reyn.hooks.loader import load_hooks
    from reyn.hooks.schema import ALLOWED_HOOK_POINTS

    registry = load_hooks(getattr(config, "hooks", None) or [])
    all_defs = [
        hook_def
        for point in ALLOWED_HOOK_POINTS
        for hook_def in registry.hooks_for(point)
    ]
    return [
        hook_def for hook_def in all_defs
        if hook_def.exec is not None or hook_def.exec_capture is not None
    ]


def _print_hook_probe_results(config: Any) -> None:
    import asyncio

    from reyn.security.sandbox import SandboxPolicy as _SandboxPolicy
    from reyn.security.sandbox.launcher import resolve_backend
    from reyn.security.sandbox.probe_argv import probe_argv

    hooks = _configured_exec_hooks(config)
    if not hooks:
        print("  no exec/exec_capture hooks configured")
        return

    sandbox_config = getattr(config, "sandbox", None)
    backend = resolve_backend(None, sandbox_config)

    async def _probe_all() -> "list[tuple[str, tuple[str, ...], Any]]":
        results = []
        for hook_def in hooks:
            argv = hook_def.exec if hook_def.exec is not None else hook_def.exec_capture
            # `_configured_exec_hooks` already filtered to `exec is not None or
            # exec_capture is not None` — one of the two is always set here.
            assert argv is not None
            label = hook_def.name or hook_def.on
            # #2827/#3005: the SAME per-hook knobs -> the SAME default policy
            # shape `run_shell_hook` builds for a real dispatch (shell_runner.py)
            # — "same backend, same profile" (architect's own C-1 spec), not a
            # doctor-invented one. `None` (omitted) keeps the floor, exactly as
            # the real dispatch path does.
            policy = _SandboxPolicy(
                network=bool(hook_def.network) if hook_def.network is not None else False,
                deny_subprocess=(
                    not hook_def.subprocess if hook_def.subprocess is not None else True
                ),
                write_paths=list(hook_def.write_paths) if hook_def.write_paths is not None else [],
            )
            results.append((label, argv, await probe_argv(backend, argv, policy)))
        return results

    probed = asyncio.run(_probe_all())
    for label, argv, result in probed:
        argv0 = argv[0] if argv else "(empty argv)"
        if result is None:
            print(f"  ? {label}: cannot probe under backend {backend.name!r} (no control binary)")
        elif result == "ok":
            print(f"  ✓ {label}: {argv0!r} is runnable under this hook's sandbox")
        elif result == "target_failed":
            print(
                f"  ✗ {label}: {argv0!r} is NOT runnable under this hook's sandbox "
                f"(probed with no arguments — a program that requires arguments "
                f"will report here without being broken)",
            )
        else:  # "sandbox_failed"
            print(
                f"  ⚠ {label}: the sandbox itself failed its own known-good "
                f"control binary — this is not about {argv0!r}",
            )
