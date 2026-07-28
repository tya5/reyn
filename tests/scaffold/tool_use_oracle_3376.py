# scaffold: triggered_by="#3376 P3 lands the last (scheme x transport) cell, after which no PR in the arc still has to show the 4 pre-existing cells unchanged"
# scaffold: removed_by="The final PR of the #3376 arc"
# scaffold: note="Re-pointed by the P1 seam PR. The original trigger named P1, but the
#   arc did not end there: P2 adds the (category x content_fence) cell and P3 the
#   remaining ones, and each of those still has to show that the 4 cells recorded
#   here did not move. Deleting the artifact at P1 would leave the two PRs with the
#   most cell-composition churn with no byte-identical target at all. Re-pointing is
#   a visible decision; a trigger left silently stale is not."
"""Capture harness for the #3376 P1 correctness oracle — the ``(llm_tools_payload,
tool_use_sp)`` each of the 4 registered ``(scheme x transport)`` cells produces today.

P1 of the #3376 arc moves those 4 cells onto an Exposure/Encoder seam
**byte-identically — zero new behaviour**. "Byte-identical" is only a measured
claim if what they produce *today* was recorded before the seam existed; this
module is that recording, and the architect made it P1's entry condition
("oracle が取れるまで実装に入らないでください", #3376 comment 2).

**Real ``SchemeOps``, not a stand-in.** ``SchemeOps`` is a Protocol the
``RouterLoop`` itself implements (``router_loop.py`` § "#1593 SchemeOps
adapter") — ``base_tools`` / ``catalog_entries`` / ``present`` are RouterLoop
methods reading a real ``RouterLoopHost``. So the object graph here is a real
``Session`` -> its ``RouterHostAdapter`` -> a real ``RouterLoop`` -> the
registered scheme instance, and ``build_presentation`` is invoked exactly as
``RouterLoop.run`` invokes it. A Fake ``SchemeOps`` (the idiom the per-scheme
unit tests use, e.g. ``tests/test_enumerate_all_scheme_1593.py``) would record
what the harness composed, not what production composes — which is the one
thing this artifact must not do.

**What is pinned.** ``RouterLoop.run`` assembles ``available`` / ``layer_ctx``
inline from live turn state (context-size signal, hot-list aliases, resolved
model family, skill registry). Reproducing a turn would require an LLM call and
would make the oracle depend on turn state, so the two dicts are pinned here as
explicit literals (``PINNED_AVAILABLE`` / ``PINNED_LAYER_CTX``) carrying the
same keys ``run`` supplies. The *host-derived* half is not pinnable by literal —
it comes out of the Session — so ``_assert_host_inputs_are_pinned`` asserts by
measurement that the constructed host yields the declared values (no agents, no
MCP servers, no file-permission block, web-fetch allowed). Ambient config
leaking into the capture therefore fails loudly instead of silently changing the
artifact.

**Process cleanliness (#3383/#3385).** litellm's provider transforms rewrite the
``tools[]`` payload they are handed **in place**, and before #3385 that reached
reyn's canonical, module-level schema constants — so a rendered schema could
depend on what ran earlier in the process. An oracle captured in such a process
would bake the corruption in and P1 would then be validated against a corrupted
target. This module captures on top of #3385 (``a0365dfe``) and asserts
cleanliness on the artifact itself, with **two independent** checks — neither
subsumes the other:

- ``assert_schemas_match_canonical`` resolves every captured tool back to its
  ``ToolDefinition`` (directly, or through ``unwrapped_tool_name`` for a
  qualified ``<category>__<verb>`` catalog projection) and requires the captured
  ``parameters`` to equal ``parameters_for_export(definition.parameters)`` —
  #3385's single projection seam. Total equality, not a fingerprint.
- ``assert_schemas_pristine`` scans for the two damage fingerprints #3383
  measured (``type: object`` injected onto a ``const`` discriminator;
  ``additionalProperties: false`` deleted from a ``oneOf`` variant). This is NOT
  redundant: if the canonical constant were itself corrupted, the equality check
  above would compare corrupted against corrupted and pass. The fingerprint arm
  is the one that still fails in that world.

**Acceptance scope.** Every cell entry carries an explicit ``status``. P1's
byte-identical guarantee covers ``CAPTURED`` entries **only**; an ``UNCAPTURED``
entry records a branch that exists in the code but cannot be captured offline,
with its reason, *in the data*. Prose alone would not do: in a data file
"omitted" and "excluded" are indistinguishable, and a silently absent branch
reads as "this cell has no such branch" — the empty-vs-absent ambiguity this arc
keeps closing elsewhere. ``capture`` refuses to write an artifact with zero
``UNCAPTURED`` entries, so the day a branch becomes capturable the RED is the
signal to capture it and update this scope, not a nuisance to delete.

**Worktree identity (#3024/#3231).** Multiple agents share one venv here and its
editable install points at a different checkout, so ``assert_worktree_import``
verifies ``reyn`` resolved under *this* repo's ``src/`` before anything is
captured.

Run it directly to (re)capture::

    python tests/scaffold/tool_use_oracle_3376.py --out tests/scaffold/tool_use_oracle_3376.json
    python tests/scaffold/tool_use_oracle_3376.py --check   # determinism: N fresh subprocesses
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ORACLE_PATH = Path(__file__).with_suffix(".json")

# ── The pinned presentation inputs ────────────────────────────────────────────
#
# Same keys ``RouterLoop.run`` builds (router_loop.py, ``_scheme_available`` /
# ``_scheme_layer_ctx``), with every turn-varying value fixed to a literal.
#
# ``router_model``/``router_model_family``: "gpt-4o" is NOT in
# ``_discovery._WEAK_TIERS``, so ``tier_wants_discovery_mandate`` is False and
# the family is "other" (non-Claude ⇒ the operational-steering slot IS
# rendered). Both are recorded in the artifact so the next reader can tell which
# branch was captured.
# ``contextual_permission=None``: no narrowing ⇒ CodeAct renders the full
# code-API and no advertisement filter runs (``RouterLoop.run`` applies
# ``apply_contextual_visibility`` AFTER ``build_presentation``, so it is outside
# this seam either way).
PINNED_AVAILABLE: dict[str, Any] = {
    "hot_list_aliases": [],
    "contextual_permission": None,
}
PINNED_LAYER_CTX: dict[str, Any] = {
    "univ_enabled": True,
    "search_visible": False,
    "ctx_signal_present": False,
    "router_model": "gpt-4o",
    "router_model_family": "other",
    "non_interactive": False,
    "available_skills": None,
}

# Host-derived inputs, asserted by measurement rather than pinned by literal.
_EXPECTED_HOST_INPUTS: dict[str, Any] = {
    "list_available_agents": [],
    "get_file_permissions": None,
    "get_mcp_servers": [],
    "get_web_fetch_allowed": True,
}

# The cells. Each is a (scheme, transport) pair resolved through the LIVE
# registry (``resolve_scheme_for_transport``) — enumerated from
# ``valid_scheme_transport_pairs()`` at capture time, never hand-listed, so a
# cell added to the registry cannot silently escape the oracle.
#
# ``retrieval x tool_calls`` is captured TWICE: its no-refinement branch forks on
# ``search_visible`` (search tool advertised vs the #2895 runtime auto-fallback
# that presents the whole flat catalog instead), and neither branch is more
# "the" cell than the other. Both are byte-identical targets for P1.
_LAYER_CTX_VARIANTS: dict[tuple[str, str], dict[str, dict[str, Any]]] = {
    ("retrieval", "tool_calls"): {
        "search_visible_false": {"search_visible": False},
        "search_visible_true": {"search_visible": True},
    },
}

# Branches that exist in the code but CANNOT be captured offline, declared so the
# artifact says so in DATA rather than only in prose.
#
# ★ The reason this is not a prose footnote: in a data file, "omitted" and
# "excluded" are indistinguishable. A branch that is simply absent reads as "that
# cell has no such branch" — the same empty-vs-absent ambiguity this arc has been
# closing everywhere else (``llm_tools_payload=[]``, ``permissions:{}``,
# ``turn_tokens=0``). The artifact whose whole job is to be unambiguous must not
# reintroduce it. Every cell entry therefore carries an explicit ``status``, and
# P1's byte-identical guarantee covers ``CAPTURED`` entries only.
_STATUS_CAPTURED = "CAPTURED"
_STATUS_UNCAPTURED = "UNCAPTURED"

_UNCAPTURED_BRANCHES: dict[tuple[str, str], dict[str, str]] = {
    ("retrieval", "tool_calls"): {
        "refinement": (
            "requires a live embedding index + provider — build_presentation with a "
            "non-empty layer_ctx['refinement'] calls ops.search_actions(query), whose "
            "result depends on an ActionEmbeddingIndex and an embedding provider. "
            "Neither offline nor deterministic, so it is EXCLUDED, not omitted: "
            "approximating it would burn a false oracle in, and an oracle records "
            "what IS, not what ought to be."
        ),
    },
}


def assert_worktree_import() -> str:
    """Fail unless ``reyn`` resolved under THIS checkout's ``src/`` (#3024/#3231).

    The venv shared across agents in this environment carries an editable
    install pointing at a different checkout; a wrong-tree import would produce
    a plausible-looking oracle for somebody else's code."""
    import reyn

    resolved = Path(reyn.__file__).resolve()
    expected_root = (_REPO_ROOT / "src").resolve()
    if expected_root not in resolved.parents:
        raise AssertionError(
            f"import reyn resolved to {resolved}, which is NOT under {expected_root}. "
            "The oracle would describe another checkout's code."
        )
    return str(resolved)


