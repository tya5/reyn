---
type: concept
topic: architecture
audience: [human, agent]
---

# 設計原則 (P1–P8)

reyn のアーキテクチャは 8 つの原則に支えられています。これらは `CLAUDE.md` でコードを書く agent への制約として列挙されていますが、本ページは **なぜ** その原則があるのか理解したい人間（と興味のある agent）向けの解説です。

## P1. Phase は状態を持たず再利用可能

Phase は `input` artifact 型と instructions だけを宣言します。Phase は **知らない**：

- 次の phase が何か
- 自分の output schema が何か
- どの skill に属するか

出力の形は外部から決まります。遷移時は遷移先 phase の `input` スキーマが、finish 時は skill の `final_output` が output schema を規定します。Phase 自身は output schema を宣言せず、参照もしません。

**理由：** `revise` のような phase は draft を生成する任意の skill から drop-in で使えるべきです。Phase が「次の phase」フィールドを持ったり output schema を重複して持つと、特定のワークフローと結合し再利用可能性を失います。また二重宣言は drift の原因になります。次の phase が input を変えたとき、output schema をハードコードしていた Phase はサイレントに陳腐化します。output shape を外部の関心事にすることで、このクラスのバグを根絶できます。

**よくある罠：**

- frontmatter に `output:` や `output_schema:` フィールドがある → 削除する。output は遷移先から導出される。
- instructions に「`title`, `body`, … というフィールドを持つ JSON を生成してください」とある → そのフィールドは artifact schema に書くべきであり instructions に書かない。

## P2. Skill が構造を所有し最終出力を定義

Skill は `entry`、`graph`（phase 間の遷移）、`final_output` を宣言します。Phase の接続を Phase ファイル内で定義することは **絶対にしない**。LLM が finish を返したとき、OS は `final_output` に対して最終 artifact を検証します。これが Skill と外部世界との契約です。

**理由：** 構造は Skill レベルの関心事です。Phase に混ぜると再利用できなくなり、ワークフロー変更のたびに Phase を書き直す羽目になります。「この skill は何を返すのか？」も Skill レベルの契約であり、Phase の決定事項ではありません。final output が phase に分散すると、グラフをリファクタしたときに戻り値の型がサイレントに変わるリスクがあります。

**よくある罠：**

- `skill.md` に `final_output` がない → OS が finish 検証の根拠を持てない。
- skill graph にエッジが欠けている → LLM が提示する遷移を OS が拒否する。
- `can_finish: true` の phase があるのに skill の `final_output` がない → OS が finish を拒否する。

## P3. OS が実行を制御

OS — LLM でも Skill でもない — がランタイムエンジンです。context frame の構築、LLM 呼び出し、output 検証、Control IR の実行、遷移管理、event 発行を担います。

**理由：** orchestration を LLM の手から離すことが、reyn を *constrained* な意思決定エンジンにする鍵です。LLM は OS が使うツールであって、OS が LLM のツールなのではない。LLM に任意のコード実行や任意の遷移選択を許すと、reyn の中核的な価値である監査可能性と予測可能性が失われます。

## P4. LLM は制約された意思決定エンジン

LLM は以下から選びます：

- 次の phase、または `finish`
- artifact（選んだ遷移先の input schema に合致するもの）
- Control IR ops のリスト（file read, ask_user, sub-skill 呼び出しなど）

OS が提示した遷移候補の中からしか選べません。graph に無い phase 名を hallucinate すると OS が拒否します。

**理由：** LLM の制御フローを無制限にすると不安定になります。OS が検証した小さな選択肢に絞ることで、再生可能・デバッグ可能・安全になります。phase instructions 内でのクリエイティブな自由は維持されますが、構造的な自由は意図的に制限されます。

## P5. Workspace がシングルソースオブトゥルース

phase 間でやりとりするすべてのデータ・artifact・ファイルは workspace に置きます。Phase は Control IR（パーミッションシステムでゲート）を通じてのみ読み書きします。Phase 内のインメモリ状態は workspace に着地するまで信頼できません。

**理由：**

