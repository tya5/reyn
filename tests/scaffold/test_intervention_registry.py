# scaffold: triggered_by="PR-refactor-session-1 wave 1 extracted
#                          InterventionRegistry out of ChatSession
#                          (commit 41ec4cb)"
# scaffold: removed_by="follow-up PR that adds Tier 2 / Tier 3 coverage for
#                       intervention announce / deliver flows via the public
#                       ChatSession surface, at which point the
#                       implementation-level coverage here is redundant"
"""Scaffolding tests for InterventionRegistry (wave 1C extraction).

These tests reach directly into private state (`_active`, `_order`) to
construct preconditions and to assert post-conditions. Per the testing
policy they qualify as Tier 4 in the steady state — they exist purely to
give the extraction a fast-feedback safety net during refactor and will be
removed once the public ChatSession surface has enough Tier 2 invariants
and Tier 3 replay coverage to make this implementation-level pinning
unnecessary.
"""
from __future__ import annotations

import asyncio
import pytest

from reyn.chat.services.intervention_registry import InterventionRegistry
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _iv(*, kind: str = "ask_user", prompt: str = "Q?", run_id: str | None = None,
        choices: list[InterventionChoice] | None = None) -> UserIntervention:
    """Create a UserIntervention with a real asyncio.Future."""
    loop = asyncio.get_event_loop()
    iv = UserIntervention(
        kind=kind,
        prompt=prompt,
        run_id=run_id,
        choices=choices or [],
    )
    # Ensure future is tied to the running loop (pytest-asyncio provides one).
    iv.future = loop.create_future()
    return iv


def _registry(announced: list | None = None) -> InterventionRegistry:
    """Build a registry whose on_announce appends to *announced*."""
    if announced is None:
        announced = []

    async def _announce(iv: UserIntervention) -> None:
        announced.append(iv)

    return InterventionRegistry(on_announce=_announce)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_adds_to_active_and_order(self):
        reg = _registry()
        iv = _iv()

        async def _resolve():
            await asyncio.sleep(0)
            iv.future.set_result(InterventionAnswer(text="hi"))

        asyncio.ensure_future(_resolve())
        await reg.dispatch(iv)

        # After dispatch resolves the entry is cleaned up.
        assert reg.is_empty()

    @pytest.mark.asyncio
    async def test_dispatch_calls_on_announce_for_first_intervention(self):
        announced: list[UserIntervention] = []
        reg = _registry(announced)
        iv = _iv()

        async def _resolve():
            await asyncio.sleep(0)
            iv.future.set_result(InterventionAnswer(text="ok"))

        asyncio.ensure_future(_resolve())
        await reg.dispatch(iv)

        assert len(announced) == 1
        assert announced[0] is iv

    @pytest.mark.asyncio
    async def test_dispatch_queued_intervention_not_announced_immediately(self):
        """Second dispatch while first is pending should NOT call on_announce."""
        announced: list[UserIntervention] = []
        reg = _registry(announced)

        iv1 = _iv(prompt="first")
        iv2 = _iv(prompt="second")

        async def _run():
            await asyncio.gather(
                reg.dispatch(iv1),
                reg.dispatch(iv2),
            )

        async def _resolve_both():
            await asyncio.sleep(0)
            iv1.future.set_result(InterventionAnswer(text="a"))
            await asyncio.sleep(0)
            iv2.future.set_result(InterventionAnswer(text="b"))

        asyncio.ensure_future(_resolve_both())
        await _run()

        # iv1 announced immediately; iv2 announced after iv1 resolved (via
        # _maybe_announce_next).
        assert len(announced) == 2
        assert announced[0] is iv1
        assert announced[1] is iv2

    @pytest.mark.asyncio
    async def test_dispatch_cancelled_returns_empty_answer(self):
        reg = _registry()
        iv = _iv()

        async def _cancel():
            await asyncio.sleep(0)
            iv.future.cancel()

        asyncio.ensure_future(_cancel())
        result = await reg.dispatch(iv)

        assert result.text == ""
        assert result.choice_id is None

    @pytest.mark.asyncio
    async def test_dispatch_cleans_up_on_exit(self):
        reg = _registry()
        iv = _iv()

        async def _resolve():
            await asyncio.sleep(0)
            iv.future.set_result(InterventionAnswer(text="done"))

        asyncio.ensure_future(_resolve())
        await reg.dispatch(iv)

        assert iv.id not in reg._active
        assert iv.id not in reg._order


class TestDeliverAnswer:
    @pytest.mark.asyncio
    async def test_free_text_resolves_future_and_returns_true(self):
        reg = _registry()
        iv = _iv()
        reg._active[iv.id] = iv
        reg._order.append(iv.id)

        result = await reg.deliver_answer(iv, "hello")

        assert result is True
        assert iv.future.done()
        assert iv.future.result().text == "hello"
        assert iv.id not in reg._active

    @pytest.mark.asyncio
    async def test_choices_hotkey_match_resolves_with_choice_id(self):
        choices = [
            InterventionChoice(id="yes", label="[Y]es", hotkey="Y"),
            InterventionChoice(id="no", label="[N]o", hotkey="N"),
        ]
        reg = _registry()
        iv = _iv(choices=choices)
        reg._active[iv.id] = iv
        reg._order.append(iv.id)

        result = await reg.deliver_answer(iv, "Y")

        assert result is True
        answer = iv.future.result()
        assert answer.choice_id == "yes"
        assert answer.text == "Y"

    @pytest.mark.asyncio
    async def test_choices_no_match_returns_false_active_remains(self):
        choices = [
            InterventionChoice(id="yes", label="[Y]es", hotkey="Y"),
        ]
        reg = _registry()
        iv = _iv(choices=choices)
        reg._active[iv.id] = iv
        reg._order.append(iv.id)

        result = await reg.deliver_answer(iv, "nope")

        assert result is False
        assert not iv.future.done()
        assert iv.id in reg._active

    @pytest.mark.asyncio
    async def test_already_done_future_returns_false(self):
        reg = _registry()
        iv = _iv()
        iv.future.set_result(InterventionAnswer(text="already"))

        result = await reg.deliver_answer(iv, "late")

        assert result is False


