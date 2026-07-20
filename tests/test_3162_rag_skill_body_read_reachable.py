"""Tier 2: OS invariant — the consolidated `build_and_query_rag_corpus` skill's
bundled reference files are actually reachable through a REGISTERED plugin
install, not merely present on disk (part of #3162: the five-skill split was
folded back into the standard one-skill + bundled-`references/` shape).

Plugin-body reads (`reyn.plugins.body_read.read_plugin_body_bytes`) became
default-allowed for a registered plugin's `skills/**` only in #3174 — the
generic mechanism is covered by `tests/test_3162_plugin_body_read_parity.py`
(a synthetic fixture plugin). This file is the SAME positive-witness shape
(`test_witness4_registered_plugin_body_reachable_without_approval`), applied
against the REAL, on-disk RAG skill directory's actual content (its router
`SKILL.md` + all four bundled `references/*.md` files), so "the reference is
documented but unreachable" cannot silently reappear for this specific skill
after the consolidation.

The install source copies the REAL `src/reyn/builtin/plugins/rag/skills/`
tree byte-for-byte (a synthetic manifest declaring only the `skills`
capability, so install never touches `mcp`/`pipelines` or shells out to
`uv` — no `requirements.txt` in the copied source, same "keep the real-
install witness independent of materialisation infrastructure" reasoning as
`tests/test_3162_plugin_body_read_parity.py`'s own fixture docstring) — not
a hand-typed placeholder standing in for the skill's content.

No mocks: real `PluginInstallIROp` → real `plugin_install.handle` → a real
`~/.reyn/plugins/<name>/` install (HOME monkeypatched to tmp_path), real
`file_handle` reads, real `PermissionResolver` denying everything outside
the bypass under test.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle as file_handle
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.core.op_runtime.plugin_install import plugins_root
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp, PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_REPO_ROOT = Path(__file__).parent.parent
_REAL_SKILL_DIR = (
    _REPO_ROOT / "src" / "reyn" / "builtin" / "plugins" / "rag" / "skills"
    / "build_and_query_rag_corpus"
)


def _run(coro):
    return asyncio.run(coro)


def _read_op(path: str) -> FileIROp:
    return FileIROp(kind="file", op="read", path=path)


def _copy_real_skill_as_plugin_source(base: Path) -> Path:
    """A minimal local plugin dir whose `skills/build_and_query_rag_corpus/`
    is a byte-for-byte copy of the real shipped skill directory (`SKILL.md` +
    `references/*.md`) — only the manifest is synthetic, and it declares
    ONLY the `skills` capability so install never shells out to `uv` or
    probes MCP servers."""
    assert _REAL_SKILL_DIR.is_dir(), f"expected real skill dir at {_REAL_SKILL_DIR}"
    plugin_dir = base / "rag_skill_only"
    (plugin_dir / ".reyn-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".reyn-plugin" / "plugin.json").write_text(
        json.dumps({
            "name": "rag_skill_only", "version": "0.1.0",
            "description": "skills-only witness copy of the real rag skill",
            "capabilities": [{"kind": "skills"}],
        }),
        encoding="utf-8",
    )
    dest = plugin_dir / "skills" / "build_and_query_rag_corpus"
    shutil.copytree(_REAL_SKILL_DIR, dest)
    return plugin_dir


def _install_ctx(project_root: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False,
    )
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    for cfg in ("pipelines.yaml", "skills.yaml", "mcp.yaml"):
        resolver.session_approve_path(
            str(project_root / ".reyn" / "config" / cfg), "test", "file.write",
        )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test",
    )


def _read_ctx(unrelated_project_root: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, base_dir=unrelated_project_root)
    resolver = PermissionResolver(
        config_permissions={}, project_root=unrelated_project_root, interactive=False,
    )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="test",
    )


def test_real_rag_skill_router_and_every_bundled_reference_reachable_without_approval(
    tmp_path, monkeypatch,
):
    """Tier 2: (positive witness) once the real `build_and_query_rag_corpus`
    skill directory is installed as a REGISTERED plugin, `SKILL.md` AND every
    one of its four bundled `references/*.md` files read successfully with NO
    approval prompt, even with `project_root` unrelated — the real content,
    not a placeholder, so a reference that is documented (linked from
    `SKILL.md`) but actually unreachable cannot hide behind a synthetic
    fixture that never exercised the real file set."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    real_reference_names = sorted(
        p.name for p in (_REAL_SKILL_DIR / "references").glob("*.md")
    )
    assert real_reference_names, (
        "fixture invariant: the real skill must ship at least one bundled "
        "reference file for this witness to be non-vacuous"
    )

    source = _copy_real_skill_as_plugin_source(tmp_path / "src")
    project_root = tmp_path / "install_proj"
    project_root.mkdir()
    ctx = _install_ctx(project_root)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(source)})
    result = _run(install_handle(op, ctx))
    assert result["status"] == "installed", result

    plugin_root = plugins_root() / "rag_skill_only"
    skill_dir = plugin_root / "skills" / "build_and_query_rag_corpus"

    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    read_ctx = _read_ctx(unrelated_root)

    skill_result = _run(file_handle(_read_op(str(skill_dir / "SKILL.md")), read_ctx))
    assert skill_result["status"] == "ok", skill_result
    assert "Build and query a RAG corpus" in skill_result["content"]
    for name in real_reference_names:
        ref_result = _run(
            file_handle(_read_op(str(skill_dir / "references" / name)), read_ctx),
        )
        assert ref_result["status"] == "ok", (name, ref_result)
        real_content = (_REAL_SKILL_DIR / "references" / name).read_text(encoding="utf-8")
        assert ref_result["content"].strip() == real_content.strip(), name
