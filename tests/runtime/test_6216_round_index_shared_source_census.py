"""Tier 2: #6216 accept⑤ (lead-coder's own revised wording, issue
#6216#issuecomment-5714813943, verbatim): "round を名指せる producer は、
すべて同じ出所から運ぶ" — every producer of a `kind="agent"` completion
frame that CAN name a round carries `round_index` from the SAME source
(`self._delta_round_index`, or a `RouterLoop` instance's own
`_delta_round_index` for `session.py`'s two force-close call sites,
which construct their own throwaway `RouterLoop`) — never a
separately-derived value.

## Population — 7, not 6 (lead-coder's own correction)

An EARLIER round of this investigation (issue #6216's own thread) said
"6" — this test's own first draft found a 7th, previously-missed real
producer (`inter_agent_messaging.py:738`) while tracing call sites
before implementing (lead-coder's own explicit instruction: "呼び出し
を辿って母集団を確定してから着手を"). Ruling (issuecomment-5714813943):
"6" hid whether a site was MISSED or EXCLUDED — the honest count is
**7 real producers, of which 6 can name a round and 1 cannot (a
DECLARED absence, not a gap)**:

- 4 `router_loop.py` `host.put_outbox(kind="agent", ...)` sites (#6218
  already threads `round_index` onto these, for `_call_parent_key`'s
  own sake).
- 2 `session.py` `OutboxMessage(kind="agent", ...)` force-close sites
  (added by #6216 itself).
- 1 `inter_agent_messaging.py` `OutboxMessage(kind="agent", ...)` site
  (a peer-failure notice to the user) — DELIBERATELY carries no
  `round_index`: no LLM round produced this row, so there is no round
  to name (ruling: writing `round_index=0` here would ASSERT a false
  fact — "this is round 0" — when the truth is "there is no round";
  the same "declared absence" shape #5891's own `tool_call_id=None`
  already established). This site's own emit-site comment carries the
  full reasoning.

`app.py:7274` (a streaming first-delta placeholder, settled later by a
REAL completion frame, never a completion itself) and `restore.py:405`
(a historical projection with no live round concept) stay OUT of this
7 entirely — disclosed, not silently dropped, in the original
investigation comment (`#6216#issuecomment-5709582350`).

A census, not a sample (matches `test_router_loop.py`'s own
`test_every_host_put_outbox_call_site_declares_persist_as` — the same
"closing a hole only closes it if EVERY site is checked" reasoning): a
future 8th site that forgets `round_index`, derives it independently
instead of reading the shared `_delta_round_index` counter, or joins
the "declared absence" set without its own comment explaining why, is
caught here — provided it is written as a literal `kind="agent"`
construction in `src/`. A producer reached only through a dynamic
forward, an adapter, or a frame built elsewhere and re-emitted is NOT
visible to this source scan (it regexes literal source text, not
runtime behaviour): one such forward (`router_host_adapter.py`) was
found in this arc's own investigation
(#6216#issuecomment-5709582350); it is a pass-through, not a producer,
but the class exists. A new producer of that shape must be classified
by hand.
"""
from __future__ import annotations

import re

from tests._support.paths import REPO_ROOT

_KIND_AGENT = re.compile(r'(?<![_\w])kind\s*=\s*"agent"')
_ROUND_INDEX_FROM_SHARED_SOURCE = re.compile(
    r'"round_index"\s*:\s*\w+\._delta_round_index\b'
)


def _line_containing(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]


def _is_prose_mention(text: str, pos: int) -> bool:
    """True when the match at *pos* is a comment/docstring MENTION of
    ``kind="agent"`` (a fact stated in prose about the codebase) rather
    than a real keyword argument in code — matches this repo's own
    established distinguishing convention (a docstring-quoted mention
    wraps the phrase in double-backticks; a comment line starts with
    ``#`` before the match)."""
    line = _line_containing(text, pos)
    before_match = line[: pos - (text.rfind("\n", 0, pos) + 1)]
    if "``" in line:
        return True
    if before_match.lstrip().startswith("#"):
        return True
    return False

# Exactly these 2 files carry a round-NAMING kind="agent" completion
# producer today (#6216's own investigation) — router_loop.py's 4
# sites (#6218) and session.py's 2 force-close sites (this stage). Any
# OTHER src/ file that starts constructing a kind="agent" frame is a
# NEW producer this census has not classified — see below for how that
# is caught.
_ROUND_NAMING_PRODUCER_FILES = ("src/reyn/runtime/router_loop.py", "src/reyn/runtime/session.py")

