"""#3028 — an MCP server that starts but never speaks must fail in a way the operator can act on.

The owner hit this in a real session: the RAG skill called `rag_query`, the stdio
server was launched via `uvx <pkg>`, and the run appeared to stop dead. Nothing
failed — `uvx` was fetching the package from PyPI on first run, so the process was
ALIVE (no execvp error, nothing on stderr) but had not yet said a word of MCP.

#3028 filed this as "reyn waits forever". **That premise is wrong, and these tests
deliberately do not encode it.** ``MCPGateway._run`` already wraps acquire+op in
``resolve_call_timeout`` (default 120s) and covers the handshake, and #2421 pins
structurally that every production MCP path goes through that seam — so no
reachable path waited forever. What actually reached the owner was 120s of silence
ending in ``MCPFault: TimeoutError:``, an error naming neither server, cause, nor
remedy. The defect was never unboundedness; it was that the bound taught nothing.

Two real gaps sit underneath, and each has tests below:

  1. **The client itself was unbounded.** FastMCP's dedicated handshake bound
     (``client_init_timeout``) defaults to None = disabled and reyn passed only the
     http/sse ``timeout``, so stdio fell through both and depended entirely on a
     caller imposing a deadline. HTTP merely *looked* protected — its ``timeout``
     is the session read timeout, bounding the handshake's read incidentally.
  2. **The failure explained nothing**, because the only bound in play was a
     generic per-op one that cannot know a handshake from a tool call.

So the invariant pinned here is the conjunction, stated wider than the uvx story
that produced it (reyn cannot tell a fetching launcher from a wedged import or a
server blocked on its own network call — from outside they are one shape):

    **When an MCP server does not respond, reyn gives up in finite time AND tells
    the operator what to do next.**

The second half is not decoration: a bound that fires with nothing to say is what
the owner already had. #3019 showed the failure that KILLS a server diagnoses
itself ("Connection closed"); this one arrives as FastMCP's ``RuntimeError: Failed
to initialize server session`` — no timeout, no duration, no remedy named.

Timing is NOT asserted anywhere here — that would pin the environment rather than
the behaviour. Each test runs a real silent subprocess under a generous outer cap
that fires ONLY if reyn never gives up: the cap is the falsifier for "finite", not
a measurement of it.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

# A "server" that launches cleanly and then never speaks MCP — the shape of a
# first-run `uvx` fetch as reyn sees it from the outside: the process is alive, so
# there is no exit code and no execvp error, and stdout stays empty forever. A real
# subprocess is essential, not incidental: the hang lives in the handshake between
# the real fastmcp client and a real pipe, which is exactly what a fake would elide.
_SILENT_SERVER_SRC = textwrap.dedent(
    '''
    import time
    while True:
        time.sleep(3600)
    '''
)

# A real, working MCP server — the non-regression control. A bound that also kills
# healthy launches would trade the owner's hang for the owner's broken startup.
_HEALTHY_SERVER_SRC = textwrap.dedent(
    '''
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("t3028healthy")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    if __name__ == "__main__":
        mcp.run(transport="stdio")
    '''
)

# Generous on purpose. Every test below gives up (or is meant to) in single-digit
# seconds, so this only ever fires when the bound is gone entirely — its size buys
# immunity to CI scheduling noise without weakening the claim, since "reyn gave up"
# and "the cap fired" are distinguishable outcomes, not a threshold judgement.
_OUTER_CAP_SECONDS = 60

# Small enough to keep the suite fast, large enough that a loaded CI box spawning a
# subprocess cannot be mistaken for a silent server. Not a value the product ships —
# the shipped default is sized for real package fetches (see client.py).
_TEST_BOUND_SECONDS = 5


@pytest.fixture()
def silent_server_cfg(tmp_path):
    server = tmp_path / "silent_server.py"
    server.write_text(_SILENT_SERVER_SRC, encoding="utf-8")
    return {"type": "stdio", "command": sys.executable, "args": [str(server)]}


@pytest.fixture()
def healthy_server_cfg(tmp_path):
    server = tmp_path / "healthy_server.py"
    server.write_text(_HEALTHY_SERVER_SRC, encoding="utf-8")
    return {"type": "stdio", "command": sys.executable, "args": [str(server)]}


async def _initialize_or_report_hang(client):
    """Drive ``initialize()`` under the outer cap, failing loudly if it never returns.

    Without the cap a regression would HANG the suite rather than fail it — the
    same silence, one layer up — so the cap converts "reyn never gives up" into a
    red test with a message that names the bug.
    """
    from reyn.mcp.client import MCPError

    try:
        await asyncio.wait_for(client.initialize(), timeout=_OUTER_CAP_SECONDS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"reyn did not give up on an unresponsive stdio server within "
            f"{_OUTER_CAP_SECONDS}s — the #3028 handshake bound is not in effect "
            f"(the pre-fix behaviour was to wait forever)."
        )
    except MCPError as exc:
        return exc
    pytest.fail("initialize() unexpectedly SUCCEEDED against a server that never speaks")


@pytest.mark.asyncio
async def test_unconfigured_silent_stdio_server_is_abandoned_in_finite_time(
    silent_server_cfg, monkeypatch,
):
    """Tier 2: a silent stdio server carrying NO timeout config is still given up on.

    The default path is the one the owner was on — nobody hand-configures a knob
    they have never heard of — so a bound that only works when explicitly asked for
    would not have saved them. The shipped default is retimed down here rather than
    waited out: the claim under test is "an unconfigured server IS bounded", and
    the default's *value* is a separate policy question (sized against a measured
    cold `uvx` fetch, argued at its definition), not something to spend two minutes
    of suite time re-measuring.
    """
    import reyn.mcp.client as client_mod

    monkeypatch.setattr(client_mod, "_DEFAULT_INIT_TIMEOUT_SECONDS", _TEST_BOUND_SECONDS)
    exc = await _initialize_or_report_hang(client_mod.MCPClient(silent_server_cfg))
    assert exc is not None


@pytest.mark.asyncio
async def test_giving_up_tells_the_operator_what_to_do_next(silent_server_cfg, monkeypatch):
    """Tier 2: the timeout error names the cause and a concrete way out.

    The #2932/#3009 rule: an error the operator cannot act on is not a fix. This is
    sharper here than for a write denial, because the failure itself is evidence-
    free — the process is alive, so there is no exit code, and a fetching launcher
    writes nothing to stderr. Everything the operator needs has to come from this
    text, or from nowhere.

    Asserts the mechanism is reachable, not the wording (per #3009): the knob's
    name, and that pre-installing is offered — those are the two things an operator
    cannot look up if the message omits them.
    """
    import reyn.mcp.client as client_mod

    monkeypatch.setattr(client_mod, "_DEFAULT_INIT_TIMEOUT_SECONDS", _TEST_BOUND_SECONDS)
    client = client_mod.MCPClient(silent_server_cfg, server_name="t3028_server")
    message = str(await _initialize_or_report_hang(client))

    assert "init_timeout" in message, "the knob that adjusts the bound is named"
    assert "install" in message.lower(), "the zero-config remedy (pre-install) is offered"
    assert "t3028_server" in message, "the config block is shown for THIS server, ready to paste"


@pytest.mark.asyncio
async def test_operator_declared_init_timeout_is_honoured(silent_server_cfg):
    """Tier 2: an explicit, non-default ``init_timeout`` reaches the client.

    Round-trips a value that is NOT the default, so the assertion cannot pass on
    the default leaking through — the bound has to have come from config. This is
    the #2964 half of the contract: the default is a floor reyn picks, the declared
    value is the operator's decision, and an operator who raises it for a slow
    server must actually get the longer wait they asked for.
    """
    from reyn.mcp.client import MCPClient

    cfg = {**silent_server_cfg, "init_timeout": _TEST_BOUND_SECONDS}
    exc = await _initialize_or_report_hang(MCPClient(cfg))
    assert f"{_TEST_BOUND_SECONDS:g}s" in str(exc), (
        "the operator's declared bound governs, and the error reports the bound that "
        "actually fired rather than a default it was never run with"
    )


@pytest.mark.asyncio
async def test_healthy_stdio_server_still_connects(healthy_server_cfg, monkeypatch):
    """Tier 2: the bound does not break a server that answers — the non-regression control.

    Guards the failure mode a too-eager bound would introduce: killing the launch it
    exists to protect. The `uvx` cold fetch this default is sized around is a real,
    correct startup that happens to be slow, so "gives up in finite time" must never
    harden into "gives up on slow-but-working servers".
    """
    import reyn.mcp.client as client_mod

    monkeypatch.setattr(client_mod, "_DEFAULT_INIT_TIMEOUT_SECONDS", _TEST_BOUND_SECONDS)
    async with client_mod.MCPClient(healthy_server_cfg) as client:
        tools = await client.list_tools()

    assert "echo" in {t.get("name") for t in tools if isinstance(t, dict)}


@pytest.mark.asyncio
async def test_gateway_path_surfaces_the_actionable_error_not_a_bare_timeout(
    silent_server_cfg, monkeypatch,
):
    """Tier 2: through the REAL seam, a silent server produces the actionable error.

    The tests above drive ``MCPClient`` directly, which is *below* the seam every
    production caller actually uses — so on their own they are structurally blind to
    the owner's failure. ``MCPGateway._run`` already bounded the handshake at
    ``call_timeout_seconds``; the whole defect was WHICH bound fires, since the
    gateway's is generic and surfaces as a bare ``MCPFault: TimeoutError:``. A fix
    that only improved the client's own error would therefore change nothing the
    owner could see, and every test above would still pass.

    So this drives ``MCPGateway.list_tools`` — the same seam ``rag_query``'s
    pre-flight uses — and asserts the operator ends up with the remedy rather than
    the empty timeout. Both bounds are left at their default ORDERING (init retimed
    for speed, gateway untouched), because that ordering is the thing under test.
    """
    import reyn.mcp.client as client_mod
    from reyn.mcp.gateway import MCPFault, MCPGateway

    monkeypatch.setattr(client_mod, "_DEFAULT_INIT_TIMEOUT_SECONDS", _TEST_BOUND_SECONDS)
    gateway = MCPGateway()
    with pytest.raises(MCPFault) as caught:
        await asyncio.wait_for(
            gateway.list_tools("t3028_server", silent_server_cfg),
            timeout=_OUTER_CAP_SECONDS,
        )

    message = str(caught.value)
    assert "init_timeout" in message, (
        "the operator reached a dead end through the real seam with no knob named — "
        "the generic gateway timeout won the race and its message says nothing"
    )
    assert "install" in message.lower(), "the pre-install remedy survives to the seam"


@pytest.mark.asyncio
async def test_handshake_bound_is_ordered_below_the_gateways_bound(silent_server_cfg):
    """Tier 2: the shipped defaults are ordered so the EXPLAINING error wins.

    Both bounds cover the handshake, so whichever is smaller decides what the
    operator reads. MEASURED against a real silent server, the two orderings give
    materially different outcomes:

        init 5 < call 30  ->  MCPError naming the cause + both remedies
        call 5 < init 30  ->  MCPFault: TimeoutError:   (nothing at all)

    So this ordering is not a tuning preference, it is the difference between #3028
    being fixed and merely being re-timed. Nothing in the type system holds it: a
    later, locally-reasonable retune of either default (raise this one for slow
    corporate mirrors; lower the gateway's for snappier failures) silently reverts
    the owner to the empty error, and every other test here would stay green.

    Pins the RELATIONSHIP, never the values — either may be retuned freely, so long
    as the error that can be acted on is still the one that arrives.
    """
    from reyn.mcp.client import _DEFAULT_INIT_TIMEOUT_SECONDS
    from reyn.mcp.gateway import _DEFAULT_MCP_CALL_TIMEOUT_SECONDS

    assert _DEFAULT_INIT_TIMEOUT_SECONDS < _DEFAULT_MCP_CALL_TIMEOUT_SECONDS, (
        "the handshake bound must fire before the gateway's generic per-op bound, or "
        "the operator gets 'MCPFault: TimeoutError:' with no cause and no remedy"
    )


@pytest.mark.asyncio
async def test_init_timeout_zero_disables_the_handshake_bound(silent_server_cfg):
    """Tier 2: ``init_timeout: 0`` disables THIS bound, as documented.

    reyn now decides, by default, to stop waiting on someone else's server — a
    judgement call. The escape hatch is what keeps that a floor rather than a
    ceiling, and an escape hatch nobody tests is a claim, not a feature: a later
    retune must not quietly make `0` mean "immediately".

    Scoped precisely: `0` disables the *handshake* bound, NOT every bound. A real op
    still runs inside the gateway's ``call_timeout_seconds``, which is why the doc
    says "disables this bound" rather than "waits forever" — the client is driven
    here directly, below that seam, so what `0` means in isolation is observable.

    Necessarily asserts a negative ("has not given up yet"), safe in the direction
    that matters: a slow box only makes it MORE true. It goes red exactly when `0`
    starts terminating early, which is the regression.
    """
    from reyn.mcp.client import MCPClient

    cfg = {**silent_server_cfg, "init_timeout": 0}
    client = MCPClient(cfg)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.initialize(), timeout=_TEST_BOUND_SECONDS)
