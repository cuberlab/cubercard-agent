---
name: cubercard
description: >
  用 cubercard CLI 為 Cuber Card 建立與維護單字、片語、句型、字母與 cloze 卡：從影片、書本、文字或 PDF 選詞，
  切音訊／影片片段並驗證、統一音量，寫 manifest 建卡；也能匯入 Anki .apkg（保留複習進度）、改卡、刪卡、暫停、調整排程。
  觸發詞：Cuber Card、單字卡、做卡、flashcard、背單字、vocab、cloze、克漏字、Anki 匯入、apkg。
---

# Cuber Card 卡片建立

卡片只透過 `cubercard` CLI（或 [docs/api.md](docs/api.md) 的 API）建立與修改；使用者在 Cuber Card iPhone app 複習。流程是：選詞 → 使用者確認 → 製作媒體並驗證 → 寫 manifest → `create --dry-run` → `create` → 回讀確認。

## 開始前

1. `cubercard status` 確認已登入、看現有牌組與儲存用量。失敗回 401 就請使用者在 app「設定 → API key」產生新 key，再跑 `cubercard auth`。
2. 用既有牌組的真實 `id`（`cubercard decks`）。要新牌組先 `cubercard decks create "🇫🇷 French" --id french`，命名沿用「旗幟 + 英文名稱」。
3. 查重：`cubercard cards list --deck <id> --all --json`，比對 `sort_key`（= term）與 `external_key`；已有的詞與同詞族不再做。
4. 影片流程確認有 `yt-dlp`、`ffmpeg`、Whisper（Apple Silicon 用 `mlx_whisper`，其他平台用 `whisper` 或 whisper.cpp）；文字或書本只需要實際用到的語音工具。

工作檔（完整影片、WAV、時間戳、截圖）放本次暫存目錄，建卡並回讀成功後清掉。

## 選詞

| 來源 | 做法 |
|---|---|
| Podcast、演講、訪談（英文） | 掃完全文的一般詞、片語、專業術語，再嚴格篩選；保留語境做影片卡 |
| 語言教學影片 | 只取目標語言的示範發音，不取英文解說或翻譯 |
| 書本、文字、PDF | 依使用者指定的範圍（書名、頁碼、章節）選詞；需要語音時用 TTS |

- **不湊詞**：沒有合格候選就不列，不設最低數量、不要求各類齊全。
- 每個候選都要在逐字稿、字幕或 Whisper 時間戳找到原文；摘要另外命名的概念不能做影片卡。
- 分三類列出，每項給編號：一般詞 W1、W2…、片語 P1、P2…、專業術語 T1、T2…。每項附中文、原文上下文、時間戳或頁碼。
- 片語要真的非字面（off-the-cuff ≠ cuff）；字面相加的普通搭配、填充語不收。
- 同詞族只收一個代表形（動詞優先）。
- 邊界候選（有點熟、可能值得複習）照列並註「可複習」，讓使用者決定。
- **使用者確認後才做**：說「go／ok／全部」就做整份；給編號就只做那幾項；修正翻譯就用修正版。

## 影片切片

1. `yt-dlp -f "bv*[height<=480]+ba/b[height<=480]" -o src.%(ext)s <URL>`；字幕只輔助定位，抓不到就直接轉錄音訊。
2. 轉 16 kHz 單聲道 WAV：`ffmpeg -i src.mp4 -ac 1 -ar 16000 src.wav`。
3. 逐詞時間戳：`mlx_whisper src.wav --model mlx-community/whisper-large-v3-turbo --word-timestamps True --output-format json`（其他平台的 Whisper 同樣開 word timestamps）。
4. 英語解說夾目標語言示範的教學片：分別用英語與目標語言各轉錄一次，交叉找真正的示範位置。
5. 依下表切片，通過「驗證」才進下一步。

| 來源 | 切法 | 輸出 |
|---|---|---|
| Podcast、演講、訪談 | 目標詞前後各留約 1.5 秒語境；每片 3–7 秒（優先 3–5 秒），100–250 KB | mp4：360p、H.264 CRF 28、AAC 64 kbps 單聲道 |
| 語言教學 | 緊貼發音，在前後靜音處切開，保留約 0.1 秒尾音 | mp3：44.1 kHz 單聲道、`libmp3lame -qscale:a 4`；另有影片卡需求才出 mp4 |

