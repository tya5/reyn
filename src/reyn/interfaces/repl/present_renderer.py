"""present renderer — `ResolvedPresentation.nodes` → Rich renderables (FP-0054 PR-B).

**Invariant (do not violate — FP-0054 §5 Option B, PR-B review).** Every leaf string
here reaches a MARKUP-INERT Rich object — `Text` / `Syntax` / `Markdown` — and this
module never calls `console.print(str)` (or hands a bare `str` to a Rich API that
itself defaults to markup interpretation, e.g. `Table.add_column`/`add_row`). Rich
console-markup injection becomes structurally impossible: `guard.py`'s terminal
strategy strips ESC/control sequences only (the surface-universal threat) and
deliberately does NOT escape `[tag]`-shaped text — see its module docstring for why
that used to be here and was wrong (Rich markup is reachable ONLY through
`console.print(str, markup=True)`, a renderer choice, not a sink property).
`rich.markdown.Markdown` is the one exception: it interprets CommonMark, not Rich
console markup, so a `markdown` component's raw (control-stripped) text goes to it
directly — no wrapping needed, no injection vector either way.

Pure: takes the already bound/neutralized/capped render model and a target width;
produces a Rich renderable. No I/O — the caller (`interfaces/repl/renderer.py`'s
`InlineChatRenderer`) owns the `Console` + `run_in_terminal` print.
"""
from __future__ import annotations

from typing import Any


class HalfBlockImage:
    """A Rich renderable for an image, built from nothing but `Segment`s —
    no third-party image-rendering library (#4474, follows flowview's own
    `examples/image.py`, 0.19.0, verbatim in shape).

    **Why this exists at all, not a `textual-image` renderable** (the full
    chain, since this supersedes #3970's own dependency decision): FlowView
    paints rows as CELLS and repaints them independently while scrolling,
    so it can only place a renderable that *occupies cells*. Sixel draws
    pixels relative to the CURSOR instead — measured (flowview's own
    README): 0 cells, 0 characters in the row, so a virtualized painter
    has no way to position or clip it (the root cause of a live-verified
    duplicated/ghosted image after a row-height reflow). The other
    cell-based option, Kitty Unicode-placeholder mode
    (`textual_image.renderable.tgp`), is ALSO unreliable: WezTerm/Konsole
    report Kitty-graphics support but render the placeholder's normally-
    invisible combining diacritics as literal visible garbled glyphs, and
    there is no query for "do placeholders actually draw" (only for base
    Kitty-graphics support) — live-verified directly, matching flowview's
    own 0.18.2/0.18.3 findings from the SAME report. Half-block cells need
    no protocol negotiation at all and render correctly everywhere.

    Each text row carries two source pixel rows: the upper half-block
    glyph (▀) is drawn in the upper pixel's colour, over the lower pixel's
    colour as background — ordinary coloured cells, exactly what FlowView
    needs to place and clip."""

    def __init__(self, image: "Any", width: int, height: int) -> None:
        # height is in TEXT rows; sample two pixel rows per text row.
        self._img = image.convert("RGB").resize((max(1, width), max(1, height) * 2))
        self._w, self._h = max(1, width), max(1, height) * 2

    def __rich_console__(self, console: "Any", options: "Any"):
        from rich.segment import Segment
        from rich.style import Style

        px = self._img.load()
        for y in range(0, self._h - 1, 2):
            row_segments = []
            for x in range(self._w):
                top, bottom = px[x, y], px[x, y + 1]
                row_segments.append(Segment(
                    "▀",
                    Style(
                        color=f"rgb({top[0]},{top[1]},{top[2]})",
                        bgcolor=f"rgb({bottom[0]},{bottom[1]},{bottom[2]})",
                    ),
                ))
            yield from row_segments
            yield Segment("\n")

    def __rich_measure__(self, console: "Any", options: "Any") -> "Any":
        from rich.measure import Measurement

        return Measurement(self._w, self._w)


# owner ruling: every rendered image gets a FIXED row height, in cells —
# `HalfBlockImage` (above) takes an explicit `width`/`height` in cells with
# no built-in aspect-ratio derivation of its own (unlike `textual_image`'s
# `ImageSize`), so `decode_image_body` below computes `width` FROM this
# fixed height and the image's own pixel aspect ratio directly. #4474/
# owner's standing "no unjustified number embedded without a comment or a
# user-facing override" rule: 20 is the shipped DEFAULT (matches
# `ImageConfig.row_height_cells`'s own default, `config/chat.py`) — the
# "right" row count is a function of the OPERATOR'S OWN terminal height,
# not something this repo can decide for every environment, so it is
# config-driven via `set_image_row_height_cells` below, called once at
# startup with the real `ReynConfig` value. This module-level default is
# only what a caller with NO config threaded (every non-TUI caller, tests)
# falls back to.
_image_row_height_cells = 20


