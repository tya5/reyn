"""reyn.runtime.usage_shim — token-usage adapter for the retry loop.

``_RouterUsageShim`` wraps a bare ``TokenUsage`` so it is readable as
``response.usage`` (the litellm response convention) by ``retry_loop``'s
learner-feedback path, which observes ``response.usage.prompt_tokens``.
Dependency-free value wrapper.
"""
from __future__ import annotations


class _RouterUsageShim:
    """Wrap a RouterLoop ``TokenUsage`` as ``.usage`` for ``retry_loop`` (#1125).

    ``retry_loop``'s learner-feedback path reads ``response.usage.prompt_tokens``
    (the litellm response convention). RouterLoop.run returns the bare
    ``TokenUsage`` instead, so this shim exposes it under ``.usage`` — letting
    the adaptive estimator observe the chat axis's actual prompt size. The
    overflow-recovery caller reads ``.usage`` back out to credit session usage.
    """

    __slots__ = ("usage",)

    def __init__(self, usage: object) -> None:
        self.usage = usage
