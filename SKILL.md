---
name: cubercard
description: >
  用 cubercard CLI 為 Cuber Card 建立與維護單字、片語、句型、字母與 cloze 卡：從影片、書本、文字或 PDF 選詞，
  切音訊／影片片段並驗證、統一音量，寫 manifest 建卡；也能匯入 Anki .apkg（保留複習進度與牌組樹）、管理子牌組、
  改卡、搬卡、刪卡（進垃圾桶、可復原）、暫停、調整排程。
  觸發詞：Cuber Card、單字卡、做卡、flashcard、背單字、vocab、cloze、克漏字、Anki 匯入、apkg、子牌組、垃圾桶。
---

# Cuber Card 卡片建立

卡片只透過 `cubercard` CLI（或 [docs/api.md](docs/api.md) 的 API）建立與修改；使用者在 Cuber Card iPhone app 複習。流程是：選詞 → 使用者確認 → 製作媒體並驗證 → 寫 manifest → `create --dry-run` → `create` → 回讀確認。

## 開始前

1. `cubercard status` 確認已登入、看現有牌組與儲存用量。失敗回 401 就請使用者在 app「設定 → API key」產生新 key，再跑 `cubercard auth`。
2. 用既有牌組的真實 `id`（`cubercard decks tree` 看整棵樹，`cubercard decks` 看平的清單與 `parent`）。要新牌組先 `cubercard decks create "🇫🇷 French" --id french`，命名沿用「旗幟 + 英文名稱」；子牌組加 `--parent <上層 id>`，見「牌組樹」。
3. 查重：`cubercard cards list --deck <id> --all --json`，比對 `sort_key`（= term）與 `external_key`；已有的詞與同詞族不再做。
4. 影片流程確認有 `yt-dlp`、`ffmpeg`、Whisper（Apple Silicon 用 `mlx_whisper`，其他平台用 `whisper` 或 whisper.cpp）；文字或書本只需要實際用到的語音工具。

工作檔（完整影片、WAV、時間戳、截圖）放本次暫存目錄，建卡並回讀成功後清掉。

## 牌組樹

牌組是一棵樹，不是用名字裡的 `::` 分層：

- 每個牌組有自己的 `parent`（沒有就是最上層），最多 5 層。名字只寫自己這一層，**不准含 `::`**（CLI 先擋，server 也回 400）。「English › Grace」= 建 `English`，再建 `Grace --parent english`。
- 同一個上層底下不能有同名牌組。母牌組自己也可以有卡。
- 牌組只由 agent 建（app 沒有建牌組功能）；使用者在 app 可以排序、改名、移動、刪牌組。
- **每日新卡上限只管這個牌組自己的卡**，不蓋子牌組。複習母牌組時，子樹裡每個牌組各自依自己的上限出新卡再合起來；app 今日頁母牌組那列是子孫加總。要整棵一起改就加 `--apply-to-subdecks`。

| 要做的事 | 指令 |
|---|---|
| 看樹（自己的卡數、含子牌組的卡數、每日新卡） | `cubercard decks tree` |
| 建子牌組 | `cubercard decks create "Grace" --id grace --parent english` |
| 改名 | `cubercard decks rename grace "Grace 2"` |
| 移到別的上層／最上層（子牌組跟著走） | `cubercard decks move grace --to turkish`、`cubercard decks move grace --top-level` |
| 每日新卡 | `cubercard decks set english --new-per-day 10`（加 `--apply-to-subdecks` 寫進所有子牌組） |
| 把一張卡搬到別的牌組 | `cubercard cards move <card id> --to grace` |

移動時目標不能是自己或自己的子孫（400 `invalid_parent`）。搬卡後 card id 不變；原本的 manifest 不要再送到舊牌組，否則舊牌組會多出一張新卡。

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

