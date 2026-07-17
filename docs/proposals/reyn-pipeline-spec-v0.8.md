# Reyn Pipeline 機能 仕様書（ドラフト v0.8）

## 改訂履歴

**v0.1 → v0.2**: 中心概念を「制御プレーンと実行プレーンの分離」に再定式化。境界契約を新設。
**v0.2 → v0.3**: 名称を「Pipeline 機能」に。`retry` / `refine` 分離。`verify` 一般化。3 層フィードバックモデル。
**v0.3 → v0.4**: `impl` 削除。`budget` デフォルト化。`call` プリミティブ。承認単位を推移閉包に。
**v0.4 → v0.5**: 部品セット最小化。イベント面を hooks に統合。
**v0.5 → v0.6**: `for_each` 統合。sandbox 設定を DSL から削除。制御不可侵を規律に。`map` → `reshape`。
**v0.6 → v0.7**: 命名体系を再設計（部品は実行主体: `agent` / `expr` / `shell`、プリミティブはプログラミング構文: `for_each` / `match`）。※ `expr` は追補 8 で `transform` に再改名。

**v0.7 → v0.8**: **Reyn 全体アーキテクチャへの統合。** Pipeline 機能が独自に設計していた承認・観測・耐障害の機構を、Reyn の既存機構への準拠として全面的に書き直した。
- **実行モデル上の位置づけを明記**（§0.5）: Pipeline 実行機構は、エージェンティックループ（マルチエージェント・マルチセッション）とは独立した第二の実行モデルである。起動者はセッションに限らない。詳細は別文書「Reyn 実行モデル概説」。
- **承認は Reyn パーミッションシステムに委譲**（§7.1）: ツール単位の Allow / Deny / Ask モデルに Pipeline も乗る。独自の承認フローは持たない。
- **記録は 3 層モデルに準拠**（§7.2）: hooks（同期・限定・副作用なし）/ Global Journal（同期・最小限・リカバリー用）/ Audit Event（非同期・全量・観測用）。
- **人間介入に同期パターンを追加**（§7.4): パーミッションの Ask により、パイプライン分割なしで同期的な人間承認を挟める。v0.5 の「人間介入は必ずパイプライン分割」を訂正。
- **capabilities は起動元アイデンティティからの縮小のみ**（§3.4）: Reyn のアイデンティティモデル（capabilities は狭める方向のみ）に準拠。
- **`agent` ステップ・`for_each` の並列インスタンスは一時セッションとして実行される**（§3.7）: セッションのライフサイクルが Global Journal に記録されるため、Pipeline はクラッシュリカバリー / タイムトラベルの恩恵を追加コストなしで受けられる。
- 付録 A: 一般的な AI エージェントタスク 10 種に対する表現可能性の検証結果。

**v0.8 追補 1（identity）**: `agent` に `identity` フィールドを追加（§1.2）。静的リテラルのみ、省略時は起動元を継承。capabilities の縮小基準を identity 基準に明確化（§3.4, §3.7）。無所属セッションは導入しない。付録 A-10 を修正: 静的に宣言されたエージェント間協調は identity 指定で表現可能であり、排除されるのは動的な委任先選択のみ。

**v0.8 追補 2（付録 B）**: 生成用コンパクト仕様を新設。エージェント（LLM）が Pipeline を設計・実装する際に最小トークンで文法・制約・意味論を伝えるための最密表現（文法定義 + Hard rules + 1 行意味論 + 正準例）。

**v0.8 追補 3（生成シミュレーションによる修正）**: 付録 B のみを前提に Sonnet がユースケース（医療文書化・HR オンボーディング・多言語翻訳）から Pipeline を生成する実験を 4 ラウンド実施し、露出した仕様欠陥を修正:
- `match` の cases / default に `pass` を追加（文法の穴。本文 §2.4 も整合）
- rule 8 を全面改訂: **call は同期**であり、split は「終了 + イベント起動の継続」を意味する。人間の待ち時間を timeout で表現することを名指しで禁止。Canonical split（分割パターンの完全例）を追加
- schema 定義のミニ文法、パイプライン初期入力の規定を追加
- `for_each` に `over:PATH` を新設（実行時リストの正規の供給手段。items は静的リテラルのみ）。呼び出し先の先頭ステップのパイプデータ = 呼び出し地点のパイプデータ、を規定
- `call` の戻り値 = 呼び出し先の最終ステップの出力、失敗の伝播、refine `on_exhausted: fail | continue` の意味論の違い（N 回でハンドオフ = continue + 呼び出し元 match）を明記（§2.5, §4.2）
- carry_forward: スコープ内での参照必須（未参照は静的解析警告）、初回イテレーションは空として解決、refine 内は値で流し外部書き込みは通過後に、を規定（§4.2）
- `on_error: continue` は失敗要素を黙って落とすため、完全性が要る場合は abort / retry を使う旨を明記

**v0.8 追補 4（parallel）**: 異種並列プリミティブ `parallel`（`branches` + `collect`）を新設（§2.3）。`for_each` が同一ステップの実行時リストへの適用（同種並列）であるのに対し、`parallel` は定義時に静的に列挙された異なるステップ群の同時実行。`match` と対をなす（静的に閉じた分岐集合を、match は一つ選び、parallel は全部実行する）。collect 必須、入力は `results.<branch名>` の名前付きマップ。`on_error` デフォルトは `abort`（異種並列では一部欠損での集約が意味を持つケースが稀なため。continue 選択時は collect が欠損 branch の扱いを明示することを静的解析が要求）。コスト上界は branch 総和として for_each より正確に計算される。

**v0.8 追補 5（fold + 付録 C）**: 関数型プログラミング概念との対応検証（付録 C）を実施し、露出した欠落として逐次畳み込みプリミティブ `fold`（`init` + `do`）を新設（§2.4）。各要素の処理が累積結果に依存するリスト処理（ローリング要約・訳語一貫性の蓄積等）は `for_each` の隔離保証と正反対の意味論であるため、フラグ追加ではなく別プリミティブとした。do は `{item}` と `{acc}` を受け、出力が次の `{acc}` になる。逐次・順序固定・collect なし・要素失敗は全体失敗（部分続行は以降の全アキュムレータを暗黙汚染するため提供しない）。付録 C により、3 部品のエフェクト分類、parallel = Applicative、静的リテラル制約 = defunctionalization、Turing 不完全 = 全域言語（Dhall 系譜）という設計の理論的裏付けも記録した。

**v0.8 追補 6（パイプライン化の判断基準）**: LangChain 系との実務タスク比較から得られた指針を §0.6 として新設。Pipeline 化の判断基準は「探索の自由度」ではなく「**明確なゴール（検証可能な終端条件）の有無**」である。動的ツール選択・戦略切り替え・Plan-and-Execute・対話的修正といった探索的パターンは一つの `agent` ステップの内部で自由に行ってよく、制御プレーンは「達したかどうか」だけを見る。探索の手順を match / for_each / fold で表現しようとするのは粒度設定の誤りのシグナル。ゴール自体が不在・変動するタスクはエージェンティックループ（実行モデル 1）の仕事であり、型が見えて反復可能になった時点で Pipeline に昇格させる。付録 B にも粒度規則（Granularity rule）として追加。

**v0.8 追補 7（公開インターフェース）**: `input` 宣言と `description` を新設（§5.7）。パイプラインは登録・承認・他者からの呼び出しを前提とする「公開境界」であり、呼び出す側が本体を読まずに使えるようにする。`input` は省略可——実効入力インターフェースは `{ctx.*}` 参照から常に導出可能（静的解析項目 6 として追加）で安全性は宣言に依存しないが、宣言すれば契約の暗黙変更（本体編集による入力要求の変化）が不整合エラーとして即座に可視化される（モジュール境界は宣言・内部は推論、という一般原則の適用）。`description` はエージェントがパイプラインを道具として選択する判断材料（MCP ツール description と同役割）としてレジストリ・承認画面・エージェント向け一覧に表示される。

