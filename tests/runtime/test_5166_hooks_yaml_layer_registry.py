"""Tier 2: #5166 (architect ruling, issuecomment-5384196419) — EVERY
hooks.yaml-shaped layer (per-agent AND per-session, ``hooks:`` AND
``composers:``) expands reyn tokens through the SAME primitive
(``Session._hooks_yaml_layers`` / ``_read_hooks_yaml_layer_key`` →
``reyn.config.loader.read_and_expand_hooks_yaml``).

Root cause this closes: hooks get copy-pasted between layers (#5164's own
docstring self-describes as "mirrors" the per-agent ``hooks:`` reader) —
#5161 fixed ONE reader, #5164 caught up a SECOND, and the 2 per-session
readers had NO expansion at all until now. A rule duplicated 4 times means
the next copy only carries whichever half someone remembered.

**Registry-driven, not 4 hand-written tests** (architect's own explicit
requirement — a hand-written test per (layer, key) pair silently misses a
5th layer added later; walking ``Session._hooks_yaml_layers()`` instead
means a NEW layer entry is automatically covered by every test here,
without a single test file edit). Acceptance④'s own test proves this by
literally adding a synthetic 3rd layer via monkeypatch and confirming
the SAME test loop picks it up with zero code changes elsewhere.

Real ``Session`` (via ``tests._support.agent_session.make_session``) + real
files on disk — no mocks."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.agent_session import make_session

_AGENT = "coder-smith"
_KEYS = ("hooks", "composers")


def _make_session_in(project_root: Path, monkeypatch, tmp_path: Path):
    monkeypatch.chdir(project_root)
    return make_session(
        agent_name=_AGENT,
        workspace_base_dir=project_root,
        workspace_state_dir=tmp_path / "state",
    )


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _entry(key: str) -> str:
    """A minimal, valid entry for *key* (``hooks:`` or ``composers:``) whose
    ONE string field carries the token under test — same shape both keys'
    real schemas accept (a ``template_push.message``)."""
    return (
        f"{key}:\n"
        "  - name: probe\n"
        "    on: turn_end\n"
        "    template_push:\n"
        "      message: broker://inbox/${REYN_AGENT_NAME}\n"
    )


def _passthrough_entry(key: str) -> str:
    return (
        f"{key}:\n"
        "  - name: probe\n"
        "    on: turn_end\n"
        "    exec:\n"
        "      command: echo ${SOME_CHILD_PROCESS_VAR}\n"
    )


@pytest.mark.parametrize("key", _KEYS)
def test_reyn_agent_name_resolves_in_every_registered_layer(
    tmp_path, monkeypatch, key,
) -> None:
    """Tier 2: acceptance ① — ``${REYN_AGENT_NAME}`` resolves in *every*
    layer :meth:`Session._hooks_yaml_layers` enumerates, for both the
    ``hooks:`` and ``composers:`` key — driven from the registry itself,
    never a hand-listed layer name."""
    project = tmp_path / "proj"
    project.mkdir()
    session = _make_session_in(project, monkeypatch, tmp_path)

    for label, path in session._hooks_yaml_layers():
        _write_yaml(path, _entry(key))
        entries = session._read_hooks_yaml_layer_key(path, key)
        assert entries, f"{label}/{key}: layer must load, not come back empty"
        assert entries[0]["template_push"]["message"] == f"broker://inbox/{_AGENT}", (
            f"{label}/{key}: ${{REYN_AGENT_NAME}} did not resolve to the "
            f"real agent name — got {entries!r}"
        )
        path.unlink()  # isolate each layer's own probe from the next iteration


@pytest.mark.parametrize("key", _KEYS)
def test_a_non_reyn_token_passes_through_in_every_registered_layer(
    tmp_path, monkeypatch, key,
) -> None:
    """Tier 2: acceptance ② — a non-reyn ``${FOO}`` (an env var meant for a
    spawned child process) must load UNTOUCHED in every layer, never
    fail-closed — the #5152-shaped regression this test is the detection
    surface for (fail-close scoped too wide would turn a healthy config
    red here)."""
    project = tmp_path / "proj"
    project.mkdir()
    session = _make_session_in(project, monkeypatch, tmp_path)

    for label, path in session._hooks_yaml_layers():
        _write_yaml(path, _passthrough_entry(key))
        entries = session._read_hooks_yaml_layer_key(path, key)
        assert entries, f"{label}/{key}: a non-reyn token must not be refused"
        assert entries[0]["exec"]["command"] == "echo ${SOME_CHILD_PROCESS_VAR}", (
            f"{label}/{key}: a non-reyn token must load untouched — got {entries!r}"
        )
        path.unlink()


@pytest.mark.parametrize("key", _KEYS)
def test_an_unresolved_reyn_token_refuses_the_layer_everywhere(
    tmp_path, monkeypatch, key,
) -> None:
    """Tier 2: fail-close applies uniformly too — not just resolution.
    Same registry walk, the OTHER half of "same字面, same意味"."""
    project = tmp_path / "proj"
    project.mkdir()
    session = _make_session_in(project, monkeypatch, tmp_path)

    body = (
        f"{key}:\n"
        "  - name: bad\n"
        "    on: turn_end\n"
        "    template_push:\n"
        "      message: ${REYN_SKILL_DIR}/note.txt\n"
    )
    for label, path in session._hooks_yaml_layers():
        _write_yaml(path, body)
        with pytest.warns(UserWarning, match="REYN_SKILL_DIR"):
            entries = session._read_hooks_yaml_layer_key(path, key)
        assert entries == [], (
            f"{label}/{key}: an unresolved reyn token must refuse the whole "
            f"layer, not load a wrong/empty value — got {entries!r}"
        )
        path.unlink()


def test_the_four_named_reader_methods_delegate_through_the_shared_primitive(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the OTHER half of #5166's own claim — walking
    ``_hooks_yaml_layers()``/``_read_hooks_yaml_layer_key()`` directly (as
    every OTHER test in this file does) proves the shared primitive itself
    resolves reyn tokens correctly, but production code never calls that
    primitive directly — ``_build_hook_registry``/``_build_composer_defs``
    call the 4 NAMED methods (``_read_per_agent_hooks`` etc.). This is a
    DIFFERENT claim (wiring, not layer-coverage) and deliberately names
    all 4 — there are exactly 4 existing call sites production code uses,
    not an open-ended set a registry could miss growing, so hand-naming
    them here is the right shape (unlike the layer enumeration above,
    which the #5166 registry exists precisely to avoid hand-naming)."""
    project = tmp_path / "proj"
    project.mkdir()
    session = _make_session_in(project, monkeypatch, tmp_path)

    for label, path in session._hooks_yaml_layers():
        for key in _KEYS:
            _write_yaml(path, _entry(key))
            method = getattr(session, f"_read_{label.replace('-', '_')}_{key}")
            entries = method()
            assert entries and entries[0]["template_push"]["message"] == f"broker://inbox/{_AGENT}", (
                f"{label}.{method.__name__}(): must delegate through the "
                f"shared primitive and resolve ${{REYN_AGENT_NAME}} — got {entries!r}"
            )
            path.unlink()


