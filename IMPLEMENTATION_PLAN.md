# flight-bot 實作計畫

定時查詢 Google Flights 票價，依目的地分組規則（短程直飛、長程可轉機）找出便宜機票，透過 Discord webhook 通知，並將每次查詢結果備份到 Cloudflare R2 以供日後趨勢分析。

---

## 1. 已確定的決策

| 項目 | 決策 |
|---|---|
| 資料來源 | Google Flights 非官方介面：**逐日查詢**（`fast-flights` 產生 `tfs` 參數，自寫 parser 解析頁面內嵌的 JSON）。包在 `Provider` 介面後，日後可換 SerpApi |
| 查詢策略 | 逐日查詢分層輪替（近的每天查、遠的隔幾天查），再對候選組合補查明細。日期價格 API（`GetCalendarGraph`）保留為實驗選項，目前會被 Google 擋（見 §5.6） |
| 執行環境 | GitHub Actions cron，**公開 repo** |
| 機密 | Discord webhook、R2 金鑰放 GitHub Secrets；設定檔公開沒關係 |
| 設定檔 | repo 內 `config.yaml`，用 GitHub 網頁或手機編輯 |
| 出發地 | 預設 `[TPE]`（桃園）；松山（TSA）只加在有直飛的目的地（目前只有羽田）。可在分組或目的地層級覆寫 |
| 搜尋範圍 | 出發日在 30 到 210 天後；回程日最晚到 210 + 最長停留天數 |
| 短程 | 單程去回分開查再配對；只要直飛；停留 5 到 7 天 |
| 長程 | 來回查詢；最多轉機 1 次；目的地先留空 |
| 便宜判斷 | 不設絕對門檻，改用 Google 價格等級、同月份中位數、同月份新低 |
| 即時通知 | 文字 embed，一次執行合併成一則訊息 |
| 每週摘要 | 熱力圖加趨勢圖（PNG），再附前 3 名組合的文字 |
| 異常警告 | 失敗率過高或連續沒有成功時通知 |
| 儲存 | R2：原始資料存成 Parquet（只新增）；狀態存成 `state.sqlite` |

---

## 2. 技術選型

