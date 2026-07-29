"""Tier 2b: `run_async`'s client-close step is scoped to clients created
during its own call — not litellm's entire process-wide async-client cache
(#3434).
"""
import asyncio

from reyn.llm.llm import run_async


def test_run_async_does_not_close_a_client_created_outside_its_own_call() -> None:
    """Tier 2b: an unrelated, LLM-free `run_async` call must not close a
    litellm async client another (already-completed) call created and left
    open.

    Regression guard for #3434: litellm's own `close_litellm_async_clients()`
    iterates its ENTIRE process-wide client cache, unconditionally — not
    just entries the calling `run_async` invocation created. `LLMClientCache`
    never evicts on close (its own docstring: an in-flight request may still
    hold the client), so under `pytest-xdist` any later LLM-free `run_async`
    call (e.g. an mcp.py CLI command test) in the same worker process closed
    every OTHER test's still-open, still-cached litellm client — including
    ones for providers that test never touched. A real (unmocked) network
    LLM call running afterward in that worker then failed with "Cannot send
    a request, as the client has been closed", with the specific failing
    test varying run to run because it depends on xdist worker assignment
    and intra-worker test order, not on any one test's own defect.

    Uses litellm's real async-client cache + real aiohttp objects (no
    mocks — `unittest.mock` is forbidden by testing policy). Asserts on the
    client session's own public `closed` attribute, not any reyn-private
    state.
    """
    async def _create_client_outside_run_async():
        # Same call litellm's own vertex_ai/gemini `make_call` makes
        # internally (`get_async_httpx_client(llm_provider=VERTEX_AI)`,
        # no params) — the exact cache key a real streaming LLM call hits.
        from litellm.llms.custom_httpx.http_handler import get_async_httpx_client

        handler = get_async_httpx_client(llm_provider="vertex_ai")
        return handler.client._transport._get_valid_client_session()

    # Simulates a prior, already-finished test/call that created a real
    # litellm async client on its OWN event loop (not through `run_async`)
    # and left it open — exactly what a real (unmocked) LLM call does.
    session = asyncio.run(_create_client_outside_run_async())
    assert session.closed is False

    async def _unrelated_llm_free_work() -> str:
        return "done"

    # Simulates a later, totally unrelated `run_async` call in the same
    # worker process (e.g. an LLM-free mcp.py CLI command).
    assert run_async(_unrelated_llm_free_work()) == "done"

    assert session.closed is False, (
        "run_async closed a litellm async client it did not create — "
        "this is the #3434 shared-cache poisoning bug"
    )