def test_adding_a_layer_to_the_registry_is_automatically_covered(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: acceptance④ — the actual differentiator from "4 hand-written
    tests": monkeypatch :meth:`Session._hooks_yaml_layers` to also report a
    SYNTHETIC 3rd layer, and confirm the SAME registry-walk this file's
    other tests already use picks it up with ZERO code changes anywhere
    else — the concrete form of "a layer added later is not silently
    missed" this issue's acceptance④ names."""
    project = tmp_path / "proj"
    project.mkdir()
    session = _make_session_in(project, monkeypatch, tmp_path)

    synthetic_path = tmp_path / "synthetic-layer" / "hooks.yaml"
    real_layers = session._hooks_yaml_layers()
    monkeypatch.setattr(
        session, "_hooks_yaml_layers",
        lambda: [*real_layers, ("synthetic-3rd-layer", synthetic_path)],
    )

    layers = session._hooks_yaml_layers()
    assert len(layers) == len(real_layers) + 1, "the monkeypatch itself must add exactly one layer"

    for label, path in layers:
        _write_yaml(path, _entry("hooks"))
        entries = session._read_hooks_yaml_layer_key(path, "hooks")
        assert entries and entries[0]["template_push"]["message"] == f"broker://inbox/{_AGENT}", (
            f"{label}: a layer reached only through the registry (never "
            f"hand-named in this test) must still resolve ${{REYN_AGENT_NAME}} "
            f"— got {entries!r}"
        )
