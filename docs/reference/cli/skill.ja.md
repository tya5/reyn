---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn skill]
---

# `reyn skill`

スキル定義のバージョン履歴管理とロールバックを行います。`skill_improver` の
finalize ステップ（FP-0006 Component B）が `.reyn/skill-versions/<name>/` に
保存したスナップショットを読み取ります。

## サブコマンド

### `reyn skill versions`

スキルの保存済みバージョンスナップショット一覧を表示します。

```
reyn skill versions <SKILL_NAME>
```

#### 位置引数

| 名前 | 説明 |
|------|-------------|
| `SKILL_NAME` | 対象スキルの名前。標準ルックアップ順（project → local → stdlib）で解決されます。 |

#### 終了コード

| コード | 意味 |
|------|---------|
| `0` | 成功 — バージョン一覧を表示、またはバージョン未保存（正常終了）。 |

#### 出力

```
my_skill version history:
  v1  2026-05-01 10:00
  v2  2026-05-05 14:30
  v3  2026-05-09 09:15  -> current
```

スナップショットディレクトリが存在しない場合:

```
No versions saved for skill 'my_skill'.
```

---

### `reyn skill rollback`

スキルを以前のバージョンに戻します。

```
reyn skill rollback <SKILL_NAME> [--to vN]
```

#### 位置引数

| 名前 | 説明 |
|------|-------------|
| `SKILL_NAME` | ロールバックするスキルの名前。 |

#### オプション

| フラグ | 説明 |
|------|-------------|
| `--to vN` | 対象バージョン（例: `v2`）。省略時は現在のバージョンの 1 つ前（current − 1）がデフォルトになります。 |

#### 動作

1. `.reyn/skill-versions/<name>/current` から現在のバージョン番号を読み取る。
2. `--to` の指定があればその番号、なければ `current - 1` を対象バージョンとする。
3. `.reyn/skill-versions/<name>/<target>.md` の存在を確認する。
4. スナップショットの内容をスキルの `skill.md` にアトミック書き込みで上書きする。
5. `.reyn/skill-versions/<name>/current` を復元後のバージョン番号に更新する。
6. 確認メッセージを stdout に表示する。

#### stdlib スキルの制限

stdlib スキルへのロールバックは拒否されます。stdlib スキルは同梱ファイルであり
変更不可です。カスタマイズするには `reyn/project/<name>/` にコピーしてから、
プロジェクトコピーに対してロールバックしてください。

#### 終了コード

| コード | 意味 |
|------|---------|
| `0` | ロールバック成功。 |
| `1` | 拒否 — 対象が stdlib スキル。 |
| `2` | エラー — スキル未発見、バージョン未保存、または対象バージョンファイルなし。 |

#### 出力

```
Rolled back 'my_skill' from v3 to v2.
skill.md content restored from .reyn/skill-versions/my_skill/v2.md.
```

## 例

保存済みスナップショットを一覧表示:

```bash
reyn skill versions my_skill
```

1 つ前のバージョン（current − 1）へロールバック:

```bash
reyn skill rollback my_skill
```

特定のバージョンへロールバック:

```bash
reyn skill rollback my_skill --to v1
```

## スナップショットディレクトリ構造

```
.reyn/skill-versions/
  my_skill/
    v1.md      # 初回保存時のスナップショット
    v2.md      # 1 回目の改善後のスナップショット
    v3.md      # 2 回目の改善後のスナップショット
    current    # "3" が書かれたプレーンテキストファイル
```

スナップショットは `skill_improver`（FP-0006 Component B）が作成します。
このコマンドはスナップショットを読み取るだけで、新たに作成することはありません。

## 関連情報

- `reyn skills` — インストール済みスキルの一覧表示と詳細確認
- [リファレンス: stdlib/skill_improver](../stdlib/skill_improver.md) — スナップショットを作成する
