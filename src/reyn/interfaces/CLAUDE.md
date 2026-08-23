# TUI colour policy — rules for `src/reyn/interfaces/`

Name the MEANING; the active Textual theme renders it. Never pick a colour
first (owner ruling #3525). **No terminal dependence** (owner ruling #4840):
reyn ships a full-colour theme, so there is nothing to defer to.

1. Meaning has a convention a reader already carries (*error*, *success*, *in-flight*) → map it to a Textual theme meaning: a theme token (`$error`, `$text-muted`, `$markdown-*` …) or an SGR `text-style`. Never an ANSI name.
2. Meaning is reyn-specific → any value that serves the design, full-colour included.

- **A colour is not a meaning.** "Error" is the meaning; red is one theme's rendering.
- **One thing is forbidden**: a literal in a widget stylesheet. Every value goes through a token in `palette.py`, and stylesheets write a `@name@` marker.
- `tests/interfaces/test_tui_colour_tokens.py` fails on any colour value named outside the palette. Textual's own `DEFAULT_CSS` and `interfaces/web/` are out of scope.

## TUI state-flow discipline (`textual_chat/`) — #5131

Architect ruling: "up" (widget→App) is already a framework — 10+ `Message`
subclasses, a widget never touches the wire directly. "Down" (App→widget) was
NOT — the App called widget setters imperatively at scattered sites/times,
so status bar / history / agent tab / announce could show mismatched values
after a session switch (the reported bug).

1. **Down is `reactive` + `watch_` only.** The App never calls a widget
   setter directly — it writes a `reactive` attribute and lets `watch_*`
   propagate. One canonical trigger per piece of state, not N call sites
   hoping to stay in sync.
2. **Up stays `Message`-only.** Already true — this codifies it, doesn't
   change it.
3. **A widget holds no state of its own.** It renders the slice it is
   given. Copying a prop into instance state to render from later (the
   `#5116`-named anti-pattern) is forbidden — a copy is a second source of
   truth the original can drift from.
4. **Never query a live object mid-render.** Render from what you were
   handed, not from re-fetching the current truth at draw time (owner:
   "都度問い合わせる…旧世代UI設計はやめて").

Gate A (`scripts/check_tui_widget_boundary.py`, structural, zero-FP):
pins that only `app.py` imports `reyn.interfaces.transport`/
`reyn.runtime.registry` — every other widget module must not. Gate B
(`scripts/check_tui_reactive_ratchet.py`) is a ratchet, not a rule
enforcement — see that script's own docstring for why the
`reactive`/`watch_` count can only be pinned to never DECREASE, and the
imperative-push count to never INCREASE, not proven sufficient on their
own.