def _walk_schema_nodes(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema_nodes(item)


def assert_schemas_pristine(payloads: dict[str, Any]) -> dict[str, int]:
    """Assert the captured payloads carry no #3385 provider-transform damage.

    Two fingerprints, both taken from #3385's measured diff:

    1. ``add_object_type`` injects ``"type": "object"`` into any node lacking a
       type — including a **string** ``const`` discriminator, where it is simply
       wrong. So: no node may carry both ``const`` and ``type == "object"``.
    2. ``_remove_additional_properties`` DELETES ``"additionalProperties": false``.
       So: every ``oneOf`` variant that is an object-with-``properties`` must
       still declare it (that is how every such variant is written in the
       canonical tool definitions, e.g. ``plugin_management_verbs.py``).

    Anti-vacuity: both arms count what they inspected and the caller fails if a
    count is 0 — an oracle whose cleanliness check never looked at a ``const``
    node proves nothing about ``const`` nodes."""
    const_nodes = 0
    oneof_variants = 0
    for cell, payload in payloads.items():
        for node in _walk_schema_nodes(payload):
            if "const" in node:
                const_nodes += 1
                if node.get("type") == "object":
                    raise AssertionError(
                        f"{cell}: a const discriminator carries type=object — the "
                        f"#3385 litellm in-place transform ran in this process. Node: {node!r}"
                    )
            for variant in node.get("oneOf", []) if isinstance(node.get("oneOf"), list) else []:
                if not (isinstance(variant, dict) and "properties" in variant):
                    continue
                oneof_variants += 1
                if variant.get("additionalProperties") is not False:
                    raise AssertionError(
                        f"{cell}: a oneOf variant lost additionalProperties=false — the "
                        f"#3385 litellm in-place transform ran in this process. Variant: {variant!r}"
                    )
    return {"const_nodes": const_nodes, "oneof_variants": oneof_variants}


# ``search_actions`` is PRESENTATION-ONLY and is not a defect — known design,
# recorded here so the next reader does not re-run the investigation.
#
# ``retrieval._search_tool_schema`` (retrieval.py:58-61) builds this schema itself:
# the tool is advertised so the model can express the affordance, but the call is
# intercepted by ``interpret`` -> ``RePresent`` and NEVER REACHES DISPATCH. A thing
# that never reaches dispatch correctly has no ``ToolDefinition``, so #3383's
# "the canonical definition is the only source" is an invariant over tools ON THE
# DISPATCH PATH; an intercepted affordance is a different category. 145-vs-1 is a
# classification difference, not a violation.
#
# ★ It is also independently safe from #3383, FOR A DIFFERENT REASON than
# everything else in the payload: ``_search_tool_schema()`` constructs a FRESH dict
# from a literal on every call, sharing nothing with any module-level constant. An
# in-place provider rewrite dies with that dict and the next turn builds a new one.
# It is not safe because it routes through ``parameters_for_export``; it is safe
# because it is rebuilt each time.
_NOT_REGISTRY_BACKED = frozenset({"search_actions"})


def assert_schemas_match_canonical(payloads: dict[str, Any]) -> dict[str, int]:
    """Assert every captured tool schema equals its canonical definition (#3385).

    Leans on the landed #3383 seam: ``parameters_for_export`` is the ONE
    projection through which a ``ToolDefinition.parameters`` leaves its
    definition, so re-exporting it here reproduces exactly what an uncorrupted
    render must have produced. Every captured entry must resolve to a definition
    — an unresolvable name means the mapping (not the schema) drifted, and is a
    failure rather than a skip."""
    from reyn.tools import get_default_registry
    from reyn.tools.types import parameters_for_export
    from reyn.tools.universal_dispatch import unwrapped_tool_name

    registry = get_default_registry()
    compared = skipped = 0
    for cell, payload in payloads.items():
        for entry in payload:
            name = entry["function"]["name"]
            if name in _NOT_REGISTRY_BACKED:
                skipped += 1
                continue
            definition = registry.lookup(name) or registry.lookup(
                unwrapped_tool_name(name) or ""
            )
            if definition is None:
                raise AssertionError(
                    f"{cell}: captured tool {name!r} resolves to no ToolDefinition, so "
                    "its schema cannot be checked against the canonical source."
                )
            compared += 1
            if entry["function"]["parameters"] != parameters_for_export(definition.parameters):
                raise AssertionError(
                    f"{cell}: {name!r}'s captured schema differs from "
                    "parameters_for_export(definition.parameters) — the capture process "
                    "was not clean, or the canonical definition changed under the oracle."
                )
    return {"compared": compared, "not_registry_backed": skipped}


def _assert_host_inputs_are_pinned(host: Any) -> None:
    for accessor, expected in _EXPECTED_HOST_INPUTS.items():
        actual = getattr(host, accessor)()
        if actual != expected:
            raise AssertionError(
                f"host.{accessor}() == {actual!r}, expected {expected!r}. Ambient config "
                "leaked into the capture — the artifact would not be reproducible."
            )


async def _capture_cells(workspace: Path) -> dict[str, Any]:
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.router_loop import RouterLoop
    from reyn.tools import get_default_registry
    from reyn.tools.transport import (
        resolve_scheme_for_transport,
        valid_scheme_transport_pairs,
    )
    from tests._support.agent_session import make_session

    get_default_registry()
    session = make_session(
        agent_name="oracle-agent",
        state_log=StateLog(workspace / "state.wal"),
        snapshot_path=workspace / "snapshot.json",
    )
    host = session._router_host
    _assert_host_inputs_are_pinned(host)

    cells: dict[str, Any] = {}
    for scheme, transport in valid_scheme_transport_pairs():
        resolved_name = resolve_scheme_for_transport(scheme, transport)
        variants = _LAYER_CTX_VARIANTS.get(
            (scheme, transport.value), {"default": {}}
        )
        for variant_name, overrides in variants.items():
            layer_ctx = {**PINNED_LAYER_CTX, **overrides}
            loop = RouterLoop(
                host=host,
                chain_id="oracle-3376",
                router_model=PINNED_LAYER_CTX["router_model"],
                scheme_name=resolved_name,
            )
            presentation = await loop._scheme.build_presentation(
                dict(PINNED_AVAILABLE), layer_ctx, ops=loop,
            )
            key = f"{scheme}|{transport.value}"
            if variant_name != "default":
                key = f"{key}|{variant_name}"
            cells[key] = {
                "scheme": scheme,
                "transport": transport.value,
                "resolved_scheme_name": resolved_name,
                "status": _STATUS_CAPTURED,
                "layer_ctx": layer_ctx,
                "llm_tools_payload": presentation.llm_tools_payload,
                "tool_use_sp": presentation.tool_use_sp,
            }

        for branch, reason in _UNCAPTURED_BRANCHES.get(
            (scheme, transport.value), {}
        ).items():
            cells[f"{scheme}|{transport.value}|{branch}"] = {
                "scheme": scheme,
                "transport": transport.value,
                "resolved_scheme_name": resolved_name,
                "status": _STATUS_UNCAPTURED,
                "reason": reason,
            }
    return cells


def captured_cells(cells: dict[str, Any]) -> dict[str, Any]:
    """The subset P1's byte-identical guarantee covers — ``CAPTURED`` entries only.

    Every consumer of the payloads goes through here, so an ``UNCAPTURED`` entry
    can never be mistaken for a capture with an empty payload."""
    return {k: v for k, v in cells.items() if v["status"] == _STATUS_CAPTURED}


def uncaptured_cells(cells: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cells.items() if v["status"] == _STATUS_UNCAPTURED}


def capture() -> dict[str, Any]:
    """Capture all registered cells. Returns the artifact body (JSON-ready)."""
    # Asserted, never recorded: an absolute path in the artifact would make it
    # machine-specific and break the byte-identical comparison everywhere else.
    assert_worktree_import()
    with tempfile.TemporaryDirectory(prefix="reyn-oracle-3376-") as tmp:
        workspace = Path(tmp)
        # cwd-independence: ``Session`` resolves config relative to the process
        # cwd, so capture from an empty workspace rather than from whatever
        # directory the harness happened to be invoked in.
        previous_cwd = Path.cwd()
        os.chdir(workspace)
        try:
            cells = asyncio.run(_capture_cells(workspace))
        finally:
            os.chdir(previous_cwd)

    payloads = {k: v["llm_tools_payload"] for k, v in captured_cells(cells).items()}
    if not uncaptured_cells(cells):
        raise AssertionError(
            "no cell entry is marked UNCAPTURED. Either a declared exclusion was "
            "dropped, or a branch became capturable — in the latter case capture it "
            "and update the acceptance condition rather than deleting the marker."
        )
    pristine_counts = assert_schemas_pristine(payloads)
    if not pristine_counts["const_nodes"] or not pristine_counts["oneof_variants"]:
        raise AssertionError(
            "the #3383 fingerprint check inspected nothing "
            f"({pristine_counts}) — it would pass vacuously."
        )
    canonical_counts = assert_schemas_match_canonical(payloads)
    if not canonical_counts["compared"]:
        raise AssertionError(
            "the #3385 canonical-equality check compared nothing "
            f"({canonical_counts}) — it would pass vacuously."
        )

    body = {
        "issue": 3376,
        "purpose": (
            "P1 byte-identical target: the (llm_tools_payload, tool_use_sp) each "
            "registered (scheme x transport) cell produces today, from a real "
            "SchemeOps (RouterLoop over a real Session's RouterHostAdapter)."
        ),
        "pinned_available": PINNED_AVAILABLE,
        "pinned_layer_ctx_base": PINNED_LAYER_CTX,
        "pinned_host_inputs": _EXPECTED_HOST_INPUTS,
        "pristineness_3383_fingerprints": pristine_counts,
        "pristineness_3385_canonical_equality": canonical_counts,
        "notes": _NOTES,
        "cells": cells,
    }
    return body


_NOTES = [
    "enumerate-all composes a DIFFERENT tool population per transport, and that "
    "asymmetry is recorded here as-is rather than smoothed: the tool_calls cell is "
    "base_tools + catalog_entries minus mcp__call_tool, while the content_fence "
    "(CodeAct) cell renders catalog_entries ONLY — so base tools such as "
    "delegate_to_agent / read_memory_body are absent from the code-API. Architect's "
    "ruling (#3376 comment 2): probably a gap, tracked separately (#3381), and P1 "
    "MUST NOT fix it — fixing it would add callables to CodeAct's system prompt and "
    "break P1's own byte-identical criterion. Do not 'correct' this artifact.",
    "The content_fence cell records llm_tools_payload == [] verbatim. codeact.py "
    "expresses 'this field does not apply to my transport' as an EMPTY LIST, which "
    "is indistinguishable from 'there are no tools to show'. That ambiguity is one "
    "of the things P1's Exposure/Encoder split intends to remove; the oracle records "
    "the empty list as the current fact and does not resolve it.",
    "tool_use_sp is a dict slot-map for three of the four cells and a bare STRING "
    "(the rendered code-API) for the content_fence cell — the Presentation field is "
    "a 'dict | str | None' union today. Recorded as-is.",
    "Captured on top of #3385 (the fix for litellm's in-place rewriting of reyn's "
    "canonical schema constants), in a process whose only work is this capture. Two "
    "independent cleanliness checks ran and are counted above: total equality against "
    "parameters_for_export(definition.parameters), and a scan for the two #3383 damage "
    "fingerprints. The retrieval cells' 'search_actions' entry is exempt from the first "
    "check because retrieval._search_tool_schema builds that schema itself (a "
    "scheme-owned literal whose call is intercepted into RePresent, never dispatched).",
    "No exec__* or rag__* action appears in any cell. That is a CONFIG fact of the "
    "pinned Session, not a machine fact: the Agent identity carries "
    "sandbox_backend=None, so universal_catalog.is_exec_available() hides the exec "
    "category, and no embedding provider is configured, so the rag category is not "
    "enumerated. Both gates read pinned inputs, so the artifact does not vary with "
    "the host OS or the developer's sandbox availability.",
    "retrieval x tool_calls is captured in both branches of its no-refinement fork "
    "(search_visible false -> the #2895 runtime auto-fallback presenting the flat "
    "catalog + hidden-state hint; true -> base + the search tool). Its refinement "
    "branch carries status=UNCAPTURED with a reason, IN THE DATA — because in a data "
    "file 'omitted' and 'excluded' are indistinguishable, and a silently absent "
    "branch would read as 'this cell has no such branch'. ACCEPTANCE SCOPE: P1's "
    "byte-identical guarantee covers status=CAPTURED entries ONLY.",
    "search_actions is presentation-only and is NOT a defect — known design, not a "
    "gap. retrieval._search_tool_schema builds it as a scheme-owned literal; the "
    "call is intercepted by interpret -> RePresent and never reaches dispatch, and "
    "something that never dispatches correctly has no ToolDefinition. It is also "
    "independently safe from #3383 for a DIFFERENT reason than everything else here: "
    "the schema is freshly constructed from a literal on every call, so it shares "
    "nothing with a canonical constant and an in-place rewrite dies with that dict. "
    "Safe because it is rebuilt each time, not because it routes through the helper.",
]


# ── Determinism proof ─────────────────────────────────────────────────────────


def capture_in_subprocess(src_root: "str | None" = None) -> str:
    """Run a capture in a FRESH interpreter and return its canonical JSON.

    Separate processes are the point: an in-process repeat would share every
    module-level constant, so it could not detect the #3385 process-history
    class of variation at all.

    ``src_root`` pins the spawn's ``PYTHONPATH`` (the ``out_of_process_reyn``
    fixture's value when called from a test — a subprocess re-resolves ``reyn``
    from the venv, which in a worktree is somebody else's checkout, #3024).
    Defaults to this checkout's ``src``; either way the child re-asserts the
    resolution itself via ``assert_worktree_import``."""
    script = (
        "import json,sys;"
        f"sys.path.insert(0, {str(_REPO_ROOT / 'tests' / 'scaffold')!r});"
        "import tool_use_oracle_3376 as m;"
        "print('<<<ORACLE>>>' + m.canonical_json(m.capture()))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "PYTHONPATH": src_root or str(_REPO_ROOT / "src")},
    )
    if proc.returncode != 0:
        raise AssertionError(f"subprocess capture failed:\n{proc.stderr}")
    marker = "<<<ORACLE>>>"
    if marker not in proc.stdout:
        raise AssertionError(f"subprocess produced no oracle:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.split(marker, 1)[1].strip()


def canonical_json(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def prove_determinism(runs: int = 3) -> str:
    """Capture ``runs`` times in separate processes; raise on any divergence."""
    captures = [capture_in_subprocess() for _ in range(runs)]
    for index, other in enumerate(captures[1:], start=2):
        if other != captures[0]:
            raise AssertionError(
                f"capture #{index} of {runs} differs from capture #1 — the oracle is "
                "not deterministic; find what varies before recording it."
            )
    return captures[0]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ORACLE_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="prove determinism across N fresh subprocesses instead of writing",
    )
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)

    if args.check:
        prove_determinism(args.runs)
        print(f"deterministic across {args.runs} separate processes")
        return 0

    text = prove_determinism(args.runs)
    args.out.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    print(f"wrote {args.out} (identical across {args.runs} separate processes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