def set_image_row_height_cells(row_height_cells: int) -> None:
    """Record the operator-configured fixed image row height, in cells
    (#4474) — called once by `run_textual_chat` with `config.image.
    row_height_cells` (already validated positive by `_build_image_config`;
    this setter re-validates defensively for any other caller)."""
    global _image_row_height_cells
    if isinstance(row_height_cells, int) and row_height_cells > 0:
        _image_row_height_cells = row_height_cells


def _cell(value: Any) -> "Any":
    """Wrap a leaf value as a markup-inert `Text` — the ONE conversion every string
    destined for a Rich renderable goes through in this module."""
    from rich.text import Text

    return Text(str(value))


def _render_keyvalue(node: dict) -> "Any":
    from rich.table import Table

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold")
    grid.add_column()
    for row in node.get("rows", []):
        grid.add_row(_cell(row.get("label", "")), _cell(row.get("value", "")))
    return grid


def _truncation_tail_text(node: dict) -> "Any | None":
    """A dim `Text` for a `table`/`list` node's `truncation_tail` (§5 visible
    truncation indicator, issue #2669) — `None` when the node was not capped."""
    from rich.text import Text

    tail = node.get("truncation_tail")
    if not tail:
        return None
    return Text(tail, style="dim")


def _render_table(node: dict) -> "Any":
    from rich.console import Group
    from rich.table import Table

    columns = node.get("columns", [])
    table = Table(show_lines=False)
    for col in columns:
        table.add_column(_cell(col.get("header", "")))
    n_rows = max((len(col.get("cells", [])) for col in columns), default=0)
    for i in range(n_rows):
        table.add_row(*[
            _cell(col["cells"][i]) if i < len(col.get("cells", [])) else _cell("")
            for col in columns
        ])
    tail = _truncation_tail_text(node)
    if tail is not None:
        return Group(table, tail)
    return table


def _render_list(node: dict) -> "Any":
    from rich.console import Group

    items = [_cell(f"• {item}") for item in node.get("items", [])]
    tail = _truncation_tail_text(node)
    if tail is not None:
        items.append(tail)
    return Group(*items)


# #4574: an artifact's inline body has NO upstream cap (binding.py's own
# "artifact" branch comment: the OS-derivation pass that fills `body` runs
# AFTER resolve_bindings, so `guard.cap_leaf`'s pre-render capping — the
# thing `_render_code_or_diff` above relies on — never sees it). This is the
# render layer's own bound instead, matching architect's #4574 fallback spec
# verbatim ("先頭 N 行" — the first N lines, never the whole file): a small
# HTML/text file inlined via the size probe (`INLINE_PROBE_MAX_BYTES`,
# artifact_payload.py) can still run to hundreds of lines, and this is a
# PREVIEW alongside the real `body["ref"]` (#4574 design C) — the truncation
# discards nothing that isn't ALSO fully openable via that ref.
_ARTIFACT_INLINE_PREVIEW_LINES = 20


def _render_artifact(node: dict) -> "Any":
    """#4574: an ``artifact`` node — an LLM-produced file the terminal can't
    render natively (see ``core/present/catalog.py``'s own "artifact"
    section). Before this, EVERY artifact node fell through to
    ``_render_node``'s unregistered-component fallback
    (``<unsupported present component 'artifact'>``) regardless of source
    or inline, on every text client (REPL + this TUI's own body renderer,
    which calls this same module — ``presenter.py``'s ``_render_row``).

    This is a FALLBACK, not a real HTML/office/pdf renderer (out of scope —
    the terminal genuinely cannot render those) — it shows what the #4574
    design calls for: name/media_type/description, plus (when the resolved
    payload carries an inline preview — design C mints a ``ref`` alongside
    it for small source-backed files, so this is a PREVIEW, not the only
    way to see the content) the first ``_ARTIFACT_INLINE_PREVIEW_LINES``
    lines. A row with an ``error`` (e.g. ``source_not_found`` — the source
    file vanished between present-time and render-time) or nothing resolved
    yet (a soft binding-miss, present's own philosophy) shows that instead
    of guessing at a body."""
    from rich.console import Group
    from rich.text import Text

    if "error" in node:
        return Text(f"[artifact: {node['error']}]", style="dim")
    body = node.get("body")
    if not isinstance(body, dict):
        return Text("[artifact: nothing resolved]", style="dim")
    name = node.get("name")
    media_type = node.get("media_type") or "unknown type"
    header = f"📎 {name}" if name else "📎 artifact"
    header += f" ({media_type})"
    lines: "list[Any]" = [Text(header, style="bold")]
    description = node.get("description")
    if description:
        lines.append(Text(str(description), style="dim"))
    inline = body.get("inline")
    if isinstance(inline, str):
        preview_lines = inline.splitlines()
        truncated = len(preview_lines) > _ARTIFACT_INLINE_PREVIEW_LINES
        preview = "\n".join(preview_lines[:_ARTIFACT_INLINE_PREVIEW_LINES])
        lines.append(Text(preview))
        if truncated:
            lines.append(Text("[preview truncated]", style="dim"))
    if "ref" in body:
        lines.append(Text("[open via the Artifacts tab]", style="dim"))
    return Group(*lines)


