#!/usr/bin/env python3
"""embedding_bench.py — FP-0043 Phase 1 N=20 retrieval benchmark runner.

Measures **Axis 1 (precision)** — does the embedding ranking surface
the expected qualified_name in the top-K? — across one or more embedding
classes for the bench fixtures declared in
``tests/data/embedding_bench/manifest.yaml``.

Axis 2 (= LLM call-rate) is NOT measured here; it requires a live router
loop with an LLM and is captured by the dogfood scenario sweep
(see #925 / B57 in FP-0043 §Phases).

Usage:
    # Single class (default: light, = "openai/text-embedding-3-small")
    python scripts/embedding_bench.py

    # Multiple classes — comma-separated
    python scripts/embedding_bench.py --classes light,standard

    # JSON output for downstream aggregation
    python scripts/embedding_bench.py --json

    # Custom manifest path
    python scripts/embedding_bench.py --manifest tests/data/embedding_bench/manifest.yaml

Output (table mode):

    bench: tests/data/embedding_bench/manifest.yaml (20 fixtures)

    class       model                              hit@1  hit@3  hit@5  N
    light       openai/text-embedding-3-small        12     18     19   20

    misses (hit@5 fail):
      - file_glob_grep_search                expected=file__grep            top=[...]

The catalog is enumerated via the production list_actions handler with
no router state attached (= static-categories only). Dynamic categories
(skill / mcp.tool / memory.entry / etc.) without router state surface
as their static-category placeholders only; this is intentional — bench
queries target the universal-catalog surface a fresh-context LLM would
see, not the per-session catalog superset.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Fixture:
    """One row of the bench manifest."""

    id: str
    prompt: str
    expected_action: str
    axis: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class HitResult:
    """Per-fixture result: where did `expected_action` land in the top-K?"""

    fixture_id: str
    expected: str
    rank: int  # 0-indexed rank in the top-K, or -1 if not present
    top_k: tuple[str, ...]


def _load_manifest(path: Path) -> tuple[str, list[Fixture]]:
    """Load manifest.yaml into (commit_sha, fixtures). Strict; no defaults."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    sha = data.get("last_synced_commit_sha", "")
    raw_fixtures = data.get("fixtures") or []
    fixtures: list[Fixture] = []
    for raw in raw_fixtures:
        if "precision" not in (raw.get("axis") or []):
            continue  # this runner measures precision only
        fixtures.append(Fixture(
            id=raw["id"],
            prompt=raw["prompt"],
            expected_action=raw["expected_action"],
            axis=tuple(raw["axis"]),
            source=raw.get("source", ""),
        ))
    return sha, fixtures


