"""DonutScreen — easter egg invoked via the hidden `/donut` slash command.

Andy Sloane's classic spinning ASCII torus (donut.c, 2006), ported to
Python + Textual. A parametric torus is rotated around two axes and
projected to 2D; surface luminance from the Lambertian dot product
between the surface normal and a fixed light source picks one of 12
shading characters.

Press any key to dismiss.
"""
from __future__ import annotations

import math

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

# Shading ramp: dimmest → brightest. Standard donut.c ramp.
_SHADES = ".,-~:;=!*#$@"

# Torus geometry.
_R1 = 1.0   # tube radius
_R2 = 2.0   # ring radius
_K2 = 5.0   # camera distance

# Sampling step. Smaller = denser surface but more CPU per frame.
_THETA_STEP = 0.10
_PHI_STEP = 0.03


class DonutScreen(ModalScreen):
    """Full-screen rotating ASCII donut. Any key dismisses."""

    DEFAULT_CSS = """
    DonutScreen {
        align: center middle;
        background: black;
    }
    DonutScreen #donut {
        width: 100%;
        height: 100%;
        background: black;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="donut")

    def on_mount(self) -> None:
        size = self.app.size
        # Cap render size: a fullscreen 200x60 frame costs ~5x more than 80x24
        # and Pure Python can't keep up. Centre a fixed window.
        self._w = min(80, max(20, size.width))
        self._h = min(28, max(10, size.height - 1))
        # K1 sized so the torus fills ~3/4 of the smaller screen dim.
        self._k1 = min(self._w, self._h * 2) * _K2 * 3 / (8 * (_R1 + _R2))
        self._a = 0.0
        self._b = 0.0
        # 12.5 fps target — donut is graceful, not frantic; this also keeps
        # us under the per-frame budget on slow terminals.
        self._timer = self.set_interval(0.08, self._tick)

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss()

    def on_click(self, event) -> None:
        event.stop()
        self.dismiss()

    def _tick(self) -> None:
        self._a += 0.07
        self._b += 0.03
        frame = self._compute_frame(self._a, self._b)
        try:
            self.query_one("#donut", Static).update(frame)
        except Exception:
            pass

    def _compute_frame(self, a: float, b: float) -> Text:
        w, h = self._w, self._h
        k1 = self._k1
        # Flat buffers: index = x + y * w
        buf = [" "] * (w * h)
        zbuf = [0.0] * (w * h)
        shade = [0] * (w * h)

        cos_a = math.cos(a); sin_a = math.sin(a)
        cos_b = math.cos(b); sin_b = math.sin(b)

        theta = 0.0
        while theta < math.tau:
            cos_t = math.cos(theta); sin_t = math.sin(theta)
            phi = 0.0
            while phi < math.tau:
                cos_p = math.cos(phi); sin_p = math.sin(phi)

                # Coordinates of point on the torus before rotation.
                circle_x = _R2 + _R1 * cos_t
                circle_y = _R1 * sin_t

                # 3D point after rotation (around X by A, around Z by B).
                x = (circle_x * (cos_b * cos_p + sin_a * sin_b * sin_p)
                     - circle_y * cos_a * sin_b)
                y = (circle_x * (sin_b * cos_p - sin_a * cos_b * sin_p)
                     + circle_y * cos_a * cos_b)
                z = _K2 + cos_a * circle_x * sin_p + circle_y * sin_a
                ooz = 1.0 / z  # one over z (depth → 1/depth for z-buffer)

                # Project to 2D screen coordinates.
                xp = int(w / 2 + k1 * ooz * x)
                yp = int(h / 2 - k1 * ooz * y / 2)  # /2: char aspect ratio

                # Lambertian luminance — surface normal · light(0, 1, -1).
                L = (cos_p * cos_t * sin_b
                     - cos_a * cos_t * sin_p
                     - sin_a * sin_t
                     + cos_b * (cos_a * sin_t - cos_t * sin_a * sin_p))

                if L > 0 and 0 <= xp < w and 0 <= yp < h:
                    idx = xp + w * yp
                    if ooz > zbuf[idx]:
                        zbuf[idx] = ooz
                        shade_idx = int(L * 8)
                        if shade_idx < 0:
                            shade_idx = 0
                        elif shade_idx > 11:
                            shade_idx = 11
                        buf[idx] = _SHADES[shade_idx]
                        shade[idx] = shade_idx

                phi += _PHI_STEP
            theta += _THETA_STEP

        # Render to Rich Text — group consecutive same-style chars into
        # single append calls to keep span count manageable. Naive
        # per-char append explodes Text's internal span list and stalls
        # Textual's render loop.
        out = Text()
        for y in range(h):
            x = 0
            while x < w:
                idx = x + y * w
                ch = buf[idx]
                if ch == " ":
                    # Run of background spaces — no style needed.
                    run_end = x + 1
                    while run_end < w and buf[run_end + y * w] == " ":
                        run_end += 1
                    out.append(" " * (run_end - x))
                    x = run_end
                else:
                    # Run of identical-shade chars.
                    s = shade[idx]
                    run_end = x + 1
                    while (
                        run_end < w
                        and buf[run_end + y * w] != " "
                        and shade[run_end + y * w] == s
                    ):
                        run_end += 1
                    chunk = "".join(buf[i + y * w] for i in range(x, run_end))
                    out.append(chunk, style=_style_for(s))
                    x = run_end
            if y < h - 1:
                out.append("\n")
        return out


# Coral gradient — dim → bright, matching the 12-step shade ramp.
# Decorative: these 12 gradient stops are intentionally not tokenised — they
# are an easter egg visual effect, not semantic palette values.
_COLOURS = (
    "#3a1a14",
    "#5a2820",
    "#7a3528",
    "#94402f",
    "#a84833",
    "#b8503a",
    "#c45740",
    "#cc6048",
    "#d36b53",
    "#dd7a64",
    "#e58a76",
    "#eea088",
)


def _style_for(shade_idx: int) -> str:
    if shade_idx < 0:
        shade_idx = 0
    elif shade_idx >= len(_COLOURS):
        shade_idx = len(_COLOURS) - 1
    return _COLOURS[shade_idx]