def _render_code_or_diff(node: dict, *, lexer: str) -> "Any":
    from rich.syntax import Syntax

    # §5 "cap before render": the text is already head-N-capped by guard.cap_leaf
    # (binding.py) before it ever reaches this render model — Syntax highlights only
    # the survivors, never the full pre-cap source.
    return Syntax(node.get("text", ""), lexer, word_wrap=True, background_color="default")


def _render_node(
    node: dict,
    image_cache: "dict[str, Any] | None" = None,
    decoded_image_cache: "dict[str, Any] | None" = None,
) -> "Any":
    from rich.markdown import Markdown
    from rich.text import Text

    component = node.get("component")
    if component == "text":
        return Text(node.get("text", ""))
    if component == "markdown":
        # CommonMark, not Rich console markup — no injection vector, no wrapping needed.
        return Markdown(node.get("text", ""))
    if component == "code":
        return _render_code_or_diff(node, lexer=node.get("language") or "text")
    if component == "diff":
        return _render_code_or_diff(node, lexer="diff")
    if component == "keyvalue":
        return _render_keyvalue(node)
    if component == "table":
        return _render_table(node)
    if component == "list":
        return _render_list(node)
    if component == "image":
        return _render_image(node, image_cache, decoded_image_cache)
    if component == "artifact":
        return _render_artifact(node)
    # Unregistered/future component — never crash the render loop over one bad node.
    return Text(f"<unsupported present component {component!r}>", style="dim")


class _SafeImageRenderable:
    """Wraps an image renderable (`HalfBlockImage`, #4474) so a PRINT-TIME
    failure degrades to a status line instead of propagating and breaking
    the render loop.

    Rich's `__rich_console__` protocol is a generator — `Console.print()`
    only iterates once it knows the real render width, so a failure inside
    it happens LATER than `_render_image`'s own `try/except` (which only
    wraps `decode_image_body`'s own decode step) can see. `HalfBlockImage`
    is reyn's own code (no known print-time bug class the way the removed
    `textual-image` dependency had — #4474's own PR removed a real,
    reproduced third-party bug of exactly this shape: a print-time-only
    failure a construction-time `try/except` structurally could not catch),
    but this wrapper stays as the general safety net for ANY future
    inner-renderable failure, not a specific bug's workaround.

    Also implements `__rich_measure__` — delegates to the inner
    renderable's own measurement (`HalfBlockImage.__rich_measure__` returns
    its real cell width). Without this, Rich has no `__rich_measure__` to
    find on THIS wrapper and falls back to a `Measurement(minimum=0,
    maximum=options.max_width)` for the enclosing `Group` (verified
    directly, #4433's live-verify finding: a caller that sizes a row to
    that measured MINIMUM before rendering — Textual's own auto-width/
    auto-height layout does this — then renders at width=0, which yields
    ZERO lines, the "image row renders as nothing, not even an error line"
    symptom)."""

    def __init__(self, inner: "Any", label: str) -> None:
        self._inner = inner
        self._label = label

    def __rich_console__(self, console: "Any", options: "Any"):
        from rich.text import Text

        try:
            yield from self._inner.__rich_console__(console, options)
        except Exception as exc:
            yield Text(
                f"[image loaded but could not render: {self._label} — {exc}]",
                style="dim",
            )

    def __rich_measure__(self, console: "Any", options: "Any") -> "Any":
        from rich.measure import Measurement

        inner_measure = getattr(self._inner, "__rich_measure__", None)
        if inner_measure is not None:
            try:
                return inner_measure(console, options)
            except Exception:
                pass
        # A measure-less inner or a measure failure still gets a real
        # (non-zero) width rather than propagating into the enclosing
        # Group's own minimum=0 fallback — 1 as the floor mirrors Rich's
        # own default `Measurement(1, max_width)` for an unmeasurable
        # renderable (see `rich.measure.Measurement.get`'s own fallback).
        return Measurement(1, options.max_width)


