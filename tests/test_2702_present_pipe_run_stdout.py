"""#2702 — a pipeline ``tool: present`` step renders to the operator's stdout (part of the #2688 sweep).

#2692 opened the pipeline INVOCATION surface for ``present`` (a ``tool: present`` step resolves +
reaches ``execute_op`` + returns ``ok:True``), but left the RENDER surface unwired outside chat: the
pipe-run ``OpContext``'s ``presentation_renderer`` routed to the ``default``-identity Session's outbox,
which nothing drains in a headless ``reyn pipe run`` — so present executed, returned ``ok:True``, and
the user saw NOTHING (a silent purpose-failure; present's purpose is the user SEEING the data).

This is the reachable-FOR-PURPOSE proof one layer deeper than #2692's ``execute_op``-reach test: it
drives a REAL ``reyn pipe run`` (``run_run``) of a real registered pipeline whose ``tool: present``
step carries a unique marker in its data, and asserts the marker text actually appears on stdout — the
render reached the operator, not just an ``ok:True`` ack. Real project scaffold / config / registry /
executor / present op — no collaborator mocks; asserts the presented CONTENT appears (a marker
substring), never exact Rich formatting/whitespace (a Tier-4 pin).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from reyn.interfaces.cli.commands.pipe import run_run

# A distinctive token that appears in the PRESENTED data but NOT in the present op's
# compact ack (which is what the final `reyn pipe run` JSON prints). So its presence on
# stdout proves the RENDER reached the operator, distinct from the ack round-trip.
_MARKER = "REYNPRESENTMARKER42"


def _write_reyn_yaml(project_root: Path, pipelines_entries: dict) -> None:
    data: dict = {
        "model": "standard",
        "models": {"standard": "openai/gpt-4o-mini"},
        "pipelines": {"entries": pipelines_entries},
    }
    (project_root / "reyn.yaml").write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False), encoding="utf-8",
    )


def _write_present_pipeline(project_root: Path) -> None:
    """A one-step pipeline whose ``tool: present`` step shows inline data (marker
    inside) via the stage-3 default viewer (no view authoring needed)."""
    (project_root / "shows.yaml").write_text(
        "pipeline: shows\n"
        "steps:\n"
        "  - tool:\n"
        "      name: present\n"
        "      args:\n"
        "        data_inline: {label: " + _MARKER + "}\n"
        "      output: ack\n",
        encoding="utf-8",
    )
    _write_reyn_yaml(project_root, {"shows": {"path": "shows.yaml"}})


def test_pipe_run_present_step_renders_to_stdout(tmp_path, monkeypatch, capsys):
    """Tier 2: a real ``reyn pipe run`` of a ``tool: present`` step renders the presented
    data to the operator's stdout — the reachable-FOR-PURPOSE bar (user SEES the data),
    not merely ``execute_op`` reach / ``ok:True``. RED on origin/main (present routes to a
    never-drained outbox → marker absent from stdout); GREEN once the pipe-run OpContext
    wires a headless stdout presentation renderer."""
    monkeypatch.chdir(tmp_path)
    _write_present_pipeline(tmp_path)

    args = argparse.Namespace(
        name="shows", input="{}", project=str(tmp_path), async_=False,
        grant_file_write=False,
    )
    run_run(args)

    out = capsys.readouterr().out
    # The presented marker reached the operator's stdout (the render fired), distinct
    # from the final JSON ack (which carries only reached-user/bind STATS, not the data).
    assert _MARKER in out, (
        "present rendered nothing to stdout — the pipe-run OpContext's "
        "presentation_renderer is unwired (or routes to a never-drained sink)."
    )

    # Sanity: the run still completed and printed its result JSON (the present ack is a
    # compact stats blob, NOT the data — so the marker's presence above is the render, not
    # the ack echoing the data back).
    json_start = out.index("{", out.index(_MARKER))
    result = json.loads(out[json_start:])
    # The step output is the present op's compact canonical ack (a reached-user / bind-
    # stats TEXT line — FP-0056), NOT the data; so the stdout marker above is the render.
    assert "Presented to the user" in result["named_stores"]["ack"]["text"]
    assert _MARKER not in json.dumps(result), (
        "the present ack unexpectedly carried the data — the stdout marker match "
        "must prove the RENDER, not an ack echo."
    )
