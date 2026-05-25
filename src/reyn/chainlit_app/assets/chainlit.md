# reyn × Chainlit

[reyn](https://github.com/tya5/reyn) は LLM 駆動の **Phase × Skill × OS** エージェントランタイム。 この画面は `reyn chainlit` で立ち上がる Web チャット surface (= TUI `reyn chat` / FastAPI `reyn web` と並列)。

## できること

- メッセージを送るとアタッチ済の agent (= 既定 `default`) が応答
- 歯車 ⚙ で **output_language / model / agent role / 新規 agent 作成** を panel 経由
- chat-profile 切替で agent 切替 (= 履歴 replay 自動、 上限は `REYN_CHAINLIT_HISTORY_CAP` 環境変数)
- tool 実行は `🔧 tool` collapsible step (= click で args / result / error 展開)
- permission gate は `[y]es / [A]lways / [n]o / [N]ever` button で round-trip
- slash: `/agents` `/skills` `/list` `/cost` quick action / `/clear-history confirm` / `/agent edit role <text>`
- 添付ボタンで png/jpg upload → 次 prompt で agent が認識

## エージェント管理

- **既存に切替**: welcome 画面上部の chat-profile picker (`default` / `test219` / ... など)
- **新規作成**: 歯車 ⚙ → 「Create new agent」 TextInput に名前入力 → save、 もしくは `/agent new <name>` slash
- **persona 編集**: 歯車 ⚙ → 「Agent role」 TextInput、 もしくは `/agent edit role <text>` slash

> ⚠ 左上「新規チャット」 ボタンは chainlit 標準のセッションリセット (= 同 agent の thread 再描画) で、 **新規 agent 作成ではない**。 新規 agent は上記の panel か slash から。

## まだできないこと (= follow-on)

- streaming (= token-by-token 表示)
- multi-user 真の isolation (= 同 process で 2 user 同時 attach 上書き)
- agent rename / delete from UI (= `reyn agent rm` CLI 経由)
- allowed_skills / allowed_mcp 編集 UI

## 操作

ブラウザ画面下の入力欄から普通にメッセージを送るだけ。 設定変更は cwd の `chainlit.md` (このファイル) と `.chainlit/config.toml` を編集 → Chainlit が hot-reload。
