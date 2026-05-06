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

batch 7 の観測 infra 投資は iteration speed を「仮説ごとに数日」から「仮説ごとに数分」に変えました。4 つのツールキットは、full payload capture / payload inspection / payload replay / attractor 自動検出をカバーします。

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
python scripts/detect_attractor.py --root .reyn/
```

すべての dogfood batch の後にこれを実行し、高レベルの scenario アウトカムでは見えないかもしれない attractor pattern をキャッチします。 scenario が (final output を生産して) 「完了」しながら、intermediate phase で 1 つ以上の attractor event を含む可能性があります。

### 他のシステムへの適用

core な要件は payload observability です: 各 call について LLM が受け取るものと生産するものを見られる必要があります。すべての LLM API プロバイダーはリクエスト/レスポンスペアのキャプチャをサポートしています。問題は、システムがすべての call を capture layer 経由でルーティングするかどうかです。

最低限の観測スタック:
1. すべての LLM call に対して `{call_id, system_prompt, messages, tools, response}` を構造化ログに書くキャプチャメカニズム
2. そのログを call ID とフィールドでフィルタリングして表示する inspection ユーティリティ
3. キャプチャした payload を変更して再実行できる replay メカニズム

Reyn の 3 つのツール (`REYN_LLM_TRACE_DUMP` / `dogfood_trace.py` / `llm_replay.py`) は 1 つの実装です。任意の LLM proxy layer (LiteLLM proxy / custom middleware) が同じ 3 つの機能を実装できます。 attractor detector はキャプチャした payload があれば任意のドメインで再構築できる post-processing step です。

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
docs/journal/dogfood/
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

複数 batch にわたる cross-batch 課題 (解決なしに追跡) については giveup tracker を使います: `docs/journal/dogfood/giveup-tracker.md`。

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

- **Batch 7** (`docs/journal/dogfood/2026-05-04-batch-7-post-infra-verify/retrospective.md`): 観測 infra 確立; care boundary 言語化; 推測スタック解体。
- **Batch 9** (`docs/journal/dogfood/2026-05-05-batch-9-fix-wave/retrospective.md`): Wrong-layer trap 発見; verify-first 原則確立。
- **Batch 10** (`docs/journal/dogfood/2026-05-05-batch-10-residual-fix-wave/retrospective.md`): Reproduce-first 原則確立; resolved-indirectly 分類の形式化。
- **Batch 13** (`docs/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`): Documented design audit 確立; 修正分類 discipline 形式化; simplicity smell test 言語化。
- **Batch 14** (`docs/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`): Production-grade phase 1 完了; full discipline 稼働。

full batch index と operational log については `docs/journal/dogfood/README.md` を参照してください。
