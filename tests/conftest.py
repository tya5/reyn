"""Pytest configuration for the Reyn test suite.

Registers the ``@pytest.mark.replay(fixture_rel)`` marker and provides the
``_llm_replay`` autouse fixture that wires it up.

Usage in tests::

    @pytest.mark.replay("fixtures/llm/skill_router/chitchat.jsonl")
    def test_router_chitchat(_llm_replay):
        ...

Environment variables
---------------------
``REYN_LLM_RECORD=1``
    Force record mode — call the real LLM and overwrite / extend the fixture.
    Requires a live LLM backend (see ``project_local_env.md`` in memory).

Record mode is also activated automatically when a fixture file is missing
(first-run bootstrap).

Environment identity
--------------------
``pytest_sessionstart`` refuses to start a session whose venv is missing a
console script this checkout declares, and ``out_of_process_reyn`` is the
fixture a test requests when it spawns something that imports ``reyn``. Both
delegate to ``scripts/verify_env_identity.py`` — see #3024 and that module's
docstring for why "the environment runs the tree I am measuring" is not
something a test may assume.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# ── Repo-root on sys.path (stable ``tests`` package imports) ───────────────────
#
# ``tests/`` has no ``__init__.py`` (it is collected from the rootdir as an
# implicit namespace package). That makes ``from tests._support... import X``
# resolve only when the repo root happens to be on ``sys.path`` — true under
# ``python -m pytest`` from the repo root, but NOT under bare ``pytest`` or when
# invoked from another cwd / an IDE runner, where it fails with
# ``ModuleNotFoundError: No module named 'tests'``. Inserting the repo root here
# (this conftest loads for any collected test, including a single isolated file)
# makes ``tests`` and ``tests._support`` importable in every invocation style,
# so shared helpers do not depend on how pytest was started.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── FP-0058 P2: A2A/MCP opt-in for pre-existing protocol tests ──────────────
#
# A2A and MCP are now secure-default OFF (``reyn.interfaces.web.surfaces`` —
# opt-in, broad machine-integration ports). Pre-existing A2A/MCP protocol
# tests across the suite (``tests/web/test_a2a.py``, ``tests/web/test_mcp_sse.py``,
# ``tests/test_fp0001_a2a_endpoints.py``, ``tests/test_a2a_runentry_task_migration_1981.py``)
# exercise those surfaces directly and were written against the previous
# always-on mount behaviour; they need the surfaces opted back in to keep
# testing what they test (this is the FP-0058 "consumer audit" for the
# secure-default flip, not a workaround — the tests are legitimate, the
# environment they assumed changed).
#
# The FastAPI ``app`` singleton in ``reyn.interfaces.web.server`` mounts its
# surfaces once, at the module's first import, for the WHOLE pytest process
# — so this override must be set at collection time, here in the root
# conftest (loaded before any test module's first import), not inside an
# individual test file, which could run after some other file already
# triggered the import with the surfaces still off.
#
# ``tests/web/test_surface_registry.py`` (the FP-0058 P2 registry's own
# tests) force a fresh re-import of ``reyn.interfaces.web.server`` per test —
# it does not rely on, or get affected by, this session-wide default.
os.environ.setdefault("REYN_WEB_ENABLE_SURFACES", "a2a,mcp")

# ── Environment identity (#3024) ───────────────────────────────────────────────
#
# Loaded by path, not imported as a package: `scripts/` is not importable, and
# the checker is deliberately stdlib-only and reyn-free (it cannot ask `reyn`
# where `reyn` is — that is the question).


def _load_env_identity():
    path = Path(_REPO_ROOT) / "scripts" / "verify_env_identity.py"
    spec = importlib.util.spec_from_file_location("_reyn_env_identity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves its own module through
    # `sys.modules[cls.__module__]` while the class body executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_ENV_IDENTITY = _load_env_identity()


@pytest.fixture(scope="session")
def reyn_console_scripts() -> None:
    """Declare that this test runs a `[project.scripts]` console script by name.

    A `[project.scripts]` entry is a declaration; the console script is a file `pip`
    writes into a venv. A venv installed before the entry existed has never heard of
    it, and the resulting failure names neither the venv nor the absence — it
    surfaces as ``execvp() failed``, or, through a stdio client, as
    ``McpError: Connection closed``. Both read as a broken feature. #3024: that
    misread reached a co-vet verdict twice in one day, once as "a pre-existing flake
    on origin/main" for a venv that was simply missing the script.

    Unlike tree divergence — which a test dissolves itself by pinning
    ``out_of_process_reyn`` — an absent console script cannot be fixed from inside
    the suite: installing it belongs to whoever provisions the venv. So the
    unreachability here is genuine, and the honest response is to skip **naming the
    absent subject**, rather than to run and report a deterministic RED as evidence
    about the feature.

    Under CI it **fails** instead. A skip is only honest while the property is
    witnessed somewhere, and CI — which installs this checkout — is that somewhere.
    A CI venv missing a script this checkout declares is a defect in CI's setup, and
    silently skipping there would make the whole mechanism dormant.
    """
    findings = _ENV_IDENTITY.verify(Path(_REPO_ROOT), only=("console-scripts",))
    if not findings:
        return
    rendered = "\n".join(f.render() for f in findings)
    message = (
        f"this venv does not carry every console script {_REPO_ROOT} declares, so "
        f"running one would measure the venv's staleness, not the feature.\n{rendered}"
    )
    if os.environ.get("CI"):
        pytest.fail(f"env-identity (CI installs this checkout — its venv must be complete): {message}")
    pytest.skip(f"env-identity: {message}")


@pytest.fixture(scope="session")
def out_of_process_reyn() -> str:
    """Declare that this test spawns something importing ``reyn``; yield the src root to pin.

    In-process, a test always reads the checkout it was started from —
    ``[tool.pytest.ini_options] pythonpath`` puts ``<rootdir>/src`` on `sys.path`.
    A subprocess gets no such favour: it re-resolves `reyn` from the venv, and in a
    git worktree (which has no venv of its own) that answer is whatever checkout
    the ambient venv's editable ``.pth`` points at. The two halves then disagree
    silently, and the spawning half is the one that is wrong.

    Pin the returned path as ``PYTHONPATH`` in the spawn's environment. Note that
    an MCP stdio server needs it threaded through the server's *configured* env:
    the SDK passes a six-key whitelist that drops ``PYTHONPATH``, so inheriting is
    not enough (``tests/test_fp0063_p3_rag_pipelines.py`` does this).

    Requesting the fixture is what makes the dependency a declaration rather than a
    thing each author rediscovers — the pin exists by hand in several files today,
    each added after the same lesson was learned again.

    The root is derived from the **in-process** ``reyn``, not from the rootdir, so
    the check below is the invariant itself rather than a proxy for it: *the reyn a
    spawn reads is the reyn this test imports*. That can be false exactly where it
    matters (a worktree, where the two halves diverge) and it is asserted there —
    a check that can only run where its property is trivially true is not a witness.
    """
    import reyn

    root = Path(reyn.__file__).resolve().parents[2]
    findings = _ENV_IDENTITY.verify(root, only=("pinned-tree",))
    if findings:
        rendered = "\n".join(f.render() for f in findings)
        pytest.fail(
            f"env-identity: a subprocess pinned to this checkout does not read the same "
            f"reyn this test imported ({reyn.__file__}), so the two halves of this test "
            f"would measure different checkouts.\n{rendered}"
        )
    return str(root / "src")


# ── Secret store isolation ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect all secret-store operations to a per-test tmp dir.

    Every test runs with REYN_SECRETS_PATH pointing at a throwaway file under
    pytest's tmp_path so that ``~/.reyn/secrets.env`` is never touched.

    The env var is restored to its prior value (or unset) automatically by
    monkeypatch at teardown — no manual cleanup needed.
    """
    tmp_secrets = tmp_path / "secrets.env"
    monkeypatch.setenv("REYN_SECRETS_PATH", str(tmp_secrets))


