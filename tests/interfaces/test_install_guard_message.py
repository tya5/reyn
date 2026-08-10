"""Tier 2: install-guard message distinguishes missing-vs-broken + robust install cmd.

Two diagnosability defects motivated ``reyn.interfaces.install_guard``:

1. A bare ``pip install -e ".[web]"`` recommendation can install into the wrong
   interpreter (pip shim ≠ active venv). The guard must recommend the robust
   ``python -m pip install -e '.[web]'`` form (interpreter-targeted + shell-glob
   safe quotes).
2. Reporting every ``ImportError`` as "not installed" masks version-conflict
   failures. The guard must distinguish a genuinely missing module
   (``ModuleNotFoundError`` whose ``name`` is the package) from an
   installed-but-broken import, and surface the real exception text in both.

A second, sibling defect in the same "unhelpful CLI error" class:
``reyn chat --connect`` printed a bare "server refused the connection (404)" that
gave no clue a 404 means the *agent* (defaulting to ``default``) wasn't found, or
that a 401 is a token problem. ``connect_failure_message`` maps the status to a
cause-naming, actionable message; those cases are tested below too.

These are Tier-2 (OS/subsystem invariant: the operator-facing failure surface
resolves the failure) tests of the pure message-selection helpers — no mocks; the
real ``ImportError`` / ``ModuleNotFoundError`` instances drive the branch.
"""
from __future__ import annotations

from reyn.interfaces.install_guard import install_command, missing_dep_message
from reyn.interfaces.repl.remote_client import connect_failure_message


def test_module_not_found_reports_not_installed_with_robust_command():
    """Tier 2: ModuleNotFoundError(name=package) → 'not installed' + robust cmd."""
    exc = ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
    msg = missing_dep_message(exc, "fastapi", "web")

    assert "not installed" in msg
    # Robust, interpreter-targeted install command with glob-safe quoting.
    assert "python -m pip install -e '.[web]'" in msg
    # Real exception text is surfaced (not masked).
    assert "No module named 'fastapi'" in msg


def test_generic_import_error_reports_installed_but_broken():
    """Tier 2: non-ModuleNotFound ImportError → 'installed but failed to import'."""
    exc = ImportError("cannot import name 'Foo' from 'fastapi'")
    msg = missing_dep_message(exc, "fastapi", "web")

    assert "installed but failed to import" in msg
    # The distinct branch must NOT claim the package is absent.
    assert "is not installed" not in msg
    # The real exception text must be visible so a version conflict is diagnosable.
    assert "cannot import name 'Foo' from 'fastapi'" in msg


def test_module_not_found_for_other_module_is_treated_as_broken():
    """Tier 2: package installed but its own dependency is missing → broken, not absent.

    ``import fastapi`` can raise ModuleNotFoundError whose ``name`` is a
    *transitive* dependency (fastapi is present but a dep it imports is not).
    That is an installed-but-broken failure, not 'fastapi is not installed'.
    """
    exc = ModuleNotFoundError("No module named 'starlette'", name="starlette")
    msg = missing_dep_message(exc, "fastapi", "web")

    assert "installed but failed to import" in msg
    assert "fastapi is not installed" not in msg
    assert "No module named 'starlette'" in msg


def test_install_command_is_interpreter_targeted_and_quoted():
    """Tier 2: install_command uses `python -m pip` and single-quotes the extra."""
    cmd = install_command("web")
    assert cmd == "python -m pip install -e '.[web]'"


# ---------------------------------------------------------------------------
# reyn chat --connect SSE-connect failure message (status-aware)
# ---------------------------------------------------------------------------
# A bare "server refused the connection (404)" gave no clue that a 404 means the
# agent (defaulting to 'default') wasn't found. connect_failure_message names the
# real cause and the next step per status.


def test_connect_404_names_agent_and_discovery_hint():
    """Tier 2: 404 → names the agent + the /a2a/agents discovery hint."""
    msg = connect_failure_message(404, "default", "http://127.0.0.1:8080")

    assert "404" in msg
    assert "default" in msg  # the agent name is surfaced
    assert "not found" in msg
    assert "http://127.0.0.1:8080/a2a/agents" in msg  # discovery hint


def test_connect_401_mentions_token_auth():
    """Tier 2: 401 → auth failure naming --token / REYN_WEB_AUTH_TOKEN."""
    msg = connect_failure_message(401, "default", "http://127.0.0.1:8080")

    assert "401" in msg
    assert "--token" in msg
    assert "REYN_WEB_AUTH_TOKEN" in msg
    # Must not be misreported as an agent-not-found problem.
    assert "not found" not in msg


def test_connect_other_status_includes_code_and_url():
    """Tier 2: other status → generic message carrying the code + failing URL."""
    msg = connect_failure_message(503, "default", "http://127.0.0.1:8080")

    assert "503" in msg
    assert "http://127.0.0.1:8080" in msg
