"""Tier 2: FP-0016 Component D — secret_store field on OpContext.

Covers:
- OpContext can be constructed with secret_store=None (default) — backward compat
- OpContext can be constructed with secret_store=<ScopedSecretStore> — field accessible
- Field is positional-or-kw (= dataclass default), not strict-only-kw

No mocks; uses real Workspace + EventLog + PermissionDecl instances.

If ScopedSecretStore is not yet on disk (D1 not landed), tests use a local
stub class that satisfies the structural contract for field acceptance.
"""
from __future__ import annotations

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.security.permissions.permissions import PermissionDecl
from reyn.workspace.workspace import Workspace

# ---------------------------------------------------------------------------
# Stub: stand-in for reyn.security.secrets.store.ScopedSecretStore until D1 lands.
# If D1 has already landed, the real class is used in test 2 instead.
# ---------------------------------------------------------------------------

class _StubScopedSecretStore:
    """Minimal stand-in satisfying the structural contract of ScopedSecretStore."""

    def __init__(self, allowed_keys: list[str] | None = None) -> None:
        self._allowed_keys: list[str] = allowed_keys or []

    def get(self, key: str) -> str | None:
        return None


def _make_context(**kwargs: object) -> OpContext:
    """Construct a minimal real OpContext for test use."""
    ws = Workspace(events=EventLog(), skill_name="test_skill")
    return OpContext(
        workspace=ws,
        events=EventLog(),
        permission_decl=PermissionDecl(),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_secret_store_default_is_none() -> None:
    """Tier 2: OpContext.secret_store defaults to None (backward compat)."""
    ctx = _make_context()
    assert ctx.secret_store is None


def test_secret_store_accepts_instance() -> None:
    """Tier 2: OpContext(secret_store=<store>) stores and exposes the instance."""
    store = _StubScopedSecretStore(allowed_keys=["GITHUB_TOKEN"])
    ctx = _make_context(secret_store=store)
    assert ctx.secret_store is store


def test_secret_store_keyword_arg() -> None:
    """Tier 2: secret_store is accepted as a keyword argument (dataclass default)."""
    # Constructing with keyword works; no positional confusion.
    store = _StubScopedSecretStore()
    ctx = _make_context(secret_store=store)
    assert ctx.secret_store is store


def test_secret_store_none_explicit() -> None:
    """Tier 2: Passing secret_store=None explicitly mirrors the default."""
    ctx = _make_context(secret_store=None)
    assert ctx.secret_store is None


def test_real_scoped_secret_store_if_available() -> None:
    """Tier 2: If ScopedSecretStore is available from D1, field accepts the real class."""
    try:
        from reyn.security.secrets.store import ScopedSecretStore  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pytest.skip("ScopedSecretStore not yet available (D1 not landed)")

    store = ScopedSecretStore(allowed_keys=["API_KEY"])  # type: ignore[call-arg]
    ctx = _make_context(secret_store=store)
    assert ctx.secret_store is store