# The ONE site that DELIBERATELY cannot name a round (lead-coder
# ruling, issuecomment-5714813943 — a "declared absence", not a gap;
# see this module's own docstring and the emit site's own comment for
# the full reasoning). Checked SEPARATELY below: it must exist, must
# still construct kind="agent", and must NOT carry round_index (if it
# ever gains one, this classification itself needs revisiting, not a
# silent pass).
_DECLARED_ABSENCE_FILES = ("src/reyn/runtime/services/inter_agent_messaging.py",)

# app.py's own streaming-delta placeholder and restore.py's own
# historical projection are NOT completion producers (disclosed,
# #6216#issuecomment-5709582350) — named here so their own presence in
# the wider repo grep below does not silently inflate or shrink the
# count this test pins.
_DISCLOSED_NON_PRODUCER_FILES = (
    "src/reyn/interfaces/inline/textual_chat/app.py",
    "src/reyn/interfaces/inline/textual_chat/restore.py",
)


def _find_call_containing(text: str, start: int) -> str:
    """Walk outward from *start* (a match INSIDE a call's arguments) to
    the nearest enclosing balanced-paren call — mirrors
    ``test_router_loop.py``'s own forward-walking version, run in
    reverse since here the search starts from a keyword deep inside the
    call rather than from the call's own name."""
    depth = 0
    i = start
    while i >= 0:
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            if depth == 0:
                break
            depth -= 1
        i -= 1
    open_paren = i
    # Now find the matching close paren going forward from open_paren.
    depth = 0
    j = open_paren
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    # Walk back further to include the callable name itself.
    k = open_paren - 1
    while k >= 0 and (text[k].isalnum() or text[k] in "._"):
        k -= 1
    return text[k + 1 : j + 1]


def test_every_live_kind_agent_producer_carries_round_index_from_the_shared_source() -> None:
    """Tier 2: accept⑤ — the census itself. Population is 7: 6 must
    carry round_index from the shared source, 1 (the declared absence)
    must NOT carry it at all."""
    round_naming_calls = 0
    declared_absence_calls = 0
    offenders: "list[str]" = []
    absence_offenders: "list[str]" = []
    unexpected_files: "list[str]" = []

    classified_files = _ROUND_NAMING_PRODUCER_FILES + _DECLARED_ABSENCE_FILES + _DISCLOSED_NON_PRODUCER_FILES

    for path in sorted((REPO_ROOT / "src" / "reyn").rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        matches = [
            m for m in _KIND_AGENT.finditer(text)
            if not _is_prose_mention(text, m.start())
        ]
        if not matches:
            continue
        if rel not in classified_files:
            # A genuinely NEW file constructing kind="agent" — not
            # classified by #6216's own investigation. Flag it rather
            # than silently assume it needs (or doesn't need)
            # round_index.
            unexpected_files.append(rel)
            continue
        if rel in _DISCLOSED_NON_PRODUCER_FILES:
            continue  # app.py / restore.py — disclosed non-producers, skipped
        for m in matches:
            call = _find_call_containing(text, m.start())
            line = text.count("\n", 0, m.start()) + 1
            has_round_index = _ROUND_INDEX_FROM_SHARED_SOURCE.search(call) is not None
            if rel in _DECLARED_ABSENCE_FILES:
                declared_absence_calls += 1
                if has_round_index:
                    absence_offenders.append(
                        f"{rel}:{line} (now carries round_index -- the "
                        "declared-absence classification is stale, fix "
                        "this test's own population instead of leaving "
                        "it silently wrong)"
                    )
            else:
                round_naming_calls += 1
                if not has_round_index:
                    offenders.append(f"{rel}:{line}")

    assert unexpected_files == [], (
        f"a NEW kind=\"agent\" producer appeared outside #6216's own "
        f"classified population -- classify it (round-naming producer, "
        f"declared absence, or disclosed non-producer) before this can "
        f"pass: {unexpected_files}"
    )
    assert round_naming_calls == 6, (
        f"expected exactly the 6 round-naming kind=\"agent\" producer "
        f"call sites (#6216's own census) -- found {round_naming_calls}. "
        f"A count change here means the population moved and this "
        f"test's own population needs a conscious update, not a silent "
        f"drift."
    )
    assert declared_absence_calls == 1, (
        f"expected exactly 1 declared-absence kind=\"agent\" site "
        f"(inter_agent_messaging.py) -- found {declared_absence_calls}."
    )
    assert offenders == [], (
        f"call site(s) missing round_index sourced from the shared "
        f"self._delta_round_index counter (or a local RouterLoop "
        f"instance's own, for session.py's 2 sites): {offenders}"
    )
    assert absence_offenders == [], absence_offenders
