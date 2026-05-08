---
type: how-to
topic: web
audience: [human]
applies_to: [reyn/local/designs/, claude.ai/design]
---

# Reyn の Web UI を自作する

**目的：** Reyn のエンジンに触れることなく、Web UI を独自のビジュアルスタイルに差し替えます。最速の方法は `claude.ai/design` を使うことです。このページにはコピーボタン付きの貼り付け用プロンプトを用意しています。

## 仕組み（図解）

```
[あなたのデザイン]  ⇄  [OpenUI Layer 0 protocol]  ⇄  [Reyn エンジン]
   ↑                  (window.OPENUI_HOST)         ↑
   あなたが作る            （ロック済み）           触れない
```

Reyn の Web シェルはランタイムにエンジンを接続します。あなたのデザインは OpenUI プロトコルに従うだけでよく、Reyn 固有のグルーコードは不要です。

## 1. プロンプトをコピーする

ブロック右上の **コピー** アイコンをクリックし、新しい `claude.ai/design` スレッドに貼り付けてください。

````markdown
# Reyn Design Prompt

You are designing a UI for **Reyn**, a workflow-engine-driven agent OS.
The design will integrate with Reyn via the **OpenUI Layer 0 protocol**
and the **reyn-ui/v1 Layer 1 schema**.

<!-- =================================================================
  🔓 EDITABLE — fill in your design brief below.
  Edit freely. This is your design intent.
================================================================= -->

## Your design brief