async def _enumerate_catalog() -> list[dict[str, Any]]:
    """Return the catalog items the bench will index, as list_actions does.

    Pulls every category through the production handler with a None
    router_state so only static-category items surface. The result shape
    matches what ActionEmbeddingIndex.build expects.
    """
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import CATEGORIES, LIST_ACTIONS

    class _Events:
        def emit(self, *a: Any, **kw: Any) -> None:
            pass

    ctx = ToolContext(
        events=_Events(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )

    items: list[dict[str, Any]] = []
    for cat in CATEGORIES:
        page = await LIST_ACTIONS.handler({"category": [cat]}, ctx)
        for item in page.get("items", []):
            items.append({
                "qualified_name": item["qualified_name"],
                "short_description": item.get("short_description", ""),
            })
    return items


async def _run_class(
    class_name: str,
    fixtures: list[Fixture],
    catalog: list[dict[str, Any]],
    top_k: int,
) -> tuple[str, list[HitResult]]:
    """Build a fresh index for `class_name` and score each fixture."""
    from reyn.config import load_config
    from reyn.data.embedding.litellm_provider import LiteLLMEmbeddingProvider
    from reyn.tools.action_index import ActionEmbeddingIndex

    cfg = load_config()
    provider = LiteLLMEmbeddingProvider(config=cfg.embedding)
    resolved = provider.resolve_model(class_name)

    index = ActionEmbeddingIndex(persist_dir=None)
    await index.build(catalog, provider, class_name)

    results: list[HitResult] = []
    for f in fixtures:
        hits = await index.query(f.prompt, provider, class_name, top_k=top_k)
        names = tuple(h["qualified_name"] for h in hits)
        try:
            rank = names.index(f.expected_action)
        except ValueError:
            rank = -1
        results.append(HitResult(
            fixture_id=f.id,
            expected=f.expected_action,
            rank=rank,
            top_k=names,
        ))
    return resolved, results


def _summarize(class_name: str, model: str, results: list[HitResult]) -> dict[str, Any]:
    """Compute hit@1 / hit@3 / hit@5 counts for a class."""
    n = len(results)
    hit1 = sum(1 for r in results if 0 <= r.rank < 1)
    hit3 = sum(1 for r in results if 0 <= r.rank < 3)
    hit5 = sum(1 for r in results if 0 <= r.rank < 5)
    return {
        "class": class_name,
        "model": model,
        "hit@1": hit1,
        "hit@3": hit3,
        "hit@5": hit5,
        "N": n,
    }


def _print_table(summaries: list[dict[str, Any]]) -> None:
    """Print results as an aligned table."""
    header = f"  {'class':12} {'model':40} {'hit@1':>6} {'hit@3':>6} {'hit@5':>6} {'N':>4}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in summaries:
        print(
            f"  {s['class']:12} {s['model'][:40]:40} "
            f"{s['hit@1']:>6} {s['hit@3']:>6} {s['hit@5']:>6} {s['N']:>4}"
        )


def _print_misses(class_name: str, results: list[HitResult]) -> None:
    """Print fixtures that missed hit@5 for debugging."""
    misses = [r for r in results if r.rank == -1 or r.rank >= 5]
    if not misses:
        return
    print(f"\n  misses for class={class_name!r} (hit@5 fail):")
    for r in misses:
        top3 = ", ".join(r.top_k[:3])
        rank_str = "miss" if r.rank == -1 else f"rank={r.rank}"
        print(f"    - {r.fixture_id:36} expected={r.expected:35} {rank_str:8} top3=[{top3}]")


async def _main_async(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    sha, fixtures = _load_manifest(manifest_path)
    if not fixtures:
        print(f"no precision-axis fixtures in {manifest_path}", file=sys.stderr)
        return 1

    catalog = await _enumerate_catalog()

    if args.dry_run:
        # Env-independent validation: manifest loads, catalog enumerates,
        # every fixture's expected_action appears in the catalog. Skips
        # the embedding provider call entirely (= safe to run in CI / on
        # machines without embedding credentials).
        catalog_qns = {item["qualified_name"] for item in catalog}
        unknown = [f for f in fixtures if f.expected_action not in catalog_qns]
        print(f"dry-run: manifest={manifest_path} sha={sha[:12]}")
        print(f"  fixtures (precision-axis): {len(fixtures)}")
        print(f"  catalog size: {len(catalog)}")
        if unknown:
            print(f"  unknown expected_action entries ({len(unknown)}):")
            for f in unknown:
                print(f"    - {f.id}: {f.expected_action}")
            return 3
        print("  all expected_action entries present in catalog ✓")
        return 0

    class_names = [c.strip() for c in args.classes.split(",") if c.strip()]
    summaries: list[dict[str, Any]] = []
    all_results: dict[str, list[HitResult]] = {}
    for class_name in class_names:
        try:
            model, results = await _run_class(class_name, fixtures, catalog, top_k=args.top_k)
        except Exception as exc:
            print(f"class={class_name!r} build/query failed: {exc}", file=sys.stderr)
            return 2
        summaries.append(_summarize(class_name, model, results))
        all_results[class_name] = results

    if args.json:
        out = {
            "manifest": str(manifest_path),
            "manifest_sha": sha,
            "catalog_size": len(catalog),
            "summaries": summaries,
            "results": {
                cn: [
                    {
                        "fixture_id": r.fixture_id,
                        "expected": r.expected,
                        "rank": r.rank,
                        "top_k": list(r.top_k),
                    }
                    for r in rs
                ]
                for cn, rs in all_results.items()
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"bench: {manifest_path} ({len(fixtures)} precision-axis fixtures, "
          f"catalog={len(catalog)} actions)\n")
    _print_table(summaries)
    for class_name in class_names:
        _print_misses(class_name, all_results[class_name])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--manifest",
        default="tests/data/embedding_bench/manifest.yaml",
        help="Path to manifest.yaml (default: tests/data/embedding_bench/manifest.yaml)",
    )
    p.add_argument(
        "--classes",
        default="light",
        help="Comma-separated embedding class names to compare (default: light).",
    )
    p.add_argument(
        "--top-k", type=int, default=5,
        help="Top-K cutoff for hit measurement (default 5).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of table.")
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Validate manifest + catalog coverage without calling the "
            "embedding provider. Useful for CI / no-credential env."
        ),
    )
    args = p.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
