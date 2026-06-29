# 外資籌碼回測框架 — 設計規格書

> 本文件只談**設計**,不含實作程式。目的是把「外資籌碼訊號到底準不準」
> 變成一套**可重複、防未來函數、參數可掃描**的回測基礎建設,作為後續開發藍圖。
> 與既有 `foreign_chips.py`(資料管線)、`signals/reversal_signals.py`(訊號燈)銜接。

---

## 0. 現況盤點與限制

**已有資產**
- `data/history.json`:日資料,每列含 `taiex_close`(指數收盤)→ 回測標的報酬來源,免另抓行情。
- 現成因子欄位:`e_total`、`spot_net`、`amount_ratio_z`、`amount_ratio`、`p_avg_put`、
  `p_avg_call`、`tx_oi_amount`、`ssf_oi_amount`、`delta_tx`、`delta_ssf`、`call/put_oi/amt`。
- `signals/reversal_signals.compute(series, cfg)`:把當天狀態壓成 verdict(🔴🟡🟢⚪),
  且**所有門檻走環境變數 `REV_*`**(見該檔 `CFG`)→ 天生適合參數掃描。
- `--backfill YYYYMMDD YYYYMMDD`:區間回補,可拉長歷史。

**三大限制(框架設計的前提)**
1. **樣本短**:目前僅約 82 個交易日,reversal 事件更稀少。結論只能當「框架驗證」,
   不能當「策略已驗證」。→ 第一優先是能吃**回補後的長歷史**。
2. **`compute()` 只算最後一天**:它吃整段序列、只輸出 `series[-1]` 的判讀。
   回測需要**逐日重放**(walk-forward):站在第 t 天、只用 ≤t 的資料重算 verdict。
3. **發布時點**:三來源每交易日約 **15:00 後**才公布。T 日籌碼算出的訊號**最快 T+1 進場**。
   報酬一律從 T+1 起算,嚴禁用 T 日 close 進場。

---

## 1. 七層架構

```
[L1 資料層]      history.json(point-in-time)＋對齊 taiex_close
[L2 訊號重放層]  逐日 replay compute() → 每日 verdict / 因子值表
[L3 部位映射層]  verdict / 因子 → 目標部位(±1 / 0),含 T+1 lag
[L4 執行層]      進出場時點、成本、滑價假設(全部可調參數)
[L5 績效層]      權益曲線、Sharpe、MDD、命中率、事件研究 vs 買進持有
[L6 穩健性層]    REV_* 參數網格、walk-forward、樣本外、亂訊號對照
[L7 報表層]      接回 docs/ 暗色 ECharts 儀表板
```

建議目錄:`backtest/`(replay.py、position.py、engine.py、metrics.py、sweep.py、report.py),
資料產物走 `docs/data/backtest*.json`,沿用既有前端渲染慣例。

---

## 2. 各層細節

### L1 資料層 — point-in-time 正確性
- 讀 `history.json`,所有計算只用「截至該日」切片,模擬當時可見資訊。
- `amount_ratio_z`(滾動 z)、`peak/slope`(視窗)天然 point-in-time 安全。
- ⚠️ `cum_etotal` 是從序列起點累積,**絕對水位依賴起算日**(2026-03-02),
  回測**只能用其斜率/變化**(S4 用的正是 windowed slope),不要用絕對水位。
- 對齊行情:每個訊號日 T 需要 T、T+1、T+5、T+10、T+20 的指數價,用 `taiex_close` 取得。

### L2 訊號重放層(框架核心,最該補)
- 重放迴圈概念:`for t in range(MIN_SAMPLE, N): compute(load_series(history[:t+1]), CFG)`。
- 產出「每日訊號表」:
  `date | verdict_code | flow_score | stock_ok | warning | S1..S5 | W1..W3 | 各因子值`。
- 回測與事件研究都建在這張表上。**好處:回測跑的就是上線那套 `compute()`,無研究/上線偏差。**
- 注意:重放每天重建 series 切片,確保 `ffill`、`MIN_SAMPLE` 不足時回 NEUTRAL 的行為與線上一致。

### L3 部位映射層 — 三種模式並存
1. **事件研究(主力,樣本短時最可信)**
   - 不持續持有。看「🟢 REVERSAL_CONFIRMED」(及 🔴)出現後第 1/3/5/10/20 日的指數報酬。
   - 與「全樣本同窗口平均報酬」對照,算超額與顯著性。
