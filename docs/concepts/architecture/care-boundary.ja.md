---
type: concept
topic: architecture
audience: [human, agent]
---

# Care boundary — Reyn が care することと care しないこと

## TL;DR

Reyn は LLM が判断する「構造的環境」を整備する。LLM の確率的な出力をレスキューしたりパッチしたりはしない。

## 原則

Reyn におけるすべての設計判断 — schema 制約を追加するか、event を emit するか、新しい Control IR op を公開するか — は、次の 3 区分のいずれかに対応する。

### 1. Reyn が care する (= structural environment、pre-call)

各 LLM call の *前に* OS が構築するもの。LLM が健全な環境で推論できるようにするための整備:

- **Schema および enum 制約。** フィールドが取りうる値が有限であれば、artifact schema の enum として表現する。LLM はその集合の外側を hallucinate できない。OS はそれ以外を拒否する。(歴史的事例: RETRO-H1 では `invoke_skill.name` に live skill 名の enum を追加した。attractor はすぐに消えた。制約が構造的だったから。)
- **Context 提供。** OS は使用可能な skill の flat リストを構築し、system prompt に inject する。LLM は何が存在するかを推測する必要がない。
- **決定論的 work の代行。** input から機械的に derive できるもの — path 計算、file glob、schema validation、format 変換 — は LLM ではなく phase preprocessor が行う。(歴史的事例: G2 の `copy_to_work` phase は当初 LLM-driven だった。LLM は繰り返し write step をスキップした。ロジックを 8 step の preprocessor に移した結果、問題は構造的に不可能になった。`max_act_turns` は 0 に設定された。)
- **Input shape の正規化。** Union artifact 型の解決、OS が計算した path の inject、context frame の組み立て — これらはすべて LLM が見る前に行われる。

これらはオプションの親切機能ではない。なければ Reyn は動かない。

### 2. Reyn が care しない (= behavioral rescue、post-call)

LLM の出力を受け取った *後に* OS が意図的に行わないもの:

- **挙動的失敗に対する retry。** LLM が valid な JSON を返したが、その内容が poor な判断を反映している場合 (誤った phase 選択、低 confidence、推論欠如 等)、OS はサイレントに再 invoke しない。clean failure event を emit し、user に surfacing する。(歴史的事例: G12 の Option B — `empty stop` 時の auto-retry — が提案されたが却下された。Option F が採用された: event を emit し、user に判断を委ねる。)
- **Fallback escalation。** メインモデルが苦戦しているからといって、OS が自動的により強いモデルに切り替えることはしない。モデル選択は設定の関心事であり、ランタイムのレスキュー機構ではない。
- **Attractor 検出 + state machine。** OS は「LLM がループにはまっているようだ」を検出して、次に LLM が何をすべきかを決める corrective state machine で介入することはしない。それは LLM を意思決定ノードから置き換えて OS レベルのヒューリスティックで代替することになり — P3 違反。

この姿勢の理由は LLM の失敗が重要でないからではない:

1. 確率的出力の auto-rescue はそれ自体が確率的。OS は失敗が一時的なものか構造的なものかを知ることができない。
2. OS 内でのレスキューは失敗を user と event log から隠す — システムをデバッグしやすくするどころか難しくする。
3. behavioral rescue ロジックは bloat trap: 新しい失敗モードごとに新しいレスキューアームが必要になり、OS は際限なく成長する。

post-call の失敗に対する正しい道具は observability: 構造化された event を emit し、clean に surfacing し、user が対処する。

### 3. Gray zone (= prompt rule — 注意して扱う)

Prompt rule は構造的に曖昧な位置にある。pre-call ではある (OS が LLM を invoke する前に system prompt に inject する) が、性質は behavioral だ (LLM にスキーマ強制ではなく任意での遵守を求める)。

Gray zone のリスク:

