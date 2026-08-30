"""Tier 2: #5580 — ``ChatMessage.spillability`` closes the persistence round
trip #5514 left open.

The crash (owner's real machine, verbatim traceback):
``AttributeError: 'str' object has no attribute 'value'`` in
``RouterHistoryBuffer.decompose_history_for_retry``
(``router_history_buffer.py:952``, ``getattr(_turn, "spillability",
Spillability.default()).value``).

#5514 closed the WRITE side (persisting ``spillability`` as its ``.value``
via ``asdict`` + ``json.dumps``) but not the READ side:
``Session._parse_history_line`` reconstructs a ``ChatMessage`` via
``ChatMessage(**raw)``, and pre-#5580 that passed the raw persisted
``str`` straight into ``self.spillability`` with no coercion back to the
``Spillability`` enum — invisible to any test that never restarted a
session and read ``history.jsonl`` back, exactly why #5547/#5558's own
TESTS-READ missed it (lead-coder's own disclosure, issue #5580).

Fix: ``ChatMessage.__init__`` now normalizes via
``chat_message._normalize_spillability`` — every construction path,
including a read-back raw dict, ends up holding a real ``Spillability``
member.

Accept criteria (lead-coder, #5580's own ordering):
① a value read back from a persisted (dict-shaped, plain-``str``
  ``spillability``) line ends up as the enum, not a ``str``.
② ``decompose_history_for_retry`` runs to completion against such a
  read-back message (the actual crash site) rather than raising.
③ an unrecognized string degrades to ``Spillability.default()`` without
  raising.
Plus the deny-side lead-coder explicitly warned ①-alone cannot rule out:
a "collapse EVERYTHING to default()" implementation would also pass ①/③
trivially — ``test_known_spillability_string_survives_round_trip_intact``
proves a REAL, non-default value is preserved distinctly, not forced to
default.

Real ``ChatMessage`` / real ``RouterHistoryBuffer`` throughout — no mocks
(CLAUDE.md mock ban), same construction pattern
``tests/runtime/test_build_history_producer_calls_2939.py`` already uses.
"""
from __future__ import annotations

from reyn.config.chat import CompactionConfig
from reyn.runtime.chat_message import ChatMessage, Spillability
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer


def _buffer(history: list, model: str = "gpt-5.6-luna") -> RouterHistoryBuffer:
    """Real RouterHistoryBuffer over a fixed history list — same minimal
    construction ``test_build_history_producer_calls_2939.py`` already
    establishes as sufficient for exercising ``decompose_history_for_retry``."""
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(),
        compaction_controller=None,
        model_fn=lambda: model,
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )


def _read_back(raw: dict) -> "ChatMessage | None":
    """Mirrors ``Session._parse_history_line``'s own construction call —
    the exact site #5580's fix lives at — without needing a real Session/
    on-disk file: a JSON round trip through ``json.dumps``/``json.loads``
    first, so *raw*'s ``spillability`` genuinely arrives as the plain
    ``str`` a persisted line would hold, not a Python object already
    carrying enum identity."""
    import json
    line = json.dumps(raw)
    reloaded = json.loads(line)
    return ChatMessage(**reloaded)


# ---------------------------------------------------------------------------
# ① read-back spillability is the enum, not a str
# ---------------------------------------------------------------------------


def test_read_back_spillability_is_an_enum_not_a_string():
    """Tier 2: #5580 accept ① — a ``spillability`` value read back from a
    persisted (plain-``str``) history line ends up as a real ``Spillability``
    member, not the raw ``str``."""
    msg = _read_back({
        "role": "user", "content": "hi", "spillability": "first_choice",
    })
    assert isinstance(msg.spillability, Spillability)
    assert msg.spillability == Spillability.FIRST_CHOICE
    # the exact operation the crash site performs — must not raise here.
    assert msg.spillability.value == "first_choice"


def test_known_spillability_string_survives_round_trip_intact():
    """Tier 2: #5580 accept ① deny-side (lead-coder's own warning: ①-alone
    would also pass under an implementation that collapses EVERY read-back
    value to default()) — proves a real, non-default value is preserved
    distinctly, not forced to default()."""
    msg = _read_back({
        "role": "assistant", "content": "x", "spillability": "never",
    })
    assert msg.spillability is Spillability.NEVER
    assert msg.spillability is not Spillability.default()


# ---------------------------------------------------------------------------
# ② decompose_history_for_retry runs against a read-back message
# ---------------------------------------------------------------------------


def test_decompose_history_for_retry_does_not_raise_on_read_back_history():
    """Tier 2: #5580 accept ② — ``decompose_history_for_retry`` (the actual
    crash site, ``router_history_buffer.py:952``) runs to completion
    against read-back ``ChatMessage``s instead of raising ``AttributeError:
    'str' object has no attribute 'value'``.

    The exact reproduction: a session restarted, read history.jsonl back
    (spillability arrives as plain str), then an overflow ladder ran."""
    history = [
        _read_back({
            "role": "user", "content": "long question " * 50,
            "spillability": "first_choice", "seq": 1,
        }),
        _read_back({
            "role": "assistant", "content": "long answer " * 50,
            "spillability": "last_resort", "seq": 2,
        }),
    ]
    buf = _buffer(history)
    # Pre-#5580 this line raised AttributeError: 'str' object has no
    # attribute 'value' (router_history_buffer.py:952).
    head, raw_middle, tail, summary, seq_by_id = buf.decompose_history_for_retry()
    all_wire = head + raw_middle + tail
    assert all_wire, "the fixture history must actually appear in the decomposition"
    for wire in all_wire:
        # #5514 §7-3: decompose annotates a plain-str .value onto the wire
        # dict — this is the POST-annotation shape, correctly a str here.
        assert isinstance(wire["spillability"], str)


# ---------------------------------------------------------------------------
# ③ an unrecognized string degrades to default(), never raises
# ---------------------------------------------------------------------------


def test_unknown_spillability_string_degrades_to_default():
    """Tier 2: #5580 accept ③ — an unrecognized ``spillability`` string
    (a future enum value this version doesn't know, or a corrupted history
    line) degrades to ``Spillability.default()`` rather than raising."""
    msg = _read_back({
        "role": "user", "content": "x", "spillability": "some_future_value",
    })
    assert msg.spillability is Spillability.default()


def test_corrupted_non_string_spillability_degrades_to_default():
    """Tier 2: #5580 accept ③ (non-string variant) — a history line could
    carry any JSON-serializable garbage in this key (a corrupted write, a
    hand-edited file); must degrade, not raise."""
    msg = ChatMessage(role="user", content="x", spillability=None)
    assert msg.spillability is Spillability.default()