1. 將來源網址設為 `URL`，執行 `SOURCE_VIDEO="$(yt-dlp -f 'bv*[height<=480]+ba/b[height<=480]' -o 'src.%(ext)s' --print after_move:filepath --no-simulate "$URL")"`，取得實際下載路徑；字幕只輔助定位，抓不到就直接轉錄音訊。
2. 轉 16 kHz 單聲道 WAV：`ffmpeg -i "$SOURCE_VIDEO" -ac 1 -ar 16000 src.wav`。
3. 逐詞時間戳：`mlx_whisper src.wav --model mlx-community/whisper-large-v3-turbo --word-timestamps True --output-format json`（其他平台的 Whisper 同樣開 word timestamps）。
4. 英語解說夾目標語言示範的教學片：分別用英語與目標語言各轉錄一次，交叉找真正的示範位置。
5. 依下表切片，通過「驗證」才進下一步。

| 來源 | 切法 | 輸出 |
|---|---|---|
| Podcast、演講、訪談 | 目標詞前後各留約 1.5 秒語境；每片 3–7 秒（優先 3–5 秒），100–250 KB | mp4：360p、H.264 CRF 28、AAC 64 kbps 單聲道 |
| 語言教學 | 緊貼發音，在前後靜音處切開，保留約 0.1 秒尾音 | mp3：44.1 kHz 單聲道、`libmp3lame -qscale:a 4`；另有影片卡需求才出 mp4 |

- 語言教學的切點：在目標詞前後約 0.3 秒內，用 25 ms RMS 視窗找連續至少 75 ms、低於 −45 dB 的靜音區段。
- 影片畫面會露出答案（拼字、數字教學）時，正面只放 mp3 發音，拼字與中文放背面。
- 範例：`ffmpeg -ss 12:03.2 -to 12:08.0 -i "$SOURCE_VIDEO" -vf scale=-2:360 -c:v libx264 -crf 28 -c:a aac -b:a 64k -ac 1 -movflags +faststart podcast_lucid.mp4`。

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
2. 預覽：`cubercard apkg-to-manifest French.apkg --out french-import --preview`。逐一核對每種 note type 的欄位對應、正反面欄位、卡數，以及最後印出的**牌組樹**（每個牌組的 id 與卡數），把預覽給使用者確認。
3. 對應不對就寫 mapping（格式見 [examples/mapping.json](examples/mapping.json)：`term`／`definition`／`context` 欄位、`blocks` 欄位型態或 `skip`、`front`／`back`、`lang`、`kind`），用 `--mapping` 再預覽。
4. 確認後輸出：`cubercard apkg-to-manifest French.apkg --out french-import --mapping mapping.json`。會寫出：
   - `decks.json`：Anki 的 `A::B::C` 拆成三個牌組，由上到下 `[{"id", "name", "parent"}]`。
   - 每個有卡的 Anki 牌組一份 manifest（只有一個牌組時是 `manifest.json`，多個時是 `manifest-<deck id>.json`），`deck_id` 是卡實際所在的那個牌組。
   - `media/`：所有 manifest 共用。
5. 媒體需要統一音量時照「音量」一節處理後覆蓋原檔（CLI 會重新算 hash）。
6. 每份 manifest 各跑一次 `cubercard create --manifest french-import/<manifest> --dry-run`，再正式 `create`。`create` 會**先依 manifest 旁的 `decks.json` 建缺的牌組，再建卡**：已存在的牌組跳過；名字或上層和 server 不同只印 `warning:`、不會改（照使用者意思用 `decks rename`／`decks move` 自己改）。第二份以後的 manifest 牌組都已存在，只建卡。
7. 回讀：`cubercard decks tree` 確認樹與卡數；每個牌組 `cubercard cards list --deck <id> --all` 張數要等於預覽；抽幾張複習過的卡 `cards get`，確認 `history` 與 `state` 存在。

牌組 id 規則：Anki 有自己 deck id 的牌組（葉，以及 Anki 有紀錄的中間層）用 `anki-<Anki deck id>`；Anki 沒有紀錄的中間層用 `anki-path-<完整路徑 A::B 的 SHA-256 前 12 hex>`。同一來源的 Anki deck id 會導出相同牌組 id。名字只存最後一段（`Grace`，不是 `English::Grace`）。Anki 篩選牌組（filtered deck）裡的卡算回原本的牌組。

