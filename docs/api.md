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

牌組是一棵樹：每個牌組有 `parent_id`（null = 最上層），最多 5 層（最上層算第 1 層）。名字只存自己這一層，**不能含 `::`**（含就 400 `invalid_field`）；要「English › Grace」就建兩個牌組，Grace 的 `parent_id` 指向 English。同一個上層底下名字不可重複（409 `deck_name_taken`）。母牌組自己也可以有卡。牌組只由 agent 透過 API／CLI 建立，app 不提供建牌組。

| 端點 | 本文 | 回應 |
|---|---|---|
| `GET /v1/decks` | — | `{"decks": [Deck]}`，不含垃圾桶裡的牌組 |
| `POST /v1/decks` | `{"name", "id"?, "parent_id"?, "new_per_day"?, "sort_order"?}` | 201 `Deck`；id 已存在 409 `deck_exists` |
| `PATCH /v1/decks/{id}` | `name`、`parent_id`（null = 移到最上層）、`new_per_day`、`sort_order` 任選；`new_per_day` 可加 `"apply_to_subdecks": true` | 200 `Deck` |
| `POST /v1/decks/reorder` | `{"parent_id", "ids": [...]}` | 把同一個上層底下的兄弟依 `ids` 順序重設 `sort_order` 0..n；`ids` 必須正好是那一層的全部牌組，否則 400 |
| `DELETE /v1/decks/{id}[?purge=1]` | — | 牌組、所有子孫牌組與它們的卡一起進垃圾桶；`?purge=1` 直接硬刪。見「垃圾桶」 |
| `POST /v1/decks/{id}/restore` | — | 從垃圾桶復原整棵（牌組、子孫牌組、卡片）。原上層已不在就回到最上層並回 `restored_to_root: true`；名字和現有兄弟衝突就在後面加「 (2)」 |

`Deck = {"id", "name", "parent_id", "new_per_day", "card_count", "sort_order"}`。

- `id` 要符合 `[a-z0-9][a-z0-9_-]{0,63}`，不給由 server 產生。
- `card_count` 只算這個牌組自己的卡，不含子孫、不含垃圾桶。
- `sort_order` 只在同一個上層的兄弟之間排序；新建預設 = 兄弟最大值 + 1。
- 移動（改 `parent_id`）時目標不能是自己或自己的子孫、不能超過 5 層，否則 400 `invalid_parent`。子孫跟著一起移。
- `new_per_day` 0–9999，預設 20，**只管這個牌組自己的卡，不蓋子孫**。複習母牌組 = 子樹裡每個牌組各自依自己的上限取新卡再合起來；app 今日頁母牌組那列顯示子孫加總。要一次改整棵就帶 `apply_to_subdecks: true`，server 寫進每個子孫。改 `new_per_day` 以 server 當下時間參與「後寫者勝」，手機之後送來較舊的設定不會蓋掉。
- 改名、移動、排序走 change_log，手機下次 sync 拿到。

## 媒體

1. 本機算每個檔案的 SHA-256（小寫 hex）。
2. `POST /v1/media/check`，本文 `{"sha256": [...]}`（最多 200 個），回 `{"present": [...], "missing": [...]}`。
3. 對 `missing` 的每個檔 `PUT /v1/media/{sha256}`：本文是檔案原始 bytes，必帶 `Content-Length` 與 `Content-Type`（`audio/*`、`video/*`、`image/*`）。單檔 ≤ 20 MB。
   - 201 `{"sha256", "size", "mime", "status": "created"}`；這個帳號已有同一份時 200 `status: "exists"`。
   - server 邊收邊算 SHA-256，不符回 400 `hash_mismatch` 並丟棄。
4. manifest 只引用已上傳的 sha256；沒上傳就建卡回 400 `media_not_uploaded`，附 `missing`。

`GET /v1/media/{sha256}` 下載（支援 `Range`、`ETag`／`If-None-Match`）；`HEAD` 取 metadata。只有上傳過該檔的帳號拿得到，其他帳號一律 404。

