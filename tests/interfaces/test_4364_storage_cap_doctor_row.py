"""Tier 2: #4364 (lead-coder assignment, part of #4364) — ``reyn doctor``
gains a project-wide storage cap row (``storage.max_bytes``/``storage.pin``,
#5366/#4478).

#5682 BLOCKING (2026-09-02): this file used to also assert an unconditional
"known gap (#5653)" disclosure line — #5653 (``save_media`` never
self-triggering the eviction pre-check) was fixed by #5667 the same night
#5658 landed the disclosure, and nobody swept it; the assertion was
protecting a now-false claim from being removed. Dropped along with the
disclosure itself (``doctor.py``'s own ``_print_storage_cap_status``). The
"disclosure disappearing flips a test red" design intent (#5658's own,
same rule ``test_4364_bounding_preference_doctor_row.py`` follows for its
OWN still-true session-layer-invisibility line) now has nothing left to
guard IN THIS FILE — the remaining assertions below (configured vs.
unconfigured differ; "unconfigured" never fabricates a number) are a
different kind of witness (a real behavioral branch, not a disclosure
line) and were never at risk from this same defect class.

No mocks — drives the real ``run`` against a real :class:`MediaStore` write
under ``tmp_path``, matching this command family's own established shape
(``test_4364_pr3a_doctor_cli.py``).
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig
from reyn.interfaces.cli.commands.doctor import run
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_real_media(project_root: Path, *, agent_name: str) -> int:
    """Writes one real media file via the real ``MediaStore.save_media``
    (post-#4478 ``<agent>/<session_id>/`` nesting) and returns its byte
    count, so the doctor row's own number is checked against genuine
    on-disk content, not a hand-computed guess."""
    store = MediaStore(
        MediaStoreConfig(),
        project_root=project_root,
        agent_name=agent_name,
        session_id="s1",
    )
    data = b"x" * 777
    store.save_media(data, mime_type="image/png")
    return len(data)


def test_storage_cap_row_reflects_declared_max_bytes_and_differs_when_unset(
    tmp_path: Path, capsys,
):
    """Tier 2: accept-side — the SAME real on-disk media write produces a
    DIFFERENT storage-cap row depending on whether ``storage.max_bytes``
    is declared. Configured: names the real cap and the real measured
    usage. Unconfigured (no ``storage:`` block, the field's own
    documented default): says "unconfigured", never invents a number."""
    written_bytes = _write_real_media(tmp_path, agent_name="alice")

    # -- configured --
    _write_yaml(
        tmp_path / "reyn.yaml",
        MINIMAL_REYN_YAML + "storage:\n  max_bytes: 100000\n  pin: [\"alice\"]\n",
    )
    run(Namespace(project_root=str(tmp_path)))
    configured_out = capsys.readouterr().out
    configured_line = next(
        line for line in configured_out.splitlines() if "currently used" in line
    )
    assert "100,000" in configured_line
    assert f"{written_bytes:,} bytes currently used" in configured_line
    pin_line = next(line for line in configured_out.splitlines() if line.strip().startswith("storage.pin:"))
    assert "alice" in pin_line

    # -- unconfigured (same on-disk content, different config) --
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    run(Namespace(project_root=str(tmp_path)))
    unconfigured_out = capsys.readouterr().out
    unconfigured_line = next(
        line for line in unconfigured_out.splitlines() if "currently used" in line
    )

    assert configured_line != unconfigured_line
    assert "unconfigured" in unconfigured_line


def test_storage_cap_row_when_unconfigured_never_fabricates_a_number(
    tmp_path: Path, capsys,
):
    """Tier 2: deny-side — with no ``storage:`` block at all (``max_bytes``
    stays ``None``, the field's own default), the row must say
    "unconfigured" and must NOT print the ``storage.max_bytes=<N>``
    form at all — that literal substring only ever appears on the
    configured branch, so its absence here is the falsifiable check that
    no number was invented for the unset case."""
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)

    run(Namespace(project_root=str(tmp_path)))
    out = capsys.readouterr().out

    cap_line = next(line for line in out.splitlines() if "currently used" in line)
    assert "unconfigured" in cap_line
    assert "storage.max_bytes=" not in out