class TestMaybeAnswerHead:
    @pytest.mark.asyncio
    async def test_empty_registry_returns_false(self):
        reg = _registry()
        result = await reg.maybe_answer_head("hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_delivers_to_head_returns_true(self):
        reg = _registry()
        iv = _iv()
        reg._active[iv.id] = iv
        reg._order.append(iv.id)

        result = await reg.maybe_answer_head("answer")

        assert result is True
        assert iv.future.result().text == "answer"

    @pytest.mark.asyncio
    async def test_evicts_stale_head_before_delivery(self):
        reg = _registry()
        stale = _iv(prompt="stale")
        stale.future.set_result(InterventionAnswer(text="old"))  # already resolved
        good = _iv(prompt="good")

        reg._active[stale.id] = stale
        reg._order.append(stale.id)
        reg._active[good.id] = good
        reg._order.append(good.id)

        result = await reg.maybe_answer_head("fresh")

        assert result is True
        assert good.future.result().text == "fresh"


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_cancels_future_and_removes(self):
        reg = _registry()
        iv = _iv()
        reg._active[iv.id] = iv
        reg._order.append(iv.id)

        cancelled = reg.cancel(iv.id)

        assert cancelled is True
        assert iv.future.cancelled()
        assert iv.id not in reg._active
        assert iv.id not in reg._order

    @pytest.mark.asyncio
    async def test_cancel_unknown_id_returns_false(self):
        reg = _registry()
        result = reg.cancel("nonexistent")
        assert result is False


class TestDropForRun:
    @pytest.mark.asyncio
    async def test_drops_only_matching_run_id(self):
        reg = _registry()
        iv_a = _iv(run_id="run-A")
        iv_b = _iv(run_id="run-B")
        iv_c = _iv(run_id="run-A")

        for iv in (iv_a, iv_b, iv_c):
            reg._active[iv.id] = iv
            reg._order.append(iv.id)

        dropped = reg.drop_for_run("run-A")

        assert set(dropped) == {iv_a.id, iv_c.id}
        assert iv_a.future.cancelled()
        assert iv_c.future.cancelled()
        assert not iv_b.future.done()
        assert iv_b.id in reg._active

    @pytest.mark.asyncio
    async def test_drop_for_run_none_returns_empty(self):
        reg = _registry()
        result = reg.drop_for_run(None)
        assert result == []


class TestResolveIdPrefix:
    def test_unique_prefix_returns_id(self):
        reg = _registry()
        iv = _iv()
        reg._active[iv.id] = iv

        # Use the first 8 chars of the id as prefix
        prefix = iv.id[:8]
        unique, candidates = reg.resolve_id_prefix(prefix)

        assert unique == iv.id
        assert candidates == [iv.id]

    def test_ambiguous_prefix_returns_none_and_candidates(self):
        reg = _registry()
        # Create two interventions whose ids share a prefix
        iv1 = _iv()
        iv2 = _iv()
        # Force a common prefix by injecting custom ids
        iv1.id = "aaaa1111"
        iv2.id = "aaaa2222"
        reg._active[iv1.id] = iv1
        reg._active[iv2.id] = iv2

        unique, candidates = reg.resolve_id_prefix("aaaa")

        assert unique is None
        assert set(candidates) == {iv1.id, iv2.id}

    def test_empty_prefix_returns_none_empty(self):
        reg = _registry()
        unique, candidates = reg.resolve_id_prefix("")
        assert unique is None
        assert candidates == []

    def test_unknown_prefix_returns_none_empty(self):
        reg = _registry()
        unique, candidates = reg.resolve_id_prefix("zzzzzzzzz")
        assert unique is None
        assert candidates == []


class TestReadOnlyQueries:
    def test_list_active_head_is_empty_queued_count(self):
        reg = _registry()
        assert reg.is_empty()
        assert reg.queued_count() == 0
        assert reg.list_active() == []
        assert reg.head() is None

    def test_queries_reflect_active_interventions(self):
        reg = _registry()
        iv1 = _iv(prompt="first")
        iv2 = _iv(prompt="second")
        reg._active[iv1.id] = iv1
        reg._order.append(iv1.id)
        reg._active[iv2.id] = iv2
        reg._order.append(iv2.id)

        assert not reg.is_empty()
        assert reg.queued_count() == 2
        assert reg.list_active() == [iv1, iv2]
        assert reg.head() is iv1
        assert reg.get(iv1.id) is iv1
        assert reg.get("nosuchid") is None
