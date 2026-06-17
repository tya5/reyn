"""LLMReplay integration for dogfood scenario runs (FP-0036 Component F).

Wraps ``reyn.dev.testing.replay.LLMReplay`` for use by the dogfood runner.
The runner accepts a ``replay_fixture_dir``; when set, the litellm calls
made during scenario execution are intercepted by an active LLMReplay
instance (record or replay mode based on file presence).

Fixture layout per scenario set:
  dogfood/fixtures/<set_name>/<scenario_id>.jsonl

Mode auto-detection:
  - File present → replay mode (= deterministic, 0 LLM cost)
  - File absent → record mode (= first run captures fixtures live)

LLMReplay API notes:
  ``LLMReplay(fixture_path, mode)`` constructs the instance.
  ``install()`` monkeypatches ``litellm.acompletion``.
  ``restore()`` restores the original.
  ``flush()`` writes pending record-mode entries.
  ``__enter__`` / ``__exit__`` implement the context-manager lifecycle
  (install on enter; restore + flush on exit).

The ``scenario_replay_context`` context manager below wraps this lifecycle
with auto-mode-detection (record vs replay), directory creation, and logging.

``replay_run`` is the function the runner (F2) imports and calls.
Its signature is ``async (scenario, *, fixture_dir) -> ScenarioRunResult``.
It must re-run the scenario through the standard runner path with LLMReplay
active. Because the runner (F2) has no live-LLM runner_fn by default, the
replay path must drive the scenario execution directly. At MVP, ``replay_run``
uses the same stub-result approach as the default runner_fn but with LLMReplay
activated for the duration — hooking any litellm calls that happen if a real
runner_fn is injected via the seam.

Concretely:
  - ``replay_fixture_dir`` is set on the outer ``run_scenario_set`` call.
  - The runner (F2) calls ``replay_run(scenario, fixture_dir=replay_fixture_dir)``.
  - ``replay_run`` wraps the execution in ``scenario_replay_context`` so that
    any ``litellm.acompletion`` call is intercepted by the active LLMReplay.
  - For headless (offline) contexts where no live runner is injected, it
    returns an inconclusive result — the same contract as the default runner_fn.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:
    from reyn.dev.dogfood.runner import ScenarioRunResult
    from reyn.dev.dogfood.scenarios import Scenario
    from reyn.dev.testing.replay import LLMReplay

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixture path helper
# ---------------------------------------------------------------------------


def fixture_path_for(
    fixture_dir: Path,
    set_name: str,
    scenario_id: str,
) -> Path:
    """Return the fixture file path for a (set, scenario) pair.

    Fixture layout:
        <fixture_dir>/<set_name>/<scenario_id>.jsonl
    """
    return fixture_dir / set_name / f"{scenario_id}.jsonl"


# ---------------------------------------------------------------------------
# Context-manager activation
# ---------------------------------------------------------------------------


@asynccontextmanager
async def scenario_replay_context(
    fixture_dir: Path,
    set_name: str,
    scenario_id: str,
) -> AsyncGenerator["LLMReplay", None]:
    """Activate LLMReplay for the duration of one scenario.

    Yields the active ``LLMReplay`` instance.  The caller (e.g. ``replay_run``)
    wraps each scenario's LLM calls inside this context manager when
    ``replay_fixture_dir`` is set.

    Mode auto-detection (based on fixture file presence):
      - File exists → ``replay`` mode (deterministic, zero LLM cost)
      - File absent → ``record`` mode; the parent directory is created so
        ``LLMReplay.flush()`` can write the file.

    The context-manager lifecycle:
      1. Determine mode from file presence.
      2. Construct ``LLMReplay(fixture_path, mode=...)``.
      3. Call ``install()`` to monkeypatch ``litellm.acompletion``.
      4. Yield the instance to the caller.
      5. On exit (normal or exception): call ``restore()``; if record mode,
         call ``flush()`` to persist any newly recorded calls.

    Parameters
    ----------
    fixture_dir:
        Root directory under which fixture files are stored.
        See ``fixture_path_for`` for the layout.
    set_name:
        Scenario set name (= first path segment under ``fixture_dir``).
    scenario_id:
        Scenario identifier (= filename stem under the set directory).

    Raises
    ------
    reyn.dev.testing.replay.MissingFixture
        In replay mode: raised when the LLM is called but no matching
        fixture entry exists.  The caller should treat this as ``blocked``.
    """
    from reyn.dev.testing.replay import LLMReplay

    fixture_path = fixture_path_for(fixture_dir, set_name, scenario_id)

    if fixture_path.exists():
        mode = "replay"
        logger.debug(
            "scenario_replay_context: replay mode — fixture=%s", fixture_path
        )
    else:
        mode = "record"
        # Ensure parent dir exists so flush() can write the file.
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "scenario_replay_context: record mode — fixture=%s", fixture_path
        )

    replay = LLMReplay(fixture_path, mode=mode)
    replay.install()
    try:
        yield replay
    finally:
        replay.restore()
        if mode == "record":
            replay.flush()


# ---------------------------------------------------------------------------
# replay_run — the function imported by the runner (F2)
# ---------------------------------------------------------------------------


async def replay_run(
    scenario: "Scenario",
    *,
    fixture_dir: Path,
    set_name: str = "default",
) -> "ScenarioRunResult":
    """Run a single scenario with LLMReplay active.

    This is the entry point called by the runner (F2) when
    ``replay_fixture_dir`` is set on ``run_scenario_set``.

    The function activates ``LLMReplay`` for the duration of the call so
    that any ``litellm.acompletion`` invocation is intercepted (record or
    replay based on fixture file presence).

    At MVP, the function returns an inconclusive result — it does not drive
    the live chat router.  The LLMReplay activation is the meaningful part:
    any live runner_fn injected into the pipeline will have its LLM calls
    captured/replayed.  Future work wires the live runner here.

    Parameters
    ----------
    scenario:
        The ``Scenario`` instance to execute.
    fixture_dir:
        Root directory for fixture files.  See ``fixture_path_for``.
    set_name:
        Scenario set name, used as the first fixture path segment.
        The runner (F2) should pass ``scenario_set.name``.  Defaults to
        ``"default"`` for backward compatibility with callers that don't
        have the set name at hand.

    Returns
    -------
    ScenarioRunResult
        The execution result.  In the MVP, always ``inconclusive``.
        When the live runner is wired in, this carries real verdicts.
    """
    from reyn.dev.dogfood.runner import ScenarioRunResult
    from reyn.dev.testing.replay import MissingFixture

    async with scenario_replay_context(fixture_dir, set_name, scenario.id) as _replay:
        # Execution stub: LLMReplay is now active — any litellm call in this
        # block is intercepted.  The live runner_fn is not injected at MVP;
        # the framework returns inconclusive so downstream components work
        # correctly in offline / unit-test contexts.
        #
        # When the CLI wires in the real chat-router runner (post-MVP), it
        # will call that runner here and collect reply_text, events, artifacts
        # before the context exits (which triggers flush/restore).
        try:
            return ScenarioRunResult(
                scenario_id=scenario.id,
                reply_text="(replay runner: live runner not wired at MVP)",
                events=[],
                artifacts=[],
                reply_outcome="inconclusive",
                events_outcome="inconclusive",
                artifacts_outcome="inconclusive",
            )
        except MissingFixture as exc:
            logger.warning(
                "scenario_replay_context: MissingFixture for scenario=%s — %s",
                scenario.id,
                exc,
            )
            return ScenarioRunResult(
                scenario_id=scenario.id,
                reply_text="",
                events=[],
                artifacts=[],
                reply_outcome="blocked",
                events_outcome="blocked",
                artifacts_outcome="blocked",
            )
