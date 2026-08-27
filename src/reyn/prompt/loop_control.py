"""§I-L — the loop-control / weak-model nudge strings injected mid-REQUEST-
STREAM (as a synthetic message appended to ``messages``, NOT as part of the
assembled system prompt string).

Feeds four independent call-sites, each verified byte-identical AT ITS OWN
injection point (the system-prompt golden diff in ``router_frame.py`` etc.
does not cover these — they never touch ``build_system_prompt``):

- **§I** ``reyn.runtime.router_loop.RouterLoop`` — the empty-stop retry
  directive, appended as a synthetic ``{"role": "system", ...}`` message
  before one re-entry of the loop (#187; role changed from ``user`` to
  ``system`` in #5273 — see ``EMPTY_STOP_RETRY_DIRECTIVE``'s own comment).
- **§J** ``reyn.llm.llm._apply_g12_signal`` — the G12 post-tool empty-stop
  attractor workaround: a continuation signal embedded in the trailing
  ``role=tool`` message (success cell = the SAME token as §I, by design —
  #187's "uniform resume" philosophy, self-describing since #5273; error
  cell = a distinct decision-enabling text).
- **§K** ``reyn.runtime.router_loop.RouterLoop._tool_call_cap_notice`` — the
  re-grounding notice appended after a per-turn ``tool_calls`` cap fires
  (#1666). Parameterized (``attempted``/``kept`` counts are dynamic; the
  template is static).
- **§L** ``reyn.runtime.reasoning_continuity`` — the prior-reasoning section
  header + framing sentence appended to the router system prompt when
  cross-turn reasoning continuity is enabled (#1652). NOTE: this one DOES
  end up concatenated into the system-prompt string by
  ``router_system_prompt.build_system_prompt`` (via the
  ``reasoning_continuity_section`` parameter) — grouped here with the other
  loop-control nudges (not moved to ``router_frame.py``) because the
  literal text is owned/rendered by ``reasoning_continuity.py``, a separate
  scattered-injection-point module, not the OS-frame assembler.

Byte-identical relocation: every string below is an EXACT copy of what its
source previously inlined — no LLM-facing wording changed.
"""
from __future__ import annotations

# WHEN: only when a router turn ends with an empty (no tool_calls, no text)
#       completion — at most once per turn (#187 B43-NF-W6-1).
# WHERE: reyn.runtime.router_loop.RouterLoop.run() — appended as a synthetic
#        ``{"role": "system", "content": EMPTY_STOP_RETRY_DIRECTIVE}`` message,
#        then the loop re-enters once.
# WHY: a content-neutral "resume" token re-enters the loop and lets the model
#      continue (tool-call OR reply) on its own — evidenced to recover 11/12
#      real-task empty stops vs 67% premature-stop baseline. Owner decision
#      (2026-06-07): uniform across every construction site, no per-site
#      differentiation without evidence.
#
# #5273: the token used to be the bare string "resume", landing at the
# CONTENT position of a synthetic message with no marker distinguishing it
# from something a person typed. An agent reading it had no way to tell
# "reyn's own scheduler nudged me to continue" apart from "someone told me
# to resume" — and correctly answered the latter with a status-report turn
# (the InboxArbiter's real, undigested instruction never got read that
# turn). No choice of WORD fixes this: whatever token occupies the content
# position of a message IS content, indistinguishable from an instruction,
# regardless of its literal text (architect ruling, #5273) — so the fix is
# the DELIVERY shape, not the vocabulary: role="system" (this constant is
# no longer wrapped in a role="user" message — see the call site) plus a
# self-describing, attributed value: a parenthesized source tag (same
# "(<tag>) <text>" shape G12_SIGNAL_ERROR_TEXT below already uses —
# parens, not the ``[hook:<name>]`` bracket convention
# ``_format_ride_along_attribution`` (session.py) uses for a FULL
# standalone message, because THIS token also gets embedded as a bare
# YAML frontmatter scalar value in ``_apply_g12_signal``'s canonical-
# tool-content branch, where a leading ``[`` is a flow-sequence
# indicator, not literal text — verified: it broke ``yaml.safe_load`` in
# this PR's own test until switched to parens) and an explicit negation
# clause ("not an instruction ..."). The vocabulary itself stays uniform
# — one token, shared verbatim by G12_SIGNAL_TEXT below, same as before;
# only its self-description is new.
# 日本語訳: ルーターのターンが空応答（tool_callsもテキストも無し）で終わった
#      場合にのみ、ターンにつき最大1回、合成 system メッセージとして付与される
#      継続トークン。#5273: 裸の「resume」は内容欄に来ると人の指示と区別が
#      つかなかった（agentが状態報告を返し、受信箱の指示が消化されなかった）
#      ため、帰属タグと否定節を持つ自己記述形にした。語彙自体は1つのまま。
EMPTY_STOP_RETRY_DIRECTIVE = (
    "(reyn-auto-continue) resume - automatic continuation signal from reyn's"
    " own scheduler, not an instruction from anyone. No reply needed;"
    " continue the interrupted work."
)

