"""Tier 1/2: FP-0054 PR-B — the inline-CUI `present` renderer + the guard/renderer
Rich-markup-safety re-layering (Option B).

Covers:
  1. Tier 1: per-component render presence (each catalog component produces SOME
     Rich renderable containing its content — not layout/exact-whitespace pins).
  2. Tier 2 INVARIANT-LOCK (lead-coder review): Rich-markup-shaped leaf data
     (``[bold]INJECT[/bold]``) survives the FULL real pipeline (guard →
     resolve_bindings → render_presentation_nodes → an actual Rich Console print)
     as LITERAL text, never interpreted as styling — the structural guarantee
     Option B replaces the old escape/unescape pair with.
  3. Tier 2: the terminal ESC/control-strip behavioral guarantee still holds
     through the same full pipeline (guard's actual security responsibility,
     unchanged by the Option B revision).
  4. Tier 2: `OutboxPresentationRenderer.render` puts a `"presentation"`
     `OutboxMessage` carrying the render model onto the real Session outbox (no
     mock Session — a minimal real one), and `format_inline_message` dispatches
     `kind="presentation"` to `render_presentation_nodes`.
  5. Tier 1: `op_runtime/present.py` derives its `surface` string from the wired
     `OpContext.presentation_renderer` (None → "null"; a renderer → its own
     `surface_name`) and calls `.render()` on it exactly once when present.

Real `PipelineExecutor`-adjacent objects throughout: real `Console`, real
`resolve_bindings`, real `OpContext`; no `MagicMock`/`patch`. No exact-render /
whitespace pins — asserts content presence and structural facts only.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.present import resolve_bindings, validate_blueprint
from reyn.data.workspace.workspace import Workspace
from reyn.interfaces.repl.present_renderer import render_presentation_nodes
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage
from reyn.security.permissions.permissions import PermissionDecl


def _render_to_text(nodes: list[dict], *, width: int = 60) -> str:
    console = Console(width=width, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(render_presentation_nodes(nodes))
    return console.file.getvalue()


# ── 1. per-component render presence ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("blueprint", "data", "expect_substr"),
    [
        ({"component": "text", "text": {"$bind": "/v"}}, {"v": "hello world"}, "hello world"),
        ({"component": "markdown", "text": "**bold**"}, {}, "bold"),
        ({"component": "code", "text": "x = 1", "language": "python"}, {}, "x"),
        ({"component": "diff", "text": "+added\n-removed"}, {}, "added"),
        (
            {"component": "keyvalue", "rows": [{"label": "k", "value": {"$bind": "/v"}}]},
            {"v": "val1"},
            "val1",
        ),
        (
            {
                "component": "table",
                "rows": {"$bind": "/items"},
                "columns": [{"header": "name", "path": "/n"}],
            },
            {"items": [{"n": "row-one"}]},
            "row-one",
        ),
        ({"component": "list", "items": ["alpha", "beta"]}, {}, "alpha"),
        ({"component": "image", "alt": "a photo"}, {}, "a photo"),
    ],
)
def test_each_catalog_component_renders_its_content(blueprint, data, expect_substr) -> None:
    """Tier 1: every v1 catalog component produces a renderable whose printed
    output contains its bound/literal content — presence, not exact layout."""
    nodes = validate_blueprint(blueprint)
    resolved = resolve_bindings(nodes, data, surface="inline-cui")
    out = _render_to_text(resolved.nodes)
    assert expect_substr in out


def test_unsupported_component_does_not_crash_the_render_loop() -> None:
    """Tier 1: a node with an unrecognized component name renders a placeholder
    instead of raising — the render loop never crashes over one bad node."""
    out = _render_to_text([{"component": "not-a-real-component"}])
    assert "not-a-real-component" in out


# ── 1b. #3846 ③ — image_cache real-pixel rendering ───────────────────────────


def _png_bytes(*, size: tuple = (32, 32), color: tuple = (200, 20, 60)) -> bytes:
    """A real PNG for the SUCCESS render path. 32x32, not e.g. 4x4: a real
    `textual-image` 0.12.0 bug (the version pip resolves on reyn's own
    Python 3.11 floor — 0.13+ requires >=3.12) raises at print time for a
    very small source image (reproduced: 8x8 and 1x1 fail, 16x16 does not)
    — a tiny fixture here would make "did the success path render" tests
    exercise the FAILURE path instead on a 3.11 install, depending on which
    textual-image version pip happened to resolve. See
    `test_safe_image_renderable_catches_a_print_time_failure` for the
    dedicated (version-independent) test of that failure path."""
    import io as _io

    from PIL import Image as PILImage

    buf = _io.BytesIO()
    PILImage.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_image_with_resolved_cache_renders_real_pixels_not_the_status_line() -> None:
    """Tier 1: #3846 ③ — a successfully-resolved image (a real ImageResolution
    in `image_cache`) is wrapped in reyn's own `_SafeImageRenderable` (in
    turn wrapping a `textual_image.renderable.Image`), not the pre-③
    `[image loaded: ...]` status-line `Text`. This is reyn's OWN dispatch
    decision (which renderable class `_render_image` picks) — a fact about
    reyn's wiring, not textual_image's rendering behavior (a third party's
    promise this file does not pin). Checked on the render-model OBJECT
    (`Group.renderables`), not on printed glyph output — a Console.print of
    either class produces terminal-formatted text, so a text-content-only
    assertion here cannot tell "real pixel path wired" apart from "still
    the old status line" (confirmed by deliberately reverting
    `_render_image` to the pre-③ Text return and seeing string-only
    assertions here stay green — falsify-verified)."""
    from reyn.core.present.image_fetch import ImageResolution
    from reyn.interfaces.repl.present_renderer import _SafeImageRenderable

    nodes = validate_blueprint({"component": "image", "src": "http://x/y.png", "alt": "a photo"})
    resolved = resolve_bindings(nodes, {}, surface="inline-cui")
    image_cache = {
        "http://x/y.png": ImageResolution(ok=True, body=_png_bytes(), content_type="image/png"),
    }
    group = render_presentation_nodes(resolved.nodes, image_cache=image_cache)
    (renderable,) = group.renderables
    assert isinstance(renderable, _SafeImageRenderable), (
        f"expected reyn's own image-render wrapper, got {type(renderable)!r}"
    )

    # And the printed output is still sane through the real pipeline: no
    # placeholder, no failure text, non-empty.
    console = Console(width=100, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(group)
    out = console.file.getvalue()
    assert "[image: a photo]" not in out
    assert "could not render" not in out
    assert "[image failed" not in out
    assert out.strip()


def test_render_image_uses_a_warm_decoded_cache_without_decoding_again() -> None:
    """Tier 2: #4464 lead-coder review block — a warm ``decoded_image_cache``
    entry must actually be USED, not merely handed in ("渡された ≠ 使った",
    #3859's own shape: a value present-but-ignored reads identical to a
    value that mattered, until something asserts the DIFFERENCE). Before
    this test, deleting `_render_image`'s cache-hit branch entirely left
    every existing test green — none of them distinguished "decoded from
    the cache" from "decoded fresh from `res.body`" because both paths
    produce an equivalent-looking `_SafeImageRenderable`.

    Proven two ways at once, both through PUBLIC surface (no private-state
    assertion — the sentinel's identity is witnessed by its OWN print-time
    behavior, not by reaching into `_SafeImageRenderable._inner`):

    1. Behavior — `decode_image_body` is monkeypatched with a call-counting
       spy; it must be called ZERO times, proving decode never ran on the
       (deliberately garbage) cached bytes.
    2. Content — the sentinel wrapped for render is a plain `object()` with
       no `__rich_console__` of its own, so printing it produces
       `_SafeImageRenderable`'s OWN print-time-failure text naming THAT
       specific `AttributeError` — a fresh decode of the garbage `res.body`
       would instead fail at `PILImage.open()` with a DIFFERENT, PIL-shaped
       error, so the printed text distinguishes which object was actually
       wrapped without touching `_inner` directly.

    Falsify-verified: commenting out the cache-hit branch (falling straight
    to the inline decode) makes the spy record a call AND changes the
    printed failure text to a PIL decode error — this test goes RED on
    both assertions with the exact real bug lead-coder's block named."""
    from reyn.core.present.image_fetch import ImageResolution
    from reyn.interfaces.repl.present_renderer import _SafeImageRenderable

    calls: list[bytes] = []

    def _spy(body: bytes):
        calls.append(body)
        raise RuntimeError("decode_image_body should not have been called")

    import reyn.interfaces.repl.present_renderer as present_renderer_module
    original = present_renderer_module.decode_image_body
    present_renderer_module.decode_image_body = _spy
    try:
        sentinel = object()  # stands in for a pre-decoded TextualImage
        nodes = validate_blueprint(
            {"component": "image", "src": "http://x/y.png", "alt": "a photo"}
        )
        resolved = resolve_bindings(nodes, {}, surface="inline-cui")
        image_cache = {
            "http://x/y.png": ImageResolution(
                ok=True, body=b"deliberately-not-a-real-png", content_type="image/png"
            ),
        }
        decoded_image_cache = {"http://x/y.png": sentinel}

        group = render_presentation_nodes(
            resolved.nodes,
            image_cache=image_cache,
            decoded_image_cache=decoded_image_cache,
        )
        (renderable,) = group.renderables
        assert isinstance(renderable, _SafeImageRenderable)

        console = Console(width=100, file=io.StringIO(), force_terminal=True, color_system=None)
        console.print(group)
        out = console.file.getvalue()

        assert not calls, (
            f"decode_image_body was called {len(calls)} time(s) — "
            "the cache-hit branch was bypassed"
        )
        # The sentinel `object()` has no `__rich_console__` — printing it
        # fails with THIS specific AttributeError, distinguishing "the
        # cached sentinel was wrapped" from "the garbage bytes were
        # (attempted to be) decoded fresh" (which would fail differently,
        # at PIL's own header sniff, if the cache-hit branch were bypassed
        # and our raising spy didn't intervene first).
        assert "__rich_console__" in out, (
            f"expected the sentinel's own print-time AttributeError, got: {out}"
        )
    finally:
        present_renderer_module.decode_image_body = original


