#!/usr/bin/env python3
"""tui_screenshot.py — capture a PNG screenshot of a REAL ``TextualChatApp``
render, for any scenario a caller supplies (#4535's Part 1 deliverable).

## Why this exists (#6228 / #4535)

PR #6228 re-shot the 3 README TUI screenshots from a real ``TextualChatApp``
render, but its own body says explicitly it did **not** commit "the
generation/rasterize scripts (build byproducts)" — the pipeline existed only
as prose in a merged PR. This script commits that pipeline as a real,
invokable tool, generalized to take ANY scenario as input (a dotted Python
module path), not just the 3 README scenes.

## The pipeline (verified working the hard way, #6228's own commit history)

    real TextualChatApp, driven through run_test()
        -> App.export_screenshot()               (a REAL rendered SVG)
        -> headless Chrome navigated DIRECTLY to that .svg file (file://…)
        -> --screenshot=<out>.png

## Two dead ends, so nobody re-pays for them

- **cairosvg**: rasterizing the exported SVG with ``cairosvg`` does NOT do
  per-glyph font fallback across a comma-separated font-family list (measured
  directly, #6228) — no single locally-installed substitute face covers
  monospace Latin + CJK corner brackets + box-drawing all at once, so any one
  substitute font fails at least one glyph class (tofu boxes, misaligned
  "column-aligned" tables). Do not reach for cairosvg here.
- **``<img src="…svg">``, i.e. embedding the exported SVG inside another HTML
  page and screenshotting THAT page**: a browser sandboxes an ``<img>``-embedded
  SVG's own resource fetches (the ``@font-face`` Textual's exporter declares
  for Fira Code, pulled from cdnjs) — the font never loads, glyphs fall back to
  tofu, and the monospace grid the TUI depends on collapses. The fix is to
  navigate the browser DIRECTLY to the ``.svg`` file as the top-level
  document (``file://…``), never to embed it. This is the one iteration
  #6228 burned a full pass on; do not repeat it.

## Scenario contract

A scenario is a dotted Python module path (``package.module``, resolved with
this repo's ``src/`` on ``sys.path`` — see ``--scenario``). It must define:

    async def direct(app: TextualChatApp, pilot: Pilot) -> None:
        '''Drive `app` (already mounted under `run_test()`) to the exact
        frame this scenario wants captured — push frames via a transport,
        `await pilot.pause()`, wait for any async settle (image decode,
        streaming) — then return. Whatever is on screen when this returns
        is what gets screenshotted.'''

Optionally:

    def make_app() -> TextualChatApp:
        '''Construct the app (transport, read_model, any monkeypatches this
        scenario needs). Defaults to `TextualChatApp(transport=QueueTransport())`
        if absent.'''

    SIZE: tuple[int, int] = (100, 30)   # terminal cell grid; default (100, 30)

See ``scripts/tui_screenshots/scenario_4535_image_present.py`` for a worked
example (an image `present()`ed, decoded, and settled before capture).

## Failure policy

If headless Chrome is not found, this script exits loudly naming exactly
what is missing (never silently degrades to a cairosvg fallback or any
other lower-fidelity rasterizer) — see ``_find_chrome``.

CI: manual -- run by hand to regenerate a TUI screenshot for a docs/issue
scene; not wired into any workflow.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

# Ensure this checkout's own src/ (not whatever tree an ambient editable
# install happens to point at) is what `import reyn` below resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

# #3024: verify the in-process `reyn` this bare-python script is about
# to import (module-level or lazily, anywhere below) resolves THIS
# checkout, not whichever tree the ambient venv's editable install
# happens to point at. Exits loudly (never a silent wrong-tree run) on
# a mismatch — see verify_env_identity.py's own guard_bare_script_or_exit
# docstring.
from verify_env_identity import guard_bare_script_or_exit  # noqa: E402

guard_bare_script_or_exit()

if TYPE_CHECKING:
    from reyn.interfaces.inline.textual_chat import TextualChatApp

_DEFAULT_SIZE = (100, 30)

# Common headless-Chrome/Chromium binary locations this script checks, in
# order, before giving up. `--chrome` overrides all of these.
_CHROME_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _find_chrome(explicit: "str | None") -> str:
    """Locate a headless-Chrome-capable binary, or exit LOUDLY.

    Never falls back to a degraded rasterizer (cairosvg or otherwise, see
    the module docstring's dead-end note) — a missing browser is reported
    as exactly that, not silently worked around."""
    if explicit:
        if Path(explicit).exists() or shutil.which(explicit):
            return explicit
        sys.exit(
            f"tui_screenshot: --chrome {explicit!r} does not exist and is not "
            "on PATH. Refusing to fall back to a degraded rasterizer (e.g. "
            "cairosvg — see this script's own module docstring for why that "
            "silently produces tofu glyphs and misaligned tables)."
        )
    for candidate in _CHROME_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
        elif shutil.which(candidate):
            return candidate
    sys.exit(
        "tui_screenshot: no headless-Chrome-capable binary found (checked "
        f"{_CHROME_CANDIDATES!r} and PATH). Install Chrome/Chromium, or pass "
        "--chrome <path-to-binary>. Refusing to fall back to a degraded "
        "rasterizer (cairosvg does not do per-glyph font fallback — see this "
        "script's module docstring)."
    )


def _load_scenario(dotted: str):
    """Import `dotted` and return (make_app, direct, size), validated."""
    try:
        mod = importlib.import_module(dotted)
    except ImportError as exc:
        sys.exit(f"tui_screenshot: could not import scenario {dotted!r}: {exc}")
    direct = getattr(mod, "direct", None)
    if direct is None or not asyncio.iscoroutinefunction(direct):
        sys.exit(
            f"tui_screenshot: scenario {dotted!r} has no `async def direct(app, "
            "pilot)` — see this script's own module docstring for the contract."
        )
    make_app = getattr(mod, "make_app", None)
    if make_app is None:
        def make_app() -> "TextualChatApp":  # noqa: ANN202
            from reyn.interfaces.inline.textual_chat import TextualChatApp
            from tests._support.textual_chat_test_helpers import QueueTransport

            return TextualChatApp(transport=QueueTransport())
    size = getattr(mod, "SIZE", _DEFAULT_SIZE)
    return make_app, direct, size


async def _capture_svg(make_app, direct, size: "tuple[int, int]") -> str:
    """Render the scenario under a real `run_test()` and return the exported SVG."""
    app = make_app()
    async with app.run_test(size=size) as pilot:
        await direct(app, pilot)
        return app.export_screenshot()


_VIEWBOX_RE = re.compile(r'viewBox="0 0 (\d+) (\d+(?:\.\d+)?)"')


def _svg_pixel_size(svg: str) -> "tuple[int, int]":
    """Read the rendered pixel size straight off the SVG's own `viewBox` —
    the authoritative size Rich's exporter stamped, not a guess."""
    m = _VIEWBOX_RE.search(svg)
    if not m:
        sys.exit(
            "tui_screenshot: exported SVG has no `viewBox=\"0 0 W H\"` attribute "
            "— cannot determine the window size to rasterize at. This means "
            "`App.export_screenshot()`'s own output shape changed; this script "
            "needs updating, not a hand-picked fallback size."
        )
    return int(float(m.group(1))), int(float(m.group(2)))


def _wake_display_if_darwin() -> "subprocess.Popen | None":
    """macOS-only gotcha, found the hard way while building this script:
    on a Mac whose display is ASLEEP (lid closed, or just idle — the
    ordinary state of an unattended dev machine), `CGGetActiveDisplayList`
    returns ZERO active displays, headless Chrome's `CVDisplayLinkCreate
    WithCGDisplay` fails with `CVReturn -6670` (`kCVReturnInvalidDisplay`),
    and the browser process either hangs indefinitely (`--headless=new`,
    multi-process) or segfaults (`--single-process`) instead of ever
    reaching `window.onload` — confirmed directly: `--dump-dom` on a
    trivial `data:` URL hung with a genuinely sleeping display and
    produced correct output the moment the display was nudged awake.

    `caffeinate -u` simulates user activity, which wakes the display (does
    NOT unlock the screen or otherwise disturb a logged-in session — it is
    the same wake a mouse jiggle would cause) for its own duration, which
    is enough to carry a rasterize call through. A no-op, silently, on any
    non-Darwin platform (no active-display precondition exists there).

    Returns the `caffeinate` process (fire-and-forget — the caller does
    not need to wait on or kill it; it exits on its own `-t` timeout) or
    `None` off Darwin."""
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    return subprocess.Popen(["caffeinate", "-u", "-t", "60"])


#: How long to poll for `out_path` to appear before giving up (seconds).
_RASTERIZE_POLL_CEILING = 60.0
_RASTERIZE_POLL_INTERVAL = 0.5


def _rasterize(chrome: str, svg_path: Path, out_path: Path, size: "tuple[int, int]") -> None:
    """Navigate headless Chrome DIRECTLY to `svg_path` (file://…, never
    `<img src>` — see module docstring) and capture a PNG at `out_path`.

    Polls for `out_path` rather than waiting for the process to exit on its
    own: measured directly on this pipeline's own dev machine, headless
    Chrome (`--headless`, old mode — see `_wake_display_if_darwin`'s own
    docstring for why old mode, not `--headless=new`) reliably WRITES the
    screenshot file well before it exits — the process itself lingers
    afterward (a GPU/network-service teardown quirk, not a rasterize
    failure) well past any patience a caller should have for this one
    step. Waiting on `wait()`/`communicate()` instead would report a false
    failure (`TimeoutExpired`) over a screenshot that already succeeded."""
    width, height = size
    _wake_display_if_darwin()  # see its own docstring — a macOS-only precondition
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        f"--window-size={width},{height}",
        f"--screenshot={out_path}",
        f"file://{svg_path}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + _RASTERIZE_POLL_CEILING
    written = False
    while time.monotonic() < deadline:
        if out_path.exists() and out_path.stat().st_size > 0:
            written = True
            break
        if proc.poll() is not None:
            break  # exited (crashed or otherwise) before ever writing the file
        time.sleep(_RASTERIZE_POLL_INTERVAL)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not written:
        stdout, stderr = proc.communicate() if proc.stdout else ("", "")
        sys.exit(
            f"tui_screenshot: headless Chrome never wrote {out_path} within "
            f"{_RASTERIZE_POLL_CEILING}s (exit {proc.returncode}).\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a PNG screenshot of a real TextualChatApp render, driven "
            "by a caller-supplied scenario module. See this file's own module "
            "docstring for the full pipeline, its two dead ends, and the "
            "scenario contract."
        )
    )
    parser.add_argument(
        "--scenario",
        required=True,
        help=(
            "dotted import path to a scenario module (e.g. "
            "scripts.tui_screenshots.scenario_4535_image_present) defining "
            "`async def direct(app, pilot)` and optionally `make_app()`/`SIZE`"
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="output PNG path")
    parser.add_argument(
        "--chrome", default=None, help="path to a headless-Chrome-capable binary"
    )
    parser.add_argument(
        "--keep-svg",
        type=Path,
        default=None,
        help="also save the intermediate exported SVG to this path (debugging only — never committed)",
    )
    args = parser.parse_args()

    chrome = _find_chrome(args.chrome)
    make_app, direct, size = _load_scenario(args.scenario)

    svg = asyncio.run(_capture_svg(make_app, direct, size))
    pixel_size = _svg_pixel_size(svg)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "screenshot.svg"
        svg_path.write_text(svg, encoding="utf-8")
        _rasterize(chrome, svg_path, args.out, pixel_size)
        if args.keep_svg:
            args.keep_svg.parent.mkdir(parents=True, exist_ok=True)
            args.keep_svg.write_text(svg, encoding="utf-8")

    print(f"tui_screenshot: wrote {args.out} ({pixel_size[0]}x{pixel_size[1]}px)")


if __name__ == "__main__":
    main()
