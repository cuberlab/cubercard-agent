# Cuber Card API 合約

給 agent 讀的人話版本。機器可讀的權威版本是 server 的 `GET /v1/openapi.json`；兩者不一致時以 openapi.json 為準。

## 通則

| 項目 | 規則 |
|---|---|
| Base | `https://api.card.cuberlab.tw`，所有路徑在 `/v1` 底下 |
| 格式 | JSON、snake_case；時間一律 UTC ISO 8601，結尾 `Z`（例 `2026-10-25T08:00:00Z`），server 存到秒 |
| 憑證 | 只收 `Authorization: Bearer <key>`，不收 query string。agent 用 API key（`ck_live_` 開頭），在 app 設定頁產生，全文只顯示一次 |
| 錯誤 | `{"error": "<code>", "detail": "<說明>"}`，部分錯誤另帶欄位（例 `missing`、`external_keys`、`state`）。代碼表見文末 |
| 限流 | 每把 key 每分鐘 300 次、每日新上傳媒體 2,000 檔；超過回 429 與 `Retry-After`（秒） |

API key 不能打 `/v1/auth/*` 與 `/v1/api-keys`（回 403 `session_required`）；其他端點與手機 session 權限相同。

## 帳號

### `GET /v1/me`

```json
{"id": "u_…", "email": "someone@example.com", "display_name": "…", "created_at": "…",
 "identities": [{"provider": "google", "email": "…", "is_private_email": false, "created_at": "…"}],
 "storage_used_bytes": 1234, "storage_quota_bytes": 5368709120}
```

### `GET /v1/health`（不需憑證）

`{"status": "ok", "version": "0.1.0"}`

## 牌組

| 端點 | 本文 | 回應 |
|---|---|---|
| `GET /v1/decks` | — | `{"decks": [Deck]}` |
| `POST /v1/decks` | `{"name", "id"?, "new_per_day"?, "sort_order"?}` | 201 `Deck`；id 已存在 409 `deck_exists` |
| `PATCH /v1/decks/{id}` | `name`、`new_per_day`、`sort_order` 任選 | 200 `Deck` |

`Deck = {"id", "name", "new_per_day", "card_count", "sort_order"}`。`id` 要符合 `[a-z0-9][a-z0-9_-]{0,63}`，不給由 server 產生；`new_per_day` 0–9999，預設 20。改 `new_per_day` 以 server 當下時間參與「後寫者勝」，手機之後送來較舊的設定不會蓋掉。

## 媒體

1. 本機算每個檔案的 SHA-256（小寫 hex）。
2. `POST /v1/media/check`，本文 `{"sha256": [...]}`（最多 200 個），回 `{"present": [...], "missing": [...]}`。
3. 對 `missing` 的每個檔 `PUT /v1/media/{sha256}`：本文是檔案原始 bytes，必帶 `Content-Length` 與 `Content-Type`（`audio/*`、`video/*`、`image/*`）。單檔 ≤ 20 MB。
   - 201 `{"sha256", "size", "mime", "status": "created"}`；這個帳號已有同一份時 200 `status: "exists"`。
   - server 邊收邊算 SHA-256，不符回 400 `hash_mismatch` 並丟棄。
4. manifest 只引用已上傳的 sha256；沒上傳就建卡回 400 `media_not_uploaded`，附 `missing`。

`GET /v1/media/{sha256}` 下載（支援 `Range`、`ETag`／`If-None-Match`）；`HEAD` 取 metadata。只有上傳過該檔的帳號拿得到，其他帳號一律 404。

配額：每人儲存 5 GiB（以每人上傳過的檔案大小總和計算），超過回 413 `quota_exceeded` 並附 `storage_used_bytes`、`storage_quota_bytes`。

## 建卡：`POST /v1/cards/batch`

```json
{"batch_id": "podcast:ep42:v1", "deck_id": "podcast", "cards": [ManifestCard, …]}
```

一批最多 500 張、本文最多 2 MB；`cubercard create` 會自動切批。

**ManifestCard**

