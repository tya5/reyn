# reyn × Chainlit

[reyn](https://github.com/tya5/reyn) は LLM 駆動の **Phase × Skill × OS** エージェントランタイム。 この画面は `reyn chainlit` で立ち上がる Web チャット surface (= TUI `reyn chat` / FastAPI `reyn web` と並列)。

## できること (PoC scope)

- メッセージを送るとアタッチ済の agent (= 既定 `default`) が応答
- skill の実行結果 / status / error は author 別 (= `agent` / `skill` / `status` / `error`) で表示
- 同 agent state (history / skills / events) を TUI / web と共有

## まだできないこと (= follow-on)

- streaming (= token-by-token 表示)
- intervention の双方向応答 (= `cl.AskUserMessage` round-trip)
- right panel 相当 (= cost / events / agents / docs)
- multi-user 真の isolation (= 同時 2 user で attach 上書き)

## 操作

ブラウザ画面下の入力欄から普通にメッセージを送るだけ。 slash command (`/help` 等) は未配線 (= follow-on)。

設定変更したい場合: cwd の `chainlit.md` (このファイル) と `.chainlit/config.toml` を編集 → Chainlit が hot-reload。
