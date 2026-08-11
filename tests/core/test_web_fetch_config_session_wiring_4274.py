"""Tier 2c: operator `web_fetch:` yaml reaches the web_fetch op THROUGH a real
Session (#4274).

#4174 T4 renamed `web:` → `web_fetch:` and declared `OpContext.web_fetch_config`,
but no factory site ever populated it — every real chat session handed the
`web_fetch` op a `None` config, silently ignoring `web_fetch.verify_ssl` /
`allow_private_ips` / `max_download_bytes`. #4274 wires
`SessionFactoryConfig.web_fetch_config` → `Session._web_fetch_config` →
`RouterOpContextSource` → both router `OpContext` builders.

**This is a genuine behavior change**: an operator who set a non-default
`web_fetch:` value (e.g. `verify_ssl: false`, `allow_private_ips: true`, or a
tighter `max_download_bytes`) and never noticed it doing nothing will now see
it actually take effect.

The point of #4274 is the Session→op link — the config value only matters if a
real Session threads it into the OpContext the op actually reads (same
reasoning as #2679's `render_template_bounds` guard, mirrored here). Both
Session-side OpContext builders are driven — `Session._make_router_op_context`
(file/MCP twin) and `Session._router_host.make_router_op_context`
(RouterHostAdapter twin, the path the `web_fetch` TOOL actually dispatches
through on chat + pipeline) — since #4174 T4's own lesson (`getattr(config,
"web", None)` invisible to grep, caught only by the regression suite) is that
one of two twin builders silently missing a wire is exactly the failure shape
to guard against.

``max_download_bytes`` (not ``allow_private_ips``) is the probe: the SSRF
guard hard-denies loopback regardless of ``allow_private_ips`` (policy, #1956
— not a config knob), so a loopback fetch can't distinguish "config wired" from
"config absent" on that field. The download cap has no such floor — a real
local HTTP server response either fits under the configured cap or trips it,
with no other axis in the way. The SSRF guard itself is patched to a no-op
(the external/boundary seam here — #1956's own policy is not what this test
is about; only a loopback TEST SERVER makes the download-cap probe possible
without standing up real internet infrastructure).

Falsify-verified: stripping the `web_fetch_config=` forwarding from either
`RouterOpContextSource.__init__`'s call in `Session` or from
`build_router_op_context`/`RouterOpContextSource.build` makes
`ctx.web_fetch_config` come back `None`, so the op falls back to its 10 MiB
built-in default instead of the configured 5-byte cap and
`test_tight_max_download_bytes_reaches_the_op_through_both_builders` goes RED
(status flips from "too_large" to "ok").
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from reyn.config import WebFetchConfig
from reyn.config.loader import load_config
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML

_BODY = b"X" * 1000  # well over a 5-byte cap, well under the 10 MiB default


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(_BODY)

    def log_message(self, *a):  # silence
        pass


def _fetch(url: str, ctx) -> dict:
    op = WebFetchIROp(kind="web_fetch", url=url)
    return asyncio.run(handle_web_fetch(op=op, ctx=ctx))


def test_tight_max_download_bytes_reaches_the_op_through_both_builders(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: a NON-DEFAULT `web_fetch.max_download_bytes: 5` yaml value
    round-trips through `load_config` into a real `Session`, and BOTH
    Session-side OpContext builders hand the op a cap that rejects a
    1000-byte response (default cap is 10 MiB — this response would pass
    uncapped)."""
    (tmp_path / "reyn.yaml").write_text(
        MINIMAL_REYN_YAML + "web_fetch:\n  max_download_bytes: 5\n"
    )

    cfg = load_config(cwd=tmp_path)
    assert cfg.web_fetch.max_download_bytes == 5

    # The production shape: the frontend factories pass `config.web_fetch`
    # into the Session via SessionFactoryConfig (registry_bootstrap / chat.py /
    # dogfood.py / mcp.py / web/deps.py — see factory_config.py).
    session = make_session(agent_name="t", web_fetch_config=cfg.web_fetch)

    # Boundary seam: #1956's SSRF policy hard-denies loopback unconditionally
    # (not a config knob — see module docstring), so a real loopback test
    # server can't otherwise reach the download-cap check this test is about.
    from reyn import _ssrf_guard
    monkeypatch.setattr(_ssrf_guard, "assert_fetch_host_allowed", lambda *a, **k: None)

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/big"

        # Builder 1 — Session._make_router_op_context (file/MCP twin).
        r_session = _fetch(url, session._make_router_op_context())
        assert r_session["status"] == "too_large", r_session

        # Builder 2 — RouterHostAdapter.make_router_op_context (the
        # op_context_factory the web_fetch tool actually dispatches through
        # on chat + pipeline).
        r_host = _fetch(url, session._router_host.make_router_op_context())
        assert r_host["status"] == "too_large", r_host
    finally:
        srv.shutdown()


def test_default_config_leaves_the_response_uncapped_through_a_real_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: with no `web_fetch:` section the Session gets the safe
    default (10 MiB), so the same 1000-byte response is NOT capped through
    either builder — the operator config is opt-in, default behaviour
    unchanged by #4274's wiring."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML)

    cfg = load_config(cwd=tmp_path)
    assert cfg.web_fetch == WebFetchConfig()

    session = make_session(agent_name="t", web_fetch_config=cfg.web_fetch)

    from reyn import _ssrf_guard
    monkeypatch.setattr(_ssrf_guard, "assert_fetch_host_allowed", lambda *a, **k: None)

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/big"
        for builder in (
            session._make_router_op_context,
            session._router_host.make_router_op_context,
        ):
            result = _fetch(url, builder())
            assert result["status"] == "ok", result
            assert result["content"] == _BODY.decode()
    finally:
        srv.shutdown()
