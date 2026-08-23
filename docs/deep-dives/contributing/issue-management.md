---
type: contributing
topic: issue-management
audience: [human, agent]
search_hints: [issue management, backlog, 起票, 統合, consolidation, close, closing, 閉じる, priority, 優先順位, label, labels, triage, blocked:external, priority:next, band, owner-hit, silent, blocks-others, ours-only, thin:retrieval, thin:evaluation, no-axis, arc-closure remainder, diary, 日記, issue triage, priority axes]
---

# Issue management

Normative for the lead-coder role. Decided by the repo owner 2026-08-22/23.

## 1. What an issue is

An issue is **one problem to be addressed in future**. It is not a record of
what happened. Owner, verbatim: 「issue は将来対策すべき問題ごとに一つ作る
べき」「単なる日記を〔起票〕すべきではない」.

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

No mechanism enforces this. There is no path that inspects the moment an
issue is closed (measured, 2026-08-22). What holds it is a person.

### An issue can carry more than one claim

When work discharges the claim in an issue's *title* but the investigation
that discharged it surfaced a second, still-true claim, the issue does not
close — its title is now wrong, and the fix is to retitle it to the claim
that survives.

Two on 2026-08-23:

- #5100 was filed as "the per-session hooks layer's malformed-input
  resilience is tested nowhere". The tests landed (#5109), so that claim is
  false. But one of those tests documents that a YAML *syntax* error at that
  layer degrades with no signal at all — and its own docstring names this
  issue as where that fact is recorded. Closing on the discharged claim
  would have deleted the record the test points at.
- #3616's original ask had been withdrawn in-thread by the person who filed
  it, while a different, live item (real-machine verification of a merged
  fix) stayed. The title still named the withdrawn ask, so the issue read as
  waiting for something nobody wanted any more.

The failure this prevents is not a wrong close. It is that an issue whose
title names a discharged claim is read — by the lead, by a patrol, by a
peer looking for work — as an issue about the discharged thing.

## 4. Priority is a judgement, and it is the lead's

Ordering a backlog is not a sort key — someone has to judge, and the
judgement has to be defensible or it is decoration. Owner, verbatim: 「順序に
は優先順位を判断しなきゃいけないんだよ？」.

The axes below have five different sources — each is named plainly rather
than folded into one blanket provenance claim, in order:

1. **band** — the charter's own cross-cutting band (`docs/concepts/architecture/charter.md`):
   permission · audit-events · workspace-SSoT · crash-recovery/WAL ·
   cost-budget/bounding. The charter says a band failure does not ship, and
   band violations are exempt from the count thresholds that otherwise
   suppress low-frequency work.
2. **owner-hit** — the operator hits it in the shipped configuration.
   Derived from `CLAUDE.md`'s second gating question, "is this visible with
   the shipped config?" — broken-in-reality outranks unclean-in-design.
3. **silent** — a wrong answer arrives with no signal. Derived from
   `CLAUDE.md`'s third gating question, "does the repair destroy the
   evidence?" — a failure with no signal leaves nothing to repair from, so
   nothing turns red and it survives indefinitely unless someone goes
   looking for it.
4. **blocks-others** — the absence of a mechanism stalls other sessions. One
   defect here costs N people's time, not one. **This axis is the lead's own
   addition** — it has no charter or `CLAUDE.md` basis.
5. **thin areas** — Retrieval and Evaluation, which `CLAUDE.md` names as the
   areas where new work is most valuable. (`CLAUDE.md`, not the charter.)

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
- `no-axis` — judged, and none of the priority axes applies. It exists so
  that an issue with no axis label means "not yet judged" rather than
  "judged and found unimportant" — without it those two states are the
  same absence, and a later reader cannot tell whether the backlog was
  triaged or merely untouched.
- `blocked:external` — needs owner judgement or an upstream dependency. An
  open issue without it is pickable by any session. This label tells other
  sessions not to touch the issue, so a stale one makes real work invisible:
  #4364 carried the label after its own hold had been lifted and after
  another session had already started the work — the label was still
  telling readers to stay away from work already in progress.

  **A block label is itself a claim, and it is checked by nobody.** It
  asserts that a named party outside the team must act first. Re-derive it
  the way §3 re-derives an issue's claim — from what the thread actually
  asks, not from the label's presence. An audit of all 33 on 2026-08-23
  found the label wrong on four, in two distinct ways:

  - **Stale** — the blocking ask had been withdrawn (#3616).
  - **Contradicted by the thread it labels** — #4478's own comment says, in
    the same breath, 「owner の返答を待つ列には入れていません」 and
    「`blocked:external` も そのまま ── 外しません」. The label was
    defensible (it waits on #4476, which does wait on the owner) but it read
    to everyone else as an unanswered question addressed to the owner. What
    was missing was not a label but a title saying what the issue waits on.
  - **Wrong when applied** — #4573 (`severity:high`, the owner's own machine
    unable to start reyn) was labelled "owner 判断待ち" for nine days. Its
    open question was which of two conflicting in-repo precedents to
    generalise, which the architect settles; and either answer removes the
    crash, so no answer had to come from the owner at all. #1811 carried
    `owner:decide` for sixty-five days — the longest-open issue in the
    backlog — while a scan of all five of its comments found zero questions
    addressed to the owner.

  The lead applies this label, so the lead is the party who has to
  re-derive it. Both wrong-when-applied cases above were the lead's own
  labels, and in both the party being waited on was never asked anything.

A new issue gets its axis label(s) when it is filed, not later. Deferring
the label is how a backlog ends up with 35 issues at equal, unlabeled
weight.

## 5b. Naming a target says how you checked it exists

A brief or a ruling that names a concrete target — an API, a config knob, a
path, an event name, a PR — should carry one line saying **how that name was
checked**: the command that was run, or the words "unverified". Naming is
cheap to write and expensive to be wrong about: the writer feels specific,
while the reader who cannot find the name has to stop and ask.

Four on 2026-08-23, in one night:

- A brief pointed a reviewer at a PR that had merged 29 hours earlier, naming
  a head that was not even the final one.
- An issue named `agent_directory_identity` as the thing to measure; it
  exists, but the assignee's `import reyn` resolved to their own clone rather
  than the worktree they had checked out, so it was absent where they looked.
- A ruling built a lattice-meet whose left operand was a knob named
  `messages`. **No knob by that name exists** — the ruling's author had been
  thinking of "the conversation body" as one knob when it is three. Had the
  implementer not asked, an implementation against a non-existent knob would
  have been written.
- A brief named a test file that was not in the assignee's tree, because
  their clone was behind `main`.

All four were caught by the recipient asking before building, which is the
downstream defence and works. This rule is the upstream half: closing it
removes a round trip rather than surviving one.

Note the shape it shares with the B-note rule in `pr-workflow.md`: both ask
the writer to say **how they know**, because the reader cannot recover it.
No mechanism enforces this either — whether a name was checked is semantic,
and a gate cannot see it.

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
