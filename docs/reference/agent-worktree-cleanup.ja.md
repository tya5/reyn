# エージェント worktree クリーンアップ (`scripts/cleanup_agent_worktrees.py`)

サブエージェント worktree のガーベジコレクター。サブエージェントが自身の worktree をクリーンアップせずに終了した際に蓄積するステールエントリを削除します。`git worktree remove --force` だけでは、ステールなロックファイルのために削除できないものも対象です。

## なぜこのツールが必要か

並列 dispatch セッションの各サブエージェントは `.claude/worktrees/agent-*` 配下の独立した git worktree を受け取ります。セッションが正常に終了すれば worktree は削除されます。しかし、クラッシュ・タイムアウト・手動 kill によってセッションが中断されると、worktree とそのロックファイルはディスクに残り続けます。

複数セッションにわたってこれは急速に蓄積します。実際にヘビーな dispatch 日の後に 145 件以上の孤立 worktree が観測されました。問題はディスク使用量だけではありません。`git worktree remove --force` は依然としてロックファイル付きの worktree の削除を拒否します (force フラグは変更ファイルチェックをバイパスするものであり、ロックファイルではありません)。クリーンアップスクリプトはロック理由を読み込んで PID を抽出し、その PID が生きていれば worktree をスキップします。そうでなければ先にロックファイルを削除してから `git worktree remove -f` を呼び出します。

## セットアップ

macOS および Linux で動作します。プロジェクト標準の依存関係以外に追加インストール不要:

```bash
python scripts/cleanup_agent_worktrees.py [flags]
```

スクリプトはクリーンアップ対象の git リポジトリ内のディレクトリから実行する必要があります。作業ディレクトリ相対の `git worktree list` を使用します。

## 使い方

### `--list` — 候補の確認 (デフォルト)

```bash
python scripts/cleanup_agent_worktrees.py
# または等価
python scripts/cleanup_agent_worktrees.py --list
```

`.claude/worktrees/` 配下で `agent-*` にマッチするすべての worktree を、ロック状態とロック PID の生死とともに一覧表示します。変更は行いません。

出力例:

```
Worktree candidates (agent-* only):

  [DEAD]  .claude/worktrees/agent-a1b2c3d4  locked by pid=12345 (dead)
  [DEAD]  .claude/worktrees/agent-e5f6a7b8  locked by pid=67890 (dead)
  [LIVE]  .claude/worktrees/agent-c9d0e1f2  locked by pid=11111 (alive)
  [UNLOCKED]  .claude/worktrees/agent-g3h4i5j6

4 worktrees found: 2 dead, 1 alive, 1 unlocked
```

破壊的な操作の前にこれを実行して、どの worktree が影響を受けるかを確認してください。

### `--dry-run` — クリーンアップのシミュレーション

```bash
python scripts/cleanup_agent_worktrees.py --dry-run
```

何も変更せずに削除されるものを正確に表示します。出力フォーマットは `--force` と同じで、各アクションの前に `[DRY RUN]` が付きます。exit code は常に 0 (変更なし)。

### `--force` — 死亡 worktree の削除

```bash
python scripts/cleanup_agent_worktrees.py --force
```

死亡 PID (またはロックファイルなし) の worktree をすべて削除します。各候補に対して:
1. `.git/worktrees/<id>/locked` ファイルを削除 (存在する場合)
2. `git worktree remove -f <path>` を呼び出す

生存 PID の worktree は触れません。ロックファイルのない worktree (unlocked) も実行中のプロセスに守られていないため、デフォルトで削除されます。

出力例:

```
Removing: .claude/worktrees/agent-a1b2c3d4  [dead pid=12345]
  deleted lock file
  git worktree remove -f: OK
Removing: .claude/worktrees/agent-e5f6a7b8  [dead pid=67890]
  deleted lock file
  git worktree remove -f: OK
Skipping: .claude/worktrees/agent-c9d0e1f2  [alive pid=11111]

Removed 2 worktrees, skipped 1 (alive)
```

### `--keep-recent N` — 最近の N 件の worktree を保持

```bash
python scripts/cleanup_agent_worktrees.py --force --keep-recent 5
```

候補を最終更新日時でソートし、最新 N 件をロック状態に関わらず削除対象から除外します。最新の dispatch batch の出力を検査のために保持したい場合に有用です:

