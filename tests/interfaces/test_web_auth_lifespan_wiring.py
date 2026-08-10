"""Tier 2: the server lifespan builds the AuthContext read by the auth gate.

ADR-0039 P0. The AG-UI endpoint auth gate reads ``app.state.auth``; if the
lifespan did not build it the gate would fail closed for every client. This
pins the wiring: after startup ``app.state.auth`` is a real AuthContext, and an
operator-configured ``web.auth.token`` is the effective secret (not silently
replaced by a generated one).

Real ``_lifespan`` + a real reyn.yaml on disk (mirrors
test_fp0009_b_web_lifespan.py); no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from fastapi import FastAPI  # noqa: E402

from reyn.interfaces.web.auth import TOKEN_ENV_VAR, AuthContext  # noqa: E402


def _write_reyn_yaml(directory: Path, content: str) -> None:
    (directory / "reyn.yaml").write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_lifespan_builds_auth_context_from_configured_token(tmp_path, monkeypatch):
    """Tier 2: lifespan sets app.state.auth using the configured web.auth.token."""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    _write_reyn_yaml(
        tmp_path,
        "model: standard\nweb:\n  auth:\n    token: operator-configured-secret\n",
    )
    monkeypatch.chdir(tmp_path)

    from reyn.interfaces.web.server import _lifespan

    app = FastAPI()
    async with _lifespan(app):
        auth = app.state.auth
        assert isinstance(auth, AuthContext)
        assert auth.token == "operator-configured-secret"


@pytest.mark.asyncio
async def test_lifespan_generates_token_when_none_configured(tmp_path, monkeypatch):
    """Tier 2: with no configured token, lifespan still yields an authenticated surface.

    A generated token is never empty — the browser surface is never left
    unauthenticated (a missing token would make every loopback connection
    unauthenticated instead of gated).
    """
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    _write_reyn_yaml(tmp_path, "model: standard\n")
    monkeypatch.chdir(tmp_path)

    from reyn.interfaces.web.server import _lifespan

    app = FastAPI()
    async with _lifespan(app):
        auth = app.state.auth
        assert isinstance(auth, AuthContext)
        assert auth.token