配額：每人儲存 5 GiB（以每人上傳過的檔案大小總和計算），超過回 413 `quota_exceeded` 並附 `storage_used_bytes`、`storage_quota_bytes`。

回收：媒體不跟卡一起刪。server 每天 04:30（Asia/Taipei，UTC 20:30）先清掉垃圾桶裡過期的卡與牌組，再回收「這個帳號已沒有任何卡引用、且上傳超過 24 小時」的檔案並釋放配額（垃圾桶裡的卡仍算引用）；`DELETE`／`PATCH /v1/cards/{id}` 結束時也會對你的帳號順手跑一次。所以先上傳、24 小時內送 batch 不會被收走；回收後要再用就重新上傳（`media/check` 會列為 `missing`）。同一份檔案別的帳號也有時，只扣你的那份。上傳期間共用的檔案剛好被回收時回 503 `retry_upload`，重傳即可。

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
| `PATCH /v1/cards/{id}` | 改卡面：`term`、`definition`、`context`、`source`、`pos`、`template`、`lang`、`direction`、`front`、`back` 任選，可帶 `based_on_content_version`；成功 `content_version + 1`。不能改 `kind`、`external_key`。版本不符 409 `version_conflict` 附目前卡片 |
| `PATCH /v1/cards/{id}`（`{"deck_id": "…"}`） | 搬到另一個牌組：目標要存在且不在垃圾桶；目標已有同 `external_key` 回 409 `card_exists`。卡 id 不變（所以搬過的卡 id 不再等於用新 `deck_id` 導出的值）；原本的 manifest 之後重送到舊牌組會在舊牌組另建一張 |

### 刪卡：`DELETE /v1/cards/{id}[?purge=1]`

預設**進垃圾桶**，保留 7 天可復原；用在建錯、重複、要重做的卡（卡本身錯了）。學過但暫時不想看的卡用暫停：暫停會留下理由（`too_easy`、`too_hard`、`not_interested`…）給 agent 看，卡與進度都還在。

- 進垃圾桶 200：`{"id": "c_…", "deleted": true, "purge_at": "…Z"}`。垃圾桶裡的卡在 `GET /v1/cards`、bootstrap、sync、`card_count` 都看不到。
- `?purge=1`：直接硬刪，連同排程狀態、媒體引用、作答紀錄、該卡的裝置事件，無法復原。回 `{"id", "deleted": true, "media_released": ["<sha256>", …], "storage_used_bytes", "storage_quota_bytes"}`；`media_released` 是這次順手回收的媒體（這個帳號超過 24 小時、已沒有任何卡引用的檔案，可能包含別張卡先前留下的）。
- 沒有 `has_history` 檢查，也沒有 `force`（0.1.x 的行為已取消）。
- 找不到或不是自己的卡：404 `not_found`。
- 同 `batch_id`、同內容的 manifest 重送：卡已硬刪就重建（同一個 card id，manifest 的 `history`／`state` 重新寫入）；卡在垃圾桶裡就先硬刪那張（含進度與紀錄）再建新卡，回應 `replaced_trashed` 列出這些 id。要保留進度請用 `restore`，不要重送。cloze 刪一張 `:cN` 不影響同 note 的其他卡。
- 手機下次 `/v1/sync` 從 `removed_card_ids` 得知這張卡不見了。

### 復原：`POST /v1/cards/{id}/restore`

把單張卡從垃圾桶拿回來（進度、紀錄都在）。它的牌組也在垃圾桶時回 409 `deck_deleted`（附 `deck_id`），改復原那個牌組。

## 垃圾桶

刪卡與刪牌組預設都進垃圾桶，保留 `retention_days`（預設 7 天）後由 server 每天 04:30（Asia/Taipei）清掉，接著回收不再被引用的媒體。垃圾桶裡的卡仍算持有它們的媒體。

| 端點 | 說明 |
|---|---|
| `GET /v1/trash` | `{"retention_days", "items"}`，依 `deleted_at` 新到舊 |
| `DELETE /v1/trash` | 清空：全部硬刪，然後跑這個帳號的媒體回收；無法復原 |

