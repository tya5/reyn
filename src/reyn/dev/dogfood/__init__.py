"""Dogfood scenario framework (FP-0036).

Components:
  scenarios   — ScenarioSet / Scenario schema + loader (F1)
  verifiers/  — reply / events verifiers (F3)
  coverage    — feature-map coverage matrix (F4)
  replay      — LLMReplay fixture integration (F5)
  runner      — scenario runner + RunResult (F2, this slice)
  compare     — baseline vs candidate regression compare (F2, this slice)
"""
from .scenarios import (
    EventAssertion,
    ExpectedEvents,
    ExpectedReply,
    OutcomePrediction,
    Scenario,
    ScenarioLoadError,
    ScenarioSet,
    load_scenario_set,
)

__all__ = [
    "ScenarioSet",
    "Scenario",
    "ExpectedReply",
    "ExpectedEvents",
    "EventAssertion",
    "OutcomePrediction",
    "ScenarioLoadError",
    "load_scenario_set",
]
