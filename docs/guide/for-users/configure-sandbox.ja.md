---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, reyn run]
---

# サンドボックスの設定

reyn のサンドボックス層は、オペレーターレベルでサブプロセス実行を隔離します。
オペレーターが `reyn.yaml` でバックエンドとポリシーを設定します。スキルは自身の封じ込めを制御できません。サンドボックスはパーミッションとは直交する概念です — [サンドボックスとパーミッション](../../concepts/architecture/sandbox-vs-permission.md)を参照してください。

## バックエンドの選択

```yaml
# reyn.yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
```

`backend: auto`（デフォルト）は現在のプラットフォームに最適なバックエンドを選択します:

| プラットフォーム | 条件 | バックエンド |
|---|---|---|
| macOS | `sandbox-exec` が利用可能 | Seatbelt（SBPL deny-default） |
| Linux | カーネル ≥ 5.13 かつ `sandbox-linux` パッケージインストール済み | Landlock（+ オプション seccomp-BPF） |
| その他 | — | Noop（監査のみ、封じ込め無し） |

`on_unsupported` は要求したバックエンドが利用不可の場合の動作を制御します:

| 値 | 動作 |
|---|---|
| `warn`（デフォルト） | 警告をログに記録し Noop にフォールバック |
| `error` | エラーを発生させる — 封じ込めが必須の環境で使用 |
| `ignore` | サイレントに Noop にフォールバック |

## エージェントレベルのサンドボックスポリシーの設定

`sandbox.policy` により、オペレーターが決定論的なサンドボックスポリシーを宣言できます。設定されている場合、すべての `sandboxed_exec` op **と** パーミッション交差の `SandboxLayer` に適用されます — スキルや LLM はこれを広げることができません。

```yaml
sandbox:
  backend: auto
  policy:
    network: false
    write_paths:
      - "{{workspace}}/output"
    read_deny_paths:
      - "~/.ssh"
      - "~/.aws"
    timeout_seconds: 120
```

`sandbox.policy` が省略されている場合（デフォルト）、エージェントレベルの制限はありません: op レベルのフィールドが適用され、SandboxLayer は無制限です。

### ポリシーフィールド

| フィールド | 型 | デフォルト | 意味 |
|---|---|---|---|
| `network` | bool | `false` | アウトバウンドネットワークを許可。主要な外部流出ゲート。 |
| `write_paths` | パスのリスト | `[]` | プロセスが書き込めるパス（厳密なガード）。書き込みは読み取りを含む。 |
| `read_deny_paths` | パスのリスト | `[]` | 広読み込みサーフェスから拒否する機密パス（多層防御）。deny-after-allow をサポートするバックエンド（Seatbelt）のみ適用。Landlock では非対応。 |
| `read_paths` | パスのリスト | `[]` | レガシー — かつての厳密な読み込み許可リスト。現在、読み込みはデフォルトで広許可のためこのフィールドは意図した読み込み対象のドキュメントとしてのみ機能します。 |
| `allow_subprocess` | bool | `false` | 子プロセスの生成を許可。Linux (seccomp) / macOS (Seatbelt) ともに適用。 |
| `env_passthrough` | 文字列のリスト | `[]` | プロセスに引き渡す環境変数名。`PATH` は常に引き渡されます。 |
| `timeout_seconds` | int | `60` | ウォールクロック制限。期限超過でプロセスを終了。 |

### スコーピングモデル

reyn は**広読み込み・厳密書き込み・ネットワークゲート**モデルを採用しています:

- **読み込みは広許可。** プロセスはファイルシステムの大部分を読み取れます。ポリシーに列挙しなくても dylib 読み込み用のシステムパスが機能します。
- **ネットワークが外部流出ゲート。** `network: false`（デフォルト）により、プロセスは広く読み取れますがデータを送信できません。
- **書き込みは厳密。** `write_paths` に記載されたパスのみ書き込み可能。
- **`read_deny_paths` は多層防御。** バックエンドが deny-after-allow を表現できる場合に、広読み込みサーフェスから機密箇所を除外します。

## バックエンド別の動作

### Seatbelt（macOS）

SBPL deny-default プロファイルを使った `sandbox-exec` を使用。macOS で最も強力な封じ込め。

| フィールド | 適用 |
|---|---|
| `write_paths` | 適用 |
| `network` | 適用 |
| `read_deny_paths` | **適用** — SBPL deny-after-allow |
| `allow_subprocess` | **適用** — off の時 `process-fork` を deny（対象自身の exec は `process-exec*` で動作、#1914） |
| `timeout_seconds` | 適用 |

