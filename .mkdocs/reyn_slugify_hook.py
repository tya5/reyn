"""Custom `toc` slugify used by `.mkdocs/mkdocs.yml` (#4173).

Same slug output as python-markdown's own default `toc.slugify` for every
character it can already ASCII-fold (Latin diacritics, circled numerals,
etc. via NFKD) — this function changes NOTHING for those. It differs only
for a character NFKD+ascii-fold would otherwise drop entirely (CJK has no
compatibility decomposition to ASCII), where it keeps the original
character instead of silently vanishing it. That vanishing is #4173's bug:
an all-Japanese heading folds to an empty slug, so mkdocs falls back to a
purely positional id (`_1`, `_2`, ...) that shifts if a heading is added
above it anywhere on the page.
"""
from __future__ import annotations

import re
import unicodedata


def slugify(value: str, separator: str) -> str:
    """Per-character ASCII-fold-or-keep, then the same regex pipeline
    ``markdown.extensions.toc.slugify`` applies. Per-character (not
    per-string) NFKD is deliberate: a whole-string NFKD+ascii-encode('ignore')
    is what silently drops CJK text today — folding one character at a time
    and falling back to the original only when THAT character's own fold is
    empty preserves every existing ASCII/Latin/circled-numeral anchor
    byte-for-byte while no longer dropping non-foldable characters."""
    folded_chars = []
    for ch in value:
        ascii_ch = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
        folded_chars.append(ascii_ch if ascii_ch else ch)
    folded = "".join(folded_chars)
    folded = re.sub(r"[^\w\s-]", "", folded).strip().lower()
    return re.sub(r"[{}\s]+".format(re.escape(separator)), separator, folded)


def on_config(config, **kwargs):
    """Inject this module's ``slugify`` as the ``toc`` extension's slugify
    function. Done here, in code, rather than via a `!!python/name:`/
    `!!python/object/apply:` tag in mkdocs.yml — a hook file is loaded with
    its own directory on ``sys.path`` (mkdocs's own `Hooks` config option),
    so a plain top-level `import` resolves regardless of the CWD mkdocs is
    invoked from, unlike a YAML python tag naming a bare module name."""
    config["mdx_configs"].setdefault("toc", {})["slugify"] = slugify
    return config
