"""Tier 2: OS invariant -- #3162's L2-router-plus-L3-references split for
`reactive_orchestration_plugins` (2nd consumer, after `reyn_cheat_sheet`
#3171) uses the OFFICIAL Agent Skills convention: relative markdown links in
the SKILL.md BODY, not a front-matter `references:` declaration (owner
ruling: https://code.claude.com/docs/en/skills.md documents body links as
the standard; front-matter `references:` is not part of it).

That convention only pays off if the reachability chain actually closes for
a model: (1) `skill_list` hands the model this skill's absolute SKILL.md
`path`, (2) the model reads the body and finds a *relative* link such as
`references/incoming-events-coalescing-and-wake.md`, (3) the model resolves
that relative link against the directory of (1) and issues an ordinary
`file` read op against the resulting absolute path. This test exercises
exactly that chain against the REAL `reyn.core.op_runtime.file.handle` read
op (the same one #2913's wheel-reachability test used for SKILL.md itself),
not just `read_builtin_body_bytes` in isolation -- proving the reference is
not merely *present on disk* but *readable through the op a model would
actually call*.

No mocks: real `BUILTIN_SKILLS` registry entry, real file bytes, real
`PermissionResolver` + `OpContext` + `handle()` dispatch (the same harness
`test_2913_builtin_body_wheel_reachable.py` established).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_SKILL_PATH = Path(BUILTIN_SKILLS["reactive_orchestration_plugins"]["path"])
_SKILL_DIR = _SKILL_PATH.parent
_REFERENCES_DIR = _SKILL_DIR / "references"

# Matches the same relative-markdown-link shape the router body uses, e.g.
# "[incoming-events-coalescing-and-wake.md](references/incoming-events-coalescing-and-wake.md)".
_RELATIVE_LINK_RE = re.compile(r"\]\((references/[^)\s]+\.md)\)")


def _linked_relative_paths() -> "list[str]":
    body = _SKILL_PATH.read_text(encoding="utf-8")
    return _RELATIVE_LINK_RE.findall(body)


def _run(coro):
    return asyncio.run(coro)


def _make_ctx() -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions={},
        # A project root elsewhere -- the same #2913 wheel-layout condition,
        # so this test also proves the builtin-body bypass generalizes to a
        # sibling reference file, not just SKILL.md itself.
        project_root=Path("/does/not/contain/the/builtin/package"),
        interactive=False,
    )
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        actor="test_skill",
    )


def test_skill_body_declares_at_least_one_relative_reference_link() -> None:
    """Tier 2: vacuity guard -- the body actually contains the links this
    test exercises, so a stripped/renamed link is caught here rather than
    making every test below pass trivially over an empty list."""
    links = _linked_relative_paths()
    assert links, "expected relative markdown links into references/ in the SKILL.md body"


def test_every_linked_reference_resolves_on_disk_next_to_skill_md() -> None:
    """Tier 2: OS invariant, property (1) -- each relative link in the body
    resolves, against the SKILL.md's own directory, to a real file under
    references/."""
    for rel in _linked_relative_paths():
        resolved = (_SKILL_DIR / rel).resolve()
        assert resolved.is_file(), f"linked reference does not exist on disk: {resolved}"
        assert resolved.parent == _REFERENCES_DIR.resolve()


def test_references_dir_has_no_orphans_beyond_the_linked_set() -> None:
    """Tier 2: OS invariant -- bidirectional parity: every file physically
    under references/ is reachable from a body link (unlinked = unreachable
    dead weight)."""
    linked_names = {Path(rel).name for rel in _linked_relative_paths()}
    on_disk_names = {p.name for p in _REFERENCES_DIR.glob("*.md")}
    assert on_disk_names == linked_names, (
        f"references/ set {on_disk_names} != linked set {linked_names}"
    )


def test_every_linked_reference_is_readable_through_the_real_file_read_op() -> None:
    """Tier 2: OS invariant, property (3), the reachability chain -- resolve
    each body link against the SKILL.md path the model gets from
    `skill_list`, then issue the SAME real `file` read op a model would
    call, with a PermissionResolver whose project_root does not contain the
    package (the wheel-install condition). Asserts actual byte-for-byte
    content, not merely a non-None/no-raise."""
    ctx = _make_ctx()
    for rel in _linked_relative_paths():
        resolved = (_SKILL_DIR / rel).resolve()
        expected_bytes = resolved.read_bytes()

        op = FileIROp(kind="file", op="read", path=str(resolved))
        result = _run(handle(op, ctx))

        assert result.get("status") == "ok", result
        content = result["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        assert content == expected_bytes, f"content mismatch for {resolved}"
