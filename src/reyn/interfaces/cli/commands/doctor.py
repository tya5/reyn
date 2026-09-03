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
than guessing a per-provider hosted endpoint. C-6's own "listen port"
example (measured #4364, before writing any code, per lead-coder's
instruction) does NOT apply to reyn's own config surface: ``reyn web``'s
``--host``/``--port`` are bare CLI arguments (``interfaces/cli/commands/
web.py``) with NO corresponding ``ReynConfig`` field anywhere — verified
by walking the full schema (:func:`reyn.config.config_schema.
walk_config_schema`) for any key naming a port; there is none. C-6's
motivating incident (architect's own report, #4364: a *different*
project's ``settings.port`` silently stopped taking effect across a
dependency bump) illustrates the GENERAL "declared ≠ effective" shape —
it was never a claim that reyn itself declares a listen port anywhere.
A declared-vs-effective pair needs a DECLARATION to pair against; reyn's
own port is set once, per-invocation, by the operator's own CLI
argument — there is nothing upstream of that argument for doctor (a
separate, later, short-lived process with no view into a sibling
``reyn web`` process's argv) to compare it with. C-6's GENERAL form
(declared config ↔ ACTUALLY-effective value, never re-reading the
declaration as its own witness) is already implemented — C-5 above is
architect's own named special case of it (sandbox posture: declared
``sandbox.*`` next to the resolved backend), not a separate, still-owed
slice.

#5226 (this slice's own addition, lead-coder ruling): a NEW category —
not declared-vs-effective, but "reyn cannot currently answer how many
of ITSELF are alive, or whose, without an operator manually shelling
out to ``ps``+``lsof``". :func:`_print_process_registry` reads
:func:`~reyn.runtime.process_registry.live_processes` — a live read of
PID-keyed markers each reyn CLI process writes about ITSELF at startup
(``interfaces/cli/__init__.py:main()``, the same hook
``set_process_title`` uses), never a process-table scan of its own
(that is the OS's job, per lead-coder's own ruling). D-2 unchanged:
report-only — no kill, no TTL expiry; that judgment call is explicitly
out of #5226's own scope until the count is visible at all.