要把整份 .apkg 全部放進一個既有牌組、不管 Anki 的樹時，改加 `--deck <id>`：只輸出一份 `manifest.json`、不寫 `decks.json`。輸出目錄已有 `decks.json` 時會在寫入前拒絕，改用新的 `--out`。

說明：

- 每張 Anki 卡的作答紀錄會變成 `history`（`source: import`），進度用 FSRS-5（與 app 相同參數）重播 revlog 得到，不是沿用 Anki 的間隔，所以到期日可能與 Anki 略有不同。Anki 裡「忘記／重設」之前的作答仍保留在 history，但不影響狀態。
- Anki 的新卡保持新卡、順序照舊；Anki 暫停的卡匯入後也是暫停。
- 粗體、顏色、表格會變純文字；`[sound:]` → audio（影片副檔名 → video）、`<img>` → image。
- 同一版本轉檔器、相同 mapping 與目標牌組產生的 manifest 可冪等重送（`batch_id` 預設由檔案 hash 導出；指定 `--deck` 時也包含目標牌組）。重新匯出更新過的 .apkg 時，內容沒變的卡回 `existing`，內容改過的卡會 `content_conflict`：先確認差異，再用 `cards update` 改，或在 mapping 用 `skip` 排除。
- 轉檔器升級後重新轉檔可能改變卡面；0.2.2 的輸入答案與 cloze 正反面修正就是如此。重試未完成匯入時沿用原 manifest；要採用新卡面時先比對，再用 `cards update` 修改指定卡片，不能另造 key 繞過衝突。
- 已存在的卡不會被新的 history／state 覆蓋。

## 改卡、暫停、刪除、排程

- 改卡面：寫一個只含要改欄位的 JSON（例 `{"definition": "…", "back": [...]}`），`cubercard cards update <id> --manifest-card fix.json --media-dir clips/`。`kind`、`external_key` 不能改。
- 排程：`cubercard schedule <id> --due 2026-12-01T09:00:00+08:00`（複習卡提前／延後）、`--new-position 0`（新卡排最前）、`--reset`（變回新卡）。CLI 會先讀 `state_version`；回 409 表示手機剛同步改過，CLI 會印出目前狀態，重新判斷再跑，不要硬蓋。
- 每牌組每日新卡數：`cubercard decks set <id> --new-per-day 10`（只管這個牌組自己的卡）。

### 暫停與刪除的差別

| | 暫停 | 刪除 |
|---|---|---|
| 什麼時候用 | 卡沒錯，只是使用者現在不想看（太簡單、太難、沒興趣） | 卡本身錯了：建錯、重複、要重做 |
| 指令 | `cubercard cards suspend <id> --reason too_easy`（`too_easy`、`too_hard`、`not_interested`、`agent`、`import`）；`cubercard cards unsuspend <id>` | `cubercard cards delete <id>` |
| 留下什麼 | 卡、進度、**暫停理由**都留著。使用者在 app 暫停的理由是給 agent 看的回饋：`cards list --json` 的 `suspended`、`suspend_reason`，做下一批卡時參考（例如太簡單的詞族不再做） | 卡進垃圾桶，7 天後連進度一起清掉 |

### 刪除與垃圾桶

- `cubercard cards delete <id>`：卡**進垃圾桶，7 天內** `cubercard cards restore <id>` 可以連進度一起拿回來。卡在垃圾桶時 app、`cards list`、牌組卡數都看不到它。
- `cubercard decks delete <id>`：**牌組連同所有子牌組和裡面所有的卡**一起進垃圾桶；`cubercard decks restore <id>` 整棵拿回來。原上層已經不在就回到最上層（CLI 印 warning）；名字和現有兄弟撞名會自動加「 (2)」。
- 卡的牌組在垃圾桶時，`cards restore` 會失敗（409 `deck_deleted`），CLI 會印出要跑的 `decks restore`。
- `cubercard trash list`：看垃圾桶（被刪的牌組只列根，附子牌組數與卡數；單獨刪的卡列 term 與牌組），各項的 `purge_at` 是清掉的時間。
- **`--purge` 才是真的刪**：`cards delete <id> --purge`、`decks delete <id> --purge`、`trash empty` 直接永久刪除，進度與複習紀錄一起消失，無法復原。一定要先問過使用者、得到明確同意才用；平常刪除都用預設（進垃圾桶）。
- 同一份 manifest（同 `batch_id`）重送：卡已永久刪除就重建；卡還在垃圾桶就先永久刪掉那張（含進度）再建新卡。要的是原本的進度就用 `restore`，不要重送。cloze 刪一張 `:cN` 不影響同 note 其他卡。
- 媒體不會跟卡一起刪，server 每天 04:30（Asia/Taipei）先清掉過期的垃圾桶，再回收 24 小時以上沒有任何卡引用的媒體並釋放配額（垃圾桶裡的卡仍算引用）。回收後要再用就讓 `cubercard create` 重新上傳（`media/check` 會列為 missing）。