def decode_image_body(body: bytes) -> "Any":
    """Decode a fetched image body into a `HalfBlockImage` renderable
    (#4464/#4474) — the CPU-heavy step `_render_image`'s own inline
    fallback below does synchronously; extracted here so the presenter
    (`ReynPresenter._resolve_image`,
    `interfaces/inline/textual_chat/presenter.py`) can run it via
    `asyncio.to_thread` instead of on the event loop, WITHOUT duplicating
    the decode logic across the two modules.

    #4474: `HalfBlockImage` (this module) replaced `textual_image`'s
    renderables entirely — see that class's own docstring for the full
    chain (Sixel unplaceable / Kitty placeholder undetectably broken on
    WezTerm+Konsole / flowview's own 0.19.0 conclusion: half-block cells,
    no third-party image library needed).

    Raises on a corrupt/undecodable body or a missing optional dep (PIL) —
    the caller (both this module's own inline fallback and the presenter's
    off-thread path) is responsible for turning that into the established
    distinguishable failure state; this function itself stays a pure
    decode step with no fallback text of its own.

    Fixed height, preserved aspect ratio (owner ruling): `HalfBlockImage`
    takes an explicit `width`/`height` in cells with no aspect-ratio
    derivation of its own, so `width` is computed HERE from the fixed
    `_image_row_height_cells` and the image's own pixel aspect ratio —
    matching flowview's own `examples/image.py` convention (2 sampled
    pixel rows per text row, which self-corrects for a typical monospace
    cell's roughly 1:2 width:height pixel shape)."""
    import io

    from PIL import Image as PILImage

    pil_image = PILImage.open(io.BytesIO(body))
    # `PILImage.open()` alone only parses the header (lazy decode) — force
    # the decode NOW so a truncated/corrupt body raises HERE, not later at
    # paint time inside `HalfBlockImage.__rich_console__`.
    pil_image.load()
    height = _image_row_height_cells
    width = max(1, round(pil_image.width / pil_image.height * height)) if pil_image.height else 1
    return HalfBlockImage(pil_image, width=width, height=height)


def _render_image(
    node: dict,
    image_cache: "dict[str, Any] | None",
    decoded_image_cache: "dict[str, Any] | None" = None,
) -> "Any":
    """The `image` component (#3846 ②/③) — a PURE dict lookup + in-memory
    decode, never a fetch.

    `image_cache` (an app-owned `dict[str, ImageResolution]`, see
    `core/present/image_fetch.py`) is populated ELSEWHERE, by a resolution
    stage kicked off when the frame first arrives (`TextualChatApp.
    _begin_image_resolutions` -> `ReynPresenter.begin_image_resolution`) —
    this module's own docstring bans doing that fetch here (the "Pure: ...
    No I/O" invariant); decoding the already-fetched bytes via PIL is pure
    CPU, not I/O, so it stays within that invariant. `image_cache=None`
    (every non-TUI caller — plain ``ConsoleChatRenderer``, `reyn pipe`'s
    `StdoutPresentationRenderer`, and every existing test that calls this
    function without the new kwarg) gets the pre-#3846 `[image: alt]` text,
    byte-identical — no resolution stage exists on those surfaces yet.

    `decoded_image_cache` (#4464, an app-owned `dict[str, HalfBlockImage]`,
    default None) is consulted FIRST when present — the presenter's own
    `_resolve_image` now does the PIL decode + `HalfBlockImage(...)`
    construction (the CPU-heavy step this docstring already called out) on
    a background thread (`asyncio.to_thread`) as part of "preparing" an
    image, so THIS function's own job shrinks to "wrap the already-built
    renderable" on the cache-hit path — no decode work left to do here at
    all. Falling through to the inline decode below on a cache MISS (the
    key absent, or `decoded_image_cache=None` entirely) keeps every
    existing caller's behavior unchanged — this is an accelerator, not a
    new required input.

    #3846 ③ / #4474: on a successful resolution, renders real pixel colour
    via `HalfBlockImage` (this module's own renderable, no third-party
    image-rendering dependency — see that class's own docstring for why:
    Sixel is unplaceable and the Kitty Unicode-placeholder mode is
    undetectably broken on WezTerm/Konsole, both live-verified; half-block
    cells are the one form FlowView's cell-based painting model can place
    and clip everywhere with no protocol negotiation). Falls back to a
    status-line `Text` if decoding fails (a genuinely non-image or corrupt
    body) or if `pillow` is unavailable for any reason (defensive — it is a
    regular dep, so this should not happen in a normal install)."""
    from rich.text import Text

    alt = node.get("alt") or ""
    src = node.get("src") or ""
    label = alt or src
    if image_cache is None or not isinstance(src, str) or src not in image_cache:
        return Text(f"[image: {label}]", style="dim")
    res = image_cache[src]
    if not res.ok:
        return Text(f"[image failed: {label} — {res.error}]", style="dim")
    if decoded_image_cache is not None and src in decoded_image_cache:
        return _SafeImageRenderable(decoded_image_cache[src], label)
    try:
        # #4464: this inline decode is now a FALLBACK (the presenter's
        # `_resolve_image` decodes on a background thread via the SAME
        # `decode_image_body` helper and populates `decoded_image_cache`
        # above on the hit path) — kept so callers with no
        # `decoded_image_cache` (every pre-#4464 caller, and any future one
        # that doesn't opt in) still work byte-identically, just back on the
        # event loop for this CPU step as before. See `decode_image_body`'s
        # own docstring for why a bad body raises HERE, not at paint time.
        return _SafeImageRenderable(decode_image_body(res.body), label)
    except Exception as exc:
        # Anything from a corrupt/unsupported body to a missing optional dep
        # (defensive only — pillow/textual-image are regular deps) degrades
        # to the pre-③ status line rather than breaking the render loop.
        return Text(
            f"[image loaded but could not render: {label} — {exc}]", style="dim",
        )


