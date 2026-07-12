# Proposal 0059 — Hook-Event Redesign (Event Bus / reactivity substrate)

**Status:** OWNER-RATIFIED (foundational decisions); design spec for phased dispatch. **Origin:** external-AI draft (v0.1, reyn 実装 未参照) → architect が実コードで全 reyn-claim を verify・reyn 実態へ整合 (v0.2) + fable5 review-pass、owner と設計討議, 2026-07-12.
**Scope:** reyn の **hook-event(reactivity)層のみ** — CLAUDE.md の 3-event 区別における hook-event(lifecycle + external reactivity trigger)。**audit-event (P6, `.reyn/events`) と WAL-event (recovery) は本 proposal の対象外・置換しない**(§0 で境界固定)。
**Language note:** owner との設計討議言語(日本語)で起票。既存 proposal は英語 primary ゆえ、必要なら docs-maintainer が英語 mirror を配置。

> **本文について**: 以下は v0.2 spec 本体。各節冒頭の `[reconcile]`(v0.1→reyn 実態の調整根拠, file:line)と `[review-pass]`(fable5 レビュー findings)は、外部 draft からの逸脱理由・レビュー判断を追える設計記録として意図的に残す。

---

# Reyn Event Bus / Hook Spec v0.2 — reyn-aligned

> **v0.1 → v0.2 の位置づけ**: v0.1 は外部AIが reyn 実装を見ずに起草した優れた骨格。v0.2 は architect が
> **実コードで全 reyn-claim を verify し**、(a) reyn の既存 hook-event subsystem の実態に整合させ、
> (b) 既存 capability を regress させず、(c) genuinely-new な追加(Schema Registry / EventPattern /
> Ingress 統一 / Composer / LLM-emit)を reyn の 8-lens + cross-cutting band で評価し phasing した最終系。
> 各節冒頭に **[reconcile]** で v0.1 からの調整と根拠(file:line)を記す。current-state-first —
> 「既に在るものは再発明せず、実態から ideal を導く」。

> **決定事項(owner ratified, 2026-07-12)** — 以下は owner 裁定済。本 spec は これに沿って書かれている:
> 1. **recovery 方針は初版 best-effort、将来 WAL-backed の余地を残す**。Composer pending(§5 Q-reyn-1)/
>    loop-valve counter persist(§10 Q-reyn-3)は共に v1 で best-effort、ただし後付けで WAL-backable な
>    **recovery seam を設計に残す**(将来リストに明示載せ、忘却しない)。
> 2. **pre/post_tool_use は v1 に入れないが将来追加の可能性が高い**(§2 論点A)。∴ namespace と dispatch seam を
>    「後で tool-level 点を restructuring なしに足せる」形で設計する(future-extensible seam)。
> 3. **phasing 承認、まず土台を固める(refactor 含む)**。Phase 1-3(typed Event+Schema / Ingress 統一 /
>    EventPattern)= foundation を先に、既存を型付け・整理(no-debt リファクタ)。Phase 4-5(Bus+Composer /
>    LLM-emit)は土台の上に後続。
>
> **review-pass(fable5, 2026-07-12)反映済**: §1 命名規律(`HookEvent`/`emit_hook_event`、P6 との code 衝突回避)/
> §2 bare-name alias 恒久サポート(既存 config 無修正)+ pipeline_start/end 将来 point / §3.3 Bus scope = per-Session(v1)/
> §4 Schema Registry 2 層 split(builtin=code-shipped+CI sync gate、operator 拡張=OUT-set — 初稿の IN/OUT 矛盾を修正)/
> §5 composer_fired/dropped P6 可視化 + Backpressure v1 除外 / §8.4 横断セキュリティ(context_safe field 宣言、
> argv template 禁止、LLM 自己覚醒 loop の valve backstop + Tier-2 pin、hooks_add 境界の新 kind 拡張)。

---

## 0. 目的とスコープ

**[reconcile — 最重要: 3-event 区別]** reyn には名前が "event" の概念が **3 つ**あり、CLAUDE.md が明示的に区別している。本仕様が扱うのは **hook-event(reactivity 基盤)だけ** であり、他 2 つを置換・統合しない:

| event の種類 | 実体 | 本仕様との関係 |
|---|---|---|
| **audit-event (P6)** | `.reyn/events/<run_id>.jsonl`(`EventStore`、append-only 監査証跡、`reyn events` replay) | **本仕様の対象外**。hook 発火は P6 に `hook_shell_executed`/`hook_push_fired` を*記録する*が、hook-event stream は P6 log そのものではない。 |
| **WAL-event** | `.reyn/state/wal.jsonl`(crash-recovery substrate) | **本仕様の対象外**。hook の*効果*(inbox push / staged context / config write)は WAL-backed だが、hook-event 自体は WAL に流さない。 |
| **hook-event (reactivity)** | lifecycle + external-reactivity trigger。現状は `HookDispatcher` の awaited dispatch(下記) | **本仕様の対象**。v0.1 の "Event / Bus" はこの層。 |

∴ 本仕様のタイトルは「Event Bus」だが、正確には **"Hook-Event reactivity substrate"**。`Event.kind` の stream ≠ P6 audit log ≠ WAL。この境界を最初に固定し、「なぜ hook-event を P6 subscriber で実装しないのか」(subscriber は await 不可、shell hook が待てない — 現行 `HookDispatcher` の設計理由、`hooks/dispatcher.py`)を都度再導出しなくて済むようにする。

