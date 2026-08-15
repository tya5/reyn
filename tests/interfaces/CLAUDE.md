# Rules for `tests/interfaces/`

The TUI colour policy lives in `src/reyn/interfaces/CLAUDE.md`. Read it before
touching `test_tui_colour_tokens.py` or any test that names a colour — the gate
is here, the rule it enforces is there, and a session that only opens `tests/`
would otherwise never load it.
