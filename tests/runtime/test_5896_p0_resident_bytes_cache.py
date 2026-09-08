"""#5896 P0 (owner-hit, 2026-09-07): ``Session._evict_oldest_resident_entries``
used to re-serialize EVERY still-resident ``ChatMessage`` on EVERY eviction
pass (every append) just to measure its size — on the owner's real machine a
single 369 MB row made that ONE re-serialize cost another 369 MB copy, on the
hot append path, contributing to a startup-time peak footprint of 8.2 GB.
``ChatMessage.resident_bytes()`` now caches each message's own serialized
size the first time it is asked.

Tier split: the pure-caching claim (a message's own size is computed once,
never re-serialized on repeat calls) is Tier 1 — it is a property of
``ChatMessage`` alone, no ``Session`` involved. The wiring claim (the real
eviction call site actually GETS that caching benefit across many real
append/evict cycles, not just that the method caches in isolation) is Tier 2.

No duration anywhere: every assertion here counts real ``json.dumps``
calls, never timing. The counter wraps the ACTUAL stdlib function (still
executes it — a spy, not a fake; testing.md: never fake a collaborator
when the real one is cheap). ``json.dumps`` is also called once per append
by ``Session._append_history``'s own durable-write line (unrelated to
this fix, unconditional every append) — the Tier 2 test below accounts
for that known, constant contribution explicitly rather than assuming
``resident_bytes()`` is the only caller (measured directly while building
this test: an earlier version of this counter wrapped
``ChatMessage.resident_bytes`` itself and checked its OWN cache attribute
before delegating to the real method — which cannot distinguish a guarded
cache from an unguarded one, since both eventually leave the SAME
attribute populated; that self-referential design silently passed even
with the guard fully removed, caught only by deliberately stripping the
guard and finding the test stayed green). Per lead-coder's own dispatch:
"duration を書かない（測るなら『json.dumps が呼ばれない』を見る）".
"""
from __future__ import annotations

import json as _json_module
from pathlib import Path

import pytest

from reyn.config.chat import HistoryResidentConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime import chat_message as chat_message_module
from reyn.runtime.chat_message import (
    ChatMessage,
    ResidentBytes,  # #5973 (c)4
)
from tests._support.agent_session import make_session


