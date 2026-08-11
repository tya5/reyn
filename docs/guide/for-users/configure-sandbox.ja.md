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
| Linux | カーネル ≥ 5.13 かつ `sandbox-linux` パッケージインストール済み | Landlock **+ seccomp-BPF（両方必須）** |
| その他 | — | Noop（監査のみ、封じ込め無し） |

この表のバックエンドは、あなたのマシンで**封じ込め self-test に通った場合にのみ**使用されます（後述）。

`on_unsupported` は使用可能なバックエンドが無い場合の動作を制御します — 要求したバックエンドがこのプラットフォームに存在しない場合に加えて、**存在するが実際には封じ込めていない場合**も含みます:

| 値 | 動作 |
|---|---|
| `warn`（デフォルト） | 警告をログに記録し Noop にフォールバック |
| `error` | エラーを発生させる — 封じ込めが必須の環境で使用 |
| `ignore` | サイレントに Noop にフォールバック |

## Reyn はサンドボックスが本当に封じ込めているかを検査します

Reyn はバックエンドを選択する際、まずそれが**あなたのマシンで実際に動作すること**を確かめます: そのバックエンド経由で短いサブプロセスを起動し、ポリシーが禁じている場所へのファイル書き込みを試みます。拒否されればサンドボックスは本物であり、Reyn はそれを使います。書き込みが通ってしまえば、そのバックエンドは封じ込めていない ∴ Reyn はそれを**インストールされていない場合とまったく同じに扱い**、`on_unsupported` の設定を適用します。

これが要るのは、「サンドボックスがインストールされている」と「サンドボックスが動作している」が別の主張だからです。バックエンドは、存在し import でき、それでいて何一つ封じ込めていない、という状態になり得ます — OS は正しく、パッケージの import も通り、しかし制限だけが静かに不在。存在するかだけを見る検査では、この2つを区別できません。∴ Reyn は本当に知りたい方を検査します: 禁じた操作が実際に拒否されるか。

期待される挙動:

- **通常は何も出ません。** 動作しているサンドボックスは静かに通ります。コストは1プロセスあたり一度、数十ミリ秒 — しかもサンドボックスを実際に使う run だけが払います。
- **封じ込めていない場合**、起動時に「何を試みて何が起きたか」を名指しした警告が出ます — 黙ったまま非サンドボックスで走る代わりに。
- **`on_unsupported: error` の場合**、AI 生成コードを非サンドボックスで実行するくらいなら Reyn は実行を拒否します。この設定は「存在しない」だけでなく「壊れている」サンドボックスにも効くようになりました。

警告が出た場合、あなたの AI コードは封じ込め無しで走っていたということです。メッセージはバックエンドと失敗内容を名指しするので、修正するか、意図して fail-closed にするかを選べます。

**範囲**: この検査が witness するのはファイルシステムの書き込み境界です。ネットワークゲートや syscall フィルタ層は exercise しません ∴ 検査に通ったことは「1つの制限が証明された」ことであり、下表のすべての制限の保証ではありません。

## エージェントレベルのサンドボックスポリシーの設定

`sandbox.policy` により、オペレーターが決定論的なサンドボックスポリシーを宣言できます。設定されている場合、すべての `sandboxed_exec` op **と** `network`/`subprocess`/`env` 軸に関してパーミッション交差の `SandboxLayer` に適用されます — スキルや LLM はこれを広げることができません。`write_paths`（および read/write deny リスト）はこの交差に**参加しません** — op が必要とするディレクトリはオペレーターが事前に知り得ない値なので、パーミッション ∩ ではなくカーネルバックエンドが直接消費します（#3901）。

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

`write_paths` を除く全フィールドが完全 compat（#3901 owner ruling）をデフォルトとします — サンドボックスの役割は許可された操作の**裏側**を bound することであり、起動元シェルが既にできることを再決定することではありません。

