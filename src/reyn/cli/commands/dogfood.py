"""`reyn dogfood` — scenario-based regression testing framework (FP-0036).

Subcommands:
  run        Run a scenario set YAML file; record results under .reyn/dogfood/runs/
  coverage   Show feature-map coverage across one or more scenario set YAML files
  report     Print 4-band breakdown + Brier score from a stored run
  compare    Regression diff between a baseline run and a candidate run
  baseline   Symlink a run as a named baseline under .reyn/dogfood/baselines/
  publish    Create a GitHub Discussion thread from a stored run's summary.json

The CLI delegates to:
  load_scenario_set  — F1 (reyn.dogfood.scenarios)
  run_scenario_set   — F2 (reyn.dogfood.runner, this slice)
  compute_coverage   — F4 (reyn.dogfood.coverage)
  compare_runs       — F2 (reyn.dogfood.compare, this slice)
  publish_run        — FP-0036 (reyn.dogfood.publish)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from reyn.cli.env_backend import (
    build_environment_backend,
    register_env_backend_args,
)


def register(sub) -> None:
    p = sub.add_parser(
        "dogfood",
        help="Dogfood scenario regression testing (FP-0036)",
        description=(
            "Run scenario sets against the chat router, measure 4-band outcomes "
            "(verified / inconclusive / refuted / blocked), compare against baselines "
            "and surface regressions across releases."
        ),
    )
    dsub = p.add_subparsers(dest="dogfood_cmd", metavar="<subcommand>")
    dsub.required = True
    p.set_defaults(func=_no_subcommand)

    # --- run ---
    run_p = dsub.add_parser(
        "run",
        help="Run a scenario set and record results",
        description=(
            "Execute every scenario in <SET_YAML> through the chat router and "
            "record per-scenario outcomes under .reyn/dogfood/runs/<run_id>/."
        ),
    )
    run_p.add_argument("set_yaml", metavar="SET_YAML",
                       help="Path to the scenario set YAML file.")
    run_p.add_argument("--n", type=int, default=1, metavar="N",
                       help="Number of repetitions for stability bands (default: 1).")
    run_p.add_argument("--replay", metavar="FIXTURE_DIR",
                       help=(
                           "Run in replay mode using recorded LLM fixtures instead of "
                           "live LLM calls.  Pass the fixture directory recorded by "
                           "'reyn dogfood run' (F5 integration)."
                       ))
    run_p.add_argument("--agent", default="default", metavar="AGENT",
                       help="Chat-router agent name (default: 'default').")
    run_p.add_argument("--storage", metavar="DIR",
                       help=(
                           "Root directory for run output. "
                           "Default: .reyn/dogfood/runs/<run_id>."
                       ))
    run_p.add_argument("--run-id", metavar="RUN_ID",
                       help="Explicit run ID (UUID generated if omitted).")
    run_p.add_argument("--with-interpretation", action="store_true",
                       help=(
                           "After verifier scoring, generate a 3-line LLM "
                           "interpretation per scenario summarising whether "
                           "the run matched expectations. Adds ~$0.0005 / "
                           "scenario at flash-lite tier."
                       ))
    run_p.add_argument("--interpretation-model", metavar="MODEL", default=None,
                       help=(
                           "Override the LiteLLM model id used for "
                           "interpretation (default: openai/gemini-2.5-flash-lite)."
                       ))
    # #1289: per-frontend container-chat — same --env-backend surface as `reyn run`.
    register_env_backend_args(run_p)
    run_p.set_defaults(func=run_run)

    # --- coverage ---
    cov_p = dsub.add_parser(
        "coverage",
        help="Show feature-map coverage across scenario sets",
        description=(
            "Parse the feature map and walk one or more scenario set YAML files "
            "to produce a coverage matrix — covered feature count, uncovered list."
        ),
    )
    cov_p.add_argument("set_yamls", nargs="*", metavar="SET_YAML",
                        help=(
                            "One or more scenario set YAML files.  "
                            "Defaults to dogfood/scenarios/*.yaml if omitted."
                        ))
    cov_p.add_argument("--feature-map", default="docs/feature-map.md",
                        metavar="FILE",
                        help="Path to the feature map Markdown file (default: docs/feature-map.md).")
    cov_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit coverage as JSON instead of the default table.")
    cov_p.set_defaults(func=run_coverage)

    # --- report ---
    rep_p = dsub.add_parser(
        "report",
        help="Print 4-band breakdown + Brier from a stored run",
        description=(
            "Read the summary.json from a previous 'reyn dogfood run' and print "
            "the 4-band outcome breakdown (verified / inconclusive / refuted / blocked) "
            "plus Brier score if outcome predictions were present in the scenarios."
        ),
    )
    rep_p.add_argument("run_id", metavar="RUN_ID",
                        help=(
                            "Run ID or path to the run directory under "
                            ".reyn/dogfood/runs/."
                        ))
    rep_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit report as JSON instead of the default table.")
    rep_p.set_defaults(func=run_report)

    # --- compare ---
    cmp_p = dsub.add_parser(
        "compare",
        help="Regression diff between a baseline and a candidate run",
        description=(
            "Compare two stored runs. Exits 1 if the verified-rate drop "
            "exceeds --threshold (default 5 percentage points). Exits 2 on errors."
        ),
    )
    cmp_p.add_argument("baseline_run_id", metavar="BASELINE",
                        help="Run ID (or path) for the baseline run.")
    cmp_p.add_argument("candidate_run_id", metavar="CANDIDATE",
                        help="Run ID (or path) for the candidate run.")
    cmp_p.add_argument("--threshold", type=float, default=0.05, metavar="FLOAT",
                        help=(
                            "Verified-rate drop (0.0–1.0) that triggers a regression "
                            "alert (exit code 1). Default: 0.05 (5 percentage points)."
                        ))
    cmp_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit comparison as JSON instead of the default table.")
    cmp_p.set_defaults(func=run_compare)

    # --- baseline ---
    bl_p = dsub.add_parser(
        "baseline",
        help="Symlink a run as a named baseline",
        description=(
            "Create a named baseline under .reyn/dogfood/baselines/<label>/ "
            "pointing at the given run. Use this label in 'reyn dogfood compare'."
        ),
    )
    bl_p.add_argument("run_id", metavar="RUN_ID",
                       help="Run ID (or path) to mark as a baseline.")
    bl_p.add_argument("--label", metavar="NAME",
                       help=(
                           "Baseline label. Defaults to the run_id if omitted. "
                           "Example: --label v1.2-stable"
                       ))
    bl_p.set_defaults(func=run_baseline)

    # --- publish ---
    from reyn.dogfood.publish import (  # noqa: E402
        _DEFAULT_TEMPLATE_PATH,
        DEFAULT_CATEGORY_SLUG,
        DEFAULT_REPO,
    )
    pub_p = dsub.add_parser(
        "publish",
        help="Create a GitHub Discussion thread from a stored run",
        description=(
            "Read the summary.json from a stored dogfood run, render a Discussion "
            "body from the Markdown template, and create a thread in the configured "
            "GitHub Discussions category. Authentication via GH_TOKEN or GITHUB_TOKEN "
            "env var (same convention as the gh CLI)."
        ),
    )
    pub_p.add_argument("run_id", metavar="RUN_ID",
                        help=(
                            "Run ID or path to the run directory under "
                            ".reyn/dogfood/runs/."
                        ))
    pub_p.add_argument("--repo", metavar="OWNER/REPO", default=None,
                        help=(
                            f"GitHub repository (default: '{DEFAULT_REPO}', "
                            "or detected from 'git remote get-url origin')."
                        ))
    pub_p.add_argument("--category", metavar="SLUG", default=DEFAULT_CATEGORY_SLUG,
                        help=(
                            f"Discussion category slug (default: '{DEFAULT_CATEGORY_SLUG}')."
                        ))
    pub_p.add_argument("--dry-run", action="store_true",
                        help=(
                            "Render the Discussion title and body to stdout without "
                            "posting to GitHub."
                        ))
    pub_p.add_argument("--template", metavar="PATH", default=None,
                        help=(
                            "Override the Discussion body template path "
                            f"(default: {_DEFAULT_TEMPLATE_PATH})."
                        ))
    pub_p.add_argument("--batch-id", metavar="N", default=None,
                        help=(
                            "Batch number (e.g. 27). Required if summary.json "
                            "does not carry a 'batch_id' field."
                        ))
    pub_p.add_argument("--topic", metavar="TOPIC", default=None,
                        help=(
                            "Short topic description. Required if summary.json "
                            "does not carry a 'topic' field."
                        ))
    pub_p.add_argument("--with-transcripts", action="store_true",
                        help=(
                            "Append a per-scenario folding markdown section "
                            "to the Discussion body (input + truncated reply "
                            "+ interpretation + verifier verdicts). "
                            "Reads scenarios/<id>/output.json from the run "
                            "directory."
                        ))
    pub_p.add_argument("--scenario-set", metavar="PATH", default=None,
                        help=(
                            "Path to the source scenario set YAML, used to "
                            "fill the per-scenario Input field when "
                            "--with-transcripts is set."
                        ))
    pub_p.set_defaults(func=run_publish)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print(
        "Usage: reyn dogfood <subcommand>  (run | coverage | report | compare | baseline)",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _dogfood_base_dir() -> Path:
    return Path.cwd() / ".reyn" / "dogfood"


def _runs_dir() -> Path:
    return _dogfood_base_dir() / "runs"


def _baselines_dir() -> Path:
    return _dogfood_base_dir() / "baselines"


def _resolve_run_dir(run_id_or_path: str) -> Path:
    """Resolve a run_id or path string to an absolute run directory."""
    candidate = Path(run_id_or_path)
    if candidate.exists() and candidate.is_dir():
        return candidate.resolve()
    # Treat as a run_id under the default runs directory
    run_dir = _runs_dir() / run_id_or_path
    if not run_dir.exists():
        print(
            f"Error: Run directory not found: {run_dir}\n"
            f"  Tried: {run_id_or_path} (as path) and {run_dir} (as run_id)",
            file=sys.stderr,
        )
        sys.exit(2)
    return run_dir


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------

def run_run(args: argparse.Namespace) -> None:
    """Execute a scenario set and record results."""
    set_yaml = Path(args.set_yaml)
    if not set_yaml.exists():
        print(f"Error: Scenario set not found: {set_yaml}", file=sys.stderr)
        sys.exit(2)

    replay_dir = Path(args.replay) if args.replay else None
    storage_dir = Path(args.storage) if args.storage else None

    try:
        from reyn.dogfood.scenarios import load_scenario_set  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: reyn.dogfood.scenarios is not available ({exc}).\n"
            "Ensure F1 (scenarios.py) is installed.",
            file=sys.stderr,
        )
        sys.exit(2)

    scenario_set = load_scenario_set(str(set_yaml))

    # Build the live-LLM runner_fn (injected seam for the real agent path)
    # #1289: build the agent-level EnvironmentBackend and pass the SAME instance
    # to both ChatSession seams (FS + exec) via _build_live_runner (single-shared
    # sandbox, #1200). A launched container is torn down at process exit.
    _env_backend, _wb, _ws, _env_cleanup = build_environment_backend(args)
    if _env_cleanup is not None:
        import atexit
        atexit.register(_env_cleanup)
    live_runner_fn = _build_live_runner(args.agent, env_backend=_env_backend)

    try:
        from reyn.dogfood.runner import run_scenario_set
    except ImportError as exc:
        print(f"Error loading runner: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"dogfood run: {scenario_set.name}  ({len(scenario_set.scenarios)} scenarios, n={args.n})")
    if replay_dir:
        print(f"  replay mode: {replay_dir}")

    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id=getattr(args, "run_id", None),
            storage_dir=storage_dir,
            agent_name=args.agent,
            n=args.n,
            replay_fixture_dir=replay_dir,
            runner_fn=live_runner_fn if not replay_dir else None,
            with_interpretation=getattr(args, "with_interpretation", False),
            interpretation_model=getattr(args, "interpretation_model", None),
        )
    )

    agg = result.aggregate()
    run_dir = storage_dir or (_runs_dir() / result.run_id)

    print()
    print(f"  run_id     : {result.run_id}")
    print(f"  verified   : {agg['verified']}")
    print(f"  inconclusive: {agg['inconclusive']}")
    print(f"  refuted    : {agg['refuted']}")
    print(f"  blocked    : {agg['blocked']}")
    print(f"  total      : {agg['total']}")
    print(f"  verified % : {agg['verified_rate'] * 100:.1f}%")
    if agg.get("brier_score") is not None:
        print(f"  Brier      : {agg['brier_score']:.4f}")
    print()
    print(f"  results → {run_dir / 'summary.json'}")


def _build_live_runner(agent_name: str, *, env_backend=None):
    """Return an async runner_fn that drives the chat router via send_to_agent_impl.

    Reuses the same path as MCP / web A2A: build a minimal AgentRegistry +
    session factory, then call send_to_agent_impl per turn.

    Per-scenario state isolation:
    - Wipes events/agents/<name>/chat/ before each scenario so captured
      events contain only the events from that scenario's turns.
    - Wipes state/action_usage.jsonl before each scenario so hot-list
      frequency counters don't bleed across scenarios.
    - Wipes agents/<name>/history.jsonl before each scenario so chat
      history from prior scenarios is not injected into the LLM's
      messages. Without this, ChatSession.load_history() (called by the
      session factory) loads the accumulated history-jsonl from disk,
      and scenario N sees scenario 1..N-1's user/assistant turns in its
      context — defeating the "fresh per scenario" guarantee. The
      dogfood_fresh_reset.sh script explicitly defers per-agent history
      wipe to callers (= the runner is the caller); this is that wipe.
    - Drops the cached ChatSession from the registry between scenarios so
      the session's in-memory EventLog starts empty each time.

    Artifact collection:
    - Snapshots .reyn/agents/<name>/artifacts/ after the run (if present).
      Artifact diffs (= new files only) are not computed here; the caller
      receives the full post-run snapshot.  Per-scenario artifact diffs
      would require a pre-run snapshot which adds latency; the verifier can
      compare across scenarios if needed.

    Permission injection:
    - PermissionResolver is constructed with interactive=False so the runner
      never blocks on stdin prompts.  This accepts the limitation that ops
      requiring interactive approval are blocked; this is the correct
      behaviour for headless dogfood dispatch (equivalent to running in CI).

    chain_id:
    - Allocated internally by send_to_agent_impl; not surfaced here because
      events are harvested by filesystem scan after the turn, not by
      chain-id filtering.
    """
    import asyncio
    import shutil
    from pathlib import Path

    from reyn.budget.budget import BudgetTracker
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession
    from reyn.config import _find_project_root, load_config, load_project_context
    from reyn.dogfood.runner import ScenarioRunResult
    from reyn.events.event_store import EventStore
    from reyn.llm.model_resolver import ModelResolver
    from reyn.mcp_server import send_to_agent_impl
    from reyn.permissions.permissions import PermissionResolver

    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    config = load_config()
    resolver = ModelResolver(config.models)
    model = config.model
    output_language = config.output_language
    safety = config.safety
    project_context = load_project_context(config, project_root)
    perm_config = getattr(config, "permissions", {}) or {}

    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=False,  # headless — never block on stdin
        unsafe_python_allowed=False,
    )
    budget_tracker = BudgetTracker(config.cost, safety=safety)
    budget_tracker.hydrate(project_root / ".reyn" / "state" / "budget_ledger.jsonl")

    # Per-call registry and session cache. Rebuilt each invocation of the
    # returned runner_fn so scenarios don't share session state.
    _registry_cache: dict = {}

    def _make_registry() -> AgentRegistry:
        """Build a fresh registry + session factory for one scenario run."""
        # registry is captured by the nested factory closure; we use a list
        # cell so the factory can reference it before assignment completes.
        _reg_cell: list = []

        def _session_factory(profile: AgentProfile) -> ChatSession:
            s = ChatSession(
                agent_name=profile.name,
                model=model,
                resolver=resolver,
                permission_resolver=perm_resolver,
                safety=safety,
                mcp_servers=config.mcp,
                output_language=output_language,
                prompt_cache_enabled=config.prompt_cache_enabled,
                project_context=project_context,
                agent_role=profile.role,
                compaction_config=config.chat.compaction,
                registry=_reg_cell[0] if _reg_cell else None,
                allowed_skills=profile.allowed_skills,
                allowed_mcp=profile.allowed_mcp,
                events_config=config.events,
                state_log=None,  # no WAL for dogfood dispatch
                budget_tracker=budget_tracker,
                sandbox_config=config.sandbox,
                multimodal_config=config.multimodal,
                tool_calls_op_loop_skills=config.tool_calls_op_loop_skills,
                action_retrieval_config=config.action_retrieval,
                # #1289: same backend instance to both seams (single-shared-sandbox).
                environment_backend=env_backend,
                sandbox_backend=env_backend,
            )
            s.load_history()
            return s

        reg = AgentRegistry(
            project_root=project_root,
            session_factory=_session_factory,
            state_log=None,
        )
        _reg_cell.append(reg)
        return reg

    def _wipe_scenario_state() -> None:
        """Delete per-scenario ephemeral state so each run starts clean."""
        # Wipe the agent's chat event files so EventStore.iter_all() only
        # returns events from this scenario's turns.
        events_chat_dir = project_root / ".reyn" / "events" / "agents" / agent_name / "chat"
        if events_chat_dir.is_dir():
            shutil.rmtree(events_chat_dir, ignore_errors=True)
        # Wipe action_usage ledger to prevent hot-list bleed across scenarios.
        action_usage_path = project_root / ".reyn" / "state" / "action_usage.jsonl"
        action_usage_path.unlink(missing_ok=True)
        # Wipe per-agent chat history so prior scenarios' user/assistant
        # turns are NOT injected into the LLM context for this scenario.
        # ChatSession.load_history() (called by the session factory) reads
        # this file unconditionally; without the wipe, scenario N sees
        # scenarios 1..N-1's messages. dogfood_fresh_reset.sh intentionally
        # defers this wipe to callers because it requires knowing the
        # agent name (which the script doesn't); the runner has it.
        history_path = project_root / ".reyn" / "agents" / agent_name / "history.jsonl"
        history_path.unlink(missing_ok=True)

    def _collect_events(registry: AgentRegistry) -> list[dict]:
        """Harvest events emitted during the scenario from the EventStore."""
        session = registry._agents.get(agent_name)
        if session is None:
            return []
        try:
            store: EventStore = session._event_store
            return [e.model_dump(mode="json") for e in store.iter_all()]
        except Exception:
            return []

    def _collect_artifacts() -> list[dict]:
        """Snapshot artifact files in .reyn/agents/<name>/artifacts/."""
        artifacts_dir = project_root / ".reyn" / "agents" / agent_name / "artifacts"
        if not artifacts_dir.is_dir():
            return []
        import json
        results: list[dict] = []
        for f in sorted(artifacts_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                raw = f.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {"raw": raw}
                results.append({"path": str(f.name), **data} if isinstance(data, dict) else {"path": str(f.name), "content": data})
            except Exception:
                continue
        return results

    async def runner_fn(scenario) -> ScenarioRunResult:
        """Async callable (Scenario) -> ScenarioRunResult.

        Per-scenario state isolation is applied before invoking the agent.
        Errors inside the run are caught and returned as a blocked outcome
        so a single crashing scenario does not abort the whole set.
        """
        _wipe_scenario_state()
        # Fresh registry per scenario — no session state bleed.
        registry = _make_registry()

        try:
            await registry.ensure_running(agent_name)
        except Exception as exc:  # noqa: BLE001 — agent not found etc.
            return ScenarioRunResult(
                scenario_id=scenario.id,
                reply_text="",
                events=[],
                artifacts=[],
                reply_outcome="blocked",
                events_outcome="blocked",
                artifacts_outcome="blocked",
                detail={"error": str(exc), "stage": "ensure_running"},
            )

        reply_parts: list[str] = []

        try:
            if scenario.is_multi_turn:
                for prompt in scenario.prompts:
                    result = await send_to_agent_impl(
                        registry,
                        agent_name=agent_name,
                        message=prompt,
                    )
                    if result.get("reply"):
                        reply_parts.append(result["reply"])
            else:
                message = scenario.input or ""
                result = await send_to_agent_impl(
                    registry,
                    agent_name=agent_name,
                    message=message,
                )
                if result.get("reply"):
                    reply_parts.append(result["reply"])
        except Exception as exc:  # noqa: BLE001
            events = _collect_events(registry)
            return ScenarioRunResult(
                scenario_id=scenario.id,
                reply_text="\n\n".join(reply_parts),
                events=events,
                artifacts=[],
                reply_outcome="blocked",
                events_outcome="blocked",
                artifacts_outcome="blocked",
                detail={"error": str(exc), "stage": "send_to_agent"},
            )
        finally:
            try:
                await registry.shutdown()
            except Exception:  # noqa: BLE001
                pass

        events = _collect_events(registry)
        artifacts = _collect_artifacts()
        reply_text = "\n\n".join(reply_parts).strip()

        return ScenarioRunResult(
            scenario_id=scenario.id,
            reply_text=reply_text,
            events=events,
            artifacts=artifacts,
            # Verifier outcomes are left at "inconclusive" (default).
            # The verifier triad (run_scenario_set's caller layer) fills
            # these in after this function returns.
        )

    return runner_fn


# ---------------------------------------------------------------------------
# Subcommand: coverage
# ---------------------------------------------------------------------------

def run_coverage(args: argparse.Namespace) -> None:
    """Show feature-map coverage across scenario sets."""
    try:
        from reyn.dogfood.coverage import compute_coverage  # type: ignore[import]
        from reyn.dogfood.scenarios import load_scenario_set  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: coverage module is not available ({exc}).\n"
            "Ensure F4 (coverage.py) is installed.",
            file=sys.stderr,
        )
        sys.exit(2)

    yaml_paths = [Path(p) for p in args.set_yamls]
    if not yaml_paths:
        # Default: all YAML files under dogfood/scenarios/
        default_dir = Path("dogfood") / "scenarios"
        if default_dir.exists():
            yaml_paths = sorted(default_dir.glob("*.yaml"))
        else:
            print("No scenario YAML files specified and dogfood/scenarios/ not found.",
                  file=sys.stderr)
            sys.exit(2)

    sets = [load_scenario_set(str(p)) for p in yaml_paths]
    feature_map_path = args.feature_map

    try:
        matrix = compute_coverage(sets, feature_map_path)
    except Exception as exc:
        print(f"Error computing coverage: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.output_json:
        print(json.dumps(matrix.__dict__, ensure_ascii=False, indent=2, default=str))
    else:
        _print_coverage(matrix)


def _print_coverage(matrix) -> None:  # pragma: no cover
    """Print coverage matrix to stdout (human-readable)."""
    print(f"Coverage: {matrix.covered_count}/{matrix.total_count} features covered")
    if matrix.uncovered:
        print("\nUncovered features:")
        for feat in matrix.uncovered:
            print(f"  - {feat}")
    else:
        print("All features covered!")


# ---------------------------------------------------------------------------
# Subcommand: report
# ---------------------------------------------------------------------------

def run_report(args: argparse.Namespace) -> None:
    """Print 4-band breakdown + Brier score from a stored run."""
    run_dir = _resolve_run_dir(args.run_id)

    try:
        from reyn.dogfood.runner import load_run_result_from_storage
    except ImportError as exc:
        print(f"Error loading runner: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        result = load_run_result_from_storage(run_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    agg = result.aggregate()

    if args.output_json:
        report_data = {
            "run_id": result.run_id,
            "set_name": result.set_name,
            "started_at": result.started_at.isoformat(),
            "completed_at": (
                result.completed_at.isoformat()
                if result.completed_at else None
            ),
            **agg,
        }
        print(json.dumps(report_data, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Run: {result.run_id}")
        print(f"Set: {result.set_name}")
        print(f"Started: {result.started_at.isoformat()}")
        if result.completed_at:
            print(f"Completed: {result.completed_at.isoformat()}")
        print()
        print(f"  verified    : {agg['verified']}")
        print(f"  inconclusive: {agg['inconclusive']}")
        print(f"  refuted     : {agg['refuted']}")
        print(f"  blocked     : {agg['blocked']}")
        print(f"  total       : {agg['total']}")
        print(f"  verified %  : {agg['verified_rate'] * 100:.1f}%")
        if agg.get("brier_score") is not None:
            print(f"  Brier       : {agg['brier_score']:.4f}")

        # Per-scenario breakdown
        print()
        print("Scenarios:")
        for sr in result.scenario_results:
            marker = {
                "verified": "✓",
                "inconclusive": "?",
                "refuted": "✗",
                "blocked": "!",
            }.get(sr.overall_outcome, "?")
            print(f"  {marker} {sr.scenario_id:<40}  {sr.overall_outcome}")


# ---------------------------------------------------------------------------
# Subcommand: compare
# ---------------------------------------------------------------------------

def run_compare(args: argparse.Namespace) -> None:
    """Regression diff between a baseline and a candidate run."""
    baseline_dir = _resolve_run_dir(args.baseline_run_id)
    candidate_dir = _resolve_run_dir(args.candidate_run_id)

    try:
        from reyn.dogfood.compare import compare_runs
        from reyn.dogfood.runner import load_run_result_from_storage
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        baseline_result = load_run_result_from_storage(baseline_dir)
        candidate_result = load_run_result_from_storage(candidate_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    report = compare_runs(baseline_result, candidate_result, threshold=args.threshold)

    if args.output_json:
        data = {
            "baseline_run_id": baseline_result.run_id,
            "candidate_run_id": candidate_result.run_id,
            "baseline_verified_rate": report.baseline_verified_rate,
            "candidate_verified_rate": report.candidate_verified_rate,
            "verified_rate_delta": report.verified_rate_delta,
            "threshold": args.threshold,
            "regression": report.exceeds_threshold(args.threshold),
            "regressed_scenarios": report.regressed_scenarios,
            "improved_scenarios": report.improved_scenarios,
            "deltas": [
                {
                    "scenario_id": d.scenario_id,
                    "baseline_outcome": d.baseline_outcome,
                    "candidate_outcome": d.candidate_outcome,
                    "regressed": d.regressed,
                    "improved": d.improved,
                }
                for d in report.deltas
            ],
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_compare_report(report, baseline_result, candidate_result, args.threshold)

    if report.exceeds_threshold(args.threshold):
        sys.exit(1)


def _print_compare_report(report, baseline, candidate, threshold: float) -> None:
    """Print human-readable compare report."""
    delta_pp = report.verified_rate_delta * 100
    delta_str = f"{delta_pp:+.1f}pp"
    regression = report.exceeds_threshold(threshold)

    print(f"  Baseline:  {baseline.run_id}  ({report.baseline_verified_rate * 100:.1f}% verified)")
    print(f"  Candidate: {candidate.run_id}  ({report.candidate_verified_rate * 100:.1f}% verified)")
    print(f"  Delta:     {delta_str}  /  threshold={-threshold * 100:.1f}pp")
    print(f"  Result:    {'REGRESSION ALERT' if regression else 'OK — no regression'}")

    if report.regressed_scenarios:
        print(f"\nRegressed scenarios ({len(report.regressed_scenarios)}):")
        for sid in report.regressed_scenarios:
            delta = next(d for d in report.deltas if d.scenario_id == sid)
            print(f"  - {sid}: {delta.baseline_outcome} → {delta.candidate_outcome}")

    if report.improved_scenarios:
        print(f"\nImproved scenarios ({len(report.improved_scenarios)}):")
        for sid in report.improved_scenarios:
            delta = next(d for d in report.deltas if d.scenario_id == sid)
            print(f"  + {sid}: {delta.baseline_outcome} → {delta.candidate_outcome}")


# ---------------------------------------------------------------------------
# Subcommand: baseline
# ---------------------------------------------------------------------------

def run_baseline(args: argparse.Namespace) -> None:
    """Symlink a run as a named baseline."""
    run_dir = _resolve_run_dir(args.run_id)
    label = args.label or args.run_id

    baselines_dir = _baselines_dir()
    baselines_dir.mkdir(parents=True, exist_ok=True)

    target = baselines_dir / label

    if target.exists() or target.is_symlink():
        print(f"Warning: Baseline '{label}' already exists; overwriting.", file=sys.stderr)
        target.unlink()

    # Create relative symlink for portability
    target.symlink_to(run_dir.resolve())
    print(f"Baseline '{label}' → {run_dir.resolve()}")
    print(f"  stored at: {target}")


# ---------------------------------------------------------------------------
# Subcommand: publish
# ---------------------------------------------------------------------------

def run_publish(args: argparse.Namespace) -> None:
    """Create a GitHub Discussion thread from a stored run's summary.json."""
    try:
        from reyn.dogfood.publish import (
            _DEFAULT_TEMPLATE_PATH,
            DEFAULT_CATEGORY_SLUG,
            DEFAULT_REPO,
            PublishConfig,
            detect_repo_from_git,
            get_token,
            publish_run,
        )
    except ImportError as exc:
        print(f"Error loading publish module: {exc}", file=sys.stderr)
        sys.exit(2)

    run_dir = _resolve_run_dir(args.run_id)

    # Resolve --repo: explicit flag → git remote → hardcoded default
    repo = args.repo
    if not repo:
        repo = detect_repo_from_git()
    if not repo:
        repo = DEFAULT_REPO

    template_path = Path(args.template) if args.template else _DEFAULT_TEMPLATE_PATH
    if not template_path.exists():
        print(
            f"Error: Discussion template not found: {template_path}\n"
            "Pass --template <path> to point at a custom template.",
            file=sys.stderr,
        )
        sys.exit(2)

    token = get_token()
    if not token and not args.dry_run:
        print(
            "Error: No GitHub token found. Set GH_TOKEN or GITHUB_TOKEN and retry.",
            file=sys.stderr,
        )
        sys.exit(2)

    config = PublishConfig(
        repo=repo,
        category_slug=args.category,
        template_path=template_path,
        token=token,
    )

    scenario_set_path = (
        Path(args.scenario_set)
        if getattr(args, "scenario_set", None)
        else None
    )

    try:
        result = publish_run(
            args.run_id,
            config=config,
            storage_dir=run_dir,
            dry_run=args.dry_run,
            batch_id=args.batch_id,
            topic=args.topic,
            with_transcripts=getattr(args, "with_transcripts", False),
            scenario_set_path=scenario_set_path,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Title: {result['title']}")
        print()
        print("[dry-run] Body:")
        print(result["body"])
    else:
        print(f"Discussion created: {result['discussion_url']}")
        print(f"  Title  : {result['title']}")
        print(f"  Number : #{result['discussion_number']}")