| 欄位 | 必填 | 說明 |
|---|---|---|
| `external_key` | 是 | 這張卡在你這邊的穩定 ID（≤ 512 字）。同 deck 內唯一，決定 card id |
| `term`、`definition`、`context`、`source` | 是 | 非空字串。`term` 也是排序鍵 |
| `front`、`back` | 是 | 非空 block 陣列（每面 ≤ 50 個） |
| `kind` | 否 | `basic`（預設）、`sentence`、`text_recall`、`alphabet`、`cloze` |
| `lang` | 否 | 語言代碼，預設 `none` |
| `direction` | 否 | `listen`、`read`、`speak`、`write` |
| `pos`、`template` | 否 | 詞性；模板名稱（預設 `Native`） |
| `history` | 否 | `[{"rating": 1–4, "reviewed_at": "…Z", "duration_ms": 整數}]`，匯入的作答紀錄（`source: import`），不能是未來時間 |
| `state` | 否 | `CardState`，見下方；不給就是新卡，`new_position` 依 manifest 順序排在 deck 最後 |
| `suspended` | 否 | `true` 建立時就暫停 |

**Blocks**

| type | 欄位 |
|---|---|
| `title`、`text`、`quote`、`translation`、`source` | `text`（必填）、`lang`、`rtl`、`highlight`（`quote` 用來粗體標出目標詞） |
| `breakdown` | `rows: [{"source", "target"}]`（非空）、`lang`、`rtl` |
| `audio`、`video`、`image` | `sha256`（必填，已上傳）、`autoplay`（audio 預設 true）、`loudness_lufs`（-70～0，agent 量測值，server 只記錄） |
| `cloze` | `text`（含 `{{cN::答案::提示}}`）、`index`（這張卡挖第幾號）、`lang`、`rtl` |

未列出的欄位與 type 一律 400 `invalid_blocks`。回應裡媒體 block 存成 `{"type", "media": "<sha256>", "autoplay"}`。

**cloze 規則**：`kind: "cloze"` 時 front 恰好一個 cloze block；back 的 cloze block 要同 `text` 同 `index`；`external_key` 結尾必須是 `:c<index>`。同一個 note 的各張卡（去掉 `:cN` 後相同）共用 `note_id`。非 cloze 卡不能有 cloze block。

**回應**

```json
{"batch_id": "…", "deck_id": "…", "replay": false, "created": 2, "existing": 0, "history_inserted": 1, "cursor": "42",
 "cards": [{"external_key": "…", "id": "c_…", "note_id": "n_…", "status": "created"}]}
```

- 新寫入 201；同 `batch_id`、同內容重跑 200 `replay: true`，什麼都不寫。
- 同 `batch_id` 內容不同：409 `batch_conflict`。
- 新 `batch_id` 引用內容完全相同的既有卡：`status: "existing"`，不重複建。
- 既有 `external_key` 但內容不同：409 `content_conflict`（附 `external_keys`），整批都不寫。要改既有卡用 `PATCH /v1/cards/{id}`。
- 既有卡的 `history`、`state` 會被忽略（不覆蓋進度）。
- card id = `c_` + SHA-256(`["card", user_id, deck_id, external_key]`) 前 32 hex；note id 同理用 `["note", user_id, deck_id, note_key]`。

## 查卡與改卡

| 端點 | 說明 |
|---|---|
| `GET /v1/cards?deck_id=&external_key=&after=&limit=&history=1` | 依 id 排序分頁；`limit` 1–500（預設 100）；`next_after` 不是 null 就帶進 `after` 取下一頁 |
| `GET /v1/cards/{id}` | 單張卡，含 `history` |
| `PATCH /v1/cards/{id}` | 改卡面：`term`、`definition`、`context`、`source`、`pos`、`template`、`lang`、`direction`、`front`、`back` 任選，可帶 `based_on_content_version`；成功 `content_version + 1`。不能改 `kind`、`deck`、`external_key`。版本不符 409 `version_conflict` 附目前卡片 |

**Card**：`id`、`deck_id`、`note_id`、`external_key`、`kind`、`template`、`lang`、`direction`、`sort_key`、`front`、`back`、`content_version`、`state`、`suspended`、`suspend_reason`、`suspended_at`、`history`（`[{reviewed_at, rating, duration_ms, source}]`）。

## 排程：`PATCH /v1/cards/{id}/state`