**v0.8 追補 8（tool 一般化・expr 改名・shell 入出力）**: (1) `expr` を `transform` に改名。名前が「式である」ことしか伝えず目的が読めなかったため、目的（データの決定論的な組み替え）で命名し直した。Vega-Lite の transform と同型の用法。旧名称（v0.6 以前の agent の旧名）との混乱は仕様策定段階のため実害なしと判断。transform のみ外部実行主体を持たない（制御プレーンの式評価器のステップ位置での利用）という特殊な位置づけを §1.1 に明文化。(2) `tool` 部品を新設: Reyn のツール群（MCP・shell 等、各ツールにパーミッション組み込み）を agent を介さず決定論的ステップとして静的に呼ぶ。name / args 構造は静的、パーミッション（Allow/Deny/Ask）はツール粒度で介在。`shell` は `tool: {name: shell}` の糖衣構文と再定義。(3) shell の入出力意味論を規定: 直前ステップの出力（パイプデータ）は JSON として **stdin** に渡され、引数テンプレートは小さな値の差し込み用、stdout が schema 検証を経て出力になる（§5.1 と Unix パイプの意味論の一致）。

---

## 0. 設計哲学

### 0.1 中心原則: 制御プレーンの決定論、実行プレーンの自由

```
制御プレーン: 順序・分岐・並列化・停止・状態管理・検証の執行
             → 完全に決定論的。DSL と Pipeline 実行機構のみで構成
実行プレーン: 各ステップの内部処理
             → 自由。単発 LLM でも、多ターンのエージェント（= Reyn セッション）でもよい
```

本機構のモチベーションは非決定性の排除ではない。**決定論的な制御構造を使えるようにすること**である。守るべきは一点: 実行プレーンの非決定性が制御プレーンに漏出しないこと。

### 0.2 原則一覧

1. **Pipeline 実行機構が実行状態の権威ある情報源である。**
2. **制御構造は決定論的である。** 同じ定義・同じ分岐値に対して、実行される経路は常に同一。
3. **ステップ内部は自由である。** 外部シグネチャ `Context -> Context` を守る限り、内部実装は問わない。
4. **非決定性は制御に「値」としてのみ影響できる。** LLM が制御構造そのもの（分岐先・ループ・トポロジー・呼び出し先）を生成・変更することはできない。
5. **合成規則は一つに絞る。**
6. **DSL は Turing 不完全である。**
7. **静的解析可能である。** 実行前に全経路・データフロー・非決定点・コスト上界を列挙できる。
8. **エージェントの完了申告とパイプラインの進行は常に分離される。**
9. **パイプライン定義は「何をするか」のみを記述する。** 何に起動され、誰に知らせ、何と連携するかは定義の外側（Reyn のイベント機構・パーミッションシステム）の責務。

### 0.3 規律ではなく構造 — ただし構造で守る範囲は DSL

本機構の価値は、安全性・決定論性を「正しく運用すれば守られる規律」ではなく「誤用そのものが構造的に成立しない制約」として実装したことにある。**人は正しく書き続けられない、という前提に立つ。**

この姿勢の具体的な現れ:
- 動作モードの宣言フィールドが存在しない: 「単発と宣言しつつツールを持つ」矛盾が書けない
- `retry` に carry_forward が存在しない: 純粋な再試行に状態を持ち越す誤用が書けない
- `refine` の carry_forward はパス明示列挙のみ: 「全部混ぜる」が書けない
- `shell` のコマンドは定義に静的に書かれる: 実行時に LLM がコマンドを組み立てる経路がない
- `call` の呼び出し先は静的リテラルのみ: LLM による動的ディスパッチが書けない
- budget 省略時はデフォルトが構造的に入る: 「書き忘れたら無制限」が存在しない
- `for_each` に `collect` が内包される: 集約漏れが書けない
- 非決定点は `agent` という部品名で構文上可視である
- capabilities は起動元からの縮小のみ: 「親より強い権限を持つ Pipeline」が書けない

**構造で守る範囲の線引き:** 構造的保証の対象は **DSL で書ける範囲**である。ステップ内部の実行環境から Reyn 自体の API を叩くといった実行環境レベルの悪用を検出・遮断する機構は実装しない（§3.2）。実行環境レベルの逸脱は規律と観測可能性（Audit Event）で扱う。

### 0.4 Dynamic Workflows との対比

DW との本質的な差は非決定性の量ではなく、**非決定性が制御を握っているか否か**である。

| 観点 | Dynamic Workflows | Pipeline 機能 |
|---|---|---|
| 制御構造の定義 | LLM が JS（Turing 完全）を生成 | DSL（Turing 不完全） |
| 制御の権威 | 生成されたスクリプト | Pipeline 実行機構 |
| ステップ内部の自由度 | 自由 | 自由 |
| 停止性 | スクリプト依存 | 実行機構が宣言的仕様で保証 |
| 実行中のトポロジー変更 | 可能 | 構造的に不可能 |
| 静的解析 | 事実上不可能 | 全経路・コスト上界を列挙可能 |

### 0.5 Reyn 全体アーキテクチャの中での位置づけ

Reyn は二つの実行モデルを持つ（詳細は別文書「Reyn 実行モデル概説」）:

```
実行モデル 1: エージェンティックループ（マルチエージェント・マルチセッション）
  非決定論的。チャットがメインループ。アイデンティティを持つセッションの集合。

実行モデル 2: Pipeline 実行機構（本仕様書の対象）
  決定論的な制御プレーンを持つ独立したワーカー。誰のセッションでもない。
```

**Pipeline 実行機構はセッションではない。** 起動者（依頼元）はセッションに限らず、以下のいずれでもよい:

- メインループ（セッション）からのツール呼び出し（`run_pipeline` 相当）
- 他の Pipeline からの `call`
- 外部イベント（hooks のメッセージプッシュ、承認イベント等）

これは原則 9（定義は「何をするか」のみを記述する）の実行主体レベルでの帰結である。もし実行機構が特定セッションに属するなら、外部イベント起動のたびにトリガー専用セッションを立てる迂遠な設計を強いられ、定義と起動経路の分離が崩れる。

二つの実行モデルの接続点:
- **モデル 1 → モデル 2**: セッションが Pipeline を依頼する（ツール呼び出し。初回はパーミッションシステムの承認対象）
- **モデル 2 → モデル 1**: `agent` ステップが一時セッションを子として起動する（§3.7）
- **モデル 2 → 外部**: Audit Event / hooks による観測・通知
- **外部 → モデル 2**: イベントによる Pipeline 起動

**メインエージェント自身が Pipeline を設計・実装することも想定される。** LLM が生成した定義も人間が書いた定義と同一の静的解析（§7.3）とパーミッション承認（§7.1）を通る。生成物の複雑度は DSL の表現力によって構造的に上限づけられるため、「生成スクリプトの品質チェックが人間の注意力に依存する」という DW の構造的問題は生じない。生成時にエージェントへ提示する最密仕様は付録 B。

### 0.6 パイプライン化の判断基準 — 探索の自由度ではなく、ゴールの有無で決める

あるタスクを Pipeline にすべきか、エージェンティックループ（実行モデル 1）に残すべきかの判断基準は、**タスクに明確なゴール（達成を検証できる終端条件）があるかどうか**であり、途中の手順に探索・試行錯誤が含まれるかどうかではない。

```
ゴールが明確       → Pipeline。探索は agent の箱の中に押し込み、
                     制御プレーンは「ゴールに達したか」（verify / refine / match）だけを見る
ゴール自体が不在・
実行しながら変わる → エージェンティックループの仕事。Pipeline に持ち込まない
```

**よくある境界の引き間違い:** 「実行しながら次の一手を LLM が決めるタスクは Pipeline で表現できない」と考えるのは、多くの場合**ステップの粒度設定の誤り**である。動的なツール選択・リトライ戦略の切り替え・計画立案と実行（Plan-and-Execute）・人間との自由な対話的修正は、いずれも**一つの `agent` ステップの内部**（境界契約の中）で自由に行ってよい。Pipeline のプリミティブ（match / for_each / fold）で探索の手順そのものを表現しようとした時点で、境界の引き方を疑うべきである。制御プレーンが管理するのは「どうやって達するか」ではなく「達したかどうか」だけである（§0.1 の帰結）。

**逆方向のシグナル:** Pipeline として書けないタスクに出会ったら、それは DSL の限界である前に「そのタスクはまだ決定論的な骨格に落とし込めるほど枯れていない」ことのシグナルである。探索段階のタスクはエージェンティックループで実施し、型が見えて反復可能になった時点で Pipeline に昇格させる——これが二つの実行モデルの正しい役割分担である。

---

## 1. コアモデル

### 1.1 部品（Step）— 実行主体で命名された 3 部品

すべての部品は同一の外部シグネチャ `Context -> Context` を持つ。部品名は**それを実行する主体**を表す。