def _counting_json_dumps(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    """Wraps the REAL ``json.dumps`` (the exact name ``chat_message.py``
    calls) to count invocations without changing its behaviour. Patched
    on the shared ``json`` module object itself (``import json`` in any
    module returns the same singleton), so this counts every call in the
    process, not just ``chat_message.py``'s own — the Tier 2 test below
    accounts for the other known caller explicitly. Returns a 1-element
    mutable box the test reads afterward."""
    real_dumps = _json_module.dumps
    calls = [0]

    def _wrapped(*args: object, **kwargs: object) -> str:
        calls[0] += 1
        return real_dumps(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chat_message_module.json, "dumps", _wrapped)
    return calls


# ── 1. Tier 1: ChatMessage.resident_bytes() caches on its own ──────────────


def test_resident_bytes_computes_once_then_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 1: calling ``resident_bytes()`` N times on the SAME message
    calls the real ``json.dumps`` exactly once. No ``Session`` involved
    here, so no other caller of ``json.dumps`` is in play — the count is
    unambiguous.

    Strip witness: removing the ``if self._resident_bytes_cache is None:``
    guard in ``ChatMessage.resident_bytes`` (recomputing unconditionally)
    turns this red (``calls[0] == 3`` instead of ``1``) — verified
    directly, restored after."""
    calls = _counting_json_dumps(monkeypatch)
    msg = ChatMessage(role="user", content="x" * 500)

    first = msg.resident_bytes()
    second = msg.resident_bytes()
    third = msg.resident_bytes()

    assert first == second == third, "the cached value must be stable across calls"
    assert calls[0] == 1, (
        f"resident_bytes() must serialize this message ONCE, not on every "
        f"call; got {calls[0]} real json.dumps call(s)"
    )


def test_resident_bytes_matches_the_pre_fix_computation_exactly() -> None:
    """Tier 1: equivalence — ``resident_bytes()`` must produce the EXACT
    same number the old inline
    ``len(json.dumps(asdict(m), ensure_ascii=False).encode("utf-8"))``
    expression did, for a variety of message shapes (str content, list
    content, tool_calls, meta). This is the byte-identical witness
    lead-coder's dispatch required — a caching fix that quietly changed
    the VALUE would be worse than the bug it fixes."""
    import json
    from dataclasses import asdict

    messages = [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ]),
        ChatMessage(role="tool", content="result body", tool_call_id="c1", name="t"),
        ChatMessage(
            role="user",
            content=[{"type": "text", "text": "multimodal"}],
            meta={"wal_seq": 7, "custom": "value"},
        ),
        ChatMessage(role="summary", content="folded"),
    ]

    for m in messages:
        old_way = len(json.dumps(asdict(m), ensure_ascii=False).encode("utf-8"))
        new_way = m.resident_bytes()
        assert new_way == old_way, (
            f"resident_bytes() drifted from the pre-fix computation for "
            f"role={m.role!r}: old={old_way}, new={new_way}"
        )


def test_resident_bytes_cache_does_not_leak_into_asdict_or_equality() -> None:
    """Tier 1: the cache slot must be invisible to ``dataclasses.asdict``,
    ``==``, and ``repr`` — it is deliberately NOT a declared dataclass
    field (see ``ChatMessage.__init__``'s own comment). If it ever became
    one, ``resident_bytes()``'s own ``asdict(self)`` call would include
    the cache slot in what it measures, silently drifting the computed
    size from the equivalence test above."""
    from dataclasses import asdict

    a = ChatMessage(role="user", content="same")
    b = ChatMessage(role="user", content="same")
    a.resident_bytes()  # populate a's cache; b's stays unset

    assert a == b, "a populated cache must not affect dataclass equality"
    assert "_resident_bytes_cache" not in asdict(a), (
        "the cache slot leaked into asdict() output"
    )


# ── 2. Tier 2: the real eviction path gets the caching benefit ─────────────


def _session(tmp_path: Path, state_log: StateLog, *, max_bytes: int) -> "object":
    return make_session(
        agent_name="p0-resident-bytes-test", state_log=state_log,
        snapshot_path=tmp_path / "snap.json",
        history_resident_config=HistoryResidentConfig(max_bytes=ResidentBytes(max_bytes)),
    )


@pytest.mark.asyncio
async def test_real_eviction_never_reserializes_a_still_resident_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a message that survives MULTIPLE eviction passes (stays
    resident across several later appends before finally aging out) must
    still only ever be serialized ONCE across its whole resident lifetime
    — not once per eviction pass it survives.

    Expected total: ``2 * n_appends`` real ``json.dumps`` calls — ``n``
    from ``resident_bytes()`` (one per DISTINCT message, ever) plus ``n``
    from ``_append_history``'s own unrelated durable-write line
    (``f.write(json.dumps(history_record(msg), ...))``, unconditional,
    once per append, untouched by this fix). Any count ABOVE that means
    some still-resident message got re-measured on a later pass.

    Strip witness: reverting ``_evict_oldest_resident_entries``'s
    ``sizes = [m.resident_bytes() for m in self.history]`` back to the
    pre-fix inline ``json.dumps(asdict(m))`` list comprehension makes the
    total exceed ``2 * n_appends`` — verified directly, restored after."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    # Cap sized so several messages stay resident across MANY appends
    # (each ~150-250 bytes serialized, the dataclass carries many fields;
    # 300 bytes keeps ~1-2 resident at once, so the oldest of those
    # survives at least one extra eviction pass before it ages out —
    # exactly the repeat-measurement scenario the pre-fix code paid for
    # on every one of those passes).
    s = _session(tmp_path, state_log, max_bytes=300)

    calls = _counting_json_dumps(monkeypatch)

    n_appends = 20
    for i in range(n_appends):
        s._append_history(ChatMessage(role="user", content=f"turn {i}"))

    assert calls[0] == 2 * n_appends, (
        f"expected exactly {2 * n_appends} real json.dumps calls "
        f"({n_appends} from resident_bytes(), one per distinct message, "
        f"plus {n_appends} from the unrelated durable-write line) — got "
        f"{calls[0]}, which is more than the fixed total if any message "
        f"were being re-measured on a later eviction pass"
    )
