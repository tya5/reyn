"""Tier 2: OS invariant — #1172 CompactionEngine is resolver-aware (by-construction).

THE BUG (dead-end-critical, all 3 compaction axes): chat / planner / phase each
constructed ``CompactionEngine(model=<class>)`` with an UNRESOLVED model class
("standard" / "light"). The engine forwards ``model`` straight to
``litellm.acompletion``, which rejects a class name
(``BadRequestError model=standard``) — so compaction failed on EVERY trigger and
the entire dead-end-prevention stack (axis-1/2, retry_loop, chat-cap,
re-summarize) was non-functional at runtime. Fake-engine unit tests missed it
because they never exercised the real model→litellm path.

THE FIX (by-construction): ``CompactionEngine.__init__`` resolves ``model`` via a
``ModelResolver`` at construction, so the engine NEVER hands an unresolved class
to litellm regardless of caller. This file pins both halves with REAL
collaborators (no Fake engine, no mock resolver):

  1. Behavioral — a real ``CompactionEngine`` built from a model CLASS + a real
     ``ModelResolver`` calls ``litellm.acompletion`` with the RESOLVED litellm
     string, never the raw class. Covers all 3 axes (one shared engine).
  2. Regression guard (AST) — every ``CompactionEngine(...)`` construction in
     ``src/`` passes a ``resolver=`` kwarg, so a future 4th caller cannot
     reintroduce the unresolved-class leak. Catches the aliased construction
     (``_CCE(...)`` in kernel/runtime.py) too.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from reyn.config import CompactionConfig
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact

# ── Behavioral: real engine + real resolver → litellm sees the RESOLVED string ──


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _run_capture(monkeypatch, engine: CompactionEngine) -> str:
    """Drive one real compact(); return the ``model`` litellm.acompletion saw."""
    seen: dict[str, str] = {}

    async def _capture(**kwargs):
        seen["model"] = kwargs["model"]
        return _resp(json.dumps({
            "topic_arc": "arc", "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))

    monkeypatch.setattr("litellm.acompletion", _capture)
    chunk = HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )
    asyncio.run(engine.compact(chunk))
    return seen["model"]


def test_model_class_is_resolved_before_litellm(monkeypatch) -> None:
    """Tier 2: a CompactionEngine built from the CLASS "standard" calls litellm
    with the resolved litellm string — never the raw class (#1172 bug)."""
    resolver = ModelResolver({"standard": "openai/resolved-standard"})
    engine = CompactionEngine(
        model="standard",  # the class that broke litellm pre-#1172
        events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
        resolver=resolver,
    )
    model_seen = _run_capture(monkeypatch, engine)
    assert model_seen == "openai/resolved-standard", (
        "the engine must resolve the model class before calling litellm; "
        f"litellm saw {model_seen!r} (a raw class would be rejected)"
    )
    assert model_seen != "standard", "the unresolved class must never reach litellm"


def test_planner_default_light_class_is_resolved(monkeypatch) -> None:
    """Tier 2: the planner axis's default class "light" is resolved too."""
    resolver = ModelResolver({"light": "openai/resolved-light"})
    engine = CompactionEngine(
        model="light",  # planner.py router_model default
        events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
        resolver=resolver,
    )
    assert _run_capture(monkeypatch, engine) == "openai/resolved-light"


def test_literal_litellm_string_passes_through(monkeypatch) -> None:
    """Tier 2: an unknown literal litellm string is unchanged (passthrough);
    resolver omitted defaults to an empty (builtin-only) resolver, so a string
    that is neither a configured class nor a builtin reaches litellm as-is."""
    engine = CompactionEngine(
        model="myvendor/custom-not-a-builtin",
        events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
    )  # no resolver → passthrough default (ModelResolver({}))
    assert _run_capture(monkeypatch, engine) == "myvendor/custom-not-a-builtin"


# ── Regression guard: every src CompactionEngine construction passes resolver= ──


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _compaction_engine_constructions(path: Path) -> list[ast.Call]:
    """Every CompactionEngine(...) construction in *path*, resolving import
    aliases (e.g. ``from ... import CompactionEngine as _CCE``) so the kernel
    runtime's ``_CCE(...)`` call is caught alongside bare ``CompactionEngine(``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "compaction.engine"
        ):
            for alias in node.names:
                if alias.name == "CompactionEngine":
                    aliases.add(alias.asname or alias.name)
    if not aliases:
        return []
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (
            node.func.id in aliases
        ):
            calls.append(node)
    return calls


def test_every_src_construction_passes_resolver() -> None:
    """Tier 2: every CompactionEngine(...) construction under src/ passes a
    ``resolver=`` kwarg — the by-construction guarantee that no compaction axis
    leaks an unresolved model class to litellm (#1172). A new construction site
    that omits resolver= reintroduces the dead-end-critical bug and fails here.
    """
    src = _repo_root() / "src" / "reyn"
    found = 0
    for py in src.rglob("*.py"):
        for call in _compaction_engine_constructions(py):
            found += 1
            kwargs = {kw.arg for kw in call.keywords if kw.arg is not None}
            assert "resolver" in kwargs, (
                f"{py.relative_to(_repo_root())}:{call.lineno} constructs "
                "CompactionEngine without resolver= — an unresolved model class "
                "would be handed to litellm (BadRequestError). Pass the caller's "
                "ModelResolver so the engine resolves the class by construction."
            )
    assert found >= 3, (
        f"expected the 3 known compaction axes (chat/planner/phase), found {found}"
        " — did a construction site move? update this guard."
    )