| 部品 | 実行主体 | 非決定性 | 副作用 | 役割 |
|---|---|---|---|---|
| `agent` | LLM / エージェント（Reyn セッション） | **あり** | tools による | 変換・生成・判断 |
| `transform` | Pipeline 実行機構自身（式評価器） | なし | **なし（純粋）** | コンテキストの決定論的な組み替え・検証 |
| `tool` | ツール実行系（パーミッション介在） | なし | **あり** | ツールの静的呼び出し。`shell` は `tool: {name: shell}` の糖衣構文 |

「非決定性の有無 × 副作用の有無」で: 非決定論 = `agent`、決定論・純粋 = `transform`、決定論・副作用あり = `tool`（`shell` を含む）。定義を眺めるだけで `agent` の箇所が非決定点として視覚的に判別できる。

**transform の特殊な位置づけ:** 3 部品のうち `transform` のみ外部の実行主体を持たない。その実体は、until / match.on / verify.condition と共通の**制御プレーンの式評価器を、ステップの位置でも使えるようにしたもの**である。目的は 3 つ: (1) ステップ間の出力形状の食い違いを埋めるグルー（集約・連結・詰め替え）を、非決定性（agent）やプロセス起動+パーミッション（tool）を浪費せずに行う、(2) until / match が参照する判定値（`{passed: bool}` 等）の生成、(3) 静的解析上「完全に無害」（副作用なし・外部実行なし・パーミッション不要・budget 不要）と分類できる唯一の経路の提供。transform が無ければ「フィールドの並べ替えだけのために LLM を呼ぶかプロセスを起動するか」の二択になり、部品の純度（agent は判断だけ、tool は副作用だけ）が崩れる。名称は Vega-Lite の transform（宣言的スペック内の決定論的なデータ組み替え）と同型の用法である。

**旧部品の表現方法:**
- 旧 `fetch` / `store` → `tool` / `shell`（頻出パターンはプリセットとして提供）
- 旧 `validate` → `transform` による `{passed: bool, ...}` の出力
- 旧 `emit` → Audit Event の自動発行（§7.2）

**規約:**
- LLM 呼び出しを内部に持てるのは `agent` のみ。
- `transform` は副作用を持たない純粋な評価に限る。I/O が必要な決定論的処理は `tool` / `shell` へ。
- 部品の追加はプラグインとして可能だが、直交性を壊す部品は追加しない。

### 1.2 agent の identity と動作モード

**identity（静的リテラル、省略可）**: `agent` ステップがどのエージェント（アイデンティティ = ロール + capabilities）のセッションとして実行されるかを指定する。

```yaml
- agent:
    identity: reviewer           # 静的リテラルのみ。省略時は起動元のアイデンティティを継承
    prompt: "review this diff"
    capabilities: {tools: [read_file, run_tests]}   # identity の capabilities からさらに縮小
```

- **identity は静的リテラルでなければならない**（`call.pipeline` と同じ規則）。実行時の値による委任先選択は書けない。
- capabilities の縮小基準（§3.4）は「指定された identity の capabilities」である。省略時は起動元の identity。
- これにより、**静的に宣言されたエージェント間協調**（例: 実装は coder、レビューは reviewer という異なるロールの協調）が一級市民として表現できる。Reyn のマルチエージェントモデル（アイデンティティ = ロールの違い）を Pipeline が素直に活用する形である。排除されているのは「実行時に委任先を動的に発見・選択すること」のみ（付録 A-10）。

**動作モード（宣言ではなく導出）**: 動作モードの指定フィールドは存在しない。`capabilities.tools` から導出される。

| capabilities.tools | 導出される動作 |
|---|---|
| なし / 空 | 単発の LLM 呼び出し |
| 非空 | ツールを使う多ターンのエージェンティックループになりうる |

### 1.3 ランタイムプリミティブ

部品ではなく Pipeline 実行機構のネイティブ機能。

| プリミティブ | 役割 | 分類 |
|---|---|---|
| `for_each`（`do` + `collect`） | 同一ステップの並列適用と集約（同種並列） | 構成 |
| `parallel`（`branches` + `collect`） | 異なるステップの同時実行と集約（異種並列） | 構成 |
| `fold`（`init` + `do`） | リストの逐次畳み込み（アキュムレータを通す順次処理） | 構成 |
| `match`（`on` / `cases` / `default`） | 閉じた分岐先集合へのパターンマッチ | 構成 |
| `call` | 名前付きパイプラインの呼び出し | 構成 |
| `verify` | 構造的ゴールの検証（ステップに付与） | 検証 |
| `retry` | 構造的失敗の再試行（ステップに付与） | 反復 |
| `refine` | 意味的ゴールへの反復接近（パイプラインに付与） | 反復 |

`match` と `parallel` は対をなす: どちらも**静的に閉じた分岐の集合**を宣言し、`match` はその一つを選んで実行し、`parallel` は全部を同時に実行して集約する。`for_each` と `fold` も対をなす: どちらもリストを処理するが、`for_each` は要素間の独立を構造的に保証して並列化し、`fold` は要素間の依存（アキュムレータ）を明示して逐次化する。

---

## 2. 合成規則

### 2.1 線形パイプライン（基本形）

```yaml
pipeline: <name>
steps:
  - <step>
  - <step>
```

各ステップは直前のステップの出力のみを暗黙入力として受け取る（§5.1）。制御プレーンの進行は同期的である: 前のステップの完了を待って次に進む。

### 2.2 for_each（do + collect）

並列実行と集約は単一のブロックで宣言する。集約（`collect`）は必須であり、集約漏れは構文的に書けない。

```yaml
steps:
  - agent: {prompt: "list suspicious files", schema: file-list}
  - for_each:
      max_parallel: 10
      on_error: continue          # continue | abort | retry(n)
      do:
        agent:
          prompt: "investigate and verify: {item}"
          capabilities: {tools: [read_file, sandboxed_shell]}
          budget: {max_turns: 15}
      collect:
        transform: {value: '{passed: all(results, r -> r.verified), items: results}', output: audit_check}
```

- リストの供給源は 3 通り: `over`（ctx のパスから決定論的に取得）、`items`（静的リストの直接指定）、いずれも省略時は直前ステップの出力（リストであることを静的検査で要求）。`items` の要素は静的リテラルのみであり、実行時のリストは `over` で渡す。
- `do` の各並列インスタンスは**一時セッションとして起動され、完了後に閉じられる**（§3.7）。Reyn のマルチセッションモデル（複数セッションの並列動作）にそのまま乗る。
- 各インスタンスは隔離される。自身の `{item}` + ストアへの読み取り専用アクセスのみ。相互通信・書き込みは不可。
- `collect` は全インスタンスの完了（または `on_error` ポリシーに基づく部分完了）後、結果リスト `results` を入力として一度だけ実行される。`collect` に置けるのは `transform` / `shell` / `agent`。

### 2.3 parallel（branches + collect）— 異種並列

構造の異なる複数の処理を同時に実行し、集約する。`for_each` が「同一ステップを実行時のリストに適用する」のに対し、`parallel` は「**定義時に静的に列挙された、異なるステップ群**を同時に実行する」。

```yaml
steps:
  - parallel:
      on_error: abort             # デフォルト abort。continue | retry(n)
      branches:
        tests: {call: {pipeline: run-tests, pass: [impl], output: _}}
        docs:  {call: {pipeline: generate-docs, pass: [impl], output: _}}
      collect:
        transform:
          value: '{test_result: results.tests, docs_result: results.docs}'
          output: merged
```

- branch の集合は定義時に静的に閉じている（`match` の cases と同じ性質。`match` は一つを選び、`parallel` は全部を実行する）。
- 各 branch は隔離される（`for_each` の do インスタンスと同じ規則: 読み取り専用 ctx、相互通信不可）。`agent` を含む branch は一時セッションとして実行される（§3.7）。
- `collect` は必須。入力は **`results` という名前付きマップ**（`results.<branch名>`）。`for_each` の `results`（順序リスト）と予約名を共有し、コンテナの形だけが異なる。
- **`on_error` のデフォルトは `abort`**（`for_each` のデフォルトとは非対称）。異種並列では各 branch が固有の役割を持つため、一部欠損での集約が意味を持つケースは稀である。`continue` を選ぶ場合、collect は欠損 branch（`results.<name>` が absent）の扱いを明示しなければならない（静的解析が要求する）。
- コスト上界（§7.3）は branch 数が静的に既知のため、**各 branch のコストの総和**として `for_each` より正確に計算される。