- **累積。** シナリオ別の fix ごとに rule が追加される。rule が積み重なる。数 batch 後には system prompt の Behaviour セクションが bloat し、rule が矛盾し始める。(歴史的事例: B2-H1 と B3-H1 がともに `list → describe → invoke` という同じチェーンを対象に MUST rule を追加した — 1 つの意図を 3 つの rule で重複表現。)
- **過剰 consolidation による regression。** 4 つの rule を 2 段落に統合したことで、weak LLM へのシグナルが弱まった。(歴史的事例: `e90c0f2` で 4 bullet を 2 段落に統合した後の B5-H1 regression。)
- **Weak model による無視。** Weak LLM (例: gemini-2.5-flash-lite) は複数文の段落を個別 MUST bullet より低優先に扱う。schema によって構造的に強制された制約は無視されない。

Prompt rule が真に必要な場合の最適バランス: **個別 bullet × bullet ごとに 1 MUST × wording dedup** — bullet は分けたまま、wording だけ整理し、bullet を段落に統合しない。そして常にまず問う: これはスキーマで表現すべき構造的制約か、それとも prompt でしか表現できない behavioral guideline か?

## 具体例

| 判断 | 区分 | 備考 |
|------|------|------|
| artifact schema の `invoke_skill.name` enum (RETRO-H1) | Structural care | Hallucinated な skill 名が構造的に不可能になった |
| preprocessor が `copy_to_work` の path 解決を担う (G2) | Structural care | LLM の write-skip attractor が構造的に不可能になった |
| OS が flat skill list を構築して context に inject | Structural care | LLM は正確な情報を持つ。推測不要 |
| OS が union artifact を LLM call 前に組み立て | Structural care | Input shape は常に well-formed |
| Option F: `empty_stop` event を emit、clean failure UX | Post-call observe-only | user が失敗を確認。サイレント retry なし |
| Option B: `empty_stop` 時の auto-retry (却下) | Behavioral rescue | 却下 — 失敗を隠す、P3-adjacent、OS bloat |
| Attractor OS state machine (提案後撤回) | Behavioral rescue | 撤回 — OS は LLM が「はまっている」かを確実に知れない |
| batch 迭代での MUST rule 累積 | Gray zone | bloat と cross-scenario interference に注意 |

## なぜこの framing か

### P3: OS は実行を制御する。結果は制御しない

P3 は OS がランタイムエンジンであることを定義する。LLM は意思決定ポリシー。OS が LLM の出力を second-guess してサイレントに修正し始めた瞬間、OS が意思決定ポリシーになる — P3 違反。OS は invalid な出力を拒否することが許される (validation は構造的); より良い出力をサイレントに代入することは許されない。

### predictability over autonomy (制約環境における Reyn の vision)

Reyn は predictability が autonomy より重要な高制約環境向けに設計されている。LLM の失敗をサイレントにレスキューするシステムはデモでは有能に見えるが、production での信頼は難しくなる: OS はいつ介入するのか? どの条件で? event log に何が記録されるのか? 明示的な失敗は観測可能。サイレントなレスキューは観測不可能。

### OS bloat 防止

Behavioral rescue ロジックは複利で増大する。新しい失敗モードごとに新しいレスキューアームが必要になる。empty stop を処理し、stuck attractor を処理し、model degradation を処理し、schema drift を処理する OS は — それぞれ条件付きで — 最初の意思決定エンジンの上に重ねられた第二の意思決定エンジンになる。OS は際限なく成長し、新しいアームごとに新しい失敗モードが生まれる。境界を clean に保つことで OS は linear に保たれる。

## Anti-patterns

### LLM の挙動的失敗に対する auto-retry

```
# Anti-pattern: OS が LLM を「間違っている」と判断してサイレントに retry
if result.is_empty_stop():
    result = await llm.call(context, hints=["try harder"])
    # user は最初の失敗を見ない
```

OS は代わりに structured event (例: `empty_stop`) を emit し、caller に clean failure を返すべきだ。user が retry、escalation、調査のどれをすべきかを判断する。

### Attractor 検出 + corrective state machine

