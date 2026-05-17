---
title: TUI light-terminal compatibility audit
last_updated: 2026-05-17
status: draft
---

# TUI light-terminal compatibility audit

## TL;DR

The premise "Reyn TUI is unreadable on a light terminal because its text uses
dim grey hex codes (`#444444` … `#aaaaaa`)" does not hold under the bundled
`reyn` Textual theme. The theme is constructed with `dark=True`, which makes
Textual's color system generate `$surface = #1E1E1E` and `$background = #121212`
regardless of the host terminal's background. Every conversation-pane widget
either sets an explicit dark background (`#111111`, `#1a1a1a`) or inherits the
dark `$surface` through a parent chain that does. Inner widgets that set
`background: transparent` (`RichLog`, `StickyStatus`, `StreamingRow Markdown`,
`_PreviewPane RichLog`) composite against the dark parent — the host
terminal's pixels do not bleed through.

A light-terminal-readability problem only re-appears if a user actively
switches away from the `reyn` theme to a Textual built-in light theme such as
`ansi-light` or `solarized-light` (and Reyn has no user-facing way to do that
in v1). When a configurable theme system ships, this doc's survey becomes the
checklist for what would need to be re-pointed.

**Recommendation: defer (Option A).** Do not refactor 8 widgets and 360
hex-color call sites today. Track this as a follow-up tied to "add user-
selectable themes".

## Survey — every hex color in the TUI

Counted from `grep -rn -E "#[0-9a-fA-F]{3,6}" src/reyn/chat/tui/`. 60 distinct
hex values across 360 occurrences (incl. `_palette.py`, `theme.tcss`, every
`DEFAULT_CSS` block, and Rich `Text(..., style="#...")` calls).

### Surfaces (background fills)

| Hex | Use |
|---|---|
| `#111111` | `_BG_PANEL` — right panel, InputBar, RightPanel tabs |
| `#1a1a1a` | `_BG_HEADER` — ReynHeader strip, `_PanelHeader`, `SlashPicker`, `_PreviewPane` header |
| `#1e1510` | `InterventionWidget` soft-tinted box |
| `#2a1a10` | `InterventionWidget Input` field |
| `#1f5856` | "you" header dash background (teal-dim) |
| `#5a2020` | "reyn" header dash background (coral-dim) |
| `#444444` | "system" header dash background |

### Borders / dividers

| Hex | Use |
|---|---|
| `#2a2a2a` | `_BORDER_DIM` — default panel/section border |
| `#333333` | `_DIVIDER_DIM` — `_PanelHeader` bottom + `#content` top divider |

### Text-color ramp (the population called out in the prompt)

| Hex | Palette name | Typical use |
|---|---|---|
| `#444444` | `_TEXT_DIMMEST` | timestamps, dim hints |
| `#555555` | `_TEXT_DIM` | secondary labels, hint strips, "no match" placeholders |
| `#666666` | `_TEXT_NEUTRAL` | InputBar hints, panel-content default fg |
| `#777777` | — | tertiary labels in cost/agents tabs |
| `#888888` | — | system-header label, +N pending hint, slash-picker summary |
| `#aaaaaa` | `_TEXT_BODY` | body / status / right-panel labels |
| `#bbbbbb` | — | selected slash-picker description |
| `#cccccc` | — | (defined but no direct callers in current source) |
| `#dddddd` | `_TEXT_BRIGHT` | primary content, header status text |
| `#eeddcc` | — | InterventionWidget body |
| `#ffffff` | — | block-cursor fg, InputBar TextArea fg, button text |

### Accent (coral primary + variants)

| Hex | Use |
|---|---|
| `#C8553D` | `_CORAL` — `Theme.primary`, also `accent`, block-cursor bg |
| `#e0664e` | Button hover/focus |
| `#3a1a14 … #eea088` | 12-stop donut gradient (`widgets/donut.py`) |

### Status colors (cost tab, skill activity, traces)