- 語言教學的切點：在目標詞前後約 0.3 秒內，用 25 ms RMS 視窗找連續至少 75 ms、低於 −45 dB 的靜音區段。
- 影片畫面會露出答案（拼字、數字教學）時，正面只放 mp3 發音，拼字與中文放背面。
- 範例：`ffmpeg -ss 12:03.2 -to 12:08.0 -i src.mp4 -vf scale=-2:360 -c:v libx264 -crf 28 -c:a aac -b:a 64k -ac 1 -movflags +faststart podcast_lucid.mp4`。

## 驗證（Cut → Verify → Upload）

絕對不能切完直接上傳碰運氣。

**Whisper 的可靠度**

| 片段長度 | 可靠度 | 策略 |
|---|---|---|
| > 1 秒 | 高 | Whisper 驗證可信 |
| 0.5–1 秒 | 中 | Whisper 只是提示，要搭配 amplitude 分析 |
| < 0.5 秒 | 低 | 跳過 Whisper，改用 amplitude 分析 |

已知問題：Whisper 會自動補完不完整的詞。一個淡出到一半的 "obrigado"，Whisper 仍輸出 "Obrigado"；反過來，一個完整但極短的片段可能輸出亂碼（"Ob…"、"of"）。Whisper 通過但切點可疑（淡出、截斷）時不信。

**必做檢查**

1. 片段 ≥ 0.5 秒時用目標語言重新辨識，必須轉出目標詞；混英語解說的教學來源另外用英語檢查，確認沒切到翻譯。
2. amplitude：解碼成 WAV，以 10 ms RMS 視窗看每個音節與尾音；用 −35 dB 能量門檻檢查弱音節。來源與輸出都要看，避免淡出切掉原本就很弱的尾音。
3. 音節間動態範圍 > 12 dB → 換 source。短於 0.5 秒、音節完整但弱音太小的片段，可先壓縮再檢查。
4. 字母音（尤其 < 1 秒）不能只靠 Whisper，要搭配字幕時間、波形與實際聽。

**換 source 的時機**：同一段試 3 次仍有問題就立刻換來源（目標詞在句尾被淡出吃掉、背景音樂蓋過、語速太快黏在一起都是常見原因）。有原講者發音先用原講者；換了來源仍不可用才改 TTS。來源只有文字／PDF，或使用者明確要合成語音時，可直接用 TTS。

## 音量

每個 mp3／mp4 上傳前統一到 **-16 LUFS（±1 LU）、真峰值 ≤ -1.5 dBTP**（EBU R128）。server 不複驗，manifest 的 `loudness_lufs` 填正規化後的量測值。

```sh
# 1. 量測：取 Summary 裡的 I（整合響度）與 Peak（真峰值）
measure() { ffmpeg -nostats -hide_banner -i "$1" -af ebur128=peak=true -f null - 2>&1 \
  | awk '/Summary/{s=1} s&&/ I:/{i=$2} s&&/Peak:/{p=$2} END{print i, p}'; }
read I TP <<<"$(measure raw.mp3)"

# 2. 增益 = -16 - I；alimiter 的 limit 0.8414 = -1.5 dBFS
GAIN=$(awk -v i="$I" 'BEGIN{printf "%.2f", -16 - i}')
ffmpeg -y -i raw.mp3 -af "volume=${GAIN}dB,alimiter=limit=0.8414:attack=5:release=50:level=false" \
  -ar 44100 -ac 1 -c:a libmp3lame -qscale:a 4 clip.mp3
# 影片：-c:v copy -c:a aac -b:a 96k -movflags +faststart clip.mp4

# 3. 驗證：I 在 -17～-15、Peak ≤ -1.5；不合就用新的 I 修正增益再做一次
measure clip.mp3
```