### 2.4 fold（init + do）— 逐次畳み込み

リストを**順番に**処理し、各要素の処理が累積結果（アキュムレータ）に依存する場合に使う。`for_each` と対をなす: `for_each` は要素間の独立を構造的に保証して並列化し、`fold` は要素間の依存を明示して逐次化する。

```yaml
- fold:
    over: "ctx.chapters"          # over | items | パイプデータ（for_each と同じ三択）
    init: '{glossary: {}, summaries: []}'   # 初期アキュムレータ（transform 構文で記述）
    do:
      agent:
        prompt: "Translate {item}, keeping terminology consistent with the accumulated glossary: {acc.glossary}"
        schema: chapter-result-schema
        verify: [{schema: chapter-result-schema}]
    output: final_acc
```

**意味論:**
- 各イテレーションの `do` は `{item}`（現在の要素）と `{acc}`（累積値）を受け取り、その出力が次のイテレーションの `{acc}` になる。最終イテレーションの出力が `output` に格納される。
- 実行は逐次・順序固定（リストの順）。`agent` を含む場合、一時セッションは 1 個ずつ順に起動・終了する（§3.7）。
- `collect` は存在しない（畳み込みの結果そのものが出力であり、集約は不要）。
- 途中要素の失敗は `retry`（ステップ付与）消化後、fold 全体の失敗となる。部分結果で続行するモードは提供しない——順序依存の処理で要素を飛ばすことは、以降の全アキュムレータを暗黙に汚染するため。
- コスト上界: リスト長 × (1 + retry.max) × budget。`over` の場合リスト長は実行時に決まるため、上界計算には `max_items` の宣言（省略時はランタイムデフォルト）を用いる。

**代表ユースケース**: 長文書のローリング要約（各チャンクの要約が既存要約を踏まえる）、多章翻訳での訳語一貫性の蓄積、履歴を順に読んだ状態の積み上げ。これらは `for_each`（rule: 兄弟間通信の禁止）では構造的に表現できない。

### 2.5 match（条件分岐）

```yaml
steps:
  - agent: {prompt: "classify: bug | feature | question", schema: label-schema, output: label}
  - match:
      on: "label.value"
      cases:
        bug:      {pipeline: bug-triage}
        feature:  {pipeline: feature-spec}
        question: {pipeline: answer}
      default:    {pipeline: manual-review}
```

- 分岐先は名前付きパイプラインへの参照のみ。分岐先集合は静的に閉じている。分岐先へ渡す名前付きストアは `pass` で明示列挙する（`call` と同じ規則、§5.2）。
- LLM が返せるのはラベル（値）のみで、分岐構造・分岐先を生成することはできない（原則 4）。
- ラベル不一致は `default` へ。未定義なら実行時エラーとして停止。

### 2.6 call（パイプラインの部品化と呼び出し）

```yaml
- call:
    pipeline: review-only        # 静的リテラルのみ。テンプレート展開不可
    pass: [impl_result]          # 呼び出し先に渡す名前付きストア（明示列挙）
    output: review_consensus
```

- `call` は一つのステップとして振る舞う: 入力はパイプデータ + `pass` で明示列挙されたストア。名前空間は呼び出し単位で分離。**戻り値は呼び出し先の最終ステップの出力**であり、`output` で指定した名前に格納される。呼び出し先の失敗（refine の `on_exhausted: fail` を含む）はこのステップの失敗として伝播する。呼び出し先の refine 消化後にハンドオフ等の分岐をしたい場合は、呼び出し先を `on_exhausted: continue` とし、呼び出し元の `match` で判定フィールドを分岐させる。
- **`pipeline:` は静的リテラルでなければならない。** 実行時の値による行き先選択は match の専任事項。
- 再帰参照は静的検査で検出し、`max_depth` の明示宣言がない限り拒否。
- **refine の範囲指定問題は call で解決する**: 部分反復したい範囲を別パイプラインに切り出して refine を付与する。

---

## 3. 境界契約（Boundary Contract）

すべての `agent` は Pipeline 実行機構に対して以下の契約を負う。

### 3.1 入出力契約と verify

- 入力: 実行機構が渡した Context のみ。出力: 単一の Context。
- 出力は `verify` によって実行機構が検証する。**LLM の自己申告ではなく実行機構の検証が権威。**

`verify` は**構造的ゴール**（決定論的手続きで真偽判定できる性質）の検証機構である。

```yaml
- agent:
    prompt: "implement the function"
    capabilities: {tools: [read_file, write_file]}
    output: impl_result
    verify:
      - schema: impl-schema
      - condition: "ctx.impl_result.diff_lines < 500"
      - shell: {command: "cargo test", timeout: 300s}
    retry: {max: 2}
```

構造的ゴールの例: schema 適合、値域制約、参照整合性、外部検証可能な性質（コンパイル・テスト・パース）、量的制約。

### 3.2 制御不可侵（規律）

ステップ内部から以下を行うことは**設計上サポートされず、規約違反である**:

- 別パイプラインの起動、match / call / retry / refine の操作
- 自身の budget の変更
- for_each の兄弟インスタンスとの通信
- パイプライン定義の読み取り・変更

実行機構はこれらの操作を行う API をステップに**提供しない**。ただし、実行環境レベルでの迂回を能動的に検出・遮断する機構は実装しない（§0.3 の線引き）。制御への正当な影響経路は出力の**値**のみである。

### 3.3 資源上限（budget）— 常にオプショナル、常に有限

```yaml
budget:
  max_turns: 15
  max_tokens: 200000
  timeout: 600s
  on_exhausted: partial      # partial（成果を出力し verify にかける）| fail
```

- 省略時は 3 段デフォルト（ランタイム設定 → パイプラインの `defaults.budget` → ステップ）。「宣言がない = 無制限」は存在しない。
- 停止の最終権威は実行機構。

### 3.4 能力スコープ（capabilities）— 起動元からの縮小のみ

```yaml
capabilities:
  tools: [read_file, grep, sandboxed_shell]
```

- 内部エージェントが使えるツールの allowlist。宣言外のツール呼び出しは拒否される。
- **Reyn のアイデンティティモデルに準拠する**: セッションの capabilities は既存アイデンティティから狭める方向のみ指定可能である。Pipeline の `agent` ステップの縮小基準は **`identity` で指定されたエージェントの capabilities**（identity 省略時は起動元のアイデンティティ）であり、その部分集合でなければならない。静的解析（§7.3）はこれを実行前に検査する。「親より強い権限を持つ Pipeline」は構造的に書けない。
- サンドボックスの構成は Reyn ランタイム全体の構成に従う（サンドボックスバックエンドは抽象化され、複数実装が存在する）。DSL には重複した設定層を持たない。

### 3.5 観測可能性 — Audit Event への準拠

- 内部エージェントの全ターン、および Pipeline 実行のあらゆるイベントは、**Reyn の Audit Event（非同期・pub/sub・OTEL 対応）として発行される**。Pipeline 機能が独自のトレース基盤を持つことはない。
- 箱の中は制御上はブラックボックスだが、**記録上はブラックボックスではない**。

### 3.6 tool / shell: 決定論的なツールの静的呼び出し

Reyn のツール群（MCP ツール・shell・その他。各ツールにパーミッション設定が組み込まれている）を、`agent` を介さず**決定論的なステップとして直接呼ぶ**部品。実行時には既存のパーミッションシステム（Allow / Deny / Ask）がツール粒度でそのまま介在する——Ask に設定されたツールなら §7.4 の同期的な人間介入がここで自然に発生する。

```yaml
- tool:
    name: mcp_github_create_issue        # 静的リテラル
    args: {title: "{ctx.review.summary}", repo: "tya5/reyn"}   # 値の差し込みのみ
    schema: issue-result-schema
    output: created_issue
```

**規約:**
- **`name` は静的リテラル、`args` の構造も定義に静的に書かれる。** テンプレート変数による値の差し込みは可能だが、どのツールをどんな引数構造で呼ぶかを実行時に LLM が組み立てることはできない。同じツールでも、`agent` の capabilities 経由で呼ばれれば非決定論（LLM が呼び出しを組み立てる）、`tool` ステップなら決定論（定義に固定）——この区別が部品名によって明示される。
- `tool` は内部から LLM を呼び出さない（§3.2 と同じ規律）。

**shell（`tool: {name: shell}` の糖衣構文）と入出力の意味論:**