| Hex | Use |
|---|---|
| `#44cc88` | OK / running checkmark |
| `#2d7a4f` / `#447744` / `#335544` / `#88ddaa` | dim greens (cost, sub-items) |
| `#ff6644` / `#ff4444` / `#cc5555` / `#ff7777` / `#aa6666` / `#884444` | errors |
| `#ff9944` / `#ffaa44` / `#ffaa66` / `#ffcc66` / `#ffcc88` / `#aa8844` / `#aaaa55` | warnings, plan IDs, queued |
| `#4abbb5` / `#1f5856` | "you" speaker |
| `#7a9fc7` / `#88aaff` / `#cc88ff` | misc accents |
| `#d8ffd8` / `#88ff88` / `#33dd33` / `#117711` | matrix demo (Easter-egg-ish) |

### Scrollbars

Tracked under `_BORDER_DIM` (`#2a2a2a`); active uses `$primary`.

## Why this doesn't break on a light terminal

### 1. Theme generation is dark-locked

`ReynTUIApp._REYN_THEME` is built with `dark=True`:

```python
_REYN_THEME = Theme(
    name="reyn",
    primary="#C8553D",
    accent="#C8553D",
    dark=True,
    ...
)
```

Calling `ColorSystem(primary='#C8553D', dark=True).generate()` produces
literally:

```
background  #121212
surface     #1E1E1E
panel       #372B29
surface-active  #2A2A2A
```

These are the values bound to `$surface`, `$background` etc. in `theme.tcss`.
The terminal's actual background color is irrelevant — Textual paints
`#1E1E1E` on every cell the Screen owns.

(Side note: Textual has an `ansi=True` flag that *would* defer to terminal
defaults — used by the built-in `ansi-light` / `ansi-dark` themes. The Reyn
theme uses the default `ansi=False`, so no ANSI passthrough.)

### 2. Every major container has an explicit dark bg

Following the DOM from Screen down through ConversationView and the right
panel, every container sets its own opaque dark fill:

| Widget | Source | Background |
|---|---|---|
| `Screen` | `theme.tcss:25` | `$surface` (= `#1E1E1E`) |
| `ReynHeader` | `theme.tcss:31`, `header.py:29` | `#1a1a1a` |
| `InputBar` | `theme.tcss:76`, `input_bar.py:61` | `#111111` |
| `InterventionWidget` | `theme.tcss:106`, `intervention.py:42` | `#1e1510` |
| `RightPanel` | `right_panel/__init__.py:66` | `#111111` |
| `_PanelHeader` | `right_panel/shells.py:33` | `#1a1a1a` |
| `SlashPicker` | `widgets/slash_picker.py:34` | `#1a1a1a` |
| `_PreviewPane #preview-header` | `right_panel/shells.py:167` | `#1a1a1a` |

`ConversationView` itself doesn't set a `background:` rule, but it inherits
from Screen's `$surface`, which is dark.

### 3. `background: transparent` cascades to the dark parent, not to the terminal

In Textual, "transparent" means "blend with the parent widget's resolved
background pixel". Since every parent up the chain resolves to a dark hex,
the transparent inner widgets render onto dark fills:

| Widget | Source | Effective bg |
|---|---|---|
| `ConversationView > RichLog` | `theme.tcss:68` | inherits ConversationView → `$surface` = `#1E1E1E` |
| `StickyStatus` | `widgets/sticky_status.py:53` | inherits ConversationView → `$surface` |
| `StreamingRow Markdown` | `widgets/streaming_row.py:71` | inherits ConversationView → `$surface` |
| `_PreviewPane RichLog` | `right_panel/shells.py:178` | inherits `_PreviewPane` → `RightPanel` `#111111` |
| `InputBar TextArea` | `theme.tcss:88` | inherits `InputBar` `#111111` |

So the dim-grey hex codes (`#444444`, `#555555`, `#666666`) are always
painted against a `#111111`–`#1E1E1E` backdrop, regardless of the host
terminal.

## When this *would* break

The audit is conditional on the user running the default `reyn` theme. The
known break path is:

