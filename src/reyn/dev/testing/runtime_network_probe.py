"""Runtime (not static) gate: one full router turn, under reyn's DEFAULT
config, reaches zero real sockets (#4421 step ③).

WHY A RUNTIME GATE, NOT JUST THE #4428 AST WALK
    #4428 closed literal ``import litellm`` / ``from litellm import ...``
    outside the seam (``litellm_bootstrap.py``) — a STATIC gate, walking
    source text. It is blind by construction to code that reaches litellm
    through ``sys.modules["litellm"]`` or ``getattr(module, name)`` — no
    import statement exists for an AST walk to see (architect's #4415
    recount hit exactly this blind spot once already, counting via AST and
    missing an attribute-access site).

    This module does not try to out-guess every such spelling. It instead
    measures the OUTCOME both spellings share when they misbehave: a real
    socket connection during a turn reyn's own contract says should stay
    fully local (a completion served from a fixture/replay, not a live
    provider). A mechanism gate over the outcome covers every syntax that
    could produce it — the trade reyn's own architect named for this exact
    axis: "静的（コードに全数・機構に盲目）と実行時（機構に全数・経路に盲目）
    の二枚が要る" — this is the second sheet, not a replacement for the first.

WHY THIS NEEDS A SUBPROCESS, NOT AN IN-PROCESS PATCH
    A pytest worker process runs many tests; by the time any single test
    calls this, ``litellm`` may already sit in ``sys.modules`` from an
    earlier test's import, so re-importing it in-process is a no-op and
    measures nothing about import-time network reach. ``NetworkReachProbe``
    is written to be usable from a **spawned subprocess script** (`python
    -c "..."`), started before ``litellm`` (or even ``reyn``) has ever been
    imported in that process — see ``tests/llm/test_4421_runtime_network_
    gate.py`` for the actual spawn, and
    ``tests/llm/test_litellm_lazy_load.py`` for the established subprocess
    pattern this reuses (real subprocess, not a mock of ``sys.modules``).

WHAT COUNTS AS "REACH"
    ``socket.socket.connect``/``connect_ex`` — the one chokepoint every
    Python HTTP client (``requests``, ``httpx``, ``aiohttp``, litellm's own
    transports) ultimately calls to open a real TCP connection, regardless
    of which higher-level library or call spelling got there. Patched at
    this level (not ``litellm.acompletion``/``aembedding``, which
    ``reyn.dev.testing.network_gate`` already wraps for pytest-pinned
    tests) so this catches litellm's OWN import-time / first-call network
    reach too, not just reyn's calls into litellm — measured directly
    (2026-08-13): a naive probe that imports ``litellm`` before ``reyn``
    (skipping ``reyn/__init__.py``'s own ``LITELLM_LOCAL_MODEL_COST_MAP``
    ``setdefault``) sees 8 real connect attempts to
    ``raw.githubusercontent.com`` (litellm's remote model-cost-map fetch,
    ``litellm_core_utils/get_model_cost_map.py``) purely from ``import
    litellm`` — a real, measured hazard this gate is built to keep caught,
    not a hypothetical.

A DENIED CONNECT, NOT A SILENTLY-ALLOWED ONE
    ``connect``/``connect_ex`` raise ``OSError`` on the attempt (mirroring
    what an offline CI runner would do anyway) rather than letting it
    through — a probe that only counted successful connections would pass
    on a machine with no network path to the target and never notice the
    attempt was made at all.
"""
from __future__ import annotations

import socket
from typing import Any


class RealNetworkReachDuringOneTurn(RuntimeError):
    """A real socket connect was attempted during a probed turn.

    Per #4421's landing condition (architect, non-negotiable): the message
    must NAME REYN'S OWN REMEDY, not just report that a third-party network
    reach occurred — "an outbound call happened" alone tells the next
    reader nothing about whose problem it is (Q1's "third-party promise"
    shape, applied to a runtime gate instead of a test assertion).
    :meth:`NetworkReachProbe.assert_clean` builds this message; do not
    raise this class directly with a bare description.
    """


class NetworkReachProbe:
    """Context manager: patch ``socket.socket.connect``/``connect_ex`` to
    record (and block) every attempt for the duration of the ``with`` block.

    Usable standalone (construct, use as a context manager, read
    ``.attempts`` or call :meth:`assert_clean`) — no pytest dependency, so a
    subprocess script spawned via ``python -c`` can import and use it
    directly.
    """

    def __init__(self) -> None:
        self.attempts: list[tuple[str, Any]] = []
        self._orig_connect: Any = None
        self._orig_connect_ex: Any = None

    def __enter__(self) -> "NetworkReachProbe":
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex

        def _record_connect(_sock_self: Any, address: Any) -> None:
            self.attempts.append(("connect", address))
            raise OSError(f"NetworkReachProbe: blocked real connect to {address!r}")

        def _record_connect_ex(_sock_self: Any, address: Any) -> int:
            self.attempts.append(("connect_ex", address))
            raise OSError(f"NetworkReachProbe: blocked real connect_ex to {address!r}")

        socket.socket.connect = _record_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _record_connect_ex  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc_info: object) -> None:
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig_connect_ex  # type: ignore[method-assign]

    def assert_clean(self, *, context: str) -> None:
        """Raise :class:`RealNetworkReachDuringOneTurn` (naming reyn's own
        known suppression points) if any attempt was recorded; no-op
        otherwise. Call after the probed turn completes."""
        if not self.attempts:
            return
        detail = "; ".join(f"{kind} -> {addr!r}" for kind, addr in self.attempts)
        raise RealNetworkReachDuringOneTurn(
            f"{context}: reyn's assumption that a default-configured run "
            f"reaches litellm with zero real network calls has become "
            f"invalid ({len(self.attempts)} attempt(s): {detail}).\n"
            "Reyn-side remedy: this almost certainly means a litellm "
            "import-time or first-call remote fetch is no longer "
            "suppressed by one of reyn's two known suppression points — "
            "check both: (1) `LITELLM_LOCAL_MODEL_COST_MAP=True`, set in "
            "`src/reyn/__init__.py` before anything else can `import "
            "litellm` (blocks litellm's own remote model-cost-map fetch, "
            "`litellm_core_utils/get_model_cost_map.py`); (2) the bundled "
            "tiktoken cache litellm ships (see `reyn._tiktoken_diag` / "
            "`reyn.llm.litellm_bootstrap._diagnose_import_failure_for_log`, "
            "#4422). If neither explains this attempt's target, litellm "
            "has grown a NEW remote-fetch surface reyn does not suppress "
            "yet — that surface is the actual reyn-side fix this gate is "
            "naming, not this test."
        )
