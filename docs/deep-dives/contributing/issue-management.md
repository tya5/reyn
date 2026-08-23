# Issue management

Normative for the lead-coder role. Decided by the repo owner 2026-08-22/23.

## 1. What an issue is

An issue is **one problem to be addressed in future**. It is not a record of
what happened. Owner, verbatim: 「issue は将来対策すべき問題ごとに一つ作る
べき」「単なる日記を起票すべきではない」.

A body that is mostly a chronicle of an incident, with the future problem
appearing in a single sentence or not at all, is not an issue — the
chronicle belongs in the PR or a doc, and only the problem statement belongs
in the issue. This keeps the backlog searchable as a list of problems, not a
log of sessions.

## 2. Consolidation

**Issues that would be addressed in the same change are one issue.** Owner,
verbatim: 「一緒に対策すべき問題を分散させるのは悪手」「同時に対策すべき
issue は統合すべき」.

The test is NOT "are these different problems" — that test passes too
easily, and passing it too easily is exactly how issues get split that
should not have been. The test is **"would fixing one force touching what
the other is about"**.

Measured instance: #5141 was split from #5139 on the grounds that one was
about rendering and the other about wire volume — two different problems by
the easy test. They were folded back after the architect's own note showed
the #5139 fix creates the data structure (`BacklogBatch`) that #5141's
limit/cursor has to live in — splitting them meant the second issue's fix
would have rewritten the first's structure.

Evidence for a grouping must be a **named artifact** — a file, function, or
data structure both issues would change. A shared theme ("both about the
TUI") is not evidence; a shared artifact is.

## 3. The only axis for closing

**An issue closes when its claim is no longer true** — fixed, superseded, or
never real to begin with. Verify this against the code, not against the
issue's own text.

Explicitly NOT grounds for closing:

- nobody plans to do it
- nobody has picked it up
- it is old
- the backlog is long

Owner, verbatim: 「計画ないものを落とすはダメでしょ。そもそも issue は計画で
はない」. A backlog exists precisely to hold problems nobody has planned yet;
closing the unplanned ones inverts its purpose — it would leave the backlog
holding only what is already being worked, which is what a project board is
for, not what a backlog is for.

## 4. Priority is a judgement, and it is the lead's

Ordering a backlog is not a sort key — someone has to judge, and the
judgement has to be defensible or it is decoration. Owner, verbatim: 「順序に
は優先順位を判断しなきゃいけないんだよ？」.

The axes below are taken from the repo's own charter (`docs/concepts/architecture/charter.md`)
rather than invented for this purpose, in order:

1. **band** — violates the cross-cutting band (permission · audit-events ·
   workspace-SSoT · crash-recovery/WAL · cost-budget/bounding). The charter
   says a band failure does not ship, and band violations are exempt from
   the count thresholds that otherwise suppress low-frequency work.
2. **owner-hit** — the operator hits it in the shipped configuration.
   Broken-in-reality outranks unclean-in-design.
3. **silent** — a wrong answer arrives with no signal. Nothing turns red, so
   it survives indefinitely unless someone goes looking for it.
4. **blocks-others** — the absence of a mechanism stalls other sessions. One
   defect here costs N people's time, not one.
5. **thin areas** — Retrieval and Evaluation, which the charter names as the
   areas where new work is most valuable.

Demotion axis: **ours-only** — an issue whose only consumer is our own
process (not the operator, not the product) ranks below product work.

## 5. Labels carry the judgement so it can be made cheaply

Owner, verbatim: 「その判断を効率化するためにタイトルやラベルを使うように
してよ」. Triage must not require reading 35 bodies to re-derive an ordering
that was already decided once.

These labels exist in the repo now, carrying the meanings below:

- `band`, `owner-hit`, `silent`, `blocks-others`, `ours-only`,
  `thin:retrieval`, `thin:evaluation` — the axes from §4.
- `priority:next` — the lead has judged this issue and it is next.
  **Absence of `priority:next` means "not yet judged", NOT "low
  priority"** — a scale with more levels than there are judgements
  manufactures false precision, so there is no `priority:later` or
  `priority:low` tier to fall back to.
- `blocked:external` — needs owner judgement or an upstream dependency. An
  open issue without it is pickable by any session. This label tells other
  sessions not to touch the issue, so a stale one makes real work invisible:
  #4364 carried the label after its own hold had been lifted and after
  another session had already started the work — the label was still
  telling readers to stay away from work already in progress.

A new issue gets its axis label(s) when it is filed, not later. Deferring
the label is how a backlog ends up with 35 issues at equal, unlabeled
weight.

## 6. What the lead owes the backlog

Each of these is a duty, not a description — and each has a failure mode
observed on 2026-08-22:

- **Know who holds each issue and whether it moves.** Without this the lead
  cannot answer "who is on #5010" without re-reading the issue from
  scratch.
- **Keep an order.** With 35 pickable issues at equal weight, nobody can
  choose — the lead gets asked every time, and searches for an answer while
  other sessions sit idle.
- **Have the next thing decided before someone frees up.** Sessions went
  idle repeatedly that night and work was found reactively, after the idle
  time had already been spent.
- **Notice what is not being filed.** Fourteen issues were filed in one
  night and none touched Retrieval or Evaluation — the two areas the
  charter names as thin. A backlog that only holds what the team tripped
  over mirrors the team's own path through the code, not the product's
  actual gaps.

## 7. Priority over reviewing and implementing

Owner, verbatim: 「リーダはレビューと作業よりも issue 管理を優先すべきで
しょ。レビューは arch でもできるし、作業は coder/subagent にふれば良いだ
け」.

PR review — finding blocking points — defaults to the architect.
Implementation goes to a coder session, or to a sonnet subagent when the
work is mechanical and the design is already settled. The lead does not
implement.

What stays with the lead: issue management (§1–§6), deciding who gives the
independent TESTS-READ note (house rule 8 — it cannot be the PR's author or
the design's author), and merging.
