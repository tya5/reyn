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
    Force record mode — call the real LLM and re-record the fixture.
    Requires a live LLM backend (see ``project_local_env.md`` in memory).
    #3634: this REPLACES each re-recorded call's stale entry rather than
    appending a duplicate alongside it (``LLMReplay.flush``'s own docstring
    has the mechanism) — a fixture shared by several tests keeps every
    OTHER test's entries untouched, so re-running one test under
    ``REYN_LLM_RECORD=1`` does not erase its siblings' recordings.

Record mode is also activated automatically when a fixture file is missing
(first-run bootstrap).

Environment identity
--------------------
Two fixtures a test requests to declare what it depends on, both delegating to
``scripts/verify_env_identity.py``:

``out_of_process_reyn``
    Requested when the test spawns something that imports ``reyn``. Yields the
    src root to pin as ``PYTHONPATH``, so the spawn reads the checkout under
    test rather than whichever one the venv resolves.
``reyn_console_scripts``
    Requested when the test runs a ``[project.scripts]`` console script by name.
    Skips (or, under CI, fails) when this venv predates a declared script.

Neither is autouse: a test that does not spawn declares nothing and pays
nothing. See #3024 and that module's docstring for why "the environment runs
the tree I am measuring" is not something a test may assume.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# `pytester` — a core pytest plugin, not auto-loaded by default — is declared
# here because tests/test_network_gate_3451.py drives a real, isolated inner
# pytest session against `reyn.dev.testing.network_gate` to exercise its
# hooks end to end.
#
# `reyn.dev.testing.network_gate` is itself a full pytest plugin (its own
# `pytest_configure`/`pytest_runtest_setup`/`pytest_runtest_teardown`/
# `pytest_sessionfinish`) so an ISOLATED inner pytester session can load just
# the gate (via `pytest_plugins = ["reyn.dev.testing.network_gate"]` in that
# inner session's OWN throwaway conftest) without dragging in this file's
# other, repo-layout-dependent fixtures. It is deliberately NOT declared in
# THIS file's own `pytest_plugins` list, even though this conftest also wires
# it in (see the hook functions below) — a `pytest_plugins` string entry is
# resolved (imported) during pytest's plugin-discovery phase, which runs
# BEFORE the root `conftest.py`'s `pytest_configure` (the #3233 in-process
# `import reyn` guard) ever gets a chance to fire. `import reyn.dev...` failing
# at that earlier phase (e.g. a decoy `reyn` cached by a stray sitecustomize/
# .pth, the exact #3233 shape) would raise its OWN raw ImportError and abort
# before the guard's friendlier "env-identity (in-process, #3233)" message —
# regression caught by tests/test_3233_inprocess_env_identity.py's decoy
# witness. So THIS conftest imports and calls it lazily, function-body-local,
# from within its own hooks below (same lazy-import style `_llm_replay`
# already uses for `LLMReplay`) — strictly after the root guard has run.
pytest_plugins = ["pytester"]

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
def _embedding_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """FP-0066 §7: the `embed` op pre-flights ``embedding.enabled`` (default
    **False** in production — opt-in / predictable-safe default) and
    returns a decision-enabling block when it is off (see
    ``reyn.core.op_runtime.embed._is_embedding_enabled`` /
    ``_embedding_disabled_block``). The overwhelming majority of the test
    suite (embed / index_update / semantic_search / ActionEmbeddingIndex /
    rag pipeline tests) predates that gate and exercises the embed-succeeds
    path directly against a FakeEmbeddingProvider with no ``reyn.yaml`` on
    disk, so the real config-load default would block them all uniformly —
    the same "provider credentials present by default" shape as
    ``_provider_credentials_present`` above. A test that specifically
    exercises the DISABLED / ``embedding.enabled: false`` path
    monkeypatches ``_is_embedding_enabled`` back to False (or False-like)
    itself, AFTER this fixture runs, which wins (monkeypatch has no
    ordering concept beyond last-write)."""
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "_is_embedding_enabled", lambda: True)


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


