---
type: contributing
topic: dogfood-discipline
audience: [human, agent]
---

# Dogfood Discipline Guide

Reyn を初めて触る developer / agent 向けの pedagogical リファレンス。 dogfood batch 7–14 で確立した discipline を理解 + 再現するために書かれています。

---

## 1. なぜこの discipline が必要か

### test green と実際の動作の間にあるギャップ

test suite が全 green でも、プロダクトは使えない場合があります。これは Reyn 固有の問題ではなく、普遍的なギャップです。test は特定の contract が成立するかを検証します。 LLM が確率的に意思決定を行うシステム全体が、実際の会話入力に対してどう振る舞うかは、test では判断できません。

Reyn においてこのギャップは 2 つの形で現れます。

**1. LLM ドリブンのワークフローは、ユニットテストで捉えられない確率的失敗モードを持つ。**
OS invariant test は、遷移バリデーターが不正な next-phase を正しく拒否するかを検証します。しかし、実際のシステムプロンプトと実際のユーザーメッセージを与えられた LLM が正しい next-phase を選ぶかは検証しません。後者は Reyn が構築する構造的環境 (schema 制約 / context injection / preprocessor 委譲) と、その環境内での LLM の確率的挙動に依存します。environment が sound かどうかは、end-to-end 実行でしか分かりません。

**2. test fixture の drift は無音の危険。**
手書き fixture でパスする test が、OS が runtime で実際に生成するアーティファクトでは失敗することがあります。fixture が実際の output shape と合わなくなっているからです。これは仮定の話ではなく、batch 9 で実際に観測されています (「wrong layer trap」、セクション 3 原則 6 で詳述)。

つまり: test green は「Reyn が動いている」の必要条件であって、十分条件ではありません。その溝を埋めるのが dogfood です。

### ここで言う dogfood とは

本ドキュメントでの「dogfood」は、**Reyn の stdlib skill を `reyn chat` 経由で実際に実行し、何が起きるかを観測すること**を意味します。 test が予測するものでも、静的解析が示すものでもなく、実際のシステムプロンプト・実際のコンテキスト・実際のアーティファクトフロー上で LLM が何をするか、です。

観測の単位は **scenario**: 特定のコードパスを実行する具体的なユーザーメッセージ。Scenario は **batch** にまとめられます。各 batch の終わりに retrospective を書き、学べる原則を抽出して次の batch の設計に引き継ぎます。

これは構造的な経験主義です。 discipline は、その経験主義を系統的・再現可能・漸進的に有用にするための方法論です。

### Reyn の設計 vision との接続

Reyn は **予測可能性 > 自律性** のために設計されています。予期しない挙動のコストが高いコンテキストへの展開を想定しています (see [Principles P1–P8](../concepts/principles.md))。その vision が意味を持つのは、「予測可能」が実際のワークロードに対して計測される場合のみです。合成的な test fixture に対してではありません。 dogfood はその計測手段です。

---

## 2. iterative loop — 1 batch の構造

各 batch は同じ 5 つのステップ構成に従います。この構造は官僚的なオーバーヘッドではなく、各ステップが他のステップでは代替できない目的を持っています。

### A1: scenario plan 草案

assistant (または batch を回す engineer) が実行する scenario のリストを草案します。各 scenario は以下を指定します。
- 具体的なユーザーメッセージ (test ID ではない)
- 行使するコードパス
- 期待される結果 (binary ではなく確率分布として)

この草案は**仮説の明示的な宣言**です。書くことで、何が起きると期待するかを言語化することを強制します。それにより A4 で、期待と現実のギャップが見える化されます。

期待結果は batch 8 で確立した 4 区分フォーマットを使います。
- **verified**: fix / 機能が prediction 通りに機能した
- **inconclusive**: 観測が曖昧 — scenario が関連 phase に到達しなかった、または複数 sub-step の結果が混在した
- **refuted**: 挙動が prediction に反した — fix が効果なし、または誤った効果
- **blocked**: 前段の bug により観測経路が遮断された — scenario が関連 phase に到達できなかった

「blocked」 の包含が重要です。新規 dogfooder がキャリブレーション初期に最も頻繁に見逃すカテゴリです。複数の blocker がある layered system では、early batch の mid-chain scenario において「blocked」 は最も頻度が高い outcome です。省略すると全 blocked outcome が「inconclusive」 に流れ込み、calibration が歪みます。

### A2: user review (必須 — skip 不可)

実行前に scenario plan を review します。**設計レベルの介入**がコスト投入前に batch を redirect できる最後の機会です。

価値は主にエラー修正ではなく**設計意図の言語化の強制**にあります。ユーザーが「60% verified と予測していますが、test fixture が runtime アーティファクト構造と一致しているか確認しましたか?」と質問することで、コードを実行する前に wrong-layer trap を防げます。

A2 は暗黙の simplicity check が起きる場所でもあります。期待される挙動の説明が難しい場合、それはしばしば基盤となる設計が incoherence を蓄積しているサインです (セクション 3 原則 9)。

### A3: worktree 隔離による並列実行

Scenario は並列 dispatch された sub-agent によって実行されます。各 sub-agent は独立した worktree と独自の `.reyn/` state directory で動作します。この隔離は必須です。

- 並行 scenario 実行間の state collision が構造的に不可能
- 1 scenario の失敗が別の scenario の観測コンテキストを汚染しない
- sub-agent の並列性で、per-scenario fidelity を落とさず wall-clock time を短縮

Reyn では: 各 `sonnet` sub-agent が新鮮な worktree を受け取り、scenario のユーザーメッセージを piped stdin 経由で `reyn chat` に渡して実行します。

Reyn 以外のシステムでは: per-scenario プロセス隔離が等価です。共有 state (モデルキャッシュ / temp ディレクトリ / event ログ) は隔離するか交絡要因として扱う必要があります。

### A4: findings aggregation + user レビュー

全 scenario が完了した後、findings をまとめます。各 finding を severity 別に分類します。
- **CRITICAL**: システム非機能
- **HIGH**: コアユーザーパスがブロックされている
- **MED**: 挙動劣化、ワークアラウンドあり
- **LOW**: cosmetic またはエッジケース

集約結果は fix dispatch 前にユーザーに見せます。これが**「user の感覚チェック」**ステップです。ユーザーが findings summary を読み、観測された挙動が自分のシステムのメンタルモデルと一致するかを確認します。ここでの相違が最も価値のあるシグナルであることが多いです。「それって X の仕組みからして起きるはずがないのでは?」と言うユーザーは、engineer よりも先に wrong-layer symptom を検出している可能性があります (セクション 3 原則 6)。

### A5: bug 分類、fix wave または defer

HIGH / CRITICAL の各 finding は 2 つのトラックのどちらかに入ります。
1. **fix wave**: 確認済み・再現可能な bug を対象とした並列 fix dispatch
2. **defer (giveup tracker)**: 構造的依存 / 設計の曖昧さ / さらなるデータが必要な non-determinism のために、このバッチでは修正できない bug

修正分類の discipline (セクション 3 原則 7) がここで適用されます。すべての fix は、**仕様変更** (ユーザー可視の挙動変更、ユーザーへの通知が必要) か**不具合修正** (documented design の復元、意図する挙動への変化なし) かを明示ラベルします。

### retrospective: 教訓抽出と申し送り

batch の fix wave が完了し、次の retest で verify された後、retrospective を書きます。構成は固定です。
- expected vs actual (A1 plan の予測と実際の比較)
- turning points (batch の方向を変えた予期しない事象)
- 強化または新確立された原則
- 次 batch への申し送り (残課題 / carry-over findings / calibration 調整)

retrospective は batch の**永続的な成果物**です。scenario と finding は運用上の記録です。retrospective は学べる原則が抽出され、次の batch の A1 計画で参照できる形にされる場所です。

---

## 3. 9 原則 framework

これら 9 つの原則は、batch 7–14 の繰り返し観測を通じて確立されました。各原則は 2 段構成で提示します。universal な定式化 (どの LLM ドリブンシステムにも適用可) と、現実的なコンテキストで原則を示す概念例です。

---

### 原則 1: 決定論 / 非決定論操作の分離

**universal 原則。** すべての LLM ドリブン workflow phase は 2 種類の処理に分解できます。input から純関数で derive 可能な処理 (決定論) と、判断 / 選択肢の評価 / 新しいコンテンツ生成が必要な処理 (非決定論) です。 両者を 1 つの LLM act loop に混在させるのが誤りです。

決定論的な処理を LLM に委ねると、LLM はそれを機械的な処理としてではなく判断として扱います。特に weak LLM は、ファイル write / path 計算 / schema validation を、周囲の判断ステップと構造的に区別できないため、スキップしたり誤って実行したりします。インストラクションの問題ではなく、それらの操作が決定論的であるにもかかわらず LLM が判断に最適化されているからです。

設計ルール: **純関数として書けるすべての処理 — file glob / path derivation / list filter / schema validation / format conversion — は phase preprocessor または deterministic op に属し、LLM act loop には属しません。**

**概念例。** 「input ファイルを読んで変換して output ファイルに書く」 phase は判断内容ゼロです。すべての output path は input path から derive 可能。すべての write は変換ルールから derive 可能。この phase が LLM ドリブンの場合、LLM は確率的に write をスキップし、glob を過広にし、read を繰り返します。インストラクションが不明確なのではなく、それらの操作が決定論的で LLM が判断に最適化されているからです。ロジックを preprocessor に移動することで、この失敗モードは構造的に消滅します。 LLM call 数はゼロになり、`max_act_turns: 0` を設定できます。

checklist: (1) output は input から純関数として derive 可能か? yes なら preprocessor 化候補。(2) 実際の判断ステップは何か? 明示的に enumerate する。(3) 非判断ステップが LLM act loop に残っていないか? 取り出す。

See: `feedback_deterministic_split.md`

---

### 原則 2: prompt 設計 — bloat と過剰 consolidation に注意

**universal 原則。** system prompt rule は scenario 別 fix として積み重なります。各 fix は発生した失敗モードを対象にしたルールを追加します。時間とともにルールセットが肥大し、微妙に異なる wording で重複する意図を持つルールが現れます。これが prompt bloat であり、2 つの失敗モードを引き起こします。(a) ルールが矛盾 / 重複してモデルを混乱させる。(b) scenario A のためのルール追加が scenario B の挙動を劣化させる (ルールが過剰特化しているため)。

対策 — 複数ルールをより少ない段落に consolidate する — は逆の失敗モードを生みます。weak LLM は 1 つの MUST を持つ複数文の段落を、各自に MUST を持つ 4 つの個別 bullet よりも低優先度として扱います。 consolidation がシグナルを弱めます。

最適バランス: **ルールごとに個別 bullet × bullet ごとに 1 MUST × bullet 間の wording dedup**。 bullet は分離したまま、各 bullet 内で wording を dedup し、bullet を段落にまとめない。

**概念例。** 同一ワークフロー (list skills → describe → invoke) を対象にした 3 つのルールが 3 つの個別 bullet として積み重なります。これらを 1 段落に consolidate すると regression が起きます。モデルが段落全体を個別の 3 つの MUST シグナルよりも低優先度の 1 ユニットとして扱うからです。fix: 3 つの bullet を維持し、各 bullet 内の共通フレーズを dedup しますが、bullet を段落にまとめません。

audit トリガー: 新しい prompt rule を追加するたびに、既存ルールと意図が重複していないか確認します。 2 つのルールが同じ行動意図を encode している場合、wording を dedup します — ただし個別 bullet として維持します。

See: `feedback_prompt_design.md`

---

### 原則 3: 仮説停止 — 1 仮説 1 修正 1 検証

**universal 原則。** LLM の挙動失敗を診断する際、複数の仮説を 1 つの「comprehensive fix」 にまとめたくなります。考えられる原因をすべて同時に対処すれば確実に解決する、という発想です。このアプローチには 2 つのコストがあります。工数が倍増し、学習が破壊されます。 bundle した fix が効いた場合、どの仮説が正しかったか分かりません。効かなかった場合、複数の fix に投資したにもかかわらず次に試すべきものについてシグナルがゼロです。