**統一される範囲**(v0.1 の一文を継承・補強): 統一するのは *記述インターフェース*(`Event` 型 + `EventPattern` match 構文 + Schema Registry)であって *実行モデル* ではない。reyn の現行 Sync 実行(`HookDispatcher` awaited chain)と、v0.2 で新設を検討する Async 観測層(Composer 用)は目的が異なるため意図的に分離する。

---

## 1. Event 型

**[reconcile]** reyn は現状 **typed な hook-event を持たない** — 発火時の payload は各 call-site で inline に組む ad-hoc な `template_vars: dict`(`hooks/dispatcher.py::dispatch(point, template_vars)`)。shape の契約は docstring と call-site literal にしか無く、中央に「`turn_end` はどの field を持つか」を列挙する場所が無い(= 現行の thin/loose seam)。∴ v0.1 の typed `Event` + Schema Registry は **reyn の実 gap を埋める正の追加**。採用する。ただし reyn の実フィールドに合わせる:

```
Event {
  id: EventId
  kind: string            // namespace付き、2章。現行の point 文字列を包含・一般化
  source: Builtin | McpServer(server_id) | Webhook(provider) | Fs(watch_root) | Cron(job) | Llm(session_id)
  payload: JSON            // schema は kind ごとに load-time 固定(4章)。現行 template_vars を typed 化
  chain_id: EventId?       // [reconcile] v0.1 の causality を reyn の既存 chain_id に mapping(下記)
  emitted_at: Timestamp
}
```

- **[reconcile] causality → chain_id**: v0.1 の `causality: EventId?`(reyn-broker 由来)は、reyn が既に持つ **`chain_id`**(turn_start/turn_end の template_vars に存在、`runtime/session.py:4427-4431/6552-6558`)に対応させる。新フィールドを発明せず既存の因果チェーン識別子を再利用。Composer の `Seq` 順序 / audit 相関に使う。
- `source` に **Fs / Cron** を追加(v0.1 は Mcp/Webhook/Llm/Builtin のみ) — reyn の 4 external source(mcp/file/cron/webhook)を全て表現するため(§6)。
- **[review-pass] code 実装上の型名は `HookEvent`(bare `Event` 禁止)**: reyn の code には既に `core/events`(P6 audit、`ctx.events.emit`)が存在する。型 `Event` / module `events` / op kind `emit` を新設すると **3-event 区別が code 識別子レベルで崩壊**する(CLAUDE.md「bare "event" を書かない」規律の code への適用)。∴ 型は `HookEvent`、module は `reyn/hooks/` 配下、LLM 発行 op kind は `emit_hook_event`(§8)。spec 文中の `Event` は略記であり、実装名は常に `HookEvent`。

---

## 2. Kind Namespace

**[reconcile]** v0.1 の namespace は健全。reyn の実 point 名に合わせて具体化:

| prefix | 発行元 | reyn の実 point(現状) |
|---|---|---|
| `builtin:lifecycle:*` | Reyn本体 | `session_start/end`, `turn_start/end`, `task_start/end`(6点、`hooks/schema.py` ALLOWED_HOOK_POINTS) |
| `builtin:external:*` | Reyn本体(external ingress) | `mcp_resource_updated`, `file_changed`, `cron_fired`, `webhook_received`(4点) |
| `mcp:<server_id>:*` | MCP server | プロトコル標準 notification のみ(6章) |
| `webhook:<provider>:*` | Webhook Ingress Adapter | provider別schema(6章) |
| `webhook:unknown:*` | 未対応provider | opaque fallback |
| `llm:<session_id>:<predefined_name>` | LLM発行event | ホワイトリスト制(8章)。**net-new**(reyn に emit_event tool は現状無い) |
| `composed:<name>` | Composer合成結果 | **net-new**(reyn に event 合成は無い、5章) |

**[reconcile — 論点 A: pre/post_tool_use]** v0.1 は Sync 対象に `builtin:pre_tool_use`/`builtin:post_tool_use` を置くが、**reyn に tool-level hook-point は存在しない**(現状は task-level の task_start/end。tool 実行は task より細粒度)。concept doc の "Deferred" は agent/phase-level hook を「未実装」と明記。∴:
- **task_start/end は保持**(reyn の実点、task Control-IR op に紐付き、`_create`/`_update_status`/`_abort` で必ず対の end を保証、`core/op_runtime/task.py`)。v0.1 が task を落として tool を足すのは reyn 実態と不整合ゆえ **不採用(task は残す)**。
- **pre/post_tool_use は v1 非採用・将来追加濃厚(owner 裁定済)** → **future-extensible seam を今の設計に残す**:
  - namespace を `builtin:lifecycle:*` に固定枚数で埋め込まず、point を **Schema Registry driven の open set** にする(§4 の schema に point を足せば dispatch 対象になる)。∴ 将来 `builtin:lifecycle:pre_tool_use`/`post_tool_use` を schema + call-site 追加だけで足せ、Event/EventPattern/Composer 側は無修正。
  - `HookDispatcher.dispatch(point, ...)` は既に point 文字列 driven(enum hardcode でない、`hooks/registry.py::hooks_for(point)`)ゆえ、tool-level 点の追加は **router loop の tool 実行前後に 2 dispatch 呼びを足すだけ**で済む構造。v1 ではその呼びを入れないが、dispatch interface を将来の tool 点でも使える汎用のまま保つ(現状既にそう)。
  - 追加時は task-level の *置換でなく additive*。Sync 保証(pre_tool_use が tool 実行を待たせる = veto でなく「必ずこのタイミング」保証)のコストは追加 PR で weigh。

