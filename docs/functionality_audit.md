# 儀表板功能審核報告

## 審核範圍

本次審核涵蓋資料抓取、指標計算、轉折訊號後端、靜態前端與部署文件。核心檔案包含：

- `foreign_chips.py`：TWSE／TAIFEX 資料抓取、歷史更新、衍生指標與 HTML 產生。
- `signals/reversal_signals.py`：讀取 `data/history.json` 並輸出轉折確認訊號 JSON。
- `docs/index.html`：GitHub Pages 靜態儀表板與 ECharts 視覺化。
- `docs/reversal_panel.js`：轉折確認訊號燈面板。
- `README.md`：使用、資料來源與部署說明。

## 現有功能盤點

### 後端資料管線

- 已整合官方公開資料來源：TWSE 現貨三大法人買賣超、TAIFEX 期貨三大法人契約、TAIFEX 選擇權買賣權分計，以及 TWSE 加權指數歷史資料。
- 期交所金額欄位已統一由「千元」還原為「元」，避免自行維護期貨乘數或個股期貨 mapping 的誤差。
- `recompute_derived()` 會在完整歷史排序後重算 `delta_tx`、`delta_ssf`、`e_total` 與 `amount_ratio_z`，可降低回補或覆寫單日資料時的順序風險。
- 提供 `--mock`、`--inspect`、`--backfill` 等維護模式，利於本機預覽、欄位除錯與歷史補資料。

### 轉折訊號後端

- 轉折訊號採「流量 flow + 存量 stock」雙確認設計，避免單一極端情緒指標過早判定轉折。
- 門檻集中於環境變數 `REV_*`，便於 GitHub Actions 或不同市場狀態下調參。
- 輸出 `docs/data/reversal_signals.json`，使前端與資料計算解耦。
- 內建 `--selftest`，可驗證現況、確認轉折與醞釀反彈三種情境。

### 前端儀表板

- 採靜態 HTML + ECharts，部署成本低，適合 GitHub Pages。
- 現有面板已涵蓋：E_Total 組成、E_Total 累積與加權指數、選擇權平均單價、Put/Call 金額比 Z-Score。
- 轉折面板使用獨立 JavaScript 讀取 JSON，可在不重產整份 HTML 的情況下更新訊號資料。
- RWD 基礎已存在，手機寬度下卡片與圖表高度會調整。

## 可優先改善項目

### P0：資料正確性與穩定性

1. **外資列彙總需更明確**  
   現貨資料目前用「列名包含外資」加總，若 TWSE 表格同時含細分列，可能發生口徑重複或口徑變動。建議改為明確白名單，例如優先找「外資及陸資」與「外資自營商」，並在找不到時才 fallback。

2. **交易日與缺資料狀態應顯示在前端**  
   若來源尚未公布、非交易日或某來源缺值，目前使用者只看到最新靜態結果。建議在 HTML payload 加上 `data_quality`，列出每個來源是否成功、是否使用前一日值、是否缺值。

3. **Z-Score 可新增穩健統計版本**  
   現在使用均值與標準差，遇到極端值會拉高標準差，降低後續異常偵測敏感度。建議新增可選的 median/MAD Z-Score，並在前端切換「標準 Z」與「穩健 Z」。

4. **資料 schema version**  
   建議在 `history.json`、`reversal_signals.json` 與 HTML payload 加入 `schema_version`。未來欄位改名或單位調整時，前端可以明確判斷相容性。

### P1：後端功能擴充

1. **新增資料品質報表**  
   每次更新後輸出 `docs/data/quality.json`，內容包含最新資料日、各來源 HTTP 狀態、解析欄位名稱、缺值欄位、回補天數與警告訊息。

2. **新增異常值與單位檢查**  
   對 `spot_net`、`tx_oi_amount`、`ssf_oi_amount`、`p_avg_call`、`p_avg_put` 設定合理區間，若超出範圍則標記而非直接覆寫歷史。

3. **新增回測與參數掃描**  
   現有轉折規則門檻可調，但尚未提供系統化回測。建議新增 `signals/backtest.py`，輸出不同 `REV_*` 組合下的命中率、提前天數、最大回撤與假訊號率。

4. **快取與重跑保護**  
   對同一交易日重跑時，可保留原始來源 payload 或 checksum，方便確認資料供應端是否事後更正。

### P1：前端功能擴充

1. **時間區間快捷鍵**  
   ECharts 已有 dataZoom，但建議增加 1M／3M／6M／YTD／ALL 快捷鍵，降低手機操作成本。

2. **圖表資料下載**  
   新增「下載 CSV / JSON」按鈕，讓使用者可下載目前圖表期間資料，用於研究或備份。

3. **指標說明與公式 tooltip**  
   卡片與圖表標題可加入 info icon，顯示公式、單位、資料來源與判讀方式，降低新使用者理解成本。

4. **轉折訊號細節展開**  
   目前訊號 detail 主要藏在 hover title；手機不易使用。建議將 S1~S5、W1~W3 改為可展開明細列，並顯示最新值、門檻與是否觸發。

5. **警戒通知入口**  
   靜態頁可加入「複製今日摘要」按鈕，方便貼到 LINE／Slack；若搭配 GitHub Actions，亦可輸出 Markdown 摘要供通知使用。

### P2：安全性、可維護性與體驗

1. **避免不必要的 `innerHTML`**  
   `docs/reversal_panel.js` 目前以 `innerHTML` 寫入文字與錯誤訊息。雖然資料來源是本 repo 產生的 JSON，仍建議改為 `textContent` 或明確 escape，降低未來接入外部資料時的 XSS 風險。

2. **前端 fetch 錯誤分類**  
   `fetch("data/reversal_signals.json")` 建議檢查 `response.ok`，並區分 404、JSON parse error 與網路錯誤，讓維護者更快定位問題。

3. **圖表 resize 效能**  
   resize 事件可加 debounce，避免視窗連續變動時多次重繪。

4. **測試覆蓋率**  
   建議將 `to_float()`、日期正規化、TAIFEX 欄位解析、`recompute_derived()` 與轉折規則納入 pytest，避免來源欄位變動時才在正式更新發現問題。

## 建議新增功能路線圖

### 短期（1～2 天）

- 補 `data_quality` 到 JSON 與前端狀態列。
- 將轉折面板 detail 改為手機可點擊展開。
- 前端 fetch 加入 `response.ok` 檢查與友善錯誤訊息。
- 新增 CSV 下載按鈕。

### 中期（1～2 週）

- 新增 pytest 測試與 CI。
- 建立 `signals/backtest.py`，可回測不同 REV 參數。
- 新增 schema version 與來源 payload checksum。
- 增加穩健 Z-Score 與前端指標切換。

### 長期

- 新增多市場或多商品支援，例如小台、電子期、金融期、週選與月選分開比較。
- 建立通知系統，於恐慌延續、醞釀反彈、確認轉折時自動產生摘要。
- 建立資料 API 或版本化 JSON，讓其他研究工具可重用同一份資料。

## 優先結論

整體而言，此儀表板已具備完整的「資料抓取 → 指標計算 → 靜態視覺化 → 轉折判讀」閉環。最值得優先投入的是資料品質可視化、回測框架與手機端訊號明細，因為這三者能直接提升使用者信任、策略驗證能力與日常使用效率。