def render_presentation_nodes(
    nodes: list[dict],
    *,
    image_cache: "dict[str, Any] | None" = None,
    decoded_image_cache: "dict[str, Any] | None" = None,
) -> "Any":
    """Convert a `ResolvedPresentation.nodes` render model into ONE Rich renderable
    (a `Group` of per-node renderables) — the one-shot inline block `present` prints
    to the conversation scrollback. See module docstring for the markup-inert
    invariant every branch here must preserve.

    `image_cache` (#3846 ②, default None) is forwarded to the `image`
    component branch — see :func:`_render_image`. `decoded_image_cache`
    (#4464, default None) is forwarded alongside it — see that function's
    own docstring for what it lets this render pass skip."""
    from rich.console import Group

    return Group(*[
        _render_node(node, image_cache, decoded_image_cache) for node in nodes
    ])


class StdoutPresentationRenderer:
    """`PresentationRenderer` (`core/present/renderer.py`) that prints a resolved
    presentation directly to **stdout** via a Rich `Console` — the headless sink a
    `present` op reaches from `reyn pipe run` (#2702), which has no live CUI outbox /
    output loop to route through.

    This is the SINK end of the same seam as the inline-CUI's `OutboxPresentationRenderer`
    (`runtime/session_buses.py`): the CUI variant is deliberately thin (it hands the raw
    render model to the outbox and lets the UI loop draining it own the Rich conversion),
    but a headless CLI run has no such loop — so this renderer owns the
    `render_presentation_nodes` conversion + the `Console.print` itself, reusing the SAME
    markup-inert render model this module already builds for the CUI. The op_runtime layer
    still never imports Rich; this interfaces-layer adapter is the seam where that boundary
    is respected.

    `surface_name = "terminal"`: the generic terminal-family surface (a registered
    neutralizer strategy in `core/present/guard.py` — ESC/control strip), so the guard's
    per-surface binding runs exactly as it does for the inline-CUI sink.

    Fire-and-continue: `render` must never raise into the `present` op (the op's ack is
    already derived from the resolved stats before this is called — see
    `op_runtime/present.py`'s fire-and-forget contract), so a Rich/IO failure is swallowed;
    a pipeline step must never crash on a display-only side effect.
    """

    surface_name = "terminal"

    def render(self, resolved: "Any") -> None:
        try:
            from rich.console import Console

            # Construct the Console per render so it binds the CURRENT sys.stdout
            # (honors capture/redirect); no ANSI is forced — a headless CLI writes
            # whatever the terminal (or a captured stream) supports.
            Console().print(render_presentation_nodes(resolved.nodes))
        except Exception:  # noqa: BLE001 — display-only fire-and-forget (see docstring)
            pass