| 本文 | 說明 |
|---|---|
| `{"based_on_version": N, "due": "…Z"}` | 提前／延後；只限非新卡 |
| `{"based_on_version": N, "new_position": 0}` | 新卡順序；只限新卡 |
| `{"based_on_version": N, "reset": true}` | 重置成新卡（排到 deck 最後） |
| `{"based_on_version": N, "state": CardState}` | 寫入整個狀態（例如 CLI 用 FSRS 算好的），不能和上面三個混用 |
| `{"suspended": true, "suspend_reason": "agent"}` | 暫停；原因 `too_easy`、`too_hard`、`not_interested`、`agent`（預設）、`import` |
| `{"suspended": false}` | 恢復 |

改排程一定要帶 `based_on_version` = 目前的 `state.state_version`（先 GET 再寫）。不符回 409 `version_conflict`，本文附 `state`（server 目前狀態）；重新讀、重新判斷再送。只改 `suspended` 不需要版本，也不會動 `state_version`。

**CardState**

```json
{"queue": "new|learning|review|relearning", "due": "…Z 或 null", "new_position": 整數或 null,
 "stability": 天數或 null, "difficulty": 1–10 或 null, "step": 0, "reps": 0, "lapses": 0,
 "last_review": "…Z 或 null", "state_version": 1}
```

新卡 `due` 必須是 null；非新卡必須有 `due`、`stability`、`difficulty`，`new_position` 必須是 null。

## 同步（手機用，agent 通常不需要）

- `GET /v1/bootstrap`：全部牌組、卡片（含 history）、媒體、`cursor`、`scheduler`（FSRS-5 參數）、儲存用量。
- `POST /v1/sync`：`{"device_id", "cursor", "events": [...]}`（≤ 1,000 事件），回 `accepted`、`duplicate`、`rejected`、`conflicts`、新 `cursor` 與之後的變動。

## 錯誤代碼

| HTTP | `error` | 意思 | 處理 |
|---|---|---|---|
| 400 | `invalid_json`、`invalid_manifest`、`invalid_field`、`invalid_blocks`、`invalid_cloze`、`invalid_history`、`invalid_state` | 格式不對，`detail` 會指出欄位與 external_key | 照 `detail` 修 manifest |
| 400 | `duplicate_external_key` | 同一批有重複 `external_key` | 改成唯一 |
| 400 | `media_not_uploaded` | 引用的 sha256 還沒上傳，附 `missing` | 先 `PUT /v1/media/{sha256}` |
| 400 | `hash_mismatch`、`length_mismatch`、`empty_media`、`invalid_sha256` | 上傳內容和路徑的 sha256 或 Content-Length 不符 | 重算 hash 重傳 |
| 401 | `unauthorized` | key 錯誤或已撤銷 | 到 app 設定頁產生新 key，重跑 `cubercard auth` |
| 403 | `session_required` | API key 不能打這個端點 | 改用 app |
| 404 | `unknown_deck`、`not_found` | 牌組／卡片／媒體不存在 | 先建牌組或確認 id |
| 409 | `deck_exists` | 牌組 id 已存在 | 直接用既有牌組 |
| 409 | `batch_conflict` | 同 `batch_id` 已送過不同內容 | 新內容用新 `batch_id` |
| 409 | `content_conflict` | 既有 `external_key` 內容不同，附 `external_keys`；整批沒寫 | 確認是要改既有卡（`PATCH`）還是 key 取錯 |
| 409 | `id_collision`、`concurrent_write` | 導出 ID 碰撞；或同時有另一個請求建同一批卡 | `concurrent_write` 重跑同一批即可；`id_collision` 回報 |
| 409 | `version_conflict` | 狀態或卡面在你讀取後被改過，附目前 `state`／`card` | 重新讀再決定 |
| 411 | `length_required` | 上傳沒帶 Content-Length | 帶上 |
| 413 | `too_many_cards`、`payload_too_large`、`quota_exceeded` | 一批超過 500 張、本文超過 2 MB、單檔超過 20 MB，或儲存配額用完 | 切批；壓縮媒體；配額用完找管理者 |
| 415 | `unsupported_media_type` | Content-Type 不是 audio／video／image | 修 Content-Type |
| 429 | `rate_limited` | 超過每分鐘 300 次或每日 2,000 檔 | 等 `Retry-After` 秒數 |
| 500 | `internal` | server 錯誤 | 原文回報 |
