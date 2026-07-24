"""``load_skill`` op handler — the dedicated skill-activation verb (FP-0066
proposal §6/§11 P0, #3247; umbrella issue #3247).

**Why this module exists.** ADR 0064 §3.5 (#3070) called for a dedicated
skill-load verb; #2971 instead chose "reading IS the invocation, no
dedicated verb" and routed skill-load INSIDE the ordinary ``file`` read op
via a special case (``is_skill_body_path`` in ``reyn.core.op_runtime.file``).
That drift scattered skill-specific logic through a general-purpose file
handler: provenance classification, ``${env}``-expansion trust-gating, and
the **#3196 symlink-swap-safe resolve-once** security surface all lived in
``file.py`` for a reason that had nothing to do with plain file reading. This
module is the owner-ratified correction: ``load_skill`` owns the WHOLE
responsibility; ``file.read`` (``reyn.core.op_runtime.file``) is stripped
back to plain file reading with zero skill special-casing.

**What moved here, verbatim (a move, not a semantics change):**

- ``read_builtin_body_bytes`` / ``read_plugin_body_bytes`` bypass (same
  helpers ``file.py`` still uses for its OWN, unrelated general
  builtin/plugin body reads — reused here, not duplicated).
- The config-registered-skill-entry provenance class
  (``_config_registered_skill_body_provenance``, unchanged from its
  pre-extraction shape in ``file.py``).
- ``reyn.plugins.skill_load.load_skill_body`` for
  ``${REYN_*}``/``${CLAUDE_*}``/``${env:VAR}`` invocation-time expansion.
- The ``skill_body_loaded`` audit-event (same name, same fields — names +
  counts only, NEVER the expanded value, per #3196/#3198's firm design).

**★ #3196 resolve-once — the security-critical invariant.** ``op.path`` is
resolved via ``reyn.core.op_runtime.context.resolve_path_for_gate`` EXACTLY
ONCE per call, into ``resolved_path``, and that single string is reused for
EVERY later decision: the permission gate, the builtin/plugin bypass check,
the config-registered-entry provenance check, AND the actual byte read.
Resolving separately for "is this trusted" vs "what do I read" reopens the
exact symlink-swap TOCTOU #3196 closed — an earlier revision of the
pre-extraction ``file.py`` code did this and had to be fixed in co-vet round
2; this module must never regress to that split. See
``resolve_path_for_gate``'s own docstring for the residual (NOT closed)
cross-process race note.

**Provenance-unclassified paths are not an error.** A ``load_skill`` call
against a path that resolves to none of the builtin/plugin/config-registered
provenance classes still succeeds with plain, unexpanded content — no
``skill_body_loaded`` event fires (fails CLOSED: no expansion, never open).
This mirrors the pre-extraction ``file.read`` fallback exactly (an
unregistered ``SKILL.md``-shaped path was never a hard error there either).
"""
from __future__ import annotations

from pathlib import Path

from reyn.builtin.docs import read_builtin_body_bytes
from reyn.data.workspace.text_codec import decode_text_or_none
from reyn.plugins.body_read import read_plugin_body_bytes
from reyn.plugins.skill_load import load_skill_body
from reyn.schemas.models import LoadSkillIROp

from . import register
from .context import OpContext, resolve_path_for_gate
from .context import sandbox_policy_from_ctx as _sandbox_policy_from_ctx


def _config_registered_skill_body_provenance(ctx: OpContext, resolved_path: str) -> bool:
    """True when *resolved_path* — an ALREADY ``resolve_path_for_gate``d
    absolute path — matches a config-registered skill entry's body path
    (#3196's third provenance class, alongside builtin/registered-plugin).

    Enumerated from ``ctx.available_skills`` — the SAME registered-skill
    snapshot ``:skill`` invocation resolves against (``Session``/
    ``RouterHostAdapter``'s ``_available_skills``, built by
    ``reyn.data.skills.registry.build_skill_registry`` from config), never a
    hand-curated path list of this module's own (a second, drifting
    enumeration would just relocate the same "curated subset diverges from
    the registry" bug #3194 fixed elsewhere). Each entry's ``path`` is
    resolved the SAME way ``reyn.interfaces.skill_invoke.resolve_skill_body``
    resolves it (project-root-relative or absolute, then ``.resolve()`` —
    the same symlink/``..``-collapsing call ``resolve_path_for_gate`` already
    made on *resolved_path*), so a symlinked or ``../``-relative entry still
    compares equal to its real target instead of by literal string.
    ``ctx.available_skills`` is ``None`` in test/phase-fallback construction
    (see ``OpContext.available_skills``'s docstring) — this simply returns
    ``False`` there, failing CLOSED (no expansion), never open.
    """
    entries = getattr(ctx, "available_skills", None)
    if not entries:
        return False
    ws = getattr(ctx, "workspace", None)
    base_dir = ws.base_dir if ws is not None else Path.cwd()
    for entry in entries:
        entry_path = getattr(entry, "path", None)
        if not entry_path:
            continue
        p = Path(entry_path).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        try:
            if str(p.resolve()) == resolved_path:
                return True
        except OSError:
            continue
    return False


