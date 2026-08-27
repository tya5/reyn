"""Tier 2: #5267 (Family A) — the task-funnel-bypass gate itself.

Real filesystem fixtures throughout (a real ``tmp_path`` tree of ``.py``
files) — the function under test reads real file content and parses real
ASTs, so faking the filesystem would test nothing real. Mirrors this
repo's own established convention for a static gate's own test file
(``tests/scripts/test_check_cancel_swallow_4988.py``).
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_task_funnel_bypass import in_scope_files, offending_files


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


def test_the_real_repo_tree_has_exactly_the_one_disclosed_pre_existing_hit() -> None:
    """Tier 2: the gate's own starting population against the real tree —
    verified, not assumed (matching the sibling gates' own "run it before
    shipping it" discipline). This PR is the family remedy (the gate)
    only; #5267's own comment thread explicitly scoped the 4 real defect
    sites (including this one, ``session.py``'s ``_turn_owner_task``) to
    their OWN separate PRs, not this one — so this gate's baseline is
    NOT zero, and pinning it to the one disclosed pre-existing hit (never
    a bare "the tree is clean") is what makes a genuinely NEW regression
    (a 2nd hit appearing) visible as a diff against this test, rather than
    silently absorbed into an already-nonzero warn count."""
    from scripts.check_task_funnel_bypass import _SRC_DIR

    offenders = offending_files(_SRC_DIR)
    assert [str(p.relative_to(_SRC_DIR.parents[1])) for p, _ in offenders] == [
        "src/reyn/runtime/session.py",
    ], (
        f"population changed: {offenders} — if this is a NEW module bypassing "
        "the funnel, fix it (route through TrackedTaskSet.spawn()); if it is "
        "the disclosed session.py hit moving line numbers, update this pin"
    )
