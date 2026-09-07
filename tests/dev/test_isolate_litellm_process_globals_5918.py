"""Tier 2: #5918 (architect ruling, owner-escalated to a CLASS fix) —
``tests/conftest.py``'s ``_isolate_litellm_process_globals`` autouse fixture
actually isolates litellm's process-global mutable state between tests.

Owner-hit background: an unrelated PR's CI (and eventually `main` itself)
went red on ``test_chat_max_retries_unaffected_by_embed_global_mutation``
under `-n auto` parallel load, with no connection to that PR's own diff.
Root cause (architect, litellm 1.100.0 source): the OpenAI-client cache's
key is suffixed with a memory address (``id(event_loop)``); CPython reuses
freed addresses, so an unrelated EARLIER test's already-GC'd loop can
collide with a LATER test's brand-new one, producing a stale cache hit that
silently skips client construction. The general fix is NOT "clear the cache
in this one test" (an instance) but "isolate litellm's process globals for
every test" (the class) — this repo already has ~10 fixtures of exactly
this shape for other third-party/process-global state.

Real, isolated inner pytest sessions throughout (``pytester``'s own
subprocess seam, the SAME technique
``test_3671_stall_trace_startup_wiring.py``'s own witness uses for a
different conftest fixture) — never a mock of pytest's own fixture
machinery, and never a mock of litellm: the actual ``tests/conftest.py``
fixture, the actual ``litellm`` module.

Per lead-coder's explicit instruction: "#5918 が緑になった" (the target
test passing) is NOT used as a witness anywhere in this file — it is
probabilistic (an id() collision may simply not occur in a given run) and
would prove nothing about whether isolation is actually wired. Every test
below observes the isolation mechanism directly instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

#: Same shape as test_3671_stall_trace_startup_wiring.py's own
#: `_INNER_CONFTEST`: puts the repo root + src on sys.path (a pytester
#: subprocess starts with neither this outer session's pyproject.toml
#: pythonpath favour nor its installed editable package necessarily
#: resolving the same way) then imports the REAL autouse fixture from the
#: REAL tests/conftest.py — pytest activates any @pytest.fixture-decorated
#: callable present in a conftest module's namespace, imported or not.
_INNER_CONFTEST = """
import sys
sys.path.insert(0, {repo_root!r})
sys.path.insert(0, {src_root!r})
from tests.conftest import _isolate_litellm_process_globals  # noqa: F401
"""

#: Acceptance ① (architect, #5918) + acceptance ② combined into one inner
#: session: test_a pollutes both litellm globals this fixture owns (the
#: client cache, and DEFAULT_MAX_RETRIES -- the exact permanent write
#: `_aembedding_bounded` makes in production) WITHOUT restoring either;
#: test_b checks both are clean, in a SEPARATE test in the SAME inner
#: session (so the fixture's per-test before/after hooks are the only
#: thing that could have cleaned up between them -- nothing else runs).
_INNER_TEST_POLLUTION = """
import litellm

_DEFAULT_MAX_RETRIES_BEFORE_POLLUTION = []
_SUCCESS_CALLBACK_LEN_BEFORE_POLLUTION = []

def test_a_pollutes_the_client_cache_default_max_retries_and_a_callback_list():
    _DEFAULT_MAX_RETRIES_BEFORE_POLLUTION.append(litellm.DEFAULT_MAX_RETRIES)
    _SUCCESS_CALLBACK_LEN_BEFORE_POLLUTION.append(len(litellm.success_callback))
    litellm.in_memory_llm_clients_cache.set_cache("poison-key", "a-stale-client-stand-in")
    assert litellm.in_memory_llm_clients_cache.get_cache("poison-key") is not None  # sanity
    litellm.DEFAULT_MAX_RETRIES = 0  # the exact production write _aembedding_bounded makes
    # #5953 BLOCKING (lead-coder, measured): an IN-PLACE mutation, not a
    # rebind -- a bare `getattr`-snapshot-and-`setattr`-restore saves a
    # REFERENCE to this SAME list object, so .append() here would survive
    # the restore untouched (only a rebind, `litellm.success_callback =
    # [...]`, would have been undone by that shape). This is the realistic
    # call a test makes (`litellm.success_callback.append(...)`), not a
    # synthetic rebind picked to make the old code look correct.
    litellm.success_callback.append("poison")