Namespace 跨ぎの spoofing 不可は 8 章で担保(`llm:*` が `builtin:*` を騙れない)。

**[review-pass — canonical 名と config 互換]** 既存の 4-layer hooks config(startup/runtime/per-agent/per-session)は bare 名(`turn_end`, `mcp_resource_updated` 等、`hooks/schema.py:30-31` ALLOWED_HOOK_POINTS)を使っている。namespaced 名の導入で既存 config を壊さない:
- **builtin 10 点の bare 名は canonical short-form alias として恒久サポート**(`on: turn_end` ≡ `on: builtin:lifecycle:turn_end`)。曖昧性なし(bare 名は builtin 10 点でのみ有効、他 namespace は必ず full form)。
- 新 namespace(`mcp:*`/`webhook:*`/`llm:*`/`composed:*`)は full form のみ。
- ∴ **既存 hooks.yaml は無修正で動き続ける**(migration 不要)。loader は alias を load 時に canonical へ正規化。

**[review-pass — 将来 point 候補の追記: pipeline_start/end]** v0.1 §5.4 の Count 例は `builtin:pipeline_end` を参照するが、**この point は reyn に存在しない**(pipeline 完了は `pipeline_result` inbox message として届くのみ)。かつ現状は非対称: `pipeline_launch`(hook の action)はあるのに pipeline 完了に反応する hook-point が無い。∴ `pipeline_start`/`pipeline_end` を **pre/post_tool_use と同じ「将来 point」枠**に置く(§11 将来リスト)。Phase 1 の schema-driven open set 設計により、追加は schema + call-site のみ。Count 例は当面、実在する kind で書き直すこと。

---

## 3. 実行モデル: Sync / Async

**[reconcile — 最重要の実態整合]** reyn の現行実装は **v0.1 の "Sync" に相当する層だけを持ち、"Async Bus" は持たない**。

### 3.1 Sync(= reyn の現行 `HookDispatcher`、そのまま)

reyn の `HookDispatcher`(`hooks/dispatcher.py:58-350`)= v0.1 の Sync 経路の実体:
- **awaited first-class dispatch**(EventLog subscriber ではない — subscriber は sync-inline で await 不可、shell hook が process 終了を待てない。この設計理由は concept doc §Awaited-dispatch に明記)。
- 各 lifecycle/external point で `await dispatcher.dispatch(point, template_vars)`。登録順の直列、per-hook `try/except`(1 hook 失敗が lifecycle point を止めない)。
- **[reconcile] 対象 point は reyn の 6 lifecycle + 4 external の全 10 点**(v0.1 の 6 種限定ではない)。reyn では external point も同じ `HookDispatcher.dispatch` に収束する(§6)。
- **veto/allow は持たない**(v0.1 の「control-flow 機能なし、時間的保証こそ価値」= reyn の実態と一致。現行も hook は context inject / self-continuation / side-effect / pipeline launch のみで、tool を止める・書き換える機能は意図的に無い — concept doc "transform-hooks は非サポート")。

**[reconcile] Action は v0.1 の `SendMessage | ExecShell` より広い** — reyn の実 capability を regress させないため、4 scheme を保持(§下記の capability 節)。

### 3.2 Async(Bus broadcast) — **net-new、Composer の前提**

**[reconcile]** reyn に pub/sub broadcast の Bus は**現状無い**。v0.1 の Async Bus は **Composer(§5)を成立させるための新層**。単独では価値が薄く、Composer とセットで導入判断する(§11 phasing)。導入する場合の設計:
- 同一 kind の event を Sync dispatch とは独立に Bus にも broadcast(相互排他でない — 同じ `builtin:external:mcp_resource_updated` を同期 side-effect 用に Sync 登録しつつ観測用に Bus 購読可)。
- Bus 上は pub/sub broadcast のみ、consume 概念なし、全 subscriber(hook・Composer)が同 instance を同時観測。
- **[reconcile — band 整合]**: Bus は reactivity 層であり、P6 audit log(既存の EventLog subscriber 経路 = console render/analytics 用、`.reyn/events`)とは別。Bus に流すのは hook-event であって audit-event ではない。混同禁止(§0)。

**Sync 現行 / Async 新設の関係**: reyn の今は Sync のみで動く。Async Bus + Composer は **相関・合成という現状の thin area** を埋める追加であり、Sync の happy-path(hook 無し時 byte-identical、`HookRegistry.hooks_for()` 空 →無処理)を変えない。

### 3.3 Bus の scope — **per-Session(v1 確定)** [review-pass 追加]