@pytest.fixture(autouse=True)
def _isolate_rich_style_ansi_memo():
    """Reset rich's process-global rendered-SGR memo after every test (#3572).

    ``Style._make_ansi_codes`` caches a Style's rendered escape in
    ``self._ansi`` and never keys that cache by ``color_system`` (measured on
    rich 15.0.0 — a bug in rich, deliberately NOT reported upstream, see
    ``tests/test_markdown_palette_gate_3469.py``'s ``_memo_cleared_theme`` for
    the owner decision and the runnable "is it still there?" snippet). Because
    ``Style.parse`` AND ``Style._add`` (which ``Style.__add__`` delegates to,
    i.e. the combined style a Console actually renders) are ``lru_cache``d, and
    ``rich.default_styles.DEFAULT_STYLES`` is a module global, those Style
    instances are shared by every test in the pytest process: whichever console
    renders a given style string FIRST bakes
    its colour system into the shared instance, and every later console —
    whatever colour system IT asked for — re-emits that escape verbatim.

    What makes this bite in CI and not on a developer's machine is the colour
    system reyn's own renderers get. ``RichChatRenderer`` / ``InlineChatRenderer``
    construct ``Console(force_terminal=True, ...)`` with colour detection left to
    rich, which is correct for production. Under CI's environment (no ``TERM``,
    no ``COLORTERM``) that detects **standard**, so any test that drives one of
    those two renderers memoizes ``_CC_DIM`` (``#6b7280``) as
    bright-black ``'90'``; a later test that explicitly asks for truecolor then
    measures ``'\\x1b[90m…'`` and goes red (measured: #3571 / #3575, byte-identical
    to a local repro with ``TERM``/``COLORTERM`` unset, running
    ``test_agui_sr5_bit_identical_p4.py`` before
    ``test_right_gutter_label_visible_3536.py``). It flaps rather than failing
    every run because ``--dist load`` decides per run which worker gets which
    pair. Locally, ``TERM=xterm-256color`` detects eight-bit and both sides of
    the pair agree often enough to hide it.

    Guarding each READER (as #3472 did for the palette gate alone) closes one
    hole and leaves the class open — the next test that renders ``#6b7280`` on a
    truecolor console inherits the bug, which is exactly how #3536's gutter test
    became the second victim. Resetting here instead is leaker-agnostic: every
    test starts from an unmemoized rich, so no test can observe another's colour
    system. ``_ansi`` is a rich-private field and there is no public reset
    (``copy()`` and ``+ Style()`` both preserve or alias it), so rich's caches
    and defaults are restored directly — and a rich upgrade that removes them
    makes this fixture raise here rather than silently become a no-op.

    Same shape as ``_isolate_budget_limit_context`` above, for the same reason.
    """
    yield
    from rich.default_styles import DEFAULT_STYLES
    from rich.style import Style

    # Every ``lru_cache``d member of ``Style`` — enumerated rather than named
    # (``parse``, ``_add``, ``normalize``, ``clear_meta_and_links``, … on rich
    # 15.0.0) because ``Style.__add__`` delegates to a CACHED ``_add``, so the
    # combined style a Console actually renders is itself process-global and
    # carries its own ``_ansi``. Clearing only ``parse`` measurably leaves the
    # red in place (verified while landing this: the poisoned instance the
    # renderer used was the cached ``_add`` result, not the parsed one).
    cleared = [
        member for member in (getattr(Style, name, None) for name in dir(Style))
        if hasattr(member, "cache_clear")
    ]
    assert cleared, "rich.style.Style exposes no cached member — reset is now a no-op"
    for member in cleared:
        member.cache_clear()
    for style in DEFAULT_STYLES.values():
        style._ansi = None

# ── Marker registration ────────────────────────────────────────────────────────


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """``trylast=True``: pluggy calls unmarked ``pytest_configure``
    implementations in REVERSE registration order (a deeper conftest, loaded
    after the root one, would otherwise run FIRST) — this file's `import
    reyn.dev...` (inside, for the #3451 network gate) must run strictly AFTER
    the root ``conftest.py``'s own ``pytest_configure`` (the #3233 in-process
    import-identity guard), which has no such marker and therefore keeps its
    normal place. Without ``trylast`` here, a decoy `reyn` (#3233's exact
    scenario) makes THIS file's import raise a raw ``ModuleNotFoundError``
    before the guard's friendlier diagnostic ever runs — regression caught by
    ``tests/test_3233_inprocess_env_identity.py``'s decoy witness."""
    config.addinivalue_line(
        "markers",
        "replay(fixture): monkeypatch litellm.acompletion AND litellm.aembedding "
        "with a JSONL fixture. Pass the fixture path relative to the tests/ "
        "directory.",
    )
    config.addinivalue_line(
        "markers",
        "docker: live-Docker integration test (#1332). Skipped when no daemon is "
        "reachable; runs against a real container. Select with `-m docker` / "
        "deselect with `-m 'not docker'`.",
    )

    # #3451 network gate — imported lazily, HERE, inside the hook body (not at
    # module top-level / not a `pytest_plugins` string): see the long comment
    # by this file's `pytest_plugins` declaration above for why an eager
    # `import reyn.dev...` at plugin-discovery time regresses the #3233 decoy
    # witness. By the time this function body runs, the root conftest.py's own
    # `pytest_configure` (the in-process import-identity guard) has already
    # had its turn.
    from reyn.dev.testing import network_gate

    network_gate.pytest_configure(config)


def pytest_runtest_setup(item: pytest.Item) -> None:
    from reyn.dev.testing import network_gate

    network_gate.pytest_runtest_setup(item)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    from reyn.dev.testing import network_gate

    network_gate.pytest_runtest_teardown(item, nextitem)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    from reyn.dev.testing import network_gate

    network_gate.pytest_sessionfinish(session, exitstatus)


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
