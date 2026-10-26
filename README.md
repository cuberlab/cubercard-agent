# cubercard-agent

Cuber Card 的 agent kit：命令列工具 `cubercard`、給 AI agent 載入的 [`SKILL.md`](SKILL.md)、[API 合約](docs/api.md) 與 [manifest 範例](examples/)。卡片由 agent 透過 API 建立與維護；複習在 Cuber Card iPhone app 上做。

## 安裝

需要：

- Python 3.12 以上
- [uv](https://docs.astral.sh/uv/)
- ffmpeg（統一音量、切片；macOS 可用 `brew install ffmpeg`）

```sh
git clone https://github.com/cuberlab/cubercard-agent.git
cd cubercard-agent
uv tool install .
cubercard --version
```

更新：`git pull && uv tool install --reinstall .`。不想裝成全域指令時，在 repo 內用 `uv run cubercard …`。

## 拿 API key

1. 在 iPhone 打開 Cuber Card，登入。
2. 進「設定」→「API key」→「產生」，取個名字（例如 `openclaw`）。
3. key（`ck_live_` 開頭）只顯示這一次，複製下來。
4. 在要跑 agent 的電腦執行 `cubercard auth`，貼上（輸入不會顯示）。

key 存在 `~/.config/cubercard/credentials`（權限 600，有設 `XDG_CONFIG_HOME` 時改用該目錄）。也可以不存檔，改用環境變數 `CUBERCARD_API_KEY`（與選用的 `CUBERCARD_API_BASE`）；環境變數優先。key 外流時到同一頁撤銷再產生新的。

## 指令

每個指令都有 `--help`。server 回錯誤時，CLI 原文印出 `error` 代碼與 `detail`，結束碼 1；本機檔案或參數錯誤結束碼 2。

### `auth`：存 key

```sh
cubercard auth                       # 互動輸入
echo "$KEY" | cubercard auth         # 非互動從 stdin 讀
cubercard auth --base http://127.0.0.1:8787   # 對本機開發 server
```

會打 `GET /v1/me` 驗證，成功才存檔並印出帳號 email。

### `status`：帳號、牌組、儲存用量

```sh
cubercard status
```

### `decks`：牌組

```sh
cubercard decks                                   # 列表（同 decks list）
cubercard decks create "🇫🇷 French" --id french --new-per-day 15
cubercard decks set french --new-per-day 10
```

### `create`：依 manifest 建卡

```sh
cubercard create --manifest examples/text-card.json --dry-run
cubercard create --manifest batch.json --media-dir clips/
```

- 先在本機驗證整份 manifest（規則見 [docs/api.md](docs/api.md) 與 [SKILL.md](SKILL.md)）。
- 媒體 block 寫 `filename`（相對 `--media-dir`，預設 manifest 所在目錄）；CLI 算 SHA-256，`POST /v1/media/check` 只上傳缺的，送給 server 的 block 只帶 `sha256`。
- 超過 500 張（或單批超過約 1.9 MB）自動切成 `<batch_id>:part001`、`part002`…依序送；某批失敗就停下並印出是哪一批，前面的批次已寫入，修好後整份重跑即可（已寫入的批次會 replay）。
- 每張卡印 `created` 或 `existing`。同一份 manifest 重跑不會重複建卡。
- `--dry-run` 只驗證與規劃；有 key 時另外查牌組是否存在、哪些媒體要上傳（唯讀）。

### `cards`：查卡、改卡、刪卡、暫停

```sh
cubercard cards list --deck french                  # 一頁 100 張；--all 自動翻頁；--json 原始資料
cubercard cards list --deck french --external-key anki:1690000000000:0
cubercard cards get c_0123456789abcdef0123456789abcdef
cubercard cards update c_0123… --manifest-card fix.json --media-dir clips/
cubercard cards suspend c_0123… --reason too_easy   # too_easy | too_hard | not_interested | agent | import
cubercard cards unsuspend c_0123…
cubercard cards delete c_0123…                      # 有複習紀錄會被擋（exit 1），要連進度一起刪加 --force
```

`update` 的檔案是一張 manifest 卡裡要改的欄位，例如 `{"definition": "短暫的", "back": [...]}`；CLI 先讀目前的 `content_version` 再送，新媒體一樣先上傳。不能改 `kind`、`external_key`。

### `schedule`：改一張卡的排程

```sh
cubercard schedule c_0123… --due 2026-12-01T09:00:00+08:00   # 複習卡提前／延後
cubercard schedule c_0123… --new-position 0                   # 新卡排到最前面
cubercard schedule c_0123… --reset                            # 忘掉進度，變回新卡
```

CLI 先讀卡片的 `state_version` 當 `based_on_version`。手機剛好同步改過時 server 回 409，CLI 印出 server 目前的狀態，重新判斷後再跑。

### `apkg-to-manifest`：Anki 匯入

```sh
cubercard decks create "French" --id french
cubercard apkg-to-manifest ~/Downloads/French.apkg --deck french --out french-import/ --preview
cubercard apkg-to-manifest ~/Downloads/French.apkg --deck french --out french-import/ --mapping examples/mapping.json
cubercard create --manifest french-import/manifest.json --dry-run
cubercard create --manifest french-import/manifest.json
```

- 讀 `.apkg` 內的 `collection.anki21b`（新版 zstd 格式）、`collection.anki21` 或 `collection.anki2`。
- 每個欄位變成文字 block：預設第一個欄位當 `term`／`title`、第二個當 `definition`／`translation`、其餘 `text`；正反面依卡片模板引用了哪些欄位決定。`[sound:x]` 變 `audio`（影片副檔名變 `video`）、`<img src>` 變 `image`，其他 HTML 去掉。
- Cloze note type 依 `{{cN::}}` 展開成 N 張 `kind: cloze` 卡，external_key `anki:<note_id>:c<N>`；一般卡 `anki:<note_id>:<ord>`。
- 每張卡的 revlog 變成 `history[]`，並用 FSRS-5（與 app 相同參數）重播出 `state`；新卡維持新卡，順序沿用 Anki；Anki 暫停的卡匯入後也是暫停。
- 媒體解到 `--out/media/`，manifest 寫到 `--out/manifest.json`。輸出前先印每種 note type 的欄位對應預覽與卡數；`--preview` 只印不寫。
- `--mapping` 指定欄位對應，格式見 [examples/mapping.json](examples/mapping.json)：每個 note type 可設 `term`、`definition`、`context`（欄位名）、`kind`、`lang`、`source`、`blocks`（欄位 → `title`／`text`／`translation`／`quote`／`source`／`skip`）、`front`／`back`（欄位清單）或 `templates`（依模板名稱或 ord 分別設 `front`／`back`）、`skip`。
- 不做音量正規化；做法見 SKILL.md「音量」。

## 範例

| 檔案 | 內容 |
|---|---|
| [`examples/video-card.json`](examples/video-card.json) | Podcast 影片卡：正面影片片段，背面字詞、中文、原句、出處 |
| [`examples/sentence-two-way.json`](examples/sentence-two-way.json) | 句型雙向卡：目標語言→中文、中文→目標語言各一張 |
| [`examples/alphabet-card.json`](examples/alphabet-card.json) | 字母卡：正面字母，背面發音與例字 |
| [`examples/text-card.json`](examples/text-card.json) | 純文字卡（不需要媒體） |
| [`examples/cloze-card.json`](examples/cloze-card.json) | cloze：一個 note 自動展開成 c1、c2，以及一張已展開的 cloze 卡 |
| [`examples/mapping.json`](examples/mapping.json) | `apkg-to-manifest --mapping` 的欄位對應 |

範例裡的媒體檔名只是示意，repo 不附檔案；實際使用時換成自己的檔案並用 `--media-dir` 指到所在目錄。

## 開發

```sh
uv sync
uv run pytest
CUBERCARD_SERVER_DIR=/path/to/cuber-card/server scripts/e2e.sh   # 對本機 wrangler dev 跑完整流程
```

`tests/fixtures/fsrs_vectors.json` 是 iPhone app 與 CLI 共用的 64 組 FSRS 向量，兩邊都必須全過。