| フィールド | 型 | デフォルト | 意味 |
|---|---|---|---|
| `network` | bool | `true`（compat） | アウトバウンドネットワークを許可。主要な外部流出ゲート — config で allow された host であっても `network: false` の下では拒否されます。 |
| `write_paths` | パスのリスト | `[]` | プロセスが書き込めるパス（厳密なガード）— デフォルトで閉じている唯一のフィールド。書き込みは読み取りを含む — ここに挙げたパスは `read_deny_paths` が deny していても*読み取り*が再開されるため、`~` ではなく具体的なディレクトリを許可すること。`~` は展開される。 |
| `read_deny_paths` | パスのリスト | `[]`（compat） | 広読み込みサーフェスから拒否する機密パス（多層防御、**opt-in**）。deny-after-allow をサポートするバックエンド（Seatbelt）のみ適用。Landlock では非対応。#3901 以前は OS レベルの機密パス7件がデフォルトだった — その保護を戻すには明示的に設定する。読み込み軸のみを deny する — 書き込み軸は `write_deny_paths` を参照。 |
| `write_deny_paths` | パスのリスト | `[]` | 書き込み軸専用の deny リスト（#3901）、`read_deny_paths` と対をなす。書き込み軸のみを deny する — #3901 以前は `read_deny_paths` が Seatbelt 上で（未文書化の副作用として）書き込みも deny していたが、その結合は解消された。両軸で保護したい場合は両方のフィールドに列挙する。 |
| `deny_subprocess` | bool | `false`（compat） | 子プロセスの生成を deny。Linux (seccomp) / macOS (Seatbelt) ともに適用。 |
| `env_deny_names` | 文字列のリスト | `[]`（compat） | プロセスに引き渡さない環境変数名。デフォルト（空）は環境全体が引き渡される、つまり起動元シェルと同じ信頼レベルを意味します。 |
| `timeout_seconds` | int | `120` | 前景 exec のウォールクロック既定（期限超過でプロセスを終了）── LLM の `exec` 呼び出しが独自の timeout を省略した場合に適用。`#3903①` で旧 `60` から引き上げ。 |
| `max_timeout_seconds` | int | `600` | 前景 exec の LLM 拡張可能な上限 ── LLM は自身の `timeout` でこの値まで要求できるが、超えることはできない。超過は無言のクランプではなく型付きエラー。 |
| `background_timeout_seconds` | int | `3600` | 背景 exec 専用の既定値（`#3903` a-2）── exec が **ephemeral** セッション（`spawn_ephemeral_session`）内で実行され、LLM の呼び出しが独自の timeout を省略した場合に `timeout_seconds` の代わりに適用される。 |
| `background_max_timeout_seconds` | int \| `null` | `null`（上限なし） | 背景 exec 専用の上限 ── `null`/未設定は無制限を意味する。整数を設定すれば上限を課せる。上限超過時は警告をログし、実効値をその上限までクランプする（`#4174` T0 の warn-not-fail posture と同じ）── 無言で無制限に通すことはない。 |

