# 台股 ETF 本機查詢資料庫

從兆豐證券 ETFMaster（emega.com.tw）等來源抓取台股 ETF 清單與持倉資料，
存入本機 SQLite，提供 CLI 查詢與 xlsx 匯出。

## Mac Mini 快速安裝

```bash
# 1. 取得程式碼（擇一）
git clone https://github.com/akitata928/financial-services.git
cd financial-services
git checkout claude/taiwan-etf-spreadsheet-nl3r64

# 2. 一鍵安裝（建虛擬環境、裝套件、初始化 DB、抓 ETF 清單）
bash etf_db/setup_mac.sh
```

完成後資料庫位於 `~/Desktop/etf_master.db`（可用環境變數 `ETF_DB_PATH` 改位置）。

## 日常使用

```bash
source etf_db/.venv/bin/activate          # 每次開新 Terminal 先啟用

python3 etf_db/etl.py --holdings-all      # 批次抓 559 支股票的 ETF 持倉
python3 etf_db/query.py stats             # 資料庫統計
python3 etf_db/query.py stock 2330        # 台積電被哪些 ETF 持有
python3 etf_db/query.py compare 0050 0056 00878
python3 etf_db/query.py holdings 0050     # 0050 前 30 大持股
python3 etf_db/query.py search 高股息
python3 etf_db/query.py export --etf 0050 --fmt xlsx
```

## 自動每日更新（launchd）

把下面存成 `~/Library/LaunchAgents/com.user.etf-update.plist`，
每天 18:30（收盤後）自動更新：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.etf-update</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd /path/to/financial-services && source etf_db/.venv/bin/activate && python3 etf_db/etl.py --etf && python3 etf_db/etl.py --holdings-all</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/tmp/etf-update.log</string>
  <key>StandardErrorPath</key><string>/tmp/etf-update.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.etf-update.plist
```

記得把 `/path/to/financial-services` 改成實際路徑。

## 若遇到 403（emega 擋爬蟲）

emega 的 API 可能需要瀏覽器 session cookie：

1. 用 Chrome 開 https://www.emega.com.tw/etfmaster/index.do
2. 按 F12 → Network 分頁 → 重新整理 → 點任一個 `api/` 請求
3. 複製 Request Headers 裡的整串 `Cookie:` 值
4. 存進 `~/.etf_session_cookie`：
   ```bash
   echo '貼上cookie字串' > ~/.etf_session_cookie
   ```
5. 重新執行 `etl.py`，會自動讀取該 cookie

或改用 repo 根目錄的 `emega_selenium_scraper.py`（Selenium 自動開瀏覽器抓取）。

## 檔案結構

```
etf_db/
├── schema.sql     8 張資料表（ETF基本資料/行情/持倉/反查/歷史權重/NAV/知識塊）
├── etl.py         抓取管線：--discover / --init / --holdings-all / --etf-weight-all
├── query.py       查詢 CLI：etf / compare / stock / holdings / search / stats / export
├── setup_mac.sh   Mac 一鍵安裝
└── .venv/         虛擬環境（安裝後產生，不入版控）
```

## 資料表

| 資料表 | 內容 |
|--------|------|
| `etf_profile` | 343 檔 ETF 基本資料（代號、名稱、發行商、分類、追蹤指數、費用率…） |
| `etf_market_snapshot` | 每日行情快照（收盤、NAV、折溢價、量） |
| `stock_master` | 559 支個股主檔 |
| `stock_etf_holdings` | 個股 → 哪些 ETF 持有（反查表） |
| `etf_holdings` | ETF → 持倉成分股明細 |
| `etf_stock_weight_history` | 持股權重歷史（時間序列） |
| `etf_nav_history` | NAV 歷史 |
| `knowledge_chunks` | RAG 用文字知識塊（ETF 說明、策略摘要） |

> 資料僅供研究參考，非投資建議。批次抓取已內建限速（預設 0.3 秒/請求），請勿移除以免對網站造成壓力。
