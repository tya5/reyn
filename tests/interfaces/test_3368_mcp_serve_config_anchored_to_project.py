"""Tier 2: OS invariant — `reyn mcp serve` reads the PROJECT's config, not cwd's.

THE BUG (#3368, owner-reported live as ``You passed model=standard``): MCP
clients (Claude Desktop, Cursor, …) ignore the ``cwd`` field in their server
config, so the spawned ``reyn mcp serve --project <root>`` process starts in
``/``. ``run_serve`` loaded the config FIRST (``InvocationContext.from_args``
→ ``load_config()``, whose whole 3-layer cascade is derived from
``Path.cwd()``) and only 44 lines later resolved ``--project`` and
``os.chdir``'d into it. From ``/`` no ``reyn.yaml`` is reachable, so the
cascade silently yielded the built-in defaults — ``models: {}`` with
``model: "standard"``, and ``permissions: {}``. ``"standard"`` was not
resolvable with no project config in effect (reyn ships no built-in model
catalog — #4349, a tier every project maps itself), so
``ModelResolver.resolve`` fell through its raw-litellm-string passthrough
and handed the bare class name to litellm, which rejected it with
``You passed model=standard`` — no ``--model`` flag and no ``/model``
command involved. (#4349 has since made an unresolved class position raise
immediately inside ``resolve()`` instead of reaching litellm at all — this
test's own fixture declares the class in its project config, so it never
exercises that path either way; the ordering bug this test guards, and the
project config actually taking effect, are what it pins.) The permission
config was dropped by the same ordering.

THE FIX: ``_anchor_project_root`` resolves the root and ``os.chdir``s BEFORE
the config is loaded, so every cwd-derived read (the config cascade included)
sees the project. Extracted as a function so the ordering is structural — a
caller cannot obtain the root without the anchor having run.

The gate below drives the REAL ordering with a REAL project on disk and a
REAL ``ModelResolver``, from a cwd outside any project. Swapping the two
calls in ``run_serve`` back (load config, then anchor) turns it red.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from reyn.interfaces.cli.commands.mcp import _anchor_project_root
from reyn.interfaces.cli.invocation_context import InvocationContext

_PROJECT_YAML = """\
llm:
  model: standard
  models:
    standard: openai/probe-standard-model
permissions:
  file.read: allow
"""


def _serve_args(project: Path | None) -> argparse.Namespace:
    """The argparse Namespace `reyn mcp serve` reads (real attribute set)."""
    return argparse.Namespace(
        project=str(project) if project is not None else None,
        model=None,
        output_language=None,
        llm_timeout=None,
        llm_max_retries=None,
    )


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "reyn.yaml").write_text(_PROJECT_YAML, encoding="utf-8")
    return project


def test_serve_resolves_model_class_from_the_project_not_the_spawn_cwd(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: spawned outside any project, `mcp serve --project` still resolves
    the default model CLASS to the project's litellm string — never the raw
    class name that litellm rejects with "You passed model=standard" (#3368)."""
    project = _make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"  # no reyn.yaml here or above (tmp_path)
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    args = _serve_args(project)
    # The production ordering: anchor, THEN load config.
    assert _anchor_project_root(args) == project.resolve()
    session_cfg = InvocationContext.from_args(args)

    model_class, resolved = session_cfg.model_for(args)
    assert model_class == "standard"
    assert resolved == "openai/probe-standard-model", (
        "the project's models: mapping must be in effect; a bare class name "
        "here is the #3368 passthrough that reaches litellm as "
        f"'You passed model={resolved}'"
    )
    assert session_cfg.resolver.is_known_class("standard"), (
        "'standard' must be a KNOWN class — an unknown one is exactly the "
        "silent passthrough #3368 is about"
    )


def test_serve_reads_project_permissions_when_spawned_outside_the_project(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the same ordering carries the project's permission config —
    the cwd-derived cascade dropped it too, running the MCP surface on
    fail-closed defaults instead of the operator's config (#3368)."""
    project = _make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    args = _serve_args(project)
    _anchor_project_root(args)
    session_cfg = InvocationContext.from_args(args)

    assert session_cfg.config.permissions.get("file.read") == "allow"


def test_run_serve_anchors_before_it_loads_the_config() -> None:
    """Tier 2: (ordering guard) `run_serve` calls `_anchor_project_root` before
    `InvocationContext.from_args` — the two behavioral arms above drive the
    helper directly, so only this arm catches the call sites being swapped
    back into the #3368 defect."""
    import ast

    from reyn.interfaces.cli.commands import mcp as mcp_cmd

    tree = ast.parse(Path(mcp_cmd.__file__).read_text(encoding="utf-8"))
    run_serve = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_serve"
    )
    anchor_lines = [
        node.lineno for node in ast.walk(run_serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_anchor_project_root"
    ]
    config_lines = [
        node.lineno for node in ast.walk(run_serve)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_args"
    ]
    assert anchor_lines, "run_serve must anchor the project root (#3368)"
    assert config_lines, "run_serve must build an InvocationContext"
    assert max(anchor_lines) < min(config_lines), (
        "run_serve loads the config before anchoring the project root — the "
        "cascade is cwd-derived, so an MCP-client spawn (cwd=/) reads NO "
        "reyn.yaml and falls back to model='standard' + permissions={} (#3368)"
    )