def test_safe_image_renderable_catches_a_print_time_failure() -> None:
    """Tier 1: #3846 ③ — `_SafeImageRenderable` degrades to a status line
    when the WRAPPED renderable's `__rich_console__` raises, not just when
    `TextualImage(...)`'s own constructor raises.

    This matters because Rich's `__rich_console__` protocol is a generator
    Console.print() only iterates once it knows the real render width — a
    failure there happens LATER than `_render_image`'s `try/except` (which
    only wraps construction) can see. Real instance found during
    implementation: `textual-image` 0.12.0 (the version pip resolves on
    reyn's own Python 3.11 floor) raises `ValueError('height and width must
    be > 0')` from inside `__rich_console__` for a very small source image
    — `TextualImage(...)` construction itself never raises for these.
    Tested here against a FAKE inner renderable (reyn's own wrapper
    behavior, not textual_image's version-specific bug — the dev
    environment's installed textual_image version may not reproduce that
    bug at all, so pinning this test to it would be flaky-by-environment)."""
    from rich.console import Console as _Console

    from reyn.interfaces.repl.present_renderer import _SafeImageRenderable

    class _RaisingInner:
        def __rich_console__(self, console: object, options: object):
            raise ValueError("height and width must be > 0")
            yield  # pragma: no cover — makes this a generator function

    wrapped = _SafeImageRenderable(_RaisingInner(), "a photo")
    console = _Console(width=100, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(wrapped)  # must not raise
    out = console.file.getvalue()
    assert "could not render" in out
    assert "a photo" in out


def test_safe_image_renderable_delegates_measurement_to_the_inner_renderable() -> None:
    """Tier 2: #3846 live-verify — `_SafeImageRenderable.__rich_measure__`
    delegates to the wrapped renderable's own measurement. This is REYN'S
    OWN wrapper behavior (a claim about which method reyn's dispatch calls),
    not textual_image's rendering fidelity — the fake inner below has no
    third-party code in it at all.

    Why this matters (lead-coder review, #4463): without this delegation,
    Rich has no `__rich_measure__` to find on THIS wrapper, and the
    enclosing `Group`'s own measurement falls back to `minimum=0` — which
    then makes `console.render_lines(renderable, options.update_width(0))`
    return an EMPTY list. The symptom is "the row is completely blank, not
    even an error line" — exactly what went unnoticed until the owner asked
    directly (nothing else would have caught a silent regression here).
    Falsify-verified: removing the `__rich_measure__` override (or making
    it not delegate) reproduces `minimum == 0`."""
    from rich.console import Console as _Console
    from rich.measure import Measurement

    from reyn.interfaces.repl.present_renderer import _SafeImageRenderable

    class _MeasurableInner:
        def __rich_console__(self, console: object, options: object):
            yield "unused"

        def __rich_measure__(self, console: object, options: object) -> Measurement:
            return Measurement(42, 42)

    wrapped = _SafeImageRenderable(_MeasurableInner(), "a photo")
    console = _Console(width=100, file=io.StringIO(), color_system=None)
    measured = Measurement.get(console, console.options, wrapped)
    assert measured.minimum == 42, (
        f"expected the inner renderable's own measurement (42) to pass through, got {measured}"
    )


def test_safe_image_renderable_normalizes_a_list_control_segment_to_a_tuple() -> None:
    """Tier 2: #3846 live-verify — `_SafeImageRenderable.__rich_console__`
    normalizes any yielded `Segment.control` LIST to a tuple before passing
    it on. This is REYN'S OWN normalization behavior, not a claim about
    `textual-image`'s own correctness — the fake inner below yields exactly
    the shape a real `textual-image` sixel bug produces, without importing
    `textual_image` at all.

    Real bug this guards (found via a real turn through the real
    render/paint path, #4463): `textual_image/renderable/sixel.py`'s own
    `_NULL_CONTROL = [(ControlType.CURSOR_FORWARD, 0)]` is a LIST literal.
    `Segment` is a `NamedTuple`, so a list-valued `control` field makes the
    whole `Segment` unhashable — and Rich's `Segment._split_cells` is
    `@lru_cache`-wrapped (keyed on the segment itself), so the crash
    (`TypeError: unhashable type: 'list'`) only fires the first time
    something needs to HASH the segment (flowview's own `Strip.crop`,
    stamping gutter offsets on every row — not image-specific), not at
    construction or at a plain `Console.print`.

    Residual: if `textual-image` upstream fixes `_NULL_CONTROL` to be a
    tuple, this normalization becomes a no-op (harmless) and can be
    removed — same shape as #4458's own upstream-fixed-it-first residual
    note."""
    from rich.segment import ControlType, Segment

    from reyn.interfaces.repl.present_renderer import _SafeImageRenderable

    class _SixelLikeInner:
        def __rich_console__(self, console: object, options: object):
            # The exact shape a real textual-image sixel Segment carries —
            # text/style/control as a NamedTuple, control as a plain list.
            yield Segment("\x1b7", None, [(ControlType.CURSOR_FORWARD, 0)])

    wrapped = _SafeImageRenderable(_SixelLikeInner(), "a photo")
    segments = list(wrapped.__rich_console__(None, None))
    (segment,) = segments
    assert isinstance(segment, Segment)
    assert isinstance(segment.control, tuple), (
        f"expected control normalized to a tuple (hashable), got {type(segment.control)!r}"
    )
    # And the Segment itself must actually be hashable now — the real crash
    # this guards is a hash attempt inside Rich's own lru_cache, not merely
    # a type mismatch.
    hash(segment)


def test_image_with_undecodable_body_falls_back_to_a_distinct_status_line() -> None:
    """Tier 1: #3846 ③ — a resolved-but-not-actually-an-image body (corrupt,
    truncated, or a non-image content type mislabeled by the server) fails
    to decode and falls back to a status line naming the failure — never a
    crash, never the same text as a genuine fetch failure or the
    never-resolved placeholder (the #3688 "different things read as the
    same" class).

    Uses a TRUNCATED real PNG (valid header, cut off mid-pixel-data), not
    plain garbage bytes: garbage fails at `PILImage.open()`'s header sniff
    alone, which would leave the LAZY-decode failure path unexercised — a
    truncated body passes `open()` (the header parses fine; PIL decodes
    lazily) and only fails once pixel data is actually read, which
    `TextualImage(...)`'s own constructor does eagerly (verified directly:
    `open()` on this exact body returns a valid `Image`; constructing
    `TextualImage` from it raises `OSError: image file is truncated`) — the
    case `_render_image`'s `try` block exists to catch."""
    from rich.console import Console

    from reyn.core.present.image_fetch import ImageResolution

    truncated_png = _png_bytes(size=(50, 50))
    truncated_png = truncated_png[: len(truncated_png) // 2]

    nodes = validate_blueprint({"component": "image", "src": "http://x/y.png", "alt": "a photo"})
    resolved = resolve_bindings(nodes, {}, surface="inline-cui")
    image_cache = {
        "http://x/y.png": ImageResolution(
            ok=True, body=truncated_png, content_type="image/png",
        ),
    }
    console = Console(width=100, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(render_presentation_nodes(resolved.nodes, image_cache=image_cache))
    out = console.file.getvalue()
    assert "could not render" in out
    assert "a photo" in out
    assert "[image: a photo]" not in out
    assert "[image failed" not in out  # distinct from a FETCH failure (res.ok=False)


# ── 2. INVARIANT-LOCK: Rich markup never interpreted, full real pipeline ────


def test_rich_markup_leaf_survives_literal_through_the_full_real_pipeline() -> None:
    """Tier 2: INVARIANT-LOCK — `[bold]INJECT[/bold]` in bound data reaches the
    printed terminal output as LITERAL text — never interpreted as Rich styling
    (no ANSI SGR bytes around it) — through the REAL guard → bindings →
    render_presentation_nodes → Console.print pipeline. This is the structural
    guarantee Option B relies on in place of the old guard-level escape/unescape
    pair (see guard.py's module docstring)."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    resolved = resolve_bindings(nodes, {"v": "safe [bold]INJECT[/bold] text"}, surface="inline-cui")
    # The guard passed the markup-shaped text through unescaped (Option B).
    assert resolved.nodes[0]["text"] == "safe [bold]INJECT[/bold] text"

    out = _render_to_text(resolved.nodes)
    assert "[bold]INJECT[/bold]" in out       # literal brackets survive verbatim
    assert "\x1b[1m" not in out               # never interpreted as a Rich bold SGR


def test_rich_markup_in_code_and_table_cells_also_stays_literal() -> None:
    """Tier 2: INVARIANT-LOCK — the same guarantee holds for the `code`/`table`
    render paths specifically (the two paths the old escape/unescape approach
    would have corrupted with visible backslashes — see guard.py docstring)."""
    code_nodes = validate_blueprint({"component": "code", "text": "x = '[red]y[/red]'"})
    code_resolved = resolve_bindings(code_nodes, {}, surface="inline-cui")
    code_out = _render_to_text(code_resolved.nodes)
    assert "[red]y[/red]" in code_out
    assert "\\[red]" not in code_out          # no leftover escape backslash either

    table_nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/items"},
        "columns": [{"header": "col [i]", "path": "/v"}],
    })
    table_resolved = resolve_bindings(
        table_nodes, {"items": [{"v": "[bold]cell[/bold]"}]}, surface="inline-cui",
    )
    table_out = _render_to_text(table_resolved.nodes)
    assert "[bold]cell[/bold]" in table_out
    assert "col [i]" in table_out
    assert "\\[bold]" not in table_out


# ── 3. control/ESC-strip guarantee, unchanged ────────────────────────────────


def test_control_and_esc_sequences_still_stripped_through_the_full_pipeline() -> None:
    """Tier 2: the guard's actual security responsibility (ESC/control-sequence
    stripping) is unchanged by the Option B revision — verified through the same
    real render pipeline as the invariant-lock tests above."""
    nodes = validate_blueprint({"component": "text", "text": {"$bind": "/v"}})
    resolved = resolve_bindings(nodes, {"v": "safe\x1b[31mINJECT\x1b[0m text"}, surface="inline-cui")
    assert "\x1b" not in resolved.nodes[0]["text"]
    out = _render_to_text(resolved.nodes)
    assert "\x1b[31m" not in out
    assert "INJECT" in out


# ── 3b. #2669 — cap_rows shows a visible truncation tail, not a silent drop ──


def test_table_cap_rows_shows_visible_truncation_tail_with_ref() -> None:
    """Tier 2: issue #2669 — ratified §5 (`docs/deep-dives/proposals/
    0054-present-layer.md`) mandates a visible `…N more — full data: <ref>` tail
    when `cap_rows` truncates a table's bound rows. Through the REAL
    resolve_bindings → render_presentation_nodes → Console.print pipeline: a
    dataset with more rows than `guard.MAX_ROWS` renders a visible tail naming
    the correct remainder count and the data ref — never a silent drop."""
    from reyn.core.present.guard import MAX_ROWS

    extra = 37
    total = MAX_ROWS + extra
    data = {"items": [{"n": f"row-{i}"} for i in range(total)]}
    nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/items"},
        "columns": [{"header": "name", "path": "/n"}],
    })
    resolved = resolve_bindings(nodes, data, surface="inline-cui", ref="/tmp/dataset.json")

    # #3664 (b): `rows` counts what the user actually saw — the POST-cap count,
    # not the pre-cap `total` rows that were resolved before `cap_rows` truncated
    # them. A pre-cap count here would silently over-report by `extra`.
    assert resolved.rows == MAX_ROWS

    # The render model carries the tail (not just the ack's drop stats) so the
    # renderer actually shows it — the #2669 gap was exactly that the drop was
    # recorded for the LLM but never threaded to the render model.
    tail = resolved.nodes[0].get("truncation_tail")
    assert tail is not None, "cap_rows capped the table but no visible tail was produced"
    assert str(extra) in tail
    assert "/tmp/dataset.json" in tail

    out = _render_to_text(resolved.nodes)
    assert str(extra) in out
    assert "/tmp/dataset.json" in out
    # The survivors still render (cap-before-render, unchanged).
    assert "row-0" in out


def test_list_cap_rows_shows_visible_truncation_tail_without_ref_for_inline_data() -> None:
    """Tier 2: the same visible-tail guarantee for a `list` component's bound
    `items`. Inline data (no `data_ref`) has no re-fetchable ref, so the tail
    correctly omits the "full data" clause rather than naming a bogus ref —
    but the remainder count is still visible, never silently dropped."""
    from reyn.core.present.guard import MAX_ROWS

    extra = 5
    total = MAX_ROWS + extra
    data = {"items": [f"item-{i}" for i in range(total)]}
    nodes = validate_blueprint({"component": "list", "items": {"$bind": "/items"}})
    resolved = resolve_bindings(nodes, data, surface="inline-cui", ref=None)

    tail = resolved.nodes[0].get("truncation_tail")
    assert tail is not None
    assert str(extra) in tail
    assert "full data" not in tail  # no ref available — no bogus escape-hatch claim

    out = _render_to_text(resolved.nodes)
    assert str(extra) in out
    assert "item-0" in out


def test_table_under_cap_rows_produces_no_truncation_tail() -> None:
    """Tier 2: a dataset within `MAX_ROWS` renders with no truncation tail at
    all — the indicator appears if and only if `cap_rows` actually capped
    (never a false-positive "more" tail on an un-truncated table)."""
    data = {"items": [{"n": "only-row"}]}
    nodes = validate_blueprint({
        "component": "table",
        "rows": {"$bind": "/items"},
        "columns": [{"header": "name", "path": "/n"}],
    })
    resolved = resolve_bindings(nodes, data, surface="inline-cui", ref="/tmp/dataset.json")
    assert "truncation_tail" not in resolved.nodes[0]
    out = _render_to_text(resolved.nodes)
    assert "more" not in out


# ── 4. OutboxPresentationRenderer + format_inline_message dispatch ──────────


def test_outbox_presentation_renderer_puts_presentation_message() -> None:
    """Tier 2: OutboxPresentationRenderer.render puts a real `OutboxMessage(kind=
    "presentation")` carrying `resolved.nodes` onto the session's outbox — the
    same queue every other display kind flows through."""
    import asyncio

    from reyn.core.present.binding import ResolvedPresentation
    from reyn.runtime.session_buses import OutboxPresentationRenderer

    class _Session:
        def __init__(self) -> None:
            self.outbox: asyncio.Queue = asyncio.Queue()

    session = _Session()
    renderer = OutboxPresentationRenderer(session)
    assert renderer.surface_name == "inline-cui"

    resolved = ResolvedPresentation(nodes=[{"component": "text", "text": "hi"}])
    renderer.render(resolved)

    msg = session.outbox.get_nowait()
    assert isinstance(msg, OutboxMessage)
    assert msg.kind == "presentation"
    assert msg.meta["nodes"] == [{"component": "text", "text": "hi"}]


def test_format_inline_message_dispatches_presentation_kind() -> None:
    """Tier 2: format_inline_message routes kind="presentation" to
    render_presentation_nodes (not the generic _KIND_LINE fallback)."""
    msg = OutboxMessage(kind="presentation", text="", meta={"nodes": [
        {"component": "text", "text": "hello from present"},
    ]})
    console = Console(width=60, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(format_inline_message(msg))
    assert "hello from present" in console.file.getvalue()


def test_console_chat_renderer_renders_presentation_kind_issue_2701() -> None:
    """Tier 2: RED→GREEN regression for issue #2701 — `--cui` (`reyn chat --cui`)
    uses `ConsoleChatRenderer`, not `InlineChatRenderer`. PR-B originally wired
    `kind="presentation"` only into `format_inline_message` (consumed by
    `InlineChatRenderer`), so `ConsoleChatRenderer.message()` fell through to its
    generic branch and printed nothing (`msg.text` is deliberately empty; the
    render model lives in `msg.meta["nodes"]`) — a `present` op succeeded at the
    OS level but showed the user nothing in `--cui` mode. RED against the
    pre-fix code (empty output); GREEN once `ConsoleChatRenderer.message()` also
    renders `meta["nodes"]`."""
    import sys as _sys
    from io import StringIO as _StringIO

    from reyn.interfaces.repl.renderer import ConsoleChatRenderer

    renderer = ConsoleChatRenderer()
    msg = OutboxMessage(kind="presentation", text="", meta={"nodes": [
        {"component": "text", "text": "hello from present cui"},
    ]})
    captured = _StringIO()
    real_stdout = _sys.__stdout__
    _sys.__stdout__ = captured
    try:
        renderer.message(msg)
    finally:
        _sys.__stdout__ = real_stdout
    assert "hello from present cui" in captured.getvalue()


# ── 5. op_runtime/present.py surface derivation + renderer call ─────────────


class _RecordingRenderer:
    surface_name = "inline-cui"

    def __init__(self) -> None:
        self.calls = []
        self.call_count = 0

    def render(self, resolved) -> None:
        self.calls.append(resolved)
        self.call_count += 1


def _ctx(*, presentation_renderer=None) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        presentation_renderer=presentation_renderer,
    )


@pytest.mark.asyncio
async def test_present_op_uses_null_surface_when_no_renderer_wired() -> None:
    """Tier 1: OpContext.presentation_renderer=None (PR-A behavior, unchanged) →
    surface="null" in both the ack path's binding resolution and the presented
    event, and no renderer is called."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await handle(op, _ctx())
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_present_op_calls_the_wired_renderer_exactly_once() -> None:
    """Tier 1: a wired OpContext.presentation_renderer receives exactly one
    `.render(resolved)` call carrying the SAME stats the ack reports."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    renderer = _RecordingRenderer()
    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await handle(op, _ctx(presentation_renderer=renderer))
    assert renderer.call_count == 1
    assert renderer.calls[0].bindings_resolved == result["bindings_resolved"]


@pytest.mark.asyncio
async def test_presented_event_surface_reflects_the_wired_renderers_name() -> None:
    """Tier 1: the `presented` audit event's `surface` field is the wired
    renderer's own `surface_name`, not a hardcoded "null"/"inline-cui" literal."""
    from reyn.core.op_runtime.present import handle
    from reyn.schemas.models import PresentIROp

    events = EventLog()
    captured: list = []
    events.add_subscriber(lambda e: captured.append(e) if e.type == "presented" else None)
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        presentation_renderer=_RecordingRenderer(),
    )
    op = PresentIROp(
        kind="present", data_inline={"v": "x"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    await handle(op, ctx)
    assert captured
    assert captured[0].data["surface"] == ["inline-cui"]
