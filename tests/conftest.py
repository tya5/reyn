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

#3662: a missing fixture file no longer activates record mode automatically.
First-run fixture creation is now the SAME explicit step as re-recording:
run with ``REYN_LLM_RECORD=1``. A fixture missing for any OTHER reason (an
accidental delete, a bad rebase) used to fall back to a real, unauthorized
network call instead of a loud, attributable failure — see
``reyn.dev.testing.network_gate``'s module docstring and #3660/#3662.

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
import warnings
from pathlib import Path
from typing import Iterator

import pytest

# `pytester` — a core pytest plugin, not auto-loaded by default — is declared
# here because tests/dev/test_network_gate_3451.py drives a real, isolated inner
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
# regression caught by tests/scripts/test_3233_inprocess_env_identity.py's decoy
# witness. So THIS conftest imports and calls it lazily, function-body-local,
# from within its own hooks below (same lazy-import style `_llm_replay`
# already uses for `LLMReplay`) — strictly after the root guard has run.
pytest_plugins = ["pytester"]

# ── Repo-root on sys.path (stable ``tests`` package imports) ───────────────────
#
# ``tests/__init__.py`` exists (#4001: without it, pytest's prepend import mode
# stops walking up at the first ancestor lacking ``__init__.py`` — which used
# to be ``tests/`` itself, so a bucket like the former ``tests/mcp/`` collected
# as the TOP-LEVEL module ``mcp``, shadowing the real third-party ``mcp`` SDK
# package on ``sys.path`` the instant any test imported it). With
# ``tests/__init__.py`` present, that walk continues past ``tests/`` to the
# repo root, and every collected test resolves as ``tests.<bucket>.test_x`` (or
# ``tests.test_x`` for a flat file) — a name that can never collide with an
# installed distribution's own top-level name.
#
# ``from tests._support... import X`` still only resolves when the repo root is
# on ``sys.path`` — true under ``python -m pytest`` from the repo root, but NOT
# under bare ``pytest`` or when invoked from another cwd / an IDE runner, where
# it fails with ``ModuleNotFoundError: No module named 'tests'``. Inserting the
# repo root here (this conftest loads for any collected test, including a
# single isolated file) makes ``tests`` and ``tests._support`` importable in
# every invocation style, so shared helpers do not depend on how pytest was
# started.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── FP-0058 P2: A2A/MCP opt-in for pre-existing protocol tests ──────────────
#
# A2A and MCP are now secure-default OFF (``reyn.interfaces.web.surfaces`` —
# opt-in, broad machine-integration ports). Pre-existing A2A/MCP protocol
# tests across the suite (``tests/web/test_a2a.py``, ``tests/web/test_mcp_sse.py``,
# ``tests/interfaces/test_fp0001_a2a_endpoints.py``, ``tests/test_a2a_runentry_task_migration_1981.py``)
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
    not enough (``tests/builtin/test_fp0063_p3_rag_pipelines.py`` does this).

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


@pytest.fixture(scope="session")
def rag_plugin_python() -> str:
    """A venv python with the rag plugin's ``requirements.txt`` installed —
    see ``tests/_support/rag_plugin_venv.py`` for the #4302 option-A
    rationale. A thin fixture wrapper: the actual build is a process-cached
    plain function so ``tests/builtin/test_fp0063_p3_rag_pipelines.py``'s
    own ``_write_project``-style helpers (called from ~20 test bodies, not
    fixtures themselves) can share the same build without threading this
    fixture through every one of those call sites.
    """
    from tests._support.rag_plugin_venv import rag_plugin_python as _build

    return _build()