```
# Anti-pattern: OS が同一 phase への連続遷移をカウントして介入
if transition_count[phase] > THRESHOLD:
    next_phase = os_heuristic_pick_recovery_phase(context)
    # OS が意思決定ノードになっている、LLM ではなく
```

phase が attractor になっているなら、fix は構造的であるべきだ: skill graph を見直してループを閉じる (例: phase preprocessor に `max_iterations` guard を追加) — OS にランタイムのエスケープハッチを追加するのではなく。

### Prompt rule の累積

```
# Anti-pattern: 失敗する scenario ごとに MUST rule を追加
MUST call invoke_skill after list_skills.
MUST call invoke_skill or explain after describe_skill.
After list_skills reveals a matching skill, MUST call describe or invoke.
After describe_skill, MUST call invoke_skill if the user asked for Action.
```

各 rule が特定のシナリオを対象にしている。重複し、矛盾し、weak model を混乱させる。fix は各 rule が何の構造的制約の症状かを見つけ — その制約を prompt ではなく schema または graph で強制すること。

## 関連

| Memory file | care boundary との関係 |
|-------------|----------------------|
| `feedback_deterministic_split.md` | structural care の一形態: 決定論的 work を preprocessor に委ねる |
| `feedback_prompt_design.md` | gray zone の典型的 trap: prompt rule bloat と過剰 consolidation |
| `feedback_minimize_speculation.md` | care 設計判断の方法: 1 仮説、1 修正、1 観測 |
| `feedback_observe_before_speculate_llm.md` | post-call observe-only を可能にする observability インフラ |

care boundary はこれら 4 つを統合するメタ原則。

## Downstream tooling — Reyn の上に build されるもの

上記の 3 区分は OS boundary が LLM 挙動に対してどこに位置するかを説明する。もう一つ名指しする価値のある境界がある: Reyn が終わり、その上に build されるエコシステムが始まる場所だ。

### パターン

Reyn は OS 層に一連の raw primitive を公開する:

- **Events log** — 全状態変化を記録した、構造化・機械可読な JSONL ストリーム ([../runtime/events.md](../runtime/events.md) 参照)。
- **WAL および skill snapshot** — crash を生き残る workspace state; P5 の workspace-as-source-of-truth の産物。
- **Cost tracker** — run 単位・skill 単位のトークン数とコスト集計を event として emit。
- **Phase trace** — run ごとに記録された phase 順序、LLM call、Control IR 実行の系列。
- **control_ir results** — phase 実行ごとに event log に書き込まれる op レベルの実行結果。

これらの primitive は、LLM-agent エコシステムが現在活発に build している一連の downstream product にとって十分な基盤だ: conversation analytics platform、durable agent runtime、eval-as-a-service、observability dashboard、agent marketplace。Reyn が substrate を提供し、それらの product が consumer layer となる。

### なぜこれが意図的なものか

P7 は OS コードが skill 固有の文字列を含んではならないと定める。同じロジックが一段上にも適用される: OS はあらゆる隣接 product ニーズを吸収してはならない。吸収された機能はそれぞれ、OS が何かしら skill 固有あるいは consumer 固有のことを知ることを要求し、Reyn を拡張可能にしている抽象を破壊する。

したがって care boundary は上述の LLM-behavior split だけを意味するのではない — 上位 product が自分で build すべきものの下方限界も定義する。Reyn を基盤として使えるほど小さく保つことが、基盤としての有用性を守る。analytics platform であり deployment runtime であり eval service でもある OS は、あらゆる場面で skill 固有の知識を必要とし — P7 違反の連鎖を生む。

### Landscape からの具体例

2025-2026 年の HN AI-agent landscape から 2 つの product がこのパターンを例示する。

**Conversation analytics platform (Lenzy AI を一例として)**

