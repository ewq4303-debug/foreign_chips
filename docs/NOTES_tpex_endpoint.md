# NOTES_tpex_endpoint.md — TPEx 上櫃外資買賣超端點驗證紀錄

> 對應 SPEC_tpex_spot.md Phase 0。
> 狀態：**候選 A 已實作、驗證工具已就緒；線上逐欄核對待 `--inspect-tpex` 實跑**（見 §5）。

## 0. 開發環境限制聲明（誠實記錄）

本次開發所在的 CI sandbox 網路政策**封鎖所有資料來源主機**
（`www.tpex.org.tw` / `www.twse.com.tw` / `www.taifex.com.tw` / `api.finmindtrade.com`
一律 CONNECT 403，含現行程式既有的 TWSE/TAIFEX 端點）。
因此 Phase 0 的「傾印原始 response、與人類版頁面逐欄核對」無法在開發環境完成，
已改為：

1. 驗證步驟**完整寫成 `--inspect-tpex YYYYMMDD`**（比照現有 `--inspect`），
   並接入 `.github/workflows/tools.yml` 的 `inspect-tpex` 模式。
2. 解析器 `_parse_tpex_insti()` 以**欄位關鍵字比對**（比照 `find_col()` 慣例）
   而非固定索引撰寫，容忍欄位順序差異與兩種 JSON 外形（`tables[]` 與頂層 `data/aaData`）。
3. 單位換算獨立成常數 `TPEX_AMOUNT_MULT`（預設 1.0），實測若為千元欄只需改一處。

**首次上線前必跑**：Actions → 工具 → Run workflow → mode=`inspect-tpex`、
start=最近交易日，依 §5 檢核清單核對後把結論回填本文件。

## 1. 候選來源評估

| # | 候選 | 評估結果 |
|---|---|---|
| A | TPEx 新版網站 JSON 端點 | **採用**。與現行 TWSE RWD 用法最接近；同一端點以 `date` 參數查歷史日期 → backfill 可用同一來源 |
| B | TPEx OpenAPI（`/openapi/`） | 不採用主線。多數 dataset 為「最新一日」快照，不支援歷史日期查詢 → backfill 不可用（SPEC §9 已知風險）。保留為每日增量的備援選項 |
| C | FinMind | 不採用。不引入新依賴/金鑰管理；市場層級彙總 dataset 的存在性與延遲需另行確認 |

## 2. 採用端點與參數（候選 A）

- 端點常數：`TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/summary"`
  （檔頭設定區，與其他 `*_URL` 併列）
- 對應人類版查詢頁：櫃買中心網站 → 上櫃 → 三大法人 → 買賣金額彙總
  （`https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/summary/day.html`）
- 參數範例（`http_get`，GET）：

```
type=Daily
date=115/07/14        ← 民國 YYY/MM/DD（優先嘗試；由 _to_tpex_date() 轉換）
response=json
```

- 程式行為：先試民國格式；該格式無資料再退回西元 `YYYY/MM/DD` 一次
  （TPEx 新版 RWD 端點對日期參數的最終格式以 `--inspect-tpex` 實測為準，
  兩種都試可同時涵蓋改版前後行為）。
- 抓取函式：`fetch_spot_net_tpex(date_yyyymmdd) -> float | None`，
  介面與回傳語意完全比照 `fetch_spot_net()`。

## 3. 欄位對照表（預期外形；實測後回填確認）

回應 JSON 預期外形（新版 RWD 端點慣例）：

```json
{
  "tables": [
    {
      "title": "…三大法人買賣金額彙總…",
      "date": "…",
      "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
      "data": [["外資及陸資(不含外資自營商)", "…", "…", "…"],
               ["外資自營商", "…", "…", "…"], …]
    }
  ]
}
```

| 標準鍵 | TPEx 欄位定位方式 | 說明 |
|---|---|---|
| 外資列 | `data` 各列前兩欄含「外資」字樣 | 涵蓋「外資及陸資(不含外資自營商)」與「外資自營商」兩列（TPEx 列名與 TWSE 不同，故用關鍵字） |
| 買賣差額 | `fields` 中含「差額」的欄位索引；找不到退最後一欄 | 比照 TWSE 版「最後一欄為買賣差額」的後備邏輯 |
| 合計去重 | 若外資列中存在含「合計」列，**只取合計列** | 避免「合計 + 子項」重複加總；無合計列時取非合計外資列之和 |

加總口徑對齊現行 `fetch_spot_net()`：**外資及陸資 + 外資自營商 之買賣差額合計**。

## 4. 三大陷阱結論

| 陷阱 | 結論 | 依據 / 待辦 |
|---|---|---|
| **日期格式** | 參數採民國 `YYY/MM/DD`（`_to_tpex_date('20260714')` → `'115/07/14'`），失敗自動退西元 `YYYY/MM/DD` | TPEx 慣例為民國年；**待 `--inspect-tpex` 確認哪一種被接受**，確認後可移除另一種以省一次請求 |
| **金額單位** | 暫定「元」（`TPEX_AMOUNT_MULT = 1.0`） | TPEx 人類版彙總頁表尾註記「單位：元」；**必須實測**：若外資差額與人類版頁面差 1000 倍，將 `TPEX_AMOUNT_MULT` 改 `1000.0` 即可，不動其他程式 |
| **外資分類欄位** | 以「外資」關鍵字擷取列、優先取「合計」列去重 | TPEx 列名（如「外資及陸資(不含外資自營商)」）與 TWSE 不同；關鍵字比對 + 合計去重可同時涵蓋「兩列分列」與「另附合計列」兩種版型 |

## 5. 上線前核對清單（跑 `inspect-tpex` 後逐項打勾回填）

在 Actions（或可連外的本機）執行
`python foreign_chips.py --inspect-tpex <最近交易日YYYYMMDD>`，然後：

- [ ] 記錄實際被接受的日期參數格式（民國 or 西元），與 response 內日期欄格式
- [ ] 傾印的 `fields` 與 §3 對照表一致（或依實況更新對照表）
- [ ] `_parse_tpex_insti` 外資合計 × `TPEX_AMOUNT_MULT` 與人類版頁面
      「外資及陸資 + 外資自營商 買賣差額合計」**至少 3 個不同交易日誤差 = 0**，
      並把日期與數字對照貼回本節
- [ ] 單位結論回填 §4（維持 1.0 或改 1000.0）
- [ ] 用歷史日期（如 20 個交易日前）確認同端點可查歷史 → backfill 可行性結論

核對數字對照（回填區）：

| 日期 | 端點外資合計（元） | 人類版頁面合計（元） | 誤差 |
|---|---|---|---|
| （待填） | | | |
| （待填） | | | |
| （待填） | | | |

## 6. 公布時間與缺值行為

- TPEx 約每交易日 15:00 後公布，與 TWSE 可能有時間差。
- 單邊缺值**不阻擋當日入庫**：`spot_net_tpex` 不參與「spot/tx/opt 全 None 才跳過」
  判斷；當日缺值存 `None`，前端顯示 `--`，隔日重抓由既有同日 upsert 機制補齊。
- 缺值當日 job 以 exit code 0 結束，僅 `log()` 警示（已驗證：`fetch_spot_net_tpex`
  失敗路徑一律回 `None`，不 raise）。
