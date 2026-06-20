---
type: how-to
topic: using-reyn
audience: [human]
---

# メモリを確認・管理する

Reyn はセッションをまたいで事実を自動的に記憶します — 設定は不要です。
このページは、記憶された内容を*見たい*・*整理したい*ときのためのものです:
事実の確認、古い記憶の修正、バックアップ、別マシンへの移行など。これらはすべて
chat セッションが動いていない状態で `reyn memory` を使って行います。

## メモリの保存場所

| レイヤ | パス | `--agent` フラグ |
|-------|------|-----------------|
| 共有（既定） | `.reyn/memory/` | 省略 |
| エージェント別 | `.reyn/agents/<name>/memory/` | `--agent <name>` |

以下のコマンドはすべて既定で共有レイヤを対象とします。特定エージェントの
メモリを対象にするには `--agent <name>` を追加します。

## 保存内容を見る

```bash
reyn memory list                    # 全メモリファイル（共有レイヤ）
reyn memory list --agent my_agent   # 特定エージェントのメモリ
reyn memory show preferences        # 1 つのメモリ内容を表示
reyn memory search "API key" -i     # 正規表現検索（-i = 大文字小文字無視）
```

## メモリを修正・削除する

```bash
reyn memory edit preferences        # $EDITOR で開く
reyn memory delete preferences      # 削除（確認プロンプトあり）
reyn memory delete preferences -y   # プロンプトなしで削除
```

`delete` はそのレイヤの `MEMORY.md` インデックスからもエントリを除去するため、
インデックスが存在しないファイルを指し続けることはありません。

## バックアップと復元

```bash
reyn memory export --out backup.json      # 全メモリをファイルに dump
reyn memory export                        # または stdout へ（既定）
reyn memory import backup.json            # 復元（既存ファイルはスキップ）
reyn memory import backup.json --overwrite # 復元（既存を上書き）
```

`import` は `--overwrite` を付けない限り既存のメモリをスキップするため、
通常の import は再実行しても安全です。

## 関連

- [リファレンス: `reyn memory`](../../reference/cli/memory.md) — 全サブコマンド・フラグ・終了コード
- [コンセプト: memory](../../concepts/data-retrieval/memory.md) — Reyn が何を記憶するか、recall の仕組み