```yaml
- shell:
    command: "chunk_docs.sh --strategy {ctx.strategy_choice.strategy}"
    timeout: 300s
    schema: chunks-schema
    output: chunked_docs
```

- **stdin**: 直前ステップの出力（パイプデータ）が JSON として stdin に渡される。DSL 全体の「各ステップは直前の出力を暗黙入力とする」（§5.1）と Unix パイプの意味論が一致する。大きな構造化データはこの経路で受け取り、コマンドライン引数への文字列展開（エスケープ・サイズの両面で破綻する）を避ける。
- **引数テンプレート**: ctx からの小さな値（戦略名・ID・フラグ）の差し込み用。
- **stdout**: `schema` で検証され、パイプデータ / `output` になる。非ゼロ終了はステップ失敗（§6）。
- 実行環境の隔離は Reyn のサンドボックス構成に従う。`tool` 一般では stdin の代わりに `args` への構造化された引数渡しとなる（ツールの入力スキーマに従う）。

**判断と実行の分離の標準形（`agent` → `match` → `tool`/`shell`）:** 非決定論的判断は `agent` に外出しし、`match` で分岐し、各分岐先は `tool` / `shell` による機械的実行のみを行う（原則 4 の応用。RAG 前処理のチャンク戦略選択が代表例）。

### 3.7 実行プレーンとセッション — 一時セッションモデル

- **`agent` ステップは、独立した一時 Reyn セッションとして起動され、ステップ完了とともに閉じられる。** 一時セッションは `identity` で指定されたエージェント（省略時は起動元のアイデンティティ）に属する。エージェントに属さない「無所属セッション」という概念は導入しない——既存のセッションモデル（セッションは必ずエージェントに属する）をそのまま流用することで、パーミッション・Journal・Audit Event のすべてが例外なく適用される。`for_each` の各並列インスタンスも同様（並列個数分の一時セッション）。
- セッションの生成・終了は Reyn の **Global Journal**（同期・最小限のイベントを記録するリカバリー基盤）に既存の仕組みとして記録される。したがって **Pipeline は Global Journal に新しい記録要求を追加しない**まま、クラッシュリカバリー / タイムトラベル（グローバル含む）の恩恵を受けられる。
- セッション分離により、複数の `agent` ステップ間（例: 実装者とレビュアー）は互いの内部会話を構造的に見られない（§4.2）。
- §5.5「ステップ内部の会話状態はステップ完了で破棄」は、実装上「セッションのライフサイクルがステップのライフサイクルと一致する」ことの帰結である。
- Pipeline 実行機構自身の制御プレーン状態（現在のステップ、refine のイテレーション等）の永続化方式は未解決事項（§10）。

---

## 4. フィードバックループ: 3 層モデル

| 層 | 機構 | ゴールの種類 | 検証者 | 権威 |
|---|---|---|---|---|
| 層 1 | エージェント内部のターンループ | 意味的ゴールへの自己接近 | エージェント自身 | **非権威的** |
| 層 2 | `verify` + `retry` | **構造的ゴール** | 実行機構 | 権威 |
| 層 3 | `refine` | **意味的ゴール** | 検証者 agent（判定）+ 実行機構（執行） | 権威 |

- 層 1 の自己検証はパイプラインの進行判断の根拠にならない（原則 8）。
- 意味的ゴールの検証を層 2 に混ぜてはならない。
- **反復が意味を持つ変化源は 2 つ**: (a) LLM の非決定性 → `retry`、(b) 持ち越された情報 → `refine`。「外部世界の変化」（ポーリング）はスコープ外で、イベント駆動起動（§0.5）が担う。

### 4.1 retry: 構造的失敗の再試行

```yaml
- agent:
    prompt: "..."
    verify: [{schema: review-schema}]
    retry:
      max: 2
      feedback: "your output failed verification: {verify_failure}"
```

- 再試行は新しい実行（新しい一時セッション）として起動。前回の内部会話は引き継がれない。渡されるのは **verify の失敗内容のみ**。
- **carry_forward という概念は存在しない。**
- verify の成否のみがトリガー。判定条件を選ぶ余地はない。

### 4.2 refine: 意味的ゴールへの反復接近

```yaml
pipeline: implement-review-test
steps:
  - call: {pipeline: implement-only, pass: [task_spec, feedback_summary], output: impl_result}
  - call: {pipeline: review-only, pass: [impl_result], output: review_consensus}
  - transform: {value: '{feedback_summary: join(review_consensus.feedbacks, "\n---\n")}', output: feedback_summary}
refine:
  until: "ctx.review_consensus.passed"
  max_iterations: 5
  carry_forward: ["feedback_summary"]
  on_exhausted: fail          # fail | continue
```

**判定タイミングと再実行の意味論:**
- `until` はスコープ内 `steps` が**最後まで正常完了した時点**の Context に対して評価される。false なら steps の**先頭から**、carry_forward で列挙された値のみを持って再実行する。イテレーションは逐次であり並列化されない。
- **層 2 の失敗は refine に吸収されない。** refine が拾うのは「全ステップ正常完了かつ until が false」のケースのみ。

**規約:**
- `until` は決定論的述語のみ。判定は検証者 agent が行ってよいが、**執行は実行機構**。`max_iterations` は必須。
- **refine のスコープは `agent` を 1 つ以上含まなければならない**（call 先の推移閉包を含めて判定。違反は静的検査エラー）。
- **`carry_forward` は必須の宣言**（空リストも明示。空は警告——それは retry で書くべきでは、というシグナル）。持ち越された値は**スコープ内のどこかで参照されていなければならない**（未参照は静的解析が警告する。参照されない持ち越しは、refine を LLM の出力揺れに賭けるだけの実質 retry に退化させる）。初回イテレーションでは持ち越し値は空として解決される。
- refine スコープ内では値としての出力を優先し、外部への書き込み（ファイル・リポジトリ等）は refine 通過後のステップに置くことを推奨する（イテレーションごとに副作用が再実行されることを避ける）。
- 非決定性の所在は構文には現れない。静的解析が `until` 述語の来歴を導出し、承認時に提示する。

**セッション分離**: 実装者とレビュアーは別々の一時セッション（§3.7）であり、互いの内部会話を構造的に見られない。レビュアーが実装を書き換えられない性質は capabilities の write 不許可で担保される。

**人間のレビューを挟む場合**: §7.4 の 2 パターン（同期 Ask / 非同期分割）を用途で使い分ける。

---

## 5. コンテキストと状態

### 5.1 パイプデータ（デフォルト）

各ステップは直前のステップの出力のみを受け取る。全履歴の暗黙参照手段は存在しない。

### 5.2 名前付きストア（明示的読み書き）

- 書き込み: `output: <name>`。読み込み: `{ctx.<name>}` 参照。
- スコープは単一パイプライン実行。match 先・call 先へは `pass` の明示宣言が必要。
- 動的なキー生成は不可。全読み書きが静的に追跡可能であること。

### 5.3 イテレーション間の持ち越し

`refine.carry_forward` のみ。`retry` には存在しない。

### 5.4 for_each（do）・parallel（branches）中の状態

- 各並列インスタンス / branch（一時セッション）: 自身の入力（`{item}` / 呼び出し時点のパイプデータ）+ ストアへの読み取り専用アクセス。書き込み・相互通信は不可。書き込みは `collect` の結果として行う。

### 5.5 ステップ内部の会話状態

- agent の内部会話はステップ実行内に閉じ、完了とともに破棄される（一時セッションのライフサイクルと一致。Audit Event としての記録は残る）。retry / refine の再実行でも引き継がれない。

### 5.6 出力スキーマ

スキーマは agent の外、パイプライン定義とは独立に定義・参照される。

```yaml
# schemas/review-schema.yaml
name: review-schema
fields:
  approved: {type: bool, required: true}
  feedback: {type: string, required: true}
```

- スキーマが決めるもの: match が参照できるフィールドの型、until / transform / carry_forward が参照できるパス。静的解析が経路グラフを構築するための前提情報。
- schema 省略時、出力は自由形式となり、match / until / carry_forward から直接参照できない。

### 5.7 パイプラインの公開インターフェース — input 宣言と description

パイプラインは登録・承認され、他者（セッション・他パイプライン・外部トリガー）から呼び出される「公開境界」である。呼び出す側が**本体を読まずに使える**ように、以下の 2 つを定義の先頭に付属できる。

