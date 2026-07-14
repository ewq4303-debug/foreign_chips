# 外資籌碼總體監控儀表板

每日自動抓取 TWSE／TAIFEX 公開資料，計算外資真實單邊押注方向與極端情緒，
輸出暗色系 ECharts 靜態網頁，部署於 GitHub Pages。

## 三大模組
| 模組 | 指標 | 用途 |
|---|---|---|
| 一 | 總體淨曝險 `E_Total` = 現貨買賣超 + Δ大台未平倉金額 + Δ股期未平倉金額 | 還原外資真實方向（過濾期現套利雜訊） |
| 二A | 平均單價 `P_Avg` = 未平倉金額 / 口數 / 50 | 判斷選擇權是價外避險或價內攻擊 |
| 二B | 金額比 `Put金額/Call金額` 的 60 日 Z-Score | 偵測極端貪婪／恐慌轉折點 |

### Context：上櫃外資（`spot_net_tpex`）
上櫃（TPEx）外資買賣超是「中小型股風險偏好」的**獨立 Context 訊號**，
不是 E_Total 的補丁——期貨/選擇權腿的標的都是加權指數（僅含上市），
期現套利抵銷邏輯天然是 TWSE 口徑，因此**上櫃現貨不進 E_Total 主定義**。
另輸出 `e_total_broad = e_total + spot_net_tpex` 作**純顯示用**對照序列
（E_Total 主圖淡色線，legend 預設關閉），不被轉折燈號的核心 S1–S5 / W1–W3 規則引用。
對燈號的介入方式為**加分不否決**：Context 訊號 `C1_tpex_confirm`
（上櫃外資近 `REV_TPEX_ALIGN_N` 日同步淨買）只在 verdict 為 🟡/🟢 時把
`confidence` +1，不改變 verdict code 判定路徑。免責同下，僅供研究參考。

## 轉折確認訊號燈（`signals/reversal_signals.py`）
在儀表板尾端新增一個**確定性、可回放、門檻可調**的燈號，把「恐慌會持續還是
開始轉折」量化為四種 verdict：🔴 恐慌持續 / 🟡 醞釀反彈 / 🟢 確認轉折 / ⚪ 中性。

核心規則：**真正的「轉折」必須流量(flow) + 存量(stock) 同時成立**。
- 流量端（會均值回歸）：S1 Z 退出極端、S2 Put/Call 反折、S3 賣權溢價收斂、S5 現貨轉買/急縮。
- 存量端（守門員 ⭐）：S4 累積 E_Total 斜率翻揚 —— 沒有 S4，最高只能到 🟡。
- 持續警告：W1 Z 續創新高、W2 真出貨、W3 期貨翻空。

本模組**不重抓資料**，只讀 `data/history.json`（adapter 見檔內 `load_series()`），
輸出 `docs/data/reversal_signals.json`，前端由 `docs/reversal_panel.js` 渲染成燈號面板
（純 DOM + CSS，不需 ECharts）。所有門檻只走環境變數（`REV_*`，見檔內 `CFG`），
方便在 Actions 直接調參回測。

```bash
python -m signals.reversal_signals             # 讀 history.json → 產生 JSON
python -m signals.reversal_signals --selftest  # 驗收自測（🔴 現況 + 🟢/🟡 反向情境）
```


## 轉折訊號回測（`signals/backtest.py`）

依照轉折確認模組的規則逐日 walk-forward 回放：每個交易日只使用當日以前資料計算 verdict，
再用未來加權指數報酬評估訊號表現。預設輸出 JSON 報告到 `docs/data/backtest_report.json`，
也可額外輸出每日事件 CSV。

```bash
python -m signals.backtest --horizon 5
python -m signals.backtest --horizon 5 --csv docs/data/backtest_events.csv
python -m signals.backtest --scan --z-extreme 1.8,2.0,2.2 --flow-min 2,3
```

報告內容包含：
- 每日事件列：日期、verdict、flow score、stock/warning 狀態、`confidence`、未來報酬、最大有利/不利變動。
- 燈號摘要：各 verdict 的出現次數、勝率、平均/中位數未來報酬、平均最大有利/不利變動；
  另含 `REVERSAL_CONFIRMED × confidence∈{0,1}` 分組（核心假設檢定：上櫃同步確認的 🟢 是否優於未確認的 🟢）。