`items` 兩種：

- `{"type": "deck", "id", "name", "parent_id", "deck_count", "card_count", "deleted_at", "purge_at"}`：被刪的那棵子樹的根（子孫牌組與卡不另列）。
- `{"type": "card", "id", "deck_id", "deck_name", "sort_key", "term", "deleted_at", "purge_at"}`：單獨刪掉的卡。

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
- `POST /v1/sync`：`{"device_id", "cursor", "events": [...]}`（≤ 1,000 事件），回 `accepted`、`duplicate`、`rejected`、`conflicts`、新 `cursor` 與之後的變動；`removed_card_ids`、`removed_deck_ids` 是 cursor 之後被刪掉或進垃圾桶的卡與牌組。牌組 JSON 帶 `parent_id`。

## 錯誤代碼

| HTTP | `error` | 意思 | 處理 |
|---|---|---|---|
| 400 | `invalid_json`、`invalid_manifest`、`invalid_field`、`invalid_blocks`、`invalid_cloze`、`invalid_history`、`invalid_state` | 格式不對，`detail` 會指出欄位與 external_key | 照 `detail` 修 manifest |
| 400 | `duplicate_external_key` | 同一批有重複 `external_key` | 改成唯一 |
| 400 | `media_not_uploaded` | 引用的 sha256 還沒上傳，附 `missing` | 先 `PUT /v1/media/{sha256}` |
| 400 | `hash_mismatch`、`length_mismatch`、`empty_media`、`invalid_sha256` | 上傳內容和路徑的 sha256 或 Content-Length 不符 | 重算 hash 重傳 |
| 401 | `unauthorized` | key 錯誤或已撤銷 | 到 app 設定頁產生新 key，重跑 `cubercard auth` |
| 403 | `session_required` | API key 不能打這個端點 | 改用 app |
| 400 | `invalid_parent` | 上層不存在、是自己或自己的子孫，或超過 5 層 | 換一個上層 |
| 404 | `unknown_deck`、`not_found` | 牌組／卡片／媒體不存在 | 先建牌組或確認 id |
| 409 | `deck_exists` | 牌組 id 已存在（`GET /v1/decks` 看不到就是在垃圾桶裡） | 直接用既有牌組；在垃圾桶就 `POST /v1/decks/{id}/restore` |
| 409 | `deck_name_taken` | 同一個上層底下已有同名牌組 | 改名，或用那個既有牌組 |
| 409 | `deck_deleted` | 要復原的卡，它的牌組在垃圾桶，附 `deck_id` | 復原那個牌組 |
| 409 | `card_exists` | 搬卡的目標牌組已有同 `external_key` 的卡 | 先決定留哪張 |
| 409 | `batch_conflict` | 同 `batch_id` 已送過不同內容 | 新內容用新 `batch_id` |
| 409 | `content_conflict` | 既有 `external_key` 內容不同，附 `external_keys`；整批沒寫 | 確認是要改既有卡（`PATCH`）還是 key 取錯 |
| 409 | `id_collision`、`concurrent_write` | 導出 ID 碰撞；或同時有另一個請求建同一批卡 | `concurrent_write` 重跑同一批即可；`id_collision` 回報 |
| 409 | `version_conflict` | 狀態或卡面在你讀取後被改過，附目前 `state`／`card` | 重新讀再決定 |
| 411 | `length_required` | 上傳沒帶 Content-Length | 帶上 |
| 413 | `too_many_cards`、`payload_too_large`、`quota_exceeded` | 一批超過 500 張、本文超過 2 MB、單檔超過 20 MB，或儲存配額用完 | 切批；壓縮媒體；配額用完找管理者 |
| 415 | `unsupported_media_type` | Content-Type 不是 audio／video／image | 修 Content-Type |
| 429 | `rate_limited` | 超過每分鐘 300 次或每日 2,000 檔 | 等 `Retry-After` 秒數 |
| 500 | `internal` | server 錯誤 | 原文回報 |
| 503 | `retry_upload` | 上傳期間共用的媒體剛好被回收 | 重傳同一個檔 |