@pytest.fixture(autouse=True)
def _provider_credentials_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2708 P3.2b: present dummy provider credentials by default.

    The missing-cred check now fires at the single LLM funnel
    (``recorded_acompletion``) BEFORE any ``litellm.acompletion`` stub / replay.
    So a unit or replay test that fakes the provider call still funnels through
    the check — and a real run needs credentials to make an LLM call at all. The
    default test environment therefore presents provider credentials, exactly as
    a configured machine would. A test that specifically exercises the MISSING-
    cred path unsets these (see the ``_keys_unset`` fixtures in
    ``test_2686_*`` / ``test_2708_*``, which depend on this fixture so their
    ``delenv`` runs AFTER this ``setenv`` and wins). Tests that set their own
    provider key / proxy ``api_base`` override these unconditionally."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY"):
        monkeypatch.setenv(var, "test-key")


@pytest.fixture(autouse=True)
def _isolate_budget_limit_context():
    """Reset ``reyn.llm.llm._llm_call_limit_context_var`` after every test.

    Root fix for the Python-3.12 CI suite hang (#1800-7 diagnostic, PR #2062).
    The over-budget pre-check in ``call_llm`` / ``call_llm_tools`` raises
    ``BudgetExceeded`` before any LLM call **iff** this contextvar is UNSET
    (fail-closed deny); when it is SET-to-allow, ``_budget_exceed_allows_continue``
    returns True and the call proceeds to ``recorded_acompletion`` → a real
    network call → on Linux an infinite ``EpollSelector.poll(timeout=-1)`` that
    hangs the whole ``-n auto`` job to its timeout. A test that calls
    ``set_llm_call_limit_context`` without resetting its token leaks the contextvar
    SET; under pytest-xdist a co-located over-quota test then bypasses the
    pre-check (the necessary condition). The hang is Linux-only (epoll), so it
    never reproduced on macOS. Resetting per-test makes the pre-check
    deterministically fail-close — leaker-agnostic, protects the whole class."""
    yield
    from reyn.llm.llm import _llm_call_limit_context_var
    _llm_call_limit_context_var.set(None)

# ── Marker registration ────────────────────────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "replay(fixture): monkeypatch litellm.acompletion with a JSONL fixture. "
        "Pass the fixture path relative to the tests/ directory.",
    )
    config.addinivalue_line(
        "markers",
        "docker: live-Docker integration test (#1332). Skipped when no daemon is "
        "reachable; runs against a real container. Select with `-m docker` / "
        "deselect with `-m 'not docker'`.",
    )


# ── Autouse fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _llm_replay(request: pytest.FixtureRequest):
    """Install / restore the LLM replay mock for tests marked with @replay."""
    marker = request.node.get_closest_marker("replay")
    if marker is None:
        # Not a replay test — let the real litellm through (or let the test
        # mock it however it likes).
        yield
        return

    fixture_rel: str = marker.args[0]
    fixture_path = Path(__file__).parent / fixture_rel

    force_record = os.environ.get("REYN_LLM_RECORD") == "1"
    mode: str
    if force_record:
        mode = "record"
    elif not fixture_path.exists():
        # First-run: no fixture yet — record automatically.
        mode = "record"
    else:
        mode = "replay"

    from reyn.dev.testing.replay import LLMReplay

    replay = LLMReplay(fixture_path, mode=mode)  # type: ignore[arg-type]
    replay.install()
    try:
        yield replay
    finally:
        replay.restore()
        if mode == "record":
            replay.flush()