🔴 **ここでいう「背景」は「ephemeral セッション」を意味し、「誰も待っていない」ではありません。** `spawn_ephemeral_session` 自身の exec はこのペアを得ますが、**persistent** な spawn セッションの exec（同じく fire-and-forget で誰も待っていない）はこのペアを得ません ── 依然として上の前景ペアが適用され、既知のギャップとして [#4193](https://github.com/tya5/reyn/issues/4193) に起票済みで、この改修の範囲外です。逆に、**attached** で駆動される ephemeral セッション（`run_pipeline_attached` ── 実際に誰かが待っている）は、依然として背景ペアを得ます ── 実際に読まれるシグナルは ephemeral かどうかであって、「この呼び出しに前景/背景のどちらが選ばれたか」ではないためです。ワークロードの timeout 上、本当に重要な区別が「自分は待たれているか」なら、この表はその近似であって完全な一致ではないと理解してください。

**`deny_subprocess: true` は、exec を一切必要としない workload にとって最も安価で最も予測可能な hardening です。** 設定は単一の boolean で、その効果は全面的かつ即時です — 子プロセス生成が完全に拒否され、後から状態がずれて驚くことはありません。exec が本当に必要な workload（ビルドステップ、CLI ラッパー等）にはこの設定は向きません — その場合はサンドボックス境界と、exec のたびに残る監査証跡（`sandboxed_exec_started`/`_completed`/`_cancelled` が `argv` を記録します — [Reference: events](../../reference/runtime/events.md) 参照）で bound されます。

### スコーピングモデル

reyn は**広読み込み・厳密書き込み・ネットワークゲート**モデルを採用しています:

- **読み込みは広許可。** プロセスはファイルシステムの大部分を読み取れます。ポリシーに列挙しなくても dylib 読み込み用のシステムパスが機能します。
- **ネットワークはデフォルトで compat**（#3901 owner ruling B）— サンドボックスは起動元シェルが既に到達できるものを再決定しません。プロセスを分離するには `network: false` を明示的に設定してください。設定すれば広く読み取れますがデータは送信できません。
- **書き込みは厳密。** `write_paths` に記載されたパスのみ書き込み可能 — デフォルトで閉じている唯一の軸（オペレーターが事前に知り得ない値）。
- **`read_deny_paths`/`write_deny_paths` は多層防御、opt-in。** バックエンドが deny-after-allow を表現できる場合に、広い読み書きサーフェスから機密箇所を除外します。デフォルトは空（何も除外されない）。

## バックエンド別の動作

### Seatbelt（macOS）

SBPL deny-default プロファイルを使った `sandbox-exec` を使用。macOS で最も強力な封じ込め。

| フィールド | 適用 |
|---|---|
| `write_paths` | 適用 |
| `network` | 適用。`network` の値に関わらず、loopback 限定の `network-bind`（`localhost:*`）は常に許可される（Landlock の `socket`/`bind` の例外と同じ理由、[#3060](https://github.com/tya5/reyn/issues/3060)）— `network-outbound`/`network-inbound` は引き続き `network` でゲートされる。 |
| `read_deny_paths` | **適用** — SBPL deny-after-allow |
| `write_deny_paths` | **適用** — SBPL deny-after-allow、`read_deny_paths` とは独立（#3901: 各軸をそれぞれ独立に deny） |
| `deny_subprocess` | **適用** — on の時 `process-fork` を deny（対象自身の exec は `process-exec*` で動作） |
| `timeout_seconds` | 適用 |

### Landlock（Linux）

Linux Landlock LSM のパス以下許可リストルールを使用。

| フィールド | 適用 |
|---|---|
| `write_paths` | 適用 — path-beneath 書き込みルール |
| `network` | **無条件に適用**（[#3030](https://github.com/tya5/reyn/issues/3030) で修正済み）。Landlock 自体はどのカーネルでもネットワークを制限しない（pin された `landlock` パッケージがネットワークルール API を持たない）ため、deny は seccomp-BPF のデフォルト拒否**アローリスト**だけが担う — 名前に無い syscall（`network: false` 時の `connect`/`sendmsg`/`accept`/`listen` を含め、さらに syscall 名の denylist では表現できない `io_uring_setup`/`io_uring_enter` も無条件に）は全て拒否される。このフィルタは以前 `deny_subprocess` が off（`false`、stdio MCP の既定）のとき丸ごとスキップされ、ネットワークゲートも道連れになっていたが、今は無条件にロードされるため `network: false` は `deny_subprocess` の値に関わらず適用される。`network` の値に関わらず常に許可される例外は2つ（[#3060](https://github.com/tya5/reyn/issues/3060)）: **(1)** `socket`/`bind` — どちらか単独ではバイトの送受信は発生せず、よく使われる HTTP クライアント依存の import 時 IPv6 対応プローブ（`::1` のポート 0 に `bind` するだけで `connect` はしない）が巻き添えで拒否されていた副作用を解消するため; **(2)** `sendto`/`recvfrom` で**アドレス引数が NULL のとき** — CPython asyncio のイベントループが自身を起こすための connected な AF_UNIX socketpair（`send`/`recv` が NULL アドレスの `sendto`/`recvfrom` syscall に落ちる）で、これを丸ごと拒否すると全ての stdio MCP server のループが pump できず server が 0 バイトしか返せなくなっていた。実際にピアへダイヤルするには引き続き `connect`（拒否）が必要で、`sendto` の**アドレス付き**形（`sendto(fd, …, &sockaddr, …)` = 実 UDP egress）はアドレスが非 NULL ゆえ同じ条件で拒否される。 |
| `read_deny_paths` | **非対応** — Landlock は許可リストのみで、許可した親から子パスを除外できない。ネットワークゲート（上の `network` 行を参照）が代償の外部流出制御であり、#3030 以降は `deny_subprocess` の値に関わらず適用される。秘密を読めるプロセスの封じ込めをこのプラットフォームに依存しないこと — ネットワーク拒否は持ち出しを止めるだけ。 |
| `write_deny_paths` | **非対応** — `read_deny_paths` と同じ許可リストのみの制約 |
| `deny_subprocess` | 利用可能な場合 seccomp-BPF で適用 |
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