- 短於 0.4 秒的片段 ebur128 量不到（回報 -70）：量測時改用重複波形 `-af "aloop=loop=-1:size=2147483647,atrim=duration=3,ebur128=peak=true"`，不要補靜音。
- 有損編碼會讓峰值略超過 limiter；超過就把 limit 再壓低 0.3 dB 重做。峰值很高、limiter 壓不下來時在 `volume` 前加 `acompressor=threshold=-30dB:ratio=4:attack=3:release=80,`。
- 正規化或淡入淡出後重新檢查音節完整。

## 卡型與卡面

| 卡型 | 正面 | 背面 | 範例 |
|---|---|---|---|
| Podcast／英文影片 | `video` 片段 | `title`、`translation`、`quote`（`highlight` 目標詞）、`source`（節目、集名、日期，不放 URL） | [video-card.json](examples/video-card.json) |
| 語言教學發音 | `audio` | `title`（原文）、`translation`、`source`（影片標題） | — |
| 句型雙向 | A：音檔＋原句；B：只有中文 | A：中文＋`breakdown`；B：原句＋音檔＋`breakdown` | [sentence-two-way.json](examples/sentence-two-way.json) |
| 字母 | 只有字母 | 音檔、例字、來源 | [alphabet-card.json](examples/alphabet-card.json) |
| 純文字（書本） | `title`（＋TTS `audio` 選用） | `translation`、`quote`、`source`（書名、頁碼） | [text-card.json](examples/text-card.json) |
| cloze | `cloze` | 同一個 `cloze`＋補充 `text`／媒體 | [cloze-card.json](examples/cloze-card.json) |

- 句型：一句建兩張卡，用兩個不同且穩定的 `external_key`（例 `…:sq-zh`、`…:zh-sq`）；中文→目標語言那張的正面不能洩漏原文、拆解或音檔。拆解以「片語／詞 → 中文功能」為主，只放答案面。
- 字母：預設只做「字母→聲音」一張，不自動做反向。
- 媒體檔名用 ASCII 與底線，前綴依卡型（`podcast_`、`rv_`、`<language>_sentence_`、`<language>_alphabet_`、多講者合成用 `multi_`）。
- 卡面只放學習需要的內容；製作資訊（TTS 供應商、驗證狀態、批次說明）不上卡面。
- 第一組新卡型建好後，用 `cubercard cards get <id>` 看回傳的 front／back，確認正面沒有洩漏答案、背面內容完整，再大量建。

## Manifest 規則

```json
{"batch_id": "podcast:ep42:v1", "deck_id": "podcast", "cards": [ … ]}
```

- 必填非空字串：`batch_id`、`deck_id`（既有牌組），以及每張卡的 `external_key`、`term`、`definition`、`context`、`source`；`front`、`back` 是非空 block 陣列（每面 ≤ 50 個）。
- 選填：`kind`（`basic` 預設、`sentence`、`text_recall`、`alphabet`、`cloze`）、`lang`、`direction`（`listen`、`read`、`speak`、`write`）、`pos`（候選有詞性就一定填）、`template`、`history`、`state`、`suspended`。未知欄位會被 CLI 擋下。
- 文字 block：`title`、`text`、`quote`、`translation`、`source`，欄位 `text`（必填）、`lang`、`rtl`、`highlight`。`breakdown`：`rows: [{"source", "target"}]`。
- 媒體 block：`audio`、`video`、`image`，寫 `filename`（相對 `--media-dir`，不能是絕對路徑或含 `..`），可加 `autoplay`（audio 預設 true）、`loudness_lufs`。CLI 自己算 SHA-256、上傳缺的檔，送出時只帶 `sha256`。也可以直接寫已上傳檔案的 `sha256`。
- 顯示在卡面的文字都要明確寫成 block；`context`、`source` 欄位只是 metadata，不會自動顯示。
- HTML 不會被解讀；不要放 Anki 的 HTML 或 `[sound:]` 標記。
- 翻面時正面會留在畫面上、背面接在下面，所以背面不要重複正面已有的 block（同一個音檔、同一段標題）；Anki `{{FrontSide}}` 那種背面轉檔時會自動去掉重複。

## 冪等