@pytest.fixture(scope="session", autouse=True)
def _flowview_pin_verified() -> None:
    """#3723: 4 of 4 sessions on 2026-08-06 measured a full suite against a
    mis-pinned ``textual-flowview`` — three had a stale version, and read
    real failures as "pre-existing on origin/main"; the fourth had the
    pinned VERSION but was reading a local working copy, invisible to a
    version-only check. Unlike `reyn_console_scripts`/`out_of_process_reyn`
    above (opt-in, per-test), this is autouse and session-scoped: it must run
    once, before the first test body, for every invocation — a session that
    never happens to request it is exactly how this went undetected. A red
    test measured under a mis-pinned flowview is not "one more failure", it
    makes the WHOLE suite's result incomparable to `origin/main`'s, so a
    version mismatch (`flowview-pin/stale`/`flowview-pin/absent`) aborts the
    run outright (`pytest.exit`) instead of failing one test.

    `flowview-pin/local-copy` is different: a local `textual-flowview` clone
    is legitimate development (#3725 review, lead-coder — tui-coder was doing
    exactly this when the blanket abort version of this fixture would have
    stopped them from running a single test). `REYN_FLOWVIEW_LOCAL_COPY="<reason>"`
    (a non-empty reason, required — see `partition_flowview_findings`'s
    docstring) downgrades that ONE finding kind from an abort to a
    `warnings.warn`, which pytest surfaces in its own warnings summary at the
    end of the run — not just printed at setup time and easy to miss, but
    landing in the same report the run's own result reaches (#3723's
    incident was exactly a case where the reason was knowable but never
    reached anyone reading the result).
    """
    findings = _ENV_IDENTITY.verify(Path(_REPO_ROOT), only=("flowview-pin",))
    if not findings:
        return

    reason = os.environ.get("REYN_FLOWVIEW_LOCAL_COPY", "")
    blocking, acknowledged = _ENV_IDENTITY.partition_flowview_findings(findings, reason)

    if acknowledged:
        rendered = "\n".join(f.render() for f in acknowledged)
        warnings.warn(
            f"env-identity (#3723): textual-flowview is a local working copy, not "
            f"the pinned commit — acknowledged via REYN_FLOWVIEW_LOCAL_COPY={reason!r}. "
            f"This run's result is not comparable to one measured against the pin.\n"
            f"{rendered}",
            stacklevel=1,
        )

    if blocking:
        rendered = "\n".join(f.render() for f in blocking)
        pytest.exit(
            f"env-identity (#3723): this venv's textual-flowview does not match "
            f"pyproject.toml's pin — the whole suite's result is not trustworthy "
            f"until this is fixed.\n{rendered}",
            returncode=1,
        )


