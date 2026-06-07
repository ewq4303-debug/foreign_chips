# 外資籌碼總體監控儀表板

每日自動抓取 TWSE／TAIFEX 公開資料，計算外資真實單邊押注方向與極端情緒，
輸出暗色系 ECharts 靜態網頁，部署於 GitHub Pages。

## 三大模組
| 模組 | 指標 | 用途 |
|---|---|---|
| 一 | 總體淨曝險 `E_Total` = 現貨買賣超 + Δ大台未平倉金額 + Δ股期未平倉金額 | 還原外資真實方向（過濾期現套利雜訊） |
| 二A | 平均單價 `P_Avg` = 未平倉金額 / 口數 / 50 | 判斷選擇權是價外避險或價內攻擊 |
| 二B | 金額比 `Put金額/Call金額` 的 60 日 Z-Score | 偵測極端貪婪／恐慌轉折點 |

## 資料來源
- 現貨：TWSE 三大法人買賣金額統計表（BFI82U）
- 期貨：TAIFEX 三大法人－區分各期貨契約（臺股期貨、股票期貨）
- 選擇權：TAIFEX 三大法人－選擇權買賣權分計（TXO）

> 期交所報表「已直接公布契約金額（千元）」，等同已完成 口數×乘數×價格 換算，
> 故本程式直接採用官方金額（已 ×1000 還原為元），免維護個股期貨 mapping 表。

## 本機預覽
```bash
pip install -r requirements.txt
python foreign_chips.py --mock      # 產生模擬資料 docs/index.html
```

## 部署
1. 推送整個資料夾到 GitHub repo。
2. Settings → Pages → Source 設為 `main` 分支的 `/docs` 資料夾。
3. Settings → Actions → General → Workflow permissions 設為 **Read and write**。
4. Actions 每個交易日 17:00（台北）自動更新；也可手動 `workflow_dispatch` 回補。

## 免責
僅供研究參考，不構成投資建議。