v0.1 は Bus の scope(1 session 内か、多 session を抱える server process 全体か)を定義していない — これは根本のアーキ判断:
- **v1 = per-Session**。現行 `HookDispatcher` の locality(Session ごとに構築、DI closure が Session method に束縛)と一致し、session 間 isolation が構造的に保たれる(他 session の event を観測できない = spoofing/漏洩の新面を開かない)。`llm:<session_id>:*` namespace とも自然に整合。
- external source(cron/webhook)は現行通り **target Session を resolve してからその session の Bus に投入**(Ingress Adapter の責務、§6)。
- **cross-session の event 観測/合成は v1 非対象**(将来: broker 級の設計が要る — 到着順 vs causal 順の補正、session 間の信頼境界。v0.1 §10 の Seq 分散問題はこの将来項に属する)。既存の cross-session **push**(hook action が別 session の inbox へ)はそのまま(action の宛先であって event の観測ではない)。

---

## 4. Schema Registry

**[reconcile]** reyn の実 gap(typed event 不在)を埋める **正の追加、採用**。reyn の実 point の template_vars を schema 化(現状 docstring/call-site literal にしか無い shape を中央化):

```yaml
# builtin schema は code-shipped(下記 [review-pass] 参照)。この YAML 表現は仕様提示用のイメージ。
schemas:
  builtin:lifecycle:session_start: { agent_name: string }
  builtin:lifecycle:session_end:   { agent_name: string }
  builtin:lifecycle:turn_start:    { agent_name: string, kind: string, chain_id: string }
  builtin:lifecycle:turn_end:      { agent_name: string, chain_id: string, user_text: string }
  builtin:lifecycle:task_start:    { task_id: string, name: string, assignee: string }
  builtin:lifecycle:task_end:      { task_id: string, status: string }   # "done" | "aborted"
  builtin:external:mcp_resource_updated: { server: string, uri: string, agent_name: string, resync: bool }  # [correction 2026-07-12] agent_name は Phase 1 discovery(live grep, message_handler.py:220-226)が捕捉。初稿 schema の漏れを修正 — impl の code-shipped schema が authority
  builtin:external:file_changed:         { path: string, event_type: string }  # created|modified|deleted
  builtin:external:cron_fired:           { job_name: string, to: string }
  builtin:external:webhook_received:     { transport: string, sender: string }  # 生 body は決して含めない
```

- 上表は subagent の code-map(§2 の call-site 実測)から起こした reyn の実フィールド。実装時は各 call-site をこの schema に合わせて typed 化(現状の ad-hoc dict を廃し、typo 耐性 + EventPattern 静的検証を得る)。
- `composed:*` の schema は Composer 定義(5章)の `emit` から自動導出(v0.1 通り)。
- `mcp:*`/`webhook:*` は 6 章。
- **[review-pass — Registry は 2 層 split(v0.2 初稿の内部矛盾を修正)]**: 初稿は「OUT-set」と書きつつパスを `.reyn/config/`(= IN-set、`config/loader.py:590`)にしていた。修正と同時に根本を正す — **builtin schema を operator ファイルに置くこと自体が誤り**(operator が編集すると code call-site と drift し、静的検証が嘘になる)。∴ Registry は 2 層に分ける:
  1. **builtin 層 = code-shipped**(typed `HookEvent` payload dataclass / TypedDict から導出、reyn 本体と同時に versioning)。operator は編集不可。**CI sync gate**: 「全 dispatch call-site の組む payload == shipped schema」を CI で assert(CLAUDE.md の `OP_KIND_MODEL_MAP` ↔ `control-ir.md` 同期 hard rule と同型の registry↔実体同期規律。Phase 1 の byte-identical 検証がそのまま初回 gate になる)。schema 進化は additive-only(optional field 追加は可、rename/削除は breaking)。
  2. **operator 拡張層 = OUT-set ファイル**(`reyn.yaml` の `event_schemas:` block、restart-only・trusted): webhook provider schema(§6.2)と llm whitelist(§8)のみ。IN-set には置かない(schema が実行時に動くと v0.1 §8.2 の load-time 検証前提が崩れる)。

---

## 5. Composer(イベント合成) — net-new

**[reconcile]** reyn に event 合成/相関は**無い**。これは v0.1 最大の新価値であり、reyn の **honest thin area(reactivity は現状 per-point、cross-event 相関なし)** を埋める。ただし stateful buffering(QueuePolicy)を伴う複雑度が高いゆえ、**Bus(§3.2)とセットで phasing 判断**(§11)。

v0.1 の §5 全体(op: All/Any/Seq/Window/Debounce/CorrelateBy/Count、QueuePolicy、Count 新設、循環検査 DAG)は設計として健全 — reyn 側の調整点のみ:
- **[reconcile] band 整合 — recovery 方針(owner 裁定済: 初版 best-effort、将来 WAL-backed)**: Composer の pending state(QueuePolicy queue: All の片側到着待ち、Window buffer、CorrelateBy の key別 pending 等)が crash で失われると「合成 event が発火しない」silent gap になる。**v1 は best-effort**(crash で pending 破棄、再構築なし)と**明示的に文書化**する(silent でなく known-limitation として)。ただし設計は将来 WAL-backable にする:
  - **recovery seam を残す**: Composer の pending を「in-memory `PendingStore` interface の背後」に置き、v1 は `InMemoryPendingStore`(破棄)、将来 `WalBackedPendingStore` を差し替え可能にする(reyn の band が要求する snapshot-backed recovery を後付けできる形)。pending の変異を最初から「append できる event(pending_admitted / pending_evicted / pending_paired の closed-vocab)」として表現しておくと、後の WAL 化が `next_turn_context_staged/cleared`(既存 C ride-along の snapshot 復元、`core/events/state_log.py:72-73`)と同型で済む。
  - WAL 化する時点で CLAUDE.md の **recovery-feature PR gate(truncate-falsify test)** が適用対象(pending set X → truncate → reconstruct → X survives)。v1 の best-effort 版はこの gate 対象外(recovery を主張しないため)だが、docstring で「pending は crash-non-durable」を fail-visible に明記。
