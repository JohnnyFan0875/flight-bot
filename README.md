# flight-bot

定時查詢 Google Flights 票價，找出便宜機票，產生每日網頁報告，並在 Discord 發送總結。

## 運作方式

每天台灣時間 03:00 由 GitHub Actions 執行：

1. **掃描**：每條航線 `query_start`–`query_end` 範圍內每天查詢 Google Flights 單程航班（目前每天約 1,300 次，約 45 分鐘）。同一組機場、同一天只查一次，多條航線共用
2. **收集**：每條航線、每個日期，出發時段內符合規則的航班全部保留，不預先組合去回程
3. **判斷**：每個單程航班和同一條航線、同一組機場、同出發月份的歷史價格比較，新低、低於中位數 15%、或低於 Google 典型價格區間就計入 Discord 總結
4. **報告**：網頁報告（GitHub Pages）左邊去程、右邊回程，可以用機場、天數、直飛／轉機、來回總額上限、出發月份、漲跌狀態篩選。點選一班航班（或下方折線圖的某個月份最低價），另一邊只列出依航線 `trip_days` 配得到的航班，並列出來回總額。Discord 只發一則簡短總結和報告連結

每週一 09:00 另外發送熱力圖、趨勢圖和摘要。

## 設定

編輯 [config.yaml](config.yaml) 即可新增航線或調整規則，用 GitHub 網頁或手機編輯就行。

`destinations` 是一條條航線，每條的設定都寫在自己底下（第一條 `倫敦範例` 是 `enabled: false` 的說明範例）：

```yaml
- name: 東京                       # 顯示名稱，也是歷史價格的單位（改名會重新累積）
  enabled: true                    # false = 暫停查詢
  origins: [TPE]                   # 出發地，可多個
  dests: [NRT, HND]                # 目的地，可多個
  query_start: 2026-10-01          # 去程出發日範圍，範圍內每天都查（今天以前略過）
  query_end: 2027-02-28            # 回程查到 query_end + trip_days 最長的天數
  trip_days: [5, 6, 7]             # 網頁上配對去回程用的天數
  max_stops: 0                     # 最多轉機次數
  max_total_duration_h: null       # 單程總時間上限（小時），null = 不限
  max_layover_h: null              # 每次轉機最長等待（小時）
  min_layover_min: null            # 每次轉機最短等待（分鐘）
```

價格一律以單程比較；`alerts.max_price` 也是單程價格上限。

## 本機開發

```sh
uv sync
cp .env.example .env               # 填入 DISCORD_WEBHOOK_URL 等設定

uv run flight-bot run --mode daily --plan-only    # 只印出今天要查什麼
uv run flight-bot run --mode daily --dry-run      # 實際查詢，但只印出 Discord 訊息、不上傳
uv run flight-bot run --mode daily                # 正式執行
uv run flight-bot run --mode weekly --dry-run     # 每週摘要，圖片存到 .local-storage/reports/

uv run flight-bot search TPE NRT 2026-12-10                  # 查一個日期的航班
uv run flight-bot calendar TPE NRT 2026-12-01 2027-03-01     # 用日期 API 查一段日期（實驗性，見下方說明）
uv run flight-bot test-webhook                               # 測試 Discord webhook

uv run pytest
```

`.env` 設定 `STORAGE_DIR` 時，state 和原始資料改存在本機資料夾，不會連到 R2。

## 每日報告網頁

報告網址設定在 `config.yaml` 的 `report.url`。第一次使用前，要在 repo 的 **Settings → Pages → Build and deployment → Source** 選 **GitHub Actions**。

- 每天的報告存一份在 R2 的 `reports/daily/`，網站保留最近 `report.keep_days` 天，可以從標題旁的日期選單切換
- 本機執行時，報告會產生在 `site/index.html`，直接用瀏覽器開啟即可
- 報告網頁是公開的（內容只有票價資訊）

## GitHub Secrets

| 名稱 | 用途 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord 通知 |
| `R2_ACCOUNT_ID` | Cloudflare 帳號 ID |
| `R2_ACCESS_KEY_ID` | R2 API token |
| `R2_SECRET_ACCESS_KEY` | R2 API token |
| `R2_BUCKET` | bucket 名稱（`flight-bot`） |

R2 API token 要到 Cloudflare 後台 **R2 → Manage API tokens** 建立，權限選 Object Read & Write，並只限 `flight-bot` bucket。

## 注意

逐日查詢使用 Google Flights 的非官方介面，Google 改版時可能失效，失敗率過高時 bot 會在 Discord 發送異常警告。[fli](https://github.com/punitarani/fli) 用同樣的方式查詢，Google 改版時可以參考它的修正。

`flight-bot calendar` 指令使用日期價格 API（`GetCalendarGraph`，一次取得 180 天的價格），但目前不能用：這個 API 需要 `x-goog-batchexecute-bgr` header，只有 Google 頁面上的 JavaScript 能產生，純 HTTP 請求會拿到沒有資料的回應（2026-10-24 實測，[fli](https://github.com/punitarani/fli) 的 README 也有說明）。這個 API 只有每天的最低價、沒有航班明細，所以每日流程只用逐日查詢。
