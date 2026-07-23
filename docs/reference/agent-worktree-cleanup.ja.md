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

`.claude/worktrees/` 配下で `agent-*` にマッチするすべての worktree を、ロック状態とロック PID の生死とともに一覧表示します。unlocked な worktree はさらに reclaimable (merged+clean — `--force` で削除される) か kept かに分類され、kept の場合は理由 (dirty / stash / no-merged-PR / no-upstream-config / wrong-remote / detached-head / gh-unavailable / git-error) が付きます。変更は行いません。

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

### `--force` — 死亡 worktree + merged+clean な unlocked worktree の削除

```bash
python scripts/cleanup_agent_worktrees.py --force
```

死亡 PID の worktree をすべて削除します(ロックされていて、かつその PID がもう生存していない場合のみ対象)。各候補に対して:
1. `.git/worktrees/<id>/locked` ファイルを削除 (存在する場合)
2. `git worktree remove -f <path>` を呼び出す

生存 PID の worktree は触れません。unlocked な worktree は **merged かつ clean であると証明された場合のみ**削除されます —
詳細な安全 criterion は下記の [unlocked-worktree clean-gate reclaim](#unlocked-worktree-clean-gate-reclaim-force-3237) を参照してください。
この基準をクリアしない unlocked worktree (dirty / unmerged / unpushed / 判定不能) は `--force` 単体では触れられません。

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

### unlocked-worktree clean-gate reclaim (`--force`, #3237)

`--force` は、安全と証明された UNLOCKED worktree(merged かつ clean)も回収します。reyn は
**squash-merge + branch-delete** で merge するため、worktree の元 commit は決して
`origin/main` の ancestor になりません: squash-merge された worktree では `git branch -r
--contains HEAD` は常に空であり、remote branch が削除・prune された後は
`git rev-parse HEAD@{upstream}` が **エラー**になります (`fatal: ambiguous argument
'...@{upstream}': unknown revision`)。どちらのシグナルも使えません。

unlocked な worktree が **reclaimable** であるのは、以下の ALL を満たす場合のみです:

1. `git status --porcelain` が空 (未コミット/untracked 変更なし)
2. `git stash list` が空
3. `origin` に push 済みで、その push 先ブランチに **merged PR** が存在する —
   `@{upstream}` ではなく `branch.<local-branch>.merge` git config でキーイングする。
   このコンフィグは純粋なローカル読み取りであり **remote ref の prune 後も生存する** ため、
   remote branch が削除された後もその push 先ブランチ名を解決できます。merged-PR の
   head 集合は worktree ごとではなく **一度だけ** `gh pr list --state merged --limit 5000
   --json headRefName` で取得します。

3 つのうち **どれか一つでも**満たさない場合、その worktree は **keep** されます。これには
以下が含まれます: git コマンドのエラー、detached HEAD (`symbolic-ref` なし)、
`branch.<name>.merge`/`.remote` config が無い (push が upstream を設定していない)、
`origin` 以外の remote、単にそのブランチにまだ merged PR が無い場合。

**`gh` が使えない場合は fail-safe。** `gh pr list` が何らかの理由(オフライン・未認証・API
エラー)で失敗した場合、merged-PR 集合は unavailable となり **すべての unlocked worktree が
無条件で keep** されます。本ツールは merge 状態を推測しません。

**既知の残余(明記のみ、未解決)。** merge の**後に** push せずコミットした場合(まれ —
すでに merge 済みの worktree に push せずコミットするケース)は本シグナルで捕捉されません。
そのような worktree は clean + merged と読めても未 push の作業を保持している可能性があります。
per-PR-coder のワークフローでは低リスクであるため、解決ではなくここに明記するに留めます。

### `--include-dirty` — reclaim 対象外の unlocked worktree も削除 (危険)

```bash
python scripts/cleanup_agent_worktrees.py --force --include-dirty
```

上記の reclaimable 判定を **通らなかった** unlocked worktree(dirty / unmerged / unpushed /
その他不確実なもの)も追加で対象にします。**未コミットまたは未 push の作業を破壊する可能性が
あります。** `--include-alive` と同様に、意図的な一括リカバリ向けであり、通常のクリーンアップ
には含めるべきではありません。

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

worktree ごとの `worktrees` 配列 (unlocked worktree には `reclaimable` / `reclaim_reason` が付与) と、
`unlocked_reclaimable` を含むサマリーカウントを持つ JSON オブジェクトを出力します。`jq` へのパイプに
よるカスタムフィルタリングや、構造化出力が必要な CI スクリプトに有用です。

## フラグリファレンス

| フラグ | デフォルト | 説明 |
|--------|-----------|------|
| `--list` | on | 候補とステータスを一覧表示。変更なし |
| `--dry-run` | off | 削除をシミュレーション。何が起きるかを表示 |
| `--force` | off | 死亡 PID (stale) の worktree、および merged+clean な unlocked worktree を削除 |
| `--keep-recent N` | 0 (0 件保持) | 最近更新された N 件の worktree を除外 |
| `--include-alive` | off | 生存 PID の worktree も削除 (危険) |
| `--include-dirty` | off | dirty/unmerged/unpushed な unlocked worktree も削除 (危険) |
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
- **誤った dispatch からの回復時。** 検査したい誤った結果を出したサブエージェントがある場合は、まず `--list` を実行して worktree のステータスを確認してから削除してください。unlocked な worktree は merged+clean と証明された場合にのみ `--force` で削除されます([unlocked-worktree clean-gate reclaim](#unlocked-worktree-clean-gate-reclaim-force-3237) 参照)。検査したい dirty / unmerged な worktree は `--include-dirty` を渡さない限り常に保持されます。クリーンアップ中に最近の worktree を保護するために `--keep-recent` を使用してください。

## セーフティ特性

- **`--include-alive` なしで生存 PID は決して触られません。** PID チェックは削除前に `ps -p <pid>` で実行されます。チェックが失敗した (PID 生存) 場合、worktree はスキップされてログに記録されます。
- **`--dry-run` は常に安全です。** ファイルシステムや git 操作は一切実行されません。
- **unlocked な worktree は merged+clean と証明されて初めて削除候補になります。** `build_candidates()` が unlocked worktree を追加するのは `classify_unlocked_reclaimability()` が `reclaimable=True` を返した場合のみです (porcelain 空、stash 空、push 先ブランチに merged PR あり — `@{upstream}` ではなく `branch.<name>.merge` config でキーイング)。dirty なツリー、upstream config 無し、remote 不一致、detached HEAD、`gh` unavailable、git エラーなどの不確実性はすべて KEEP に解決されます。この範囲を非 reclaimable な unlocked worktree にまで広げる唯一の方法が `--include-dirty` であり、これは危険です。
- **スクリプトは `.claude/worktrees/` 配下の `agent-*` パスのみを対象とします。** フィーチャーブランチや main などの他のすべての worktree はスクリプトには見えません。

## 関連リソース

- [LLM ペイロードトレース](dogfood-tracing.md) — 並列 dispatch セッションからの LLM ペイロード検査用の補完ツール