- 參數掃描：針對 `REV_Z_EXTREME`、`REV_Z_EXIT`、`REV_FLOW_MIN`、`REV_CUM_FLAT_EPS`、`REV_ETOTAL_WARN`、`REV_TPEX_ALIGN_N`（`--tpex-align-n 1,2,3`）做組合排序。

## 資料來源網址與驗證方式

三個來源網址集中定義在 `foreign_chips.py` 上方設定區（`*_URL` 常數）。
GET 的參數接在網址後，可直接貼瀏覽器看；POST 的參數在請求 body，需用「人類版查詢頁」核對。

| # | 內容 | 方法 | 端點（程式用） | 參數 |
|---|---|---|---|---|
| ① | 現貨 TWSE | GET | `https://www.twse.com.tw/rwd/zh/fund/BFI82U` | `type=day` `dayDate=YYYYMMDD` `response=json` |
| ② | 期貨 TAIFEX | POST | `https://www.taifex.com.tw/cht/3/futContractsDateDown` | `queryStartDate=YYYY/MM/DD` `queryEndDate=YYYY/MM/DD` `commodityId=`（空＝全部） |
| ③ | 選擇權 TAIFEX | POST | `https://www.taifex.com.tw/cht/3/callsAndPutsDateDown` | `queryStartDate` `queryEndDate` `commodityId=TXO` |
| ④ | 上櫃現貨 TPEx | GET | `https://www.tpex.org.tw/www/zh-tw/insti/summary` | `type=Daily` `date=YYY/MM/DD`（民國，備援西元）`response=json`；單位「元」（`TPEX_AMOUNT_MULT`，實測見 NOTES） |

**對應抓取函式**：① `fetch_spot_net()`、② `fetch_futures()`、③ `fetch_options()`、④ `fetch_spot_net_tpex()`。
其中 `http_get` / `http_post` 是本檔自訂的小工具，封裝 `requests`，加上重試 3 次、逾時 20 秒、瀏覽器 User-Agent。

**自行驗證**：
- ① 直接貼這個網址看 JSON：`https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=20260605&response=json`
- ② 期貨人類版查詢頁：`https://www.taifex.com.tw/cht/3/futContractsDate`
- ③ 選擇權人類版查詢頁：`https://www.taifex.com.tw/cht/3/callsAndPutsDate`
- ④ 上櫃人類版查詢頁：櫃買中心 → 上櫃 → 三大法人 → 買賣金額彙總；端點驗證
  用 `--inspect-tpex YYYYMMDD`（陷阱結論與核對清單見 `docs/NOTES_tpex_endpoint.md`）

帶 `Down` 結尾＝直接下載 CSV 的端點（`--inspect` 看到的欄位即來自此）；去掉 `Down`＝同內容的網頁版。三者皆每交易日約 15:00 後公布。

**抓取的關鍵欄位（千元欄已 ×1000 還原為元）**：
- ① 「外資」買賣差額（外資及陸資＋外資自營商）
- ② 臺股期貨、股票期貨的「多空未平倉契約金額淨額(千元)」取外資列
- ③ 臺指選擇權 CALL/PUT 外資的「買方未平倉口數 / 買方未平倉契約金額(千元)」（`OPT_SIDE="buy"`；可改 `"net"` 改用未平倉買賣淨額口徑）

> 期交所報表已直接公布契約金額，等同完成 口數×乘數×價格 換算，故直接採用官方金額，免維護個股期貨 mapping 表。

## 本機預覽 / 維護指令
```bash
pip install -r requirements.txt
python foreign_chips.py --mock                  # 產生模擬資料 docs/index.html
python foreign_chips.py --inspect 20260605       # 傾印期交所原始 CSV 欄位（除錯用）
python foreign_chips.py --inspect-tpex 20260605  # 傾印 TPEx 三大法人彙總原始回應（端點驗證）
python foreign_chips.py --backfill 20260301 20260605   # 區間回補歷史
```
> 無本機環境時，上述 `--inspect` / `--backfill` 可改由 `.github/workflows/tools.yml`
> 在 GitHub Actions 雲端執行（Actions → 工具 → Run workflow，選 mode）。

## 部署
1. 推送整個資料夾到 GitHub repo。
2. Settings → Pages → Source 設為 `main` 分支的 `/docs` 資料夾。
3. Settings → Actions → General → Workflow permissions 設為 **Read and write**。
4. Actions 每個交易日 17:00（台北）自動更新；也可手動 `workflow_dispatch` 回補。

## 免責
僅供研究參考，不構成投資建議。
