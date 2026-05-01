"""`reyn eval` — run an eval spec against an app."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from reyn.llm import run_async
from reyn.pricing import TokenUsage

from ..common_args import add_model_arg, add_limits_args, add_output_language_arg
from ..skill_loader import resolve_skill_path, stdlib_root
from ..eval_report import EvalReport
from ..session import Session
from ..summary import print_eval_total


_EVAL_EPILOG = """
Note for skills using `python` preprocessor steps:
  Each python step must be approved before eval — eval runs non-interactively
  and cannot prompt. Two ways to pre-approve:

    (a) Run the target once interactively first:
          reyn run <target_skill> "<sample input>"
        Approve at the prompt; the choice is persisted to .reyn/approvals.yaml.

    (b) Set a project-wide allow in reyn.yaml:
          permissions:
            python.pure: allow      # for pure-mode steps
            python.trusted: allow   # for trusted-mode steps (also requires
                                    # --allow-untrusted-python at runtime)

  Without prior approval, the target's run will fail and the case will be
  marked as not-finished. The framing reads as a target-skill bug; it isn't.
"""


def register(sub) -> None:
    p = sub.add_parser(
        "eval", help="Run an eval spec against an app",
        epilog=_EVAL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("spec", metavar="FILE",
                   help="Path to the eval.md spec file (e.g. reyn/local/my_app/eval.md)")
    add_model_arg(p)
    p.add_argument("--dsl-root", dest="dsl_root", default=None, metavar="DIR",
                   help="DSL root override for the target app (default: inferred from path)")
    add_output_language_arg(p)
    add_limits_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.agent import Agent
    from reyn.compiler import load_dsl_skill
    from reyn.compiler.eval_loader import load_eval_spec

    session = Session.from_args(args)

    try:
        spec = load_eval_spec(args.spec)
    except Exception as e:
        print(f"Error loading eval spec: {e}", file=sys.stderr)
        sys.exit(1)

    target_skill_path, target_dsl_root = _resolve_target(args, spec)

    sl = stdlib_root()
    eval_app_md = sl / "skills" / "eval" / "skill.md"
    try:
        eval_app = load_dsl_skill(str(eval_app_md), dsl_root=str(sl))
    except Exception as e:
        print(f"Error loading eval stdlib app: {e}", file=sys.stderr)
        sys.exit(1)

    # Eval has its own model precedence: CLI > spec > config
    model = args.model or spec.model or session.config.model
    output_language = session.output_language_for(args)
    resolved_model = session.resolver.resolve(model)
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model

    print(f"=== Eval: {spec.skill_dsl_path}  [{len(spec.cases)} case(s)] ===")
    print(f"    model={model_display}")
    print()

    case_results: list[dict] = []
    total_tokens = TokenUsage()
    total_cost_usd = 0.0

    for case in spec.cases:
        case_result, usage, cost = _run_case(
            case, eval_app, args, spec, target_skill_path, target_dsl_root,
            model, output_language, session,
        )
        case_results.append(case_result)
        if usage:
            total_tokens = total_tokens + usage
        if cost is not None:
            total_cost_usd += cost
        print()

    all_passed = all(r.get("passed") for r in case_results)
    passed_count = sum(1 for r in case_results if r.get("passed"))
    overall_sym = "✓" if all_passed else "✗"
    print(f"{'═' * 55}")
    print(f" {overall_sym} {passed_count}/{len(case_results)} cases passed")
    print_eval_total(total_tokens, total_cost_usd)

    report = EvalReport(
        spec_path=args.spec,
        app=spec.skill_dsl_path,
        model=resolved_model,
        cases=case_results,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
    )
    result_path = report.write_to(".reyn",
                                  Path(target_skill_path).parent.name)
    print(f" Results → {result_path}")
    print(f"{'═' * 55}")

    if not all_passed:
        sys.exit(2)


def _resolve_target(args: argparse.Namespace, spec) -> tuple[str, str | None]:
    """Resolve the target app referenced by the eval spec."""
    app_ref = spec.skill_dsl_path
    if "/" not in app_ref and not app_ref.endswith(".md"):
        app_dir, inferred_root = resolve_skill_path(app_ref)
        target_skill_path = str(app_dir / "skill.md")
        target_dsl_root = args.dsl_root or str(inferred_root)
        print(f"resolved        : {target_skill_path}  (dsl-root: {target_dsl_root})")
        return target_skill_path, target_dsl_root
    return app_ref, spec.dsl_root or args.dsl_root


def _run_case(
    case, eval_app, args, spec, target_skill_path, target_dsl_root,
    model, output_language, session,
) -> tuple[dict, TokenUsage | None, float | None]:
    from reyn.agent import Agent

    print(f"━━━ case: {case.name} ━━━")
    print(f"  input: {case.input[:120]}")

    phase_criteria = [
        {
            "phase_name": pc.phase,
            "criteria": [
                {"description": qc.text}
                if qc.tag != "aspirational"
                else {"description": qc.text, "required": False}
                for qc in pc.criteria
            ],
        }
        for pc in case.phase_criteria
        if pc.phase is not None
    ]

    input_artifact: dict = {
        "type": "eval_case_input",
        "data": {
            "case_name": case.name,
            "case_input": case.input,
            "spec_path": args.spec,
            "target_skill_path": target_skill_path,
            "phase_criteria": phase_criteria,
        },
    }
    if target_dsl_root:
        input_artifact["data"]["dsl_root"] = target_dsl_root

    agent = Agent(
        model=model,
        resolver=session.resolver,
        limits=session.limits_for(args),
        prompt_cache_enabled=session.config.prompt_cache_enabled,
    )

    try:
        result = run_async(
            agent.run(eval_app, input_artifact, output_language=output_language)
        )
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return ({
            "case_name": case.name,
            "passed": False,
            "overall_score": 0.0,
            "passed_criteria": 0,
            "total_criteria": 0,
            "weakest_phase": "",
            "summary": str(e),
        }, None, None)

    data = result.data
    passed_sym = "✓" if data.get("passed") else "✗"
    score = data.get("overall_score", 0.0)
    pc_count = data.get("passed_criteria", 0)
    tc_count = data.get("total_criteria", 0)
    print(f"  {passed_sym} score={score:.2f}  ({pc_count}/{tc_count} required)")
    if data.get("weakest_phase"):
        print(f"  weakest: {data['weakest_phase']}")
    if data.get("summary"):
        print(f"  {data['summary']}")

    return ({"case_name": case.name, **data}, result.token_usage, result.cost_usd)
