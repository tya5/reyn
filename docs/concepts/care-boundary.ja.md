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

## See also

- [principles.md](principles.md) — P1–P8 (特に P3、P4、P7)
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — レイヤー境界アーキテクチャ
- [llm-as-decision-engine.md](llm-as-decision-engine.md) — LLM を制約する理由、レスキューしない理由
- [events.md](events.md) — post-call の道具としての observability (P6)
