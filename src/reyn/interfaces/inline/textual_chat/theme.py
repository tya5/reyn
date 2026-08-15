"""Reyn's own full-colour default Textual theme (#4840).

**Why this exists (owner ruling, 2026-08-16, verbatim):** "Textual テーマ採用
時点で端末の規定色に従う意味は無くなってるでしょ。そこ修正して" / "そこは
Textual のテーマから意味にマッピングすべき。reyn の問題。" Deferring to the
terminal's own default colours (the ``ansi-*`` theme family, reyn's default
since #3505) made sense only while reyn had no theme of its own to defer to
instead. This module is that theme — registered with the App and set as its
default in ``app.py``'s ``on_mount`` (replacing ``self.theme = "ansi-dark"``).

**Values are a candidate, not a final visual sign-off.** The MECHANISM
(module structure, registration, default-flip, the ``ansi_default``-family
tokens no longer being reachable from reyn's own default) is what #4840
delegated; the exact RGB is a visual-preference call that stays the owner's
(CLAUDE.md's TUI colour policy: "Never pick a colour first" is about MEANING,
but which concrete value RENDERS a meaning is still a taste call this module
does not get to make unilaterally — see the PR this landed in for the
specific trade-offs flagged for review).

**5 of 8 base tokens reuse existing, already-reasoned values** — no new
colour picked for those, only a new place for the SAME value to live:
``primary``/``accent`` = ``palette.TOKENS["@accent@"]`` (terracotta,
``#d97757``), ``secondary`` = ``@secondary@`` (``#6cb6ff``), ``warning`` =
``@warning@`` (``#e3b341``), ``error`` = ``@error@`` (``#f97066``),
``success`` = ``@success@`` (``#7ee787``) — all #4787/#4861's own
classification of reyn's PRE-EXISTING colour meanings, not a fresh choice.
``surface`` also reuses an existing value: ``#1e222a`` is ``_CC_USER_BG``
(``interfaces/repl/renderer.py``, #3371's WCAG-measured contrast pairing
against ``_CC_DIM``/``@dim@`` — unaffected here, since this module doesn't
touch that pairing's own two values, only reuses one of them for a
DIFFERENT role).

**``background``/``panel`` are the genuinely new choices** — reyn's TUI has
never had a full-colour ground before (#4787's own finding). Picked to stay
in the SAME dark blue-gray family ``surface``/``_CC_ERR_BG`` already
established (lead-coder's own "A" recommendation, offered while explicitly
declining to decide it — visual calls are the owner's).

**``panel`` is explicit, not ``boost``-derived — a rejected alternative,
not an oversight.** Textual's own ``ColorSystem.generate()`` only computes
``panel`` from ``surface.blend(primary, 0.1) + boost`` when ``panel`` is
left unset (measured, #4840's own ``boost``-anomaly investigation). Tried
that path first: ``surface="#1e222a"`` blended 10% toward ``primary``
(``#d97757``, terracotta — a WARM hue) pulls the auto-derived ``panel``
toward brown (measured: resolves to ``#363034``), drifting OFF the cool
blue-gray family ``background``/``surface`` are IN — a visible seam nobody
asked for. An explicit ``panel="#232833"`` (between ``background`` and
``surface`` in the same family) avoids it; the cost is ``boost`` staying
transparent (harmless — nothing in reyn's own CSS reads ``$boost`` today).

**``variables["screen-selection-background"/"-foreground"]`` deliberately
carry #3542's own intent forward**, not Textual's auto-derived default
(``primary.with_alpha(0.5)``, which would use the ACCENT hue at 50% alpha —
loud, the same complaint #3542 was about, just with reyn's own primary
instead of Textual's ``ansi_bright_blue``). #3542, read explicitly per
lead-coder's #4840 ruling: a MUTED, DISTINCT hue — not full accent
intensity — is the actual invariant, not "blue" specifically. Chosen a
muted blue independent of ``primary``/``secondary`` (neither terracotta nor
reyn's own bright secondary blue) so the band reads as a selection
overlay, not as either semantic accent. WCAG contrast against this
module's own ``foreground`` (left unset — auto-derives to
``background.inverse``, measured ``#e5e2dc``, so not re-declared here):
5.50:1 (comfortably above AA's 4.5:1 for normal text; not AAA-loud either).

**Scope**: this module is the theme-BUILD half of #4840's "full colour"
ruling — it does not add the config knob to let a user pick a DIFFERENT
theme (the original #4840 issue's item ①, ``self.theme`` from config
instead of a fixed literal) or override ``palette.py`` tokens from config
(item ②). Both stay open, tracked on #4840; this module only replaces WHAT
the fixed literal points at.
"""
from __future__ import annotations

from textual.theme import Theme

from reyn.interfaces import palette

#: Every value below is a ``palette.TOKENS`` lookup, not a literal — this
#: module names no colour of its own; ``palette.py`` is still the one place
#: ``interfaces/`` does that (``test_tui_colour_tokens.py`` enumerates every
#: colour-bearing declaration under here and would fail on a literal hex in
#: this file). The 5 base tokens (``primary``…``accent``) reuse EXISTING
#: values; ``@theme-*@`` are the genuinely new ones — see their own comments
#: in ``palette.py`` for the reasoning (boost-vs-explicit-panel, #3542, WCAG
#: contrast) this module's own docstring above summarises.
#:
#: The active default background/foreground/panel/surface WCAG contrast
#: ratios this module's own docstring cites (5.50:1 selection,
#: 13.06:1 background/foreground, 11.42:1 panel/foreground) are computed
#: once, off these token values, in this module's own test — see
#: ``tests/interfaces/test_reyn_theme_4840.py`` — not re-verified here at
#: import time, so a value edited in ``palette.py`` without re-running that
#: test can silently drift out of the range this docstring documents.
REYN_THEME = Theme(
    name="reyn",
    primary=palette.TOKENS["@accent@"],
    secondary=palette.TOKENS["@secondary@"],
    warning=palette.TOKENS["@warning@"],
    error=palette.TOKENS["@error@"],
    success=palette.TOKENS["@success@"],
    accent=palette.TOKENS["@accent@"],
    background=palette.TOKENS["@theme-background@"],
    surface=palette.TOKENS["@theme-surface@"],
    panel=palette.TOKENS["@theme-panel@"],
    dark=True,
    variables={
        "screen-selection-background": palette.TOKENS["@theme-selection-bg@"],
        "screen-selection-foreground": palette.TOKENS["@theme-selection-fg@"],
    },
)

__all__ = ["REYN_THEME"]
