"""Tier 2: #5267 (Family A) — the task-funnel-bypass gate itself.

Real filesystem fixtures throughout (a real ``tmp_path`` tree of ``.py``
files) — the function under test reads real file content and parses real
ASTs, so faking the filesystem would test nothing real. Mirrors this
repo's own established convention for a static gate's own test file
(``tests/scripts/test_check_cancel_swallow_4988.py``).
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.check_task_funnel_bypass import (
    _BASELINE_PATH,
    _SRC_DIR,
    in_scope_files,
    load_declared_baseline,
    offending_files,
)


def test_a_required_constructor_param_module_is_in_scope(tmp_path: Path) -> None:
    """Tier 2: criterion ① — a module whose ``__init__`` takes
    ``task_tracker`` as a required (no default, non-Optional)
    ``TrackedTaskSet``-typed param is in scope, the ``outbox_hub.py`` /
    ``spawn_tracker.py`` shape."""
    (tmp_path / "hub.py").write_text(
        "class Hub:\n"
        "    def __init__(self, *, task_tracker: \"TrackedTaskSet\") -> None:\n"
        "        self._task_tracker = task_tracker\n",
        encoding="utf-8",
    )
    assert in_scope_files(tmp_path) == [tmp_path / "hub.py"]


def test_an_optional_tracker_param_module_is_not_in_scope(tmp_path: Path) -> None:
    """Tier 2: accept-side — ``ChainManager``'s own deliberately
    None-tolerant ``task_tracker: TrackedTaskSet | None = None`` must NOT
    bring a module into scope; forcing every optional-tracker caller to
    thread one through was explicitly rejected (architect/lead-coder,
    #5267 comment thread) as out of proportion to this gate's own
    concern."""
    (tmp_path / "chain_manager.py").write_text(
        "class ChainManager:\n"
        "    def __init__(self, *, task_tracker: \"TrackedTaskSet | None\" = None) -> None:\n"
        "        self._task_tracker = task_tracker\n"
        "\n"
        "    def start(self) -> None:\n"
        "        import asyncio\n"
        "        self._t = asyncio.create_task(self._watch())\n",
        encoding="utf-8",
    )
    assert in_scope_files(tmp_path) == []
    assert offending_files(tmp_path) == []


def test_a_module_that_constructs_its_own_tracker_is_in_scope(tmp_path: Path) -> None:
    """Tier 1: criterion ② — THE case #5267 exists for. ``session.py``
    does not RECEIVE a ``TrackedTaskSet``, it CONSTRUCTS one
    (``self._background_tasks = TrackedTaskSet()``); criterion ① alone
    structurally excludes exactly the file #5267's own chain names — this
    is the falsifier for that gap (architect self-correction, #5267)."""
    (tmp_path / "session.py").write_text(
        "from reyn.runtime.tracked_tasks import TrackedTaskSet\n"
        "\n"
        "class Session:\n"
        "    def __init__(self) -> None:\n"
        "        self._background_tasks = TrackedTaskSet()\n",
        encoding="utf-8",
    )
    assert in_scope_files(tmp_path) == [tmp_path / "session.py"]


def test_a_raw_create_task_in_a_tracker_owning_module_is_flagged(tmp_path: Path) -> None:
    """Tier 1: the gate's own reason to exist — a module that owns a
    TrackedTaskSet but spawns via a bare ``asyncio.create_task`` instead
    of routing through it. This is the exact shape of #5267's own chain
    (``Session.run_one_iteration``'s ``_turn_owner_task``)."""
    (tmp_path / "session.py").write_text(
        "import asyncio\n"
        "from reyn.runtime.tracked_tasks import TrackedTaskSet\n"
        "\n"
        "class Session:\n"
        "    def __init__(self) -> None:\n"
        "        self._background_tasks = TrackedTaskSet()\n"
        "\n"
        "    async def run_one_iteration(self):\n"
        "        self._turn_owner_task = asyncio.create_task(self._run_turn_body())\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    assert offenders == [(tmp_path / "session.py", [9])]


def test_routing_through_the_funnels_own_spawn_is_not_flagged(tmp_path: Path) -> None:
    """Tier 1: accept-side — the ACTUAL fix this gate prescribes
    (``self._task_tracker.spawn(...)``, the ``outbox_hub.py`` /
    ``spawn_tracker.py`` shape) must not itself be flagged; a gate that
    flags its own prescribed fix would be self-defeating."""
    (tmp_path / "hub.py").write_text(
        "class Hub:\n"
        "    def __init__(self, *, task_tracker: \"TrackedTaskSet\") -> None:\n"
        "        self._task_tracker = task_tracker\n"
        "\n"
        "    def start(self) -> None:\n"
        "        self._drain_task = self._task_tracker.spawn(self._drain(), name='drain')\n",
        encoding="utf-8",
    )
    assert offending_files(tmp_path) == []


def test_a_module_with_no_tracker_at_all_is_not_in_scope_regardless_of_create_task(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity — a module with plenty of raw ``asyncio.
    create_task`` calls but NO ``TrackedTaskSet`` ownership at all (most
    of ``src/`` — architect's own explicit warning against scoping this
    to "everything", #5267 comment thread) must never be flagged; this
    gate's whole point is the funnel-bypass, not raw ``create_task`` in
    general."""
    (tmp_path / "unrelated.py").write_text(
        "import asyncio\n"
        "\n"
        "async def fire_and_forget():\n"
        "    asyncio.create_task(do_work())\n",
        encoding="utf-8",
    )
    assert in_scope_files(tmp_path) == []
    assert offending_files(tmp_path) == []


def test_the_funnels_own_implementation_file_is_excluded_from_scope(tmp_path: Path) -> None:
    """Tier 1: ``tracked_tasks.py`` itself constructs no ``TrackedTaskSet``
    (it defines the class) and its own ``spawn()`` method's ``asyncio.
    create_task`` call is the one legitimate call site this whole gate
    exists to fence everything else away from — the real file's own path
    is excluded unconditionally in the source (a name match, not a
    tmp_path fixture, since the exclusion is keyed off the real repo
    path)."""
    from scripts.check_task_funnel_bypass import _TRACKED_TASKS_MODULE

    assert _TRACKED_TASKS_MODULE.name == "tracked_tasks.py"
    real_text = _TRACKED_TASKS_MODULE.read_text(encoding="utf-8")
    assert "asyncio.create_task(coro, name=name)" in real_text, (
        "sanity: the real funnel file must still contain its own "
        "legitimate create_task call for this exclusion to matter at all"
    )


def test_the_real_repo_tree_matches_the_declared_baseline_exactly() -> None:
    """Tier 2: the gate's own starting population against the real tree —
    verified, not assumed (matching the sibling gates' own "run it before
    shipping it" discipline). #5267's own comment thread explicitly
    scoped the 4 real defect sites (including this one, ``session.py``'s
    ``_turn_owner_task``) to their OWN separate PRs, not this one — so
    this gate's baseline is NOT zero.

    lead-coder review (#5270): a bare "the tree has exactly this one
    disclosed hit" pin, whose own failure message says "update this pin,"
    silently absorbs a NEW hit — defect or false positive,
    indistinguishable — with no record that anything happened, which
    means the module docstring's own "0 new false positives across 20
    PRs" promotion condition could never be evaluated (nothing ever
    counts a false positive when one fires). Comparing against the
    DECLARED baseline instead (``task_funnel_bypass_baseline.json``)
    means a new, undeclared offender fails THIS test — forcing whoever
    introduced it to add a classified (``"defect"`` / ``"false_positive"``)
    entry before the PR is green, the same declared-baseline idiom this
    repo already uses for exactly this problem (``mypy_ratchet.py``,
    ``flat_tests_ratchet.py``)."""
    offenders = offending_files(_SRC_DIR)
    measured = {str(p.relative_to(_SRC_DIR.parents[1])) for p, _ in offenders}
    declared = set(load_declared_baseline().keys())
    new = measured - declared
    assert not new, (
        f"undeclared offender(s): {new} — add an entry to "
        f"{_BASELINE_PATH.name} classifying each as \"defect\" (a real "
        "hazard, tracked for its own fix PR) or \"false_positive\" (the "
        "gate is wrong about this one), with a one-line note explaining "
        "which and why"
    )
    # A declared entry that no longer measures (fixed, or the module
    # changed shape) is allowed to silently drop — same as mypy_ratchet's
    # own "a fix just stops appearing" behavior; only NEW, undeclared
    # growth needs a person to act.


def test_load_declared_baseline_round_trips_a_classified_entry(tmp_path: Path) -> None:
    """Tier 1: ``load_declared_baseline`` reads back exactly what a real
    declaration writes — the ``type``/``note`` fields the ratchet's own
    failure message tells a person to add."""
    fixture = tmp_path / "baseline.json"
    fixture.write_text(
        json.dumps({"src/reyn/example.py": {"type": "false_positive", "note": "reason"}}),
        encoding="utf-8",
    )
    declared = load_declared_baseline(fixture)
    assert declared == {"src/reyn/example.py": {"type": "false_positive", "note": "reason"}}


def test_an_undeclared_offender_is_what_the_ratchet_exists_to_catch(tmp_path: Path) -> None:
    """Tier 1: the exact regression lead-coder's review found in the first
    draft — a NEW offender not present in the declared baseline must be
    distinguishable from "the same accepted population as before", via a
    plain set difference against the real baseline file's own declared
    keys (the same check ``test_the_real_repo_tree_matches_the_declared_
    baseline_exactly`` runs against the real tree)."""
    (tmp_path / "new_offender.py").write_text(
        "import asyncio\n"
        "from reyn.runtime.tracked_tasks import TrackedTaskSet\n"
        "\n"
        "class NewOwner:\n"
        "    def __init__(self) -> None:\n"
        "        self._t = TrackedTaskSet()\n"
        "\n"
        "    def go(self) -> None:\n"
        "        asyncio.create_task(self._work())\n",
        encoding="utf-8",
    )
    offenders = offending_files(tmp_path)
    measured = {str(p.relative_to(tmp_path)) for p, _ in offenders}
    declared = set(load_declared_baseline(_BASELINE_PATH).keys())  # the real repo's own baseline
    assert measured - declared == {"new_offender.py"}, (
        "a synthetic new offender, absent from the real declared baseline, "
        "must show up as undeclared growth"
    )