discipline: **仮説 1 つを isolate し、それをテストする最小の変更を加え、観測し、判断する**。 その後、次の仮説へ。

**概念例。** Phase が誤った output を生産します。 3 つの仮説: (a) field 命名により LLM が field を認識できない; (b) schema が field を明示的に宣言していない; (c) instruction が field を参照していない。3 つを bundle すると 1 時間かかって 1 bit の情報 (bundle が効いたか否か) を得ます。仮説 (a) を単独でテストすると 5 分です (field を rename して再実行)。効いたなら (b)(c) は不要。 効かなければ (b) へ。総コストは低く、学びは高いです。

順序: 仮説を観測コストが最も低いものから順にテストします。 field rename はコストが低い。 artifact contract 変更を伴う schema 拡張はコストが高い。 cheap なものから先に verify します。

See: `feedback_minimize_speculation.md`

---

### 原則 4: LLM 挙動を疑う前に観測 infra を作る

**universal 原則。** LLM 挙動仮説 — 「モデルがこの field を無視する」「モデルがこの skill 名を誤って識別する」「モデルがこの attractor にはまっている」 — はコードを読んで確認 / 否定できません。 LLM が実際に何を受け取り何を生産するかを観測することでしか確認できません。観測 infra なしでは、すべての LLM 挙動分析は推測です。推測はスタックします。未検証の仮説がそれぞれ次の仮説の前提になり、スタックは自己強化します。矛盾する観測が現れるまで続きます。

discipline: **LLM の挙動を疑った瞬間、LLM への input payload と output を観測できるかを確認します。** infra が存在しなければ、仮説を立てる前にそれを作ります。

**概念例。** Finding に「router が skill 名を誤って識別している」と書かれています。4 つの仮説が提案されます: (a) enum 制約が欠落している; (b) skill description が truncate されている; (c) prompt rule が意図せず削除された; (d) モデルが類似コンテキストで見た名前を hallucinate する。観測 infra なしでは 4 つすべてがもっともらしく、comprehensive fix が 4 つすべてに対処します。観測 infra (実際の system prompt を dump し、enum を確認し、payload を replay) があれば、4 つのうち 3 つを数分で除去できます。正しい fix は確認された原因のみを対象にします。

観測 infra を作った後、以前の仮説をすべて retroactive に verify します。 Reyn batch 7 での実例: 新しいツールを使って 4 つの過去仮説を評価したところ、1.5 件が否定されました。これらの仮説に基づく fix は wrong-layer になるところでした。

See: `feedback_observe_before_speculate_llm.md`

---

### 原則 5: care boundary — structural / behavioral / gray の 3 区分

**universal 原則。** LLM ドリブンシステムのすべての設計決定は、LLM call のライフサイクルのどの段階で機能するかに基づいて 3 つのカテゴリに分類できます。

1. **Structural (pre-call care、常に行う):** LLM が決定を行う環境を構築すること。 schema 制約 / context injection / 決定論的 preprocessing / input shape 正規化。これらは必須です。なければシステムが機能しません。 structural な変更は決定論的な効果を持ちます。

2. **Behavioral rescue (post-call、行わない):** LLM の output を事後的に救済またはパッチすること。 auto-retry / fallback escalation / attractor state machine。 behavioral rescue は bloat trap です。新しい失敗モードのたびに新しい rescue arm が必要になります。また、失敗を event ログとユーザーから隠し、システムのデバッグをより困難にします。 LLM の確率的失敗は可視的に surfacing すべきであり、無音で修正してはなりません。

3. **Gray zone (prompt rule、注意して扱う):** pre-call だが behavioral な性質を持ちます。 prompt rule は LLM が制約を自発的に honor することを求めます。時に必要ですが、原則 2 で述べた蓄積と過剰 consolidation のリスクを持ちます。

**概念例。** LLM が繰り返し空の output を生産します。3 つの対応候補: (a) output schema に enum 制約を追加する (structural — 空の output を構造的に防ぐ); (b) 空の output 検出時に OS で auto-retry を追加する (behavioral rescue — 失敗を隠す); (c) system prompt に MUST rule を追加する (gray zone — 効くかもしれないが bloat するかもしれない)。正しい対応は (a) です。 (a) が適用できない場合 (output が genuinely optional)、構造化された event を emit してユーザーに surfacing することが正しく、(b) は正しくありません。

すべての fix に対する分類質問: 「これは structural な準備か、post-call rescue か、gray-zone の prompt rule か?」 答えが正しい fix layer を決定します。

See: `feedback_reyn_care_boundary.md`、[care-boundary.md](../concepts/care-boundary.md)

---

### 原則 6: verify-first / reproduce-first

**universal 原則。** fix を「landing した」と宣言する前に、2 つのゲートを通る必要があります。

**Reproduce-first gate:** fix に投資する前に、現在の HEAD でその bug が実際に再現するかを確認します。 bug の観測は特定の瞬間の特定の実行で行われます。他の fix が landing した後、以前に観測された bug が再現しなくなることがあります。直接修正されたからではなく、トリガーとなった upstream の条件が消滅したからです。これらは **resolved-indirectly** な findings です。reproduce gate をスキップすると、もはや存在しない bug に fix 投資をしてしまいます。

**Verify-first gate:** fix が landing してテストが pass した後、実際の dogfood scenario で fix が end-to-end で有効であることを確認します。テストの pass は十分ではありません。 test fixture が OS が runtime で実際に生成するアーティファクト形状を反映していない可能性があります (「wrong layer trap」)。e2e 観測のみが、fix が実際の失敗点に到達することを確認します。

**wrong layer trap の概念例。** test fixture が `{"type": "unknown", "data": {"target_skill": "..."}}` として書かれています。OS は runtime に `{"eval_spec": {...}, "target_skill": "..."}` を生成します — `data` wrapper がありません。 `data["target_skill"]` を check する fix はテストをパスしますが (fixture に wrapper がある)、runtime で失敗します (実際のアーティファクトにない)。 test は wrong layer をテストしています。 e2e 検証のみがこれを明らかにします。

**resolved-indirectly の分類。** bug が再現しない場合、resolved-indirectly として分類し、以下を記録します: (a) どの upstream fix が解消を引き起こしたか; (b) なぜ観測が root cause ではなく downstream symptom だったか。このドキュメントにより、将来の batch で同じ偽の bug が再調査されることを防ぎます。

歴史的な calibration データ: batch 9–10 で、3 つの候補 bug のうち 2 つが resolved-indirectly でした。 Brier score は 0.96 (batch 8、verify/reproduce gate なし) から 0.30 (batch 10、両ゲート適用) に改善しました。

See: `feedback_verify_reproduce_first.md`

---

### 原則 7: 修正分類の明示 — 仕様変更 / 不具合修正

**universal 原則。** すべての fix dispatch は以下の 2 つの分類のどちらかでラベルされるべきです。

- **不具合修正 (documented design の復元):** 文書化された仕様が存在し、以前の変更によって違反されました。fix は仕様に準拠したシステムを復元します。ユーザー可視の挙動変更は意図されていません。 production deployment に影響なし。
- **仕様変更 (新しいまたは変更された挙動):** 仕様が拡張または変更されています。ユーザー可視の挙動が変わります。 production deployment に通知が必要かもしれません。

分類の discipline はオーバーヘッドではなく、具体的な目的を果たします。fix を dispatch する前に documented design を audit すべきかどうかを教えてくれます。

**audit の含意:** fix を不具合修正 (documented design の復元) として分類する場合、最初のステップは文書化された設計が実際に復元しようとしている挙動を指定しているかを確認することです。 関連する仕様が曖昧または不在の場合、fix は不具合修正として分類できません。de facto な仕様変更であり、そのように扱うべきです。

**概念例。** permission system fix が「non-interactive コンテキストの auto-approval 追加」として dispatch されます。dispatch 前に permission model 仕様を確認すると、documented design が 4 つの承認メカニズム (config file / CLI flag / approvals file / interactive prompt) を説明しており、auto-approval variant がないことが分かります。 「fix」は実際には仕様変更であり、documented model にない非対称な挙動を導入します。正しい対応は fix を却下し、実際に壊れている documented behavior を特定することです。

この原則は batch 13 で確立されました。トリガーはユーザーの simplicity test: 「permission system を一言で説明できますか?」 簡潔な説明ができなかったことが、accumulated fix が undocumented behavior を導入したシグナルでした。

---

### 原則 8: documented design 整合性 audit

**universal 原則。** 複数の fix batch を経ると、accumulated な変更が実装を documented design から離れた方向に drift させる可能性があります。単一の fix が大きな incoherence を導入するわけではなく、各 fix は局所的に合理的です。しかし累積効果として、documented な原則では挙動を説明できないシステムが生まれます。

audit discipline: **fix batch を dispatch する前に、関連する仕様ドキュメントを読み、提案された fix が documented design と一致することを確認します。** これは原則 4 (観測 infra を作ってから推測する) の architectural 版です。文書化されたモデルを読まずに、正しい挙動が何かを推測すべきではありません。

**simplicity smell test** は audit をトリガーするための user-side heuristic です。システムを理解している人が、あるコンポーネントの挙動を 2〜3 文で説明できない場合、そのコンポーネントが incoherence を蓄積しているシグナルです。 simplicity test は formal check ではなく、formal audit に先行する会話レベルの検出器です。

**概念例。** 数 batch にわたる 3 つの fix が permission model に accumulated しました。各 fix は dispatch 当時は内部的に一貫していました。ユーザーが permission の仕組みについてシンプルな説明を求めます。 応答には 5 つのルールと 1 つの例外が必要です。例外はサインです。例外はおそらく、モデルの基盤となる対称性を破った fix によって導入されました。 audit が問題の fix を発見し、doc 違反の変更として分類し、revert します。

revert 後、permission model は例外なしの 3 ルールで説明でき、挙動は予測可能です。

---

### 原則 9: simplicity smell test

**universal 原則。** accumulated fix は、局所的には正しい (個々の部分はそれぞれ正当化できる) が全体として incoherent (システム全体を簡単に説明できない) なシステムを生み出す可能性があります。 simplicity smell test は、この状態がさらに進む前に検出するための heuristic です。

test: **コンポーネントの挙動を例外なしの 1〜2 文で説明できるか?** できない場合、2 つのどちらかが真です: (a) コンポーネントは genuinely complex で説明に深さが必要; または (b) accumulated fix が属さない非対称性と例外を導入した。(a) と (b) を区別するには documented design を読む必要があります。 documented design がシンプルだが現在の挙動が例外を必要とする場合、(b) が確認されます。

**設計の対称性を positive な判断基準として。** 構造的によく設計されたコンポーネントは対称的な挙動を持ちます。特定の呼び出しモードやコンテキストに対する特別ケースなしに、同じ原則が一様に適用されます。非対称な挙動 — 「このモードではこう動くが、あのモードでは別の動き方をする」 — は incoherence への positive なシグナルです。

**概念例。** 承認メカニズムがインタラクティブで呼び出された場合 (ユーザーにプロンプトが表示される) と non-interactive で呼び出された場合 (サイレントに自動承認) で異なる動作をします。この非対称性は導入時に正当化されます (「non-interactive ではプロンプトを表示できない」)。しかし documented design は呼び出しモードに関わらず承認を一様に扱います。 simplicity smell test が非対称性にフラグを立て、audit が documented model の違反を確認します。正しい fix は非対称性をさらに拡張することではなく、両方のモードで機能する対称的なメカニズムを見つけることです。

この原則は原則 8 (audit) を補完し、トリガーシグナルを提供します。原則 8 は audit の方法を、原則 9 は audit のタイミングを教えます。

### 原則 10: attractor 命名の前に structural pre-check (= batch 17 lift)

**症状クラス原則。** 観測した行動を 「attractor」 (= 利用可能な代替があるのに LLM が誤った path を選ぶ) と命名する前に、 expected path が実際に LLM 視界に存在することを confirm: tool が catalog にあり、 dispatch 経路が wired、 candidate が `candidate_outputs` に含まれている。 wiring 漏れによる 0/N invocation rate は attractor と表面上同じだが、 fix 経路は完全に異なる (= 構造的 code 変更、 prompt fix ではない)。