def test_b_starts_with_a_clean_cache_original_default_and_untouched_callback_list():
    assert _DEFAULT_MAX_RETRIES_BEFORE_POLLUTION, "setup: test_a must run first, in this same process"
    assert litellm.in_memory_llm_clients_cache.get_cache("poison-key") is None, (
        "the isolation fixture must clear the client cache BEFORE this test ran -- "
        "a leftover entry here is exactly #5918's own mechanism (a stale cached "
        "client silently reused by an unrelated later test)"
    )
    assert litellm.DEFAULT_MAX_RETRIES == _DEFAULT_MAX_RETRIES_BEFORE_POLLUTION[-1], (
        f"DEFAULT_MAX_RETRIES leaked from test_a (0) into test_b -- the isolation "
        f"fixture must restore it to what it was BEFORE test_a ran "
        f"({_DEFAULT_MAX_RETRIES_BEFORE_POLLUTION[-1]!r}), not leave production's "
        f"own permanent write visible to an unrelated later test"
    )
    assert "poison" not in litellm.success_callback, (
        "test_a's litellm.success_callback.append('poison') leaked into test_b -- "
        "the isolation fixture must snapshot a COPY of a mutable attribute, not a "
        "reference to litellm's own list object (#5953 BLOCKING)"
    )
    assert len(litellm.success_callback) == _SUCCESS_CALLBACK_LEN_BEFORE_POLLUTION[-1], (
        "litellm.success_callback's length changed across tests -- restore did not "
        "return it to test_a's own starting state"
    )
"""


def test_isolation_clears_the_client_cache_and_restores_default_max_retries(
    pytester: pytest.Pytester,
) -> None:
    """Tier 2: #5918 acceptance ① (client-cache isolation) + ② (production's
    permanent DEFAULT_MAX_RETRIES write does not leak into a later,
    unrelated test) — driven together since both are the SAME fixture's
    before/after halves, in ONE real isolated inner pytest session so
    ordering is guaranteed (pytester's own subprocess, not `-n auto` --
    the outer suite's own parallelism is exactly what this fixture exists
    to make irrelevant, so the WITNESS must not depend on it either).

    Strip (①): comment out this fixture's `cache.flush_cache()` call in
    `tests/conftest.py` -- `test_b` goes red (`get_cache("poison-key")`
    still returns the stand-in).
    Strip (②): comment out the `for name, value in saved.items():
    setattr(...)` restore loop -- `test_b` goes red (`DEFAULT_MAX_RETRIES`
    stays `0`).
    """
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    src_root = str(repo_root / "src")

    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(repo_root), src_root=src_root))
    pytester.makepyfile(test_inner=_INNER_TEST_POLLUTION)

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=2)


#: Acceptance ③ (architect, #5918): a test that never imports litellm must
#: not have this fixture import it either -- checked in its OWN, separate
#: inner session (a fresh subprocess with nothing else having imported
#: litellm at collection time) so `sys.modules` genuinely reflects only
#: what THIS test file itself caused.
_INNER_TEST_NO_OP = """
import sys

def test_litellm_was_never_imported_by_the_isolation_fixture():
    assert "litellm" not in sys.modules, (
        "the autouse isolation fixture imported litellm even though this "
        "test never referenced it -- the fixture must no-op (not even "
        "`import litellm`) when litellm is not already in sys.modules"
    )
"""


def test_isolation_is_a_no_op_when_litellm_was_never_imported(
    pytester: pytest.Pytester,
) -> None:
    """Tier 2: #5918 acceptance ③ -- the #3671 discriminator
    (`"litellm" not in sys.modules`) this repo's own lazy-load contract
    (`tests/llm/test_litellm_lazy_load.py` et al.) already relies on. A
    SEPARATE inner session from the pollution test above: that one
    necessarily imports litellm itself, so combining the two would make
    this check trivially fail regardless of the fixture's own behavior.

    Strip: replace the fixture's `if "litellm" not in sys.modules: yield;
    return` guard with an unconditional `import litellm` -- this goes red.
    """
    import reyn

    repo_root = Path(reyn.__file__).resolve().parents[2]
    src_root = str(repo_root / "src")

    pytester.makeconftest(_INNER_CONFTEST.format(repo_root=str(repo_root), src_root=src_root))
    pytester.makepyfile(test_inner=_INNER_TEST_NO_OP)

    result = pytester.runpytest_subprocess("test_inner.py")
    result.assert_outcomes(passed=1)