#4364 (this slice, 2026-09-02, lead-coder assignment — issue-comment
candidate ②, chosen because it directly matches the owner's own
motivating incident, "会社で llm.model の設定効果なさそうな挙動を見た"):
declared vs. COMPOSED for every #4206 leaf with a live agent-layer
override receptacle (:func:`_print_bounding_preference_composition`) —
the general form C-5/C-6 already established (declared config next to
its resolved/enforced effect), applied to axes ② (``BOUNDING_KEYS``,
narrowest-wins, ``runtime/bounding.py``) and ③ (``PREFERENCE_KEYS``,
last-wins, ``runtime/preferences.py``) instead of sandbox posture. Reads
:func:`~reyn.config.config_schema.walk_config_schema`'s own ``axis``/
``override_enabled`` metadata (#5673, landed the same day) rather than a
second hand list — the exact "no new machinery, read and display what's
already there" increment #4364 requires. Loads each agent's own
``profile.yaml`` via :meth:`~reyn.runtime.profile.AgentProfile.load` —
the SAME validated loader a real session-spawn already uses — never a
second hand-parse. Reports mismatches only (an agent that sets no
override, or narrows to the same value, produces no line). Session-layer
overrides (``resolve_preference``'s third parameter) are NOT visible to
doctor — a separate, one-shot process with no live session, the same D-2
limitation C-2/C-3(b) already disclose for other session-scoped state;
this function's own printed line says so explicitly. Deliberately does
NOT cover ``B-1``..``B-4`` (the ``config validate``-side candidates from
the same issue-comment) — lead-coder's explicit scope note: handled
separately.
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
    # #4364 (this slice, 2026-09-02): every leaf carrying a LIVE
    # agent-layer override receptacle (config_axis.py's #4206 axes ②/③,
    # `override_enabled=True`) — this module now reads the COMPOSED
    # value for each (see `_print_bounding_preference_composition`),
    # not merely the declared one, so each qualifies as measurable by
    # this list's own criterion below.
    "llm.model",
    "chat.reasoning.display",
    "cost.daily_cost_usd.warn_ratio",
    "cost.daily_tokens.warn_ratio",
    "cost.monthly_cost_usd.warn_ratio",
    "cost.monthly_tokens.warn_ratio",
    "cost.per_agent_cost_usd.warn_ratio",
    "cost.per_agent_tokens.warn_ratio",
    "cost.rate_limit_warn_ratio",
    "output_language",
    # #4364 (this slice, 2026-09-03): reads the LIVE parsed
    # `.transports` dict and names the actual consumer call sites
    # (`_print_external_transports_status`) — not merely re-reading
    # the declared value back.
    "external_transports",
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
    reports on exactly the population a real purge would consider.

    #4364 C-7 census (#4671): ``count`` used to be fixed to
    ``len(files)`` BEFORE the per-file ``stat()`` loop, so a file that
    vanished mid-scan (e.g. a concurrent ``reyn events purge``) still
    counted toward ``count`` while silently NOT counting toward
    ``total_bytes`` — the two figures could disagree with no disclosure
    (a D-3 gap). Fixed by only incrementing ``count`` alongside a
    successful ``stat()``, keeping both figures over the SAME population.
    Only ``FileNotFoundError`` is treated as "vanished mid-scan, skip
    silently" — any OTHER ``OSError`` (e.g. a permission error) is
    deliberately not swallowed; see
    ``history_tail_reader.history_file_stats``'s identical reasoning.
    """
    from reyn.core.events.event_purge import collect_dated_files

    files = collect_dated_files(root)
    count = 0
    total_bytes = 0
    oldest: "date | None" = None
    for path, start_date in files:
        try:
            total_bytes += path.stat().st_size
        except FileNotFoundError:
            continue
        count += 1
        if start_date is not None and (oldest is None or start_date < oldest):
            oldest = start_date
    return count, total_bytes, oldest


def _print_storage_cap_status(config: Any, media_stats: Any) -> None:
    """part of #4364 (2026-09-02): ``storage.max_bytes``/``storage.pin``
    (:class:`~reyn.config.infra.StorageConfig`, #5366) is the ONE
    project-wide, cross-session disk cap covering ``.reyn/media/`` +
    ``.reyn/memory/history-content/`` TOGETHER (#4478 — one operator
    number, not two, so two individually-under-cap trees can't silently
    sum over it). ``media_stats`` is the SAME
    :class:`~reyn.data.workspace.media_store.MediaStorageStats` snapshot
    ``run()`` already computed via ``store.storage_stats()`` (#5652's own
    recursive ``_dir_stats_recursive`` — correctly counts a nested
    ``<agent>/<session_id>/`` write) — this function is not a second
    producer of that count (D-1/CLAUDE.md: one producer, or the two
    silently drift); ``.tool_result_bytes`` measures
    ``.reyn/memory/history-content/`` despite its own legacy field name
    (see ``storage_stats``'s own docstring — tool-result writes moved
    there under #5364).

    D-3: ``max_bytes=None`` (the field's own documented "off" state,
    never a second boolean) prints "unconfigured" — never a fabricated
    number, and never silently omits the line the way a bare "cap: none"
    might read as "0 bytes allowed" instead of "no cap at all".

    #5653 (the eviction pre-check running only from ``save_tool_result``,
    never ``save_media``) was fixed by #5667 — ``save_media`` now calls
    :meth:`~reyn.data.workspace.media_store.MediaStore.
    _evict_cross_session_over_cap` too (verified directly,
    ``media_store.py``'s own ``save_media``). This function's own former
    "known gap" disclosure named a gap that no longer exists; removed
    here rather than left to mislead an operator about a mechanism that
    was already fixed (#5682's own BLOCKING finding — landed the same
    night #5658 disclosed it and #5667 closed it, with nobody sweeping
    the disclosure)."""
    from reyn.config.infra import StorageConfig

    storage_cfg = getattr(config, "storage", None) or StorageConfig()
    used_bytes = media_stats.media_bytes + media_stats.tool_result_bytes

    if storage_cfg.max_bytes is None:
        print(
            f"  storage.max_bytes: unconfigured (no project-wide cap — "
            f"{used_bytes:,} bytes currently used, unbounded)",
        )
    else:
        over = used_bytes > storage_cfg.max_bytes
        mark = "⚠" if over else "✓"
        suffix = " — OVER CAP" if over else ""
        print(
            f"  {mark} storage.max_bytes={storage_cfg.max_bytes:,}: "
            f"{used_bytes:,} bytes currently used{suffix}",
        )

    pin_desc = ", ".join(storage_cfg.pin) if storage_cfg.pin else "none"
    print(f"  storage.pin: {pin_desc}")


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

    # ── C-7: media/ / tool-results/ / history.jsonl — no PER-RESOURCE
    # declared retention policy for any of these (#4480: "no one owns this
    # resource" made visible, not asserted). #4478 landed a project-wide
    # CAP covering media/+tool-results/ jointly (storage.max_bytes/pin) —
    # a separate, coarser knob than a per-resource policy; see the
    # "Project-wide storage cap" block below this section for it. history.jsonl
    # has no policy of any kind yet.
    print()
    print("Disk usage — no declared retention policy (visibility only):")
    # #5364: read-only here (storage_stats never writes) — session_id is
    # a required kwarg (no default: a forgotten value must never silently
    # resolve to a real session's directory, #5369) but this store never
    # calls save_tool_result, so the value itself is inert.
    store = MediaStore(
        MediaStoreConfig(), project_root=resolved_root, session_id="<read-only>",
    )
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

    # ── C-7 addendum: project-wide storage cap (part of #4364, 2026-09-02)──
    print()
    print("Project-wide storage cap (storage.max_bytes/pin, #5366/#4478 —")
    print("bounds media/ + tool-results/ TOGETHER, one operator number):")
    _print_storage_cap_status(config, media_stats)

    # ── C-1: hook argv[0] launch probe (#4364 PR-2, architect ruling) ──────
    print()
    print("Hook launch probe (argv[0] only, no configured args — a launch")
    print("probe, not a run; D-2: doctor never executes a hook for real):")
    _print_hook_probe_results(config, resolved_root)

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

    # ── #4364 (this slice, 2026-09-02): bounding/preference declared vs.
    # composed — the general "declared ↔ effective" form (C-5/C-6) applied
    # to #4206's ②/③ agent-layer override axes.
    print()
    print("Agent-layer overrides — declared (project) vs. COMPOSED (after")
    print("each agent's own bounding:/preferences:), mismatches only:")
    _print_bounding_preference_composition(config, resolved_root)

    # ── #4364 (this slice, 2026-09-03): external_transports — configured
    # entries are inert unless reached via the web/AGUI server runner ──────
    print()
    print("external_transports: — configured entries vs. the 2 real")
    print("consumers (both web/AGUI-server-only; inert under `reyn chat`):")
    _print_external_transports_status(config)

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

    # ── #5226: reyn process registry — how many, and whose ─────────────────
    print()
    print("Reyn process registry (~/.reyn/processes/) — every reyn CLI")
    print("process currently alive on this machine, across every workspace:")
    _print_process_registry()

    # ── #5428: hook env — what REYN_* an exec/exec_capture hook would see ──
    print()
    print("Hook env (REYN_* an exec/exec_capture child would receive right")
    print("now, per configured agent — D-1: reads the SAME agent-profile")
    print("override resolve_base_dir_candidate uses, not a restated declaration):")
    _print_hook_env_snapshot(resolved_root)

    # ── #5620: proxy-side litellm patch status (a SEPARATE process/venv) ───
    print()
    print("litellm proxy patch (#5620) — status file written by the owner's")
    print("own proxy process (scripts/litellm_proxy_patch/), a separate")
    print("venv/version this process never imports:")
    _print_litellm_proxy_patch_status()


def _print_litellm_proxy_patch_status() -> None:
    """#5620: reads ``~/.reyn/litellm-proxy-patch-status.json`` — written
    by a COMPLETELY SEPARATE process (the owner's own `junk/litellm`
    proxy, its own venv, its own litellm version) that this reyn process
    never imports and has no live connection to. Unlike the lib-side
    `_print_litellm_patch_status` above (which triggers a real import in
    THIS process), there is nothing to trigger here — the file either
    exists (the proxy has started at least once with the patch
    installed) or it doesn't, and doctor only reads.

    Path/schema come from `reyn.llm.litellm_proxy_patch_status` — the
    ONE reyn-side place that constant lives (see that module's own
    docstring for why the standalone patch file cannot share it via
    import, and how a Tier 2 gate test keeps the two copies in sync)."""
    import json

    from reyn.llm.litellm_proxy_patch_status import litellm_proxy_patch_status_path

    path = litellm_proxy_patch_status_path()
    if not path.is_file():
        print(f"  ? not installed or not started — no status file at {path}")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a malformed status file is reported, not fatal
        print(f"  ✗ status file present but unreadable ({type(exc).__name__}: {exc}): {path}")
        return

    version = data.get("litellm_version", "?")
    at = data.get("at", "?")
    print(f"  litellm {version}, last written {at}, pid {data.get('pid', '?')}")
    patched = data.get("patched") or {}
    reached = data.get("reached") or {}
    for name in sorted(patched):
        label = "✓ applied" if patched[name] else "✗ NOT applied"
        print(f"  {label}: {name} (reached {reached.get(name, '?')} time(s) this process)")
    if data.get("legacy_present"):
        print(
            "  ⚠ the pre-#5620 legacy patch (litellm_patch.py / "
            "zz_litellm_patch.pth) is still active in this venv — this "
            "patch refused to double-wrap the same method; uninstall "
            "the legacy install (see scripts/litellm_proxy_patch/README.md)"
        )


def _print_hook_env_snapshot(resolved_root: Path) -> None:
    """#5428: the operator-facing consumer this issue required — ``reyn
    doctor`` is the declared receiving surface (module docstring: "reach
    into sandbox / MCP / hook internals"). #5447: ``Session`` no longer
    has a matching public "hook env" read method to consume — a public
    method with only a test consumer is the #4866 shape #5442 already
    spent a PR closing, and this function has no live ``Session`` to call
    such a method on anyway (see below), so it was removed rather than
    wired in.

    No live ``Session`` is constructed (doctor constructs none anywhere —
    #5428's own investigation confirmed this: ``Session(`` has zero call
    sites in this module), and no ``Session`` method is called either
    (#5447, architect finding: an earlier revision of this function
    resolved the same 4 values through its OWN literal ``print(f"...")``
    lines instead of going through
    :meth:`~reyn.hooks.shell_runner.HookProcessContext.as_env`, silently
    duplicating that method's own docstring-declared single source of
    the 4 ``REYN_*`` names). This function instead builds the SAME
    :class:`~reyn.hooks.shell_runner.HookProcessContext`
    :meth:`~reyn.runtime.session.Session._build_hook_process_context`
    builds for a live session, via the SAME shared primitive
    (:func:`~reyn.runtime.workspace_paths.resolve_base_dir_candidate`) —
    with no session-layer override (doctor has no session), only the
    agent-profile layer, mirroring what a session with no per-session
    ``config.yaml`` override would resolve to anyway — then prints
    ``context.as_env()`` verbatim, so a 5th key added to that method
    shows up here with zero further changes to this file. Reads
    ``.reyn/agents/<name>/profile.yaml`` directly for each configured
    agent — the SAME enumeration ``_merged_hook_registry`` above already
    uses for its own per-agent hook layer."""
    from reyn.hooks.shell_runner import HookProcessContext
    from reyn.runtime.services.recovery import default_snapshot_path
    from reyn.runtime.workspace_paths import resolve_base_dir_candidate

    agents_dir = resolved_root / ".reyn" / "agents"
    if not agents_dir.is_dir():
        print("  no agents configured yet (.reyn/agents/ does not exist)")
        return
    found_any = False
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        found_any = True
        agent_name = agent_dir.name
        profile_path = agent_dir / "profile.yaml"
        base_dir_raw: "str | None" = None
        if profile_path.is_file():
            import yaml
            try:
                raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — hand/LLM-written yaml, surface not crash
                raw = None
            if isinstance(raw, dict):
                value = raw.get("base_dir")
                base_dir_raw = str(value) if value else None
        agent_base_dir = resolve_base_dir_candidate(
            base_dir_raw, workspace_root=resolved_root,
        )
        if agent_base_dir is None:
            # No valid agent-profile override -> the Agent object's own
            # default, which for an unnarrowed agent is "no restriction"
            # -> the project root itself (the SAME fallback
            # Session._build_hook_process_context's own
            # `self._workspace_base_dir or self._reyn_state_root.parent`
            # resolves to when neither layer has a value).
            agent_base_dir = resolved_root
        agent_state_dir = default_snapshot_path(
            agent_name, root=resolved_root / ".reyn",
        ).parent
        context = HookProcessContext(
            project_dir=resolved_root,
            agent_base_dir=agent_base_dir,
            agent_name=agent_name,
            agent_state_dir=agent_state_dir,
        )
        print(f"  {agent_name}:")
        for key, value in context.as_env().items():
            print(f"    {key}={value}")
    if not found_any:
        print("  no agents configured yet (.reyn/agents/ is empty)")


def _configured_exec_hooks(config: object, project_root: Path) -> "list[HookDef]":
    """Every configured ``exec``/``exec_capture`` ``HookDef``, across ALL
    layers (startup/runtime/trusted-per-agent (#5505)/per-agent — #5351
    (B-2), was startup-only before: this used to build its own
    single-layer ``load_hooks(config.hooks)`` registry, silently never
    probing a runtime or per-agent hook's argv at all) — the caller reads
    ``.name``/``.on``/``.exec``/
    ``.exec_capture``/``.subprocess``/``.network``/``.write_paths``/
    ``.origin`` directly off it, so the per-hook sandbox knobs AND the
    layer that declared it travel with the argv rather than being
    stripped here."""
    from reyn.hooks.schema import ALLOWED_HOOK_POINTS

    registry = _merged_hook_registry(config, project_root)
    all_defs = [
        hook_def
        for point in ALLOWED_HOOK_POINTS
        for hook_def in registry.hooks_for(point)  # type: ignore[attr-defined]
    ]
    return [
        hook_def for hook_def in all_defs
        if hook_def.exec is not None or hook_def.exec_capture is not None
    ]


def _print_hook_probe_results(config: Any, project_root: Path) -> None:
    import asyncio

    from reyn.security.sandbox import SandboxPolicy as _SandboxPolicy
    from reyn.security.sandbox.launcher import resolve_backend
    from reyn.security.sandbox.probe_argv import probe_argv

    hooks = _configured_exec_hooks(config, project_root)
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
            # #5351 (B-2): name the layer alongside the hook — the (B-2)
            # item from the #5351 issue thread. hook_def.origin comes
            # from _merged_hook_registry's own load_hooks(..., origin=)
            # call (#5213's provenance field), so every layer this
            # doctor process can read (startup/runtime/per-agent) is
            # named, not just "which hook" — a per-agent hook whose
            # write_paths silently never took effect at THIS layer
            # (#5244③/#5356) is now visible as "(per-agent)" right next
            # to its probe result, not indistinguishable from a startup
            # one.
            label = f"{hook_def.name or hook_def.on} ({hook_def.origin})"
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
    """The 4-layer ADDITIVE combine (#4555's own registry read as the
    reference — ``Session._build_hook_registry``'s SAME shape, minus the
    5th, session-scoped per-session layer doctor's standalone-process
    read has no way to reach): reyn.yaml's top-level ``hooks:`` (startup,
    trusted — must load, fail loud) -> ``.reyn/config/hooks.yaml`` (runtime
    IN-set) -> every ``.reyn/config/agents/<name>/hooks.yaml`` (#5505:
    trusted per-agent) -> every ``.reyn/agents/<name>/hooks.yaml``
    (per-agent).

    #5505 deliberate divergence from the live Session: a live Session
    treats a malformed trusted-per-agent file as FAIL-LOUD (refuses to
    boot — that layer is permission-bearing). This standalone doctor
    process never crashes on ANY finding (D-2: report-only, never mutate
    — "Exits 0 regardless of findings", this module's own standing rule)
    — so here it is try-added like every other untrusted layer below,
    same as the trusted STARTUP layer already isn't (see that one's own
    unguarded ``load_hooks`` call, unchanged). A bad trusted-per-agent
    file surfaces here as a dropped layer (same visible shape as any
    other malformed layer), not as a doctor crash — the caller reading
    ``hooks_layer_rejected``-shaped output (or a future doctor line, not
    added by this PR) is who would need to know a LIVE boot with this
    same file would actually refuse to start; that stronger contract is
    Session's own, not reproduced here.

    Each untrusted layer is try-added independently — a malformed one is
    dropped, its siblings kept (same per-layer resilience as the real
    Session combine), so one bad file cannot hide a real zero-responder
    gap in the layers that DID load.

    #5351 (B-2): each layer is now parsed by its OWN ``load_hooks`` call
    with ``origin=<label>`` and the resulting ``HookDef`` LISTS are
    merged — never concatenated as raw dicts first, the shape this
    function used before (``load_hooks(combined + list(layer))``,
    re-parsing every earlier layer's entries on every iteration with no
    ``origin=`` at all, so every ``HookDef.origin`` here silently read
    the "unknown" default regardless of which file actually declared the
    hook). This was the SAME pre-#5213 defect that field's own docstring
    describes for ``Session._build_hook_registry``'s history — it had
    already been fixed there but not here, a second copy of the same
    fact that drifted (CLAUDE.md). Doctor's own per-agent granularity
    (a doctor process reads EVERY agent's hooks.yaml at once, unlike a
    live Session which only ever reads its own) is preserved separately
    in *layers*' agent-name labels — ``HookDef.origin`` itself stays the
    canonical #5213 vocabulary (``"per-agent"``, not ``"per-agent:
    <name>"``), so a future comparison against
    :func:`~reyn.hooks.schema.hook_origin_is_at_least_as_specific_as`
    (which only recognises the 4 canonical strings) keeps working.

    The startup layer is read off *config* (``config.hooks`` — the SAME
    already-``project_root``-resolved ``load_config`` result ``run()``
    builds every other C-2/C-7 reading from), not a second, separately-
    invoked ``build_policy_tier_config()`` call — #4555's own review
    caught exactly this shape of bug (a second root-resolution call that
    silently reads the WRONG project when the process cwd differs from
    *project_root*, e.g. ``reyn doctor --project-root <other-dir>`` or
    any test driving ``run()`` directly with a ``tmp_path``)."""
    from reyn.config.loader import (
        load_hot_reload_config,
        load_per_agent_hooks,
        load_trusted_per_agent_hooks,
    )
    from reyn.hooks.loader import HookConfigError, load_hooks
    from reyn.hooks.registry import HookRegistry

    policy_hooks = getattr(config, "hooks", None) or []
    combined = list(policy_hooks) if isinstance(policy_hooks, list) else []
    # trusted startup layer — fail loud (unchanged from pre-#5351 behavior).
    defs = load_hooks(combined, origin="startup").all_defs()

    in_set_merged = load_hot_reload_config(project_root)
    in_set_hooks = in_set_merged.get("hooks") or []
    layers = [("runtime", in_set_hooks if isinstance(in_set_hooks, list) else [])]

    # #5505: trusted per-agent layer — see this function's own docstring
    # for why it is try-added HERE (unlike the live Session's fail-loud
    # contract for the same file).
    trusted_agents_dir = project_root / ".reyn" / "config" / "agents"
    if trusted_agents_dir.is_dir():
        for agent_dir in sorted(trusted_agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            trusted_hooks = load_trusted_per_agent_hooks(project_root, agent_dir.name)
            if trusted_hooks:
                layers.append(("trusted-per-agent", trusted_hooks))

    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent_hooks = load_per_agent_hooks(project_root, agent_dir.name)
            if agent_hooks:
                layers.append(("per-agent", agent_hooks))

    for origin, layer in layers:
        if not layer:
            continue
        try:
            defs = defs + load_hooks(layer, origin=origin).all_defs()
        except HookConfigError:
            # Untrusted layer, malformed — dropped, siblings kept (same
            # resilience as Session._build_hook_registry's real combine).
            continue
    return HookRegistry(defs)


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

    # #4935: capability disclosure — no new mechanism, this section (C-5)
    # already exists (#4364); it just gains a column. Declaration only
    # (`resolved.supported_capabilities`, never probed), same D-1 rule
    # every other doctor line follows. See `reyn.security.sandbox.
    # capability`'s own module docstring for the CI-witness gap this line
    # carries for Seatbelt specifically (no macOS CI runner can re-verify
    # this claim — a human on a real Mac is the only witness).
    declared_capabilities = list(getattr(sandbox_config, "require_capabilities", []) or [])
    supported = resolved.supported_capabilities.as_dict()
    if declared_capabilities:
        for name in declared_capabilities:
            support = supported.get(name)
            mark = "✓" if support is not None and support.value == "supported" else "✗"
            print(f"  required capability: {name!r} — {mark} {support}")
    else:
        print(
            "  required capabilities: none declared (sandbox.require_capabilities "
            "is empty) — this backend's own support: "
            + ", ".join(f"{k}={v.value}" for k, v in supported.items()),
        )


def _print_bounding_preference_composition(config: object, project_root: Path) -> None:
    """#4364 (this slice, 2026-09-02, lead-coder assignment): declared
    (project-level schema default) vs. COMPOSED (after applying each
    agent's own ``bounding:``/``preferences:`` override) for every
    #4206 leaf with a live override receptacle — the general form of
    C-5/C-6's "declared ↔ effective" pattern, applied to axes ②/③
    instead of sandbox posture. Owner's own motivating incident (issue
    body, verbatim): "会社で llm.model の設定効果なさそうな挙動を見た"
    — ``llm.model`` is exactly axis ②'s one member today.

    No new machinery: reads ``config_axis.py``'s existing
    ``walk_config_schema()`` axis/``override_enabled`` metadata (#5673),
    composes via the SAME ``compose_model_ceiling``/``resolve_preference``
    every real agent spawn already calls (``runtime/bounding.py``/
    ``runtime/preferences.py``), and loads each agent's profile the SAME
    way a real session does (``AgentProfile.load`` — the same validated
    loader, not a second hand-parse of the YAML, D-1: measure the actual
    loaded effect).

    D-2: reads only ``.reyn/agents/<name>/profile.yaml`` — a STATIC,
    on-disk declaration. The SESSION layer (``resolve_preference``'s own
    ``session_preferences`` parameter) is a live, in-process value no
    separate one-shot ``doctor`` process can see — same limitation
    C-2/C-3(b) already disclose for other session-scoped state; this
    function's own printed line says so (D-3) rather than silently
    only covering 2 of the 3 real layers.

    Reports ONLY a mismatch (declared != composed) — an agent that sets
    no override, or narrows to the SAME value the project already has,
    produces no line (lead-coder's own scope: "食い違いを報告する").
    An agent whose profile.yaml fails to load (a bounding/preferences
    key #4206 doesn't recognize, or an out-of-range ``bounding.model``
    value — the same validation a real session-load already enforces,
    ``AgentProfile.load``) is reported by name, not silently skipped —
    the SAME defect class ``AgentProfile.load`` failing at real
    session-start would abort with, made visible ahead of time instead
    of at the next agent boot."""
    from reyn.config.config_schema import resolve_config_value, walk_config_schema
    from reyn.config_axis import Axis
    from reyn.runtime.bounding import compose_model_ceiling
    from reyn.runtime.preferences import resolve_preference
    from reyn.runtime.profile import AgentProfile

    bounding_nodes = [
        n for n in walk_config_schema()
        if n.axis == Axis.BOUNDING and n.override_enabled
    ]
    preference_nodes = [
        n for n in walk_config_schema()
        if n.axis == Axis.PREFERENCE and n.override_enabled
    ]

    agents_dir = project_root / ".reyn" / "agents"
    agent_names = (
        sorted(p.name for p in agents_dir.iterdir() if p.is_dir())
        if agents_dir.is_dir() else []
    )
    if not agent_names:
        print("  no .reyn/agents/<name>/ found — nothing to compose against")
        print(
            "  (session-layer overrides are never visible to doctor — a "
            "separate, one-shot process with no live session, D-2)",
        )
        return

    mismatches = 0
    load_errors: list[str] = []
    for agent_name in agent_names:
        try:
            profile = AgentProfile.load(agents_dir / agent_name)
        except FileNotFoundError:
            continue  # a directory with no profile.yaml — not a real agent
        except Exception as exc:  # noqa: BLE001 — D-1: report the real failure, never swallow it silently
            load_errors.append(f"{agent_name}: {exc}")
            continue

        for node in bounding_nodes:
            override_key = node.override_key or node.key
            if override_key not in profile.bounding:
                continue
            _found, declared_bounding = resolve_config_value(config, node.key)
            raw_agent_bound = profile.bounding[override_key]
            composed_bounding: "str | None" = compose_model_ceiling(
                declared_bounding,
                str(raw_agent_bound) if raw_agent_bound is not None else None,
            )
            if composed_bounding != declared_bounding:
                mismatches += 1
                print(
                    f"  ⚠ [{agent_name}] {node.key} (bounding.{override_key}): "
                    f"declared={declared_bounding!r}, composed={composed_bounding!r}",
                )

        for node in preference_nodes:
            if node.key not in profile.preferences:
                continue
            _found, declared_pref = resolve_config_value(config, node.key)
            composed_pref: object = resolve_preference(
                node.key, declared_pref, agent_preferences=profile.preferences,
            )
            if composed_pref != declared_pref:
                mismatches += 1
                print(
                    f"  ⚠ [{agent_name}] {node.key} (preferences.{node.key}): "
                    f"declared={declared_pref!r}, composed={composed_pref!r}",
                )

    if load_errors:
        print(f"  ✗ {len(load_errors)} agent profile(s) failed to load (not compared):")
        for line in load_errors:
            print(f"    {line}")
    if mismatches == 0:
        qualifier = (
            f" (among the {len(agent_names) - len(load_errors)} that loaded)"
            if load_errors else ""
        )
        print(
            f"  ✓ no agent narrows/overrides these "
            f"{len(bounding_nodes) + len(preference_nodes)} key(s) away from "
            f"the project default{qualifier}",
        )
    print(
        "  (session-layer overrides are never visible to doctor — a "
        "separate, one-shot process with no live session, D-2)",
    )


def _print_external_transports_status(config: object) -> None:
    """#4364 (this slice, 2026-09-03, lead-coder assignment — @tui-coder's
    candidate ①): report ``external_transports:`` entries as INERT for
    the surface this ``doctor`` process is most likely run from —
    ``reyn chat`` (the plain CLI).

    Same third-state shape ``config.py``'s AgentProfile-unknown-key
    report already established (verbatim: "it is read, kept in no
    in-memory state, and does nothing") — applied here to a section
    that IS a recognized key (unlike that report's case), but whose
    only 2 real consumers are both reachable exclusively through the
    web/AGUI server runner, never through ``reyn chat``'s own
    run-loop. No new machinery: this reads the SAME
    ``config.external_transports.transports`` dict the loader already
    builds (D-1 — the live parsed value, not a re-parse of the raw
    YAML), and names the 2 real consumer call sites by file:line
    (grep-confirmed, #4364 investigation) rather than asserting the
    inertness in the abstract:

      - ``interfaces/web/deps.py:411-412`` — the outbox interceptor
        wiring, gated on ``config.external_transports.transports``
        being non-empty, itself only reached from the web app's own
        session-construction path.
      - ``interfaces/web/server.py:123`` — the cron-job-failure
        notifier (``_failure_notifier``), part of the web server's own
        background runner.

    D-2: this is a static declaration check — doctor has no way to
    tell whether the operator actually runs via the web/AGUI server
    (where these entries DO apply) or plain ``reyn chat`` (where they
    do not); the printed line discloses that limit explicitly rather
    than asserting one mode or the other. D-3: an UNCONFIGURED section
    prints "unconfigured", never a fabricated "0 configured" framed as
    a finding — matching #5658/#5679's own "declare no data, don't
    invent a zero" posture for an absent value."""
    external_transports = getattr(config, "external_transports", None)
    transports = getattr(external_transports, "transports", None) or {}
    if not transports:
        print("  unconfigured — no external_transports: entries in reyn.yaml")
        return
    names = sorted(transports)
    print(
        f"  ⚠ {len(names)} configured ({', '.join(names)}) — this section "
        f"is wired ONLY by interfaces/web/deps.py:411-412 (outbox "
        f"interceptor) and interfaces/web/server.py:123 (cron-failure "
        f"notifier), both reachable only through the web/AGUI server "
        f"runner. `reyn chat` (the plain CLI) never reaches either — "
        f"these entries have no effect there.",
    )
    print(
        "  (doctor cannot tell whether you actually run via the web/AGUI "
        "server — if you do, these entries DO apply there; this is a "
        "static declaration check, D-2)",
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


def _print_process_registry() -> None:
    """#5226: owner's own observation ("I only launched one reyn session,
    so the rest are your own cleanup misses") plus lead-coder's real-
    machine trace (12 reyn/reyn:chat processes, 11 abandoned, oldest 11
    days) found there was no way for reyn ITSELF to answer "how many of
    me are alive, and whose" — only a manual ``ps``+``lsof -d cwd``
    reconstruction. This section is that read seam, D-1/D-2 unchanged
    (a live read of :func:`~reyn.runtime.process_registry.live_processes`,
    never a restatement of config; report-only, no kill/TTL — that
    judgment is explicitly out of THIS issue's scope, an owner-level call
    once the count is visible at all).

    Deliberately prints only the fields the marker itself carries — pid/
    ppid/cwd/subcommand/age — never full argv or any path beyond cwd
    (see ``process_registry.py``'s own module docstring for why: it
    mirrors ``reyn.runtime.proctitle``'s explicit stance against leaking
    more than the minimum into anything read back after the fact)."""
    import time

    from reyn.data.index.build_lock import pid_alive
    from reyn.runtime.process_registry import read_process_markers

    # #5709 R3: read_process_markers() (non-destructive) instead of
    # live_processes() — this call must never reap a dead-PID marker as
    # a side effect. reyn doctor is not the only reader (a broker health
    # poll is the other); whichever reads first must not destroy the
    # evidence the SECOND reader came to see. The "currently alive"
    # framing below is unchanged — filtered locally via pid_alive(),
    # same population as before, just without the destructive read.
    all_markers = read_process_markers()
    processes = [
        entry for entry in all_markers
        if isinstance(entry.get("pid"), int) and pid_alive(entry["pid"])
    ]
    if not processes:
        print("  no reyn process markers found (none registered, or none still alive)")
        return

    def _age_desc(epoch: "float | None", now: float) -> str:
        if not isinstance(epoch, (int, float)):
            return "never"
        age_seconds = max(0, int(now - epoch))
        days, rem = divmod(age_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h ago"
        if hours:
            return f"{hours}h {minutes}m ago"
        return f"{minutes}m ago"

    now = time.time()
    print(f"  {len(processes)} process(es) currently alive:")
    for entry in processes:
        pid = entry.get("pid")
        ppid = entry.get("ppid")
        cwd = entry.get("cwd", "?")
        subcommand = entry.get("subcommand") or "(no subcommand)"
        started_desc = _age_desc(entry.get("started_at"), now)
        # #5709 R8: numbers only, no judgement word ("stale"/"alive"/
        # "dead") — that would be a threshold, and this issue explicitly
        # defers threshold design to whenever an operator first says a
        # raw age isn't enough (a real re-open trigger, not a guess made
        # here).
        beat_desc = _age_desc(entry.get("last_loop_beat_at"), now)
        # #5709 R9: a process's own RECORDED identity (record_process_
        # identity, #5350) — never derived from cwd (the #5350-named
        # incident: an unrelated process sharing a directory is not the
        # same identity). Absent until Session construction resolves it
        # (register_process runs at CLI startup, before that), so a
        # process that died before then genuinely has no identity to
        # show — printed blank, not guessed.
        agent_name = entry.get("agent_name") or ""
        broker_session_id = entry.get("broker_session_id") or ""
        print(f"    pid={pid} ppid={ppid} started {started_desc}")
        print(f"      cwd: {cwd}")
        print(f"      subcommand: {subcommand}")
        print(f"      loop beat: {beat_desc}")
        print(f"      agent_name: {agent_name}")
        print(f"      broker_session_id: {broker_session_id}")
