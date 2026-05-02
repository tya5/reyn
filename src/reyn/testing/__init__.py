"""reyn.testing — test utilities for Reyn Agent OS.

Public surface:
  LLMReplay   record / replay LLM responses at the litellm.acompletion boundary.
  MissingFixture  raised on a replay-mode cache miss.
"""

from .replay import LLMReplay, MissingFixture, REPLAY_DATETIME

__all__ = ["LLMReplay", "MissingFixture", "REPLAY_DATETIME"]
