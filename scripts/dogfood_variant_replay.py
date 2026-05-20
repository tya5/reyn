"""Multi-variant trace-patch-replay runner for dogfood attractor analysis.

Consolidates the repeated "N=10 × M variant ablation" pattern that
attracts most of the dogfood-coder cost during NF investigations:
each NF typically requires 3-6 variant comparisons, each variant is
N=10 ``scripts/llm_replay.py --patch ... --n 1`` invocations + a
custom Python classifier expressed inline in bash. This script wraps
those calls behind a single YAML config + emits a comparison table.

Usage::

    python scripts/dogfood_variant_replay.py --config variant_ablation.yaml

Config format (YAML)::

    trace: /tmp/reyn-worktrees/b43-7/.reyn/llm_trace.jsonl
    req_id: 1847830e-8eb0-4056-b129-cc160dc7d3f9
    model: openai/gemini-2.5-flash-lite
    n: 10
    parallel: 8                         # max concurrent replay subprocesses
    classifiers:
      - label: EMPTY
        expr: 'not content and not tool_calls'
      - label: HALLUCINATE
        expr: 'content and len(content) >= 200 and "consistency" in content.lower()'
      - label: ACK
        expr: 'content and 0 < len(content) < 300'
      - label: TOOL_CALL
        expr: 'bool(tool_calls)'
      - label: OTHER          # fallback bucket
        expr: 'True'
    variants:
      - name: A_bare
        patches: []
      - name: D_directive
        patches:
          - 'messages[3].content+=\n\n---\nThe skill has been spawned...'

The script runs each (variant, sample) pair through ``llm_replay.py``
with ``--n 1 --output-format json``, parses the JSON response, applies
the classifier expressions in order (first match wins), aggregates the
counts per variant, and prints a comparison table. Each classifier
expression evaluates against three locals: ``content`` (str|None),
``tool_calls`` (list), and ``finish_reason`` (str).

Tier 2 testing seam: ``classify_response`` is imported by the test
suite to verify classifier ordering + fallback behaviour without
running real subprocesses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Path to llm_replay.py, resolved relative to this script.
_LLM_REPLAY = str(Path(__file__).parent / "llm_replay.py")


@dataclass(frozen=True)
class ClassifierRule:
    label: str
    expr: str


@dataclass(frozen=True)
class Variant:
    name: str
    patches: tuple[str, ...]


@dataclass
class RunConfig:
    trace: str
    req_id: str
    model: str
    n: int
    parallel: int
    classifiers: tuple[ClassifierRule, ...]
    variants: tuple[Variant, ...]


def load_config(path: Path) -> RunConfig:
    """Parse the YAML config into a typed RunConfig.

    Validates required keys + classifier ordering. Raises ValueError
    on missing fields so the user sees a clean error rather than a
    KeyError mid-run.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping; got {type(raw).__name__}")
    required = ("trace", "req_id", "model", "n", "classifiers", "variants")
    for key in required:
        if key not in raw:
            raise ValueError(f"config missing required key: {key!r}")
    classifiers = tuple(
        ClassifierRule(label=c["label"], expr=c["expr"])
        for c in raw["classifiers"]
    )
    if not classifiers:
        raise ValueError("classifiers must be non-empty")
    variants = tuple(
        Variant(name=v["name"], patches=tuple(v.get("patches") or []))
        for v in raw["variants"]
    )
    if not variants:
        raise ValueError("variants must be non-empty")
    return RunConfig(
        trace=raw["trace"],
        req_id=raw["req_id"],
        model=raw["model"],
        n=int(raw["n"]),
        parallel=int(raw.get("parallel", 8)),
        classifiers=classifiers,
        variants=variants,
    )


# Whitelisted builtins for classifier expressions — covers the common
# response-shape predicates (len / bool / str / any / all / etc.)
# without exposing dangerous globals (``open``, ``__import__``, …).
_CLASSIFIER_BUILTINS: dict[str, Any] = {
    name: __builtins__[name] if isinstance(__builtins__, dict)
    else getattr(__builtins__, name)
    for name in (
        "len", "str", "int", "float", "bool",
        "list", "dict", "tuple", "set",
        "any", "all",
        "min", "max", "sum",
        "abs", "round",
        "isinstance", "type",
    )
}


