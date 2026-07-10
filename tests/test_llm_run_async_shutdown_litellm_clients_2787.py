"""Tier 2b: run_async's shutdown path closes litellm's cached async HTTP
client(s), preventing the "Unclosed client session" / "Unclosed connector"
GC-time warnings (surfaced as "Unhandled exception in event loop: /
Exception None") reported in #2787.

Uses litellm's real async-client cache + real aiohttp objects (no mocks —
`unittest.mock` is forbidden by testing policy). Asserts on the client
session's own public `closed` attribute (aiohttp's documented public
surface for this exact check), not any reyn-private state.
"""
from reyn.llm.llm import run_async


def test_run_async_closes_cached_litellm_async_client() -> None:
    """Tier 2b: a litellm async client created during a `run_async` call is
    closed by the time `run_async` returns, not left for `__del__`/GC.

    Regression guard for #2787 (aiohttp `Unclosed client session` /
    `Unclosed connector` warnings firing from `__del__` at shutdown,
    surfacing as "Unhandled exception in event loop: Exception None").
    """
    async def _create_litellm_async_client():
        # Exercises litellm's real cache + real aiohttp-backed transport —
        # the same call shape `litellm.acompletion(...)` makes internally
        # for the aiohttp-transport providers (issue #2787's leak class).
        from litellm.llms.custom_httpx.http_handler import get_async_httpx_client

        handler = get_async_httpx_client(llm_provider="vertex_ai")
        session = handler.client._transport._get_valid_client_session()
        return session

    session = run_async(_create_litellm_async_client())

    # Public attribute of aiohttp.ClientSession — not reyn-private state.
    assert session.closed is True


def test_run_async_shutdown_is_idempotent_with_no_litellm_client_created() -> None:
    """Tier 2b: `run_async`'s litellm-client-close step is a no-op (does not
    raise) when no litellm async client was ever created during the run —
    the common case for LLM-free `run_async` callers (e.g. mcp.py CLI
    commands that never call an LLM).
    """
    async def _no_llm_call() -> str:
        return "done"

    # Must not raise even though no litellm async client cache entry exists
    # for this call.
    assert run_async(_no_llm_call()) == "done"