- **再生可能性。** すべての書き込みが OS を経由し event を発行する（[P6](#p6-events-are-the-audit-truth)）ため、event log だけでワークフローが何を見たかを再構築できます。「OS が見逃した隠れた状態」は存在しません。
- **パーミッション強制。** パーミッションシステムは Control IR を通じたすべての読み書きをゲートします。インメモリのサイドチャネルで workspace を迂回した phase は、パーミッションチェックを完全に回避することになります。
- **クラッシュリカバリ。** 実行中に OS が再起動した場合、workspace に書かれたものだけが残ります。書かれなかったものは失われます。

**よくある罠：**

- Python preprocessor の戻り値で phase 間データを渡し workspace に書かない → P5 違反。`file.write` + `file.read` か artifact チャネルを使う。
- モジュールレベル変数で LLM 呼び出しをまたいで状態を蓄積する → event log に見えず、クラッシュ時に回復不能。

## P6. Events が監査の真実

すべての状態変化は event を発行します。event log（`events/`）は append-only で再生可能です。状態の回復・デバッグ・エージェント間トレース・将来のハッシュチェーンはすべて event から導出されます。event を発行せずに状態を変更したものは OS から見えません。

**理由：**

- **デバッグ可能性。** 何かが間違っているとき、event log が最初の — そして通常唯一の — ツールです。すべての LLM 呼び出し・Control IR op・バリデーション失敗が記録されます。
- **再生。** 完全な event log は実行の完全な記述です。`reyn events <log>` は LLM を再呼び出しせずにランを再現します。
- **監査証跡。** コンプライアンス要件のある環境では、append-only log が監査可能な記録の基盤になります。将来の作業でハッシュチェーンを追加し改ざん検知を可能にする計画があります。
- **エージェント間トレース。** agent A が agent B に委譲し（さらに B が別の agent に委譲することもある）、その全ホップが、最初の user submission で採番された同じ `chain_id` を持つ event を発行します。マルチホップ chain の end-to-end 再構築は、各 agent の `events.jsonl` を `grep <chain_id>` するだけ。

**よくある罠：**

- OS を介さずに workspace のファイルを直接書く（例：preprocessor から） → OS が `write_file` event を発行しないため、監査・再生から見えない。
- 構造化 event の代わりに自由形式のアプリケーションログを使う → 再生不能・フィルタ不能・監査チェーンの対象外。

## P7. OS は skill 不可知 (CRITICAL)

OS のコードに、特定の skill に固有の phase 名・artifact 型名・フィールド名が **含まれてはいけません**。

**検出ルール：** 特定 phase（`"revise"`, `"draft_article"`）や特定フィールド（`"title"`, `"body"`, `"quality_notes"`）を名指す文字列リテラルが OS コードに現れたら違反。

**理由：** 新しい Skill が追加されたとき、OS のコードは変わってはいけません。これが reyn を拡張可能にします — skill は出入りするが、ランタイムは不変。

避けるべき罠：

- skill 固有のフィールドを fabricate する fallback ロジック → 生 artifact データを返す。
- skill 概念をエンコードする decision 値 (`decision="revise"`) → OS レベルの値だけを使う：`continue | finish | abort`。
- OS モジュール内のハードコード artifact 型名。

## P8. Phase 指示書はドメインロジックのみ

Phase 指示書は output artifact のフィールドを列挙してはいけないし、Control IR のフォーマットを記述してもいけません。両方とも OS が `candidate_outputs` と `available_control_ops` でランタイム注入します。

**正当な指示書の内容：**

- **WHAT** を分析・生成・決定するか
- **WHEN** どの候補遷移を選ぶか
- ドメイン固有のルール

**理由：** Phase 指示書が schema 情報を重複して持つと、schema 変更時にサイレントに desync します。OS のランタイム注入が output shape と利用可能 ops について LLM が見る内容の single source of truth です。schema 情報を再記述する指示書はコンテキストウィンドウも肥大化させ、現在の遷移先が期待しないフィールドを LLM が生成するリスクも高めます。

## 参考

- [architecture.md](architecture.md) — レイヤがどう組み合わさるか
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — 責務境界
- [workspace.md](workspace.md) — Workspace の詳細 (P5)
- [events.md](events.md) — Events の詳細 (P6)
- [Reference: llm-output-contract](../reference/runtime/llm-output-contract.md) — LLM output contract