2. **狀態映射(timing 策略)**
   - 🟢=+1(多)、🔴=0 或 −1(空手/放空)、🟡/⚪=維持前部位或空手(規則需寫死)。
   - 標的:加權指數或台指期。
3. **單因子檢驗(因子有效性)**
   - 對 `e_total`、`amount_ratio_z`、賣權溢價(`p_avg_put − p_avg_call`)、`spot_net` 等,
     算其與未來 N 日報酬的 **IC**(資訊係數)與**分層報酬**(高/中/低分位的後續表現)。

### L4 執行層 — 假設寫死成可調參數
- 進場:訊號日 T → **T+1 開盤**(保守版 T+1 收盤)。本資料只有 close,初版用 T+1 close。
- 出場:固定持有期(事件研究)或狀態翻轉(timing)。
- 成本:來回手續費＋交易稅＋滑價,合併成可調 `COST_BPS`。
- 原則:刻意保守,寧可低估績效。

### L5 績效層
- **基準永遠是買進持有加權指數**;不贏它就沒意義。
- 連續策略指標:累積/年化報酬、Sharpe、Sortino、最大回撤(MDD)、Calmar、
  命中率、賺賠比、單筆訊號報酬分布。
- 事件研究指標:各窗口平均/中位報酬、勝率、與基準的 **t 檢定 p 值**(避免「大盤本來就漲」)。
- 因子指標:IC、Rank-IC、IC_IR、分層單調性。

### L6 穩健性層(本專案殺手鐧)
- **參數網格掃描**:既然門檻全走 `REV_*`,直接掃 `Z_EXTREME`、`FLOW_MIN`、`CUM_SLOPE_WIN`、
  `ETOTAL_WARN`… 看績效是「高原」(穩健)還是「一根針」(過擬合,丟棄)。
- **Walk-forward / 樣本外**:前段調參、後段驗證;戰績只認樣本外。
- **對照組**:把訊號隨機 shuffle 或反向,好策略應顯著打敗亂訊號。

### L7 報表層
- 沿用暗色 ECharts:輸出 `docs/backtest.html` 或主儀表板加分頁。
- 內容:權益曲線 vs 大盤、訊號進出場標記、事件研究柱狀圖(各窗口報酬+信賴區間)、
  參數熱力圖。資料檔 `docs/data/backtest.json`,與 `reversal_signals.json` 同套慣例。

---

## 3. 防未來函數檢查清單

| 風險 | 本專案具體形態 | 對策 |
|---|---|---|
| 用到未來收盤 | T 日籌碼 15:00 才出,卻用 T 日 close 進場 | 進場一律 ≥ T+1 |
| 滾動統計偷看 | z-score / peak / slope 用到 t 之後的點 | replay 只傳 `history[:t+1]` |
| 累積量起點偏差 | `cum_etotal` 絕對水位依賴起算日 | 只用斜率,不用水位 |
| 回補偏差 | 回補時用了事後修正值 | 確認來源為當日原始公布值 |
| 參數過擬合 | 對短樣本硬調 `REV_*` | 參數高原＋樣本外驗證＋亂訊號對照 |

---

## 4. 落地順序(後續實作建議)

1. **回補長歷史**:用既有 `--backfill` 拉到 2~3 年,否則一切免談。
2. **訊號重放器**(L2):`backtest/replay.py`,輸出每日訊號表。
3. **事件研究**(L3 事件模式＋L5):先回答最基本問題「🟢 出現後指數真的會漲嗎?顯著嗎?」。
4. **連續持倉＋績效曲線**(L3 狀態映射＋L4＋L5)。
5. **單因子檢驗**(L3 因子模式):IC / 分層。
6. **參數掃描＋穩健性**(L6)。
7. **報表**(L7):接回暗色儀表板。

---

## 5. 待確認的設計決策(實作前再敲定)

- 事件研究窗口:採 1/3/5/10/20 日?是否含 T+1 開盤口徑(需另抓開盤價)?
- timing 模式下 🔴 是「空手」還是「放空」?🟡/⚪ 是維持前部位還是空手?
- 標的用加權指數(現貨,無法直接交易)還是台指期(可交易,但有轉倉成本)?
- 成本 `COST_BPS` 預設值。
- 樣本外切分點與 walk-forward 視窗長度。

> 免責:僅供研究參考,不構成投資建議。