- `external_key` 是這張卡的永久身分：同一張卡永遠用同一個 key，card id 由它導出。
- 重跑同一份 manifest（同 `batch_id`、同內容）不會重複建卡；CLI 每張印 `existing`，批次印 `replay`。
- 改了內容就換新的 `batch_id`；同 `batch_id` 不同內容 server 回 `batch_conflict`。
- 既有 `external_key` 內容不同 → `content_conflict`，整批都沒寫。先查清楚：是要改既有卡（`cubercard cards update`），還是 key 取錯。**不要為了繞過衝突另造 key**，那會建出重複卡。
- 超過 500 張由 CLI 切成 `<batch_id>:part001`…；中途失敗修好後整份重跑，已寫入的批次會 replay。切批是決定性的，所以只要 manifest 不變，重跑永遠切在同樣位置；加新卡請用新的 `batch_id` 另建一份。
- 新的 `batch_id` 引用內容完全相同的既有卡，只會回 `existing`，不會重複。

## Cloze

語法沿用 Anki：`{{c1::答案}}`、`{{c1::答案::提示}}`。同一句可以有 c1、c2…，每個編號是一張卡；同編號出現多次就一起挖空。

最簡單的寫法是一個 note：`kind: "cloze"`，front 放一個不帶 `index` 的 cloze block，back 放同一個 block 再加補充。CLI 會展開成每個編號一張卡，`external_key` 自動加 `:c1`、`:c2`，`term` 預設為該編號的答案、`definition` 預設為提示（沒有就用答案）、`context` 預設為填好答案的整句：

```json
{"external_key": "geo:capitals", "kind": "cloze", "source": "Geography notes",
 "front": [{"type": "cloze", "text": "Paris is the capital of {{c1::France::國家}}."}],
 "back":  [{"type": "cloze", "text": "Paris is the capital of {{c1::France::國家}}."}, {"type": "text", "text": "補充"}]}
```

也可以自己展開：每張卡帶 `index`，`external_key` 結尾必須是 `:c<index>`，back 的 cloze block 要和 front 同 `text` 同 `index`。

## Anki 匯入

1. 請使用者在 Anki 桌面版「檔案 → 匯出」，格式選「Anki Deck Package (*.apkg)」，**勾選「包含排程資訊」與「包含媒體」**。
2. 建目標牌組：`cubercard decks create "French" --id french`。
3. 預覽：`cubercard apkg-to-manifest French.apkg --deck french --out french-import --preview`。逐一核對每種 note type 的欄位對應、正反面欄位與卡數，把預覽給使用者確認。
4. 對應不對就寫 mapping（格式見 [examples/mapping.json](examples/mapping.json)：`term`／`definition`／`context` 欄位、`blocks` 欄位型態或 `skip`、`front`／`back`、`lang`、`kind`），用 `--mapping` 再預覽。
5. 確認後輸出：`cubercard apkg-to-manifest French.apkg --deck french --out french-import --mapping mapping.json`。
6. 媒體在 `french-import/media/`；音檔需要統一音量時照「音量」一節處理後覆蓋原檔（CLI 會重新算 hash）。
7. `cubercard create --manifest french-import/manifest.json --dry-run`，再正式 `create`。
8. 回讀：`cubercard cards list --deck french --all` 張數要等於預覽的總卡數；抽幾張複習過的卡 `cards get`，確認 `history` 與 `state` 存在。

說明：

- 每張 Anki 卡的作答紀錄會變成 `history`（`source: import`），進度用 FSRS-5（與 app 相同參數）重播 revlog 得到，不是沿用 Anki 的間隔，所以到期日可能與 Anki 略有不同。Anki 裡「忘記／重設」之前的作答仍保留在 history，但不影響狀態。
- Anki 的新卡保持新卡、順序照舊；Anki 暫停的卡匯入後也是暫停。
- 粗體、顏色、表格會變純文字；`[sound:]` → audio（影片副檔名 → video）、`<img>` → image。
- 同一份 .apkg 重跑是冪等的（`batch_id` 預設由檔案 hash 導出）。重新匯出更新過的 .apkg 時，內容沒變的卡回 `existing`，內容改過的卡會 `content_conflict`：先確認差異，再用 `cards update` 改，或在 mapping 用 `skip` 排除。
- 已存在的卡不會被新的 history／state 覆蓋。

