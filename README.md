# 台股強勢雷達

只做上市股票。每天收盤後抓證交所日行情，算出強勢股、即將突破、突破後回踩的標的，並給出進場價、止損價、目標價。

## 檔案
- `index.html`：網頁，讀同資料夾的 `data.json`
- `build_data.py`：抓資料、排除特殊標的、跑策略、更新訊號紀錄（只用 Python 標準函式庫）
- `data.json`：目前是模擬資料。執行 `python build_data.py` 後會換成真實資料
- `data/exclude.txt`：自行補充要排除的代號
- `.github/workflows/update.yml`：GitHub Actions 每個交易日 15:40 自動更新

## 使用
1. `python build_data.py --demo`：用模擬資料測試
2. `python build_data.py`：抓真實資料。第一次要抓約 60 個交易日，因證交所限流，約需 3 到 4 分鐘
3. 把整個資料夾放到 GitHub Pages、Cloudflare Pages 或 Netlify 即可

## 尚未驗證的地方
- 開發環境連不到證交所，真實資料的串接沒有實測過。第一次執行後請檢查 `data.json` 的 `meta.passed` 是否合理（通常上市普通股約 900 檔，通過排除後數百檔）。
- 全額交割股的名單端點沒有設定，目前以「股價 10 元以上」「成交量門檻」與 `data/exclude.txt` 代替。確認證交所端點後，填進 `build_data.py` 的 `EXTRA_URLS`。
- 處置股、注意股取自證交所 OpenAPI。注意股只加標籤，不排除。
- 日 K 未還原除權息，除權息日前後可能出現不實的跳空訊號。
- 證交所可能擋海外 IP。如果 GitHub Actions 抓不到，改在自己的電腦或台灣的主機排程執行。

## 策略參數
都在 `build_data.py` 最上方：成交量門檻、最低股價、止損距離上限（7%）、最低風險報酬比（1.5）、持有天數（2 到 15）。