**operational rule.** prelude の各 scenario は **structural pre-check status** (= ✓ / ⚠️ / ❌) を behavioral prediction の前に明文化。 structural pre-check 失敗時は `verdict=blocked` を記録 (= `refuted` ではない)、 これで attractor base rate が 構造 bug データで汚染されない。

事例: batch 17 (= ADR-0033 RAG Phase 1 初 dogfood) が S5 0/5 invoke を attractor と分類しかけたが、 真因は 3-layer wiring drift (`ToolRegistry` 登録 + `build_tools()` 不在 + `_REGISTRY_DISPATCH_TOOLS` 不在) — chat router から tool が呼べる為には 3 つの独立 box すべて ✓ が必要だった。 `feedback_observe_before_speculate_llm.md` (= 観測前に推測しない) の双対として、 観測前に決定論的な構造 check を置く。

**Config-default audit extension (= batch 23 lift).** wiring check に加え、 prelude が暗黙に仮定する config 値を actual 値で verify する。 `python -c "from reyn.config import load_config; c = load_config(); print(...)"` で actual を確認し、 prelude の 「Structural pre-check」 section に `### Config defaults verified` sub-row を追加、 各 assumption を 1 行で記述する。 事例: batch 23 S3 は `sandbox.backend` を `noop` と想定したが actual は `auto` (= 環境依存 default)、 prelude が exercise すべき gating variant に未到達。 同 batch で SP base chars を 2500 と想定したが actual は 3735 (= main repo の `project_context_path` injection が加算)。 env-dependent default は wiring と同様に決定論的に verify 可能、 prelude commit 前に必須化。

### 原則 11: structural / behavioral 予測軸の分離 (= batch 18 lift)

**症状クラス原則。** scenario の verified rate 予測は単一値ではなく、 **2 つの独立軸の積**: (a) **structural axis** = 「expected path が LLM 視界にあるか?」 (= 決定論、 binary、 pre-check 可能)、 (b) **behavioral axis** = 「path がある状態で LLM が picks するか?」 (= 確率的、 prior batch の base rate 依存、 N runs で測定)。 両者を混ぜると structural fix が landing した直後に楽観バイアス (「wiring 直したから 70%+ verified に届く」) で behavioral base rate が支持しない予測になる。

**operational rule.** prelude の各 scenario は 2 row で予測: structural axis row (= pre-check 状態、 ✓/⚠️/❌) + behavioral axis row (= prior batch attractor base rate、 X%)。 verified prediction は `P(structural ✓) × P(behavioral ✓)`。 fix wave 後の 「structural ✓ → 楽観予測 → calibration miss」 trap を catch。

事例: batch 18 retest は batch 17 fix wave の 6 件 release-blocker を全 close、 4 scenario の structural axis は 100% confirmed、 ただし predictin 70-75% verified に対して actual 25% (= 3/12 primary)。 behavioral 軸で新 attractor (= S6 R-RAG-srcread / S9 R-RAG-numerical-vs-flag-bias / S8 verification-path gap) が surface したため。 2 軸を別々に予測していれば 25-40% に着地していたはず。

### 原則 12: verdict 区分の false-attribution discipline (= batch 18 lift)

**calibration 原則。** 「refuted」 は非 verified の catch-all ではない。 verdict 間の false attribution は calibration record を汚染し、 fix path 判断を誤る。 3 区分:

- **`refuted`** — LLM に path 利用可能で別を選んだ (= R-attractor data point、 prompt / schema / model fix 候補)
- **`inconclusive`** — LLM は intended path を正しく選んだが、 verification harness が完走を観測できなかった (= driver / harness / config gap、 LLM 行動でない)
- **`blocked`** — structural pre-check 自体が fail (= 構造 bug、 behavioral 測定の前段階)

**operational rule.** driver の verdict 判定は明示的、 per-run doc が specific evidence を citation (= 「tool は invoke されたが reyn web の `PermissionResolver(interactive=False)` で ask cycle が deny に short-circuit、 verification path unreachable」 → inconclusive)。 cross-batch attractor base rate は `refuted` runs のみから算出、 inconclusive / blocked は混ぜない。

事例: batch 18 S8 (= drop_source via chat) は LLM が `drop_source` を 3/3 で正しく invoke、 permission_denied event も 3/3 で発火、 wiring は end-to-end 動作。 ただし `reyn web` が non-interactive permission resolver を構築するため intended ask-and-approve cycle が deny に short-circuit。 これを `refuted` と分類すると 「drop_source attractor」 という存在しない phantom evidence を作ってしまう。 `inconclusive` で UX config gap (= R1 carry-over) として正しく attribute。

**自動化候補 (= batch 23 lift).** batch 23 S3 で driver verdict (= verified) と analyst verdict (= inconclusive) が分離した経験から、 verdict 判定の ambiguity が driver 実行の観測精度に依存することが判明した。 将来の自動化候補として: (1) driver が per-run verdict を structured log に出力し human analyst が再読できるようにする、 (2) `refuted` / `inconclusive` / `blocked` の 3 区分を driver が自動 tag し cross-batch base rate を集計スクリプトが算出する。 現状は human driver の manual attribution に依存しているが、 verdict 区分の一貫性はスクリプト化可能な rule set であるため automation で calibration 汚染リスクを下げられる。

### 原則 13 (candidate): behavioral attractor class taxonomy

> ⚠️ **Status: 部分 evidence — Class A 確定、 Class B 仮説保留、 Class C は既存知識.** 事例と scope は将来 replication のために記録、 confirmed evidence の範囲を超えて一般化しない。

**仮説.** behavioral attractor は **誤 path を生む原因** で subdivide、 effective fix layer は class 依存:

- **Class A — Cognitive-bias attractor** (✅ valid evidence、 batch 19 S9): LLM は input 全部見ているが evidence 比重を間違える (= numeric value を boolean policy flag より重く weight)。 Fix layer: **prompt-layer named anti-attractor callout** = *「Common attractor to avoid: when X, do NOT Y. Z wins over W.」* 形式。 S9 で ~100% compliance 達成、 smoking-gun (= LLM が小数値を reasoning で自己引用しつつ abort 出力) で active override 確認。
- **Class B — Affordance-bias attractor** (⚠️ 仮説保留): 複数 tool / source が同 query を plausibly 処理可能な時、 LLM が 1 つで satisfied する pattern が存在しうる。 batch 18-20 で 3 度試行、 各々 scenario design confound が surface。 decisive 判定 prompt の仕様確定 (= prompt が両 source を structurally 必要とする、 例 *「Give me (a) the conceptual overview AND (b) the actual class names I'll need to import」*)、 ただし retest 自体は post-1.0 fast-follow scope。 valid scenario が data 出すまで Class B は仮説のまま。
- **Class C — Protocol-level attractor** (✅ valid evidence、 既存 G12): LLM API protocol-level quirk (= post-tool empty-stop / format leak / role artifact)。 Fix layer: **envelope-layer adapter pattern** (`feedback_envelope_layer_fix.md` 参照)。

**介入 layer ladder** (= cheap → expensive): prompt-layer → schema-layer → envelope-layer → model-layer。 Class A は prompt-layer、 Class C は envelope-layer。 Class B (検証時) は schema-layer 以降と仮説、 ただし未検証。

**部分 evidence で operationalize する理由.** Class B を 早期に確定とすると cost が real: batch 19 retrospective が当初 taxonomy を確立済として schema-layer escalation を提案、 後 `feedback_pre_retrospective_discipline.md` (= 原則 batch 19) で trace dump 再読時に過剰一般化を catch。 hypothesis を 明示 evidence-status 付で記録することで、 confident-sounding だが unsupported な claim の継承を防ぐ。

### 原則 14 (candidate): scenario design audit checklist

> Status: batch 20 で確立。 4 dimension audit で、 batch 18-20 の 3 連続 scenario design flaw を生んだ implicit 1 dimension audit を置換。

**operational rule.** 各 prelude scenario は 4 dimension を明文化:

| Dimension | Audit point | Mitigation example |
|---|---|---|
| 1. Data semantic match | indexed source / data の content が prompt topic と prompt が問う深さで match するか | 「how is X implemented?」 prompt に対して source が concept-only ならデータ不一致 |
| 2. Tool affordance match | 関連 tool description が prompt の exact use case を claim していないか? expected verdict と conflict しないか? | `reyn_src_read` description は 「for any 'how does Reyn X work?' question」 を claim、 同形 prompt は正しくそれに routing する (test が `recall` を期待しても) |
| 3. Structural source-count requirement | prompt が **structurally** expected な source 数を必要としているか? 単一 source で rational に satisfy できないか? | 「How does X work?」 は concept doc 単独で rational に satisfy、 multi-source picks 測定には source 固有 content を要求する prompt が必要 |
| 4. Rational alternative paths | 同 query に対する rational alternative path (= web_search / file_read 等) は何か? expected path が真に most rational か、 alternative の方が強いか? | indexed sources がある状態で web_search / file_read が同 query を natural に処理できるなら、 expected path は LLM の rational choice でない可能性 |

scenario は 4 dimension すべて ✓ で初めて execution 承認、 1 row でも ⚠️ なら prelude commit 前に redesign。

事例: batch 18 が dimension 1+2 で fail (= S6 prompt 「How is recall implemented?」 — concept-only data が implementation question と不一致 + `reyn_src_read` description が exact use case を claim)。 batch 19 fix wave が表層 symptom のみ対応で新 evidence を生まず。 batch 20 が synthetic source で dimension 2 を fix (= reyn_src_read が fictional 「Quantum Bridge Protocol」 prompt に答えられない) するも、 dimension 3 で fail (= 「How does X work?」 は単一 source で十分)。 4 つ目 (= rational alternative paths) は implicit だが uncodified だった、 ここで明文化して checklist を close。

**redesign の蓄積でなく checklist が真の lift である理由.** batch 18-20 の各 scenario redesign は局所的に正しかった、 systemic flaw は audit が 1 次元だったこと。 prelude template に 4 dimension を強制するように operationalize すれば、 将来の scenario が batch budget を消費する前に gap を catch する。

### 原則 15 (candidate): prompt class taxonomy

> Status: batch 21 (= real e2e dogfood) で確立。 batch 18 S5 の 83% verified は **explicit-search hint 込み prompt** が driver、 同 scenario を **natural concept query** で測ると 0%。 dogfood scenario は prompt class を明示し、 prediction base rate を class 別 calibrate すべき。

**operational rule.** 各 scenario の prompt を以下 2 class に分類:

- **Class P-explicit** — 明示的 search / lookup / find verb 含む prompt (= 「Search the docs」 / 「look up X」 / 「find the X」)。 user 意図は tool-level (= retrieval を要求)。 router SP の 「When user says 'search' / 'find in docs' / 'lookup', use recall」 rule が trigger。
- **Class P-natural** — tool-level verb なき自然な質問 (= 「What is X?」 / 「Explain X」 / 「How does X work?」)。 user 意図は content-level (= 答えを要求、 tool は意識しない)。 routing は context (= tool description + SP rule + indexed source description) からの推論依存。

両 class は **同 attractor に対して異なる base rate** を持つ。 batch 18 S5 P-explicit は 83% verified、 batch 21 同 scenario P-natural は schema-layer fix 前 0%。 prompt class を分類せず予測すると、 dogfooder が思い出した方の rate に anchor して systematic miscalibration。

事例: batch 18 S5 の verified rate は 「RAG が動いている」 headline metric として扱われていたが、 batch 21 (= real e2e against `docs/concepts/*.md`) で natural concept query が 0/3 + hallucinated path を観測。 83% は real だが P-explicit class の rate、 real-world UX への gap は P-natural class の measurement 不在だった。

**Implication for prelude predictions.** 両 class が applicable な場合、 prediction row を class 別に分割:
- Structural axis: 両 class 同じ (= 原則 11)
- Behavioral axis P-explicit: prior batches の explicit-search base rate
- Behavioral axis P-natural: prior batches の natural-question base rate (= しばしば schema-layer fix landing 前は大幅低い)

### 原則 16 (candidate): pre-fix multi-agent context analysis

> Status: batch 22 (= affordance-bias schema-layer fix) で確立。 pre-retrospective discipline (= 原則 batch 19) を 1 phase 前倒し: fix 設計の前に並列 info-gathering agents を dispatch、 evidence ベースで設計開始。

**operational rule.** behavioral attractor (= 原則 13 Class A / B / C) の fix を設計する時、 code を書く前に **info-gathering only mode** (= no edits、 read-only) で並列 sonnet agents dispatch。 typical fan-out は 5 agents:

1. **Trace deep-dive** — 当該 attractor の trace dumps 全部 + 比較対象 batch (= 同 surface が verified した batch) を読み、 LLM-input level の最小 structural difference を特定
2. **Industry research** — mainstream agent frameworks (= OpenAI / Anthropic / LangChain / MCP / practitioner blogs) で同 affordance conflict をどう description するか? documented pattern あるか?
3. **Description / rule history audit** — 既存 description / SP rule の git blame、 commit + motivation + 元 wording が保護する use cases を enumerate
4. **Constraint audit** (= reverse direction) — fix が触る surface の existing constraints (= empty-state / vocab / required field / B17 disambig) を列挙、 fix が preserve すべき
5. **Design space mapping** — fix lever 全て enumerate (= tool description / SP rule / parameter schema / tool ordering / conditional suppression / category field / strict mode / empty-state suppression)、 effort × evidence × risk で ranking

main agent が 5 reports を synthesize、 multi-layer fix を **1 commit** で land、 prompt-tweak speculation iteration ではなく。

事例: batch 22 (= affordance-bias schema-layer fix)。 batch 18-20 は 4 attempts iterating on prompt rewrites + synthetic-content scenario redesigns、 全 0/3 verified。 batch 22 は 5 並列 context-analysis agents を走らせ、 真の driver は SP-level rule (= tool description ではなかった、 当初仮説を覆す) と発見、 multi-layer fix (= SP rule + 2 tool description rewrites with practitioner 4-part template) を 1 commit で land。 同 scenario / 同 prompts / 同 N=3: 0/3 → 3/3 verified、 first attempt。 extra synthesis stage の cost (= ~10 min wall-clock parallel info-gathering + ~5 min synthesis) は prompt-tweak iteration の 4 hours wasted 対比で大幅 net positive。

**When to use vs skip.** Use:
- behavioral attractor (= LLM が利用可能な代替があるのに wrong path picks)
- prior batches で同 attractor が複数回 surface (= base rate ≥ 1)
- root cause unclear (= 「SP rule か description か parameter か?」)
- 1.0 release blocker / production user-impact 高

Skip:
- 単純な structural / wiring / null-safety bug (= trace で root cause 既に明確)
- isolated bug fix (= single file、 single line)
- 投機的 hypothesis 検証 (= valid evidence なし)

本原則は 原則 11 (= predict before observing) + 原則 batch 19 (= pre-retrospective discipline) と pair で **agent self-discipline 3 段 ladder**: predict (prelude) → audit before retrospective (= batch 19) → audit before fix (= batch 22)。

---

## 4. common patterns / anti-patterns

### pattern: 1 つの fix が 1 layer を解消し、次の layer を露呈する

**abstract pattern。** 確率的コンポーネント (LLM / ネットワーク / OS) を持つ layered system では、最上位の visible blocker を fix しても完了は得られず、失敗が次の layer にシフトします。次の layer は常に存在していましたが、以前の blocker によってマスクされていました。

**概念例。** 6 phase の chain が phase 2 (permission denied) で失敗します。 phase 2 を fix します。今度は phase 4 (wrong artifact shape) で失敗します。 phase 4 を fix します。今度は phase 5 (LLM が空の output を生産) で失敗します。各 fix は real で、それぞれが正当化でき、それぞれが新しい blocker を露呈します。

**検出。** calibration モデルに「前段の bug により観測経路が遮断された」ための「blocked」カテゴリが含まれていない場合、関連 phase に到達すらできない scenario で「verified」を系統的に over-predict してしまいます。 「blocked」をアウトカムカテゴリに追加し、early batch の mid-chain phase では baseline ~15–25% として扱います。

**fix layer 別 verified 確率:**
- structural fix (schema 制約 / preprocessor / deterministic path): verified 40–60%
- layer-targeted fix (正しい layer、正しい root cause): verified 30–45%
- wording/prompt fix: verified 10–25%
- wrong-layer fix (test pass + e2e fail): refuted ~80–100%

### pattern: downstream symptom のマスキング

**abstract pattern。** 失敗した観測が root-cause bug として分類されますが、実際は upstream 失敗の symptom です。upstream 失敗が異常な中間結果を生み出し、downstream phase が別のエラーで失敗します。 downstream エラーが観測され、primary bug として扱われます。

**検出。** Reproduce-first (原則 6) が primary な検出メカニズムです。upstream fix が landing した後、bug が再現しなければ、それは downstream symptom でした。 resolved-indirectly として記録し (「fixed」ではなく)、upstream 原因を記録します。

**なぜ重要か。** downstream symptom を symptom に対して fix する (root cause を見つけずに) のは wasteful です。upstream 条件が再発すれば symptom も再発します。特に prompt ベースの symptom fix は problematic です。real fix が landing すると消滅する失敗モードに対して bloat を追加し、system prompt から消えることはありません。

### anti-pattern: prompt rule 累積 trap

各 dogfood scenario が失敗を発見します。各失敗に prompt rule が付きます。 N batch 後、prompt は N ルールを accumulated していて、その多くが微妙に異なる wording で同一の根本的な失敗モードを対象にしています。ルール同士が相互作用し始めます。あるルールの wording が、元の scenario がテストしていない隣接 scenario でトリガーになります。

検出: system prompt の MUST rule 数を数えます。 consolidation pass なしで単調増加している場合、accumulation trap が起動中です。 structural fix audit を実行します。すべての prompt rule に対して、意図された制約を schema enum または deterministic preprocessor step として表現できるかを問います。 yes なら prompt から取り出します。

### anti-pattern: 過剰 consolidation regression

accumulation への対応は consolidation です。これも失敗します。4 つの個別 MUST bullet を 2 段落ブロックに consolidate すると、weak LLM へのシグナルが弱まります。 LLM は複数文の prose に単独 bullet より低い優先度を適用します。

検出: prompt consolidation commit の直後の scenario 挙動の regression。具体的: prompt によって以前は正しく処理されていた scenario が、consolidation 後に失敗します。 fix は元の 4 ルールを逐語的に revert することではなく、wording を dedup した 4 つの bullet に戻すことです。

### anti-pattern: N=1 milestone 格上げ

1 回の実行で scenario が正常に完了したことを観測することは milestone ではありません。データポイントです。 LLM ドリブンのワークフローは確率的であり、1 回の成功は non-deterministic なラッキーケースかもしれません。 milestone status には N≥5 回の実行と最小成功率 (通常「動いている」は ≥60%、「安定している」は ≥80%) が必要です。

N=1 を milestone に格上げすると次の batch で calibration error を引き起こします。次の batch の prediction が milestone の挙動が安定していると仮定しますが、N を増やして underlying blocker が見つかると実際の挙動が戻ります。

### anti-pattern: undocumented behavior の導入

観測された失敗を処理するために fix が dispatch されます。 fix は仕様ドキュメントに記述されていない新しいメカニズムを導入します。そのメカニズムは観測された失敗には効きますが、非対称な挙動を導入します (原則 9 で説明)。 数 batch を経て、複数の undocumented メカニズムが accumulated します。

検出: simplicity smell test (原則 9) がコンポーネントを簡単に説明できないことでトリガーされ、続いて documented design audit (原則 8) が行われます。

---

## 5. calibration discipline

### 観測前に予測する理由

観測前の予測は、dogfood 実行を operational な検証から学習可能なデータに変換するメカニズムです。予測なしでは、すべての観測がシステムのモデルと equally compatible です。予測があれば、予測と観測の間の不一致はシグナルです。モデルの何かが間違っており、更新できます。

calibration は確率を正確にする実践です。60% の prediction は約 60% の確率で正しくあるべきです。 calibration accuracy は Brier score で測定されます (低いほど良い)。 batch 8–14 の Brier score 履歴:

| Batch | Brier | 主な改善要因 |
|-------|-------|-------------|
| 8 | 0.96 | ベースライン — blocked カテゴリなし |
| 9 | 0.55 | blocked カテゴリ追加; wrong-layer から学習 |
| 10 | 0.30 | verify-first + reproduce-first 適用 |
| 11 | 0.65 | N=1 provisional milestone を base rate として使用 |
| 12 | 0.40 | batch 11 の過大評価を修正 |
| 13 | 0.20 | documented design audit が prediction に追加 |
| 14 | 0.18 | 安定 — full framework 稼働 |

batch 11 regression からの教訓: 1 回の成功実行を prediction の基礎として使うと過剰な確信を生みます。 base rate は single-run outcome ではなく、累積観測の成功率を反映すべきです。

### 4 区分 outcome 分類

すべての scenario prediction は以下の 4 つのアウトカムに対する確率分布として表現すべきです。

- **verified**: fix または機能が prediction 通りに動作した
- **inconclusive**: 観測が曖昧 — scenario が関連 phase に到達しなかった、または複数 sub-step の結果が混在した
- **refuted**: 挙動が prediction に反した — fix が効果なし、または誤った効果
- **blocked**: 観測経路が前段の bug に遮断された — scenario が関連 phase に到達できなかった

「blocked」カテゴリの強調は重要です。新規 dogfooder が最も頻繁に省略するカテゴリです。複数の blocker がある layered system では、early batch の mid-chain scenario において「blocked」が最も可能性の高いアウトカムです。省略すると全 blocked outcome が「inconclusive」 に流れ込み、予測モデルがそのカテゴリで使えなくなります。

### fix type 別 base rate

以下は batch 7–14 の歴史的に観測された成功率です。大まかな prior として扱い、保証として扱わないでください。

| Fix type | Verified rate | Notes |
|----------|--------------|-------|
| Structural (schema / enum) | 40–60% | 決定論的効果; 残る失敗は通常 next-layer |
| Deterministic path fix (preprocessor) | 40–60% | structural と同様 |
| Layer-targeted bug fix (正しい診断) | 30–50% | wrong layer にヒットすることも |
| Wording のみの prompt fix | 10–25% | Weak LLM は wording の変化を honor しないことが多い |
| Wrong-layer fix | ~0% (refuted) | Test pass + e2e fail |

### N≥5 stability 必須

N=1 で観測された挙動変化は N≥5 の実行で確認されるまで stable ではありません。「working」宣言の最小閾値は N≥5 で ≥60% 成功です。「stable」(production-ready) 宣言の閾値は N≥5 で ≥80% です。

N≥5 要件の理由: LLM ドリブンのワークフローは、毎回の実行では必ずしも現れない non-deterministic な失敗モードを持ちます。 1 回の成功実行は「fixed」と「特定の LLM 決定シーケンスでは fixed だが一般的には fixed でない」の両方と compatible です。

---

## 6. Reyn 固有のツール

> **このセクションは Reyn 固有のコンテンツです。** このセクションの原則 (観測 infra / payload inspection / replay) はすべての LLM ドリブンシステムに適用されます。ここで説明する specific なツールは Reyn の実装です。別のシステムにこの discipline を適用する場合は、このセクション末尾の「他のシステムへの適用」段落を参照してください。

### これらのツールが存在する理由

batch 7 以前、Reyn での LLM 挙動分析は LLM が実際に何を受け取ったかを観測するメカニズムなしで行われていました。 LLM 挙動についての仮説はコードを読んで形成されました。これが 5 段の推測スタックを生み、複数 batch をかけて解きほどき、複数の wrong-layer fix のコストがかかりました。

batch 7 の観測 infra 投資は iteration speed を「仮説ごとに数日」から「仮説ごとに数分」に変えました。 ツールキットは、full payload capture / payload inspection / payload replay / attractor 自動検出 / 非 TTY からの chat 駆動の 5 軸をカバーします。

### REYN_LLM_TRACE_DUMP

`reyn chat` または `reyn run` を実行する前に環境変数 `REYN_LLM_TRACE_DUMP=<path>` を設定します。 Reyn はすべての LLM call の full input payload — system prompt / messages / tools schema — を `<path>` の JSONL ファイルに書き込みます。

このファイルはすべての LLM 挙動問題への ground truth です。「モデルは enum 制約を見たか?」→ dump の tools schema を読む。「prompt rule は存在するか?」→ dump の system prompt を読む。「モデルは何を返したか?」→ response を読む。

dump は **production-gated** (production deployment ではデフォルトで無効) であり、dogfood session と debug session のみで使用します。

### scripts/dogfood_trace.py

dump file と workspace state のためのマルチモード inspection ユーティリティ:

```bash
# LLM payload サマリーを inspect
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-payloads

# 1 回の call の full system prompt + messages を表示
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-detail --call-id <id>

# call の tools schema を inspect
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-tools-schema --call-id <id>

# cross-session 比較のための multi-trace merge
python scripts/dogfood_trace.py --trace a.jsonl,b.jsonl --mode llm-payloads
```

このツールは raw JSONL ファイルに対して `grep` / `jq` / `cat` を手動で実行するパターンを置き換えます。4〜5 scenario と scenario ごとに複数の LLM call がある batch では、手動アプローチは scenario ごとに 10+ のツール呼び出しかかります。`dogfood_trace.py` は 1 コマンドに集約します。

### scripts/llm_replay.py

キャプチャした LLM call を Reyn の OS layer をバイパスして LiteLLM 経由で直接 replay します。仮説テストの primary ツールです:

```bash
# キャプチャした call を replay
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id>

# patched payload で replay (例: system prompt を変更)
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --patch '{"system": "..."}'

# original response と replay response の diff を表示
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --diff

# N 回実行して確率分布を計測
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --n 10

# 別のモデルで replay (model spike)
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --model openai/gpt-4o
```

`--patch` フラグが **landing 前の fix 効果 verify** を可能にします。提案された fix を反映するように payload を変更し (例: enum field の追加 / prompt rule wording の変更)、コードに触れる前に LLM の応答を観測できます。 これにより test cycle が「fix を実装 → dogfood 実行 → 観測」から「payload を patch → 観測」に短縮されます。

`--n` フラグが **確率分布計測**を可能にします。同じ payload を 10 回実行し、LLM がそれぞれ distinct な output を生産する回数を数えます。これが deterministic vs. probabilistic な失敗を区別する方法であり、attractor fix effectiveness を計測する方法です。

### scripts/detect_attractor.py

dogfood workspace 内の 3 つの attractor pattern の自動検出:

1. **Empty stop**: LLM が空のコンテンツで `finish` output を生産した
2. **Enum violation**: LLM が enum 制約に含まれないオプションを選択した
3. **Tool name hallucination**: LLM が tools schema にない名前でツールを呼び出した

```bash
python scripts/detect_attractor.py --trace <jsonl_path>
```

すべての dogfood batch の後にこれを実行し、高レベルの scenario アウトカムでは見えないかもしれない attractor pattern をキャッチします。 scenario が (final output を生産して) 「完了」しながら、intermediate phase で 1 つ以上の attractor event を含む可能性があります。

### `reyn web` A2A endpoint — 非 TTY からの chat 駆動

TUI は Reyn を駆動する唯一の手段ではありません。 `reyn web` は `localhost:8080` で FastAPI サーバを起動し、登録済み agent をすべて A2A (Agent2Agent) JSON-RPC endpoint として公開します。 次の用途に最適です:

- **fix verify 時の chat フロー再現** — シェルからの `curl` ループは TUI を script 化するより遥かに簡単。
- **チュートリアル例 query の sanity check** を非 TTY 環境（CI、 agent harness、 本セッションのような subprocess 経由）から行う。
- **特定の agent を `--attach` 手順なしで叩く** — agent 名で URL 指定可能。
- **別の LLM (Claude Code / Cursor) から Reyn を駆動** — MCP 未設定でも HTTP は通る。

**サーバ起動:**

```bash
reyn web --reload         # 127.0.0.1:8080、 コード編集で自動リロード
reyn web --port 9000      # ポート上書き
reyn web                  # 素 mode — 編集してもリロードしない
```

**dev / debug iteration では `--reload` を使う。** これなしだと、 tool description / system prompt / router 配下のコード編集は **手動で `kill` → 再 `reyn web`** するまで反映されない。 `--reload` 付きなら uvicorn が ~2 秒でファイル変化を拾う。 dogfood の編集 → 再 curl ループがハンズフリーになる。

サーバは `reyn chat` と同じ `reyn.yaml` / registry を読みます — 別 config 不要。

**Agent 一覧（サーバレベル discovery）:**

```bash
curl -s http://localhost:8080/a2a/agents | jq
```

登録済み agent (`default`、 `reyn agent new` で作成したもの、 `_default` topology が auto-create したものすべて) を返します。

**メッセージ送信 + 返信読み取り（1 ラウンドトリップ）:**

```bash
curl -s -X POST http://localhost:8080/a2a/agents/default \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "kind": "message",
        "role": "user",
        "messageId": "t1",
        "parts": [{"kind": "text", "text": "what is this project about?"}]
      }
    }
  }' | jq -r '.result.parts[0].text'
```

返信は agent の最終統合テキストで、 TUI が render するものと同じです。 multi-turn 履歴は同じ agent への後続 `POST` で継続されます。

**`dogfood_trace.py` / `llm_replay.py` を使うべき場面.** A2A endpoint は routing / skill spawn / multi-turn 統合を含む chat 全経路を駆動します。 単一 phase の LLM payload を観察 / replay したいだけなら trace / replay の方が外科的。 「user が end-to-end で何を見るか」 を問うなら A2A、 「LLM が何を見て、 別 prompt なら何を出すか」 を問うなら trace / replay。

**忘れがちな理由.** web サーバは dogfood batch driver script の一部ではありません（ driver は real user との等価性のため `reyn chat --cui` を subprocess 駆動）。 A2A endpoint は operator が手で叩く debug 用ツール。 `reyn chat --cui` への pipe が面倒な状況（ TUI バッファリング、 terminal なし等）で reach for してください。

### scripts/dogfood_sp_render.py

System prompt レンダリング確認用 CLI。wrapper-only / legacy SP のプレビューと差分 stats を 1 コマンドで取得できます。LLM に何が渡るかを確認するための ad-hoc スクリプトを都度書く必要がなくなります。

完全リファレンス: [docs/reference/dogfood-sp-render.md](../../reference/dogfood-sp-render.md)

### 他のシステムへの適用

core な要件は payload observability です: 各 call について LLM が受け取るものと生産するものを見られる必要があります。すべての LLM API プロバイダーはリクエスト/レスポンスペアのキャプチャをサポートしています。問題は、システムがすべての call を capture layer 経由でルーティングするかどうかです。

最低限の観測スタック:
1. すべての LLM call に対して `{call_id, system_prompt, messages, tools, response}` を構造化ログに書くキャプチャメカニズム
2. そのログを call ID とフィールドでフィルタリングして表示する inspection ユーティリティ
3. キャプチャした payload を変更して再実行できる replay メカニズム

Reyn の 3 つのツール (`REYN_LLM_TRACE_DUMP` / `dogfood_trace.py` / `llm_replay.py`) は 1 つの実装です。任意の LLM proxy layer (LiteLLM proxy / custom middleware) が同じ 3 つの機能を実装できます。 attractor detector はキャプチャした payload があれば任意のドメインで再構築できる post-processing step です。

---

## 6.5 Plan-mode dogfood の特殊観点

> **このセクションは plan-mode がテスト対象に入った時点から適用します。** セクション 3 の 9 原則は変更なく適用されます。変わるのは観測軸です。plan-mode は非同期 dispatch / 並行 in-flight plan / resume 時の memo replay を導入します。これらは skill-side dogfood では決して現れない観測面です。

---

### 6.5.1 なぜ plan-mode には特別な discipline が必要か

Skill-side dogfood は、skill の phase graph が LLM の確率的決定の下で正しく実行されるかを検証します。Plan-mode はそれに加えて、質的に異なる 3 つの関心事を導入します。

**非同期 dispatch と completion 順序。** Plan は background の `asyncio.Task` として実行されます。プランが in-flight の間も、ユーザーは新しいメッセージを送れます。複数のプランが重複できます。 Outbox メッセージはユーザーが issue した順ではなく、completion 順に届きます。これらの性質は skill-side のトレースでは見えません。意図的に concurrent plan を実行して outbox を観測したときのみ現れます。

**Resume 時の memo replay。** クラッシュ耐性 (ADR-0023 + ADR-0025) の価値はコードを読んでテストできません。プロセスを mid-step で kill し、再起動して、完了済みのステップが追加の LLM コストを発生させず同一の output を返すことを確認する必要があります。これは skill-side のいかなるテストとも異なる観測経路です。

**ルーター側の LLM 呼び出し。** Plan-mode はチャットルーター LLM が `plan` ツールを選ぶことでトリガーされます。Skill-side dogfood とは異なり — そこではユーザーがどの skill を呼び出すかを制御する — plan-mode はルーター LLM が確率的に「分解が有益だ」と判断することに依存します。人間の判断では「十分複雑」なクエリでも、ルーター LLM が一貫して直接回答を好めば plan-mode はトリガーされません。プランの invocation 自体が観測ポイントであり、前提ではありません。

セクション 3 の原則は引き続き適用されます — 特に原則 4 (先に観測 infra を作る)、原則 6 (verify-first / reproduce-first)、原則 3 (1 仮説 1 修正 1 検証) — ただし具体的な観測面は skill-side dogfood と異なります。

---

### 6.5.2 新しい観測面

Plan-mode は 6 つの異なる場所に永続的な状態を生産します。それぞれ目的と decay ライフサイクルが異なります。

| 観測面 | 場所 | 何を見るか |
|---|---|---|
| WAL | `state/wal.jsonl` | `plan_started` / `plan_completed` / `plan_aborted` / `plan_step_started` / `plan_step_completed` / `plan_step_failed` — resume の基盤 |
| Events log (forensic 専用) | `events/<caller>/...` | `plan_emitted` / `plan_aggregated` / `plan_run_interrupted` / `plan_step_memoized` / `plan_step_memo_failed` / `plan_step_llm_memoized` |
| Per-plan snapshot | `state/plans/<plan_id>.snapshot.json` | `step_results` / `step_result_refs` / `step_llm_calls` — resume を駆動する永続キャッシュ |
| スピルした step 結果 | `state/plans/<plan_id>/step_results/<step_id>.txt` | ADR-0024 — 32 KB 超の output のみスピル、それ以外は inline |
| スピルした LLM call 記録 | `state/plans/<plan_id>/step_llm_calls/<step_id>/<turn_idx>.json` | ADR-0025 — 32 KB 超の結果のみスピル |
| Outbox (= UI / TUI) | `session.outbox` キュー、chat REPL でも可視 | `kind=status` per-step 進捗ナレーション; `kind=agent` 最終テキスト (`meta.plan_id` 付き) |
| 実行中タスク | `session.running_plans: dict[plan_id, asyncio.Task]` | `/plan list` slash コマンドで確認 |

**各観測面の読み方 discipline:**

- **WAL を先に読む。** WAL は resume の primary な基盤であり、最速で読める観測面です。step が完了したと主張するなら、`plan_step_completed` が存在しなければなりません。存在しなければ、その step はコミットされていません — snapshot に古いデータが入っている可能性があります。
- **次に snapshot を読む。** Snapshot は resume coordinator が読む対象です。memo 化を期待するステップに対して `step_results` (inline) または `step_result_refs` (スピル) が populated されているか確認します。
- **因果関係には events log を使う。** `plan_step_memoized` と `plan_step_llm_memoized` は memo パスが実際に起動したことを確認します。結果が存在するだけでは不十分です。 Events log はこの forensic 用途にのみ使います — 運用チェックには使いません。
- **User 向け正確性には outbox を使う。** Outbox はユーザーが見るものです。`meta.plan_id` タグが並行 plan の output を区別するメカニズムです。各 plan の最終テキストが正しい `plan_id` を持つか確認します。

---

### 6.5.3 ツール cheat sheet

`dogfood_trace.py` ユーティリティは既存の skill-side モードに加えて plan 専用のモードを公開します。

```bash
# Plan-mode サマリー (= プラン数 / memo ヒット数 / 最大並行数)
python scripts/dogfood_trace.py --mode plan-summary

# Per-plan タイムライン (= 1 plan_id の WAL + events log + outbox)
python scripts/dogfood_trace.py --mode plan-trace <plan_id>

# Per-plan workspace dump (= decomposition + snapshot + スピルファイル)
python scripts/dogfood_trace.py --mode plan-snapshot <plan_id>

# Cost モード (= memo 節約額の見積もりが追加された)
python scripts/dogfood_trace.py --mode cost
```

> 注: `--mode plan-summary` / `plan-trace` / `plan-snapshot` はこのセクションと同じ prep wave で追加されます。まだ landing していない場合、このドキュメントは forward-looking です。モードが landing するまでは WAL / snapshot の手動確認で同等の観測を行ってください。

既存の skill-side モードはセクション 6 を参照してください。`--mode cost` の出力は共有です — skill-side と plan-side の LLM call コストを両方含み、plan の resume が firing した場合は memo 節約額が別途 breakdown されます。

Attractor detector (`scripts/detect_attractor.py`) は plan-mode でも有用です。個別ステップの実行内の empty-stop / enum violation attractor を検出するために、sub-loop のトレース dump に対して実行します。

```bash
REYN_LLM_TRACE_DUMP=plan_trace.jsonl reyn chat
python scripts/detect_attractor.py --trace plan_trace.jsonl  # step レベルの attractor をキャッチ
```

---

### 6.5.4 Plan-mode 向け scenario 設計

Batch 設計 (A1 ステップ) において、plan-mode は固有の性質を行使する意図的に構築されたシナリオを必要とします。5 つの scenario クラスが主要なリスク面をカバーします。

#### クラス 1: 多ソース合成 (long-step)

**目的。** ルーター LLM が分解を要するクエリに対して実際に plan-mode を呼び出すかを検証し、decomposition と aggregation が coherent であることを確認します。

**クエリ例。** 「README と CLAUDE.md を比較して、新規コントリビューター向けに主な違いをまとめてください。」

**観測するもの。**
1. ルーター LLM が `plan` ツールを呼び出すか? (`REYN_LLM_TRACE_DUMP` でルーターのターンに `plan` ツール呼び出しがあるか確認)
2. Decomposition が well-formed か (2〜7 ステップ、循環依存なし)?
3. 最終集約ステップが両ソースを参照した coherent な回答を生産するか?

**Verified の定義。** ルーターが `plan` を呼び出す; `state/plans/<plan_id>/decomposition.json` が存在する; outbox が `meta.plan_id` 付きの `kind=agent` メッセージを受け取る; コンテンツが両ドキュメントを参照している。

**Refuted の定義。** ルーターが `plan` を呼ばずに直接回答する。これはバグではありません (ルーターが直接回答の方が適切と判断した可能性がある)。しかし、あなたのシナリオが plan-mode をテストできていないことを意味します。クエリをより明示的に多ソースに修正します。

**Blocked の定義。** ルーターがツール呼び出しを生産する前にエラーになる。Plan-mode finding ではなく、prior-layer のバグとして扱います。

#### クラス 2: 並行 plan (multi-plan UX)

**目的。** 複数の in-flight plan が completion 順に正しくタグ付けされた outbox output を生産するかを検証します。Issue 順ではありません。

**実行方法。** 2 つのユーザープロンプトを back-to-back で issue します (どちらの plan が完了する前に)。短い plan (2 ステップ) と長い plan (5 ステップ) を使います。Outbox の順序を観測します。

**観測するもの。**
1. Outbox が 2 つの別々の `meta.plan_id` タグ付き最終メッセージを受け取るか?
2. 短い plan のメッセージが長い plan より先に届くか — どちらが先に issue されたかに関わらず?
3. どちらかが完了する前に `/plan list` が両プランを active として表示するか?

**Verified の定義。** 2 つの distinct な `plan_id` 値; 短い plan の `kind=agent` メッセージが先に届く; 両プランが state collision なく完了する。

**Refuted の定義。** Duration に関わらず Issue 順に完了する — outbox 順序が誤っていることを示唆する。または state collision (一方の plan の step 結果が他方の snapshot に現れる)。

**Blocked の定義。** ルーターが 2 つのクエリのうち 1 つのみで plan-mode をトリガーする。

#### クラス 3: Crash + resume

**目的。** ADR-0023 (step 結果 memoization) と ADR-0025 (sub-loop LLM call memoization) が resume 時に firing するかを検証します。これはクラッシュ耐性の信頼性にとって最も重要な scenario クラスです。

**実行方法。**
1. 複数ステップの plan を開始する (クラス 1 以上)。
2. WAL でステップ 1 が `plan_step_completed` を emit するまで待つ。
3. `reyn chat` プロセスを mid-step-2 で `kill -9` する。
4. `reyn chat` を再起動する。
5. Resume 挙動を観測する。

**観測するもの。** (完全な手順は 6.5.5 を参照)

**Verified の定義。** ステップ 1 が resume 時に新しい LLM コストを発生させない (`plan_step_memoized` event が firing する); ステップ 2 が中断地点から再実行される; プランが正しく完了する。

**Refuted の定義。** ステップ 1 が resume 時に LLM コストを再発生させる (`plan_step_memoized` event なし、cost ledger にステップ 1 の呼び出しの新しいエントリ)。

**Blocked の定義。** `_recover_plans_for_agent` が firing しない (ログメッセージが不在) — WAL replay または agent registry restore パスに plan-mode の upstream でバグがあることを示唆する。

#### クラス 4: operator escape hatch

**目的。** `/plan list` / `/plan discard <plan_id>` / `/plan resume <plan_id> --from <step_id>` が live および interrupted な plan に対して正しく動作するかを検証します。

**観測するもの。**
1. `/plan list` — in-flight plan 中に正しい `plan_id` / step 数 / running/pending 状態を表示するか。
2. `/plan discard` — タスクをキャンセルし、`plan_aborted` を WAL に書き込み、decomposition artifact と snapshot を削除し、outbox 通知を送るか。
3. `/plan resume --from <step_id>` — 指定したステップから再実行し、それ以前のステップが memo-replay され (LLM コストなし)、最終 output が再実行ステップを反映するか。

**Verified の定義。** 各コマンドが期待される WAL エントリと outbox 状態変化を生産する。

**Refuted の定義。** `/plan discard` が decomposition artifact を削除しない — stale artifact ghost のリスク (6.5.6 anti-pattern を参照)。

#### クラス 5: long-tail step (> 32 KB output)

**目的。** ステップが 32 KB を超える output を生産したときに、ADR-0024 step 結果スピルがデータロスなしにトリガーされるかを検証します。

**実行方法。** 大きなテキスト output を合成するステップを構築します (例: 「`src/` 以下の全ファイルがエクスポートするシンボルを列挙してください」 — 中規模コードベースで通常 > 32 KB)。

**観測するもの。**
1. Snapshot に `step_results.<step_id>` ではなく `step_result_refs.<step_id>` が現れるか?
2. `state/plans/<plan_id>/step_results/<step_id>.txt` が存在し、truncation なしに完全な output を含むか?
3. Downstream の集約ステップが完全なコンテンツを受け取るか (= `get_step_result` による透過的解決)?

**Verified の定義。** `step_result_refs` が populated; スピルファイルが存在する; downstream ステップのコンテンツがスピルファイル内にのみ存在するコンテンツを参照している (= truncation なし)。

**Refuted の定義。** 32 KB 超にもかかわらず output が snapshot に inline で現れる — スピルがトリガーされなかった。または: スピルファイルが存在するが downstream ステップが truncated なバージョンを受け取った。

---

### 6.5.5 Memo ヒット検証手順

これは ADR-0023 (step 結果 memoization) と ADR-0025 (sub-loop LLM call memoization) の両方が resume 時に正しく replay されることを確認するための step-by-step 手順です。クラス 3 scenario を実行するときにこの手順を実行します。

**ステップ 1: plan を完走させる (baseline)。**

クリーンな state directory から開始します (`state/plans/` が空または active plan を含まない)。複数ステップの plan (3 ステップ以上推奨) を実行し、完走させます。以下を記録します:
- WAL または `/plan list` からの `plan_id`。
- `python scripts/dogfood_trace.py --mode cost` の cost ledger output (新鮮な実行の LLM コストを比較用にキャプチャ)。

**ステップ 2: 同じクエリを再実行し、mid-step-2 で kill する。**

同じクエリを再実行します。これで新しい `plan_id` を持つ新しいプランが開始されます。WAL でステップ 1 (`s1`) の `plan_step_completed` を監視します。これが現れたら即座にプロセスを `kill -9` します。ステップ 2 (`s2`) は進行中または未開始のはずです。

**ステップ 3: `reyn chat` を再起動する。**

Resume パスは起動時に自動的にトリガーされます。ログ出力を観察します:
```
_recover_plans_for_agent fired for agent <name>, plan_id <id>
```
このメッセージが不在の場合、WAL replay または agent registry restore パスに plan-mode の upstream で問題があります — prior-layer のバグとして report します。

**ステップ 4: per-plan snapshot を開く。**

```bash
cat state/plans/<plan_id>.snapshot.json | python -m json.tool
```

以下を確認します:
- `step_results.s1` (inline) **または** `step_result_refs.s1` (スピル) がステップ 1 の最初の実行結果で populated されている。
- `step_llm_calls.s1` が sub-loop の記録された LLM call エントリで populated されている。

どちらかが不在の場合、kill 前に snapshot がコミットされませんでした — kill タイミングが早すぎました。より長いステップで再試行します。

**ステップ 5: events log で `plan_step_memoized` を監視する。**

再起動後、そのプランの events log を観察します:

```bash
python scripts/dogfood_trace.py --mode plan-trace <plan_id>
```

`s1` に対して (`plan_step_completed` ではなく) `plan_step_memoized` が現れることを確認します。区別:
- `plan_step_completed` = ステップが新鮮に実行された。
- `plan_step_memoized` = ステップが LLM 呼び出しなしに snapshot から replay された。

代わりに `s1` に対して `plan_step_completed` が現れた場合、memo replay が firing しませんでした — ステップ 1 が再実行され、新しい LLM コストが発生しています。

**ステップ 6: s1 内の sub-loop 呼び出しに対して `plan_step_llm_memoized` を監視する。**

ステップ 1 が複数の sub-loop ターン (= ステップ executor 内の複数の LLM 呼び出し) を含む場合、kill 前に記録された各 sub-loop LLM 呼び出しが resume 時に `plan_step_llm_memoized` を emit するはずです。これが ADR-0025 のメカニズムです — ステップが部分的にしか完了していない場合でも、sub-loop の LLM コストの再支払いを防ぎます。

**ステップ 7: s1 に追加 LLM コストがないことを確認する。**

Resume 完了後、`python scripts/dogfood_trace.py --mode cost` を実行します。Resume された plan の cost ledger は以下を示すはずです:
- ステップ 1 (`s1`): $0.00 (または 0 tokens) — memo ヒット。
- ステップ 2 以降 (`s2`...): 新鮮なコスト — これらは再実行された。

`s1` が非ゼロコストを示す場合、ステップ 1 の memoization が firing しませんでした。これは HIGH バグです: クラッシュ耐性の主張が満たされていません。

**ステップ 8: plan が正しく完了することを確認する。**

Resume された plan は baseline 実行 (ステップ 1) と同じ最終 output で完了するはずです。Output が実質的に異なる場合 (単なる空白 / トークンサンプリングの分散ではなく)、memo replay がデータの破損を導入しています。CRITICAL として report します。

---

### 6.5.6 Plan-mode 特有の patterns / anti-patterns

このセクションはセクション 4 の patterns / anti-patterns を plan-mode 固有のケースに拡張します。skill-side の layer-by-layer パターンと downstream symptom パターンについてはセクション 4 を参照してください — それらは equally here に適用されます。

#### Pattern: multi-plan の completion 順序は設計によるもの、偶然ではない

2 つのプランが in-flight で、短い方が先に完了した場合、outbox の順序は**正しい挙動**です — タイミングの偶然ではありません。各 `kind=agent` メッセージの `meta.plan_id` タグが、順序が issue 順と異なる場合でも UI が正しいプランに output を帰属させるためのメカニズムです。

scenario 設計への示唆: クラス 2 (concurrent plans) を実行する際、outbox メッセージの `meta.plan_id` 値を明示的に確認します。どのプランがどの output を生産したかを位置だけで判断しないでください。`meta.plan_id` 帰属なしに plan output を混在して表示する UI は UX バグです。Plan-mode のバグではありません。

#### Pattern: 32 KB 閾値の境界で spill vs inline が混在するのは正常

ステップ結果が snapshot に inline で入るか、ファイルにスピルするかは、書き込み時の output サイズで決まります。両方のパスが正しいです。あるステップがスピルし、別のステップが inline に収まるテスト batch は inconsistency のサインではありません — シナリオの実際の output サイズ分布を反映しているだけです。

> 32 KB の output を生産するためにシナリオを意図的に構築した場合 (クラス 5) 以外は、「このステップは必ずスピルしなければならない」という特別ケースのアサーションを追加しないでください。汎用シナリオでは両方を有効なアウトカムとして扱い、downstream ステップがパスに関わらず正しいコンテンツを受け取ったかのみを確認します。

#### Anti-pattern: `plan_step_failed` をハードエラーとして扱う

Per-step の失敗は plan runtime によってキャッチされ記録されます。プランは後続のステップの実行を継続します (= 失敗したステップの output が downstream ステップに必要な場合を除く)。WAL で `plan_step_failed` が見つかった dogfood finding は**自動的に HIGH バグではありません** — 以下によります:
1. 失敗が期待されていたか (ステップのクエリに有効な回答がなかった)。
2. Downstream ステップが欠落した入力を gracefully に処理したか。
3. 最終集約ステップが失敗にもかかわらず coherent な output を生産したか。

`plan_step_failed` が現れた場合、severity をエスカレートする前に graceful degradation を確認します。プランが失敗を認識した coherent な output で完了した場合、severity は MED (劣化しているが壊れていない) です。プランが失敗を surfacing せずに誤った集約 output をサイレントに生産した場合、severity は HIGH (データ正確性の問題) です。

cross-ref: これはセクション 4 の「downstream symptom のマスキング」の plan-mode 版です — 可視的な失敗 event が常に root-cause の finding とは限りません。

#### Anti-pattern: stale decomposition artifact での再実行

以前の実行の `state/plans/<plan_id>/decomposition.json` が残っている場合 (例: cleanly 完了しなかった `/plan discard` の後、または artifact が削除される前の手動 kill の後)、resume coordinator は新しい実行の `plan_id` に対して古いプラン形状を replay しようとします。結果は予測不能です: step ID が一致しない場合があり、memoization が誤ったステップに対して firing する場合があり、coordinator が corrupt artifact の通知でプランを完全に discard する場合があります。

**フレッシュスタートシナリオを行使する batch 間は、`state/plans/` を完全にクリーンにしてください:**

```bash
rm -rf .reyn/state/plans/
```

これはクラス 3 (crash + resume) またはクラス 5 (long-tail) のシナリオを実行する前に必須です。それ以前の中断実行が artifact を残している場合。Batch 間に実行することは安全です — クリーンアップ後、WAL は削除された artifact を参照しません。

---

### 6.5.7 Plan-mode 向けの calibration 調整

セクション 5 は 4 区分 outcome 分類 (verified / inconclusive / refuted / blocked) と一般的な base rate を確立します。Plan-mode batch では、各 batch の実行前に 3 つの per-scenario バイナリ予測を追加します。

**バイナリ予測 1: 「Resume 時に memo が firing する」(クラス 3 scenario)**

これはテスト可能なバイナリ主張です。次のように表現します: 「ステップ 1 完了後の mid-step-2 での kill-9 の後、resume 時にステップ 1 に対して `plan_step_memoized` が現れる。」

最初の plan-mode batch の suggested prior: 60% verified (= メカニズムは存在するが、kill タイミングがコミットウィンドウを見逃す場合があり、blocked になる可能性がある)。そこからキャリブレートします。

**バイナリ予測 2: 「Multi-plan の completion 順序が issue 順ではなく duration 順になる」(クラス 2 scenario)**

次のように表現します: 「短い duration の plan の `kind=agent` メッセージが長い duration の plan より前に outbox に現れる。」

Suggested prior: 70% verified (= 設計がこれを保証するが、並行 LLM タイミングにより両プランが 1 秒以内に完了して順序が曖昧になる場合がある)。「inconclusive」は両プランが 1 秒以内に完了する場合のアウトカムです。

**バイナリ予測 3: 「手動介入なしに 32 KB スピルがトリガーされる」(クラス 5 scenario)**

次のように表現します: 「32 KB 超の output を生産するステップが snapshot に `step_result_refs` を emit し、スピルファイルが `state/plans/<plan_id>/step_results/<step_id>.txt` に存在する。」

Suggested prior: 75% verified (= 決定論的な閾値だが、特定のシナリオで LLM が確実に > 32 KB を生産するように構築するには、LLM の output verbosity の事前知識が必要であり、これは変動する)。

**Plan-mode 予測の Brier スコアリング。**

これら 3 つのバイナリをセクション 5 と同じ Brier 式で batch ごとにスコアリングします。Skill-side の予測と別々に追跡します — plan-mode と skill-side は異なる base rate プロファイルを持ち、十分なデータが揃うまでプールすべきではありません。

Plan-mode batch の期待 Brier score 軌跡 (skill-side batch 7〜9 との構造的アナロジーによる rough prior):
- Batch 1 (plan-mode): 0.7〜0.9 (観測面が馴染みなく、kill タイミングが不安定)
- Batch 2〜3 (plan-mode): 0.3〜0.5 (観測面を習得し、kill タイミングを練習)
- Batch 4 以降 (plan-mode): 9 原則を一貫して適用すれば 0.2〜0.3

---

### 6.5.8 やってはいけないこと (scope の discipline)

Plan-mode は**チャットルーター側の機能**です。そのスコープは、単一エージェントの runtime 内でのプランの dispatch / execution / memoization / resume です。Skill-side dogfood やマルチエージェント連携と混同しないでください。

**Sub-loop のツールオペレーション memoization 期待をテストしない。**

ADR-0023 §3.4 はツールオペレーション (= 非 LLM ツール dispatch) の memoization を plan resume 設計から明示的に defer しています。Plan のステップが resume するとき、その sub-loop のツール dispatch (例: ファイル読み込み / workspace 書き込み) は**再実行します** — これは documented な設計であり、バグではありません。「ファイル読み込みが resume 時に繰り返された」という dogfood finding は `verified` (正しい挙動) と分類すべきです。バグではありません。

この期待のテストは plan-mode dogfood スコープ外です。ツールオペレーションの再実行下での idempotency を検証したい場合、それは特定のツールオペレーションを対象とした別の skill-side scenario に属します。

**Plan がユーザーターン間で状態を共有することを期待しない。**

プランのスコープは、それをトリガーしたユーザーの単一ターンです (`/plan resume` 経由で明示的に resume された場合を除く)。あるプランのステップによって書き込まれた状態は、次のユーザーターンにトリガーされた後続のプランに自動的には利用できません。各プランは独自の `plan_id` / snapshot / decomposition artifact を持ちます。

あるターンから次のターンへ状態が引き継がれているように見える場合、あるプランのステップが workspace ファイル (= P5 SSoT) に書き込み、別のプランがそれを読んでいないか調査してください — これは正しく期待される動作です。プラン内部の状態 (snapshot / decomposition) が共有されているように見える場合、それはバグです。

**LLM が plan-mode を実際に呼び出すかどうかを必ず観測する。**

これは plan-mode dogfood で最もよく見逃されるポイントです。ルーター LLM が `plan` ツールを呼び出さない場合、クエリがどれほど複雑でも、あなたのシナリオは plan-mode をテストしていません。plan-mode 固有の finding を分析する前に、`REYN_LLM_TRACE_DUMP` でルーターのターンに `plan` ツール呼び出しが含まれることを確認します。含まれない場合、そのシナリオは plan-mode ではなく skill-side のルーティングシナリオです。

複数のクエリ表現を試みても一貫して plan-mode をトリガーできない場合、仮説を立てる前に原則 4 (観測 infra) を適用します。ルーターの system prompt と tool schema を dump し、`plan` ツールが存在することを確認し、ルーターが何を受け取ったかを確認します。セッション設定によってはルーターのカタログに `plan` ツールが含まれていない場合があります。

---

## 6.6 Long-lived session パターン (G12 / context-bloat 測定)

### A. このパターンが存在する理由

セクション 2 および section 6 全体で説明している既存の per-run dogfood パターンは、シナリオ実行のたびに workspace state をリセットします。この分離は R1 type attractor (= LLM がフレッシュな context で拒否 / misroute / 構造的に無効な出力を出す) の測定に有効です。しかし G12 type attractor、特に Pattern E (= 複数ターンにまたがる context bloat によって引き起こされる empty completion) は測定できません。G28 (`giveup-tracker.md` 参照) はこの測定ギャップを明示しました: batch 16 で 8% の empty-reply rate が観測されましたが、後に production 問題ではなく dogfood driver の `clean_state` 呼び出しが disk history と server の in-memory `ChatSession._history` のズレを生じさせたことが原因と判明しました。その結果、production user が経験しない人工的な context 重複が発生していました。production 等価な挙動を測定する — ユーザーのセッションがリセットなしに自然にターンをまたいで成長する状態 — には、driver がそのライフサイクルをミラーする必要があります。

### B. driver の概要

`scripts/dogfood_long_session.py` は Reyn 用の long-lived session driver です。同一の A2A agent endpoint にプロンプトを順に送信し、ターンをまたいで history を自然に蓄積させながら、ターンごとのメトリクスとシナリオ終了後の events log を収集します。

**ターンごとに記録するもの:**

- `reply_len`: 合成テキスト reply の文字数
- `elapsed_s`: ターンの wall-clock latency
- `empty`: reply が空かどうか (空白文字を除いて文字数ゼロ)
- HTTP status および JSON-RPC エラーメッセージ

**シナリオ後の収集:**

- agent の budget-ledger token entries (総 token 数と LLM call 数)
- events log のパス (`detect_attractor.py` による downstream attractor 分析用)

**ターン間でリセットしないもの:** history。server 側の `ChatSession._history` はシナリオの全ターンにわたって継続的に成長します — production user の場合と同様です。

**scenarios ファイル:** `dogfood/scenarios/long_session_v1.yaml` — research chain / pronoun-reference followup / cross-reference 比較 / 反復 context (G12 Pattern E の主要ターゲット) / 一般 Python トピック / file・doc lookup chain / 概念説明 chain の 7 シナリオを収録。

**baseline 実行で使用したコマンド:**

```bash
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml
```

追加フラグ:

```bash
# agent やポートを変更する
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --agent default --web-url http://localhost:8080

# multi-shot (各 shot が異なる agent endpoint を使用: default-shot1, default-shot2, ...)
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --n-shot 3

# downstream 分析用に JSON を出力
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --json --out results.json
```

`--n-shot N > 1` では各 shot が異なる agent 名 (`default-shot1` 〜 `default-shotN`) を使用し、各 shot が真にフレッシュな server 側セッションを得ます。agent は registry に存在するか `reyn agent new` で事前作成が必要です。

### C. どちらのパターンを使うか

| 答えたい問い | 使う driver |
|---|---|
| 「特定シナリオの LLM の R1 拒否率は?」 | Per-run clean_state (既存パターン、セクション 2–5) |
| 「multi-turn 会話での empty-completion base rate は?」 | Long-lived session (本セクション) |
| 「新しい fix が context 処理をターンをまたいで変化させるか?」 | Long-lived session — fix 前後でシナリオを再実行 |
| 「plan-mode の crash + resume 挙動は?」 | Per-run clean_state と `kill -9` 手順 (section 6.5 Class 3) |
| 「特定の attractor が 1 ターンのシナリオに存在するか?」 | Per-run clean_state + `detect_attractor.py` |

### D. 既知の制限

- **Empty 検出は response-text layer で動作し、events layer ではありません。** driver は合成された `reply_len` がゼロのときにターンを empty とカウントします。これはユーザーから見た empty reply を捉えます。events log の `finish_reason: stop` + `completion_tokens: 0` と直接対応するわけではありません。G12 Pattern E / JSON-RPC error / network timeout のどれが原因かを確認するには events log と `--json` 出力の `status` フィールドを参照してください。

- **ターンごとの token 成長曲線は直接取得できません。** budget ledger は agent セッション全体の総 token 数を記録しますが、ターンごとには記録しません。`--json` 出力にはタイムスタンプ付きの `token_entries` が含まれており、ターン wall-clock タイムスタンプとの照合で per-turn 相関を導くことができます。粗い分析にはシナリオごとの合計 token 数で十分です。

- **N=37 ターンは安定したレート推定には小さすぎます。** baseline 実行 (7 シナリオ × 5–6 ターン) では overall empty rate 2% が得られました。方向性の指標としては有用ですが、N=37 での誤差余地は大きい。95% 信頼区間で ±5% 精度のレート推定には N ≥ 100 ターンが必要です。N を増やすには `--n-shot N` で複数 shot を実行するか、シナリオセットを拡張してください。

- **非常に長いターン数 (10+, 20+) での context bloat は未測定です。** baseline は 5–6 ターン/シナリオを使用しました。G12 Pattern E は context サイズの関数として現れます。10+ ターンで empty completion が増加するかどうかはまだ未確認であり、そのレンジをテストするにはシナリオを拡張または新規追加する必要があります。

### E. クロスリファレンス

- **セクション 5 calibration discipline** (N≥10 / N≥5 要件): long-session パターンは測定全体の一方の半分であり、per-run clean_state がもう一方の半分です。どちらか一方だけでは不十分です。
- **`giveup-tracker.md` の G28**: このドライバーの動機となったエントリ。batch 16 の 8% baseline と driver-induced と判明した経緯を含む。
- **`dogfood/scenarios/long_session_v1.yaml`**: 7 シナリオのスターターセット。特定の context 成長仮説をテストする際は拡張してください。
- **`scripts/detect_attractor.py`**: `--json` で報告された events log パスに対して実行し、phase レベルの empty-stop event を確認。

---

## 7. 新規 dogfooder 向け quickstart

### 新 batch 開始時 — checklist

scenario を書く前に:

- [ ] 最新 batch の retrospective を読んで carry-over findings を把握する
- [ ] 以前の batch の HIGH / CRITICAL findings で、まだ verified / resolved-indirectly 分類されていないものを特定する
- [ ] N≥5 確認が必要な prior「provisional milestone」があるかを確認する
- [ ] 観測 infra が動作しているか確認: `REYN_LLM_TRACE_DUMP` が正常にキャプチャし、`dogfood_trace.py` がエラーなく読み込める
- [ ] scenario plan (A1) を草案: 具体的なユーザーメッセージ / 行使するコードパス / 各期待アウトカムへの 4 区分確率分布を含める
- [ ] 予測に「blocked」がアウトカムカテゴリとして含まれているか確認する
- [ ] 何も実行する前に user review (A2) のために plan を提出する

### fix dispatch 時 — checklist

各 fix を dispatch する前に:

- [ ] **Reproduce-first gate:** 現在の HEAD で scenario を実行し、bug が再現することを確認する。再現しない場合は resolved-indirectly として分類し、記録する。
- [ ] **Documented design audit:** 関連する仕様ドキュメント (phase.md / permission-model.md / 関連 concept doc) を読む。提案された fix が documented design と一致することを確認する。
- [ ] **修正分類:** fix を「不具合修正 (documented design の復元)」または「仕様変更 (新しい挙動)」としてラベルする。この分類をユーザーに伝える。
- [ ] **Fix layer:** care boundary チェック (原則 5) を適用する。 fix は structural か? Behavioral rescue か? Prompt rule か? structural を目指す。
- [ ] **仮説の isolation:** fix が LLM 挙動の問題に対処する場合、それは 1 つの仮説だけをテストしているか? 複数の仮説が bundle されている場合は分離する。
- [ ] **Verify-first gate:** fix が landing してテストが pass した後、修正したパスを行使する e2e dogfood scenario を実行する。 fix が実際のアーティファクトフローで有効であることを確認する。

### retrospective template

```markdown
# Batch N — Retrospective

> [batch の main outcome を一言で]

## Expected vs actual

| Scenario | Prediction | Actual | Hit/Miss |
|----------|-----------|--------|---------|
| S1 | X% verified | [outcome] | hit/miss |

## Turning points

[予測と観測が乖離した 2〜3 の瞬間と、そこから学んだことをリストアップ]

## 強化または新確立された原則

[このバッチのデータで強化された原則、および新しく生まれた原則をリストアップ]

## 次 batch への申し送り

- 残課題: [severity 付きでリスト]
- Calibration 調整: [base rate やアウトカムカテゴリの変更点]
- Infra 更新の必要性: [あれば]
```

### batch ディレクトリ構造

```
docs/deep-dives/journal/dogfood/
└── YYYY-MM-DD-batch-N-{label}/
    ├── prelude.md          ← batch 開始時の状態 + carry-over findings
    ├── scenarios.md        ← 予測付きの具体的な scenario
    ├── findings.md         ← サマリーテーブル + narrative
    ├── findings/
    │   ├── BN-H1-<slug>.md ← per-finding 詳細 (severity / status / 診断)
    │   └── ...
    └── retrospective.md    ← 抽出された原則 + 申し送り
```

finding ID フォーマット: `B{batch}-{Severity}{rank}-{slug}`。 Severity prefix: `H` (HIGH) / `M` (MED) / `L` (LOW) / `INFO`。 例: `B13-H1-permission-revert.md`。

複数 batch にわたる cross-batch 課題 (解決なしに追跡) については giveup tracker を使います: `docs/deep-dives/journal/dogfood/giveup-tracker.md`。

### ペーシング: 最初の batch は practice batch

最初に実行する batch は**practice batch**として扱うべきです。その primary な目的は bug 発見ではなく、**calibration**です。
- 観測 infra (`REYN_LLM_TRACE_DUMP` が正常にキャプチャするか?)
- scenario design (scenario は clean な findings を生産できるほど specific か?)
- 予測モデル (4 区分分布は有用なシグナルを生産するか?)
- fix dispatch プロセス (reproduce-first gate が downstream symptom をキャッチするか?)

最初の batch の Brier score が高い (≥0.6) ことを予期してください。これは正常です。 Brier score はテスト対象のシステムの実際の挙動に対して base rate がキャリブレートされるにつれて改善します。 9 つの原則を一貫して適用することで、batch 3〜4 までには Brier score 0.3〜0.4 の範囲が達成可能です。

scenario が成功裏に完了したとしても、最初の batch から milestone を宣言しないでください。 provisional なデータポイントとして記録し、後続の batch で N≥5 で確認します。

---

## Appendix: batch case study

以下の retrospective は、本ガイドで説明した原則の詳細な case study を確立順に提供します。

- **Batch 7** (`docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/retrospective.md`): 観測 infra 確立; care boundary 言語化; 推測スタック解体。
- **Batch 9** (`docs/deep-dives/journal/dogfood/2026-05-05-batch-9-fix-wave/retrospective.md`): Wrong-layer trap 発見; verify-first 原則確立。
- **Batch 10** (`docs/deep-dives/journal/dogfood/2026-05-05-batch-10-residual-fix-wave/retrospective.md`): Reproduce-first 原則確立; resolved-indirectly 分類の形式化。
- **Batch 13** (`docs/deep-dives/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`): Documented design audit 確立; 修正分類 discipline 形式化; simplicity smell test 言語化。
- **Batch 14** (`docs/deep-dives/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`): Production-grade phase 1 完了; full discipline 稼働。
- **Batch 17** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-17-rag-phase-1/retrospective.md`): structural pre-check 必要性 (= 原則 10); ADR-0033 RAG Phase 1 初 dogfood で 「production grade landed」 判断撤回; 6 release-blocker bug を 5-commit fix wave で解消。
- **Batch 18** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-18-rag-fix-retest/retrospective.md`): Headline (S5) full recovery 0/5 → 3/3 + 拡張 N=12 で 83% (= dogfood log の per-scenario calibration recovery 史上最大、 Brier 0.575 → 0.067); structural × behavioral 予測軸分離 (= 原則 11) と verdict false-attribution discipline (= 原則 12) 確立。
- **Batch 19 (revised post self-audit)** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-19-rag-attractor-fix-retest/retrospective.md`): cognitive-bias 系 named anti-attractor callout pattern を 100% compliance で検証 (= S9 Class A)。 当初 affordance-bias attractor (= Class B) も確立と記載したが、 user 指摘 self-audit で S6 evidence は scenario design flaw に起因と判明 (= prompt が `reyn_src_read` の claim use case と一致)。 Class B を仮説に格下げ、 pre-retrospective discipline 確立 (= retrospective 執筆前に LLM trace + tool description + scenario design 前提を必読)。
- **Batch 20** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-20-rag-multi-source-retest/retrospective.md`): S6 を synthetic sources で redesign し `reyn_src_read` affordance conflict を排除、 main agent が pre-retrospective discipline を first time 自己実行、 retrospective 執筆前に 2 度目の scenario design confound (= prompt が単一 source で structurally satisfy 可能) を catch。 affordance-bias hypothesis は依然 pending、 4 dimension scenario design audit checklist (= 原則 14) を batch 18-20 の 1 dimension audit 連続 flaw に対する systemic fix として lift。
- **Batch 21** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-21-rag-real-e2e/retrospective.md`): real e2e dogfood (= 21 EN concept docs → 418 chunks via real `gemini-embedding-001`、 explicit-search prompt ではない natural concept queries)。 first instance of: (a) main agent direct execution (= no sub-agent dispatch) で prelude / index / chat / audit / fix / retrospective pipeline 全自走、 (b) description/path propagation bug B21-S0-1 が in-flight surface + fix、 (c) affordance-bias attractor の valid evidence 取得 (= description fix landing 後も 0/3)。 prompt class taxonomy (= 原則 15) を lift — batch 18 S5 の 83% は P-explicit class、 natural P-natural query での 0% が prior synthetic dogfood で見えなかった real-world gap。
- **Batch 22** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/retrospective.md`): batch 21 で surface した affordance-bias attractor の schema-layer fix。 **First instance of pre-fix multi-agent context analysis (= 原則 16)** — 5 並列 sonnet info-gathering agents (= trace deep-dive + industry research + description history audit + constraint audit + design space mapping) で真の driver を SP-level rule と特定 (= tool description ではなかった、 当初仮説を覆す)。 multi-layer reinforcement fix (= SP rule + 2 tool description rewrites、 practitioner 4-part template) を 1 commit で land、 同 N=3 retest が 0/3 → 3/3 verified、 first attempt。 Class B (= affordance-bias) hypothesis status を 「partial validation」 → **decisive validation** に格上げ、 schema-layer multi-layer reinforcement pattern が Class B fix template として確立。

full batch index と operational log については `docs/deep-dives/journal/dogfood/README.md` を参照してください。