def classify_response(
    response: dict[str, Any],
    classifiers: tuple[ClassifierRule, ...],
) -> str:
    """Return the first-matching classifier label for one response.

    ``response`` is the parsed llm_replay JSON output (a dict with at
    least ``content`` and ``tool_calls`` keys). Each classifier's
    ``expr`` is evaluated with ``content`` / ``tool_calls`` /
    ``finish_reason`` exposed as locals + ``_CLASSIFIER_BUILTINS``
    (= len / bool / any / etc.) as globals; first truthy wins. The
    final classifier acts as the catch-all fallback.

    If no classifier matches (= caller's final rule isn't ``True``),
    returns ``"UNCLASSIFIED"`` so the table still tallies the run.
    """
    locals_ns = {
        "content": response.get("content") or "",
        "tool_calls": response.get("tool_calls") or [],
        "finish_reason": response.get("finish_reason") or "",
    }
    globals_ns: dict[str, Any] = {"__builtins__": _CLASSIFIER_BUILTINS}
    for rule in classifiers:
        try:
            if eval(rule.expr, globals_ns, locals_ns):
                return rule.label
        except Exception:  # noqa: BLE001
            continue
    return "UNCLASSIFIED"


async def _run_one_replay(
    trace: str,
    req_id: str,
    model: str,
    patches: tuple[str, ...],
) -> dict[str, Any]:
    """Execute one ``llm_replay.py`` subprocess and return the parsed
    response dict. On parse failure returns ``{}`` so the caller can
    proceed with an UNCLASSIFIED bucket rather than crashing the batch.
    """
    cmd = [
        sys.executable, _LLM_REPLAY,
        "--trace", trace, req_id,
        "--model", model,
        "--n", "1",
        "--output-format", "json",
    ]
    for p in patches:
        cmd.extend(["--patch", p])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env_with_api_base(),
    )
    out_bytes, _ = await proc.communicate()
    text = out_bytes.decode("utf-8", errors="replace")
    return _extract_last_json(text)


def _env_with_api_base() -> dict[str, str]:
    """Build the subprocess env, propagating ``OPENAI_API_BASE`` from
    the caller's shell. Most dogfood replays target the local LiteLLM
    proxy; omitting the env hits Vertex auth and fails.
    """
    import os
    env = dict(os.environ)
    env.setdefault("OPENAI_API_BASE", "http://localhost:4000")
    return env


def _extract_last_json(text: str) -> dict[str, Any]:
    """Find the last top-level JSON object in ``text``. ``llm_replay``
    prints a preamble (= run banner / patch summary) followed by the
    JSON when ``--output-format json``; this isolates the JSON.
    """
    lines = text.strip().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    return {}


async def run_ablation(config: RunConfig) -> dict[str, Counter[str]]:
    """Run the full variant × sample matrix and return per-variant
    label counts.

    Concurrency is bounded by ``config.parallel`` via an asyncio
    semaphore — wall time is roughly
    ``ceil(n_variants * n_samples / parallel) * llm_latency``.
    """
    sem = asyncio.Semaphore(config.parallel)
    results: dict[str, Counter[str]] = {v.name: Counter() for v in config.variants}

    async def _one(variant: Variant, sample_idx: int) -> None:
        async with sem:
            response = await _run_one_replay(
                config.trace, config.req_id, config.model, variant.patches,
            )
        label = classify_response(response, config.classifiers)
        results[variant.name][label] += 1

    tasks = [
        _one(v, i)
        for v in config.variants
        for i in range(config.n)
    ]
    await asyncio.gather(*tasks)
    return results


def render_table(
    results: dict[str, Counter[str]],
    classifiers: tuple[ClassifierRule, ...],
    n: int,
) -> str:
    """Render the variant × label counts as a Markdown comparison
    table. Columns ordered by ``classifiers`` order so the caller's
    YAML ordering is preserved in the output.
    """
    labels = [c.label for c in classifiers]
    # Always include UNCLASSIFIED if any variant has it
    if any(results[v].get("UNCLASSIFIED", 0) for v in results):
        labels.append("UNCLASSIFIED")
    # Header
    name_width = max(len("variant"), max(len(name) for name in results))
    col_width = max(4, max(len(lbl) for lbl in labels))
    sep = "|"
    out = [
        f"{sep} {'variant'.ljust(name_width)} {sep} "
        + f" {sep} ".join(lbl.ljust(col_width) for lbl in labels)
        + f" {sep}",
        f"{sep}{'-' * (name_width + 2)}{sep}"
        + sep.join("-" * (col_width + 2) for _ in labels)
        + sep,
    ]
    for name, counts in results.items():
        row = f"{sep} {name.ljust(name_width)} {sep} "
        row += f" {sep} ".join(
            f"{counts.get(lbl, 0)}/{n}".ljust(col_width) for lbl in labels
        )
        row += f" {sep}"
        out.append(row)
    return "\n".join(out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--config", required=True, type=Path,
        help="YAML config (trace, req_id, model, n, variants, classifiers).",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-call progress; print only the final table.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if not args.quiet:
        print(
            f"=== Variant ablation: {config.req_id[:12]}... "
            f"× {len(config.variants)} variants × N={config.n} ===",
            file=sys.stderr,
        )
    results = asyncio.run(run_ablation(config))
    print(render_table(results, config.classifiers, config.n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