- **[reconcile] Sync 由来 event は合成対象外**(v0.1 §5.1 通り、reyn でも正しい): Sync の価値は「その瞬間の同期保証」で、buffering に乗せると壊れる。reyn の task_start/end 等の Sync 点は Composer に流さない(Async Bus 経由の観測コピーのみ合成可)。
- `fold`(Pipeline DSL)との役割分担(v0.1 §5.4)は reyn の実 `fold` と整合 — pipeline 内部の畳み込み(control plane 決定操作)vs Count(pipeline 実行回数という外側メタ観測)。責務混在回避、そのまま採用。※ v0.1 の Count 例が参照する `builtin:pipeline_end` は現 reyn に存在しない点は §2 [review-pass] 参照(将来 point 枠)。
- **[review-pass — Observability band: silent drop 禁止]**: Composer の状態遷移は P6 audit-event で fail-visible にする — **`composer_fired`**(composed emit 時)/ **`composer_dropped`**(QueuePolicy overflow / ttl-evict / 未登録 pipeline 等の skip 時、理由コード付き)。**payload は決して記録しない**(既存 `hook_push_fired` が message body を never 記録するのと同じ規律 — webhook payload の PII を P6 に流さない)。silent な overflow-drop は「合成が発火しない」無音バグの温床であり reyn の fail-visible 規律に反する。
- **[review-pass — QueuePolicy の `Backpressure` は v1 から除外]**: broadcast semantics(全 subscriber が同 instance を同時観測、publisher は待たない)と publisher-blocking backpressure は両立しない — 1 つの Composer queue の背圧が Bus publish を block したら broadcast でない上、external ingress の non-blocking 規律(cron 配送/webhook HTTP 応答を hook が遅延させない、既存不変条件)を壊す。v1 の overflow は `DropOldest | DropNewest | Reject` のみ(既存 ingress queue の drop-newest 規律と整合)+ drop は上記 `composer_dropped` で可視化。将来 Backpressure を入れるなら「subscriber-local lag(publisher never blocks)」として再定義すること。

---

## 6. Ingress Adapter

**[reconcile — reyn の最大の構造 seam を解消する良い redesign target]** subagent が「subsystem 中で最も明確な構造 seam」と flag した点: reyn の 4 external source は **2 つの異なる ingress パターン**で `HookDispatcher.dispatch` に収束している:

| source | process locality | 現行 bridge |
|---|---|---|
| `mcp_resource_updated` | in-process(MCP receive-loop task) | bounded `asyncio.Queue` + drain task(`mcp/connection_service.py:444-487`) |
| `file_changed` | in-process cross-thread(watchdog OS thread) | `call_soon_threadsafe` + bounded Queue(`runtime/fs_watcher.py:237-296`) |
| `cron_fired` | **out-of-process**(web-server cron runner) | `resolve_cron_session` → `Session.dispatch_external_event` + `fire_and_forget`(`runtime/cron/routing.py`) |
| `webhook_received` | **out-of-process**(webhook gateway) | 同上(`runtime/webhook_routing.py`) |

∴ v0.1 の統一 Ingress Adapter は reyn の実 seam を綺麗にする — **ただし v0.1 は MCP/Webhook しか挙げていない**。最終系は reyn の 4 source 全てを Adapter として位置づける:
- `[外部 protocol/signal] --raw--> [Ingress Adapter] --Event--> [Sync dispatch + (Async Bus)]`
- **6.1 MCP Adapter**: v0.1 通り(標準 notification のみ、独自 capability 拡張しない)。reyn の現行方針と一致(reyn は MCP 標準 `resources/updated` を受け、独自方言を作らない)。
- **6.2 Webhook Adapter**: v0.1 通り(provider別 schema、unknown は opaque、署名検証はこの層)。reyn の現行 gateway(`gateway/api.py::push_to_agent` = 全 webhook plugin の単一 stable ingress)に対応。**[reconcile] reyn の既存不変条件を保持**: webhook の template_vars は routing metadata(`transport`/`sender`)のみ、**生 request body は never**(token/PII 混入防止、concept doc + `runtime/webhook_routing.py`)。
- **6.3 Fs Adapter [reconcile 追加]**: watchdog → `file_changed`。reyn の実態: OUT-set 宣言のみ(`fs_watch.paths`、restart-only、agent が広げる op/tool 無し — sandbox policy と同クラスの concern)。debounce per-path。Adapter として位置づけるが「op で widen 不可」の security 不変条件を保持。
- **6.4 Cron Adapter [reconcile 追加]**: message-based cron job → `cron_fired`。**[reconcile] cron は "外部 protocol ingress" ではなく内部 scheduler** ゆえ Adapter 抽象に無理に押し込まず、「internal scheduler source」として分類(v0.1 の 6.3 拡張性の枠)。out-of-process ゆえ target Session を `AgentRegistry` から resolve する現行パターンを保持。
- **[reconcile] 統一の要点**: 2 パターン(in-process bridge closure vs out-of-process resolve+fire)を **1 つの Ingress Adapter interface** に収束させるのが redesign の実利。Adapter は `(raw signal) -> Event` の純変換 + `deliver(Event)`(Session resolve を含む)を担い、Sync dispatch / Async Bus は Adapter の内部を意識しない。out-of-process の Session resolve は Adapter 実装の責務に閉じる。

