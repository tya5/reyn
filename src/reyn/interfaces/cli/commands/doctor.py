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

## Scope of this slice (PR-3a + PR-2 + PR-2b)

C-7 (disk: declared retention ↔ actual bytes/age for ``.reyn/events/``, plus
policy-independent visibility for ``.reyn/media/`` / ``.reyn/tool-results/``
/ every ``history.jsonl`` — none of which has a declared retention policy
yet, #4478/#4476 Phase 2 unimplemented). C-1 (hook argv[0] launch probe,
#4364 PR-2, architect ruling): every configured ``exec``/``exec_capture``
hook's argv[0] is probed under the SAME sandbox backend + the SAME per-hook
policy a real dispatch would use (:mod:`reyn.security.sandbox.probe_argv`),
NEVER with the hook's own configured arguments (D-2: a real run, even a
partial one, is the one thing this command must never do). C-2
(zero-responder subscription, #4364 PR-2b, architect ruling): for each of
the 4 external ingress points (``hooks.schema_registry._EXTERNAL_POINTS``),
check whether a PRODUCER exists (declared config for ``file_changed``/
``cron_fired``, past audit-log evidence for ``mcp_resource_updated``/
``webhook_received`` — subscriptions themselves are a volatile op-level
concept doctor cannot see from a separate process, so the check pairs
producer↔consumer, not subscription↔consumer) and, only where one does,
whether any consumer (hook) is registered for it. ``webhook_received``
had no config or audit-log surface at all until #4618 gave it its own
audit-event kind (#4620) — it now routes through the SAME windowed
evidence-based check ``mcp_resource_updated`` already had. C-5 (sandbox
posture, this slice's own addition): declared ``sandbox.backend``/
``sandbox.on_unsupported``/``sandbox.policy`` next to the ACTUALLY
RESOLVED backend (:func:`reyn.security.sandbox.launcher.resolve_backend`
— the SAME production resolution C-1's probe already calls, which
self-tests a real deny before returning a backend, #2983). Architect's
ruling (#4364): "which backend resolved" already witnesses enforcement
(a backend that cannot enforce is treated as absent at resolution,
``sandbox.on_unsupported`` applied) — doctor reports that resolution's
OWN verdict, never a second probe of its own. Doctor has no op context,
so it does NOT merge a caller-supplied ``write_paths`` floor the way a
real dispatch would (:func:`reyn.security.sandbox.policy.
resolve_sandbox_policy` needs one, and inventing a stand-in would
contrast against nothing real) — only the declared ``sandbox.policy``
dict's own write-scope keys are shown, next to the resolved backend
name. C-3(b) (MCP negotiated version/capabilities, this slice's own
addition): for each declared ``mcp.servers`` entry, the newest
``mcp_initialized`` audit-event's ``negotiated_version``/``capabilities``
— the SAME windowed evidence-based scan C-2's ``mcp_resource_updated``/
``webhook_received`` checks already use
(:func:`_mcp_initialized_evidence`, sharing ``_MCP_EVENT_SCAN_MAX_FILES``
and #4624's empty-history branch), never a live probe (C-3(a), a real
``tools/list`` connect, was ruled unnecessary — the evidence already
exists from connections ``reyn`` itself made, and a held MCP connection
is a session concept doctor's separate one-shot process cannot observe
directly anyway, the same architect correction C-2's own
producer↔consumer design rests on). C-4 (model/api_base reachability,
this slice's own addition, question REPLACED per this session's
ruling): the original ask was a real litellm completion call — doctor
charging the operator for inference is exactly what the cross-cutting
cost/budget band exists to keep OS-internal diagnostics from doing.
Replaced with a 0-token ``GET {api_base}/v1/models``
(:func:`_print_model_reachability`, via
:func:`reyn._network.build_sync_http_client` — never a free-hand
``httpx.Client(...)``, so this call site is covered by #3075's
standard-proxy-env/CA completeness gate) — reachability from the HTTP
response itself (any response, including 401/403, proves reachability;
API keys are NEVER read, litellm-boundary convention, no
``Authorization`` header sent) and, when the response lists models,
whether each declared ``llm.models`` entry's BARE name (stripped of the
``provider/`` routing prefix) is in that list — architect's own repro
was exactly this name-form mismatch. Only ``llm.api_base`` (a LiteLLM
proxy) is checked; a provider with no declared ``api_base`` has no URL
this module knows to probe, so it prints "not checked" (D-3) rather
than guessing a per-provider hosted endpoint. C-6's remaining named pair
(listen port) is a later slice — it needs its own NEW measurement code
(introspecting a live bound socket), not reuse of an existing function
the way C-1/C-2/C-3(b)/C-4/C-5/C-7 are.
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
    "sandbox.backend",
    "sandbox.on_unsupported",
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

    # ── C-2: zero-responder external subscription (#4364 PR-2b) ────────────
    print()
    print("External-event producer/consumer pairing (a producer with 0")
    print("subscribing hooks is a real gap; a point with no producer is not")
    print("reported — see 'not checked' below for why some points aren't):")
    _print_external_point_pairing(config, resolved_root)

    # ── C-5: resolved sandbox posture (#4364, architect ruling) ────────────
    print()
    print("Sandbox posture — declared vs. RESOLVED (absence of a declaration")
    print("does not mean unrestricted; see the resolved backend below):")
    _print_sandbox_posture(config)

    # ── C-3(b): MCP negotiated protocol version + capabilities (#4364) ─────
    print()
    print("MCP servers — last negotiated version/capabilities (audit-log")
    print("evidence, not a live probe; D-2: doctor never connects):")
    _print_mcp_negotiation(config, resolved_root)

    # ── C-4: model/api_base reachability, 0-token (#4364) ──────────────────
    print()
    print("Model reachability — 0-token GET {api_base}/v1/models (never a")
    print("real completion call; D-2: doctor never spends inference cost):")
    _print_model_reachability(config)


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


# ── C-2: zero-responder subscription (#4364 PR-2b) ──────────────────────────
#
# Architect's own correction mid-design: the original C-2 spec asked "is
# something subscribed?" — unanswerable from a separate process, because an
# MCP resource subscription lives only on a HELD connection (mcp_subscribe_
# resource.py's own docstring: "a subscription is only meaningful on a HELD
# (persistent) connection") — volatile, not config. The corrected pairing is
# PRODUCER <-> CONSUMER: the consumer side (a hook registered for a point) is
# always readable from config; the producer side is readable ONLY where a
# static declaration or a past audit-log record exists.
#
# #4620: `webhook_received` USED to have neither (no config surface, no
# AUDIT_EVENT_KINDS entry) and lived here — #4618 (same night, same repo)
# gave it an audit-event of its own (webhook_routing.py), the same
# evidence-based surface `mcp_resource_updated` already had. That made
# this constant's own reasoning stale without anyone editing this file:
# the printed "no config or audit-log surface exists" claim went false
# the moment #4618 merged. `webhook_received` now routes through the
# SAME windowed evidence-based check as `mcp_resource_updated` (below) —
# this constant stays EMPTY, not deleted, so a future point that
# genuinely has no producer surface has somewhere to land without
# reinventing the D-3 disclosure. `cron_fired`/`file_changed` do NOT
# move here or into the windowed check — both already have a COMPLETE
# config-declared producer surface (`cron.jobs[]` / `fs_watch.paths`),
# and turning a complete read into a windowed scan would strictly lose
# information for no gain (architect's own point, #4620).
_UNCHECKABLE_EXTERNAL_POINTS: Final[tuple[str, ...]] = ()

# .reyn/events is append-only with no kind index (reyn-dir-layout.md) — a
# kind lookup is a scan. #4479 retention normally bounds the file count, but
# an operator who disabled retention (0 = off, both axes) has no upper
# bound. This check only needs "did it happen at least once" (architect's
# own ruling), so a bounded newest-first scan is exact for a POSITIVE
# result — an early exit can never turn a real positive into a false
# negative. #4614 correction: the negative case is NOT merely a display
# concern — "not seen in the newest N files" and "genuinely never
# happened" are indistinguishable from a bool, so a producer whose last
# arrival is older than the window (0 subscribing hooks, the exact state
# C-2 exists to catch) silently printed NOTHING before this fix. This
# DOES bear on correctness, which is why the window is unconditionally
# disclosed in the output below (D-3), not just alongside a positive
# finding — never widen this number without keeping that disclosure.
_MCP_EVENT_SCAN_MAX_FILES: Final[int] = 20


def _external_event_kind_seen(events_dir: Path, kind: str) -> "tuple[bool, int]":
    """(seen, files_scanned) — whether *kind* appears in any of the newest
    ``_MCP_EVENT_SCAN_MAX_FILES`` dated ``.jsonl`` files under *events_dir*.
    ``events_dir`` missing → ``(False, 0)`` (nothing written yet, not an
    error). Shared by every windowed-evidence external point (#4620:
    generalized from ``mcp_resource_updated``-only so ``webhook_received``
    can route through the identical check once it has its own audit-event
    kind — the scan itself has no point-specific logic, only *kind*
    differs)."""
    import json

    from reyn.core.events.event_purge import collect_dated_files

    if not events_dir.is_dir():
        return False, 0
    # collect_dated_files sorts oldest-first (its own docstring) — reverse
    # for "newest N" so the early exit favors recent evidence.
    newest_first = list(reversed(collect_dated_files(events_dir)))
    window = newest_first[:_MCP_EVENT_SCAN_MAX_FILES]
    for path, _start_date in window:
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == kind:
                        return True, len(window)
        except OSError:
            continue
    return False, len(window)


def _mcp_initialized_evidence(events_dir: Path) -> "tuple[dict[str, dict], int]":
    """(per-server evidence, files_scanned) — for each ``server`` named in
    the newest ``_MCP_EVENT_SCAN_MAX_FILES`` dated ``.jsonl`` files' own
    ``mcp_initialized`` records, its MOST RECENT ``negotiated_version`` /
    ``capabilities`` (#4364 C-3(b)). Same windowed-scan shape as
    :func:`_external_event_kind_seen` — reused rather than re-derived — but
    per-KEY evidence instead of a single seen/not-seen bool, since C-3(b)
    is "what did we last negotiate with this server," not "did this point
    ever fire." Scanning newest-first means the FIRST record seen for a
    given server is already its most recent — a server already recorded
    is never overwritten by an older file. ``events_dir`` missing →
    ``({}, 0)``, matching ``_external_event_kind_seen``'s own contract."""
    import json

    from reyn.core.events.event_purge import collect_dated_files

    if not events_dir.is_dir():
        return {}, 0
    newest_first = list(reversed(collect_dated_files(events_dir)))
    window = newest_first[:_MCP_EVENT_SCAN_MAX_FILES]
    evidence: dict[str, dict] = {}
    for path, _start_date in window:
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "mcp_initialized":
                        continue
                    data = obj.get("data") or {}
                    server = data.get("server")
                    if not isinstance(server, str) or server in evidence:
                        continue
                    evidence[server] = {
                        "negotiated_version": data.get("negotiated_version"),
                        "capabilities": data.get("capabilities"),
                    }
        except OSError:
            continue
    return evidence, len(window)


def _merged_hook_registry(config: object, project_root: Path) -> object:
    """The 3-layer ADDITIVE combine (#4555's own registry read as the
    reference — ``Session._build_hook_registry``'s SAME shape, minus the
    4th, session-scoped per-session layer doctor's standalone-process
    read has no way to reach): reyn.yaml's top-level ``hooks:`` (startup,
    trusted — must load, fail loud) -> ``.reyn/config/hooks.yaml`` (runtime
    IN-set) -> every ``.reyn/agents/<name>/hooks.yaml`` (per-agent). Each
    untrusted layer is try-added independently — a malformed one is
    dropped, its siblings kept (same per-layer resilience as the real
    Session combine), so one bad file cannot hide a real zero-responder
    gap in the layers that DID load.

    The startup layer is read off *config* (``config.hooks`` — the SAME
    already-``project_root``-resolved ``load_config`` result ``run()``
    builds every other C-2/C-7 reading from), not a second, separately-
    invoked ``build_policy_tier_config()`` call — #4555's own review
    caught exactly this shape of bug (a second root-resolution call that
    silently reads the WRONG project when the process cwd differs from
    *project_root*, e.g. ``reyn doctor --project-root <other-dir>`` or
    any test driving ``run()`` directly with a ``tmp_path``)."""
    from reyn.config.loader import load_hot_reload_config, load_per_agent_hooks
    from reyn.hooks.loader import HookConfigError, load_hooks

    policy_hooks = getattr(config, "hooks", None) or []
    combined = list(policy_hooks) if isinstance(policy_hooks, list) else []
    registry = load_hooks(combined)  # trusted startup layer — fail loud

    in_set_merged = load_hot_reload_config(project_root)
    in_set_hooks = in_set_merged.get("hooks") or []
    layers = [("runtime", in_set_hooks if isinstance(in_set_hooks, list) else [])]

    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent_hooks = load_per_agent_hooks(project_root, agent_dir.name)
            if agent_hooks:
                layers.append((f"per-agent:{agent_dir.name}", agent_hooks))

    for _label, layer in layers:
        if not layer:
            continue
        try:
            registry = load_hooks(combined + list(layer))
            combined = combined + list(layer)
        except HookConfigError:
            # Untrusted layer, malformed — dropped, siblings kept (same
            # resilience as Session._build_hook_registry's real combine).
            continue
    return registry


def _print_external_point_pairing(config: object, project_root: Path) -> None:
    from reyn.hooks.schema_registry import _EXTERNAL_POINTS

    registry = _merged_hook_registry(config, project_root)
    events_dir = project_root / ".reyn" / "events"

    for point in _EXTERNAL_POINTS:
        if point in _UNCHECKABLE_EXTERNAL_POINTS:
            print(f"  ? {point}: not checked (no config or audit-log surface exists for its producer)")
            continue

        if point == "file_changed":
            fs_watch = getattr(config, "fs_watch", None)
            paths = getattr(fs_watch, "paths", None) or []
            has_producer = bool(paths)
            evidence = f"{len(paths)} declared fs_watch path(s)"
        elif point == "cron_fired":
            cron = getattr(config, "cron", None)
            jobs = getattr(cron, "jobs", None) or []
            enabled_jobs = [j for j in jobs if getattr(j, "enabled", True)]
            has_producer = bool(enabled_jobs)
            evidence = f"{len(enabled_jobs)} enabled cron job(s)"
        elif point in ("mcp_resource_updated", "webhook_received"):
            # #4614/#4620: this check is windowed (a bounded scan, see
            # _MCP_EVENT_SCAN_MAX_FILES's own comment) — unlike
            # file_changed/cron_fired's complete config reads, "not seen"
            # here is NOT proof of "no producer": a producer whose last
            # arrival predates the window is indistinguishable from one
            # that never fired. Silently printing nothing in that case
            # (the pre-#4614 shape) hid the exact state C-2 exists to
            # catch — so this point is disclosed UNCONDITIONALLY (D-3),
            # never folded into the generic "no producer -> no finding"
            # rule below. #4620: webhook_received joined this branch once
            # #4618 gave it its own audit-event kind (matching its own
            # point name, same convention as mcp_resource_updated) — it
            # has no config surface at all, so it can ONLY ever be
            # evidence-based, never a complete config read like
            # file_changed/cron_fired.
            seen, scanned = _external_event_kind_seen(events_dir, point)
            consumers = registry.hooks_for(point)  # type: ignore[attr-defined]
            if seen and consumers:
                print(
                    f"  ✓ {point}: producer present (seen in the newest "
                    f"{scanned} event file(s) scanned), {len(consumers)} "
                    f"subscribing hook(s)",
                )
            elif seen:
                print(
                    f"  ✗ {point}: producer present (seen in the newest "
                    f"{scanned} event file(s) scanned) but 0 subscribing "
                    f"hooks — this point's notifications have nowhere to go",
                )
            elif scanned == 0:
                # #4624: scanned == 0 means .reyn/events has NO dated files
                # at all (a fresh install, or retention already purged
                # everything) — "a producer whose last arrival predates
                # the window" cannot be true when there is no window to
                # predate, so the #4614 caveat below would be a TRUE but
                # EMPTY statement here. An empty caveat printed on every
                # fresh install trains the reader to skip caveats, which
                # is exactly what #4614 introduced the caveat to prevent
                # (architect's finding, #4622 co-vet). Say the plain fact
                # instead — no window talk.
                print(f"  ? {point}: no event history yet")
            else:
                print(
                    f"  ? {point}: not seen in the newest {scanned} event "
                    f"file(s) scanned — a producer whose last arrival is "
                    f"older than that is not covered here, so this is NOT "
                    f"proof no producer exists",
                )
            continue
        else:  # pragma: no cover — _EXTERNAL_POINTS is the closed population
            continue

        if not has_producer:
            # D-2/D-3: a point with no producer gets no finding — reporting
            # "0 hooks" here would be noise, not signal (architect's own
            # ruling: "report only where a producer exists"). Does NOT
            # apply to mcp_resource_updated (handled above, its own
            # branch) — that check is windowed, so "no producer" can't be
            # asserted the same way a complete config read can.
            continue

        consumers = registry.hooks_for(point)  # type: ignore[attr-defined]
        if consumers:
            print(f"  ✓ {point}: producer present ({evidence}), {len(consumers)} subscribing hook(s)")
        else:
            print(
                f"  ✗ {point}: producer present ({evidence}) but 0 subscribing "
                f"hooks — this point's notifications have nowhere to go",
            )


# ── C-5: resolved sandbox posture (#4364, architect ruling) ─────────────────
#
# Architect's ruling, verbatim shape: declared = sandbox.policy/sandbox.backend
# (from config); effective = the resolved backend (+ if downgraded via
# on_unsupported, that fact). Doctor does not try a write of its own — same
# reason as C-1 (doctor never changes the environment; a deny is harmless to
# probe but a SUCCESS would not be, since doctor itself has no business
# writing files). "Which backend resolved" already carries the enforcement
# witness (backend.py's own get_default_backend: a backend that cannot
# enforce is treated exactly like one that is absent) — this is production's
# own gate reporting its own verdict, not a weaker doctor-invented probe.
#
# Doctor has NO op context, so it does not build a resolve_sandbox_policy()
# call (that needs a caller-supplied write_paths floor "this op needs this
# directory" — a value doctor cannot know and must not invent a stand-in
# for, per lead-coder's own ruling on this check). Only the declared
# sandbox.policy dict's own write-scope keys are shown, never a merged/
# resolved policy.
_SANDBOX_POLICY_WRITE_SCOPE_KEYS: Final[tuple[str, ...]] = (
    "allow_write_paths",
    "deny_write_paths",
)


def _print_sandbox_posture(config: object) -> None:
    from reyn.security.sandbox.launcher import resolve_backend

    sandbox_config = getattr(config, "sandbox", None)
    declared_backend = getattr(sandbox_config, "backend", "auto")
    declared_on_unsupported = getattr(sandbox_config, "on_unsupported", "warn")
    declared_policy = getattr(sandbox_config, "policy", None)

    print(
        f"  declared: sandbox.backend={declared_backend!r}, "
        f"sandbox.on_unsupported={declared_on_unsupported!r}",
    )
    if declared_policy:
        write_scope = {
            k: v for k, v in declared_policy.items()
            if k in _SANDBOX_POLICY_WRITE_SCOPE_KEYS
        }
        if write_scope:
            print(f"  declared write scope (sandbox.policy): {write_scope}")
        else:
            print(
                "  declared: sandbox.policy is set, but neither "
                "allow_write_paths nor deny_write_paths appears in it",
            )
    else:
        # This is the exact real-world case the check was written for
        # (#4364 architect note): "sandbox.policy: not declared" reads like
        # "unrestricted" but is NOT — the resolved backend below is what
        # actually governs.
        print("  declared: no sandbox.policy — NOT the same as unrestricted, see resolved backend below")

    try:
        resolved = resolve_backend(None, sandbox_config)
    except RuntimeError as exc:
        # on_unsupported="error" with no real backend available — the
        # RESOLUTION itself refuses (fail-closed), which doctor reports
        # rather than swallowing (D-1: measure, don't paper over a raise).
        print(f"  ✗ resolved: refuses to run ({exc})")
        return

    downgraded = declared_backend not in ("auto", "noop", resolved.name)
    if downgraded:
        print(
            f"  resolved: {resolved.name!r} — DOWNGRADED from declared "
            f"{declared_backend!r} (on_unsupported={declared_on_unsupported!r} "
            f"applied; a backend that cannot enforce is treated as absent, #2983)",
        )
    else:
        print(
            f"  resolved: {resolved.name!r} (production's own resolution — "
            f"a backend that cannot enforce is already treated as absent "
            f"at this step, #2983, so this name IS the enforcement witness)",
        )


def _print_mcp_negotiation(config: object, project_root: Path) -> None:
    """#4364 C-3(b): for each declared MCP server, the negotiated protocol
    version + capabilities from the newest ``mcp_initialized`` audit-event
    evidence — same windowed evidence-based shape C-2 already uses
    (:func:`_mcp_initialized_evidence`, reusing #4627's empty-history
    branch), not a live probe. C-3(a) (an actual `tools/list` connect) was
    ruled unnecessary — this evidence already exists in the audit log from
    the connections `reyn` itself already made, so a SEPARATE live
    reachability check from doctor would duplicate work doctor's own
    process cannot even perform faster (a held connection is a session
    concept, #4364 C-2's own architect correction). D-2: report-only,
    never connects.

    Also flags the #4631 shape defect (``mcp.<name>`` written where
    ``mcp.servers.<name>`` belongs) when it explains why nothing is
    declared here — reusing ``reyn config validate``'s own detector
    against the SAME merged ``mcp:`` dict this function already has, not
    a second raw-file read (validate needs to name which SOURCE FILE is
    wrong; this only needs to say the shape is wrong at all). Report-only,
    same as everything else here (D-2) — doctor never rewrites the entry."""
    from reyn.interfaces.cli.commands.config import _mcp_misplaced_server_entries

    mcp_config = getattr(config, "mcp", None) or {}
    servers = sorted((mcp_config.get("servers") if isinstance(mcp_config, dict) else None) or {})
    misplaced = _mcp_misplaced_server_entries(mcp_config)
    if not servers:
        if misplaced:
            print(
                f"  no MCP servers declared under mcp.servers — but "
                f"{len(misplaced)} entr{'y' if len(misplaced) == 1 else 'ies'} "
                f"directly under mcp: ({', '.join(sorted(misplaced))}) "
                f"look like misplaced server entries (see `reyn config validate`)",
            )
        else:
            print("  no MCP servers declared")
        return
    if misplaced:
        print(
            f"  note: {len(misplaced)} entr{'y' if len(misplaced) == 1 else 'ies'} "
            f"directly under mcp: ({', '.join(sorted(misplaced))}) also look "
            f"like misplaced server entries, alongside the declared servers "
            f"below (see `reyn config validate`)",
        )

    events_dir = project_root / ".reyn" / "events"
    evidence, scanned = _mcp_initialized_evidence(events_dir)
    for server in servers:
        seen = evidence.get(server)
        if seen is not None:
            version = seen.get("negotiated_version")
            caps = seen.get("capabilities") or []
            print(
                f"  ✓ {server}: last negotiated {version!r}, "
                f"capabilities={caps}",
            )
        elif scanned == 0:
            # #4627: no window to predate — say the plain fact, not the
            # windowed caveat (see _external_event_kind_seen's own sibling
            # branch in _print_external_point_pairing for the same shape).
            print(f"  ? {server}: no event history yet")
        else:
            print(
                f"  ? {server}: not seen in the newest {scanned} event "
                f"file(s) scanned — a connection whose last occurrence is "
                f"older than that is not covered here, so this is NOT "
                f"proof the server was never reached",
            )


_MODEL_REACHABILITY_TIMEOUT_SECONDS: Final[float] = 3.0


def _print_model_reachability(config: object) -> None:
    """#4364 C-4, question REPLACED per this session's ruling (owner-bound,
    band-relevant): the original ask was a real litellm completion probe —
    doctor charging the operator a real inference call is exactly the kind
    of thing the cross-cutting cost/budget band exists to keep OS-internal
    diagnostics from doing. Replaced with a 0-token ``GET {api_base}/v1/models``
    — reachability AND (when the response lists models) whether the
    declared model name's BARE form is accepted are both answered by the
    same request, at zero inference cost. API keys are NEVER read here
    (litellm-boundary convention, owner's standing instruction) — the
    request carries no Authorization header; a provider that requires one
    to list models still proves reachability by responding at all (401/403
    is a real HTTP response, not a connection failure).

    Only ``llm.api_base`` (a LiteLLM proxy) is checked — a provider with no
    declared ``api_base`` routes straight to its own hosted endpoint, which
    this module has no per-provider URL table for and is not this check's
    motivating case (architect's own repro was a local proxy). D-3: prints
    "cannot confirm" rather than silently omitting the line (same shape as
    C-2's ``webhook_received`` before it gained a surface, #4618/#4620)."""
    llm_config = getattr(config, "llm", None)
    api_base = (getattr(llm_config, "api_base", "") or "").strip()
    models = getattr(llm_config, "models", None) or {}

    if not api_base:
        print(
            "  ? not checked — no llm.api_base declared (a provider-hosted "
            "endpoint has no URL this check knows to probe)",
        )
        return

    from reyn._network import build_sync_http_client

    url = api_base.rstrip("/") + "/v1/models"
    try:
        with build_sync_http_client(
            egress="doctor_model_reachability", timeout=_MODEL_REACHABILITY_TIMEOUT_SECONDS,
        ) as client:
            response = client.get(url)
    except Exception as exc:  # noqa: BLE001 — D-2: report the failure, never raise
        print(f"  ✗ {api_base}: unreachable ({type(exc).__name__}: {exc})")
        return

    print(f"  ✓ {api_base}: reachable (HTTP {response.status_code})")

    listed_ids: "set[str] | None" = None
    if response.status_code == 200:
        try:
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, list):
                listed_ids = {
                    str(item["id"]) for item in data
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
        except Exception:  # noqa: BLE001 — malformed body is not a crash
            listed_ids = None

    if listed_ids is None:
        print(
            f"    model name form not checked — HTTP {response.status_code} "
            f"response did not carry a model list",
        )
        return

    for model_class in sorted(models):
        raw_name = models[model_class]
        model_name = raw_name if isinstance(raw_name, str) else raw_name.get("model") if isinstance(raw_name, dict) else None
        if not isinstance(model_name, str):
            continue
        bare_name = model_name.split("/", 1)[1] if "/" in model_name else model_name
        if bare_name in listed_ids:
            print(f"    ✓ {model_class} ({bare_name!r}): accepted by the proxy's model list")
        else:
            print(
                f"    ✗ {model_class} ({bare_name!r}): NOT in the proxy's model "
                f"list — check the name form (bare vs 'provider/name')",
            )
