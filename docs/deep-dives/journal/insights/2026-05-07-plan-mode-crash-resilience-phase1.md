---
title: plan-mode crash resilience Phase 1 — audit → ADR → impl → e2e verify の 4 段階で fail-safe + observability を land
discovered: 2026-05-07
session-context: plan-mode が同日 dogfood で healthy verified された (= insight #4) 直後、 crash resilience design wave に進んで Phase 1 (= fail-safe + observability) を ADR-0022 として確定 + 実装 + e2e dogfood verify。 既存 skill resume 系 design (= 10 ADR + 6 source file) の audit を user push back で先行させた点が methodology の核
related-commits:
  - 6b09844  # ADR-0022 (= Phase 1 design)
  - 5f4944a  # Phase 1 impl (= 7 file touch + 10 Tier 2 tests)
  - 2fbf3aa  # plan-mode dogfood findings (= 直前 insight、 healthy verify が前提条件)
  - f4c5df2  # category-only catalog landing (= 同 session 連続性)
  - aab6be2  # G12 envelope fix (= 同 session 連続性)
related-giveup: []
related-memory: [feedback_minimize_speculation, feedback_observe_before_speculate_llm, feedback_verify_reproduce_first]
status: stable
---

# Plan-mode crash resilience Phase 1 — audit → ADR → impl → e2e verify

## TL;DR

plan-mode (= chat router の `plan` tool、 user query を 2-7 sub-step に decompose
する mechanism) を **crash-discoverable** にする Phase 1 を land。

到達 path は **4 phase narrative**:

1. **AUDIT** — plan-mode resume design を「案 B (= per-plan snapshot) が sweet
   spot」 と私が提案 → user push back (= 「子供 skill 起動してる可能性は？」 +
   「過去の phase resume 議論のドキュメントは全部読んだ？」) で軌道修正 → 2 件
   audit 派遣で既存 design (10 ADR + 6 source file) と整合する surface を確定
2. **ADR** (`6b09844`) — ADR-0022 で Phase 1 scope (= fail-safe + observability)
   と explicit non-goals (= step result preservation / mid-step resume / policy
   schema 全部 Phase 2 territory) を declared
3. **IMPL** (`5f4944a`) — 7 file touch (= state_log + agent_snapshot + journal +
   planner + router_loop + session + registry) + 10 Tier 2 tests、 既存 pattern
   (= R-D13 additive field、 ADR-0013 finally clause) 踏襲
4. **E2E DOGFOOD VERIFY** — 実 `reyn chat` subprocess で plan_started → SIGKILL
   → restart → cleanup hook の 4 step、 active_plan_ids persist + plan_aborted +
   user-facing outbox を確認

最も重要な学び: **「過去の design ドキュメントを読んでない state で design 提案
するな」** = Wave A revert wave で確立した「数字に踊らされる」 trap variant 1
の **設計版**。 多 layer 介入 (= envelope > schema > SP) と同型に **多 layer
design** (= ADR > impl > test > dogfood) の段階を踏むのが筋。

## Section 1 — Pre-context (= 直前 insight からの連続)

直前 insight ([plan-mode dogfood findings](2026-05-07-plan-mode-dogfood-findings.md))
で plan-mode は 5 scenarios × N=10 = 50 runs の dogfood verify を完走、 LLM-side
bug は全消滅 (= refuted 0/50)。

ただしそこで verified されたのは **「正常 path で plan が機能する」** だけ。
**crash 時の挙動 (= SIGKILL / generic exception / cancel)** は未検証であり、
対する設計も未存在。

plan-mode は構造的にこう問題:

- `Plan` artifact、 `step_results`、 `_PlanStepHost.captured_text` は全部 **in-
  memory only**
- WAL event は per-step audit log のみで、 **plan lifecycle as a unit** の
  event なし
- `AgentSnapshot` に in-flight plan tracking field なし

= mid-plan crash で **all progress 喪失** + **child skill orphan** + 再 issue 時の
**duplicate spawn** リスク。

これは Reyn の他 primitive (= skill / chain / intervention は全部 PR21 / R-D14 /
R-D12 で crash recovery story 済) と structural inconsistency。

## Section 2 — AUDIT phase (= user push back の重要性)

### 私の初期提案 (= 案 B "sweet spot" framing)

私は最初、 「**per-plan snapshot を導入して step results を memoize、 crash 時に
未完了 step から再開**」 を案 B として提案、 「**sweet spot**」 と framing。

= **数 hour で land する小ぶり design** に見えた。

### user push back 1: orphan + duplicate spawn risk

user 即時 push back:

> 「未完了 step からの再開に対して、 crash 前に子供 skill 起動してる可能性は？」

→ 案 B 単独では **orphan child skill** + **resume 時の duplicate spawn** を
扱えない。

`SkillRegistry` は plan の有無を知らずに skill snapshot を WAL に persist する
ので、 plan が crash しても **child skill だけは auto-resume** する。 親 plan は
消えてるので **skill が reply を「どこにも届けない」** 状態に。

逆に user が同 query を再 issue すると LLM が再 plan、 **同じ child skill を
再 spawn** (= duplicate side effect、 double LLM cost)。

→ user 提案で軌道修正:

> 「fail-safe + fall-back + observability で **段階的アプローチ**」

= 「Phase 1 で先ず crash discoverable にする (= step results は捨てる、 user に
"please retry" 通知)、 Phase 2 で forward replay」 という **段階を踏む** design。

### user push back 2: 「過去のドキュメントは全部読んだ？」

私が Phase 1 surface を draft し始めたところで user 2 度目 push back:

> 「過去の phase resume 議論のドキュメントは全部読んだ？」

→ discipline 立ち戻り。 Reyn は skill resume / chain resume / intervention crash
recovery で **既に 10+ ADR の deep design 蓄積** がある。 plan-mode resume を
ゼロから設計することは **既存設計と乖離する design** を生むリスク。

= 私は **既存設計を読まずに新規 design 提案する** trap に踏み込みかけていた。

### 2 件 audit 派遣

user 提案の discipline で 2 件 sub-task 派遣:

| audit | scope |
|---|---|
| **docs side** | `docs/en/decisions/` の 10+ ADR を全 read。 phase resume / state model / runtime lifecycle / cross-agent discard / parent_run_id の design landscape を要約 |
| **impl side** | `SkillRegistry` / `SnapshotJournal` / `AgentRegistry` / `OSRuntime` の source を全 read。 既存 resume primitive の concrete shape を要約 |

各 audit は **関連実装も事前に読む** ことを mandate (= ドキュメントだけだと
abstraction、 コードが ground truth)。

### audit Section 7-8-9-10 で判明したこと

audit output の Section 7-10 (= plan-mode 特有問題 + 既存流用 surface) で 4 点
明確化:

1. **step が skill spawn する場合の double recovery** — child skill は
   `SkillRegistry` で独立 resume、 plan は資料なし → 二重 recovery 不整合
2. **decomposition 非決定論性** — LLM が plan を再生成する step に純粋関数性なし
3. **phase boundary 不在** — plan step は phase ではない (P1 の transition graph
   を経由しない) ので、 既存 OS の phase resume 機構 (ADR-0002 forward replay) を
   そのまま流用できない
4. **「same primitives, separate runtime entry」 が筋** — `PlanRuntime` を将来
   `OSRuntime` peer として置く形 (= Phase 2 territory)、 ただし Phase 1 では
   inline executor のまま

= **「Phase 1 では既存 primitive (= WAL + AgentSnapshot + SnapshotJournal +
AgentRegistry.restore_all) を流用する surface だけ追加、 Phase 2 で初めて
PlanRuntime を peer として導入する」** という段階分割が筋。

これは audit 無しで案 B 「per-plan snapshot」 をいきなり land していたら、
**Phase 2 の PlanRuntime 設計と乖離する snapshot schema** を作っていた可能性が
高い。

## Section 3 — ADR phase (= `6b09844`)

audit を踏まえた上で、 [ADR-0022](../../en/decisions/0022-plan-mode-crash-fail-safe.md)
を draft + land。

### 採用 alternative

3 案 considered:

- **A. Defer entirely.** Plan-mode を「MVP, retry on crash」 と documented で済
  ます → silent duplicate-spawn は user 報告無しでも既に起きてるので reject
- **B. Full forward-replay (= skill-resume parity).** Per-plan snapshot + analyzer
  + coordinator + `PlanRuntime` → multi-week scope、 `reyn.yaml` policy schema
  change 必要、 child skill との coordination question (= adopt vs cancel) が
  open → **Phase 2 territory** として deferred
- **C. Phase 1 fail-safe + observability only.** WAL events + `active_plan_ids`
  field + restart cleanup + user-facing outbox → **Accepted**

### explicit non-goals (= scope creep 防止)

ADR は **Phase 2 territory に明示 push out** する 4 項目を declared:

- step result preservation (= 4/5 step 完了で crash しても全捨てる)
- mid-step resume (= step が `invoke_skill` mid-execution で crash した場合の
  child cancel + user retry)
- `reyn.yaml` `plan_resume:` policy schema (= Phase 1 は固定 policy "discard")
- `PlanRuntime` を `OSRuntime` peer として導入する (= Phase 1 は inline executor
  のまま)

= 「production-grade」 と「MVP fail-safe」 の境目を明示。

### 既存 ADR との整合

ADR Cross-references 4 件:

- **ADR-0001** (= state model + WAL/snapshot) — plan-mode が新規 participant に。
  Phase 1 では WAL truncation floor は touch しない (= plan は短命なので revisit
  Phase 2)
- **ADR-0002** (= forward-replay resume) — 将来 Phase 2 ADR は ADR-0002 の peer
  として
- **ADR-0013** (= runtime crash lifecycle) — Phase 1 の finally clause は
  ADR-0013 と **同じ exception-aware classification** (= `WorkflowAbortedError`
  → complete、 generic Exception → preserve for cleanup) を **継承**
- **ADR-0018** (= cross-agent discard notify) — R-D14 の `notify_chain_discarded`
  は **deliberate に divergence** (= chains は peer agent が待つ、 plan は end
  user が outbox で待つ、 directly `put_outbox` で通知)

→ Phase 1 は **既存設計と structural にも consistent**、 Phase 2 で初めて
plan-specific runtime / policy schema が登場する。

## Section 4 — IMPL phase (= `5f4944a`)

ADR の Implementation surface section が enumerate した **7 file touch** をそのまま
1 commit で land、 + 1 new test file。

### Touch summary

| file | 何を追加したか | 既存 pattern reuse |
|---|---|---|
| `events/state_log.py` | 3 WAL kinds (= `plan_started/completed/aborted`) を `WAL_EVENT_KINDS` に append | 既存 kind enum に additive |
| `events/agent_snapshot.py` | `active_plan_ids: list[str]` field + 3 apply handlers | **R-D13 `parent_run_id` precedent** = additive field、 SNAPSHOT_VERSION bump せず、 `data.get("active_plan_ids", []) or []` on load |
| `chat/services/snapshot_journal.py` | 3 `record_plan_*` method (= started / completed / aborted)、 各 WAL append + atomic save | 既存 `record_skill_*` の同形 mirror |
| `chat/router_loop.py` | `RouterLoopHost` Protocol に 3 optional plan-lifecycle method | 既存 optional method pattern (= `mcp_*` 等) |
| `chat/session.py` | ChatSession で 3 method を `_journal.record_plan_*` に wire | 既存 wiring pattern |
| `chat/planner.py` | `execute_plan` で `plan_id = uuid4().hex[:8]` allocate、 step loop を try/finally で wrap、 **ADR-0013 と同じ exception-aware classification** | **ADR-0013 finally clause precedent**、 `WorkflowAbortedError` → complete、 generic → preserve |
| `chat/registry.py` | `restore_all` post-replay cleanup pass、 各 agent の non-empty `active_plan_ids` を discover、 `record_plan_aborted` + `OutboxMessage(kind="error")` で user 通知 | 既存 `restore_all` pattern + R-D14 outbox path |

### deliberate divergence (= R-D14 chain notify は流用しない)

ADR が cross-reference で declared した通り、 **R-D14 の `notify_chain_discarded`
path は plan abort では使わない**。

理由: chain は **peer agent が waiter** なので、 cross-agent notify が必要。 一方
plan は **end user が waiter** (= chat outbox 経由で待つ)、 かつ plan-mode は
chat turn の chain_id 内で動く (= plan 自身に独立 chain_id なし)。 → plan abort
は `session.put_outbox(OutboxMessage(kind="error", ...))` で directly user 通知。

これを deliberate divergence として ADR に明記。 **流用したい誘惑** (= R-D14 の
infrastructure を再利用) を抑え、 **異なる waiter shape** (= peer agent vs end
user) を尊重する design。

### Tier 2 tests (= `tests/test_plan_lifecycle_crash.py`)

10 invariant pin:

- normal completion → `plan_started` + `plan_completed` round-trip
- `WorkflowAbortedError` → clean completion (= ADR-0013 と同じ classification)
- external cancel → `active_plan_ids` preserved (= no `plan_completed` 発火)
- host without lifecycle methods → 落ちない (= AttributeError catch path)
- `apply_events` for 3 kinds × idempotency on duplicate `plan_started`
- legacy snapshot file (= `active_plan_ids` field なし) load → default `[]`
- save→load round-trip preserves `active_plan_ids`

= **OS invariant level** (= Tier 2)、 LLM behavior は触らない、 mock 不使用 (=
real `AgentSnapshot` + real `SnapshotJournal`)。

### test count

1269 → 1279 passed (+10)、 0 regression。 4 router LLMReplay fixture は
re-record (= `RouterLoopHost` Protocol に新 method 追加で hash 変動)。

## Section 5 — E2E DOGFOOD VERIFY phase

「test pass」 は OS invariant verify で必要十分ではない。 ADR 不変条件 = 「**実
runtime で SIGKILL → restart → cleanup hook 起動**」 が e2e で動くことの確認が
gate。

実 `reyn chat` subprocess で 4 ステップ verify:

| step | action | observed |
|---|---|---|
| 1 | P4-style multi-source query (= "Read both README.md and CLAUDE.md, then build a side-by-side comparison") を user input | `plan_started` event が WAL に landed |
| 2 | subprocess を `kill -9` で SIGKILL (= finally clause が bypass される best-case adversarial test) | snapshot file の `active_plan_ids=['92df8f79']` persist 確認 |
| 3 | `reyn chat` を restart | `AgentRegistry.restore_all` の cleanup pass が `active_plan_ids` を discover、 `plan_aborted` event を WAL に append、 `active_plan_ids=[]` に clear |
| 4 | restart 後の chat 画面を確認 | user-facing outbox に "A plan-mode reply was interrupted by a previous session crash. Please retry your last message." の error message |

= **ADR-0022 不変条件全部 verified**:

- [x] `plan_started` が WAL に persist
- [x] SIGKILL で finally bypass されても active_plan_ids が persist (= state は
      失わない、 cleanup の hook が起動)
- [x] restart で `plan_aborted` event 発火
- [x] user-facing notification (= silent failure ではなく loud failure)
- [x] cleanup 後 active_plan_ids がクリーンに

## Section 6 — 教訓 (= 既存 4 insights の延長で何が新しく見えたか)

### 6.1 「数字に踊らされる」 trap の **設計版**

[envelope-layer attractor fix](2026-05-07-envelope-layer-attractor-fix.md) と
[category-only catalog landing](2026-05-07-category-only-catalog-landing.md)
で確立したのは **「N=10 で 1/10 を noise と dismiss するな」** という **観測の
trap**。

今回 surfaced した variant 1 は **設計の trap**:

- **「過去の design ドキュメントを読んでない state で新規 design 提案するな」**
- 私は案 B 「per-plan snapshot」 を「sweet spot」 と framing したが、 これは
  Reyn の既存 10+ ADR (= ADR-0001/0002/0013/0017/0018) の design landscape を
  読んでない状態での提案
- user push back (= 「過去のドキュメントは全部読んだ？」) で **discipline 立ち
  戻り**、 audit 派遣で landscape を grasp してから再 draft

= 「rate variance」 → 「design landscape variance」 への **同型 trap pattern**。

### 6.2 多 layer 介入 → 多 layer design

[envelope-layer attractor fix](2026-05-07-envelope-layer-attractor-fix.md) で
確立した **多 layer 介入** (= envelope > schema > SP の介入 layer 階層) と
**同型 structure** が design phase にもある:

| 介入 layer (envelope-layer fix) | design layer (今回) |
|---|---|
| envelope (= LLM call frame の最外殻) | ADR (= scope + non-goals declared) |
| schema (= JSON schema 定義) | impl (= 既存 pattern reuse、 protocol surface 拡張) |
| SP (= system prompt) | test (= Tier 2 invariant pin) |
| (verify) | e2e dogfood (= 実 runtime SIGKILL → restart) |

= **段階を踏む** discipline は介入だけでなく design でも load-bearing。 ADR で
scope を pin すれば impl で bloat しない、 impl で test を書けば regression を
catch、 e2e で初めて runtime の不変条件を verify。

### 6.3 「production-grade」 と「MVP fail-safe」 の境目を明示する

ADR-0022 の **explicit non-goals** section は今回 design の **核**。

- step result preservation **しない** = Phase 2 territory
- mid-step resume **しない** = Phase 2 territory
- `reyn.yaml` policy schema **増やさない** = Phase 2 territory
- `PlanRuntime` peer **作らない** = Phase 2 territory

これら明示しないと、 implement 中に「ついでに step results も保存しよう」 等の
**scope creep** が発生し、 1 commit が multi-week scope に膨張する。

= **「Phase 1 は何を**しない**か」 を declared する規律** が、 段階を踏む design
の essential building block。

### 6.4 deliberate divergence は ADR に書く

R-D14 の `notify_chain_discarded` を流用しなかった件は **「同じ infrastructure を
使いたい誘惑を意図的に抑えた」** 設計判断。

これを ADR に明記しないと、 後の reviewer が「なんで chain notify pattern 流用
してないの？」 と疑問を持って **将来 PR で意図せず合流させる** リスク。

→ **deliberate divergence は ADR の Cross-references section で明示** = future
contributor (= 自分含む) への design intent の伝達。

## Section 7 — References

### Same session insights (= 連続 narrative)

1. [envelope-layer attractor fix + mutation isolation methodology](2026-05-07-envelope-layer-attractor-fix.md)
2. [industry tool discovery patterns survey](2026-05-07-industry-tool-discovery-survey.md)
3. [category-only SP catalog landing — Wave A → revert → G12 fix → retry](2026-05-07-category-only-catalog-landing.md)
4. [plan-mode dogfood — 3 bugs found via LLM context analysis discipline](2026-05-07-plan-mode-dogfood-findings.md)
5. **(this insight)** plan-mode crash resilience Phase 1

### ADRs (= 引用元 + design landscape)

- [ADR-0001 state model + WAL/snapshot](../../en/decisions/0001-agent-state-model.md)
- [ADR-0002 forward-replay resume](../../en/decisions/0002-forward-replay-resume.md)
- [ADR-0013 runtime crash lifecycle](../../en/decisions/0013-runtime-crash-lifecycle.md) — Phase 1 finally pattern の precedent
- [ADR-0017 parent_run_id](../../en/decisions/0017-parent-run-id.md) — R-D13 additive field precedent
- [ADR-0018 cross-agent discard notify](../../en/decisions/0018-cross-agent-discard-notify.md) — Phase 1 が deliberate に divergence する pattern
- [ADR-0022 plan-mode crash resilience Phase 1](../../en/decisions/0022-plan-mode-crash-fail-safe.md) — **本 insight が記録する design**

### Commits

- `2fbf3aa` plan-mode dogfood findings (= 直前 insight、 healthy verify が前提)
- `f4c5df2` category-only catalog landing (= 同 session 連続性)
- `aab6be2` G12 envelope fix (= 同 session 連続性)
- `dc8296f` Wave A trial → `589e50f` revert (= 「数字に踊らされる」 trap の元
  事例)
- `6b09844` ADR-0022 (= Phase 2 of narrative)
- `5f4944a` Phase 1 impl (= Phase 3 of narrative)

### Source files (= Phase 1 で touch した 7 file)

- `src/reyn/events/state_log.py` (= WAL kinds)
- `src/reyn/events/agent_snapshot.py` (= `active_plan_ids` field + apply handlers)
- `src/reyn/chat/services/snapshot_journal.py` (= 3 record_plan_* methods)
- `src/reyn/chat/router_loop.py` (= RouterLoopHost protocol)
- `src/reyn/chat/session.py` (= journal wiring)
- `src/reyn/chat/planner.py` (= execute_plan finally clause + plan_id allocation)
- `src/reyn/chat/registry.py` (= restore_all cleanup pass)
- `tests/test_plan_lifecycle_crash.py` (= 10 Tier 2 invariants、 new file)

### Phase 2 territory (= 別 ADR で扱う、 本 insight の scope 外)

`Plan-Mode Forward Replay` (working title、 future ADR):

- `PlanSnapshot` dataclass (= `step_results` persistence)
- `PlanResumeAnalyzer` / `PlanResumeCoordinator` (= skill-resume primitives mirror)
- `PlanRuntime` as `OSRuntime` peer
- child skill との coordination policy (= adopt vs cancel、 ADR-0003 purity
  taxonomy 適用)
- `reyn.yaml` `plan_resume:` policy schema
- decomposition output を workspace artifact 化 (= P5 invariant 整合)
