"""Tier 2: OS invariant -- #3162's L2-router-plus-L3-references split for
`reactive_orchestration_plugins` (2nd consumer, after `reyn_cheat_sheet`
#3171) uses the OFFICIAL Agent Skills convention: a `${CLAUDE_SKILL_DIR}`-
prefixed relative markdown link in the SKILL.md BODY, not a front-matter
`references:` declaration (owner ruling, ground-truthed against both
https://code.claude.com/docs/en/skills.md and reyn's own token-expansion
code: `${CLAUDE_SKILL_DIR}` is the documented standard for making a
same-skill link resolve regardless of install location; a bare
`references/foo.md` relative path is NOT expanded by anything and the
`file` read op resolves a non-absolute path against the WORKSPACE, not the
skill's own directory (`reyn/core/op_runtime/file.py`'s `_resolve_for_gate`
/ `is_absolute()` branch) -- so a bare relative link would silently never
reach the skill's own `references/` directory).

That convention only pays off if the FULL chain actually closes for a
model: (1) `skill_list` hands the model this skill's absolute SKILL.md
`path`; (2) reading SKILL.md through the real `file` read op runs it
through `reyn.plugins.skill_load.load_skill_body`
(`is_skill_body_path` routes any read whose basename is `SKILL.md`,
`alias_claude=True` unconditionally so the `${CLAUDE_*}` alias table in
`reyn.plugins.tokens.CLAUDE_ALIAS_MAP` applies), which expands
`${CLAUDE_SKILL_DIR}` to this skill's own absolute directory; (3) the model
resolves the now-absolute link and issues an ordinary `file` read op
against it. This test exercises that FULL chain against the REAL
`reyn.core.op_runtime.file.handle` op for BOTH reads (SKILL.md's own body,
then each reference) -- not `read_builtin_body_bytes` in isolation and not
a hand-rolled token substitution -- proving the token actually expands at
the same seam a model's read would go through, and that the resulting
absolute path is readable.

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

# Matches the router body's `${CLAUDE_SKILL_DIR}`-prefixed reference link
# shape, e.g.
# "[incoming-events-coalescing-and-wake.md](${CLAUDE_SKILL_DIR}/references/incoming-events-coalescing-and-wake.md)".
_TOKEN_LINK_RE = re.compile(
    r"\]\(\$\{CLAUDE_SKILL_DIR\}/(references/[^)\s]+\.md)\)"
)
# Same shape, but against the EXPANDED body (token already replaced by an
# absolute path) -- captures the absolute path directly.
_EXPANDED_LINK_RE = re.compile(r"\]\((/[^)\s]+\.md)\)")


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


def _read_skill_md_via_real_op(ctx: OpContext) -> str:
    """Reads SKILL.md through the REAL `file` read op -- the same op a
    model's read would go through, including the skill-load token-expansion
    pass (`is_skill_body_path` routes on basename == "SKILL.md")."""
    op = FileIROp(kind="file", op="read", path=str(_SKILL_PATH))
    result = _run(handle(op, ctx))
    assert result.get("status") == "ok", result
    return result["content"]


def test_raw_disk_body_uses_the_claude_skill_dir_token_not_a_bare_relative_path() -> None:
    """Tier 2: vacuity guard -- the UNEXPANDED body on disk actually contains
    `${CLAUDE_SKILL_DIR}`-prefixed links (not bare `references/...` links,
    which the file read op would resolve against the workspace, never
    reaching this skill's own directory)."""
    raw_body = _SKILL_PATH.read_text(encoding="utf-8")
    token_links = _TOKEN_LINK_RE.findall(raw_body)
    assert token_links, (
        "expected ${CLAUDE_SKILL_DIR}-prefixed reference links in the raw "
        "SKILL.md body on disk"
    )
    bare_relative = re.findall(r"\]\((references/[^)\s]+\.md)\)", raw_body)
    assert not bare_relative, (
        f"found bare relative reference link(s), never expanded by anything "
        f"and not resolvable against the skill's own directory: {bare_relative}"
    )


def test_reading_skill_md_through_the_real_op_expands_the_token_to_an_absolute_path() -> None:
    """Tier 2: OS invariant -- reading SKILL.md through the REAL `file` read
    op (the seam `reyn.plugins.skill_load.load_skill_body` hooks) expands
    `${CLAUDE_SKILL_DIR}` to this skill's own absolute directory. Falsify
    surface: if the expansion seam were not wired for this file, the raw
    `${CLAUDE_SKILL_DIR}` token would still be present in the op's result
    and this assertion would fail."""
    ctx = _make_ctx()
    expanded_body = _read_skill_md_via_real_op(ctx)

    assert "${CLAUDE_SKILL_DIR}" not in expanded_body, (
        "the read op did not expand ${CLAUDE_SKILL_DIR} -- skill-load "
        "token expansion did not run for this file"
    )

    expanded_links = _EXPANDED_LINK_RE.findall(expanded_body)
    assert expanded_links, "expected absolute reference links after expansion"
    for link in expanded_links:
        assert link.startswith(str(_SKILL_DIR)), (
            f"expanded link {link!r} does not start with this skill's own "
            f"directory {_SKILL_DIR} -- ${{CLAUDE_SKILL_DIR}} resolved wrong"
        )


def test_every_expanded_link_resolves_on_disk_under_references() -> None:
    """Tier 2: OS invariant, property (1) -- each ${CLAUDE_SKILL_DIR}-expanded
    link, once the real read op has expanded it, resolves to a real file
    under this skill's references/ directory."""
    ctx = _make_ctx()
    expanded_body = _read_skill_md_via_real_op(ctx)
    for link in _EXPANDED_LINK_RE.findall(expanded_body):
        resolved = Path(link).resolve()
        assert resolved.is_file(), f"expanded reference link does not exist on disk: {resolved}"
        assert resolved.parent == _REFERENCES_DIR.resolve()


def test_references_dir_has_no_orphans_beyond_the_linked_set() -> None:
    """Tier 2: OS invariant -- bidirectional parity: every file physically
    under references/ is reachable from a (token-prefixed) body link
    (unlinked = unreachable dead weight)."""
    raw_body = _SKILL_PATH.read_text(encoding="utf-8")
    linked_names = {Path(rel).name for rel in _TOKEN_LINK_RE.findall(raw_body)}
    on_disk_names = {p.name for p in _REFERENCES_DIR.glob("*.md")}
    assert on_disk_names == linked_names, (
        f"references/ set {on_disk_names} != linked set {linked_names}"
    )


def test_every_linked_reference_is_readable_through_the_real_file_read_op() -> None:
    """Tier 2: OS invariant, property (3), the FULL reachability chain --
    read SKILL.md through the real op to get the expanded absolute links,
    then issue the SAME real `file` read op a model would call against each
    resulting path, with a PermissionResolver whose project_root does not
    contain the package (the wheel-install condition). Asserts actual
    byte-for-byte content, not merely a non-None/no-raise."""
    ctx = _make_ctx()
    expanded_body = _read_skill_md_via_real_op(ctx)
    links = _EXPANDED_LINK_RE.findall(expanded_body)
    assert links, "expected at least one expanded reference link to exercise"

    for link in links:
        resolved = Path(link).resolve()
        expected_bytes = resolved.read_bytes()

        op = FileIROp(kind="file", op="read", path=str(resolved))
        result = _run(handle(op, ctx))

        assert result.get("status") == "ok", result
        content = result["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        assert content == expected_bytes, f"content mismatch for {resolved}"


def test_reference_files_do_not_themselves_contain_unexpanded_tokens() -> None:
    """Tier 2: OS invariant -- token expansion is documented (and wired,
    `is_skill_body_path`) to run ONLY for the basename `SKILL.md`; a
    reference file under references/ must not rely on it, since it would be
    handed to the model unexpanded (owner ruling)."""
    for ref_path in _REFERENCES_DIR.glob("*.md"):
        text = ref_path.read_text(encoding="utf-8")
        assert "${CLAUDE_SKILL_DIR}" not in text, (
            f"{ref_path} contains an unexpanded ${{CLAUDE_SKILL_DIR}} token -- "
            "only SKILL.md itself is expanded"
        )
