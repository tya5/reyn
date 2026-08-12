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


def _render_code_or_diff(node: dict, *, lexer: str) -> "Any":
    from rich.syntax import Syntax

    # §5 "cap before render": the text is already head-N-capped by guard.cap_leaf
    # (binding.py) before it ever reaches this render model — Syntax highlights only
    # the survivors, never the full pre-cap source.
    return Syntax(node.get("text", ""), lexer, word_wrap=True, background_color="default")


def _render_node(node: dict, image_cache: "dict[str, Any] | None" = None) -> "Any":
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
        return _render_image(node, image_cache)
    # Unregistered/future component — never crash the render loop over one bad node.
    return Text(f"<unsupported present component {component!r}>", style="dim")


class _SafeImageRenderable:
    """Wraps a `textual_image` renderable so a PRINT-TIME failure degrades to
    a status line instead of propagating and breaking the render loop.

    Rich's `__rich_console__` protocol is a generator Console.print() only
    iterates once it knows the real render width — so a failure inside it
    happens LATER than `_render_image`'s own `try/except` (which only wraps
    `TextualImage(...)` construction) can see. This is not hypothetical: a
    real `textual-image` 0.12.0 bug (the version pip resolves on reyn's
    Python 3.11 floor — 0.13+ requires >=3.12) raises `ValueError('height
    and width must be > 0')` from inside `__rich_console__` for a very
    small source image (reproduced: 8x8 and 1x1 fail, 16x16 does not) —
    `TextualImage(...)` construction itself never raises for these, so the
    pre-existing `try/except` in `_render_image` never sees it; this wrapper
    is the only thing standing between that bug and an uncaught exception
    reaching whatever Console the presenter/renderer layer owns.

    Also implements `__rich_measure__` (#3846 live-verify follow-up) —
    delegates to the inner `TextualImage`'s own measurement, which computes
    a real cell width from the decoded image. Without this, Rich has no
    `__rich_measure__` to find on THIS wrapper and falls back to a
    `Measurement(minimum=0, maximum=options.max_width)` for the enclosing
    `Group` (verified directly: `Measurement.get(console, options,
    render_presentation_nodes(...))` returns `minimum=0` for an image node
    with no measure delegation). A caller that sizes the row to that
    measured MINIMUM before rendering (Textual's own auto-width/auto-height
    layout does this) then renders at width=0, which yields ZERO lines —
    reproduced directly: `console.render_lines(renderable,
    options.update_width(0))` returns `[]` — the exact "image row renders
    as nothing, not even an error line" symptom (#4433's live-verify
    finding), not a sixel/pty capture artifact."""

    def __init__(self, inner: "Any", label: str) -> None:
        self._inner = inner
        self._label = label

    def __rich_console__(self, console: "Any", options: "Any"):
        from rich.segment import Segment
        from rich.text import Text

        try:
            for segment in self._inner.__rich_console__(console, options):
                # #3846 live-verify fix: a real `textual-image` 0.12.0/0.13.x
                # sixel bug (`textual_image/renderable/sixel.py`'s own
                # module-level `_NULL_CONTROL = [(ControlType.CURSOR_FORWARD,
                # 0)]`) emits `Segment.control` as a plain LIST, not a tuple.
                # `Segment` is a `NamedTuple` — a list-valued field makes the
                # whole tuple unhashable, and Rich's own
                # `Segment._split_cells` is `@lru_cache`-wrapped (keyed on
                # the segment itself), so the CONSTRUCTION never raises but
                # the FIRST time flowview needs to crop this row mid-segment
                # (`textual_flowview`'s `_decorate_line`, stamping gutter
                # offsets — every row, not image-specific) it does, with
                # `TypeError: unhashable type: 'list'` — reproduced directly
                # end-to-end through a real turn (real LLM tool call -> real
                # router loop -> real op dispatch -> real TUI paint) once
                # the two OTHER #3846 live-verify bugs (the SSRF-pin
                # str/bytes SNI crash, `_ssrf_pin.py`; this class's own
                # missing `__rich_measure__`, both fixed in the same PR)
                # were fixed and let a real sixel Segment reach this far.
                # Normalizing to a tuple here is a one-line, contained fix
                # for a genuine third-party bug, matching this class's own
                # established precedent (module docstring) of defending
                # against real `textual-image` print-time failures rather
                # than reporting upstream and blocking on it.
                #
                # Residual: if `textual-image` upstream fixes `_NULL_CONTROL`
                # to a tuple, this normalization becomes a no-op (a tuple
                # `isinstance(..., list)` check is already False) and can be
                # removed then — same shape as #4458's own upstream-fixed-it
                # residual note.
                if isinstance(segment, Segment) and isinstance(segment.control, list):
                    segment = Segment(segment.text, segment.style, tuple(segment.control))
                yield segment
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


def _render_image(node: dict, image_cache: "dict[str, Any] | None") -> "Any":
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

    #3846 ③: on a successful resolution, renders REAL pixels via
    `textual_image.renderable.Image` (owner-approved regular dep, #3970) —
    Kitty/WezTerm true pixels or Sixel when the terminal supports either,
    half-block/unicode approximation otherwise (that fallback selection is
    `textual_image`'s own, made once per process — see
    `interfaces/inline/textual_chat/app.py`'s `run_textual_chat` for WHY the
    triggering import must happen there and not lazily here). Falls back to
    a status-line `Text` if decoding fails (a genuinely non-image or
    corrupt body) or if `pillow`/`textual-image` are unavailable for any
    reason (defensive — they are regular deps, so this should not happen in
    a normal install)."""
    from rich.text import Text

    alt = node.get("alt") or ""
    src = node.get("src") or ""
    label = alt or src
    if image_cache is None or not isinstance(src, str) or src not in image_cache:
        return Text(f"[image: {label}]", style="dim")
    res = image_cache[src]
    if not res.ok:
        return Text(f"[image failed: {label} — {res.error}]", style="dim")
    try:
        import io

        from PIL import Image as PILImage
        from textual_image.renderable import Image as TextualImage

        pil_image = PILImage.open(io.BytesIO(res.body))
        # `PILImage.open()` alone only parses the header (lazy decode) — a
        # truncated/corrupt body can open cleanly and only fail once pixel
        # data is actually read. No separate `.load()` call is needed here
        # to force that: `TextualImage`'s own constructor reads the pixel
        # data eagerly (`PixelData.__init__`), so a bad body already raises
        # HERE, inside this `try`, not later at paint time (verified: a
        # truncated PNG that `open()` accepts raises `OSError` from
        # `TextualImage(...)` itself).
        return _SafeImageRenderable(TextualImage(pil_image, width="auto"), label)
    except Exception as exc:
        # Anything from a corrupt/unsupported body to a missing optional dep
        # (defensive only — pillow/textual-image are regular deps) degrades
        # to the pre-③ status line rather than breaking the render loop.
        return Text(
            f"[image loaded but could not render: {label} — {exc}]", style="dim",
        )


def render_presentation_nodes(
    nodes: list[dict], *, image_cache: "dict[str, Any] | None" = None
) -> "Any":
    """Convert a `ResolvedPresentation.nodes` render model into ONE Rich renderable
    (a `Group` of per-node renderables) — the one-shot inline block `present` prints
    to the conversation scrollback. See module docstring for the markup-inert
    invariant every branch here must preserve.

    `image_cache` (#3846 ②, default None) is forwarded to the `image`
    component branch — see :func:`_render_image`."""
    from rich.console import Group

    return Group(*[_render_node(node, image_cache) for node in nodes])


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