---

## 7. Sync hook の fact 化について(非採用)

**[reconcile]** v0.1 §7 の判断(Sync hook 実行後に fact-event として Bus へ流す二段構造は不要)は reyn でも正しい。§3.2 通り同一 kind を Sync/Async 両方に登録できるため変換層不要。reyn の現行も Sync dispatch と(将来の)Bus broadcast を別レイヤーにできる。そのまま採用。

---

## 8. LLM発行Event — net-new

**[reconcile]** reyn に `emit_event` tool は**現状無い**。v0.1 §8 は net-new 追加。security 設計(namespace spoofing 防止、define 禁止・emit のみ許可、whitelist 事前宣言、load-time schema 固定)は **band の Security lens に完全整合** — 採用可、ただし priority は §11 phasing。
- **[reconcile] reyn の tool 実装に載せる**: v0.1 の「emit_event は特別 primitive でなく通常 tool 呼び出し」= reyn の Control-IR op として実装。**op kind は `emit_hook_event`**(§1 の命名規律: bare `emit`/`emit_event` は P6 の `ctx.events.emit` と衝突し 3-event 区別を崩す)。既存の permission-gate / P6 audit / (将来の)pre/post_tool_use が無修正で適用。**新 op kind ゆえ CLAUDE.md hard rule 適用: `OP_KIND_MODEL_MAP` 追加 + `control-ir.md` 節を同一 PR で**。
- **[reconcile] namespace 固定**: `llm:<session_id>:<predefined_name>`。reyn の既存 `.reyn/config/hooks.yaml`(IN-set)か event-schemas.yaml(OUT-set)で whitelist 宣言。**schema 固定ゆえ OUT-set 推奨**(§4 と同理由 — LLM 出力で schema が動かない)。
- v0.1 §8.2 の define 禁止理由(load-time 検証前提 / spoofing・prompt-injection リスク)は reyn の Security band そのもの。保持。

### 8.4 横断セキュリティ規律(hook/Composer/Adapter 全体、review-pass 追加)

新 namespace の payload を hook が扱えるようになることで開く 3 つの ingress を、規律で先に閉じる:

1. **untrusted payload → template → LLM context(prompt-injection ingress)**: 現 reyn は `webhook_received` の template_vars を routing metadata のみに制限し **生 body を決して渡さない**(token/PII + injection 防衛)。新 `webhook:<provider>:*` event の payload を `template_push` の message に補間できるようにすると、**この防衛を再び開ける**(provider payload は外部者が author する untrusted text — PR title 1 つで `[hook:name]` message 経由の injection が成立)。同様に `llm:<session_id>:*` の payload は LLM-authored ゆえ、cross-session push の template に補間すると **session 間 injection** になる。規律:
   - **matcher / EventPattern / Composer の match には全 payload field を使ってよい**(制御判断であり LLM context に入らない)。
   - **template 補間(message への render)に使えるのは、schema で `context_safe: true` と明示宣言された field のみ**(default false)。builtin schema の現行 vars(server/uri/path/job_name 等)は context_safe。webhook provider schema の本文系 field と `llm:*` payload は default 非-safe — operator が provider schema 定義で明示 opt-in した場合のみ補間可(責任の所在が config に残る)。
2. **shell argv への template 補間は禁止(v0.1 §9 例 `command: "... {{payload.path}}"` は不採用)**: 現 reyn の規律が正しい — **shell command(argv)は static config 文字列、event データは stdin の JSON でのみ渡す**(`hooks/shell_runner.py`: `backend.run(argv, policy, stdin=json(event_context))`)。argv への payload 補間は command injection そのもの(untrusted な `payload.path` が shell に届く)。この規律を仕様として明文化し、loader が `{{` を含む shell_exec/shell_push を **load-time reject** する。
3. **LLM 自己覚醒 loop(emit_hook_event → Composer → wake:true hook)**: LLM が event を emit → Composer が合成 → wake:true hook が新 turn → その turn で再び emit、という **LLM 経由の循環は §5.5 の静的 DAG 検査では捕まえられない**(閉路が LLM を通るため)。backstop は既存 **loop-valve**(`max_hook_driven_turns` — wake push は全て inbox kind="hook" を通るため、composed/llm 由来の wake も既存 counter が数える。経路の追加実装は不要だが、**この不変条件「全 wake 経路は kind=hook を通る」を Tier-2 test で pin** する)。§11 将来リストの valve-persist(crash 越し保証)がこの経路の増加で更に load-bearing になる。加えて **hooks_add(LLM-op)の autonomy 境界を新 kind に拡張定義**: hooks_add が登録できるのは現行通り template_push のみ、`on:` に指定できる kind は builtin + `composed:*` + 自 session の `llm:*`(他 session の `llm:*`/raw `webhook:*` は operator 層のみ)。
4. **(補)全 template render は既存の Jinja2 `SandboxedEnvironment`**(`hooks/render.py`)を Composer emit template にも適用(新 render surface を非 sandbox にしない)。