```bash
# 最新の 3 回分より古いものをすべてクリーンアップ
python scripts/cleanup_agent_worktrees.py --force --keep-recent 3
```

### `--include-alive` — 生存 worktree も削除 (危険)

```bash
python scripts/cleanup_agent_worktrees.py --force --include-alive
```

ロック PID が生存していても worktree を削除します。現在その worktree を使用している任意のサブエージェントを終了させます。

**使用するのは以下の場合のみ:**
- プロセステーブルに PID が存在するが実際には動作していないゾンビプロセスであることが確実な場合
- スタックしたセッションを意図的に終了させる場合

デフォルトの挙動 (alive = keep) はセーフティのために存在します。`--include-alive` はまれな回復シナリオ向けに提供されており、通常のクリーンアップには含めるべきではありません。

### `--json` — 機械可読出力

```bash
python scripts/cleanup_agent_worktrees.py --list --json
python scripts/cleanup_agent_worktrees.py --force --json
```

worktree ごとに 1 つの JSON オブジェクトを stdout に出力し、最後にサマリーオブジェクトを出力します。`jq` へのパイプによるカスタムフィルタリングや、構造化出力が必要な CI スクリプトに有用です:

```json
{"path": ".claude/worktrees/agent-a1b2c3d4", "status": "dead", "pid": 12345, "action": "removed"}
{"path": ".claude/worktrees/agent-c9d0e1f2", "status": "alive", "pid": 11111, "action": "skipped"}
{"summary": {"total": 2, "removed": 1, "skipped": 1}}
```

## フラグリファレンス

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--list` | on | 候補とステータスを一覧表示。変更なし |
| `--dry-run` | off | 削除をシミュレーション。何が起きるかを表示 |
| `--force` | off | 死亡 PID と unlocked の worktree を削除 |
| `--keep-recent N` | 0 (0 件保持) | 最近更新された N 件の worktree を除外 |
| `--include-alive` | off | 生存 PID の worktree も削除 (危険) |
| `--json` | off | 機械可読な JSON 出力 |

## ワークフローとの統合

### ヘビーな並列 dispatch セッションの後

まず `--list` で孤立した worktree がいくつ蓄積したかを確認し、次に `--force` で削除します:

```bash
python scripts/cleanup_agent_worktrees.py --list
python scripts/cleanup_agent_worktrees.py --force
```

### 保持ウィンドウを設けた定期クリーンアップ

セッション後の検査のために最新 5 件の worktree を保持しながら、それより古いものをすべて削除します:

```bash
python scripts/cleanup_agent_worktrees.py --force --keep-recent 5
```

### CI でのステール worktree 検出

CI では `--list --json` を使って蓄積した worktree を検出し、件数がしきい値を超えたらアラートします。自動削除は行いません:

```bash
count=$(python scripts/cleanup_agent_worktrees.py --list --json | \
  jq -r 'select(.status == "dead") | .path' | wc -l)
if [ "$count" -gt 20 ]; then
  echo "Warning: $count stale worktrees detected"
fi
```

### 使うべきでない場面

- **エージェント以外の worktree を削除したい場合。** スクリプトは `.claude/worktrees/` 配下の `agent-*` プレフィックスでフィルタリングします。意図的に他のすべての worktree を無視します。セーフティへの影響を理解せずにフィルタを拡大しないでください。
- **誤った dispatch からの回復時。** 検査したい誤った結果を出したサブエージェントがある場合は、まず `--list` を実行して worktree が dead/unlocked リストにあることを確認してから削除してください。クリーンアップ中に最近の worktree を保護するために `--keep-recent` を使用してください。

## セーフティ特性

- **`--include-alive` なしで生存 PID は決して触られません。** PID チェックは削除前に `ps -p <pid>` で実行されます。チェックが失敗した (PID 生存) 場合、worktree はスキップされてログに記録されます。
- **`--dry-run` は常に安全です。** ファイルシステムや git 操作は一切実行されません。
- **スクリプトは `.claude/worktrees/` 配下の `agent-*` パスのみを対象とします。** フィーチャーブランチや main などの他のすべての worktree はスクリプトには見えません。

## 関連リソース

- [LLM ペイロードトレース](dogfood-tracing.md) — 並列 dispatch セッションからの LLM ペイロード検査用の補完ツール