async def handle(op: LoadSkillIROp, ctx: OpContext) -> dict:
    # #3196: resolve `op.path` EXACTLY ONCE and thread THIS SAME string into
    # every later decision (permission gate, builtin/plugin bypass,
    # config-registered provenance check, the actual byte read). Do not
    # reintroduce a second `resolve_path_for_gate` call anywhere below this
    # point — see this module's docstring / `resolve_path_for_gate`'s own
    # docstring for the exact TOCTOU this closes.
    resolved_path = resolve_path_for_gate(ctx, op.path)

    # #2913/plugin-body-parity bypass: a builtin skill body (package-shipped,
    # `reyn.builtin.registry`) or a registered-plugin skill body
    # (`~/.reyn/plugins/`) resolves outside `project_root` in every deploy —
    # both readers gate on REAL registration (importlib package membership /
    # `plugin_install.is_registered_plugin_root`), so a non-None result here
    # already IS a trusted provenance, not merely a permission-bypass signal.
    provenance: "str | None" = None
    body_bytes = read_builtin_body_bytes(resolved_path)
    if body_bytes is not None:
        provenance = "builtin"
    else:
        body_bytes = read_plugin_body_bytes(resolved_path)
        if body_bytes is not None:
            provenance = "plugin"

    if ctx.permission_resolver is not None and body_bytes is None:
        sandbox = _sandbox_policy_from_ctx(ctx)
        await ctx.permission_resolver.require_file_read(
            ctx.permission_decl, resolved_path, ctx.actor,
            sandbox_policy=sandbox, bus=ctx.intervention_bus,
        )

    if body_bytes is not None:
        raw_bytes, found = body_bytes, True
    else:
        raw_bytes, found = ctx.workspace.read_file_bytes(resolved_path)
    if not found:
        ctx.events.emit("tool_executed", op="load_skill", path=op.path, found=False)
        return {
            "kind": "load_skill",
            "path": op.path,
            "status": "not_found",
            "error": f"file not found: {op.path}",
            "content": "",
        }

    content, _detected_encoding = decode_text_or_none(raw_bytes)
    if content is None:
        ctx.events.emit("tool_executed", op="load_skill", path=op.path, mode="binary_skipped")
        return {
            "kind": "load_skill",
            "path": op.path,
            "status": "error",
            "binary": True,
            "error": (
                f"binary file ({len(raw_bytes)} bytes) — a skill body must be "
                "text; its bytes were not loaded into context."
            ),
            "content": "",
        }
    enc_field = {"encoding": _detected_encoding} if _detected_encoding else {}

    # #3196: filename-shaped provenance is necessary but never sufficient —
    # require a resolved (symlink/`..`-collapsed) provenance class too.
    # `provenance` may already be "builtin"/"plugin" from the bypass check
    # above; otherwise check the config-registered-entry class (the ONLY
    # class that check hasn't already ruled on). Judged against
    # `resolved_path` — the SAME single resolve the bytes above were
    # actually read from — never a fresh, independent resolve.
    if provenance is None and _config_registered_skill_body_provenance(ctx, resolved_path):
        provenance = "config_entry"

    if provenance is not None:
        content, env_names_expanded, env_names_denied = load_skill_body(
            content,
            skill_path=resolved_path,
            project_dir=ctx.workspace.base_dir,
            # SKILL.md is the one open standard (agentskills.io) multiple
            # hosts share, so every skill-load IS the ingestion boundary
            # ADR 0064 §3.6 scopes the `${CLAUDE_*}` alias to — there is no
            # narrower "this one is Claude-authored" signal to gate on.
            alias_claude=True,
            # #3198: the allowlist gate on WHAT a body that already cleared
            # the #3196 provenance gate may read from os.environ.
            # ctx.permission_decl is a required (non-Optional) OpContext
            # field — always present.
            permission_decl=ctx.permission_decl,
        )
        # NEVER include the expanded/denied VALUES here — an audit-event is
        # not a second secret-storage location (#3196 firm design, extended
        # by #3198 to the denial side too).
        ctx.events.emit(
            "skill_body_loaded",
            path=op.path,
            provenance=provenance,
            env_tokens_expanded=len(env_names_expanded),
            env_names_expanded=list(env_names_expanded),
            env_tokens_denied=len(env_names_denied),
            env_names_denied=list(env_names_denied),
        )
    # else: unregistered SKILL.md-shaped path (#3196) — an ordinary,
    # unremarkable read. `content` stays byte-identical to disk; no
    # expansion, no `skill_body_loaded` event (the `tool_executed` event
    # below still fires as for any other load).

    # #1209-style read-bounding: a skill body is loaded WHOLE (no offset/
    # limit windowing — this is a dedicated "load the whole thing" verb, not
    # a general paginated reader), but an oversized body is still
    # self-bounding rather than blowing the context unconditionally.
    from reyn.core.context_builder import control_ir_inline_cap

    model_str: "str | None" = None
    if ctx.resolver is not None:
        try:
            model_str = ctx.resolver.resolve(ctx.model).model
        except Exception:
            model_str = None
    cap = control_ir_inline_cap(model_str, events=ctx.events, phase=ctx.actor)

    if len(content) > cap:
        truncated_content = content[:cap]
        ctx.events.emit(
            "tool_executed", op="load_skill", path=op.path,
            truncated=True, total_chars=len(content),
        )
        return {
            "kind": "load_skill",
            "path": op.path,
            "status": "truncated",
            "content": truncated_content,
            "total_chars": len(content),
            "_truncated": True,
            "_self_bounded": True,
            "note": (
                f"content truncated to fit context ({len(truncated_content)} of "
                f"{len(content)} chars shown); the full skill body is on disk at "
                f"{op.path!r}."
            ),
            **enc_field,
        }

    ctx.events.emit("tool_executed", op="load_skill", path=op.path)
    return {
        "kind": "load_skill",
        "path": op.path,
        "status": "ok",
        "content": content,
        "_self_bounded": True,
        **enc_field,
    }


from reyn.core.offload.canonical import load_skill_to_canonical  # noqa: E402

register("load_skill", handle, canonical=load_skill_to_canonical)