---

## 9. 設定ファイル面 — **[reconcile] reyn の実 config 構造に整合**

**[reconcile]** v0.1 は `reyn.hooks.yaml` / `reyn.event-schemas.yaml`(新ファイル)を提案するが、reyn の実 config は異なる。reyn の実構造を使う:

reyn の hook config は **4-layer additive combine**(`runtime/session.py:3707-3749`):
1. **startup**(`reyn.yaml` の `hooks:` key)= OUT-set、boot 時 1 回 capture、re-read しない、trusted・fail-loud。
2. **runtime**(`.reyn/config/hooks.yaml`)= IN-set、hot-reloadable(turn 境界 re-read)、untrusted・try-add。
3. **per-agent**(`.reyn/agents/<name>/hooks.yaml`)。
4. **per-session**(per-session state dir の `hooks.yaml`、#2285)。

∴ 最終系の config 面:
```yaml
# reyn.yaml の hooks: block(OUT-set、startup)/ .reyn/config/hooks.yaml(IN-set、runtime)
# — v0.1 の sync_hooks/async_hooks/composers を reyn の hooks: 傘下に整理
hooks:
  # 現行の scheme をそのまま保持(regress させない):
  - on: session_start
    shell_exec: "reyn-env-init.sh"          # F: sandboxed side-effect(consent-gated)
  - on: turn_end
    template_push: { message: "...", wake: true }   # E: self-continuation(loop-valve bounded)
  - on: builtin:external:mcp_resource_updated
    matcher: { server: "github", uri: "file:///repo/docs/**" }
    pipeline_launch: { name: reindex_docs, input_template: { uri: "{{ uri }}" } }
  # v0.2 新設(導入する場合):
  - on: composed:deploy_approved            # Composer 出力を購読
    shell_exec: "reyn deploy.sh"

composers:                                   # net-new(§5)
  - name: deploy_approved
    op: all
    inputs:
      - { kind: builtin:external:mcp_resource_updated, match: { ... } }
      - { kind: mcp:approval-server:approved }
    policy: { capacity: 10, overflow: reject, ttl: 5m, pairing: fifo }
    emit: { kind: composed:deploy_approved }
```
- **[reconcile] sync/async の区別は entry の `on:` kind で表現**(v0.1 の sync_hooks/async_hooks 分離ブロックは不要 — §3.2 で「同一 kind を両方に登録可」ゆえ、実行モデルは kind と登録先で決まり、config の 2 ブロック分割は冗長)。reyn の単一 `hooks:` list を保持。
- **event-schemas.yaml は OUT-set 専用**(§4/§8、schema は static)。
- reyn の既存 capability(4 scheme / wake / matcher glob / cross-session `session:` / loop-valve / sandbox+consent+allowlist / hooks_add op で template_push のみ追加可の autonomy 境界)を **全て保持**。

---

## 10. Open Questions(v0.1 継承 + reyn 由来の追加)

v0.1 の 4 点(CorrelateBy key 衝突 / Seq の分散順序と causality / Webhook 署名検証共通化 / EventPattern match 文法)は継承。reyn 由来の追加:

- **[Q-reyn-1 — 裁定済]** Composer pending の crash-recovery(§5): **v1 best-effort(pending crash-non-durable を fail-visible に明記)+ `PendingStore` interface で将来 WAL-backable な seam を残す**。WAL 化時に recovery-feature PR gate(truncate-falsify)適用。
- **[Q-reyn-2 — 裁定済]** pre/post_tool_use(§2 論点A): **v1 非採用・将来追加濃厚 → point を Schema-Registry-driven の open set にして future-extensible seam を残す**(dispatch interface は既に point 文字列 driven ゆえ追加は schema+call-site のみ)。
- **[Q-reyn-3 — 裁定済: Q1 と同扱い]** loop-valve counter の persist(§3.1): 現状 `_hook_driven_turns` は in-memory-only(crash で reset = self-continuation の crash 越し穴、`session.py:1279` が自認)。**redesign(Bus/Composer/LLM-emit)が hook-driven turn の生成経路を増やしこの穴を load-bearing にする**ゆえ flag した。裁定 = **v1 best-effort(現状維持)、将来 WAL-backed**(Q1 と一貫)。ただし **将来リストに明示掲載**(下記 §11 末)して忘却しない。
- **[Q-reyn-4]** EventPattern match 文法(kind/source/payload の述語表現力)は Phase 3 で詳細化(v0.1 の未定義項を継承)。

---

## 11. Phasing(owner 承認済 — まず土台を固める)

reyn の現行 Sync 層は完成・稼働中。**Phase 1-3 = 土台(foundation)を先に固める**(既存を型付け・整理する no-debt リファクタ、外形挙動は byte-identical or additive)。Phase 4-5 は土台の上に後続の新 capability。

### 土台(foundation)— 先行、リファクタ主体
- **Phase 1 — Typed Event + Schema Registry(§1/§4)**: 現行の ad-hoc `template_vars: dict` を typed `Event` + `.reyn/config/event-schemas.yaml`(OUT-set)に。**最も低リスク・高価値**(現行の thin seam を埋め、EventPattern 静的検証の前提を作る)。既存動作 byte-identical(値は同じ、型が付くだけ)。**pre/post_tool_use の future-seam(point を schema-driven open set に)をここで作り込む**(将来 tool 点を schema 追加だけで足せる形)。
- **Phase 2 — Ingress Adapter 統一(§6)**: 2 ingress パターン(in-process bridge / out-of-process resolve+fire)を 1 interface に収束。**subsystem 最大の構造 seam を解消**、外形挙動不変。4 source(mcp/file/cron/webhook)を Adapter 化。
- **Phase 3 — EventPattern match 文法(§10 Q-reyn-4)**: 現行 matcher(field→pattern、uri/path glob)を EventPattern に一般化(kind/source/payload 述語)。現行 matcher の後方互換を保つ(空 matcher=always、absent field=never 等)。

### 後続(新 capability)— 土台の上に
- **Phase 4 — Async Bus + Composer(§3.2/§5)**: reactivity の thin area を埋める最大の新機能。**Composer pending は v1 best-effort + `PendingStore` seam(将来 WAL-backable)**。Bus は Sync happy-path を変えない(hook 無し時 byte-identical 維持)。
- **Phase 5 — LLM-emit(§8)**: `emit` Control-IR op + whitelist(OUT-set schema)。Security band 整合済。Composer と組むと LLM-triggered 合成が可能。

各 Phase は独立 PR、既存 capability regress ゼロ(Appendix チェックリスト)、8-lens/band 通過。Phase 1 は既存挙動 byte-identical ゆえ、SP/tool-desc relocation で確立した **byte-identical co-vet 手法(cross-tree diff / baseline-unchanged + strip-falsify)** をそのまま適用できる。

### 将来リスト(明示掲載 — 忘却防止、owner 裁定「best-effort now, WAL-backed later」対象)
foundation 段階では入れないが、redesign が load-bearing にするため将来必ず戻る項目:
1. **Composer pending の WAL-backing**(§5 Q-reyn-1): `WalBackedPendingStore` 差し替え + recovery-feature PR gate(truncate-falsify)。
2. **loop-valve counter(`_hook_driven_turns`)の persist**(§3.1 Q-reyn-3): 現状 in-memory-only(crash で reset = self-continuation の crash 越し穴)。Bus/Composer/LLM-emit が hook-driven turn の生成経路を増やすため、self-continuation の安全 bound を crash 越しに保証する必要が上がる。snapshot-backed 化(既存 `AgentSnapshot` 系と同型)。
3. **pre/post_tool_use point の実追加**(§2 Q-reyn-2): Phase 1 で作った schema-driven seam に schema + router-loop の 2 dispatch call-site を足す。
4. **pipeline_start / pipeline_end point の追加**(§2 review-pass): 現状は pipeline_launch(action)だけあり完了に反応する point が無い非対称。Composer の Count 系 use case(v0.1 §5.4 例)の前提でもある。追加は #3 と同じく schema + call-site のみ(pipeline driver-session の起動/完了 seam に dispatch を足す)。
5. **cross-session の event 観測/合成**(§3.3): v1 は per-Session Bus。session 間 correlate は broker 級の設計(到着順 vs causal 順、信頼境界)とセットで将来。

---

## Appendix: 保持すべき reyn 既存 capability チェックリスト(regress 防止)

redesign が落としてはならない現行機能(v0.1 の簡略 Action モデルが漏らしていたもの):
- [ ] 4 scheme: `template_push` / `shell_exec` / `shell_push`(computed push) / `pipeline_launch`
- [ ] 4 capability: C(context inject, wake:false) / E(self-continuation, wake:true) / F(shell side-effect) / pipeline-launch
- [ ] wake flag + run-loop drain(1 turn = 全 wake:false ride-along + 1 wake:true で新 turn)
- [ ] loop-valve(`max_hook_driven_turns`, human turn で reset, on_limit warn→ask_user→abort)
- [ ] matcher(field→pattern, uri/path glob, absent field は never match, 空 matcher は always fire)
- [ ] cross-session push(`session:` field → 別 session inbox)
- [ ] sandbox(Seatbelt/Landlock/Noop/container, network:false, consent fail-closed)
- [ ] consent + allowlist(intervention bus, `~/.reyn/shell-hooks-allowlist.json`)
- [ ] P6 audit: `hook_shell_executed` / `hook_push_fired`(metadata only, message body は never)
- [ ] `[hook:name]` attribution(history を silent mutate しない、object-identity 保持)
- [ ] crash-recovery: E push は WAL-backed(`inbox_put`)、C ride-along は `next_turn_context_staged/cleared` closed-vocab kind で snapshot 復元
- [ ] 4-layer config combine(startup/runtime/per-agent/per-session)+ IN/OUT-set write-gate
- [ ] hooks_add op は `template_push` のみ追加可(autonomy 境界: F は sandboxed, E は loop-valved, C は benign)
- [ ] hot-reload(turn 境界, 1 turn = 1 config snapshot)
- [ ] hooks-free byte-identical(hook 無し時ゼロ overhead)