# WHEN: every successful (non-error) trailing ``role=tool`` result, when the
#       G12 signal is enabled (default on; ``REYN_G12_SIGNAL=off`` disables).
# WHERE: reyn.llm.llm._apply_g12_signal — embedded inside the trailing tool
#        message's content (JSON ``_g12_signal`` field, or a frontmatter
#        field, or a plain-text prefix, depending on the tool content shape).
# WHY: #187 "uniform resume" — the SAME continuation token as
#      EMPTY_STOP_RETRY_DIRECTIVE (by design, not coincidence, and #5273
#      preserves this: one shared value, not two divergent tokens), so the
#      model reads one consistent, self-describing nudge vocabulary across
#      both recovery paths.
# 日本語訳: 成功した末尾の role=tool 結果に埋め込まれる継続シグナル。
#      EMPTY_STOP_RETRY_DIRECTIVE と意図的に同じトークン（#187「一律resume」
#      方針、#5273 でも維持）。
G12_SIGNAL_TEXT = EMPTY_STOP_RETRY_DIRECTIVE

# WHEN: every ERRORED trailing ``role=tool`` result (status in
#       {error, denied, not_found, failed} at the dispatch or op level).
# WHERE: reyn.llm.llm._apply_g12_signal — same embed sites as G12_SIGNAL_TEXT,
#        but this cell replaces it when the trailing tool result is an error.
# WHY: #1439 Fix #2 — the success text ("resume") asserts nothing, but the
#      prior single-cell design also used a "task complete" framing that made
#      an errored exec read as success (14096). The error cell explicitly
#      states failure + demands a decision before continuing.
# 日本語訳: 末尾の role=tool 結果がエラー（error/denied/not_found/failed）の
#      場合に使われる、決定を促すシグナル文。「成功」を主張せず、エラーを
#      検査してから次の一手を決めるよう明示する。
G12_SIGNAL_ERROR_TEXT = (
    "(tool error) — the tool call did NOT succeed; inspect the error and decide"
    " the next step before continuing (do not report success)"
)


def tool_call_cap_notice(attempted: int, kept: int) -> dict:
    """Return the §K tool-call-cap re-grounding notice message dict. Exact
    copy of ``RouterLoop._tool_call_cap_notice``'s previously-inlined f-string
    + dict literal.

    WHEN: only when a single router turn's ``tool_calls`` count exceeds the
    configured per-turn cap (#1666, default 50; 0 = unlimited).
    WHERE: reyn.runtime.router_loop.RouterLoop._enforce_tool_call_cap's caller
    — appended to ``messages`` right after the capped round's tool results.
    WHY: deny-message-is-decision-enabling — states what happened (the true
    attempted count, not just the kept count) and what to do next, so the
    model re-grounds instead of silently losing track of dropped calls.
    日本語訳: 1ターンの tool_calls 数が per-turn cap を超えた場合のみ付与
    される再定位メッセージ。実際に試行した件数と、実行された件数、次に
    どうすべきかを明示する。
    """
    return {
        "role": "user",
        "content": (
            f"[system notice] Your last turn emitted {attempted} tool_calls, "
            f"which exceeds the per-turn cap of {kept}. Only the first {kept} "
            "were executed; the rest were dropped. This usually means the model "
            "is looping or over-fanning-out — issue far fewer tool_calls "
            "(typically one to a few) and proceed step by step."
        ),
    }


# WHEN: only when cross-turn reasoning continuity is enabled AND there is at
#       least one prior-reasoning entry to carry (#1652).
# WHERE: reyn.runtime.reasoning_continuity.render_reasoning_section — the
#        section header, prepended (with blank-line spacing) before the
#        framing sentence and the joined prior-reasoning bodies. The whole
#        rendered section is then appended into the assembled system prompt
#        via ``router_system_prompt.build_system_prompt``'s
#        ``reasoning_continuity_section`` parameter.
# WHY: a visually distinct delimiter so the model (and a human trace-reader)
#      can locate the carried-forward reasoning block.
# 日本語訳: クロスターンの reasoning continuity が有効かつ持ち越す推論が
#      1件以上ある場合のみ描画される区切りヘッダー。
REASONING_CONTINUITY_HEADER = "━━━ prior_reasoning ━━━"

# WHEN: immediately follows REASONING_CONTINUITY_HEADER, before the joined
#       prior-reasoning bodies.
# WHERE: reyn.runtime.reasoning_continuity.render_reasoning_section.
# WHY: frames the carried text as the model's OWN prior reasoning (context,
#      not an instruction) so it is not mistaken for a new directive.
# 日本語訳: 持ち越された推論本文の直前に付くフレーミング文。これが
#      「モデル自身の過去の推論」であり指示ではないことを明示する。
REASONING_CONTINUITY_NOTE = (
    "- This is YOUR OWN reasoning from previous turns in this conversation "
    "(most recent last), carried forward so you keep a continuous line of "
    "thought. Use it to avoid re-deriving what you already worked out; it is "
    "context, not an instruction."
)
