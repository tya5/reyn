"""Tier 2: #6034 — ``tests/conftest.py``'s ``_isolate_litellm_process_
globals`` autouse fixture (#5918) closes every cached litellm async
client BEFORE dropping the cache's own references, instead of only
dropping them.

Root cause (measured, primary evidence — PR #6033's own investigation of
a #6031 CI red): ``cache.flush_cache()`` alone clears the cache dict's
KEYS but never calls ``.close()``/``.aclose()`` on the client objects
those keys pointed at. A prior test's real litellm call left a still-open
``aiohttp.ClientSession`` cached; the NEXT test's fixture-before-phase
dropped the only reference to it, orphaning the session into garbage
collection at some LATER, unrelated point — surfaced as ``asyncio``'s
own "Unclosed client session" finalizer warning landing in a completely
different test's ``caplog`` window under ``-n auto`` (#6033).

Not the same root as #4365 (litellm's own OFFICIAL close routine misses
a bare ``AsyncOpenAI``-shaped cached client — a confirmed upstream gap,
owner ruling: reyn does not grow a branch of its own to cover it). This
fix calls that SAME official routine, not a hand-rolled one — a real
``BaseLLMAIOHTTPHandler``-wrapped session (the shape that routine's own
branch ① DOES correctly close) is exactly what #6033's own primary
evidence showed leaking, so this is the right shape to witness.

Real, isolated inner pytest sessions throughout (``pytester``'s own
subprocess seam, the same technique ``test_isolate_litellm_process_
globals_5918.py`` already uses for the sibling fixture behavior) — never
a mock of litellm or of pytest's own fixture machinery: the actual
``tests/conftest.py`` fixture, the actual ``litellm`` module, a real
``aiohttp.ClientSession`` wrapped in litellm's own real
``BaseLLMAIOHTTPHandler``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

#: Same shape as test_isolate_litellm_process_globals_5918.py's own
#: `_INNER_CONFTEST`.
_INNER_CONFTEST = """
import sys
sys.path.insert(0, {repo_root!r})
sys.path.insert(0, {src_root!r})
from tests.conftest import _isolate_litellm_process_globals  # noqa: F401
"""

#: test_a caches a REAL, OPEN aiohttp.ClientSession wrapped in litellm's
#: own BaseLLMAIOHTTPHandler (the exact shape close_litellm_async_clients'
#: branch ① closes) and leaves it open -- never calling .close() itself,
#: simulating the real shape (a test that drove a real litellm HTTP call
#: and never explicitly drained it). test_b runs NEXT in the SAME inner
#: session, so the fixture's own before-phase (between test_a and test_b)
#: is the only thing that could have closed it.
_INNER_TEST_CLOSE_BEFORE_FLUSH = """
import asyncio
import aiohttp
import litellm
from litellm.llms.custom_httpx.aiohttp_handler import BaseLLMAIOHTTPHandler

_HANDLER_BOX = []

def test_a_caches_an_open_aiohttp_backed_client_and_never_closes_it():
    async def _make_open_session():
        return aiohttp.ClientSession()

    session = asyncio.run(_make_open_session())
    assert not session.closed  # sanity: genuinely open going in
    # NOT passed via the constructor's own `client_session=` kwarg -- that
    # marks `_owns_session=False` (the constructor's own convention for
    # "caller still owns this, close() must not touch it"), which would
    # make close() correctly skip it and defeat this witness for the
    # wrong reason. Assigning after construction (still `_owns_session`
    # defaulting True from the no-arg constructor) matches how litellm's
    # OWN real call sites populate a handler it does own.
    handler = BaseLLMAIOHTTPHandler()
    handler.client_session = session
    litellm.in_memory_llm_clients_cache.set_cache("6034-poison-key", handler)
    _HANDLER_BOX.append(handler)
    # deliberately no close() here -- #6034's own real-world shape.

def test_b_the_cached_client_was_closed_before_the_cache_was_flushed():
    assert _HANDLER_BOX, "setup: test_a must run first, in this same process"
    handler = _HANDLER_BOX[0]
    assert handler.client_session.closed, (
        "the isolation fixture dropped test_a's cached client WITHOUT "
        "closing its underlying aiohttp.ClientSession first -- this is "
        "#6034 itself: a still-open session orphaned into GC at some "
        "later, unrelated point"
    )
    # #5918's own witness must still hold too -- the cache is still
    # flushed (not merely closed-and-left-cached).
    assert litellm.in_memory_llm_clients_cache.get_cache("6034-poison-key") is None, (
        "closing before flush must not skip the flush itself -- #5918's "
        "own isolation (a stale cached client reused by an unrelated "
        "later test) would regress"
    )
"""


def test_cached_client_is_closed_before_the_next_tests_cache_flush(
    pytester: pytest.Pytester,
) -> None:
    """Tier 2: #6034 acceptance ① — a cached, still-open litellm async
    client is closed by the fixture's before-phase, before the cache is
    flushed for the next test. Combined with #5918's own flush witness
    in the same inner session (①+② together, ordering-guaranteed via
    pytester's own subprocess — never `-n auto`, the exact parallelism
    this fixture exists to make irrelevant).

    Strip: comment out the
    `_close_cached_litellm_clients_before_flush(litellm)` call in
    `tests/conftest.py`, leaving only `cache.flush_cache()` — `test_b`
    goes red (`handler.client_session.closed` is `False`, performed
    during review).
    """
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    src_root = str(repo_root / "src")

    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(repo_root), src_root=src_root))
    pytester.makepyfile(test_inner=_INNER_TEST_CLOSE_BEFORE_FLUSH)

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=2)
