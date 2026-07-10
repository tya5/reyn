"""reyn.data.pipelines.registry — config-entries → PipelineRegistry loader.

The production population path for the pipeline feature. Pipelines are
registered PURELY via explicit ``pipelines.entries`` declarations in config —
the same registration model as ``reyn.data.skills.registry.build_skill_registry``
and ``mcp.servers`` (clean break: the prior directory-scan model — a blind
``scan_dirs`` glob over a ``pipelines/`` directory for any ``*.yaml`` file
present — is removed; a DSL file with no config entry is invisible to every
session).

Unlike skills (explicit ``entries`` config, metadata only, never parses the
file), a pipeline must be PARSED to be usable — the ``PipelineRegistry`` stores
live ``Pipeline`` objects and surfaces their name+description to the LLM (IS-5's
D19 catalog enumerator). So this loader parses each entry's ``path`` via
:func:`~reyn.core.pipeline.parser.parse_pipeline_docs` (a file may hold MORE
than one ``pipeline:`` document — #2722) and registers EVERY parsed pipeline.

**Namespacing is ALWAYS ON (#2722).** A registered pipeline's global name is
uniformly ``{entry-key}.{local-name}`` — for every pipeline, regardless of how
many ``pipeline:`` documents the file holds. The config entry key is PURELY a
namespace label; it no longer needs to equal any pipeline's declared name (the
old ``key == declared-name`` coupling is gone). A single-``pipeline:`` file
under ``entries: {orders: {path}}`` whose doc is ``pipeline: main`` registers as
``orders.main`` — there is no bare-name registration anywhere.

``call``/``match`` target resolution is a clean dot/no-dot dichotomy (#2722),
applied HERE (the loader owns the entry key; the parser stays config-agnostic —
H4):

  - a **dot-less** target (``call: {pipeline: helper}``) is a same-file SIBLING
    reference — it resolves to ``{entry-key}.helper``. An unresolved sibling (no
    ``pipeline:`` doc named ``helper`` in the same file) is a load-time error
    (fail-loud; NO silent fallback to some unrelated global).
  - a **dotted** target (``call: {pipeline: other.helper}``) is a GLOBAL
    reference — left unchanged, resolved against the whole registry at run time.

Because ``.`` is reserved in both declared names and entry keys (#2722 R1 — a
dot-less name has 0 dots, a global has exactly 1), dot-presence alone
classifies a reference with no ambiguity.

Failure posture is PER-ENTRY ISOLATED, not process-fatal: a malformed DSL file,
an unreadable path, an entry key containing the reserved ``.`` (R1), an
unresolved dot-less sibling reference, an intra-file duplicate declared name
(R2), or a duplicate global name across entries are each caught INSIDE the
``entries`` loop — logged as a Python ``logging`` warning AND durably emitted as
a ``pipeline_load_failed`` P6 event (via ``emit_cli_event`` — see its own
docstring; this loader has no per-session ``ctx``/``EventLog`` handle threaded
in, and is called from multiple distinct entrypoints, so the session-independent
sink is the right fit), then SKIPPED — the remaining entries still load. This
was changed from an earlier fail-loud design (raise straight out of
``build_pipeline_registry``) because ``SessionFactoryConfig.from_config``
(``reyn/runtime/factory_config.py``) calls this with no enclosing try/except,
at EVERY session construction — so one broken pipeline entry anywhere in
``pipelines.entries`` crashed reyn's ENTIRE startup (`reyn chat` / `reyn web`),
not just that one pipeline. Visibility is still preserved (the warning + event
carry the exact same descriptive message the old raise would have) — this is
NOT the fully-silent ``skills.registry`` pattern (a typo must not silently
vanish with zero trace); it is "visible but non-fatal".

Duplicate GLOBAL name across entries (``{entry-key}.{local-name}`` collision —
possible only if two entries share the same key, or a namespaced name coincides
with another): the FIRST-registered entry with that name wins; a later entry
that collides is the one skipped/logged (dict iteration order = config
declaration order — the first entry an operator listed keeps its registration).

``raw_pipelines=None`` (the util/no-root path) → an empty registry; an
empty/absent ``pipelines:`` block or an empty ``entries`` map → an empty
registry (zero pipelines is a valid, non-error state — same as skills). These
are the only cases where ``build_pipeline_registry`` returns early WITHOUT
even entering the per-entry loop — they are not "failures", just an absent
config shape, so nothing is logged.

``strict=True`` (opt-in, default ``False``) restores the ORIGINAL fail-loud
posture — the first per-entry failure raises :class:`PipelineLoadError`
straight out of ``build_pipeline_registry``, with NO logging/eventing (the
raise itself is the signal). This exists for ``Session._reapply_pipelines``
(``reyn/runtime/session.py``, the ``pipelines`` hot-reload seam): that seam
needs ATOMIC last-good-registry semantics — reject the ENTIRE rebuild on any
broken entry and keep serving the old registry unchanged, rather than
silently dropping just the broken pipeline from a live session's registry
graph mid-reload (a "the pipeline this operator was relying on just vanished
from the running session" surprise). Session-factory construction
(``SessionFactoryConfig.from_config``) is the opposite case — a NEW session
about to start has no "old registry" to fall back to, so ``strict=False``
(the default) is the right posture there: isolate the bad entry, keep the
good ones, let the session start.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from reyn.core.pipeline.registry import PipelineRegistry

logger = logging.getLogger(__name__)


class PipelineLoadError(ValueError):
    """A ``pipelines.entries`` declaration could not be loaded (unreadable file,
    malformed DSL, a reserved ``.`` in the entry key, an unresolved dot-less
    sibling reference, or a duplicate global name). Raised internally by
    :func:`_load_one_entry` and caught PER-ENTRY
    by :func:`build_pipeline_registry` (logged + durably emitted as a
    ``pipeline_load_failed`` event, then the entry is skipped — see the module
    docstring). Still importable/raisable directly by callers that want the
    old fail-loud posture for a single entry (e.g. an install-time validation
    path that should refuse a bad file rather than silently accept it)."""


def _entry_path(project_root: Path, raw_path: str) -> Path:
    """Resolve an entry's ``path`` — project-root-relative or absolute, mirroring
    the skill entry ``path`` resolution convention."""
    p = Path(raw_path)
    return p if p.is_absolute() else (project_root / p)


def _resolve_ref(target: str, key: str, siblings: "set[str]", *, where: str) -> str:
    """Resolve a single ``call``/``match`` target under the ``{key}.`` namespace
    (#2722, the loader-side dot/no-dot dichotomy — H4).

    - A **dotted** target (``other.helper``) is a GLOBAL reference — returned
      unchanged (resolved against the whole registry at run time).
    - A **dot-less** target (``helper``) is a same-file SIBLING reference — it
      must match a sibling ``pipeline:`` doc name in ``siblings`` and is rewritten
      to ``f"{key}.{helper}"``. An unresolved sibling raises
      :class:`PipelineLoadError` (fail-loud; there is NO bare global to fall back
      to under uniform namespacing)."""
    if "." in target:
        return target
    if target not in siblings:
        raise PipelineLoadError(
            f"{where}: dot-less call/match target {target!r} does not match any "
            f"sibling pipeline in the same file (siblings: {sorted(siblings)!r}) "
            "— a dot-less target resolves to a same-file sibling only; use "
            "'<entry-key>.<name>' to target a pipeline registered from another entry"
        )
    return f"{key}.{target}"


def _namespace_step(step: object, key: str, siblings: "set[str]", *, where: str) -> object:
    """Recursively rewrite every ``call``/``match`` target inside ``step`` under
    the ``{key}.`` namespace (#2722). Walks the full step tree — a target can be
    nested in a ``fold.do``, a ``for_each.do``/``collect``, or a
    ``parallel`` branch/``collect``, arbitrarily deep. Frozen dataclasses are
    rebuilt via :func:`dataclasses.replace`; linear steps (transform/tool/agent —
    no pipeline reference) pass through unchanged."""
    from dataclasses import replace

    from reyn.core.pipeline.executor import (
        CallStep,
        FoldStep,
        ForEachStep,
        MatchStep,
        ParallelStep,
    )

    if isinstance(step, CallStep):
        return replace(step, pipeline=_resolve_ref(step.pipeline, key, siblings, where=where))
    if isinstance(step, MatchStep):
        new_cases = {
            label: replace(
                case,
                pipeline=_resolve_ref(
                    case.pipeline, key, siblings, where=f"{where} match case {label!r}"
                ),
            )
            for label, case in step.cases.items()
        }
        new_default = (
            replace(
                step.default,
                pipeline=_resolve_ref(
                    step.default.pipeline, key, siblings, where=f"{where} match default"
                ),
            )
            if step.default is not None
            else None
        )
        return replace(step, cases=new_cases, default=new_default)
    if isinstance(step, FoldStep):
        return replace(step, do=_namespace_step(step.do, key, siblings, where=where))
    if isinstance(step, ForEachStep):
        return replace(
            step,
            do=_namespace_step(step.do, key, siblings, where=where),
            collect=_namespace_step(step.collect, key, siblings, where=where),
        )
    if isinstance(step, ParallelStep):
        return replace(
            step,
            branches={
                name: _namespace_step(branch, key, siblings, where=where)
                for name, branch in step.branches.items()
            },
            collect=_namespace_step(step.collect, key, siblings, where=where),
        )
    return step


def _namespace_pipeline(pipeline: object, key: str, siblings: "set[str]") -> object:
    """Return a copy of ``pipeline`` namespaced under ``key`` (#2722): its
    declared name becomes ``f"{key}.{name}"`` and every ``call``/``match`` target
    in its step tree is resolved via :func:`_namespace_step` (dot-less siblings
    prefixed with ``{key}.``, dotted globals left as-is). Raises
    :class:`PipelineLoadError` for an unresolved dot-less sibling reference."""
    from dataclasses import replace

    where = f"pipeline {pipeline.name!r}"  # type: ignore[attr-defined]
    new_steps = [
        _namespace_step(step, key, siblings, where=f"{where} step {i}")
        for i, step in enumerate(pipeline.steps)  # type: ignore[attr-defined]
    ]
    return replace(pipeline, name=f"{key}.{pipeline.name}", steps=new_steps)  # type: ignore[attr-defined]


def _load_one_entry(
    key: str, raw: dict, project_root: "Path", registry: PipelineRegistry,
) -> None:
    """Load + register ONE ``pipelines.entries.<key>`` declaration (#2722:
    a file may hold multiple ``pipeline:`` documents — ALL are registered,
    each under the uniform ``{key}.{local-name}`` namespace).

    Raises :class:`PipelineLoadError` for:
      - a config entry key containing the reserved ``.`` (#2722 R1),
      - a missing ``path``,
      - an unreadable / malformed DSL file (path included),
      - an intra-file duplicate declared name (#2722 R2, surfaced by the parser),
      - an unresolved dot-less sibling ``call``/``match`` reference (#2722),
      - a duplicate GLOBAL name across entries.

    Callers (:func:`build_pipeline_registry`) catch this per-entry — see the
    module docstring for the visible-but-non-fatal posture. This function
    itself still fails loud; it is the isolation boundary that changed, not
    the validation logic.
    """
    # Deferred import: parser pulls the pipeline executor/schema stack, which is
    # heavier than this loader's own surface — keep module import cheap.
    from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_docs
    from reyn.core.pipeline.schema import SchemaRegistry

    # #2722 R1: '.' is RESERVED as the namespace separator — a config entry key
    # is the namespace label, so a key containing '.' would make the derived
    # global name ambiguous ('a.b' + doc 'c' -> 'a.b.c', two dots). Fail loud.
    if "." in key:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: a config entry key must not contain "
            "'.' — '.' is the namespace separator (a registered pipeline's "
            "global name is '<entry-key>.<pipeline-name>')."
        )

    raw_path = str(raw.get("path") or "").strip()
    if not raw_path:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r} has no 'path' — a pipeline entry "
            "must declare the DSL file to load."
        )
    path = _entry_path(project_root, raw_path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: could not read {path}: {exc}"
        ) from exc

    # One SchemaRegistry per FILE, shared by every pipeline parsed from it — a
    # `schema:` document in the file serves every sibling pipeline (#2722).
    schema_registry = SchemaRegistry()
    try:
        pipelines = parse_pipeline_docs(text, schema_registry)
    except PipelineParseError as exc:
        # Covers malformed DSL, a declared name with a reserved '.' (R1), and an
        # intra-file duplicate declared name (R2).
        raise PipelineLoadError(
            f"pipelines.entries.{key!r}: malformed pipeline file {path}: {exc}"
        ) from exc

    # Sibling-name set for dot-less `call`/`match` resolution (#2722).
    siblings = {p.name for p in pipelines}

    for pipeline in pipelines:
        if not pipeline.name:
            # parse_pipeline_docs requires a non-empty ``pipeline:`` name, so
            # this is defensive — a nameless Pipeline has no key to register under.
            raise PipelineLoadError(
                f"pipelines.entries.{key!r}: file {path} produced a pipeline "
                "with no declared name — a 'pipeline:' name is required."
            )
        # Namespace: prefix the declared name AND every resolved dot-less sibling
        # ref with `{key}.` (raises PipelineLoadError for an unresolved sibling).
        namespaced = _namespace_pipeline(pipeline, key, siblings)
        try:
            registry.register(namespaced.name, namespaced, schema_registry)
        except ValueError as exc:
            # Duplicate GLOBAL name across entries — the registry's own
            # re-registration guard. First-registered entry keeps the name; this
            # (later) entry is the one that fails.
            raise PipelineLoadError(
                f"pipelines.entries.{key!r}: file {path} registers name "
                f"{namespaced.name!r} which is already registered by an earlier "
                f"entry: {exc}"
            ) from exc


def build_pipeline_registry(
    raw_pipelines: "dict[str, Any] | None", project_root: "Path",
    *, strict: bool = False,
) -> PipelineRegistry:
    """Build a populated :class:`PipelineRegistry` from the ``pipelines:`` config.

    For each ``pipelines.entries.<key>`` declaration, ``path`` is resolved
    (project-root-relative or absolute), read, and parsed via
    ``parse_pipeline_docs`` into one-or-more ``Pipeline``s + a shared
    ``SchemaRegistry`` (so a registered pipeline's ``verify: schema`` steps
    resolve), then EVERY parsed pipeline is registered under the uniform
    ``{key}.{local-name}`` namespace (#2722).

    ``raw_pipelines=None`` → an empty registry (the util/no-root path). An
    empty/absent ``pipelines:`` block, or a ``pipelines:`` block with no
    ``entries``, → an empty registry (zero-config default — no pipelines
    registered until the operator or an install tool declares one). These two
    cases return before the per-entry loop even starts — they describe an
    absent config shape, not a failure, so nothing is logged.

    This function itself never raises for a per-entry failure. A reserved-``.``
    entry key (R1), a missing ``path``, an unreadable / malformed DSL file, an
    intra-file duplicate declared name (R2), an unresolved dot-less sibling
    reference, or a duplicate global name are each caught PER ENTRY (via
    :func:`_load_one_entry` raising :class:`PipelineLoadError`), logged as a
    ``logging`` warning, durably emitted as a ``pipeline_load_failed`` P6
    event (``reyn.core.events.events.emit_cli_event`` — see the module
    docstring for why this sink), and then skipped — the remaining entries
    still load normally. A duplicate global name resolves first-registered-
    wins: the later entry is the one logged/skipped.
    """
    from reyn.core.events.events import emit_cli_event

    registry = PipelineRegistry()
    # ``None`` = the util/no-root path (from_config without a project_root) →
    # never register anything.
    if not isinstance(raw_pipelines, dict):
        return registry

    raw_entries = raw_pipelines.get("entries")
    if not isinstance(raw_entries, dict):
        return registry

    for key, raw in raw_entries.items():
        key = str(key)
        if not isinstance(raw, dict):
            continue  # malformed entry — lenient-default pattern matching skills
        if not bool(raw.get("enabled", True)):
            continue

        try:
            _load_one_entry(key, raw, project_root, registry)
        except PipelineLoadError as exc:
            if strict:
                # Preserve the original fail-loud contract for callers that
                # need atomic all-or-nothing semantics (Session._reapply_pipelines'
                # hot-reload seam — see the docstring's ``strict`` paragraph).
                raise
            logger.warning(
                "pipelines.entries.%r failed to load and was skipped: %s",
                key, exc,
            )
            try:
                emit_cli_event(
                    "pipeline_load_failed",
                    key=key,
                    path=str(raw.get("path") or ""),
                    error=str(exc),
                )
            except Exception:  # noqa: BLE001 -- durable-capture must never crash startup
                pass
            continue

    return registry


__all__ = ["build_pipeline_registry", "PipelineLoadError"]
