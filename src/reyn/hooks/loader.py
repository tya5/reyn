"""reyn.hooks.loader — parse and validate the ``hooks:`` config block (#1800 slice A).

Entry point: ``load_hooks(raw)`` — accepts the raw value of the ``hooks:``
key from a reyn.yaml dict and returns a ``HookRegistry``.

Validation is *structural only* (field presence, types, hook-point membership,
the template_push / exec / exec_capture / pipeline_launch mutual-exclusion
— exactly one, #2608 H3 adds ``pipeline_launch``). Template *semantics* are
not validated here — rendering is a later slice.

#3226 Phase 4: ``exec`` / ``exec_capture`` (renamed from ``shell_exec`` /
``shell_push`` — naming honesty, the runner never shell-interpreted a
string) now take an **argv list** (``list[str]``, non-empty, every item a
non-empty string) rather than a shell-command string — a clean break, not a
compat alias; an operator's pre-Phase-4 ``shell_exec: "cmd arg1 arg2"``
becomes ``exec: ["cmd", "arg1", "arg2"]``.

Validation errors raise ``HookConfigError`` with a decision-enabling message
that names the entry index and the failing field so the operator can fix the
config immediately.
"""
from __future__ import annotations

import logging

from reyn.hooks.composer import COMPOSED_KIND_PREFIX
from reyn.hooks.event_pattern import from_legacy_matcher
from reyn.hooks.event_pattern import validate_against_schema as validate_event_pattern
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import (
    ALLOWED_HOOK_POINTS,
    HookConfigError,
    HookDef,
    PipelineLaunchBlock,
    PushBlock,
)
from reyn.hooks.schema_registry import HookSchemaError, bare_point, canonical_kind

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_push_block(raw: object, entry_index: int) -> PushBlock:
    """Validate and convert the ``template_push:`` sub-dict to a ``PushBlock``.

    All Jinja2 template strings are stored raw; no rendering here.
    """
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}].template_push must be a mapping, "
            f"got {type(raw).__name__!r}."
        )

    # Required: message
    message = raw.get("message")
    if message is None:
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.message is required."
        )
    if not isinstance(message, str):
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.message must be a string, "
            f"got {type(message).__name__!r}."
        )
    if not message.strip():
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.message must not be empty."
        )

    # Optional: wake (bool or Jinja2 template string → bool, default True)
    raw_wake = raw.get("wake", True)
    if not isinstance(raw_wake, (bool, str)):
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.wake must be a bool or template string, "
            f"got {type(raw_wake).__name__!r}."
        )
    wake: bool | str = raw_wake

    # Optional: push_when (Jinja2 template string → bool, default "true")
    raw_push_when = raw.get("push_when", "true")
    if not isinstance(raw_push_when, (bool, str)):
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.push_when must be a bool or template string, "
            f"got {type(raw_push_when).__name__!r}."
        )
    # Normalise a plain bool to its string form so the type is uniform.
    if isinstance(raw_push_when, bool):
        push_when: str = "true" if raw_push_when else "false"
    else:
        push_when = raw_push_when

    # Optional: session (template string or None)
    raw_session = raw.get("session", None)
    if raw_session is not None and not isinstance(raw_session, str):
        raise HookConfigError(
            f"hooks[{entry_index}].template_push.session must be a string or null, "
            f"got {type(raw_session).__name__!r}."
        )
    session: str | None = raw_session if raw_session else None

    return PushBlock(
        message=message,
        wake=wake,
        push_when=push_when,
        session=session,
    )


def _parse_pipeline_launch_block(raw: object, entry_index: int) -> PipelineLaunchBlock:
    """Validate and convert the ``pipeline_launch:`` sub-dict to a
    ``PipelineLaunchBlock`` (#2608 H3).

    ``input_template`` is stored raw (a dict or a Jinja2 template string); no
    rendering here — rendering happens at dispatch time against the hook's
    ``template_vars`` (``reyn.hooks.render.render_pipeline_input``).
    """
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}].pipeline_launch must be a mapping, "
            f"got {type(raw).__name__!r}."
        )

    name = raw.get("name")
    if name is None:
        raise HookConfigError(
            f"hooks[{entry_index}].pipeline_launch.name is required."
        )
    if not isinstance(name, str):
        raise HookConfigError(
            f"hooks[{entry_index}].pipeline_launch.name must be a string, "
            f"got {type(name).__name__!r}."
        )
    if not name.strip():
        raise HookConfigError(
            f"hooks[{entry_index}].pipeline_launch.name must not be empty."
        )

    input_template = raw.get("input_template", None)
    if input_template is not None and not isinstance(input_template, (dict, str)):
        raise HookConfigError(
            f"hooks[{entry_index}].pipeline_launch.input_template must be a "
            f"mapping, string, or null, got {type(input_template).__name__!r}."
        )

    return PipelineLaunchBlock(name=name, input_template=input_template)