```yaml
pipeline: review-only
description: |
  実装 diff をレビューし、承認可否と指摘一覧を返す。
  入力: impl_result（実装結果）。出力: review_consensus（passed / feedbacks）。
input:
  impl_result: {schema: impl-schema}
steps:
  - ...
```

**input 宣言（省略可）:**
- パイプラインが `{ctx.*}` として参照する外部由来の値の名前と型を宣言する。
- **省略時は静的解析が実効入力インターフェースを導出する**——動的キー生成が禁止されているため（§5.2）、本体が参照する `{ctx.*}` は全て静的に抽出可能であり、導出インターフェースはレジストリ・承認画面に常に表示される。安全性は宣言の有無に依存しない（誤った `pass` は登録時に必ず検出される）。
- **宣言する価値は境界の安定化にある**: 宣言があれば、本体内部の編集（プロンプトに `{ctx.new_field}` を足す等）による**契約の暗黙変更**が、`input` との不整合として即座にエラーになる。契約変更は `input` の差分として diff 上に明示的に現れ、承認（推移閉包ハッシュ）でも識別できる。内部用の小さいパイプラインは省略してよく、共有・公開されるパイプラインには宣言を推奨する。
- 検証規則: 宣言がある場合、本体の参照を宣言がカバーしていなければエラー、宣言したが未参照の項目は警告。トリガー設定・call/match の `pass` の検証は、宣言・導出を問わず実効入力インターフェースに対して行う。

**description（省略可、推奨）:**
- パイプラインの目的・入出力の要約を自然言語で記述する。
- 単なるコメントではなく、**メインエージェントがパイプラインを道具として選択する際の判断材料**である（MCP のツール description と同じ役割）。レジストリ・承認画面に加え、エージェントに提示されるパイプライン一覧に載る。
- 実行の意味論には一切影響しない（description のみの変更が承認ハッシュの再承認を要求すべきかは §10 の検討事項）。

---

## 6. エラーと停止の意味論

| 事象 | 挙動 |
|---|---|
| verify 失敗 | `retry` → 消化後、ステップ失敗として停止。**refine には吸収されない** |
| budget 到達 | `on_exhausted: partial | fail`。partial でも verify は実施される |
| match ラベル不一致 | `default` へ、未定義なら停止 |
| refine 消化 | `on_exhausted: fail | continue` |
| for_each（do）部分失敗 | `on_error` に従う。continue の場合、失敗要素を除いた results で collect が実行される |
| parallel の branch 失敗 | `on_error` に従う（デフォルト abort）。continue の場合、collect は欠損 branch の扱いを明示している必要がある |
| fold の要素失敗 | ステップ付与の `retry` 消化後、fold 全体の失敗。部分続行モードはない（順序依存処理での要素スキップは以降の全アキュムレータを暗黙に汚染するため） |
| tool / shell の失敗（Deny・非ゼロ終了・timeout） | ステップ失敗として retry → エラー意味論に従う |
| call 先の失敗 | 呼び出し元のステップ失敗として伝播 |
| パーミッション Deny | 該当ステップの失敗として扱う |
| 外部からの停止要求 | ステップ境界での安全停止、または即時 kill（モード指定） |

すべての事象は Audit Event として発行される。エラーの通知・エスカレーション・後続処理は購読側の責務。

**原則: すべての停止は実行機構の決定である。**

---

## 7. Reyn 既存機構への準拠

Pipeline 機能は承認・観測・耐障害・通知の独自機構を持たない。すべて Reyn の既存機構に準拠する。

### 7.1 承認 — パーミッションシステム

Reyn のパーミッションシステムは、ツールごとに **Allow / Deny / Ask** を事前設定でき、ツール実行時に介在する（Ask の場合はユーザーへの同期的な承認割り込みが発生する）。

- Pipeline の起動はツール呼び出し（`run_pipeline` 相当）として、このパーミッションシステムの対象になる。
- **承認の単位は定義の推移閉包のハッシュ**（match 分岐先・call 先を含む全定義）。定義が変われば別物として扱われ、初回承認が再度走る。呼び出し先だけを差し替えて承認済みを装うことは構造的にできない。
- Pipeline 内部の各ステップは、Pipeline 全体の承認に集約される（capabilities 宣言の範囲内である限り、ステップごとの個別承認は発生しない）。ただし特定ツールのパーミッションが Ask に設定されていれば、§7.4 の同期介入が発生する。

### 7.2 記録 — 3 層モデル

| 機構 | 発行方式 | 対象 | Pipeline との関係 |
|---|---|---|---|
| **hooks** | 同期・限定 | Reyn プリミティブな状態変化 | Pipeline 由来の状態変化にもフックを登録できる。フックは服属先の状態に副作用を持てず、できるのは **shell 起動 / 特定セッションへのメッセージプッシュ** のみ。実行フローへの介入手段ではなく、同期的な通知手段である |
| **Global Journal** | **同期** | リカバリー / タイムトラベルに必要な**最小限** | Pipeline は新しい記録要求を追加しない。一時セッションの生成・終了（§3.7）が既存の記録対象としてカバーされる |
| **Audit Event** | 非同期・pub/sub（OTEL） | あらゆるイベント、制約なし | Pipeline 実行の全イベント（開始/完了/verify/retry/refine/budget 等）と agent の全ターントレースはここに発行される |

### 7.3 静的解析

パイプライン登録時に生成:

1. **経路グラフ**: match の全分岐 × call の展開 × refine の反復構造を含む全実行経路
2. **非決定点マップ**: 全 agent の位置・動作クラス・モデル・実効 budget
3. **データフローグラフ**: ストア・carry_forward・pass の読み書き関係。refine の until 述語の来歴を含む
4. **コスト上界**: `Σ (max_parallel × max_iterations × (1 + retry.max) × budget.max_turns)`。call の入れ子は積。`parallel` は branch 数が静的に既知のため各 branch のコストの総和として計算される（for_each より正確）。`fold` は `max_items`（over 使用時。省略時はランタイムデフォルト）× budget で上界を取る。budget はデフォルトにより常に有限
5. **能力の総和と縮小検査**: 推移閉包全体の tools の合併、および全 agent の capabilities が起動元アイデンティティの部分集合であることの検査（§3.4）
6. **実効入力インターフェース**: 本体が参照する `{ctx.*}` から導出される入力の名前・型の一覧（§5.7）。`input` 宣言がある場合は宣言との整合検査（未カバーはエラー、未参照宣言は警告）。レジストリ・承認画面・エージェント向けパイプライン一覧に表示される

### 7.4 人間介入の 2 パターン

**同期パターン（パーミッション Ask 経由）**: 人間の承認を要するツール（承認要求ツール、あるいは特定の shell コマンド）のパーミッションを Ask に設定し、パイプラインのステップとして呼ぶ。実行時にパーミッションシステムが同期的にユーザー承認を割り込ませ、承認されれば続行、拒否は失敗として扱われる。**パイプライン分割は不要。** 単純なゴーサイン向き。

**非同期パターン（パイプライン分割 + イベント接続）**: パイプライン A の完了（Audit Event / hooks のメッセージプッシュで通知）→ 人間が任意のタイミングで判断 → 承認イベントがパイプライン B を起動する。レビューに時間がかかる場合・複数人が関与する場合向き。

### 7.5 マルチエージェント協調

Pipeline から他のエージェント（セッション）への疎な連携は、hooks の**メッセージプッシュ**で表現できる。重要なのは、これが A2A 的な動的委任とは異なることである:

- 送信側は「このセッションにメッセージを送る」という**静的に決まった宛先**に投げるだけ（送信側の制御構造は決定論のまま）
- 受信側が何をするかは**受信側セッションの自律的判断**であり、送信側の Pipeline 定義とは無関係

動的な連携に見えるものが「静的な送信 + 受信側の自律性」に分解され、原則 4 と静的解析可能性が保たれる。

---

## 8. スコープ外（意図的な非目標）

- 実行中のトポロジー動的変更
- 任意コードによるオーケストレーション、DW への委譲経路
- ステップ内部の決定論化
- ステップ間の暗黙の会話共有
- LLM による停止・進行の直接執行（判定値の提供のみ可）
- 無制限の再帰・ネスト
- **外部状態のポーリング・待機**: 「待つ」はイベント駆動起動、「繰り返す」は retry / refine
- **通知・連携・トリガーの定義への混入**（原則 9）
- **実行環境レベルの悪用の検出・遮断**: 規律と Audit Event で扱う
- **承認・トレース・リカバリー・サンドボックス構成の独自実装**: すべて Reyn 既存機構に準拠（§7）
- **実行時の動的なエージェント間委任（A2A 型）**: `call.pipeline` / `agent.identity` の静的リテラル制約により排除。静的に宣言されたエージェント間協調（identity 指定）と疎な連携（§7.5）は表現可能

