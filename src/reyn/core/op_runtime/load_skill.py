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

**#3629 — the result carries a second, persist-safe content variant when a
provenance class matched.** ``content`` stays the fully-expanded body (what
the model reads THIS turn, unchanged from before #3629). ``content_history``
/ ``token_map`` / ``skill_source_path`` (present only when ``content_history
is not None``, i.e. a provenance class matched) are what
``load_skill_to_canonical`` threads onto the persisted history entry
instead — the location tokens (``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}``)
are left literal in ``content_history`` rather than baked to an absolute
value, so a later rename/move (#3588 was one instance) can never freeze a
now-dead path into ``.reyn/agents/<id>/history.jsonl``, which is immutable
by design. See ``reyn.plugins.skill_load.load_skill_body``'s docstring for
the full mechanism and ``refresh_location_tokens`` for how a persisted entry
re-resolves fresh on replay.

**#4699 — the threat-scan chokepoint.** ``skill_install``'s scan
(``op_runtime/skill_install.py``) only covers the frontmatter ``description``
and only runs when a skill is registered THROUGH that op — a hand-written
``.reyn/config/skills.yaml`` entry bypasses it entirely. ``load_skill`` is
the one path every skill body crosses regardless of how it was registered,
so the OS-required scan (``content_guard.scan_for_threats(scope="strict")``,
same shape ``skill_install`` uses) runs here, on the final ``content`` —
mirroring the existing ``scan_tool_result``/``fence_tool_result`` tool-result
chokepoint (``router_loop.py``), not a new mechanism. A blocking-severity
match returns ``status="blocked"`` with an empty ``content`` — the body
never reaches this turn's context. ``skill_install``'s own scan stays as an
additive install-time fail-fast; it is not what makes a loaded body safe.
"""
from __future__ import annotations

from pathlib import Path

from reyn.builtin.docs import read_builtin_body_bytes
from reyn.data.text_codec import decode_text_or_none
from reyn.plugins.body_read import read_plugin_body_bytes
from reyn.plugins.skill_load import load_skill_body
from reyn.schemas.models import LoadSkillIROp

# Module-level import so tests can monkeypatch the threat-scan callables —
# same reasoning `skill_install.py` states for the identical import (#4699).
from reyn.security.content_guard import first_blocking_match, scan_for_threats

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

    # #3629: set only when `load_skill_body` actually ran (below) — the
    # persist-safe (location tokens left literal) body + the token map the
    # caller (`router_loop.py`'s tool-result assembly) stashes on the
    # persisted history entry's `meta`, plus the identity `content_history`
    # can be re-resolved against on replay. `None` here (unregistered path,
    # the `else` branch below) means "nothing to persist differently" — the
    # ordinary `content` is used for both current-turn and history, exactly
    # as before #3629.
    content_history: "str | None" = None
    location_token_map: "dict[str, str] | None" = None
    if provenance is not None:
        content, content_history, location_token_map, env_names_expanded, env_names_denied = (
            load_skill_body(
                content,
                skill_path=resolved_path,
                project_dir=ctx.workspace.base_dir,
                # SKILL.md is the one open standard (agentskills.io) multiple
                # hosts share, so every skill-load IS the ingestion boundary
                # ADR 0064 §3.6 scopes the `${CLAUDE_*}` alias to — there is
                # no narrower "this one is Claude-authored" signal to gate on.
                alias_claude=True,
                # #3198: the allowlist gate on WHAT a body that already
                # cleared the #3196 provenance gate may read from os.environ.
                # ctx.permission_decl is a required (non-Optional) OpContext
                # field — always present.
                permission_decl=ctx.permission_decl,
            )
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

    # ── #4699 (②, the ratified band): threat-scan the body BEFORE it can
    # reach the model's context. `skill_install`'s own scan (scope="strict",
    # `skill_install.py:494-530`) only covers the frontmatter `description`
    # and only fires when a skill is installed THROUGH that op — a
    # hand-written `.reyn/config/skills.yaml` entry (or any other path this
    # op is called against) never passes through it. `load_skill` is the one
    # chokepoint every skill body crosses regardless of how it was
    # registered, so this is where the OS actually gates the capability
    # (CLAUDE.md Security lens: "no capability reaches the world without
    # passing the gatekeeper") — mirrors the existing `scan_tool_result`/
    # `fence_tool_result` tool-result chokepoint (`router_loop.py`), not a
    # new mechanism. Scans the FINAL `content` (post-expansion when
    # provenance matched) — what this turn's call actually delivers — so a
    # payload split across `${...}` expansion still reads as one string.
    # `skill_install`'s own scan is unaffected by this addition and stays in
    # place as an install-time fail-fast; it is additive UX, not a second
    # gate this one depends on.
    _ts = getattr(ctx, "threat_scan", None)
    if _ts is not None and getattr(_ts, "enabled", True) and content:  # #4523: shadow default matches ThreatScanConfig.enabled's own declared True
        _matches = scan_for_threats(content, _ts, scope="strict")
        if _matches:
            for _m in _matches:
                ctx.events.emit(
                    "skill_body_threat_match",
                    pattern_id=_m.pattern_id,
                    severity=_m.severity,
                    scope=_m.scope,
                )
            _block = first_blocking_match(_matches, getattr(_ts, "block_severity", "block"))
            if _block is not None:
                ctx.events.emit(
                    "skill_body_threat_blocked",
                    pattern_id=_block.pattern_id,
                    severity=_block.severity,
                    path=op.path,
                )
                return {
                    "kind": "load_skill",
                    "path": op.path,
                    "status": "blocked",
                    "content": "",
                    "error": (
                        f"load blocked: SKILL.md body matched threat pattern "
                        f"'{_block.pattern_id}' ({_block.scope}/{_block.severity}). "
                        "The body was not loaded into context."
                    ),
                }

    # #1209-style read-bounding: a skill body is loaded WHOLE (no offset/
    # limit windowing — this is a dedicated "load the whole thing" verb, not
    # a general paginated reader), but an oversized body is still
    # self-bounding rather than blowing the context unconditionally.
    #
    # #4381 PR-5: the cap is a RESOURCE BOUND (bytes, model-independent,
    # config-driven — architect design) — shares `ctx.read_cap_config` with
    # `file.py`'s own read op (`load_skill` "同じ整理が当たり... read_file
    # と同じ扱いへ一緒に移る" — architect). No more model resolution here.
    from reyn.core.context_builder import byte_safe_prefix, control_ir_inline_cap

    cap = control_ir_inline_cap(ctx.read_cap_config)

    # #3629: `content_history`/`token_map` (set above, only when
    # `load_skill_body` ran) ride along in the SAME field shape regardless of
    # the truncated/ok branch below — `load_skill_to_canonical` reads them
    # off the raw result dict, never off `content` itself.
    history_fields: dict = {}
    if content_history is not None:
        history_fields["content_history"] = (
            byte_safe_prefix(content_history, cap)
            if len(content_history.encode("utf-8")) > cap
            else content_history
        )
        history_fields["token_map"] = location_token_map
        history_fields["skill_source_path"] = op.path

    if len(content.encode("utf-8")) > cap:
        # #4431 (architect correction): was a bare `content[:cap]` char slice
        # with NO resume position returned at all — the note pointed at "the
        # full body is on disk" but gave no way to continue from where this
        # read stopped. load_skill is deliberately NOT a paginated reader
        # (module docstring) — the fix is not to add offset/limit here, it's
        # to make the escape hatch this note already promises actually work:
        # `read_file` (same file, since `op.path` is a real on-disk path for
        # every provenance class this module and file.py share the same
        # builtin/plugin bypass for) already supports a 0-indexed LINE
        # `offset` (#4381/#4432 fixed its own char_offset-within-a-line
        # sibling). Whole-line accumulation (mirroring op_runtime/file.py's
        # own #1209/#2335 algorithm) so `next_offset` names a genuine line
        # boundary `read_file(path=op.path, offset=next_offset)` can resume
        # from without re-showing or dropping a partial line.
        #
        # #4381 PR-5: measured in BYTES (UTF-8 encoded length), never
        # len(str) — see file.py's own truncation loop, mirrored here.
        all_lines = content.splitlines(keepends=True)
        shown: list[str] = []
        acc = 0  # bytes
        next_offset = 0
        for i, line in enumerate(all_lines):
            line_bytes = len(line.encode("utf-8"))
            if shown and acc + line_bytes > cap:
                next_offset = i
                break
            if not shown and line_bytes > cap:
                # A single line alone exceeds the cap (e.g. a minified/
                # one-line body) — byte-safe truncate it so `content` stays
                # genuinely bounded (never split a multi-byte codepoint);
                # `read_file(offset=0)` on resume hits this exact same line
                # and returns ITS OWN `next_char_offset` (file.py's #2335
                # mechanism), so no new field is needed here.
                shown.append(byte_safe_prefix(line, cap))
                acc += len(shown[-1].encode("utf-8"))
                next_offset = i
                break
            shown.append(line)
            acc += line_bytes
            next_offset = i + 1
        truncated_content = "".join(shown)
        ctx.events.emit(
            "tool_executed", op="load_skill", path=op.path,
            truncated=True, total_chars=len(content), next_offset=next_offset,
        )
        return {
            "kind": "load_skill",
            "path": op.path,
            "status": "truncated",
            "content": truncated_content,
            "total_chars": len(content),
            "next_offset": next_offset,
            "_truncated": True,
            "note": (
                f"content truncated to fit context ({len(truncated_content)} of "
                f"{len(content)} chars shown); the full skill body is on disk "
                f"at {op.path!r} — continue reading with "
                f"read_file(path={op.path!r}, offset={next_offset})."
            ),
            **enc_field,
            **history_fields,
        }

    ctx.events.emit("tool_executed", op="load_skill", path=op.path)
    return {
        "kind": "load_skill",
        "path": op.path,
        "status": "ok",
        "content": content,
        **enc_field,
        **history_fields,
    }


from reyn.core.offload.canonical import load_skill_to_canonical  # noqa: E402

register("load_skill", handle, canonical=load_skill_to_canonical)
