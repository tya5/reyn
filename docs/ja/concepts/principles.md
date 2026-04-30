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

**理由：** `revise` のような phase は draft を生成する任意の skill から drop-in で使えるべきです。Phase が「次の phase」を持つと特定のワークフローと結合し、再利用可能性を失います。

## P2. Skill が構造を所有

Skill は `entry_phase`、`graph`（phase 間の遷移）、`final_output_schema` を宣言します。Phase の接続を Phase ファイル内で定義することは **絶対にしない**。

**理由：** 構造は Skill レベルの関心事です。Phase に混ぜると再利用できなくなり、ワークフロー変更のたびに Phase を書き直す羽目になります。

## P3. OS が実行を制御

OS — LLM でも Skill でもない — がランタイムエンジンです。context frame の構築、LLM 呼び出し、output 検証、Control IR の実行、遷移管理、event 発行を担います。

**理由：** orchestration を LLM の手から離すことが、reyn を *constrained* な意思決定エンジンにする鍵です。LLM は OS が使うツールであって、OS が LLM のツールなのではない。

## P4. LLM は制約された意思決定エンジン

LLM は以下から選びます：

- 次の phase、または `finish`
- artifact（選んだ遷移先の input schema に合致するもの）
- Control IR ops のリスト（file read, ask_user, sub-skill 呼び出しなど）

OS が提示した遷移候補の中からしか選べません。graph に無い phase 名を hallucinate すると OS が拒否します。

**理由：** LLM の制御フローを無制限にすると不安定になります。OS が検証した小さな選択肢に絞ることで、再生可能・デバッグ可能・安全になります。

## P5. Phase に output schema は無い

Output schema は以下で決まります：

- 次の phase の input schema、または
- skill の `final_output_schema`

**理由：** 二重宣言は drift の原因になります。「次 phase = X」だけで output schema が決まれば、X が変わっても output が自動で追従します。

## P6. Skill が final output を所有

最終出力 schema は Skill だけが定義します。OS が LLM の最終 artifact をそれに対して検証します。

**理由：** 「この skill は何を返すのか？」は Skill レベルの契約であって、Phase の決定事項ではありません。Phase に分散させるとリファクタが脆弱になります。

## P7. OS は skill 不可知 (CRITICAL)

OS のコードに、特定の skill に固有の phase 名・artifact 型名・フィールド名が **含まれてはいけません**。

**検出ルール：** 特定 phase（`"revise"`, `"draft_article"`）や特定フィールド（`"title"`, `"body"`, `"quality_notes"`）を名指す文字列リテラルが OS コードに現れたら違反。

**理由：** 新しい Skill が追加されたとき、OS のコードは変わってはいけません。これが reyn を拡張可能にします — skill は出入りするが、ランタイムは不変。

避けるべき罠：

- skill 固有のフィールドを fabricate する fallback ロジック → 生 artifact データを返す
- skill 概念をエンコードする decision 値 (`decision="revise"`) → OS レベルの値だけを使う：`continue | finish | abort`
- OS モジュール内のハードコード artifact 型名

## P8. Phase 指示書はドメインロジックのみ

Phase 指示書は output artifact のフィールドを列挙してはいけないし、Control IR のフォーマットを記述してもいけません。両方とも OS が `candidate_outputs` と `available_control_ops` でランタイム注入します。

**正当な指示書の内容：**

- **WHAT** を分析・生成・決定するか
- **WHEN** どの候補遷移を選ぶか
- ドメイン固有のルール

**理由：** Phase 指示書が schema 情報を重複して持つと、schema 変更時にサイレントに desync します。OS のランタイム注入が single source of truth。

## 参考

- [architecture.md](../../en/concepts/architecture.md) — レイヤがどう組み合わさるか（英語版にフォールバック）
- `concepts/phase-vs-skill-vs-os.md` — 責務境界（Phase 2）
- `reference/runtime/llm-output-contract.md` — LLM output contract（Phase 2）
