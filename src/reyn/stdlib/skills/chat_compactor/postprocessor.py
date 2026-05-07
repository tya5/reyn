"""Deterministic seq-aggregation postprocessor for the chat_compactor skill.

Pure-mode python step — runs sandboxed via reyn._python_harness. Reads the
LLM-produced ``chat_summary_raw`` artifact (with section content + the
verbatim ``new_turn_seqs`` list) and folds in the deterministic
``covers_through_seq`` field the LLM should not be trusted with on weak
models — getting it wrong corrupts ChatSession history (turn duplication
or loss).

The function preserves every field already present in ``data`` (so
section content like ``topic_arc`` / ``decisions`` etc. survive) and:

  - drops ``new_turn_seqs`` (it is a transit-only list, not part of the
    caller-facing schema)
  - adds ``covers_through_seq`` = ``max(new_turn_seqs)`` (or ``0`` when the
    list is empty / missing)

Edge cases:

- ``new_turn_seqs`` is missing or empty — returns ``covers_through_seq=0``.
  ChatSession falls back to ``candidates[-1].seq`` in that case (see
  ``src/reyn/chat/session.py`` ``_run_compactor``), so this is recoverable
  rather than catastrophic.
- ``new_turn_seqs`` contains non-monotonic / out-of-order values — ``max()``
  picks the largest regardless of order. The ChatSession slicer cares only
  about the highest seq covered, not the order in which turns appeared.
- ``new_turn_seqs`` entries are not strict ints (e.g. JSON yields floats) —
  ``int(s)`` coerces. The output_schema requires ``integer``, so a non-numeric
  value will fail validation cleanly rather than silently corrupt.
"""


def compute_covers_through_seq(artifact):
    """Return a new ``data`` dict with ``covers_through_seq`` derived from seqs.

    The caller writes the return value back at ``into: data``, so the dict
    must include every field the caller-facing schema requires. The
    transit-only ``new_turn_seqs`` field is dropped on the way out.
    """
    data = dict(artifact.get("data", {}) or {})
    seqs = data.pop("new_turn_seqs", None)
    if not seqs:
        data["covers_through_seq"] = 0
        return data
    data["covers_through_seq"] = max(int(s) for s in seqs)
    return data
