"""Shared types + YAML loader for dogfood batch tools.

``dogfood_batch_dispatch.py`` and ``dogfood_aggregate.py`` both consume
the same YAML batch config — this module defines the schema and the
load path once so the two tools stay consistent.

Config shape (YAML)::

    batch:
      name: B44
      date: 2026-05-21
      head: e96d479f
      env_vars:
        REYN_EMPTY_STOP_RETRY: "1"
      user_params:
        hot_list_n: 10
        models_tier: flash-lite
      hard_caps:
        tool_uses: 50
        wall_clock_min: 15

    workers:
      - name: W1
        scenario_set: chat_router_smoke.yaml
        scenario_set_path: dogfood/scenarios/chat_router_smoke.yaml
        port: 8231
        n_scenarios: 7
        worktree: /tmp/reyn-worktrees/b44-1
        agent_prefix: dogfood-b44-1-s
      ...

    past_batches:
      - name: B43
        aggregate_path: docs/deep-dives/journal/dogfood/2026-05-20-batch-43-post-empty-stop-retry/aggregate.json
      - name: B42
        aggregate_path: docs/deep-dives/journal/dogfood/2026-05-19-batch-42-b40-v2-cumulative/aggregate.json

    journal_dir: docs/deep-dives/journal/dogfood/2026-05-21-batch-44-...
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _count_scenarios(scenario_yaml_path: str) -> int | None:
    """Count ``- id:`` entries in a scenario yaml file.

    Returns ``None`` when the file is unreadable / malformed / not a
    mapping with a ``scenarios`` list — the caller falls back to the
    declared ``n_scenarios`` rather than crashing the batch load.
    """
    try:
        path = Path(scenario_yaml_path)
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        scenarios = data.get("scenarios")
        if not isinstance(scenarios, list):
            return None
        return sum(
            1 for s in scenarios
            if isinstance(s, dict) and "id" in s
        )
    except Exception:  # noqa: BLE001 — best-effort; never break batch load
        return None


@dataclass(frozen=True)
class BatchMeta:
    name: str
    date: str
    head: str
    env_vars: dict[str, str] = field(default_factory=dict)
    user_params: dict[str, Any] = field(default_factory=dict)
    hard_caps: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    scenario_set: str
    scenario_set_path: str
    port: int
    n_scenarios: int  # caller-declared, validated/overridden against actual scenario yaml at load time
    worktree: str
    agent_prefix: str


@dataclass(frozen=True)
class PastBatch:
    name: str
    aggregate_path: str


@dataclass(frozen=True)
class BatchConfig:
    batch: BatchMeta
    workers: tuple[WorkerSpec, ...]
    past_batches: tuple[PastBatch, ...]
    journal_dir: str


def load_batch_config(path: Path) -> BatchConfig:
    """Parse the YAML config into a typed BatchConfig.

    Validates required keys at every level. ValueError on missing fields
    so the caller sees a clean error instead of a downstream KeyError.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping; got {type(raw).__name__}")
    for top_key in ("batch", "workers", "past_batches", "journal_dir"):
        if top_key not in raw:
            raise ValueError(f"config missing required top-level key: {top_key!r}")

    batch_raw = raw["batch"]
    for key in ("name", "date", "head"):
        if key not in batch_raw:
            raise ValueError(f"batch.{key} is required")
    batch = BatchMeta(
        name=batch_raw["name"],
        date=batch_raw["date"],
        head=batch_raw["head"],
        env_vars=dict(batch_raw.get("env_vars") or {}),
        user_params=dict(batch_raw.get("user_params") or {}),
        hard_caps=dict(batch_raw.get("hard_caps") or {}),
    )

    workers_raw = raw["workers"]
    if not workers_raw:
        raise ValueError("workers must be non-empty")
    workers: list[WorkerSpec] = []
    for i, w in enumerate(workers_raw):
        for key in ("name", "scenario_set", "scenario_set_path", "port",
                    "worktree", "agent_prefix"):
            if key not in w:
                raise ValueError(f"workers[{i}].{key} is required")
        # B47 carry-over fix (2026-05-21): n_scenarios is now derived from
        # the actual scenario yaml. Caller-declared value is honoured only
        # when it matches the on-disk scenario count; mismatch emits a
        # warning and uses the on-disk count. This prevents the recurring
        # W6 drift where ``n_scenarios: 7`` was hardcoded against a
        # ``plan_mode.yaml`` that had 3 scenarios → sub-agent dispatched
        # 7 agents, 4 of them ended up Blocked.
        actual_n = _count_scenarios(w["scenario_set_path"])
        declared_n = int(w.get("n_scenarios", actual_n))
        if actual_n is not None and declared_n != actual_n:
            warnings.warn(
                f"workers[{i}].n_scenarios={declared_n} does not match "
                f"the actual scenario count in {w['scenario_set_path']!r} "
                f"(= {actual_n}). Using {actual_n}. Drop the n_scenarios "
                f"field from the batch yaml to silence this warning.",
                UserWarning,
                stacklevel=2,
            )
        n_scenarios = actual_n if actual_n is not None else declared_n
        workers.append(WorkerSpec(
            name=w["name"],
            scenario_set=w["scenario_set"],
            scenario_set_path=w["scenario_set_path"],
            port=int(w["port"]),
            n_scenarios=n_scenarios,
            worktree=w["worktree"],
            agent_prefix=w["agent_prefix"],
        ))

    past_batches = tuple(
        PastBatch(name=p["name"], aggregate_path=p["aggregate_path"])
        for p in (raw.get("past_batches") or [])
    )

    return BatchConfig(
        batch=batch,
        workers=tuple(workers),
        past_batches=past_batches,
        journal_dir=raw["journal_dir"],
    )