## 出錯怎麼讀

CLI 原文印出 server 的 `error` 代碼與 `detail`，部分錯誤另附欄位（`missing`、`external_keys`、`state`）。先照 `detail` 修，常見情況：

| 代碼 | 處理 |
|---|---|
| `invalid_*`、`duplicate_external_key`（400） | `detail` 會指出哪張卡（external_key）哪個欄位，修 manifest 再跑 |
| `media_not_uploaded`（400） | `missing` 列出還沒上傳的 sha256；用 `filename` 讓 CLI 上傳，或確認 sha256 沒寫錯 |
| `hash_mismatch`（400） | 檔案在上傳途中變了；重跑 |
| `unauthorized`（401） | key 錯了或被撤銷；請使用者產生新 key，`cubercard auth` |
| `unknown_deck`（404） | 先 `cubercard decks create`，或在 `decks.json` 補上 |
| `invalid_parent`（400） | 上層不存在、是自己或自己的子孫，或超過 5 層；換一個上層 |
| `deck_exists`（409） | 牌組 id 已存在；`cubercard decks` 看不到就是在垃圾桶，`cubercard decks restore <id>` |
| `deck_name_taken`（409） | 同一個上層底下已有同名牌組；改名或用那個既有牌組 |
| `deck_deleted`（409） | 要復原的卡，它的牌組在垃圾桶；`cubercard decks restore <deck_id>` |
| `card_exists`（409） | 搬卡的目標牌組已有同 `external_key` 的卡；先問使用者留哪張 |
| `batch_conflict`（409） | 這個 `batch_id` 已送過不同內容；新內容換新的 `batch_id` |
| `content_conflict`（409） | 見「冪等」；整批沒寫，不要另造 key |
| `concurrent_write`（409） | 有另一個請求同時在建同一批；重跑 |
| `version_conflict`（409） | 讀完之後被手機改過；重新讀再決定 |
| `retry_upload`（503） | 相同媒體仍在上傳、保留逾期或物件剛被回收；CLI 對同本文媒體 PUT 最多短期重試 5 次，仍失敗才重跑 `create` |
| `too_many_cards`、`payload_too_large`（413） | 一般由 CLI 自動切批；單張卡太大就縮短文字，單檔超過 20 MB 就壓縮 |
| `quota_exceeded`（413） | 儲存配額用完，附用量；停下回報使用者 |
| `rate_limited`（429） | 每分鐘 300 次的限制 CLI 會自動等；每日 2,000 檔媒體上限用完就隔天再跑 |
| `internal`（500） | server 問題；原文回報，不要一直重試 |

卡住時把完整指令、CLI 印出的錯誤原文（不含 API key）與 manifest 片段一起回報給使用者。

## 收尾

回報新增／existing 張數、牌組、未完成項目與原因。不把沒有驗證過的卡說成完成。

牌組搬移與匯入會在寫入交易內重新檢查目的牌組；目的已刪除時回 `unknown_deck`，牌組樹在同時搬移形成循環或超過深度時回 `invalid_parent`，先重讀牌組樹再決定。`card_delete`／`card_restore` 以事件 id 去重後才改資料，事件 id 不決定操作先後；同 id 重送不會再次刪除或還原。匯入遇到已搬入的卡片會回其實際 `note_id`。媒體上傳的暫存保留以帳號與 SHA-256 為單位；不同帳號共用內容雜湊不會互相擋住，內容仍須通過雜湊驗證。