- Python 3.12，用 `uv` 管理相依套件
- `fast-flights` 3.x：產生 `tfs` 查詢參數和 Google Flights 連結（它內建的 parser 會漏掉「最佳航班」區塊、也沒有航班號碼和價格等級，所以自己解析）
- `primp`：模擬瀏覽器的 HTTP client（`fast-flights` 的相依套件）
- 參考專案：[punitarani/fli](https://github.com/punitarani/fli) 用同樣的方式查詢（GET `tfs` 網址、解析 `ds:1`），社群活躍；Google 改版時可以參考它的修正
- `selectolax`：從 HTML 取出內嵌的 JSON
- `pydantic`：驗證設定檔
- `pyyaml`：讀取設定檔
- `pyarrow`：寫入 Parquet（zstd 壓縮）
- `boto3`：透過 S3 API 存取 R2
- `requests`：發送 Discord webhook
- `matplotlib`：畫每週摘要圖（Actions 環境要安裝 `fonts-noto-cjk`）
- `duckdb`：選用，本機做趨勢分析
- `pytest`：測試

---

## 3. 專案結構

```
flight-bot/
├── config.yaml
├── pyproject.toml
├── src/flight_bot/
│   ├── main.py              # CLI 入口：run --mode daily|weekly、search、calendar、test-webhook
│   ├── config.py            # pydantic 模型，處理 defaults → 分組 → 目的地的覆寫
│   ├── airports.py          # 機場代碼 → 中文名稱
│   ├── planner.py           # 產生掃描任務（日期 API 分段、逐日分層輪替）
│   ├── providers/
│   │   ├── base.py          # Provider 介面、Flight 等資料類別
│   │   └── google.py        # 日期價格 API、逐日查詢與解析
│   ├── runner.py            # 並行執行、重試、間隔、失敗統計
│   ├── daily.py             # 每日流程：掃描 → 配對 → 候選 → 明細 → 判斷 → 通知
│   ├── rules.py             # 轉機數、總飛行時間、轉機等待時間的過濾
│   ├── pairing.py           # 單程去回配對
│   ├── pricing.py           # 中位數、新低、價格等級判斷
│   ├── storage/
│   │   ├── backend.py       # R2 或本機資料夾（開發用）
│   │   ├── raw.py           # 寫入 raw parquet
│   │   └── state.py         # state.sqlite 存取
│   ├── notify/
│   │   ├── discord.py       # 發送 webhook（含附件）
│   │   ├── alerts.py        # 即時降價訊息
│   │   ├── weekly.py        # 每週摘要
│   │   └── charts.py        # 熱力圖、趨勢圖
│   └── links.py             # 產生 Google Flights 連結
├── tests/
└── .github/workflows/
    ├── daily.yml
    ├── weekly.yml
    └── keepalive.yml
```

---

## 4. 設定檔 `config.yaml`

覆寫優先順序：**目的地 > 分組 > defaults**。

```yaml
defaults:
  origins: [TPE]             # TPE 桃園；松山（TSA）只在有直飛的目的地個別加上
  search_from_days: 30
  search_to_days: 210
  depart_weekdays: []        # 留空 = 每天都可以出發（0 = 週一 … 6 = 週日）
  allow_mixed_origins: true  # 單程配對時，允許桃園去、松山回

scan:
  strategy: per_date         # per_date：逐日查詢；calendar：日期價格 API（實驗性，目前會被 Google 擋）
  per_date_tiers:            # 逐日查詢的分層頻率：距今 N 天內，每 every 天查一次
    - {until_days: 60, every: 1}
    - {until_days: 120, every: 3}
    - {until_days: 217, every: 10}
  detail_budget: 40          # 每次執行最多幾次明細查詢
  # 以下只在 strategy: calendar 時使用
  calendar_max_days: 180     # 日期 API 單次最多涵蓋的天數（實測 181 天可用、211 天回傳空的）
  fallback_to_per_date: true # 日期 API 失敗時，該航線改用逐日查詢
  verify_samples: 4          # 每次另外隨機抽幾個日期，比對日期 API 和明細查詢的價格

runner:
  concurrency: 2
  delay_seconds: [2, 5]      # 每次查詢後隨機等待的秒數範圍
  retries: 2
  abort_after_consecutive_failures: 20

alerts:
  max_price: null            # 先不設；之後可改成 {default: N, by_month: {12: N, ...}}
  use_google_price_level: true
  below_median_pct: 15
  notify_new_low: true
  warmup_days: 7
  renotify_drop: 300         # 同一組日期要再便宜 TWD 300 以上才重新通知

weekly_summary:
  enabled: true
  weekday: 0                 # 週一
  hour: 9                    # 台灣時間

health:
  max_failure_rate: 0.30
  max_days_without_success: 3

groups:
  short_haul:
    mode: one_way
    max_stops: 0
    trip_length_days: [5, 7]
    destinations:
      - code: CTS            # 北海道・札幌（新千歲）
      - code: SDJ            # 宮城・仙台
      - code: NRT            # 東京・成田
      - code: HND            # 東京・羽田
        origins: [TPE, TSA]  # 松山只有飛羽田有直飛

  long_haul:
    mode: round_trip
    origins: [TPE]
    max_stops: 1
    max_total_duration_h: 24
    max_layover_h: 6
    min_layover_min: 75
    trip_length_days: [10, 14]
    destinations: []
```

在目的地層級覆寫的範例：

```yaml
      - code: CTS            # 北海道・札幌（新千歲）
        origins: [TPE]
        trip_length_days: [4, 6]
        enabled: false
```

驗證規則：
- 機場代碼必須是 3 個大寫字母
- `trip_length_days` 和 `search_*_days` 都必須是遞增的
- `per_date_tiers` 的 `until_days` 必須遞增，最後一層要涵蓋回程的最晚日期
- 未知欄位一律報錯，避免打錯字卻沒發現

---

## 5. 查詢規劃（planner）

### 5.1 日期範圍

- 去程：今天 + `search_from_days` 到今天 + `search_to_days`
- 回程（單程模式的回程方向）：今天 + `search_from_days` + 最短停留，到今天 + `search_to_days` + 最長停留。只用去程範圍的話，最晚幾天出發的組合會配不到回程
- `depart_weekdays` 只限制去程日

### 5.2 掃描：逐日查詢（預設）

設 `n` 為日期距今天的天數，依 `per_date_tiers` 分層：

| 範圍 | 頻率 | 去程方向每天的查詢數 |
|---|---|---|
| 30–60 天 | 每天 | 31 |
| 61–120 天 | 每 3 天 | 20 |
| 121–210 天（回程到 217 天） | 每 10 天 | 約 9 |

某一層的日期只在 `日期.toordinal() % every == 今天.toordinal() % every` 時查，這樣每天的查詢量固定（實測連續 10 天都是 575 次）。來回模式只取停留天數的兩端值。

逐日查詢本身就拿得到航班明細和價格等級，所以掃描結果會直接存成明細，當天不會重複查。遠期日期是輪流查的，配對時從 state 讀取各日期最近一次的價格（見 §7.2）。

### 5.3 明細查詢（候選組合）

掃描結果配對後（見 §7），挑選候選組合，補查今天沒查過的航段，拿到航班號碼、時間、轉機資訊和 Google 價格等級：

1. 依掃描價格已經觸發 `new_low`、`below_median`、`max_price` 的組合，依低於中位數的幅度排序
2. 每個 (目的地, 出發月份) 的最便宜組合，用來檢查 `google_low`
3. 依序查詢，直到用完 `detail_budget`。單程組合最多查 2 次（去程和回程），來回組合查 1 次；當天已經查過的航段不重複查

### 5.4 預估查詢量

短程 4 個目的地，桃園出發，羽田另外加松山：10 條航線方向。

| 項目 | 每天查詢數 |
|---|---|
| 掃描：去程 5 條 × 60 | 300 |
| 掃描：回程 5 條 × 55 | 275 |
| 明細 | 最多 40（多數航段掃描時已查過，實際更少） |
| **合計** | **約 600** |

- 每次查詢約 1–3 秒，加上 2–5 秒的隨機等待，每個 worker 平均約 5.5 秒一次；並行 2 個約 **30 分鐘**
- 重試另外計算：失敗率 10% 時約多 10–20%
- 每加一個短程目的地（只從桃園出發）：約 +115 次
- 每加一個長程目的地（停留 10 到 14 天，取兩端值）：約 +120 次

如果之後查詢量變多，再加回「自動略過無航班航線」：連續幾次執行都查不到航班的航線，30 天後再確認一次。

### 5.5 日期價格 API（實驗選項，`strategy: calendar`）

`GetCalendarGraph` 一次請求就能拿到最多 180 天、每天的最低價，每條航線方向每天只要約 2 次請求。程式已經實作，但目前不能當主要方案（見 §5.6）。

- 單程模式：每個 (出發地, 目的地, 方向) 依日期範圍切成不超過 `calendar_max_days` 的幾段，平均分配長度
- 來回模式：每個 (出發地, 目的地, 停留天數) 一組請求，停留天數取 `trip_length_days` 範圍內的每個整數
- 轉機數用 Google 的編碼：`0` = 不限、`1` = 只要直飛、`2` = 最多轉 1 次，也就是 `max_stops + 1`
- 日期 API 不會套用總飛行時間和轉機等待時間的限制，這些在明細階段才過濾
- 每次另外隨機抽 `verify_samples` 個日期做明細查詢，記錄兩者的價格差距
- `fallback_to_per_date: true` 時，失敗的那一段日期改用逐日查詢：先送 1 個請求試探，失敗就整批改用逐日查詢；中途連續失敗 3 次也放棄剩下的；日期 API 的失敗不計入「連續失敗就提前結束」的次數

### 5.6 日期價格 API 的實測紀錄

2026-10-24：

- 18:06 到 18:11 之間約 15 次請求都成功，價格和逐日查詢一致（例如 TPE→NRT 12/10 都是 TWD 4,868）
- 18:23 之後一律回傳 HTTP 200 但沒有資料（錯誤代碼 13）。換語言、幣別、日期範圍、先開頁面拿 cookie、改用 `GetCalendarPicker`、改用一般的 `requests`，結果都一樣；同一時間逐日查詢仍然正常
- [fli](https://github.com/punitarani/fli) 的 README 寫明：`GetCalendarGraph` 和 `GetShoppingResults` 需要 `x-goog-batchexecute-bgr` header，這個值只有頁面的 JavaScript 能產生，純 HTTP 請求會拿到沒有資料的 200。fli 因此也放棄日期 API，改成逐日查詢
- 結論：一開始能成功，比較可能是 Google 暫時放行沒有 header 的請求，不是 IP 被限流。這個 header 屬於 Google 的反機器人機制，不嘗試自行產生。唯一正當的做法是用瀏覽器（Playwright）讓頁面自己發出請求，但太重也容易壞，第一版不做

---

## 6. 執行（runner）

- 用 `ThreadPoolExecutor(max_workers=concurrency)` 並行查詢
- 每次查詢後隨機等待 `delay_seconds` 範圍內的秒數
- 失敗重試 `retries` 次，採指數退避
- 連續失敗 `abort_after_consecutive_failures` 次就提前結束這次執行，避免持續觸發封鎖
- 依請求類型（掃描、明細、驗證）統計成功、失敗、無結果的次數，供健康檢查使用

### Provider 介面

```python
class Provider(Protocol):
    def calendar(self, origin, dest, start, end, max_stops, trip_length=None) -> list[CalendarQuote]: ...
    def search(self, origin, dest, depart, ret, max_stops) -> SearchResult: ...

@dataclass
class CalendarQuote:
    depart_date: date
    return_date: date | None
    price: int | None        # TWD；None = 沒有航班

@dataclass
class Segment:
    airline: str
    flight_no: str
    from_airport: str
    to_airport: str
    depart_at: datetime      # 當地時間
    arrive_at: datetime
    duration_min: int

@dataclass
class Flight:
    airlines: list[str]
    segments: list[Segment]
    stops: int
    duration_min: int
    layovers_min: list[int]
    price: int               # TWD

@dataclass
class SearchResult:
    flights: list[Flight]
    price_level: Literal["low", "typical", "high"] | None
    typical_range: tuple[int, int] | None
```

Google 回傳的價格等級代碼不容易直接對應，所以用它附的典型價格區間判斷：最低價低於區間下限是 `low`，高於上限是 `high`，其餘是 `typical`。

---

## 7. 過濾與配對

### 7.1 規則過濾（rules）

明細查詢時，依分組或目的地的設定排除不符合的航班：
- 轉機數超過 `max_stops`
- 總飛行時間超過 `max_total_duration_h`
- 有任一段轉機等待超過 `max_layover_h`，或短於 `min_layover_min`

### 7.2 單程配對（pairing）

1. 每個 (目的地, 方向, 日期) 只取最低價。`allow_mixed_origins` 為 true 時，去程和回程的出發地可以不同
2. 對每個去程日 D，搭配回程日 R，條件是 `R − D` 在 `trip_length_days` 的範圍內（含兩端）
3. 組合價格 = 去程價 + 回程價
4. 每個 (目的地, 出發月份) 只保留最便宜的一組，作為通知的主要內容；並記錄同月份還有幾組在最低價 +10% 以內

配對時從 state 讀取各日期**最近一次**查到的最低價。超過「最大輪替間隔 + 1」天（目前是 11 天）沒更新的價格視為過期，不納入配對。

---

## 8. 便宜判斷（pricing）

基準單位是 (目的地, 出發月份)，模式分單程和來回。

| 觸發條件 | 定義 |
|---|---|
| `new_low` | 組合價格低於這個基準單位有記錄以來的最低價 |
| `below_median` | 組合價格比近 30 天所有觀察值的中位數低 `below_median_pct`% |
| `google_low` | 單程模式：去程和回程的價格等級都是 `low`；來回模式：結果的價格等級是 `low`。只有做過明細查詢的組合才能判斷 |
| `max_price` | 有設門檻時，組合價格低於該月份的門檻 |

- 前 `warmup_days` 天只收集資料，`new_low` 和 `below_median` 不觸發
- 歷史記錄（`combo_history`）一律用掃描價格，確保比較基準一致；當天補查的明細價格如果不同，判斷和通知改用明細價格
- 去重複：以 (目的地, 去程日, 回程日, 出發地組合) 為 key。上次通知後，價格要再降 `renotify_drop` 以上才重新通知

---

## 9. 通知

### 9.1 即時降價（每次每日執行後）

- 這次執行的所有觸發結果合併成一則 webhook 訊息，每個 embed 一組結果
- 一則訊息最多 10 個 embed，超過就分批發送
- embed 左側顏色依觸發條件決定，優先順序為：🟢 `new_low` > 🔵 `below_median` > 🟡 `google_low`
- 沒有明細的組合（明細預算用完或查詢失敗）照樣通知，只顯示價格和日期

訊息範例：

```
✈️ 東京・成田 NRT ｜ 3 月新低

💰 TWD 7,850（去 3,900 + 回 3,950）
📉 比 3 月中位數低 22% ・ Google：偏低

去程  3/14（六）TPE 07:10 → NRT 11:30
      台灣虎航 IT200 ・ 直飛
回程  3/19（四）NRT 12:40 → TPE 15:20
      台灣虎航 IT201 ・ 直飛
停留 5 天

另有 3 組 TWD 8,600 以下
🔗 查看去程 ・ 查看回程
```

- 連結使用 `fast-flights` 產生的 `tfs` 參數網址（`hl=zh-TW&curr=TWD`）
- 目的地中文名稱放在 `airports.py` 的對照表裡，例如 `CTS → 北海道・札幌（新千歲）`

### 9.2 每週摘要（週一 09:00 台灣時間）

一則訊息內含：

1. **熱力圖 PNG**：列是目的地，欄是出發月份，格子內是最低組合價格，顏色依「該價格在這個目的地所有月份中的相對位置」決定
2. **趨勢圖 PNG**：每個目的地近 8 週、每週的最低價折線
3. **文字 embed**：
   - 本週前 3 名組合（附日期、出發機場、連結）
   - 各目的地、各出發月份的最低價，以及和上週相比的漲跌（▲▼）
   - 健康狀態：本週查詢次數、失敗率（使用日期 API 時另外顯示它和明細價格的平均差距）

技術細節：
- 圖片以 multipart 附件上傳，embed 裡用 `attachment://heatmap.png` 引用
- 字型使用 Noto Sans CJK（本機開發時自動退回 PingFang TC）
- 圖片同時備份到 R2 的 `reports/`

### 9.3 異常警告

符合任一條件就發送：
- 單次執行的失敗率超過 `max_failure_rate`
- 使用日期 API 時，日期 API 全部失敗、改用逐日查詢
- 連續 `max_days_without_success` 天都沒有任何成功的查詢
- 程式發生未預期的例外（`main.py` 最外層攔截，訊息附 traceback 摘要）

```
⚠️ flight-bot 異常
本次查詢失敗率 64%（可能被 Google 擋了，或介面改版）
已連續 2 次執行異常
```

---

## 10. 儲存

### 10.1 R2 結構

```
r2://flight-bot/
├── raw/kind=calendar/date=YYYY-MM-DD/part-<run_id>.parquet   # 只新增
├── raw/kind=flights/date=YYYY-MM-DD/part-<run_id>.parquet    # 只新增
├── state/state.sqlite
├── state/state.sqlite.bak
└── reports/YYYY-Www-{heatmap,trend}.png
```

本機開發時設 `STORAGE_DIR=./.local-storage`，改存在本機資料夾，結構相同。

### 10.2 原始資料 Parquet schema

`kind=calendar`（每個日期一列）：

| 欄位 | 型別 |
|---|---|
| run_id | string |
| queried_at | timestamp (UTC) |
| mode | string（`one_way` / `round_trip`） |
| origin, dest | string |
| depart_date, return_date | date（單程的 return_date 為 null） |
| max_stops | int |
| price | int（null = 沒有航班） |
| currency | string |

`kind=flights`（每個航班一列）：

| 欄位 | 型別 |
|---|---|
| run_id | string |
| queried_at | timestamp (UTC) |
| purpose | string（`detail` / `verify` / `scan`） |
| mode, origin, dest | string |
| depart_date, return_date | date |
| airlines | list<string> |
| flight_nos | list<string> |
| depart_at, arrive_at | string（當地時間 ISO 格式） |
| stops, duration_min | int |
| layovers_min | list<int> |
| price | int |
| currency | string |
| price_level | string |
| status | string（`ok` / `empty` / `error`，查詢失敗時也寫一列，方便事後分析失敗原因） |
| error | string |

### 10.3 state.sqlite 資料表

- `quotes(mode, origin, dest, depart_date, return_date, price, source, queried_at)`：配對用，存每個日期最近一次查到的最低價
- `details(mode, origin, dest, depart_date, return_date, price, price_level, flight_json, queried_at)`：明細查詢結果，同一天內重複使用
- `combo_history(run_date, dest, mode, depart_date, return_date, origins, price)`：計算中位數和歷史新低。每次執行、每個出發日只存最便宜的一組（4 個目的地每天約 720 列），超過 400 天的自動刪除
- `notified(key, price, notified_at)`：去重複
- `verify_log(run_id, mode, origin, dest, depart_date, return_date, calendar_price, detail_price)`：日期 API 準確度
- `runs(run_id, started_at, finished_at, mode, ok, failed, empty, aborted, calendar_failed, requests_json)`：健康檢查、每週摘要統計

### 10.4 執行流程

1. 從 R2 下載 `state.sqlite`；沒有就建立新的
2. 執行掃描、配對、明細、判斷、通知
3. 寫入 raw parquet
4. 上傳前先把 R2 上現有的 `state.sqlite` 複製成 `state.sqlite.bak`，再上傳新的

---

## 11. GitHub Actions

### 11.1 `daily.yml`

- cron：`0 19 * * *`（UTC，等於台灣時間 03:00）
- 支援 `workflow_dispatch` 手動觸發，可帶 `dry_run` 參數
- `concurrency: { group: flight-bot, cancel-in-progress: false }`：和 weekly 共用同一組，確保 state 不會被同時讀寫
- `timeout-minutes: 90`（預估約 30 分鐘，留給重試和之後新增目的地的空間）
- 步驟：checkout → 安裝 uv → `uv sync` → `uv run flight-bot run --mode daily`

### 11.2 `weekly.yml`

- cron：`0 1 * * 1`（UTC 週一 01:00，等於台灣時間週一 09:00）
- 額外步驟：`sudo apt-get install -y fonts-noto-cjk`
- 執行 `uv run flight-bot run --mode weekly`

### 11.3 `keepalive.yml`

- 每月 1 日執行，更新並 commit `.github/heartbeat`
- 目的：避免公開 repo 連續 60 天沒有活動時，排程 workflow 被自動停用

### 11.4 Secrets

| 名稱 | 用途 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord 通知 |
| `R2_ACCOUNT_ID` | R2 endpoint：`https://<id>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | R2 API token |
| `R2_SECRET_ACCESS_KEY` | R2 API token |
| `R2_BUCKET` | bucket 名稱（也可以放 Variables） |

本機開發時用 `.env`，並把 `.env` 加進 `.gitignore`。

---

## 12. 實作階段

| 階段 | 內容 | 完成標準 |
|---|---|---|
| M1 | 專案骨架、`config.py`、覆寫邏輯 | 單元測試涵蓋覆寫規則和驗證錯誤 |
| M2 | Google provider：逐日查詢（和實驗性的日期價格 API）；`flight-bot search`、`flight-bot calendar` 指令 | 本機能印出航班清單 |
| M3 | `planner`（逐日分層輪替、日期 API 分段）、`runner` | `--plan-only` 印出今天的查詢清單和數量 |
| M4 | 儲存：本機資料夾和 R2、raw parquet、state.sqlite | 本機跑一次後，R2 上有對應檔案 |
| M5 | `rules`、`pairing`、`pricing`、明細階段、即時通知 | `--dry-run` 印出 Discord payload；實際發送到測試頻道 |
| M6 | GitHub Actions：daily 和 keepalive | 手動觸發成功；確認 Actions 的 IP 沒有被擋 |
| M7 | 每週摘要（圖表和文字） | 測試頻道收到正確的圖片和中文 |
| M8 | 健康檢查、異常警告 | 模擬失敗時會收到警告 |

**M6 是關鍵檢查點**：如果 Actions 的 IP 經常被 Google 擋，就評估兩個備案：改在 Mac 本機用 launchd 執行，或換成 SerpApi provider。

### 測試重點

- `planner`：逐日分層邊界（第 60 天和第 61 天）、輪替分配、回程日期範圍、`depart_weekdays`、日期 API 分段（剛好 180 天、181 天）
- `providers`：用存下來的真實回應當 fixture，測試日期 API 和逐日查詢的解析
- `pairing`：停留天數的邊界、混用出發地、過期價格的排除
- `pricing`：warmup 期間、中位數計算、`renotify_drop` 去重複
- `notify`：embed 的長度和數量限制（每個 embed 6000 字元、每則訊息 10 個 embed）
- CI 測試不打真的 Google Flights

---

## 13. 風險與對策

| 風險 | 對策 |
|---|---|
| 逐日查詢因 Google 改版失效 | 異常警告會通知；參考 fli 的修正更新 parser；Provider 介面可以換成 SerpApi |
| GitHub Actions 的 IP 被擋 | 分層輪替把查詢量控制在每天約 600 次；控制並行數和間隔、提前結束機制；備案是本機 launchd |
| state.sqlite 損毀或被覆蓋 | concurrency group 確保同時只跑一個；上傳前保留 `.bak` |
| 排程 workflow 被自動停用 | keepalive workflow |
| 季節造成價格差異大 | 以出發月份為基準計算中位數和新低，不使用全年單一門檻 |

---

## 14. 之後可以做的事（不在第一版範圍）

- 加入長程目的地
- 依月份設定 `max_price`（等累積 2 到 3 週資料後再決定數值）
- 用 Playwright 開瀏覽器取得日期價格 API 的資料，把掃描降到每天幾十次（要評估穩定性）
- 用 Travelpayouts 的快取價格做初步掃描，只對候選日期查 Google（要先測台日航線的涵蓋率）
- 以 DuckDB 直接查 R2 做趨勢分析，例如比較各航空公司的特價頻率、找出提前幾天訂最便宜
- 用 Google Sheet 或 Discord Bot 指令管理目的地清單
- 查詢量變多時，加回「自動略過無航班航線」

---

## 15. 開始實作前要準備的東西

- [ ] Discord webhook URL（建議先建一個測試頻道）
- [x] Cloudflare R2 bucket `flight-bot`（已用 wrangler 建立）
- [ ] R2 API token（Object Read & Write 權限，只限 `flight-bot` bucket）：wrangler 無法建立，要到 Cloudflare 後台 R2 → Manage API tokens 建立
- [ ] GitHub 公開 repo，並設定好上述 Secrets
