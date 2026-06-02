---
type: how-to
topic: reliability
audience: [human]
applies_to: [.reyn/events/]
---

# LLM の行動を説明する — 監査・コンプライアンス対応

**ゴール:** スキルが何をしたか、LLM がどの判断を下したか、どのファイルに書き込んだかを、Events ログだけで第三者に説明できるようにする。

内部承認フロー・コンプライアンス監査・インシデント調査のいずれにも使える。

> **debug との違い:** `debug-with-events.md` は「原因を探す」ための how-to です。このページは「何が起きたかを説明する」ための how-to です。目的が問題解決から説明責任に変わります。

---

## Events ログが証明できること

reyn は OS を経由したあらゆる状態変化に対してイベントを発行します (P6)。LLM 呼び出し・ファイル書き込み・フェーズ遷移・権限拒否はすべてイベントとして記録されるため、ログが存在する限り「何が起きたか」は再現可能です。

| 説明したいこと | 根拠となるイベント |
|---|---|
| スキルが実行された | `workflow_started` / `workflow_finished` |
| LLM がどのフェーズを選択したか | `phase_completed` (`.decision`, `.next_phase`) |
| LLM への入力・出力 | `context_built` / `llm_called` |
| どのファイルに書き込んだか | `write_file`, `edit_file`, `workspace_updated` |
| 何を読み取ったか | `read_file`, `glob_files`, `grep` |
| 外部ツール（MCP）を呼んだか | `mcp_called`, `mcp_completed` |
| 権限のない操作を試みたか | `permission_denied` |
| エラーが発生した時刻 | `phase_failed`, `validation_error` |
| サブスキル・エージェントを呼んだか | `run_skill_started`, `agent_message_sent` |

---

## ログの場所とフォーマット

```
.reyn/
└── events/
    └── YYYY-MM/
        └── YYYY-MM-DDTHHMMSS_<suffix>.jsonl
```

ファイル名の先頭は実行開始のタイムスタンプです。1 実行 = 1 ファイル。

各行は JSON オブジェクト（JSONL 形式）です：

```json
{
  "ts": "2026-05-09T10:23:45.123Z",
  "kind": "phase_completed",
  "phase": "review",
  "run_id": "a1b2c3d4-...",
  "decision": "continue",
  "next_phase": "finalize",
  "confidence": 0.92,
  "reason": {"summary": "レビュー基準を満たした"}
}
```

`ts` は UTC の ISO-8601 です。`run_id` が同じ行が同一実行のレコードです。

---

## よく使うシナリオ

### 「このスキルは何をしたか」を一覧する

```bash
reyn events .reyn/events/<run_id>.jsonl
```

ライブ実行と同じフォーマットで全イベントが表示されます。LLM は再呼び出しされません。

概要だけ欲しい場合はライフサイクルイベントだけに絞ります：

```bash
reyn events <log> \
  --filter workflow_started \
  --filter phase_started \
  --filter phase_completed \
  --filter workflow_finished
```

---

### 「LLM はどの遷移を選択したか」

`phase_completed` には LLM が下した判断が含まれます：

```bash
reyn events <log> --filter phase_completed
```

各行の `.decision`（`continue` / `finish` / `abort`）と `.next_phase` が遷移の証跡です。`reason.summary` には LLM が遷移理由として出力したテキストが入ります。

複数フェーズにわたる全遷移をテキストに出力するには：

```bash
reyn events <log> --filter phase_completed \
  | grep -E '"(phase|next_phase|decision|summary)"'
```

---

### 「どのファイルに書き込んだか」

```bash
reyn events <log> --filter write_file --filter edit_file
```

`write_file` / `edit_file` イベントのペイロードにはパスと書き込んだ内容のサマリが含まれます。`workspace_updated` はアーティファクト単位の書き込みを記録します。

---

### 「外部サービスを呼んだか」（MCP・シェル）

```bash
reyn events <log> --filter mcp_called --filter shell_started
```

MCP イベントには呼び出したツール名・入力・完了時刻が記録されます。権限のないシェルコマンドが試みられた場合は `shell_not_allowed` / `permission_denied` として記録されます。

---

### 「エラーが起きた時刻は」

```bash
reyn events <log> \
  --filter phase_failed \
  --filter validation_error \
  --filter normalization_error
```

`phase_failed` には `.error` フィールドと `.ts` があります。インシデント報告書への転記はこの行の `ts` を使ってください。

---

### 期間を絞って複数実行をまとめて確認する

```bash
reyn events .reyn/events/ --since 2026-05-01 --until 2026-05-09
```

ディレクトリを渡すと範囲内の全 JSONL を時系列順に結合して表示します。

---

## 内部承認・コンプライアンスでの使い方

### スクリーンショットより JSONL が強い理由

スクリーンショットはある瞬間の画面を切り取るだけです。Events ログは：

- **改ざん検知が可能**です。`run_id` と `ts` のシーケンスが壊れていれば明らかです。
- **マシンリーダブル**です。監査ツールや SIEM にそのまま取り込めます。
- **LLM が発した理由が含まれます**。`phase_completed.reason.summary` は LLM 自身が出力したテキストです。

### 説明資料を作る手順

1. 対象の実行の `run_id` を `workflow_started` から確認する。
2. `reyn events <log> --filter workflow_started --filter phase_completed --filter workflow_finished` で遷移サマリを出力する。
3. ファイル操作の証跡が必要な場合は `--filter write_file --filter edit_file` を追加する。
4. 出力を `>` でテキストに保存し、承認申請に添付する。

### ログの保持期間管理

不要になったログは `purge` で削除できます：

```bash
# 確認（削除しない）
reyn events purge --before 2026-03-01 --dry-run

# 実行
reyn events purge --before 2026-03-01
```

エージェント単位で絞る場合：

```bash
reyn events purge --before 2026-03-01 --agent <agent_name>
```

保持期間ポリシーがある場合は、この purge を定期的に実行してください。

---

## マルチエージェント実行のトレース

複数エージェントが連携した実行（`run_skill` / `agent_message_sent`）では、`chain_id` で一連のやり取りを横断的に追跡できます：

```bash
# 各エージェントのログから同一 chain_id を収集
grep "<chain_id>" .reyn/events/**/*.jsonl
```

`chain_id` は `user_message_received` イベントに記録され、以降の全 A2A ホップに引き継がれます。どのエージェントがどの順序でどんな判断を下したかを一本の証跡として示せます。

---

## See also

- [debug-with-events.md](debug-with-events.md) — 問題の原因を探す（デバッグ用途）
- [Reference: events](../../../reference/runtime/events.md) — イベント種別の完全一覧とフィールド定義
- [Concepts: events](../../../concepts/runtime/events.md) — Events 設計思想（なぜすべてをイベントにするか）
- [Reference: Control IR](../../../reference/runtime/control-ir.md) — Control IR op の一覧
