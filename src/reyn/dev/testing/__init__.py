"""reyn.dev.testing — test utilities for Reyn Agent OS.

Public surface:
  LLMReplay   record / replay LLM responses at the litellm.acompletion boundary.
  MissingFixture  raised on a replay-mode cache miss.
  PreconditionMismatch  raised when the scenario matched but the environment
                  it was captured under did not (#3473).
"""

from .replay import LLMReplay, MissingFixture, PreconditionMismatch

__all__ = ["LLMReplay", "MissingFixture", "PreconditionMismatch"]
