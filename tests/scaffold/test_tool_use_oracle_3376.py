# scaffold: triggered_by="#3376 P3 lands the last (scheme x transport) cell, after which no PR in the arc still has to show the 4 pre-existing cells unchanged"
# scaffold: removed_by="The PR that closes #3376"
# scaffold: note="Re-pointed by the P1 seam PR. The original trigger named P1, but the
#   arc did not end there: P2 adds the (category x content_fence) cell and P3 the
#   remaining ones, and each of those still has to show that the 4 cells recorded
#   here did not move. Deleting the artifact at P1 would leave the two PRs with the
#   most cell-composition churn with no byte-identical target at all. Re-pointing is
#   a visible decision; a trigger left silently stale is not."
"""Tier 1: the #3376 P1 byte-identical gate — the 4 registered ``(scheme x
transport)`` cells still produce exactly the ``(llm_tools_payload,
tool_use_sp)`` recorded in ``tool_use_oracle_3376.json``.

This is the characterization half of the harness in ``tool_use_oracle_3376.py``
(read its module docstring for how the real ``SchemeOps`` object graph is built
and what is pinned). It is the snapshot-test exception the testing policy grants
scaffolding for legacy-refactor characterization: it exists only to make "P1
changed nothing" a measured claim, and dies with P1.

The comparison runs the capture in a **fresh subprocess** rather than in-process:
the failure class this oracle guards against (#3385 — litellm's provider
transforms rewriting reyn's canonical schema constants in place) is invisible to
an in-process re-capture, which would share the very constants that get
corrupted. ``out_of_process_reyn`` pins that subprocess's ``reyn`` resolution to
this checkout (#3024).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SCAFFOLD_DIR = Path(__file__).resolve().parent
if str(_SCAFFOLD_DIR) not in sys.path:
    sys.path.insert(0, str(_SCAFFOLD_DIR))

import tool_use_oracle_3376 as oracle  # noqa: E402

from reyn.tools.transport import valid_scheme_transport_pairs  # noqa: E402


def _recorded() -> dict:
    return json.loads(oracle.ORACLE_PATH.read_text(encoding="utf-8"))


def test_live_capture_matches_the_recorded_oracle(out_of_process_reyn) -> None:
    """Tier 1: a fresh-process capture is byte-identical to the recorded artifact.

    Post-P1 this is the whole acceptance criterion for "the Exposure/Encoder seam
    changed no cell's output". Pre-P1 it is the guard that keeps the artifact
    honest while the seam is being built."""
    live = oracle.capture_in_subprocess(out_of_process_reyn)
    recorded = oracle.ORACLE_PATH.read_text(encoding="utf-8")
    assert json.loads(live) == json.loads(recorded), (
        "the live (scheme x transport) presentations diverged from the recorded "
        f"#3376 oracle ({oracle.ORACLE_PATH}). If this is P1's seam landing, the "
        "divergence IS the regression; if it is an intended behaviour change, "
        "re-capture with `python tests/scaffold/tool_use_oracle_3376.py` and say so."
    )
    # Parsed equality first (a readable diff on a 200 kB artifact), then the
    # literal bytes — both sides come from the same ``canonical_json`` writer, so
    # "byte-identical" is checkable rather than approximated by value equality.
    assert live.rstrip("\n") == recorded.rstrip("\n")


def test_oracle_covers_every_registered_cell() -> None:
    """Tier 1: coverage is enumerated from the LIVE registry, not hand-listed.

    A cell added to ``_VALID_SCHEME_TRANSPORT_PAIRS`` without a recorded oracle
    entry would otherwise sail past the gate above (which only compares what was
    captured). Coverage counts ``CAPTURED`` entries only — a cell present solely as
    an ``UNCAPTURED`` declaration is not covered. Vacuity guard: the registry must
    be non-empty."""
    pairs = valid_scheme_transport_pairs()
    assert pairs, "the (scheme, transport) registry is empty — this gate would be vacuous"

    recorded_cells = oracle.captured_cells(_recorded()["cells"]).values()
    covered = {(c["scheme"], c["transport"]) for c in recorded_cells}
    missing = {(s, t.value) for s, t in pairs} - covered
    assert not missing, f"registered cells with no recorded oracle entry: {sorted(missing)}"


def test_an_uncaptured_branch_is_declared_in_the_data() -> None:
    """Tier 1: at least one branch is marked ``UNCAPTURED``, in the artifact itself.

    The witness is on the *count*, not on a named branch, so it survives the branch
    being renamed and still fires the day someone starts claiming full capture.
    When the excluded branch genuinely becomes capturable and the count reaches
    zero, this RED is the signal to capture it and update the acceptance condition
    below — not a nuisance to delete (same shape as #3347: put the witness on the
    size rather than trusting the removal).

    Why in the data and not in prose: "omitted" and "excluded" are
    indistinguishable in a data file, so a silently absent branch reads as "this
    cell has no such branch" — the empty-vs-absent ambiguity this arc has been
    closing. Each ``UNCAPTURED`` entry must therefore also carry a reason and must
    NOT carry a payload, so it can never be mistaken for a capture that came back
    empty."""
    cells = _recorded()["cells"]
    uncaptured = oracle.uncaptured_cells(cells)
    assert uncaptured, (
        "no UNCAPTURED entry: either a declared exclusion was dropped, or a branch "
        "became capturable — capture it and widen the acceptance scope."
    )
    for key, entry in uncaptured.items():
        assert entry.get("reason"), f"{key}: UNCAPTURED without a reason"
        assert "llm_tools_payload" not in entry, f"{key}: UNCAPTURED must carry no payload"
        assert "tool_use_sp" not in entry, f"{key}: UNCAPTURED must carry no payload"

    # Acceptance scope, pinned: the byte-identical guarantee covers CAPTURED only.
    assert oracle.captured_cells(cells), "no CAPTURED entry — the oracle is empty"
    assert set(cells) == set(oracle.captured_cells(cells)) | set(uncaptured), (
        "a cell entry carries neither CAPTURED nor UNCAPTURED status"
    )


def test_recorded_payloads_match_the_canonical_definitions() -> None:
    """Tier 1: every recorded schema equals its canonical source (#3383/#3385).

    Leans on the landed ``parameters_for_export`` projection rather than
    re-deriving one. Anti-vacuity: the comparison count must be non-zero, so an
    artifact whose names all failed to resolve cannot pass silently."""
    captured = oracle.captured_cells(_recorded()["cells"])
    payloads = {k: v["llm_tools_payload"] for k, v in captured.items()}
    counts = oracle.assert_schemas_match_canonical(payloads)
    assert counts["compared"] > 0, counts


def test_recorded_payloads_carry_no_3383_transform_damage() -> None:
    """Tier 1: the artifact carries neither #3383 damage fingerprint.

    Independent of the arm above, not redundant with it: if a canonical constant
    were itself corrupted, an equality check would compare corrupted against
    corrupted and pass. Anti-vacuity: an artifact containing no ``const`` node
    and no ``oneOf`` variant would make both fingerprint arms pass without
    inspecting anything."""
    captured = oracle.captured_cells(_recorded()["cells"])
    payloads = {k: v["llm_tools_payload"] for k, v in captured.items()}
    counts = oracle.assert_schemas_pristine(payloads)
    assert counts["const_nodes"] > 0 and counts["oneof_variants"] > 0, counts


def test_content_fence_cell_records_the_empty_payload_verbatim() -> None:
    """Tier 1: the ``llm_tools_payload == []`` ambiguity is recorded, not resolved.

    ``codeact.py`` expresses "this field does not apply to my transport" as an
    empty list — indistinguishable from "no tools to show". P1's Exposure/Encoder
    split intends to remove that ambiguity; until then the oracle must carry the
    empty list as the current fact, and the whole tool-use surface for this cell
    must live in ``tool_use_sp`` (a rendered code-API string, not a slot-map)."""
    cell = _recorded()["cells"]["enumerate-all|content_fence"]
    assert cell["llm_tools_payload"] == []
    assert isinstance(cell["tool_use_sp"], str) and cell["tool_use_sp"]


def test_enumerate_all_transport_asymmetry_is_recorded_not_smoothed() -> None:
    """Tier 1: the #3381 base-tools asymmetry is preserved in the artifact.

    The ``enumerate-all`` name spans two cells that expose DIFFERENT tool
    populations: the ``tool_calls`` cell merges ``base_tools`` + the catalog,
    while the ``content_fence`` (CodeAct) cell renders ``catalog_entries`` only.
    Architect's #3376 ruling: probably a gap, but P1 must not fix it — fixing it
    would add callables to CodeAct's system prompt and break P1's own criterion.
    This arm makes an accidental "correction" fail loudly here rather than pass
    as an improvement."""
    cells = _recorded()["cells"]
    tool_calls_names = {
        t["function"]["name"] for t in cells["enumerate-all|tool_calls"]["llm_tools_payload"]
    }
    # The code-API declares each callable as ``- `def <name>(...)` `` — match the
    # DECLARATION, not a bare substring: "delegate_to_agent" also occurs inside
    # ``multi_agent__delegate``'s prose description, where it is a cross-reference,
    # not an exposed callable.
    declared = set(re.findall(r"`def (\w+)\(", cells["enumerate-all|content_fence"]["tool_use_sp"]))
    assert declared, "the code-API declared no callables — arm is vacuous otherwise"

    base_only = [n for n in ("delegate_to_agent", "read_memory_body") if n in tool_calls_names]
    assert base_only, "expected base-tool names in the tool_calls cell — arm is vacuous otherwise"
    for name in base_only:
        assert name not in declared, (
            f"{name} is now a callable in the CodeAct code-API. That is the #3381 "
            "asymmetry being fixed — a behaviour change P1 explicitly excludes."
        )