---

## 9. 旧スキル機能からの教訓

旧スキル機能は DAG 構造でフェーズを表現しつつ、失敗情報を次の実行の入力に暗黙的に混ぜてロールバックする設計だった。問題はフェーズ内部が非決定的だったことではない。**境界契約が存在せず、非決定性が制御プレーンに漏れていたこと**である。

1. **遷移判断の漏出**: ロールバック判断が LLM の推論内部にあり、ランタイムは傍観者だった。→ refine の until / max_iterations が回収
2. **暗黙の状態流入**: 失敗情報が宣言なしに混入し、データフローが追跡不能だった。→ carry_forward の明示列挙が回収
3. **再試行と改善の混同**: 異なる意味論が同じ機構に同居していた。→ retry / refine の分離が回収

**「構造は決定論的に見えるが、制御の実権が箱の中にある」**——このパターンを DSL の範囲で構造的に不可能にするのが境界契約（§3）と 3 層モデル（§4）である。

---

## 10. 未解決事項

- [ ] **Pipeline 実行機構の制御プレーン状態の永続化方式**（現在のステップ・refine イテレーション等。Global Journal は最小限主義のため対象外、Audit Event は非同期のため正確性の保証がない。第三の同期的永続化が必要か、Journal の対象に最小限の Pipeline 状態遷移を加えるかの判断）
- [ ] **エージェントの永続状態と一時セッションの関係**: エージェントがセッション横断の状態（記憶・学習等）を持つ場合、Pipeline の一時セッションがそれに書き込むべきか。「一時セッションは identity に属するが、エージェントの永続状態には書き込まない」という規約で足りるかの検証
- [ ] Pipeline 起動のトリガー設定の構文（外部イベント → Pipeline 起動時の Context 引き継ぎ宣言を含む）
- [ ] `until` / `transform.value` / `verify.condition` の式言語の仕様確定（共通の決定論的評価器）
- [ ] `transform` の実行系: 式言語で足りるか、制限付きコードを許すか
- [ ] agent の一時セッション起動・budget 執行の実装方法（Reyn セッション API とのマッピング）
- [ ] capabilities.tools と Reyn パーミッションシステムの粒度のマッピング詳細
- [ ] 旧 fetch / store 相当の shell プリセット集の設計
- [ ] carry_forward されたフィールドの for_each（do）内からの可視性
- [ ] スキーマの enum 型と match.cases の静的整合検査
- [ ] 名前付きストアの型システム
- [ ] call のバージョニング（呼び出し先更新と承認ハッシュの運用）。description のみの変更が再承認を要求すべきかの扱いを含む
- [ ] **承認ハッシュ計算前の定義正規化**: YAML のマルチライン記法の揺れ（`|` / `>` / `|-` 等）やインデント・キー順の違いにより、意味的に同一の定義がバイト列として異なりうる。記法変更だけで再承認が走る体験を避けるため、ハッシュ計算前の正規化方式（意味論的に等価な AST への正規化の範囲）を実装時に確定する
- [ ] refine のイテレーション横断の観測
- [ ] for_each.collect に agent を置いた場合の意味論の詳細
- [ ] §7.5 マルチエージェント協調（メッセージプッシュ経由）の標準パターンの詳細化

---

## 付録 A: 一般的な AI エージェントタスクに対する表現可能性の検証

ネット上で一般的に語られる AI エージェントのユースケース 10 種を、本仕様の DSL で表現できるか検証した結果。

| # | タスク | 判定 | 表現 |
|---|---|---|---|
| 1 | カスタマーサポート（応対・返金・エスカレーション） | ✅ | `agent` → `match` → `shell` |
| 2 | セールスコール分析（transcript → 洞察 → 通知） | ✅ | `shell` → `agent` → `shell` の線形 |
| 3 | リサーチ（複数ソース横断 → レポート） | ✅ | `for_each`（並列調査）→ `collect` → `agent` |
| 4 | コーディング（実装・テスト・デバッグの反復） | ✅ | `agent` + `verify`(test) + `retry`。§3.1 の標準形そのもの |
| 5 | リード選別・CRM 更新（継続的処理） | ✅ | 1 件の処理を Pipeline で表現し、新規リード発生イベントごとに起動する（イベント駆動起動、§0.5）。常駐性は Pipeline ではなく起動側の責務 |
| 6 | 分類器による一次判定 → 複雑案件のみ高性能モデルへ | ✅ | `agent`（軽量モデル）→ `match` → `agent`（高性能モデル） |
| 7 | コンプライアンス監視（異常検知・監査証跡付き） | ✅ | `shell` → `agent` → `verify` + `transform`。監査証跡は Audit Event が自動で満たす |
| 8 | 医療文書化（音声 → ノート → 人間承認 → EHR） | ✅ | `agent` → `verify` → 人間承認（§7.4 のいずれか）→ `shell` |
| 9 | HR オンボーディング（数週間・複数マイルストーン） | ✅ | 各マイルストーンを個別 Pipeline とし、イベント駆動で接続。**1 Pipeline = 1 完結タスク、長期の状態追跡は起動側（イベント機構）の責務**という粒度設計で解決 |
| 10 | A2A 型の動的エージェント間委任 | 🔺（動的のみ排除） | **静的に宣言されたエージェント間協調は `identity` 指定で表現できる**（実装は coder、レビューは reviewer 等、異なるロールへの委譲は一級市民）。排除されるのは「実行時に委任先を発見・動的に選択する」ことのみ（identity は静的リテラル）。疎な連携はさらに §7.5（メッセージプッシュ + 受信側の自律性）でも表現可能 |

**総括**: 一般的なタスクの大半は「取得 → 判断 → 実行 → 検証」の線形〜分岐構造に落ち、3 部品 + プリミティブで表現できる。表現が難しく見えた「継続的・長期的タスク」（5, 9）は、**Pipeline の粒度を「1 件の完結した処理」に保ち、繰り返し・待機・長期状態をイベント駆動起動側に移す**ことで解決する。エージェント間協調（10）も、静的に宣言された委譲（identity 指定）としては最初から表現可能であり、排除されているのは動的な委任先選択という一点のみである。

---

## 付録 B: 生成用コンパクト仕様

エージェント（LLM）が Pipeline を設計・実装する際にコンテキストへ投入するための最密表現。本仕様書の全内容のうち、**生成に必要な文法・制約・意味論のみ**を抽出したものであり、生成時はこの付録のみを提示すればよい。人間向けの動機・経緯は本文を参照。