def _parse_matcher(raw: object, entry_index: int) -> "dict[str, str] | None":
    """Validate and convert the ``matcher:`` sub-dict (#2608 H2).

    Structural only: ``matcher`` must be a mapping of string keys to string
    values (a per-hook-point matchable-field ALLOWLIST is deliberately NOT
    enforced here — the loader has no per-point payload schema, and a future
    external-event source (H4/H5) may introduce its own matchable fields
    with zero change to this function; ``reyn.hooks.matcher.matches`` is the
    seam that interprets field names). ``None`` or ``{}`` -> ``None``
    (fire-always default, unchanged from H1).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}].matcher must be a mapping of string field -> "
            f"string pattern, got {type(raw).__name__!r}."
        )
    if not raw:
        return None
    matcher: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise HookConfigError(
                f"hooks[{entry_index}].matcher keys must be non-empty strings, "
                f"got {key!r}."
            )
        if not isinstance(value, str):
            raise HookConfigError(
                f"hooks[{entry_index}].matcher[{key!r}] must be a string pattern, "
                f"got {type(value).__name__!r}."
            )
        matcher[key] = value
    return matcher


def _parse_entry(
    raw: object,
    entry_index: int,
    composed_schemas: "dict[str, frozenset[str]] | None" = None,
) -> HookDef:
    """Validate one raw hooks list entry and return a ``HookDef``."""
    if not isinstance(raw, dict):
        raise HookConfigError(
            f"hooks[{entry_index}] must be a mapping, "
            f"got {type(raw).__name__!r}."
        )

    # ── on (required) ──────────────────────────────────────────────────────
    # YAML 1.1 (PyYAML default) parses the bare keyword ``on`` as boolean
    # ``True``; quote-free ``on: turn_end`` in reyn.yaml becomes
    # ``{True: 'turn_end', ...}``.  Try the string key first (= quoted
    # ``"on"``), then fall back to the boolean key ``True`` so that both
    # ``on: turn_end`` and ``"on": turn_end`` work.
    on_raw = raw.get("on", raw.get(True))
    if on_raw is None:
        raise HookConfigError(
            f"hooks[{entry_index}].on is required."
        )
    if not isinstance(on_raw, str):
        raise HookConfigError(
            f"hooks[{entry_index}].on must be a string, "
            f"got {type(on_raw).__name__!r}."
        )
    # Hook-Event Redesign Phase 1 (proposal 0059 §2 review-pass): the bare
    # short-form (``turn_end``) is the pre-existing spelling and stays the
    # canonical INTERNAL key (HookDef.on / HookRegistry / HookDispatcher are
    # unchanged); the namespaced kind (``builtin:lifecycle:turn_end``) is a
    # newly-accepted ALIAS, normalized to the bare form right here so every
    # existing hooks.yaml keeps working unmodified and a new full-form config
    # resolves to the exact same HookDef.on value.
    on_key = bare_point(canonical_kind(on_raw.strip().lower()))
    # Hook-Event Redesign Phase 5 part 1 (proposal 0059 §9 item 3 / #2881,
    # the "#5 structural-non-reentry -> §224 valve-metered-allow" transition
    # ratified in #2880's §9 annotation): ``composed:<name>`` is an OPEN
    # namespace (one entry per ``composers:`` config's ``emit.kind``, not a
    # fixed enum), so it is accepted by PREFIX here rather than being added to
    # ``ALLOWED_HOOK_POINTS`` (a closed frozenset of the 10 builtin points).
    # Its consumer is ``reyn.hooks.composed_consumer.
    # ComposedEventConsumer``, not ``HookDispatcher.dispatch()``'s Sync loop.
    # #2889: a composed-kind hook's ``matcher`` IS now schema-validated below,
    # against ``composed_schemas`` (every composed event's payload shape is
    # the fixed ``{"inputs", "correlation_key"}`` — see ``event_pattern.
    # validate_against_schema``'s docstring) — closing the Phase-3 open-set
    # gap this class of hook was left in.
    if on_key not in ALLOWED_HOOK_POINTS and not on_key.startswith(COMPOSED_KIND_PREFIX):
        sorted_points = ", ".join(sorted(ALLOWED_HOOK_POINTS))
        raise HookConfigError(
            f"hooks[{entry_index}].on={on_raw!r} is not a recognised hook-point. "
            f"Allowed: {sorted_points}, or a {COMPOSED_KIND_PREFIX}<name> composed-event kind."
        )
    # #2889 sub-decision (b), included: a dangling composed-kind subscription
    # (``on: composed:X`` with NO configured composer producing ``composed:X``)
    # can never fire — the SAME silent-never-fire class the matcher check
    # below closes, and the reorder in ``Session.__init__`` (composer defs
    # built before hooks) makes the FULL known composed-kind universe
    # available here. ``composed_schemas is None`` (the default — a caller
    # with no composer configuration to thread, e.g. a bare ``load_hooks(raw)``
    # test call) skips this check entirely, preserving the pre-#2889
    # permissive posture for every such caller.
    if (
        composed_schemas is not None
        and on_key.startswith(COMPOSED_KIND_PREFIX)
        and on_key not in composed_schemas
    ):
        raise HookConfigError(
            f"hooks[{entry_index}].on={on_raw!r} names a composed kind no configured "
            f"composer produces — it would never fire. Known composed kinds: "
            f"{sorted(composed_schemas) or '(none configured)'}."
        )

    # ── scheme: exactly one of template_push / exec / exec_capture /
    # pipeline_launch (#2069, #2608 H3; #3226 Phase 4 renamed shell_exec/
    # shell_push → exec/exec_capture) ───────────────────────────────────────
    present = [
        k for k in ("template_push", "exec", "exec_capture", "pipeline_launch")
        if k in raw
    ]
    if len(present) > 1:
        raise HookConfigError(
            f"hooks[{entry_index}]: template_push / exec / exec_capture / "
            f"pipeline_launch are mutually exclusive; specify exactly one "
            f"(got {present})."
        )
    if not present:
        raise HookConfigError(
            f"hooks[{entry_index}]: exactly one of template_push / exec / "
            f"exec_capture / pipeline_launch is required."
        )

    # ── template_push block ──────────────────────────────────────────────────
    template_push: PushBlock | None = None
    if "template_push" in raw:
        template_push = _parse_push_block(raw["template_push"], entry_index)

    # ── exec / exec_capture (#3226 Phase 4: argv-list-only — a clean break
    # from the pre-Phase-4 shell-command STRING; the runner already ran argv
    # via shlex.split/shell=False, so this closes the string/argv shape gap
    # rather than changing execution semantics) ────────────────────────────
    def _argv(key: str) -> "tuple[str, ...]":
        value = raw[key]
        if not isinstance(value, list):
            raise HookConfigError(
                f"hooks[{entry_index}].{key} must be a list of argv strings "
                f"(e.g. [\"scripts/cleanup.sh\", \"--force\"]), "
                f"got {type(value).__name__!r}."
            )
        if not value:
            raise HookConfigError(f"hooks[{entry_index}].{key} must not be an empty list.")
        for item_index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise HookConfigError(
                    f"hooks[{entry_index}].{key}[{item_index}] must be a "
                    f"non-empty string, got {item!r}."
                )
        return tuple(value)

    exec_argv: "tuple[str, ...] | None" = _argv("exec") if "exec" in raw else None
    exec_capture_argv: "tuple[str, ...] | None" = _argv("exec_capture") if "exec_capture" in raw else None

    # ── per-hook sandbox knobs: subprocess (#2827) / network + write_paths ────
    # (#3005). The three axes an operator owns per-site — the same triad a stdio
    # MCP server exposes. They exist per-hook because the agent-level
    # ``sandbox.policy`` is resolved on the op path only and does NOT reach a
    # hook shell; the hook site is where a hook shell's sandbox is decided.
    #
    # Eager-rejection model (#2976's 'write_paths'/'auth' on a non-stdio MCP
    # server): a security field that only SOME schemes honour must be rejected on
    # the others, never silently ignored — a silently-ignored security field reads
    # as an applied restriction that was never applied. Key PRESENCE (not the
    # value) expresses the operator's will, so `subprocess: false` on a
    # template_push hook is rejected too: it would restrict nothing.
    def _exec_only(key: str) -> None:
        if exec_argv is None and exec_capture_argv is None:
            raise HookConfigError(
                f"hooks[{entry_index}].{key} is only supported on an "
                f"exec / exec_capture hook (it scopes the sandboxed exec "
                f"argv); this hook declares {present[0]!r}."
            )

    def _sandbox_bool(key: str) -> bool | None:
        if key not in raw:
            return None
        _exec_only(key)
        value = raw[key]
        if not isinstance(value, bool):
            raise HookConfigError(
                f"hooks[{entry_index}].{key} must be a boolean, got "
                f"{type(value).__name__!r}."
            )
        return value

    subprocess_raw = _sandbox_bool("subprocess")
    network_raw = _sandbox_bool("network")

    # write_paths — a list of path strings. An explicit `[]` is a real (empty)
    # grant, so presence, not truthiness, decides "the operator wrote this";
    # stored as a tuple because HookDef is frozen.
    write_paths_raw: "tuple[str, ...] | None" = None
    if "write_paths" in raw:
        _exec_only("write_paths")
        value = raw["write_paths"]
        if not isinstance(value, list):
            raise HookConfigError(
                f"hooks[{entry_index}].write_paths must be a list of path strings, "
                f"got {type(value).__name__!r}."
            )
        for item_index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise HookConfigError(
                    f"hooks[{entry_index}].write_paths[{item_index}] must be a "
                    f"non-empty path string, got {item!r}."
                )
        write_paths_raw = tuple(value)

    # ── pipeline_launch block (#2608 H3) ──────────────────────────────────────
    pipeline_launch: PipelineLaunchBlock | None = None
    if "pipeline_launch" in raw:
        pipeline_launch = _parse_pipeline_launch_block(raw["pipeline_launch"], entry_index)

    # ── matcher (optional, #2608 H2 — a field->pattern filter dict) ────────
    matcher: "dict[str, str] | None" = _parse_matcher(raw.get("matcher", None), entry_index)

    # Hook-Event Redesign Phase 3 (proposal 0059 §10 Q-reyn-4) + #2889: a
    # matcher that names a payload field the kind's schema does NOT carry is
    # a silent "never fire" footgun (a ``srever`` typo matches nothing, and the
    # operator gets no signal). ∴ fail-loud at load — validate the matcher (as
    # a payload-only ``EventPattern``) against the kind's schema: a builtin
    # point's ``BUILTIN_HOOK_SCHEMAS`` entry, OR (#2889) a ``composed:*``
    # kind's entry in ``composed_schemas`` (the fixed ``{"inputs",
    # "correlation_key"}`` shape every Composer emits — closes the Phase-3
    # open-set gap ``composed:*`` was left in). A kind with NO schema in
    # either source (a future/custom point, or a composed kind when the
    # caller passed no ``composed_schemas`` — the schema-driven OPEN SET)
    # stays permissive: ``validate_against_schema`` is a no-op there (proposal
    # §4 open-set posture, preserved). This is additive correctness — a
    # schema-VALID matcher still parses/evaluates byte-identically; only a
    # schema-EXTERNAL (dead) matcher now surfaces as a HookConfigError.
    if matcher is not None:
        try:
            validate_event_pattern(from_legacy_matcher(matcher), on_key, composed_schemas)
        except HookSchemaError as exc:
            raise HookConfigError(f"hooks[{entry_index}].matcher: {exc}") from exc

    # ── name (optional, #1800 slice 6) — the [hook:name] attribution label; ──
    # absent / blank → None (the dispatcher defaults it to the hook-point).
    name_raw = raw.get("name", None)
    if name_raw is not None and not isinstance(name_raw, str):
        raise HookConfigError(
            f"hooks[{entry_index}].name must be a string or null, "
            f"got {type(name_raw).__name__!r}."
        )
    name: str | None = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else None

    return HookDef(
        on=on_key,
        name=name,
        template_push=template_push,
        exec=exec_argv,
        exec_capture=exec_capture_argv,
        pipeline_launch=pipeline_launch,
        matcher=matcher,
        # #2827/#3005: None when omitted (keep the floor) vs an explicit value
        # (the operator's expressed will) — the distinction the knobs hinge on.
        subprocess=subprocess_raw,
        network=network_raw,
        write_paths=write_paths_raw,
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_hooks(
    raw: object,
    composed_schemas: "dict[str, frozenset[str]] | None" = None,
) -> HookRegistry:
    """Parse and validate the ``hooks:`` value from a reyn.yaml dict.

    Parameters
    ----------
    raw:
        The value of the ``hooks:`` key from the config dict.  May be
        ``None`` (absent), an empty list, or a list of hook dicts.
        Any other type is logged as a warning and treated as empty.
    composed_schemas:
        #2889 — a ``{emit_kind: frozenset(field_names)}`` map for every
        currently-configured ``composed:*`` kind (``reyn.runtime.session.
        Session`` builds this from ``self._composer_defs`` and passes it
        here). Used to (a) schema-validate a ``composed:*`` hook's
        ``matcher`` (mirrors the Phase-3 builtin-point enforce-at-load path)
        and (b) fail loud on a ``composed:*`` subscription with no producing
        composer. ``None`` (the default) skips both checks — the pre-#2889
        permissive posture, for callers with no composer configuration to
        thread (most direct ``load_hooks(raw)`` test calls).

    Returns
    -------
    HookRegistry
        A ready registry containing all validated ``HookDef`` objects in
        registration (list) order.

    Raises
    ------
    HookConfigError
        On structural validation failure.  The message names the offending
        entry index and the failing constraint so the operator can fix it.
    """
    if raw is None:
        return HookRegistry([])

    if not isinstance(raw, list):
        _log.warning(
            "config key 'hooks' must be a list; got %s — ignoring it.",
            type(raw).__name__,
        )
        return HookRegistry([])

    defs: list[HookDef] = []
    for idx, entry in enumerate(raw):
        defs.append(_parse_entry(entry, idx, composed_schemas))

    return HookRegistry(defs)
