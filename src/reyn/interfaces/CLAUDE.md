# TUI colour policy — rules for `src/reyn/interfaces/`

Name the MEANING; the active Textual theme renders it. Never pick a colour
first (owner ruling #3525). **No terminal dependence** (owner ruling #4840):
reyn ships a full-colour theme, so there is nothing to defer to.

1. Meaning has a convention a reader already carries (*error*, *success*, *in-flight*) → map it to a Textual theme meaning: a theme token (`$error`, `$text-muted`, `$markdown-*` …) or an SGR `text-style`. Never an ANSI name.
2. Meaning is reyn-specific → any value that serves the design, full-colour included.

- **A colour is not a meaning.** "Error" is the meaning; red is one theme's rendering.
- **One thing is forbidden**: a literal in a widget stylesheet. Every value goes through a token in `palette.py`, and stylesheets write a `@name@` marker.
- `tests/interfaces/test_tui_colour_tokens.py` fails on any colour value named outside the palette. Textual's own `DEFAULT_CSS` and `interfaces/web/` are out of scope.