```
# Reyn Pipeline DSL — compact spec for generation

Pipeline  = pipeline:NAME description?:TEXT input?:{NAME:{schema:REF}}* defaults?{budget}
            steps:[Step+] refine?
            # input: declares externally-supplied ctx values. Optional — the effective input
            # interface is derived from {ctx.*} references either way; declaring it stabilizes
            # the contract (undeclared refs become errors). description: purpose summary,
            # used by agents to select pipelines (like an MCP tool description).
Step      = agent | transform | tool | shell | for_each | parallel | fold | match | call
agent     = {identity?:LIT prompt:TPL capabilities?:{tools:[LIT*]}
             budget?:{max_turns,max_tokens,timeout,on_exhausted:partial|fail}
             schema?:REF verify?:[Check*] retry?:{max:N feedback:TPL} output?:NAME}
transform = {value:EXPR output:NAME}                    # pure, no side effects; runs in the
                                                        # pipeline engine's expression evaluator
tool      = {name:LIT args?:{KEY:TPL}* timeout? schema?:REF output?:NAME}
                                                        # static tool invocation; permission
                                                        # system (Allow/Deny/Ask) applies per tool
shell     = {command:TPL timeout? schema?:REF output?:NAME}
                                                        # sugar for tool name=shell. command
                                                        # structure is static. PIPE DATA of the
                                                        # previous step is fed to STDIN as JSON;
                                                        # stdout → schema-checked output.
for_each  = {over?:PATH | items?:[LIT*] max_parallel? on_error:continue|abort|retry(n)
             do:Step collect:Step}   # list source: over (ctx path) | items (static) | pipe data.
                                     # collect required. items entries are LITERALS — for runtime
                                     # lists use over, never templates inside items.
parallel  = {on_error?:abort|continue|retry(n)          # heterogeneous branches. default: abort.
             branches:{NAME:Step}+ collect:Step}        # static branch set; collect required;
                                                        # collect input: results.NAME (named map).
                                                        # on_error:continue → collect MUST handle
                                                        # absent branches.
fold      = {over?:PATH | items?:[LIT*] init:EXPR do:Step output:NAME max_items?}
             # SEQUENTIAL. do receives {item} and {acc}; do's output becomes next {acc};
             # final acc → output. No collect. Item failure (after retry) fails the whole fold.
             # Use for_each when items are independent; fold ONLY when each item depends on
             # the accumulated result.
match     = {on:PATH cases:{LABEL:{pipeline:LIT pass?:[NAME*]}}+ default?:{pipeline:LIT pass?:[NAME*]}}
call      = {pipeline:LIT pass:[NAME*] output:NAME}   # returns the callee's FINAL step output.
                                                       # callee failure = this step fails.
refine    = {until:PRED max_iterations:N carry_forward:[PATH*]  # required, may be []
             on_exhausted:fail|continue}
             # fail: pipeline FAILS (propagates to caller). continue: pipeline completes
             # normally with until unmet — branch on the judged field afterward.
             # To hand off after N tries, use continue + a match in the caller. 
Check     = {schema:REF} | {condition:PRED} | {shell:{command:TPL}}
Schema    = name:NAME fields:{FIELD:{type:bool|string|number|enum[LABEL*]|list required?}}+
            # defined separately from pipelines; referenced by REF
TPL       = string with {item} {ctx.NAME.field} interpolation (values only).
            Multi-line strings (YAML block scalars: |, >, |-) are valid TPL/TEXT;
            interpolation works across lines. Use | for prompts/descriptions freely.
PRED,EXPR = deterministic: field refs, comparisons, all/any/count/join. No calls, no LLM.
LIT       = static literal. NEVER a template/expression.
# Pipeline input: the invocation payload is the pipe data of the first step;
# named values supplied at invocation (by trigger config or caller's pass) appear as {ctx.NAME}.

## Hard rules (violations = invalid pipeline)
1. Only `agent` may invoke an LLM. transform is pure; tool/shell never call LLMs.
2. All LIT fields (identity, call.pipeline, match case targets) are static.
   Runtime-value dispatch is ONLY match. LLM output selects a case label, never a target.
3. agent.capabilities ⊆ capabilities(identity); identity defaults to invoker.
4. refine scope must contain ≥1 agent (incl. via call). retry has NO carry_forward.
   Carried values MUST be referenced somewhere in scope (e.g. in an agent prompt as
   {ctx.NAME}) — otherwise the refine degrades into an output lottery. On the FIRST
   iteration carried values resolve to empty. Prefer value outputs inside a refine scope;
   perform external writes (files, repos) in steps AFTER the refine succeeds.
5. Pipe data: each step receives only the previous step's output.
   A called/matched pipeline's first step receives the caller's pipe data at the call site.
   Cross-step values: write output:NAME, read {ctx.NAME}. call/match targets see only pass:[...].
   A called/matched pipeline may reference {ctx.X} ONLY if X was passed to it or produced inside it.
6. for_each do-instances and parallel branches: isolated, read-only ctx, no sibling/branch
   communication. Writes happen in collect. for_each results = ordered list;
   parallel results = named map (results.NAME).
   on_error:continue silently DROPS failed items/branches from results — when completeness is
   required (all must succeed), use abort or retry(n). parallel defaults to abort.
7. Fields referenced by match.on/until/carry_forward must exist in a declared schema.
   Prefer schema enums + verify to guarantee value membership; use match only when the
   branches actually differ.
8. Pipelines NEVER wait for the outside world. call is SYNCHRONOUS (blocks until the callee
   finishes), so a callee must not wait either. Human intervention:
   - immediate go/no-go (seconds): a tool call whose permission is set to Ask (sync interrupt)
   - anything slower: the pipeline ENDS after producing its output; a separate pipeline is
     later triggered by the approval/external event. "Split" means end + event-triggered
     continuation, NOT a call to a waiting pipeline. Never model human latency as a timeout.
9. Budgets/iterations always finite (defaults apply if omitted).

## Semantics in one line each
steps: sequential, sync. for_each: parallel temp sessions over a list, then collect once.
parallel: named heterogeneous branches run simultaneously, then collect once.
fold: sequential walk over a list threading {acc}; use only when items depend on prior results.
retry: re-run same step on verify failure, fresh session, only {verify_failure} passed.
refine: re-run whole steps from top when until=false at end, carrying ONLY carry_forward paths.
Layer rule: agent self-checks never gate progress; verify (deterministic) and refine (judge agent
+ runtime enforcement) do.
Granularity rule: exploration (dynamic tool choice, planning, iterative dialogue) belongs INSIDE
one agent step; pipeline structure expresses only goal checks and the deterministic skeleton.
Do not model exploration steps with match/for_each/fold.

## Canonical example
pipeline: implement-review
steps:
  - call: {pipeline: implement-only, pass: [task_spec, feedback], output: impl}
  - call: {pipeline: review-only, pass: [impl], output: review}
  - transform: {value: '{feedback: join(review.comments, "\n")}', output: feedback}
refine:
  until: "ctx.review.passed"
  max_iterations: 5
  carry_forward: ["feedback"]
  on_exhausted: fail

## Canonical split (human-in-the-loop, slow approval)
pipeline: draft-note            # ends after drafting; does NOT wait
steps:
  - agent: {prompt: "structure the consultation: {ctx.recording}", schema: note-schema,
            verify: [{schema: note-schema}], retry: {max: 2, feedback: "{verify_failure}"},
            output: draft}
# → human reviews at their own pace; the approval event triggers the next pipeline:
pipeline: apply-approved-note   # triggered by approval event; receives draft + decision via ctx
steps:
  - match:
      on: "decision.value"
      cases:
        approved: {pipeline: ehr-write, pass: [draft]}
      default:    {pipeline: manual-review, pass: [draft, decision]}
```

**運用規約:**
- 生成された定義は、人間作成の定義と同一の静的解析（§7.3）とパーミッション承認（§7.1）を通る。
- 本付録は本文の規範的内容と同期して維持する。本文との齟齬がある場合は本文が優先し、付録を修正する。

---

## 付録 C: 関数型プログラミング概念との対応

本 DSL の設計判断が場当たりではなく、関数型プログラミングの確立された構造に対応していることの確認。設計の理論的裏付けおよび将来の拡張判断の参照点として残す。

| FP 概念 | Pipeline DSL | 備考 |
|---|---|---|
| 純粋関数 | `transform` | |
| 作用付き関数（IO） | `shell` | |
| 非決定的な作用 | `agent` | 3 部品は「純粋 / IO / 非決定」というエフェクトの分類そのもの |
| 関数合成（モナド的、前の結果に依存） | `steps` の逐次実行 | |
| Applicative（依存のない計算の積） | `parallel` | 逐次（依存あり）と独立（依存なし）を構文で区別する設計は Haxl と同型で、静的解析可能性の源泉 |
| map | `for_each` | |
| reduce | `collect` | for_each 全体は mapReduce |
| **fold（アキュムレータを通す畳み込み）** | `fold` | 本対応表の作成によって欠落が発見され、追加された（追補 5） |
| 直和型 + パターンマッチ | schema enum + `match` | |
| let 束縛 / Reader 環境 | `output:` / `{ctx.*}` | |
| 契約・篩型（事後条件の動的検査） | `verify` | |
| fuel（燃料付き計算） | `budget` / `max_iterations` / `max_depth` / `max_items` | |
| 全域言語（total language） | Turing 不完全 DSL | 最も近い既存物は Dhall（全域な設定言語） |
| 高階関数 | **意図的に不在** | 関数を値として渡す代わりに、名前付きパイプラインの閉じた集合を参照する。これは **defunctionalization** そのものであり、静的リテラル制約（call.pipeline / identity / match cases）の理論的正当化である |
| Alternative（フォールバック合成 `<|>`） | プリミティブなし | 「安い方法を試し、失敗したら高い方法」は transform + match の組み合わせで表現する。プリミティブ追加は不要と判断（標準パターンとして文書化候補） |
| パラメトリック多相 | なし（パイプラインは単相） | schema だけ差し替えた汎用パイプラインは書けない。現時点では不要と判断 |

---