### Landlock（Linux）

Linux Landlock LSM のパス以下許可リストルールを使用。

| フィールド | 適用 |
|---|---|
| `write_paths` | 適用 — path-beneath 書き込みルール |
| `network` | Linux 6.7+（ABI v4）で適用。旧カーネルでは警告ログを出力 |
| `read_deny_paths` | **非対応** — Landlock は許可リストのみで、許可した親から子パスを除外できない。ネットワークゲートが主要な外部流出制御。 |
| `allow_subprocess` | 利用可能な場合 seccomp-BPF で適用 |
| `timeout_seconds` | 適用 |

### Noop

封じ込めは適用されません。ポリシーフィールドは監査ログに記録されますが動作には影響しません。封じ込めが利用不可の信頼された環境でのみ使用してください。

## コンテナで実行する（マウントモード）

最も強力な隔離を行うため、またはホスト OS に関わらず一貫した Linux 環境でスキルを実行するために、Docker バックエンドを使用します:

```bash
# 新しいコンテナを起動（マウントモード）
reyn run my_skill --env-backend=docker

# 特定のイメージを使用
reyn run my_skill --env-backend=docker --image my-registry/my-image:latest

# 追加のバインドマウントを指定
reyn run my_skill --env-backend=docker \
  --mount /data/inputs:/data/inputs:ro \
  --mount /data/outputs:/data/outputs:rw

# 実行後もコンテナを残す（検査用）
reyn run my_skill --env-backend=docker --keep-container

# 既存の実行中コンテナにアタッチ
reyn run my_skill --env-backend=docker --container my-container --repo-dir /workspace
```

マウントモードでは、ワークスペースルートが自動的にコンテナ内の `/workspace` にバインドマウントされます。コンテナ内で使用されるサンドボックスバックエンドは `reyn.yaml sandbox.backend` で決まります（通常 Linux では `landlock`）。

### デフォルトイメージ

`--image` を省略した場合、reyn は現在のプラットフォーム向けにビルドされたバンドルベースイメージを使用します。カスタムイメージを使用するには `--image` を渡すか、`reyn.yaml` でデフォルトを設定してください（[`reyn.yaml` リファレンス](../../reference/config/reyn-yaml.md)参照）。

### devcontainer.json

ワークスペースに `devcontainer.json`（`.devcontainer/devcontainer.json` または `.devcontainer.json`）がある場合、reyn は最小サブセット（`image` / `postCreateCommand` / `mounts` / `remoteUser`）を読み取って起動のデフォルトに反映します。明示的な `--image` は常に devcontainer より優先されます。

- **image ベース**（`image: ...`）— そのまま起動。
- **build ベース**（`dockerFile` / `build`）— reyn が Dockerfile を**オンデマンドでビルド**（`docker build`）して起動します。ビルド済みイメージは内容ハッシュでタグ付けされ、Dockerfile / build args / target が変わったときのみ再ビルドされます。`build.args` と `build.context` に対応。
- **compose ベース**（`dockerComposeFile`）— 非対応（ランチャーは単一コンテナ）。警告を出してデフォルトイメージにフォールバックします。

!!! warning "ビルドはワークスペースの Dockerfile をホストで実行します"
    build ベース devcontainer のビルドは、その Dockerfile の `RUN` ステップを**ビルド時にホストの Docker デーモン上で**実行します。これは reyn のランタイムサンドボックスでは保護されません（network-off / non-root / read-only-rootfs は*実行中*コンテナに適用され、`docker build` には適用されません）。これは VS Code の「Reopen in Container」と同じ信頼モデルです。信頼できるワークスペースの build ベース devcontainer のみ使用してください。reyn はビルドをログ出力します。`--env-backend=docker` がオプトインです。

## 関連ドキュメント

- [コンセプト: サンドボックスとパーミッション](../../concepts/architecture/sandbox-vs-permission.md) — 両者が直交する理由
- [コンセプト: サンドボックス](../../concepts/runtime/sandbox.md) — バックエンドフィールドリファレンスとスコーピングモデルの詳細
- [リファレンス: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `sandbox:` 設定スキーマ全体
- [ハウツー: パーミッションの管理](manage-permissions.md) — スキルレベルの機能パーミッションの宣言と承認