Lenzy AI は「product analytics for AI agents」— agent とユーザーの会話を分析して product insight を抽出する。Reyn の primitive として消費するのは events log だ: `workflow_started`、`phase_completed`、`llm_called`、および per-skill 集計は、会話の弧を再構成して analytics を導出するために必要なものをすべて持っている。

Reyn が行うこと: 安定した envelope を持つ、run 単位の構造化 event を emit すること。意図的にスコープ外とすること: それらの event をユーザー・run・skill をまたいで集計し、dashboard、トレンドライン、product insight レポートを生成すること。そのレイヤーには product 固有の schema 知識が必要だ (この skill にとって「成功した会話」とは何か?) — OS が encode してはならないもの。

**Stateful agent runtime (Agentainer を一例として)**

Agentainer (「Vercel for stateful AI agents」) は durable agent container を persistent state、auto-recovery、proxy routing 付きで zero-DevOps で提供する。消費する Reyn primitive は WAL + skill snapshot + state-dir contract — P5 crash recovery を可能にするのと同じ機構だ。

Reyn が行うこと: crash を生き残る workspace の維持; 最後の一貫した WAL checkpoint から run を resume すること。意図的にスコープ外とすること: zero-DevOps container 管理、HTTP proxy routing、マルチテナント state 分離、インフラ障害モードに合わせた retry policy。これらは deployment layer の関心事であり、agent OS の関心事ではない。

**Eval-as-a-service product**

Reyn が行うこと: phase 単位・skill 単位のテスト実行のために `LLMReplay` と eval framework を提供すること。意図的にスコープ外とすること: hosted eval pipeline、組織横断の benchmark 集計、rubric marketplace。Reyn を消費する eval service は `LLMReplay` を API 経由で駆動する — Reyn が hosting インフラを ship することを要求しない。

**Observability dashboard**

Reyn が行うこと: 安定した envelope (`ts`、`kind`、`phase`、`run_id`、payload) を持つ構造化 JSONL として event を emit すること。意図的にスコープ外とすること: それらの event をクエリ可能な database に保存し、時系列 dashboard をレンダリングし、異常をアラートすること。JSONL 互換の observability tool であれば、Reyn が embedded dashboard を ship することなく log を ingestion できる。

### これが意味する contract

downstream consumer が events log、WAL、state-dir format に依存しているため、それらの format は public API と同等の慎重さで進化させるべきだ。event envelope への破壊的変更 — `kind` のリネーム、`run_id` format の変更、payload フィールドの再構築 — は、その上に build されたすべての analytics・observability integration への破壊的変更となる。

pre-1.0 の安定性注意書きが適用される: これらの contract はまだ frozen されていない。しかし方向性は安定性と明示性に向かっており、churn ではない。追加は安全; 削除とリネームには deprecation window が必要だ。

### contributor へのソフトな境界線

新機能を評価するとき、問う: 「これを提供するために Reyn は skill 固有あるいは consumer 固有のことを知る必要があるか?」

もし yes なら — 「成功した会話」が何を意味するかを OS が理解する必要がある、あるいはどの consumer のためにどの event を集計するかを知る必要がある、あるいはどの deployment 環境にどの retry policy が合うかを知る必要がある — それは downstream layer に属し、OS には属さない。これはニーズの否定ではない; 責任の正しい割り当てだ。OS が primitive を提供し、downstream layer が product を提供する。

もし no なら — OS が特定の skill あるいは consumer について何も知ることなく提供できる汎用の structural 機能なら — OS layer の候補だ。

この問いは P7 をコード境界だけでなく product 境界に適用したものだ。

## See also

- [../architecture/principles.md](../architecture/principles.md) — P1–P8 (特に P3、P4、P7)
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md) — レイヤー境界アーキテクチャ
- [../architecture/llm-as-decision-engine.md](../architecture/llm-as-decision-engine.md) — LLM を制約する理由、レスキューしない理由
- [../runtime/events.md](../runtime/events.md) — post-call の道具としての observability (P6)
- [../architecture/architecture.md](../architecture/architecture.md) — component layer と OS-as-constant モデル