## 改卡、刪卡、暫停、排程

- 改卡面：寫一個只含要改欄位的 JSON（例 `{"definition": "…", "back": [...]}`），`cubercard cards update <id> --manifest-card fix.json --media-dir clips/`。`kind`、`external_key` 不能改。
- 暫停／恢復：`cubercard cards suspend <id> --reason too_easy`（`too_easy`、`too_hard`、`not_interested`、`agent`、`import`）、`cubercard cards unsuspend <id>`。
- 刪卡：建錯、重複、要重做的卡用 `cubercard cards delete <id>`；學過但暫時不想看的用 suspend。有複習紀錄的卡 delete 會被擋（CLI 印出筆數、exit 1），確定要連進度一起刪才加 `--force`，先問過使用者。刪掉後同一份 manifest（同 `batch_id`）重送會重建這張卡；cloze 刪一張 `:cN` 不影響同 note 其他卡。
- 媒體不會跟卡一起刪，server 每天 04:30（Asia/Taipei）回收 24 小時以上沒有任何卡引用的媒體並釋放配額。要重建卡就在那之前重送，或讓 `cubercard create` 重新上傳（回收後 `media/check` 會列為 missing）。
- 排程：`cubercard schedule <id> --due 2026-12-01T09:00:00+08:00`（複習卡提前／延後）、`--new-position 0`（新卡排最前）、`--reset`（變回新卡）。CLI 會先讀 `state_version`；回 409 表示手機剛同步改過，CLI 會印出目前狀態，重新判斷再跑，不要硬蓋。
- 每牌組每日新卡數：`cubercard decks set <id> --new-per-day 10`。

## 出錯怎麼讀

CLI 原文印出 server 的 `error` 代碼與 `detail`，部分錯誤另附欄位（`missing`、`external_keys`、`state`）。先照 `detail` 修，常見情況：

| 代碼 | 處理 |
|---|---|
| `invalid_*`、`duplicate_external_key`（400） | `detail` 會指出哪張卡（external_key）哪個欄位，修 manifest 再跑 |
| `media_not_uploaded`（400） | `missing` 列出還沒上傳的 sha256；用 `filename` 讓 CLI 上傳，或確認 sha256 沒寫錯 |
| `hash_mismatch`（400） | 檔案在上傳途中變了；重跑 |
| `unauthorized`（401） | key 錯了或被撤銷；請使用者產生新 key，`cubercard auth` |
| `unknown_deck`（404） | 先 `cubercard decks create` |
| `batch_conflict`（409） | 這個 `batch_id` 已送過不同內容；新內容換新的 `batch_id` |
| `content_conflict`（409） | 見「冪等」；整批沒寫，不要另造 key |
| `concurrent_write`（409） | 有另一個請求同時在建同一批；重跑 |
| `version_conflict`（409） | 讀完之後被手機改過；重新讀再決定 |
| `has_history`（409） | 要刪的卡有複習紀錄（附 `reviews` 筆數）；改用 suspend，或確認後加 `--force` |
| `retry_upload`（503） | 上傳時共用的媒體剛好被回收；重跑 `create` |
| `too_many_cards`、`payload_too_large`（413） | 一般由 CLI 自動切批；單張卡太大就縮短文字，單檔超過 20 MB 就壓縮 |
| `quota_exceeded`（413） | 儲存配額用完，附用量；停下回報使用者 |
| `rate_limited`（429） | 每分鐘 300 次的限制 CLI 會自動等；每日 2,000 檔媒體上限用完就隔天再跑 |
| `internal`（500） | server 問題；原文回報，不要一直重試 |

卡住時把完整指令、CLI 印出的錯誤原文（不含 API key）與 manifest 片段一起回報給使用者。

## 收尾

回報新增／existing 張數、牌組、未完成項目與原因。不把沒有驗證過的卡說成完成。
