---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run, reyn eval, reyn chat]
---

# 共通フラグ

`reyn run`、`reyn eval`、`reyn chat` に共通のフラグです。コマンド固有のフラグはそれぞれのページに記載されています。

## モデル選択

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--model MODEL` | `reyn.yaml` の `model`（または `standard`） | モデルクラス（`light` / `standard` / `strong`）または LiteLLM モデル文字列。`reyn.yaml` の `models` マップを通じて解決されます。 |

## 出力言語

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--output-language LANG` | `reyn.yaml` の `output_language`（または `ja`） | LLM コンテキストに `output_language` として注入される言語コード。ユーザー向けテキストを生成する Phase がそれに従います。 |

## ランタイム制限

すべての制限はデフォルトで `reyn.yaml` の `safety:` ブロックから読み取られ、呼び出しごとにオーバーライドできます。

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--max-phase-visits N` | `safety.loop.max_phase_visits`（または `25`） | 1 回のランの任意の単一 Phase への再訪問上限。`0` で上限を無効化。暴走する修正ループを防ぎます。超過するとランはステータス `loop_limit_exceeded` で終了します。 |
| `--phase-budget SECONDS` | `safety.timeout.phase_seconds`（または `0`） | Phase ごとのウォールクロックバジェット。リトライ/ターンの境界でのソフトチェック。呼び出し途中はキャンセルしない。`0` で無効化。超過するとランはステータス `phase_budget_exceeded` で終了します。 |
| `--llm-timeout SECONDS` | `safety.timeout.llm_call_seconds`（または `60`） | LiteLLM に渡される呼び出しごとの HTTP タイムアウト。 |
| `--llm-max-retries N` | `safety.timeout.llm_max_retries`（または `3`） | LLM 呼び出しごとの一時的エラーのリトライ数（LiteLLM 指数バックオフ）。 |

## Permission ゲーティング

| フラグ | 利用可能 | デフォルト | 説明 |
|------|---------|---------|-------------|
| `--allow-shell` | `run` | オフ | `shell` Control IR op を有効にする。サブプロセスを呼び出す Skill に必要。 |
| `--allow-unsafe-python` | `run`、`chat` | オフ | `mode: unsafe` の Python preprocessor ステップを許可（AST サンドボックスなし）。`--allow-untrusted-python` はレガシーエイリアス。safe モードのステップはこれなしで動作します。 |
| `--strict` | `run` | オフ | すべてのネスト深さで必須フィールドを検証します（デフォルト: トップレベルのみ）。 |

## 診断

| フラグ | 利用可能 | 説明 |
|------|--------------|-------------|
| `--events` | `run` | 実行終了後に完全なイベントログを表示。 |

## 解決順序

各フラグについて、ランタイムは（優先度が高い順で）チェックします:

1. CLI フラグ
2. `reyn.yaml`（プロジェクト）— 一致するキーの値
3. `.reyn/config.yaml`（個人設定オーバーライド）— `reyn.yaml` と同じスキーマ
4. 組み込みデフォルト

`reyn eval` は `--model` に追加のレイヤーを加えます: eval スペックの `model:` フィールドが CLI と `reyn.yaml` の間に位置します。

## 関連情報

- [run.md](run.md)、[eval.md](eval.md)、[chat.md](chat.md)
- [リファレンス: reyn.yaml](../config/reyn-yaml.md)
- [リファレンス: permissions](../config/permissions.md)