# ── Workspace isolation (#3705) ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """Every test starts chdir'd into its own throwaway ``tmp_path``.

    #3705: the owner opened `reyn` and found 656/1158 (56%) of their real
    conversation history was synthetic test fixtures — 67 `.reyn/agents/`
    directories that should never have existed outside a test's own
    isolated tmp dir. Root cause: `Agent.workspace_dir` (and several
    Session-owned paths derived from it) fall back to a cwd-relative
    `.reyn/...` when a caller does not supply an explicit
    `workspace_state_dir` — and two DIFFERENT test helpers
    (`tests/_support/session.py`, `tests/_support/agent_session.py`, the
    latter used by 223 test files) were each independently found to
    sometimes take that fallback path (measured: ~16/223 of
    `agent_session.make_session`'s own call sites pass
    `workspace_state_dir` explicitly — the other ~207 rely on it).

    Fixing every individual call site is an N+1 problem: the NEXT test
    helper, or the next call site added to an existing one, inherits the
    exact same "forgot to pass workspace_state_dir" failure mode — which is
    precisely how this incident happened twice in one review round (a
    helper lead-coder found, then a second ~16/223 gap this session found
    independently). This fixture closes the CLASS instead: it does not make
    every call site pass an explicit root — it makes the FALLBACK ITSELF
    safe, unconditionally, for every test, regardless of which helper or
    call site takes it. A cwd-relative `.reyn/...` write can now land only
    under this test's own disposable `tmp_path`, never the directory the
    test process happened to be started from.

    A test that goes RED under this fixture has been cwd-dependent all
    along (owner + lead-coder's framing, #3705 review) — that is the
    fixture doing its job, not a reason to opt out of it. A test with a
    genuine, deliberate reason to control its OWN cwd (e.g. one exercising
    `_find_project_root`'s upward walk, or the RED-gate pair in
    `test_session_writes_stay_in_its_workspace_3705.py`, which chdir's
    again mid-test to a DIFFERENT directory than this fixture's own
    `tmp_path`) still can — `monkeypatch.chdir(...)` calls happening later
    in the SAME test simply override this fixture's earlier one, same as
    any other autouse fixture a test's own setup legitimately shadows.

    A DIFFERENT, rarer case is a test that needs cwd to stay the REAL repo
    root for its own duration — e.g. reading a COMMITTED doc/config file by
    a repo-relative path (`Path("docs/...").read_text()`), which has
    nothing to do with `.reyn`-write isolation. Marking it
    `@pytest.mark.repo_root_cwd(reason="...")` opts out explicitly, with a
    REQUIRED reason (enforced below) — never a silent bypass; the whole
    point of a marker over an ad-hoc `monkeypatch.chdir(repo_root)` in each
    such test is that every opt-out is greppable in one place, with its
    justification attached, rather than rediscovered file-by-file.
    """
    marker = request.node.get_closest_marker("repo_root_cwd")
    if marker is not None:
        if not marker.args and "reason" not in marker.kwargs:
            pytest.fail(
                f"{request.node.nodeid}: @pytest.mark.repo_root_cwd(...) "
                "requires a reason=\"...\" — opting out of cwd isolation "
                "must be justified, not silent (#3705)."
            )
        return
    monkeypatch.chdir(tmp_path)


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
    ``tests/interfaces/test_markdown_palette_gate_3469.py``'s ``_memo_cleared_theme`` for
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


@pytest.fixture(autouse=True)
def _clear_find_project_root_cache() -> Iterator[None]:
    """Reset `_find_project_root`'s process-global cache before AND after
    every test (#3681).

    `_find_project_root` (`reyn.config.loader`) is now `lru_cache`d, keyed on
    the resolved starting path, so a single `reyn` process walks the
    filesystem once per distinct starting directory instead of once per
    caller (#3671 P4 item A-3: `reyn chat` alone called it 3x for the same
    cwd — interactive-logging setup, `load_config()`,
    `build_environment_backend()`).

    Cleared on BOTH sides, not just after: a fixture that only clears in its
    teardown leaves every worker's FIRST test running against whatever the
    cache picked up during collection-time / session-scoped fixture setup —
    the one point an after-only clear cannot reach, since nothing ran this
    fixture's teardown yet.

    Safe in production TODAY: a `reyn` process's own `reyn.yaml` ancestry
    does not change mid-run — verified by checking every command that WRITES
    `reyn.yaml` — only `reyn init` (`interfaces/cli/commands/init.py`) —
    prints and exits immediately after, never querying `_find_project_root`
    again in the same process (verified by grep: no other command writes
    `reyn.yaml`, and `init.py` itself never calls `_find_project_root` or
    `load_config`).
    This is an observation about the CURRENT command set, not an invariant:
    a future command that writes `reyn.yaml` and then CONTINUES running
    (unlike `init`'s write-then-exit) would need its own explicit
    `_find_project_root_uncached.cache_clear()` call at that write site.

    NOT safe across a whole pytest session sharing one interpreter, where a
    `tmp_path`-based test could in principle collide with a stale cached
    miss from an earlier test's walk over the same absolute path (unlikely
    in practice — pytest's `tmp_path` is unique per test — but not something
    to leave to chance given how cheaply it is closed). Same shape as
    `_isolate_rich_style_ansi_memo` above, for the same reason: a process-
    global cache leaking across tests is bugs waiting for the wrong pair of
    tests to run adjacently, not a bug already reproduced.
    """
    from reyn.config.loader import _find_project_root_uncached
    _find_project_root_uncached.cache_clear()
    yield
    _find_project_root_uncached.cache_clear()

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
    ``tests/scripts/test_3233_inprocess_env_identity.py``'s decoy witness."""
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
    config.addinivalue_line(
        "markers",
        "repo_root_cwd(reason): opt OUT of the autouse `_isolated_cwd` fixture "
        "(#3705) — this test genuinely needs cwd to be the real repo root "
        "(e.g. reading a COMMITTED doc/config file by repo-relative path), not "
        "a disposable tmp_path. `reason` is required (enforced by "
        "`_isolated_cwd` itself) so an opt-out is never silent — a red test "
        "under the autouse chdir means it WAS cwd-dependent; this marker is for "
        "the (rare) cases where that dependency is deliberate and correct, not "
        "a way to make an inconvenient red go away.",
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

    # #5283: unconsumed-LLMReplay-entry gate — same lazy-import-inside-the-
    # hook-body reason as network_gate immediately above.
    from reyn.dev.testing import replay_unconsumed

    replay_unconsumed.pytest_configure(config)

    # #3872: a per-process memory ceiling. A test reached ~10 GB and cost the
    # operator three reboots; nothing on the machine stopped it, and macOS does
    # not enforce RLIMIT_AS. Started here so every pytest run carries it without
    # anyone remembering to ask for it.
    from reyn.dev.testing import memory_ceiling

    memory_ceiling.pytest_configure(config)

    # #4986: CI teardown-hang stall dump — opt-in (REYN_STALL_TRACE_CI unset
    # means this is a no-op), see that module's own docstring for why a
    # session-spanning watchdog is needed at all.
    from reyn.dev.testing import stall_dump

    stall_dump.pytest_configure(config)


def pytest_runtest_setup(item: pytest.Item) -> None:
    from reyn.dev.testing import memory_ceiling, network_gate

    network_gate.pytest_runtest_setup(item)
    memory_ceiling.pytest_runtest_setup(item)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    from reyn.dev.testing import network_gate

    network_gate.pytest_runtest_teardown(item, nextitem)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    from reyn.dev.testing import (
        extra_skip_report,
        network_gate,
        replay_unconsumed,
        stall_dump,
    )

    network_gate.pytest_sessionfinish(session, exitstatus)
    extra_skip_report.pytest_sessionfinish(session, exitstatus)
    replay_unconsumed.pytest_sessionfinish(session, exitstatus)
    # #4986: cancel LAST — a session-teardown hang (the exact case this
    # watchdog exists to catch) must not have its timer cancelled by an
    # earlier sessionfinish hook's own side effect finishing first; this
    # call itself is cheap and unconditional either way.
    stall_dump.pytest_sessionfinish(session, exitstatus)


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
    mode: str = "record" if force_record else "replay"

    # #3662: a missing fixture file used to auto-activate record mode — the
    # convenience of "write a new @replay test, run it, fixture appears"
    # doubled as a silent fallback to a real, unauthorized network call for
    # a fixture missing for any OTHER reason. Fail loud here, before
    # `LLMReplay.install()` ever runs, with the exact next command — first-run
    # creation is now the SAME explicit step as re-recording.
    if not force_record and not fixture_path.exists():
        pytest.fail(
            f"Replay fixture not found: {fixture_path}\n"
            f"Re-run with: REYN_LLM_RECORD=1 python -m pytest {request.node.nodeid}\n"
            "(this makes a real LLM call and writes the fixture — #3662: a "
            "missing fixture no longer records automatically)."
        )

    from reyn.dev.testing.replay import LLMReplay

    replay = LLMReplay(fixture_path, mode=mode)  # type: ignore[arg-type]
    replay.install()
    try:
        yield replay
    finally:
        replay.restore()
        if mode == "record":
            replay.flush()
        else:
            # #5283: report this instance's own (loaded, consumed) key sets
            # to the shared cross-worker events file — record mode is a
            # different question (#3634's own in-place-replace dedup), so
            # it never reports here. See replay_unconsumed's module
            # docstring for why fail-open gates the actual check on this
            # data, not the reporting itself (reporting is always cheap and
            # unconditional; only pytest_sessionfinish's own read-back is
            # gated on REYN_REPLAY_UNCONSUMED_CHECK=1).
            from reyn.dev.testing import replay_unconsumed

            replay_unconsumed.report_instance(
                str(fixture_path), replay.loaded_keys(), replay.consumed_keys(),
            )