**Brand voice** (1–2 sentences):
> [REPLACE: e.g. "Warm and approachable but technically credible.
> Inspired by Linear's clarity and Stripe's precision."]

**Primary color**: [REPLACE: coral / amber / teal / monochrome / your-pick]
**Mode**: [REPLACE: light / dark / both]
**Density**: [REPLACE: cozy / comfortable / dense]

**Typography**:
- Body: [REPLACE: e.g. Inter, Geist, Söhne]
- Display (optional): [REPLACE: e.g. Instrument Serif for App headers only]
- Mono (Studio only): [REPLACE: e.g. JetBrains Mono, IBM Plex Mono]

**Screens to prioritise** (App side):
- [REPLACE: Today, Conversation, Agent gallery, Library card → guided run]

**Screens to prioritise** (Studio side):
- [REPLACE: Conversation+inspector, Skill graph, Run timeline, Permissions]

**Inspirations** (optional): [REPLACE]
**Avoid** (optional): [REPLACE]

<!-- =================================================================
  🔒 LOCKED — DO NOT EDIT BELOW THIS LINE.
  This is the OpenUI / reyn-ui/v1 protocol contract. Editing it
  will break the engine ↔ design integration.
================================================================= -->

## What is Reyn

Reyn lets non-technical users converse with specialist AI agents and
lets developers build & ship those agents from Markdown. The Reyn UI
has **two faces**:

- **App** — friendly end-user surface (default landing). Tone:
  Claude.ai / OpenClaw / ChatGPT. Hides engine vocabulary entirely.
- **Studio** — dense developer surface. Build & debug skills, inspect
  runs, edit permissions. Tone: Linear / Vercel / Cursor / Temporal /
  LangSmith. Surfaces engine vocabulary verbatim.

The two faces share agent identity (name, color, avatar) and a
top-right App ↔ Studio toggle, but **share nothing else** — different
chrome, density, vocabulary.

## How the design connects to the engine

Reyn implements the **OpenUI Layer 0 protocol**. The design reads four
globals on `window`:

- `window.OPENUI_HOST` — `{ invoke(action, payload?), listen(channel, handler) }`
- `window.OPENUI_DATA` — initial data (shape: `ReynUiData`)
- `window.OPENUI_SCHEMA` — `"reyn-ui/v1"`
- `window.OPENUI_DESIGN_MODE` — `true` standalone, `false` embedded

## Required components, data shape, actions, channels

Specified canonically in:

- Component contracts: `docs/deep-dives/spec/openui/schemas/reyn-ui-v1/components.md`
- Data shape (`ReynUiData`): `docs/deep-dives/spec/openui/schemas/reyn-ui-v1/data.types.ts`
- Actions / channels: `docs/deep-dives/spec/openui/schemas/reyn-ui-v1/manifest.yaml`

Treat as the contract: every required component must be exported,
prop shapes must match, action / channel names must be used as defined.

## Hard rules

- **No hardcoded mock data inside components.** Read from
  `window.OPENUI_DATA` when embedded, fallback mock when standalone.
  Mock lives in a separate `data.js`.
- **All user actions go through `window.OPENUI_HOST.invoke()`.** Every
  user-driven side-effect (sending a message, answering an
  intervention, attaching to an agent, switching face, accepting a
  permission, cancelling a run) MUST `await
  window.OPENUI_HOST.invoke(<action>, <payload>)`. Local-only state
  updates stay in component state.
- **All async data goes through `window.OPENUI_HOST.listen()`** with
  unsubscribe on unmount.
- **No `fetch` / `XMLHttpRequest` / `WebSocket` calls in components.**
- **No global state libraries** (Zustand, Redux, …). Local state only.
- **No bundler / framework configs.** The host owns the build.
- **App face vocabulary**: never expose `phase`, `artifact`,
  `control_ir`, `event`, `validation`, `schema`. Studio uses these
  verbatim.
- **i18n**: App face strings come from `OPENUI_DATA.COPY[lang]`.
- **Two HTML entries (REQUIRED)**:
  - `Reyn.html` — host-mountable runtime, hash-routed App/Studio
    mount, no artboards. This is what `reyn web` loads.
  - `Reyn UI.html` — design canvas with artboards (designer mode only).
- **`Reyn.html` MUST trigger Babel transformation explicitly** if it
  uses babel-standalone:

  ```html
  <script>
    (function () {
      var t = setInterval(function () {
        if (window.Babel && Babel.transformScriptTags) {
          clearInterval(t);
          Babel.transformScriptTags();
        }
      }, 50);
    })();
  </script>
  ```

  Auto-runner fires on DCL, which has already passed when the host
  shell injects the design.

## Now generate

Append one of these on the next line and send:

> `→ App + Studio` (recommended — both faces in one export)
>
> `→ App` (App face only)
>
> `→ Studio` (Studio face only)

Begin by enumerating which screens you'll cover and your token /
typography proposal, then iterate.
````

## 2. ブリーフを記入する

`claude.ai/design` の中で、`🔓 EDITABLE` マーカー間のセクションのみを編集します。各 `[REPLACE: …]` プレースホルダーをあなたの選択に置き換えてください。`🔒 LOCKED` 以降はすべてそのままにしておいてください。

## 3. ビジュアルを反復する

キャンバスのチャットで色、レイアウト、コピーを平易な英語で調整します。OpenUI のグローバル変数やコンポーネント名の変更は Claude Design に求めないでください — それらは Reyn エンジンの接続を支えるロック済み contract です。

## 4. エクスポートして配置する

**Export → `.zip`** を使用し、その後以下を実行します。

```bash
DESIGN=warm-coral   # 任意のスラッグ
mkdir -p "reyn/local/designs/$DESIGN"
unzip ~/Downloads/Reyn-export.zip -d "reyn/local/designs/$DESIGN"
```

`reyn web` を再起動またはリフレッシュしてください。デザインピッカーが自動的に検出します。

## ブリーフの例

### デフォルトコーラル

```
Brand voice: Warm and approachable, like a knowledgeable friend.
             Inspired by Claude.ai App tone and Stripe's precision.
Primary color: coral
Mode: light
Density: comfortable
Body: Inter
Display: Instrument Serif (App headers only)
Mono: JetBrains Mono (Studio)
Inspirations: Claude.ai, Linear, OpenClaw
Avoid: generic SaaS purple, Tailwind-default look
```

### ダークモノクローム（ターミナル風）

```
Brand voice: Quiet competence. The interface gets out of the way.
             Inspired by Vercel and the bare elegance of well-tuned
             terminal apps.
Primary color: monochrome (zinc 50→950)
Mode: dark
Density: dense
Body: Inter
Mono: JetBrains Mono
Inspirations: Vercel dashboard, Linear's dark mode, Warp
Avoid: any color that isn't a neutral
```

### Nordic

```
Brand voice: Cool, precise, breathing. Inspired by Nordic minimal
             design and the Notion-but-quieter aesthetic.
Primary color: muted blue-gray (oklch 0.62 0.06 240)
Mode: both
Density: cozy
Body: Inter
Display: Söhne (App)
Mono: IBM Plex Mono
Inspirations: Notion, Things 3, Bear Notes
Avoid: anything saturated
```

## トラブルシューティング

- **白い画面** — ホストシェルがデザインのグローバル変数を認識できていません。最も多い原因: `Babel.transformScriptTags()` ポーリングスニペットが欠けています。
- **デザインがピッカーに表示されない** — `reyn/local/designs/<slug>/Reyn.html` が存在するか確認してください。シェルはこのファイル名を正確に取得します。
- **エンジンがメッセージに反応しない** — `submit` ハンドラーが `window.OPENUI_HOST.invoke('agent.submit', { agentId, text })` を呼び出していません。PR31 時代の v1 エクスポートにはこのバグがありました。デザインの `app-screens.jsx` と `studio-screens.jsx` の配線を確認してください。
- **スタイルが適用されない** — デザインの `<link href="styles.css">` タグはシェルではなくデザイン自身の HTML の中に含まれている必要があります。シェルは相対 URL を自動的に書き換えます。

## 関連情報

完全な操作ガイドと OpenUI Layer 0 プロトコル仕様はリポジトリ内の `docs/deep-dives/spec/design/` および `docs/deep-dives/spec/openui/` にあります（= 内部 contract ドキュメント、公開サイトの一部ではありません）。より詳細が必要な場合は GitHub でご確認ください。

- `docs/deep-dives/spec/design/design-author-guide.md` — 完全な操作ガイド（このハウツーより詳細）
- `docs/deep-dives/spec/design/multi-design-selection.md` — 3 ルートレイアウト（`local` / `project` / バンドル済み）、選択優先度
- `docs/deep-dives/spec/openui/` — OpenUI Layer 0 プロトコル仕様（デザインを作るだけなら読む必要はありません。ロックゾーンが強制する contract です）
