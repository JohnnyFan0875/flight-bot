# flight-bot

定時查詢 Google Flights 票價，找出便宜機票後透過 Discord 通知。完整設計見 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 運作方式

每天台灣時間 03:00 由 GitHub Actions 執行：

1. **掃描**：逐日查詢 Google Flights，60 天內每天查、61–120 天每 3 天查、更遠的每 10 天查（目前每天約 575 次，約 30 分鐘）
2. **配對**：單程去回價格組成 5 到 7 天的組合，每個目的地、每個出發月份取最便宜的一組
3. **明細**：對候選組合補查今天還沒查過的航段，取得航班號碼、時間和 Google 價格等級
4. **通知**：新低、低於中位數 15%、Google 評為偏低時，發送到 Discord

每週一 09:00 另外發送熱力圖、趨勢圖和摘要。

## 設定

編輯 [config.yaml](config.yaml) 即可新增目的地或調整規則，用 GitHub 網頁或手機編輯就行。

## 本機開發

```sh
uv sync
cp .env.example .env               # 填入 DISCORD_WEBHOOK_URL 等設定

uv run flight-bot run --mode daily --plan-only    # 只印出今天要查什麼
uv run flight-bot run --mode daily --dry-run      # 實際查詢，但只印出 Discord 訊息、不上傳
uv run flight-bot run --mode daily                # 正式執行
uv run flight-bot run --mode weekly --dry-run     # 每週摘要，圖片存到 .local-storage/reports/

uv run flight-bot search TPE NRT 2026-12-10                  # 查一個日期的航班
uv run flight-bot calendar TPE NRT 2026-12-01 2027-03-01     # 用日期 API 查一段日期
uv run flight-bot test-webhook                               # 測試 Discord webhook

uv run pytest
```

`.env` 設定 `STORAGE_DIR` 時，state 和原始資料改存在本機資料夾，不會連到 R2。

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

`config.yaml` 的 `scan.strategy: calendar` 會改用日期價格 API（一次取得 180 天的價格），但這個 API 目前需要 Google 頁面 JavaScript 產生的 header，純 HTTP 請求會被擋，詳見 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §5.6。