1. The user switches to a Textual built-in `ansi-light` theme (uses
   `ansi=True` → terminal-default surface).
2. The user wires a custom Theme with `dark=False`. Textual's color-system
   generator with `dark=False` produces a light `#fbfbfb`-ish surface — at
   which point every dim-grey fg becomes unreadable.

Neither of those is reachable in v1. There is no user-facing theme picker, no
`/theme` slash command, no CLI flag. The only way in is to monkey-patch
`ReynTUIApp.theme` from a custom entry point.

## Option B — what a "minimum viable" fix would look like

Documented for completeness. *Not* recommended now.

The minimum viable change that would make the TUI light-terminal-safe even
under a future light theme:

1. **Lock the Screen bg.** Replace `Screen { background: $surface; }` with
   `Screen { background: #111111; }` (or expose a hard `--reyn-bg` variable).
2. **Drop or harden `background: transparent`** in:
   - `theme.tcss` line 68 (`ConversationView > RichLog`)
   - `widgets/sticky_status.py` line 53
   - `widgets/streaming_row.py` line 71
   - `widgets/right_panel/shells.py` line 178 (`_PreviewPane RichLog`)
3. **Audit Rich `Text(..., style="#...")` call sites.** ~80 occurrences in
   `widgets/right_panel/__init__.py`, `widgets/conversation.py`, `app.py`,
   `widgets/right_panel/cost_tab.py`, `widgets/right_panel/docs_tab.py`.
   Each renders inside a RichLog; if the RichLog has an opaque dark bg per
   (2), these are fine, otherwise each `style="#666666"` line needs `on
   #111111`.

Scope: ~5 CSS rules + 1 Screen rule + verifying ~80 Rich Text sites read
from a known-dark RichLog. The prompt budgeted "~5 widgets max" for Option
B; this is right at that edge if you trust the RichLog-bg fix, and over it
if every Rich Text call site has to be touched. Combined with the fact that
no observed user complains about light-terminal readability *today*,
deferring is the right trade-off.

## Recommendation

**Option A — defer.** Action items:

- File a follow-up issue: *"Add a user-selectable theme. When it ships,
  apply the Option-B checklist in this doc so light themes don't regress."*
- When the issue lands, this doc's survey table is the literal acceptance
  checklist: every hex in §"Text-color ramp" needs either (a) replacement
  with a theme variable or (b) an explicit dark `on`-background companion.
- Until then, keep `_palette.py` as the single source of truth so a future
  theme refactor edits 1 file, not 7+.

## Pre-conclusion checklist (CLAUDE.md trigger)

Triggered by "conclusion / recommendation" framing above. Walk-through:

1. **List observations.** ColorSystem(dark=True) output (primary data,
   captured via direct `python3 -c` invocation in this audit); theme.tcss /
   widget DEFAULT_CSS reads (primary data, direct file reads); count of 60
   distinct hex codes / 360 call sites (`grep` output, primary data).
2. **Primary vs inferred.** All three above are primary. The claim "every
   transparent widget composites against a dark parent" is one inferential
   hop from "every parent has a dark bg AND Textual transparency =
   composite-with-parent". Textual's transparency semantics is documented;
   the parent-chain check is direct.
3. **Falsification attempts.** Looked for: `ansi=True`, user-switchable
   themes, widgets that set `background:` to nothing (none found in the
   chain above ConversationView), Rich Text styles that bypass widget bg
   (they don't — they paint on top of widget bg). The remaining failure
   mode (user manually switches to ansi-light) is acknowledged.
4. **Observation infra.** Direct file reads + a working Textual install
   for `ColorSystem.generate()`. No event log needed for a CSS audit.
5. **N/N inspection.** 60 distinct hex values inspected (full `sort -u`
   list above); 8 widgets with `DEFAULT_CSS` inspected directly. Not
   extrapolated.

Confidence: high that the bundled `reyn` theme is dark-safe today; medium
that a future light theme would need the Option-B fixes listed (untested
because no light theme exists to test against).
