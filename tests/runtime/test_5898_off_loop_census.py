"""Tier 2: #5898 — every stage on the LLM request path whose cost is
proportional to the history's byte count (token counting, JSON
serialisation, hashing) runs OFF the event loop, in ``asyncio.to_thread``.

Architect (#5898 ruling): "判別子は site でなく性質 — 要求 1 回の cost が
history の byte 数に比例する段は loop 上で走らせない". The property is not
observable as a duration (the ruling's own GIL caveat: ``to_thread`` cannot
promise the loop advances while C/Rust holds the GIL, so the acceptance is
"a response returns", never a wall-clock), so this gate reads the CODE: a
census-type gate (same shape as #5887's ``persist_as`` gate) over the named
call sites, each of which must reach its O(history bytes) callee only
through ``asyncio.to_thread`` — as the callable handed to it, or inside a
lambda handed to it. A callee named in the table that is CALLED directly
anywhere in its file is a violation; a table entry that finds fewer
``to_thread`` sites than declared is a violation too (a site silently
removed reads as "covered" otherwise — Q4).

Sites deliberately NOT gated here, and why (each is listed in the PR body's
census with its own reason): ``compaction/engine.py``'s ``json.dumps`` for
``compaction_started.input_chars`` and ``session.py``'s ``/compact``
estimate — both callees are generic names (``json.dumps``, ``_est``) with
legitimate small-input uses in the same file, so a name-keyed gate cannot
tell the O(history) call from the O(summary) one; both are wrapped in a
``to_thread`` lambda and read in review instead.

Plus the ONE runtime witness the gate cannot give: a ``MediaStore`` write
submitted from a worker thread (what ``RouterLoop.feedback`` now is) still
goes through the store's ``DurabilityWorker`` on the loop — not an inline
write in the thread — so the write-ahead barrier, FIFO order and the
``durability_failed`` latch are the same as on-loop.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from reyn.data.workspace.media_store import MediaStore
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src" / "reyn"

#: file → {callee name: how many ``to_thread`` sites hand it over}. Names are
#: the attribute/function name at the call site (``self._x`` → ``_x``).
_OFF_LOOP_SITES: "dict[str, dict[str, int]]" = {
    "runtime/router_loop.py": {"format_feedback": 2},
    "llm/llm.py": {"_message_chars": 1, "stream_chunk_builder": 1},
    "runtime/services/router_loop_driver.py": {
        "decompose_history_for_retry": 2,
        "_spill_batch_within_face": 1,
        "build_history": 1,  # #4995's incumbent — the shape every other site copies
    },
    "runtime/services/compaction_controller.py": {
        "_history_from_disk": 1,
        "_measure_and_select": 1,
        "shrink_pool_after_overflow": 1,  # the retry ladder's spill batch (+ sync spill_fn inside)
    },
    "services/compaction/engine.py": {
        "shrink_pool_after_overflow": 1,
    },
}


def _callee_name(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_to_thread(call: ast.Call) -> bool:
    return _callee_name(call.func) == "to_thread"


def _census(path: Path, callee: str) -> "tuple[int, list[int]]":
    """(to_thread sites handing *callee* over, line numbers of direct calls
    to *callee* made ON THE LOOP — i.e. whose nearest enclosing function
    is an ``async def`` and which are not under a to_thread lambda).

    A direct call inside a plain ``def`` is the body of whatever worker
    the loop handed that function to — it is on the loop only if some
    ``async def`` calls THAT function directly, which this same census
    catches at that call (e.g. ``_spill_batch_for_retry`` calls
    ``_spill_batch_within_face`` synchronously; the loop reaches it only
    through ``shrink_pool_after_overflow``, gated in its two async
    callers)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: "dict[ast.AST, ast.AST]" = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def _under_to_thread_lambda(node: ast.AST) -> bool:
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Call) and _is_to_thread(cur) and cur.args:
                return isinstance(cur.args[0], ast.Lambda)
        return False

    def _on_the_loop(node: ast.AST) -> bool:
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, ast.Lambda | ast.FunctionDef):
                return False
            if isinstance(cur, ast.AsyncFunctionDef):
                return True
        return False

    handed_over = 0
    direct_calls: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_to_thread(node) and node.args:
            if _callee_name(node.args[0]) == callee:
                handed_over += 1
        elif isinstance(node, ast.Call) and _callee_name(node.func) == callee:
            if _on_the_loop(node) and not _under_to_thread_lambda(node):
                direct_calls.append(node.lineno)
    return handed_over, direct_calls


def test_every_history_proportional_stage_is_handed_to_to_thread() -> None:
    """Tier 2: the census — each named callee reaches the loop only via
    ``asyncio.to_thread`` (as its callable or inside its lambda), and the
    declared number of sites is actually present. Non-empty guard:
    fewer than 9 sites checked means the table itself broke, never a
    green. Strip-falsify: unwrap any one site (call the callee directly)
    → its line is reported → red; delete a site → count short → red."""
    checked = 0
    problems: list[str] = []
    for rel, table in _OFF_LOOP_SITES.items():
        path = _SRC / rel
        for callee, expected in table.items():
            handed_over, direct = _census(path, callee)
            checked += handed_over
            if handed_over != expected:
                problems.append(f"{rel}: {callee} handed to to_thread {handed_over}x, expected {expected}")
            if direct:
                problems.append(f"{rel}: {callee} CALLED on the loop at line(s) {direct}")
    assert not problems, "\n".join(problems)
    assert checked >= 11, f"census gate saw only {checked} sites — the table is broken, not green"


@pytest.mark.asyncio
async def test_a_bound_worker_accepts_a_job_from_another_thread_and_runs_it_on_its_loop() -> None:
    """Tier 2: the seam ``RouterLoop.feedback``'s off-loop ``save_tool_
    result`` now relies on — ``DurabilityWorker.submit_threadsafe``: once
    bound to a running loop (``bind_to_running_loop``, what ``MediaStore.
    flush`` does on the barrier every turn takes first), a job handed in
    from a worker thread is accepted (True) and runs ON THE LOOP THREAD
    when the worker drains — FIFO with everything else queued, same retry
    policy and latch as an on-loop submit. An UNBOUND worker refuses
    (False), and the caller's own no-loop path takes over (``MediaStore.
    _submit_write_or_inline``'s inline ``asyncio.run``, unchanged for
    scripts). Strip-falsify: make ``submit_threadsafe`` always return
    False → the accepted-and-ran assertions go red."""
    import threading

    from reyn.core.events.durability_worker import DurabilityWorker

    ran_on: list[str] = []

    async def _job() -> None:
        ran_on.append(threading.current_thread().name)

    unbound = DurabilityWorker()
    assert await asyncio.to_thread(unbound.submit_threadsafe, _job) is False
    assert ran_on == []

    worker = DurabilityWorker()
    worker.bind_to_running_loop()
    accepted = await asyncio.to_thread(worker.submit_threadsafe, _job)
    assert accepted is True
    await worker.flush()
    assert ran_on == [threading.main_thread().name], "the job ran on the loop's own thread, not the caller's"


@pytest.mark.asyncio
async def test_a_tool_result_saved_from_a_worker_thread_is_durable_after_the_barrier(tmp_path: Path) -> None:
    """Tier 2: end to end through the real store — ``save_tool_result``
    from a worker thread (the shape ``RouterLoop.feedback`` now has under
    ``to_thread``) with the worker bound by the turn's own ``flush``
    barrier: the ref is minted, the body is on disk after the next
    ``flush()``, and the store's durability latch is untouched."""
    store = MediaStore(project_root=tmp_path, agent_name="a", session_id="s")
    await store.flush()  # the barrier every turn takes first: binds the worker to this loop

    block = await asyncio.to_thread(store.save_tool_result, "body from a worker thread", spilled=False)
    await store.flush()

    assert (tmp_path / block["path"]).read_text(encoding="utf-8") == "body from a worker thread"
    assert store.is_unspilled_file(block["path"])
    assert store.durability_failed is False
