#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外資籌碼總體監控儀表板  foreign_chips.py
==========================================
資料來源：台灣證交所 (TWSE) + 台灣期交所 (TAIFEX)，皆為官方公開資料。

三大模組
  模組一  總體淨曝險  E_Total = 現貨買賣超 + Δ(大台未平倉金額) + Δ(股票期貨未平倉金額)
  模組二A 平均單價反推法  P_Avg = 未平倉契約金額 / 未平倉口數 / 50
  模組二B 極端情緒指標    Amount_Ratio = Put金額 / Call金額，並計算 60 日 Z-Score

重要設計說明
  原始公式為  ΔN × 契約乘數 × 收盤價。但 TAIFEX 三大法人報表「已直接公布契約金額
  (單位：千元)」，等同於已經幫我們做完  口數 × 乘數 × 價格  的換算，且更精準
  (含當日結算價、無乘數對應錯誤)。因此本程式直接採用官方公布金額，省去自行維護
  個股期貨 mapping 表的誤差來源。所有「千元」欄位已 ×1000 還原為「元」。

執行方式
  python foreign_chips.py            # 正常抓今日資料、更新歷史、產生 HTML
  python foreign_chips.py --mock     # 不連網，產生模擬資料的 HTML 預覽 (本機檢視用)
  python foreign_chips.py --date 20260605   # 指定日期回補
"""

import os
import sys
import csv
import json
import math
import time
import argparse
import datetime as dt
from io import StringIO

import requests

# ───────────────────────────────────────────────────────────────────
#  設定區（首次部署若欄位對不上，主要調整這裡）
# ───────────────────────────────────────────────────────────────────
TPE_TZ = dt.timezone(dt.timedelta(hours=8))           # 台北時區
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
OUTPUT_HTML = os.path.join(DOCS_DIR, "index.html")

ZSCORE_WINDOW = 60          # Z-Score 滾動視窗（交易日）
TXO_MULTIPLIER = 50         # 台指選擇權每點 50 元

# 選擇權未平倉口徑：
#   "buy" = 買方未平倉（外資實際投入的權利金，對應文件「真金白銀投入」）
#   "net" = 未平倉買賣淨額（傳統券商報告常用口徑）
OPT_SIDE = "buy"
REQUEST_TIMEOUT = 20
RETRY = 3

# 平均單價判讀門檻（點）
P_AVG_OTM_HEDGE = 20        # 低於此值：深度價外避險單，方向性弱
P_AVG_DIRECTIONAL = 150     # 高於此值：價平/價內，外資強烈單邊表態

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}

# TAIFEX 下載端點（CSV，POST）
TAIFEX_FUT_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
TAIFEX_OPT_URL = "https://www.taifex.com.tw/cht/3/callsAndPutsDateDown"
# TWSE 三大法人買賣金額統計表
TWSE_BFI_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
# TWSE 發行量加權股價指數歷史（每月一檔，含每日開高低收）
TWSE_TAIEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
# TPEx 上櫃三大法人買賣金額彙總（新版 RWD 網站 JSON；候選 A，
# 端點驗證流程與陷阱結論見 docs/NOTES_tpex_endpoint.md，用 --inspect-tpex 實測）
TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/summary"

# ── Context：上櫃（TPEx）外資買賣超 ──
# 金額欄位換算為「元」的乘數：TPEx 人類版彙總頁表尾註記「單位：元」→ 預設 1.0；
# 若 --inspect-tpex 實測為千元欄，改為 1000.0（不得憑記憶假設與 TWSE 相同）
TPEX_AMOUNT_MULT = 1.0
TPEX_CUM_WINDOW = 20        # 上櫃買賣超滾動累積視窗（交易日）
TPEX_MIN_SAMPLE = 15        # 累積視窗內最低有效樣本數，不足 → cum 為 None


# ───────────────────────────────────────────────────────────────────
#  小工具
# ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{dt.datetime.now(TPE_TZ):%H:%M:%S}] {msg}", flush=True)


def to_float(s):
    """把 '1,234' / '-' / '' 之類字串轉成 float，失敗回 0.0"""
    if s is None:
        return 0.0
    s = str(s).replace(",", "").replace("　", "").strip()
    if s in ("", "-", "--", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def http_get(url, **kw):
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:                       # noqa: BLE001
            log(f"  GET 失敗({i+1}/{RETRY}) {url} → {e}")
            time.sleep(2)
    return None


def http_post(url, data, **kw):
    for i in range(RETRY):
        try:
            r = requests.post(url, data=data, headers=HEADERS,
                              timeout=REQUEST_TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:                       # noqa: BLE001
            log(f"  POST 失敗({i+1}/{RETRY}) {url} → {e}")
            time.sleep(2)
    return None


def decode_taifex(content_bytes):
    """TAIFEX CSV 多為 Big5/MS950，少數 UTF-8，逐一嘗試"""
    for enc in ("big5", "cp950", "utf-8-sig", "utf-8"):
        try:
            return content_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return content_bytes.decode("utf-8", errors="ignore")


def find_col(header, *keywords):
    """在 CSV 表頭中找出同時包含所有 keyword 的欄位索引，找不到回 -1"""
    for idx, name in enumerate(header):
        n = name.replace(" ", "")
        if all(k in n for k in keywords):
            return idx
    return -1


# ───────────────────────────────────────────────────────────────────
#  資料抓取
# ───────────────────────────────────────────────────────────────────
def fetch_spot_net(date_yyyymmdd):
    """模組一：外資現貨買賣超總金額（元）。TWSE BFI82U。"""
    params = {"type": "day", "dayDate": date_yyyymmdd, "response": "json"}
    r = http_get(TWSE_BFI_URL, params=params)
    if not r:
        return None
    try:
        j = r.json()
        if j.get("stat") != "OK" or not j.get("data"):
            log("  TWSE：當日無資料（可能非交易日或尚未公布）")
            return None
        foreign = 0.0
        for row in j["data"]:
            name = str(row[0])
            if "外資" in name:                       # 外資及陸資 + 外資自營商
                foreign += to_float(row[-1])         # 最後一欄為買賣差額
        return foreign                               # 單位：元
    except Exception as e:                           # noqa: BLE001
        log(f"  TWSE 解析失敗：{e}")
        return None


def _to_tpex_date(date_yyyymmdd):
    """西元 YYYYMMDD → TPEx 慣用民國日期 'YYY/MM/DD'。
    例：'20260714' → '115/07/14'、'20250102' → '114/01/02'"""
    d = dt.datetime.strptime(str(date_yyyymmdd), "%Y%m%d").date()
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _parse_tpex_insti(j):
    """從 TPEx 三大法人彙總 JSON 取出「外資買賣差額合計」（原始單位，未換算）。
    口徑對齊 fetch_spot_net()：外資及陸資 + 外資自營商 之買賣差額合計；
    若表內已有外資「合計」列則直接採用，避免與子項重複加總。解析失敗回 None。"""
    if not isinstance(j, dict):
        return None
    tables = j.get("tables")
    if not isinstance(tables, list) or not tables:
        tables = [j]                                 # 後備：頂層直接帶 fields/data
    for t in tables:
        if not isinstance(t, dict):
            continue
        fields = [str(f) for f in (t.get("fields") or [])]
        data = t.get("data") or t.get("aaData") or []
        if not data:
            continue
        c_diff = next((i for i, f in enumerate(fields) if "差額" in f), None)
        foreign_rows = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            name = "".join(str(x) for x in row[:2])  # 單位名稱可能在第 1 或 2 欄
            if "外資" not in name:
                continue
            idx = c_diff if (c_diff is not None and c_diff < len(row)) else len(row) - 1
            foreign_rows.append((name, to_float(row[idx])))
        if not foreign_rows:
            continue
        totals = [v for n, v in foreign_rows if "合計" in n]
        if totals:
            return totals[0]
        return sum(v for n, v in foreign_rows if "合計" not in n)
    return None


def fetch_spot_net_tpex(date_yyyymmdd):
    """Context：上櫃（TPEx）外資買賣超總金額（元）。介面與回傳語意比照
    fetch_spot_net()：非交易日 / 未公布 / 解析失敗回 None 並 log()。
    日期參數先試民國格式（TPEx 慣用），無資料再退回西元格式。"""
    slash = dt.datetime.strptime(str(date_yyyymmdd), "%Y%m%d").strftime("%Y/%m/%d")
    for date_str in (_to_tpex_date(date_yyyymmdd), slash):
        params = {"type": "Daily", "date": date_str, "response": "json"}
        r = http_get(TPEX_INSTI_URL, params=params)
        if not r:
            continue
        try:
            val = _parse_tpex_insti(r.json())
        except ValueError as e:
            log(f"  TPEx 回應非 JSON：{e}")
            continue
        if val is not None:
            return val * TPEX_AMOUNT_MULT            # 統一換算為「元」
    log("  TPEx：當日無資料（可能非交易日或尚未公布）")
    return None


def _parse_taifex_fut(text):
    """解析三大法人-區分各期貨契約 CSV，回傳 {(商品,身分別): 未平倉淨額金額(元)}"""
    reader = csv.reader(StringIO(text))
    rows = [r for r in reader if len(r) > 3]
    if not rows:
        return {}
    header = rows[0]
    c_prod = find_col(header, "商品")
    c_role = find_col(header, "身分別") if find_col(header, "身分別") >= 0 \
        else find_col(header, "身份別")
    # 未平倉多空淨額 的「契約金額」欄（千元）
    c_amt = find_col(header, "未平倉", "淨額", "金額")
    if c_amt < 0:                                    # 後備：抓最後一個含「金額」的欄
        cands = [i for i, n in enumerate(header) if "金額" in n]
        c_amt = cands[-1] if cands else -1
    if min(c_prod, c_role, c_amt) < 0:
        log(f"  TAIFEX 期貨表頭比對失敗：{header}")
        return {}
    out = {}
    for row in rows[1:]:
        if max(c_prod, c_role, c_amt) >= len(row):
            continue
        prod = row[c_prod].strip()
        role = row[c_role].strip()
        out[(prod, role)] = to_float(row[c_amt]) * 1000.0   # 千元→元
    return out


def fetch_futures(date_slash):
    """模組一：外資 大台(TX) + 股票期貨(SSF) 未平倉淨額金額（元）。"""
    payload = {"queryStartDate": date_slash, "queryEndDate": date_slash,
               "commodityId": ""}
    r = http_post(TAIFEX_FUT_URL, payload)
    if not r:
        return None, None
    table = _parse_taifex_fut(decode_taifex(r.content))
    if not table:
        return None, None

    def pick(prod_key):
        for (prod, role), amt in table.items():
            if prod_key in prod and "外資" in role:
                return amt
        return None

    tx = pick("臺股期貨")                            # 大台指
    ssf = pick("股票期貨")                           # 個股期貨彙總
    return tx, ssf


def _parse_taifex_opt(text):
    """解析三大法人-選擇權買賣權分計 CSV，回傳買權/賣權 外資未平倉(口數,金額元)。
       依 OPT_SIDE 明確選欄：buy=買方未平倉、net=未平倉買賣淨額。"""
    reader = csv.reader(StringIO(text))
    rows = [r for r in reader if len(r) > 3]
    if not rows:
        return None
    header = rows[0]
    c_prod = find_col(header, "商品")
    c_cp = find_col(header, "買賣權")
    c_role = find_col(header, "身分別") if find_col(header, "身分別") >= 0 \
        else find_col(header, "身份別")
    if OPT_SIDE == "net":
        c_oi = find_col(header, "未平倉", "口數", "淨額")     # 未平倉口數買賣淨額
        c_amt = find_col(header, "未平倉", "金額", "淨額")    # 未平倉契約金額買賣淨額(千元)
    else:                                                    # buy（預設）
        c_oi = find_col(header, "買方", "未平倉", "口數")      # 買方未平倉口數
        c_amt = find_col(header, "買方", "未平倉", "金額")     # 買方未平倉契約金額(千元)
    if min(c_prod, c_cp, c_role, c_oi, c_amt) < 0:
        log(f"  TAIFEX 選擇權表頭比對失敗：{header}")
        return None
    res = {"call": [0.0, 0.0], "put": [0.0, 0.0]}    # [口數, 金額(元)]
    for row in rows[1:]:
        if max(c_prod, c_cp, c_role, c_oi, c_amt) >= len(row):
            continue
        if "臺指選擇權" not in row[c_prod] and "TXO" not in row[c_prod].upper():
            continue
        if "外資" not in row[c_role]:
            continue
        cp = row[c_cp]
        key = "call" if ("買權" in cp or "CALL" in cp.upper()) else \
              ("put" if ("賣權" in cp or "PUT" in cp.upper()) else None)
        if not key:
            continue
        res[key][0] = to_float(row[c_oi])
        res[key][1] = to_float(row[c_amt]) * 1000.0  # 千元→元
    return res


def fetch_options(date_slash):
    """模組二：外資 TXO 買權/賣權 未平倉口數與金額。"""
    payload = {"queryStartDate": date_slash, "queryEndDate": date_slash,
               "commodityId": "TXO"}
    r = http_post(TAIFEX_OPT_URL, payload)
    if not r:
        return None
    return _parse_taifex_opt(decode_taifex(r.content))


_TAIEX_CACHE = {}        # {YYYYMM: {iso_date: 收盤指數}}


def _norm_roc_date(s):
    """把 '115/06/03' 或 '2026/06/03' 正規化為 '2026-06-03'，失敗回 None"""
    s = str(s).strip().replace("年", "/").replace("月", "/").replace("日", "")
    parts = [p for p in s.replace("-", "/").split("/") if p != ""]
    if len(parts) < 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if y < 1911:                       # 民國 → 西元
        y += 1911
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def fetch_taiex_month(yyyymm):
    """抓某月份每日加權指數收盤，回傳 {iso_date: close}。容忍民國/西元日期、
       以欄位關鍵字定位『收盤指數』。結果快取於 _TAIEX_CACHE。"""
    if yyyymm in _TAIEX_CACHE:
        return _TAIEX_CACHE[yyyymm]
    r = http_get(TWSE_TAIEX_URL, params={"date": yyyymm + "01", "response": "json"})
    out = {}
    if r:
        try:
            j = r.json()
            fields = j.get("fields", [])
            c_date = next((i for i, f in enumerate(fields) if "日期" in f), 0)
            c_close = next((i for i, f in enumerate(fields) if "收盤" in f), -1)
            if c_close < 0:
                c_close = len(fields) - 1          # 後備：通常收盤在最後一欄
            for row in j.get("data", []):
                iso = _norm_roc_date(row[c_date])
                if iso:
                    out[iso] = to_float(row[c_close])
        except Exception as e:                       # noqa: BLE001
            log(f"  加權指數解析失敗：{e}")
    _TAIEX_CACHE[yyyymm] = out
    return out


def fetch_taiex_close(date_obj):
    """取得指定日的加權指數收盤（元/點），無資料回 None"""
    month = fetch_taiex_month(date_obj.strftime("%Y%m"))
    return month.get(date_obj.isoformat())


# ───────────────────────────────────────────────────────────────────
#  歷史儲存 + 指標計算
# ───────────────────────────────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"history": []}


def save_history(hist):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def zscore(series, value):
    """以最近 ZSCORE_WINDOW 筆 series 計算 value 的 Z-Score"""
    sample = [x for x in series[-ZSCORE_WINDOW:] if x is not None]
    if len(sample) < 5:
        return None
    mean = sum(sample) / len(sample)
    var = sum((x - mean) ** 2 for x in sample) / len(sample)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (value - mean) / sd


def compute_record(date_iso, spot, tx, ssf, opt, taiex=None, history=None,
                   spot_tpex=None):
    """組裝單日「原始 + 當日」欄位（不含跨日 Δ / E_Total / Z-Score，
       那些由 recompute_derived 以完整歷史統一重算，避免插入順序造成錯誤）。"""
    rec = {
        "date": date_iso,
        "spot_net": spot,                 # 現貨買賣超（元）
        "tx_oi_amount": tx,               # 大台未平倉淨額金額（元）
        "ssf_oi_amount": ssf,             # 股票期貨未平倉淨額金額（元）
        "taiex_close": taiex,             # 加權指數收盤（點）
        "spot_net_tpex": spot_tpex,       # 上櫃外資買賣超（元）；無資料 = None
    }
    # ── 模組二：選擇權（皆為當日欄位）──
    if opt:
        call_oi, call_amt = opt["call"]
        put_oi, put_amt = opt["put"]
        rec["call_oi"], rec["call_amt"] = call_oi, call_amt
        rec["put_oi"], rec["put_amt"] = put_oi, put_amt
        rec["p_avg_call"] = (call_amt / call_oi / TXO_MULTIPLIER) if call_oi else None
        rec["p_avg_put"] = (put_amt / put_oi / TXO_MULTIPLIER) if put_oi else None
        rec["amount_ratio"] = (put_amt / call_amt) if call_amt else None
        rec["contract_ratio"] = (put_oi / call_oi) if call_oi else None
    else:
        for k in ("call_oi", "call_amt", "put_oi", "put_amt",
                  "p_avg_call", "p_avg_put", "amount_ratio", "contract_ratio"):
            rec[k] = None
    return rec


def recompute_derived(history):
    """以完整、排序後的歷史，統一重算所有跨日欄位（Δ大台、Δ股期、E_Total、
       金額比 Z-Score）。與資料插入順序無關，可重複呼叫（冪等）。"""
    history.sort(key=lambda r: r["date"])
    ar_series = []                                   # 逐日累積金額比，供 Z-Score
    tpex_vals = []                                   # 上櫃買賣超（與 history 對齊，含 None）
    tpex_z_series = []                               # 上櫃買賣超有效值序列，供 Z-Score
    for i, rec in enumerate(history):
        prev = history[i - 1] if i > 0 else {}
        tx, ssf = rec.get("tx_oi_amount"), rec.get("ssf_oi_amount")
        spot = rec.get("spot_net") or 0.0
        d_tx = (tx - prev["tx_oi_amount"]) if (
            tx is not None and prev.get("tx_oi_amount") is not None) else 0.0
        d_ssf = (ssf - prev["ssf_oi_amount"]) if (
            ssf is not None and prev.get("ssf_oi_amount") is not None) else 0.0
        rec["delta_tx"] = d_tx
        rec["delta_ssf"] = d_ssf
        rec["e_total"] = spot + d_tx + d_ssf
        ar = rec.get("amount_ratio")
        rec["amount_ratio_z"] = zscore(ar_series, ar) if ar is not None else None
        if ar is not None:                           # Z 以「之前」的天數為基準
            ar_series.append(ar)
        # ── Context：上櫃外資買賣超衍生欄位（舊紀錄無此 key，一律 .get，
        #    None 視為缺值不當 0 參與計算；e_total_broad 純顯示用，不回寫 e_total）──
        tp = rec.get("spot_net_tpex")
        tpex_vals.append(tp)
        window = [v for v in tpex_vals[-TPEX_CUM_WINDOW:] if v is not None]
        rec["spot_net_tpex_cum20"] = (
            sum(window) if len(window) >= TPEX_MIN_SAMPLE else None)
        rec["spot_net_tpex_z"] = zscore(tpex_z_series, tp) if tp is not None else None
        if tp is not None:                           # Z 以「之前」的天數為基準
            tpex_z_series.append(tp)
        rec["e_total_broad"] = rec["e_total"] + (tp or 0.0)
    return history


def upsert(history, rec):
    """同日覆寫，否則新增，並依日期排序。"""
    history = [r for r in history if r["date"] != rec["date"]]
    history.append(rec)
    history.sort(key=lambda r: r["date"])
    return history


# ───────────────────────────────────────────────────────────────────
#  模擬資料（--mock：本機檢視 UI 用，不連網）
# ───────────────────────────────────────────────────────────────────
def build_mock_history(n=80):
    import random
    random.seed(7)
    hist = []
    tx = 1.8e11
    ssf = 4.0e10
    taiex = 21800.0
    base = dt.date(2026, 2, 17)
    d = base
    for i in range(n):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        spot = random.gauss(0, 1) * 1.2e10
        tx += random.gauss(0, 1) * 6e9
        ssf += random.gauss(0, 1) * 2e9
        taiex += random.gauss(15 if i < n * 0.7 else -45, 1) * 1.3 + random.gauss(0, 1) * 70
        call_oi = random.uniform(8.0e3, 1.4e4)
        put_oi = random.uniform(3.0e4, 5.0e4)
        p_call = random.uniform(3800, 5200)      # 外資深價內合成多單，平均單價偏高
        p_put = random.uniform(90, 280)          # 避險賣權，數十至數百點
        call_amt = call_oi * p_call * TXO_MULTIPLIER
        put_amt = put_oi * p_put * TXO_MULTIPLIER
        # 製造一段情緒飆升
        if 55 <= i <= 63:
            put_amt *= 1.6
        opt = {"call": [call_oi, call_amt], "put": [put_oi, put_amt]}
        # 上櫃外資買賣超：量級約為 TWSE spot 的 ~1/8
        spot_tpex = random.gauss(0, 1) * 1.5e9
        rec = compute_record(d.isoformat(), spot, tx, ssf, opt, round(taiex, 2),
                             spot_tpex=spot_tpex)
        hist = upsert(hist, rec)
        d += dt.timedelta(days=1)
    return {"history": hist}


# ───────────────────────────────────────────────────────────────────
#  HTML 產生（ECharts 暗色系）
# ───────────────────────────────────────────────────────────────────
def signal_for(rec):
    """依金額比 Z-Score 與 E_Total 給出總覽燈號"""
    z = rec.get("amount_ratio_z")
    if z is None:
        return ("#8b93a7", "資料不足", "尚無足夠歷史計算 Z-Score")
    if z >= 2:
        return ("#ff4d4f", "極度恐慌", "外資避險金額異常飆高，留意波段回檔")
    if z >= 1:
        return ("#ffa940", "偏向避險", "賣權投入資金升溫")
    if z <= -2:
        return ("#52c41a", "極度樂觀", "避險需求極低，留意過熱")
    if z <= -1:
        return ("#73d13d", "偏向樂觀", "賣權投入資金偏低")
    return ("#40a9ff", "情緒中性", "金額比落在常態區間")


def render_html(hist):
    history = recompute_derived(hist["history"])   # 畫圖前統一重算，冪等
    if not history:
        raise SystemExit("無資料可產生 HTML")
    latest = history[-1]
    updated = dt.datetime.now(TPE_TZ).strftime("%Y-%m-%d %H:%M")
    sig_color, sig_text, sig_desc = signal_for(latest)

    def col(key, scale=1.0):
        return [round((r.get(key) or 0) / scale, 2) for r in history]

    def coln(key, scale=1.0):
        """同 col()，但缺值保留 null（圖上留空，不畫成 0）"""
        return [round(r.get(key) / scale, 2) if r.get(key) is not None else None
                for r in history]

    payload = {
        "dates": [r["date"] for r in history],
        "e_total": col("e_total", 1e8),          # 億元
        "e_total_broad": col("e_total_broad", 1e8),   # 含上櫃對照線（純顯示用）
        "spot": col("spot_net", 1e8),
        "delta_tx": col("delta_tx", 1e8),
        "delta_ssf": col("delta_ssf", 1e8),
        "spot_tpex": coln("spot_net_tpex", 1e8),          # 上櫃外資買賣超（億）
        "spot_tpex_cum20": coln("spot_net_tpex_cum20", 1e8),  # 20 日滾動累積（億）
        "p_call": [round(r.get("p_avg_call"), 1) if r.get("p_avg_call") else None for r in history],
        "p_put": [round(r.get("p_avg_put"), 1) if r.get("p_avg_put") else None for r in history],
        "ratio": [round(r.get("amount_ratio"), 3) if r.get("amount_ratio") else None for r in history],
        "ratio_z": [round(r.get("amount_ratio_z"), 2) if r.get("amount_ratio_z") is not None else None for r in history],
        "taiex": [round(r.get("taiex_close"), 2) if r.get("taiex_close") else None for r in history],
        "thr_otm": P_AVG_OTM_HEDGE,
        "thr_dir": P_AVG_DIRECTIONAL,
    }

    cards = {
        "e_total": f"{(latest.get('e_total') or 0)/1e8:+.1f}",
        "spot": f"{(latest.get('spot_net') or 0)/1e8:+.1f}",
        "ratio": f"{latest.get('amount_ratio'):.3f}" if latest.get("amount_ratio") else "—",
        "ratio_z": f"{latest.get('amount_ratio_z'):+.2f}" if latest.get("amount_ratio_z") is not None else "—",
        "p_call": f"{latest.get('p_avg_call'):.0f}" if latest.get("p_avg_call") else "—",
        "p_put": f"{latest.get('p_avg_put'):.0f}" if latest.get("p_avg_put") else "—",
        "tpex": (f"{latest.get('spot_net_tpex')/1e8:+.1f}"
                 if latest.get("spot_net_tpex") is not None else "--"),
    }

    html = HTML_TEMPLATE
    html = html.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__UPDATED__", updated)
    html = html.replace("__DATE__", latest["date"])
    html = html.replace("__SIG_COLOR__", sig_color)
    html = html.replace("__SIG_TEXT__", sig_text)
    html = html.replace("__SIG_DESC__", sig_desc)
    html = html.replace("__C_ETOTAL__", cards["e_total"])
    html = html.replace("__C_SPOT__", cards["spot"])
    html = html.replace("__C_RATIO__", cards["ratio"])
    html = html.replace("__C_RATIOZ__", cards["ratio_z"])
    html = html.replace("__C_PCALL__", cards["p_call"])
    html = html.replace("__C_PPUT__", cards["p_put"])
    html = html.replace("__C_TPEX__", cards["tpex"])

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"  已輸出 HTML → {OUTPUT_HTML}")


# HTML 模板於檔末定義
HTML_TEMPLATE = ""   # placeholder，實際內容在 _load_template() 注入


# ───────────────────────────────────────────────────────────────────
#  主流程
# ───────────────────────────────────────────────────────────────────
def fetch_one_day(date_obj):
    """抓取單一日期的來源原始值，回傳 (spot, tx, ssf, opt, taiex, spot_tpex)"""
    ymd = date_obj.strftime("%Y%m%d")
    slash = date_obj.strftime("%Y/%m/%d")
    spot = fetch_spot_net(ymd)
    tx, ssf = fetch_futures(slash)
    opt = fetch_options(slash)
    taiex = fetch_taiex_close(date_obj)
    spot_tpex = fetch_spot_net_tpex(ymd)
    return spot, tx, ssf, opt, taiex, spot_tpex


def run_single(date_obj):
    """抓一天、更新歷史、重產 HTML"""
    iso = date_obj.isoformat()
    log(f"開始抓取 {iso}")
    spot, tx, ssf, opt, taiex, spot_tpex = fetch_one_day(date_obj)
    log(f"  現貨買賣超：{spot}")
    log(f"  大台未平倉金額：{tx}　股票期貨：{ssf}")
    log(f"  選擇權：{opt}")
    log(f"  加權指數：{taiex}")
    log(f"  上櫃外資買賣超：{spot_tpex}")
    # TPEx 公布時間可能晚於 TWSE：單邊缺值不阻擋當日入庫，
    # spot_net_tpex 不參與「全 None 才跳過」的判斷（隔日 upsert 自動補齊）
    if spot is None and tx is None and opt is None:
        log("三個來源皆無資料，視為非交易日，結束（不更新）")
        return
    hist = load_history()
    rec = compute_record(iso, spot, tx, ssf, opt, taiex, spot_tpex=spot_tpex)
    hist["history"] = upsert(hist["history"], rec)
    recompute_derived(hist["history"])       # 統一重算跨日欄位
    save_history(hist)
    log(f"  E_Total={rec.get('e_total', 0)/1e8:+.2f} 億　金額比={rec.get('amount_ratio')}")
    render_html(hist)
    log("完成")


def run_backfill(start_ymd, end_ymd):
    """區間回補：逐個交易日抓取，依日期遞增累積歷史，最後重產 HTML。
       建議在本機執行（資料量大、時間長，會超過 Actions 的 10 分鐘上限）。"""
    start = dt.datetime.strptime(start_ymd, "%Y%m%d").date()
    end = dt.datetime.strptime(end_ymd, "%Y%m%d").date()
    hist = load_history()
    d = start
    ok = skip = 0
    while d <= end:
        if d.weekday() >= 5:                 # 跳過週末
            d += dt.timedelta(days=1)
            continue
        spot, tx, ssf, opt, taiex, spot_tpex = fetch_one_day(d)
        if spot is None and tx is None and opt is None:
            log(f"  {d} 無資料（假日/未公布），略過")
            skip += 1
        else:
            rec = compute_record(d.isoformat(), spot, tx, ssf, opt, taiex,
                                 spot_tpex=spot_tpex)
            hist["history"] = upsert(hist["history"], rec)
            ok += 1
            log(f"  {d}　金額比={rec.get('amount_ratio')}　共 {len(hist['history'])} 筆")
        save_history(hist)                   # 每天即時存檔，中斷可續跑
        time.sleep(1.0)                      # 對伺服器友善
        d += dt.timedelta(days=1)
    recompute_derived(hist["history"])       # 全部補完後統一重算 Δ/E_Total/Z-Score
    save_history(hist)
    render_html(hist)
    log(f"回補完成：成功 {ok} 天、略過 {skip} 天，歷史共 {len(hist['history'])} 筆")


def run_inspect(date_obj):
    """傾印 TAIFEX CSV 與 TWSE 加權指數原始回應，用來校對欄位（除錯用）"""
    slash = date_obj.strftime("%Y/%m/%d")
    for label, url, cid in [("期貨", TAIFEX_FUT_URL, ""),
                            ("選擇權", TAIFEX_OPT_URL, "TXO")]:
        log(f"===== {label} 原始 CSV（{slash}）=====")
        r = http_post(url, {"queryStartDate": slash, "queryEndDate": slash,
                            "commodityId": cid})
        if not r:
            log("  抓取失敗")
            continue
        text = decode_taifex(r.content)
        rows = list(csv.reader(StringIO(text)))
        for i, row in enumerate(rows[:6]):
            print(f"  [{i}] {row}")

    # TWSE 加權指數（MI_5MINS_HIST，回傳當月每日開高低收 JSON）
    ym = date_obj.strftime("%Y%m01")
    log(f"===== 加權指數 原始 JSON（{ym} 當月）=====")
    r = http_get(TWSE_TAIEX_URL, params={"date": ym, "response": "json"})
    if r:
        try:
            j = r.json()
            print("  fields:", j.get("fields"))
            for i, row in enumerate(j.get("data", [])[:4]):
                print(f"  data[{i}] {row}")
        except Exception as e:                       # noqa: BLE001
            print("  解析失敗：", e, " 原始前 300 字：", r.text[:300])
    else:
        log("  抓取失敗")


def run_inspect_tpex(date_obj):
    """Phase 0 端點驗證：傾印 TPEx 三大法人彙總原始回應（民國/西元兩種日期參數
    都試），列出全欄位與外資列，供與人類版查詢頁逐欄核對單位、日期、欄名。
    結論記錄於 docs/NOTES_tpex_endpoint.md。"""
    ymd = date_obj.strftime("%Y%m%d")
    slash = date_obj.strftime("%Y/%m/%d")
    for date_str in (_to_tpex_date(ymd), slash):
        log(f"===== TPEx 三大法人彙總 原始回應（date={date_str}）=====")
        r = http_get(TPEX_INSTI_URL,
                     params={"type": "Daily", "date": date_str, "response": "json"})
        if not r:
            log("  抓取失敗")
            continue
        try:
            j = r.json()
        except ValueError:
            print("  非 JSON，原始前 500 字：", r.text[:500])
            continue
        print("  top-level keys:", list(j.keys()) if isinstance(j, dict) else type(j))
        tables = j.get("tables") if isinstance(j, dict) else None
        for ti, t in enumerate(tables if isinstance(tables, list) else [j]):
            if not isinstance(t, dict):
                continue
            print(f"  ── table[{ti}] title={t.get('title')!r} date={t.get('date')!r}")
            print("     fields:", t.get("fields"))
            for i, row in enumerate((t.get("data") or t.get("aaData") or [])[:12]):
                print(f"     data[{i}] {row}")
        parsed = _parse_tpex_insti(j)
        print(f"  → _parse_tpex_insti 外資合計（原始單位）= {parsed}")
        if parsed is not None:
            print(f"  → × TPEX_AMOUNT_MULT({TPEX_AMOUNT_MULT}) = {parsed * TPEX_AMOUNT_MULT} 元")
            print("  ↑ 請與人類版頁面（櫃買中心 → 上櫃 → 三大法人 → 買賣金額彙總）"
                  "同日數字核對，確認單位與口徑")


def main():
    global HTML_TEMPLATE
    HTML_TEMPLATE = _TEMPLATE

    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="產生模擬資料 HTML 預覽")
    ap.add_argument("--date", help="指定單一日期 YYYYMMDD（回補一天）")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"),
                    help="區間回補 YYYYMMDD YYYYMMDD（建議本機執行）")
    ap.add_argument("--inspect", metavar="YYYYMMDD",
                    help="傾印期交所原始 CSV 表頭，用於校對欄位")
    ap.add_argument("--inspect-tpex", metavar="YYYYMMDD",
                    help="傾印 TPEx 三大法人彙總原始回應，用於 Phase 0 端點驗證")
    ap.add_argument("--rebuild", action="store_true",
                    help="不抓網路，重算現有 history.json 的跨日欄位並重產 HTML")
    args = ap.parse_args()

    if args.mock:
        log("模擬模式：產生 UI 預覽")
        render_html(build_mock_history())
        return

    if args.rebuild:
        log("重算模式：以現有 history.json 重算 Δ／E_Total／Z-Score")
        hist = load_history()
        if not hist["history"]:
            log("history.json 無資料，結束")
            return
        recompute_derived(hist["history"])
        save_history(hist)
        render_html(hist)
        log(f"重算完成，共 {len(hist['history'])} 筆")
        return

    if args.inspect:
        run_inspect(dt.datetime.strptime(args.inspect, "%Y%m%d").date())
        return

    if args.inspect_tpex:
        run_inspect_tpex(dt.datetime.strptime(args.inspect_tpex, "%Y%m%d").date())
        return

    if args.backfill:
        run_backfill(args.backfill[0], args.backfill[1])
        return

    today = dt.datetime.now(TPE_TZ).date()
    if args.date:
        today = dt.datetime.strptime(args.date, "%Y%m%d").date()
    run_single(today)


# ===================================================================
#  ECharts 暗色系儀表板模板
# ===================================================================
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>外資籌碼總體監控</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  :root{
    --bg:#0b0e14; --panel:#141925; --panel2:#1b2130; --line:#222a3a;
    --txt:#e6e9f0; --sub:#8b93a7; --up:#ff4d4f; --down:#52c41a; --accent:#40a9ff;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);
       font-family:"PingFang TC","Microsoft JhengHei","Noto Sans TC",-apple-system,sans-serif;
       padding:16px 12px 48px;line-height:1.5}
  .wrap{max-width:1120px;margin:0 auto}
  header{display:flex;align-items:baseline;justify-content:space-between;
         flex-wrap:wrap;gap:8px;margin-bottom:14px}
  h1{font-size:19px;letter-spacing:.5px}
  .meta{color:var(--sub);font-size:12px}
  .sig{display:flex;align-items:center;gap:12px;background:var(--panel);
       border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:14px}
  .dot{width:14px;height:14px;border-radius:50%;flex:none;
       box-shadow:0 0 12px 2px currentColor}
  .sig b{font-size:16px}
  .sig span{color:var(--sub);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px}
  .card .k{color:var(--sub);font-size:12px;margin-bottom:6px}
  .card .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
  .card .u{color:var(--sub);font-size:11px;margin-left:4px}
  .pos{color:var(--up)} .neg{color:var(--down)}
  .panel{background:var(--panel);border:1px solid var(--line);
         border-radius:14px;padding:14px 12px 6px;margin-bottom:14px}
  .ptitle{display:flex;align-items:center;gap:8px;font-size:14px;margin:2px 4px 8px}
  .ptitle .tag{font-size:11px;color:var(--accent);border:1px solid var(--accent);
               border-radius:6px;padding:1px 6px;opacity:.85}
  .ptitle .hint{color:var(--sub);font-size:11px;font-weight:400;margin-left:auto}
  .chart{width:100%;height:300px}
  footer{color:var(--sub);font-size:11px;text-align:center;margin-top:18px;line-height:1.8}
  /* ── 轉折確認訊號燈面板 ── */
  .rv-panel{background:var(--panel);border:1px solid var(--line);
            border-radius:14px;padding:14px 14px 12px;margin-bottom:14px}
  .rv-head{display:flex;align-items:center;gap:8px;margin:0 2px 12px;flex-wrap:wrap}
  .rv-tag{font-size:11px;color:var(--accent);border:1px solid var(--accent);
          border-radius:6px;padding:1px 6px;opacity:.85}
  .rv-title{font-size:14px}
  .rv-hint{color:var(--sub);font-size:11px;margin-left:auto}
  .rv-main{display:flex;align-items:center;gap:14px;background:var(--panel2);
           border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
  .rv-bigdot{width:22px;height:22px;border-radius:50%;flex:none}
  .rv-maintext{display:flex;flex-direction:column;gap:3px}
  .rv-maintext b{font-size:17px}
  .rv-blurb{color:var(--sub);font-size:12px}
  .rv-score{margin-left:auto;text-align:right}
  .rv-scoreval{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
  .rv-scorelab{color:var(--sub);font-size:11px}
  .rv-row{display:flex;align-items:center;gap:10px;padding:9px 4px;flex-wrap:wrap;
          border-top:1px solid var(--line)}
  .rv-row:first-of-type{border-top:none}
  .rv-rowlab{color:var(--sub);font-size:12px;min-width:104px}
  .rv-lamps{display:flex;gap:14px;flex-wrap:wrap;flex:1}
  .rv-lamp{display:flex;align-items:center;gap:6px;cursor:default}
  .rv-dot{width:11px;height:11px;border-radius:50%;flex:none}
  .rv-lab{font-size:12px;color:var(--txt)}
  .rv-rowtail{color:var(--sub);font-size:12px;font-variant-numeric:tabular-nums;
              margin-left:auto}
  .rv-stock{background:var(--panel2);border:1px solid var(--accent);
            border-radius:10px;margin:4px 0;padding:9px 10px}
  .rv-stock.rv-row{border-top:none}
  .rv-gate{color:var(--sub)}
  .rv-foot{color:var(--sub);font-size:11px;margin-top:8px;text-align:right}
  .rv-err{color:var(--up);font-size:12px;padding:6px 4px}
  @media(max-width:560px){
    .cards{grid-template-columns:repeat(2,1fr)}
    .chart{height:260px}
    h1{font-size:17px}
    .rv-rowlab{min-width:100%}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🛰️ 外資籌碼總體監控</h1>
    <div class="meta">資料日 __DATE__ ・ 更新 __UPDATED__</div>
  </header>

  <div class="sig">
    <div class="dot" style="color:__SIG_COLOR__"></div>
    <div><b style="color:__SIG_COLOR__">__SIG_TEXT__</b><br><span>__SIG_DESC__</span></div>
  </div>

  <div class="cards">
    <div class="card"><div class="k">今日總體淨曝險 E<sub>Total</sub></div>
      <div class="v" id="cv-etotal">__C_ETOTAL__<span class="u">億</span></div></div>
    <div class="card"><div class="k">現貨買賣超</div>
      <div class="v" id="cv-spot">__C_SPOT__<span class="u">億</span></div></div>
    <div class="card"><div class="k">金額比 Z-Score</div>
      <div class="v" id="cv-ratioz">__C_RATIOZ__<span class="u">σ</span></div></div>
    <div class="card"><div class="k">Put/Call 金額比</div>
      <div class="v">__C_RATIO__</div></div>
    <div class="card"><div class="k">買權平均單價</div>
      <div class="v">__C_PCALL__<span class="u">點</span></div></div>
    <div class="card"><div class="k">賣權平均單價</div>
      <div class="v">__C_PPUT__<span class="u">點</span></div></div>
    <div class="card"><div class="k">上櫃外資買賣超</div>
      <div class="v" id="cv-tpex">__C_TPEX__<span class="u">億</span></div></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組一</span>總體淨曝險（現貨＋期貨變化）
      <span class="hint">單位：億元　正＝偏多</span></div>
    <div id="c1" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">籌碼背離</span>E_Total 累積　vs　加權指數
      <span class="hint">左軸：加權指數 點　右軸：E_Total 累積 億</span></div>
    <div id="c1b" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">Context</span>上櫃外資買賣超（中小型股風險偏好）
      <span class="hint">bar：每日買賣超　線：20日累積　單位：億元</span></div>
    <div id="c1t" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組二A</span>選擇權平均單價 — 部位意圖
      <span class="hint">賣權左軸·買權右軸　＞150 價內　＜20 價外避險</span></div>
    <div id="c2" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組二B</span>極端情緒：Put/Call 金額比 Z-Score
      <span class="hint">±2σ 外＝極端區</span></div>
    <div id="c3" class="chart"></div>
  </div>

  <!-- 轉折確認訊號燈（後端 signals/reversal_signals.py → data/reversal_signals.json） -->
  <div id="reversal-panel" class="rv-panel"></div>

  <footer>
    資料來源：TWSE 證交所、TAIFEX 期交所、TPEx 櫃買中心（官方公開資料，每日約 15:00 後更新）<br>
    本儀表板僅供研究參考，不構成投資建議。
  </footer>
</div>

<script>
const D = __DATA__;
const AX = '#222a3a', SUB = '#8b93a7', TXT = '#e6e9f0';
const baseGrid = {left:54, right:18, top:30, bottom:54};
const dataZoom = [
  {type:'inside', start:55, end:100},
  {type:'slider', start:55, end:100, height:16, bottom:6,
   borderColor:AX, fillerColor:'rgba(64,169,255,.15)',
   textStyle:{color:SUB,fontSize:10}}
];
const axisCommon = {
  axisLine:{lineStyle:{color:AX}}, axisLabel:{color:SUB,fontSize:11},
  splitLine:{lineStyle:{color:AX,type:'dashed',opacity:.45}}
};
function tip(extra){return Object.assign(
  {trigger:'axis', backgroundColor:'#1b2130', borderColor:AX,
   textStyle:{color:TXT,fontSize:12}, axisPointer:{lineStyle:{color:SUB}}}, extra||{});}

// ── 模組一：E_Total 長條 + 堆疊組成（e_total_broad 為含上櫃對照線，預設關閉）──
echarts.init(document.getElementById('c1'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
    data:['現貨','大台Δ','股期Δ','E_Total','E_Total 廣義(含上櫃)'],
    selected:{'E_Total 廣義(含上櫃)':false}},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:{type:'value', name:'億', nameTextStyle:{color:SUB}, ...axisCommon},
  series:[
    {name:'現貨', type:'bar', stack:'comp', data:D.spot, itemStyle:{color:'#3b6fb0'}},
    {name:'大台Δ', type:'bar', stack:'comp', data:D.delta_tx, itemStyle:{color:'#8e54c9'}},
    {name:'股期Δ', type:'bar', stack:'comp', data:D.delta_ssf, itemStyle:{color:'#c98e54'}},
    {name:'E_Total', type:'line', data:D.e_total, smooth:true, symbol:'none',
     lineStyle:{width:2,color:'#40a9ff'}, z:5},
    {name:'E_Total 廣義(含上櫃)', type:'line', data:D.e_total_broad, smooth:true,
     symbol:'none', lineStyle:{width:1.4,color:'#7f9bd1',opacity:.55}, z:4}
  ]
});

// ── Context：上櫃外資買賣超 bar + 20 日累積線 ──
echarts.init(document.getElementById('c1t'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
    data:['上櫃買賣超','20日累積']},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:[
    {type:'value', name:'億', nameTextStyle:{color:SUB}, ...axisCommon},
    {type:'value', name:'累積·億', position:'right', nameTextStyle:{color:SUB},
     axisLine:{lineStyle:{color:AX}}, axisLabel:{color:SUB,fontSize:11}, splitLine:{show:false}}
  ],
  series:[
    {name:'上櫃買賣超', type:'bar', data:D.spot_tpex, itemStyle:{color:'#36cfc9'}},
    {name:'20日累積', type:'line', yAxisIndex:1, data:D.spot_tpex_cum20, smooth:true,
     symbol:'none', connectNulls:true, lineStyle:{width:2,color:'#ffd666'}}
  ]
});

// ── 籌碼背離：E_Total 累積(右軸) vs 加權指數(左軸) ──
(function(){
  let acc=0; const cum=D.e_total.map(v=>{acc+=(v||0);return Math.round(acc);});
  echarts.init(document.getElementById('c1b'),'dark',{renderer:'canvas'}).setOption({
    backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
      data:['加權指數','E_Total 累積']},
    grid:baseGrid, dataZoom:dataZoom,
    xAxis:{type:'category', data:D.dates, ...axisCommon},
    yAxis:[
      {type:'value', name:'加權指數', scale:true, nameTextStyle:{color:SUB}, ...axisCommon},
      {type:'value', name:'累積·億', position:'right', nameTextStyle:{color:SUB},
       axisLine:{lineStyle:{color:AX}}, axisLabel:{color:SUB,fontSize:11}, splitLine:{show:false}}
    ],
    series:[
      {name:'加權指數', type:'line', data:D.taiex, smooth:true, symbol:'none',
       connectNulls:true, lineStyle:{width:2.2,color:'#e6e9f0'}},
      {name:'E_Total 累積', type:'line', yAxisIndex:1, data:cum, smooth:true, symbol:'none',
       lineStyle:{width:2.6,color:'#ffd666'}, areaStyle:{color:'rgba(255,214,102,.07)'}}
    ]
  });
})();

// ── 模組二A：買權(右軸)/賣權(左軸) 平均單價，門檻線放賣權軸 ──
echarts.init(document.getElementById('c2'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
    data:['賣權平均單價(左)','買權平均單價(右)']},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:[
    {type:'value', name:'賣權·點', nameTextStyle:{color:SUB}, ...axisCommon},
    {type:'value', name:'買權·點', position:'right', nameTextStyle:{color:SUB},
     axisLine:{lineStyle:{color:AX}}, axisLabel:{color:SUB,fontSize:11}, splitLine:{show:false}}
  ],
  series:[
    {name:'賣權平均單價(左)', type:'line', data:D.p_put, smooth:true, symbol:'none',
     connectNulls:true, lineStyle:{width:2,color:'#ff4d4f'},
     markLine:{symbol:'none', label:{color:SUB,fontSize:10,position:'insideEndTop'},
       lineStyle:{type:'dashed'}, data:[
         {yAxis:D.thr_dir, name:'價內', lineStyle:{color:'#ffa940'}},
         {yAxis:D.thr_otm, name:'價外', lineStyle:{color:'#8b93a7'}}]}},
    {name:'買權平均單價(右)', type:'line', yAxisIndex:1, data:D.p_call, smooth:true,
     symbol:'none', connectNulls:true, lineStyle:{width:2,color:'#52c41a'}}
  ]
});

// ── 模組二B：金額比 Z-Score + ±2σ 區帶 ──
echarts.init(document.getElementById('c3'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent',
  tooltip:tip({formatter:function(p){
    let s=p[0].axisValue+'<br/>';
    p.forEach(x=>{if(x.seriesName==='Z-Score'||x.seriesName==='金額比')
      s+=x.marker+x.seriesName+'：<b>'+(x.value==null?'—':x.value)+'</b><br/>';});
    return s;}}),
  legend:{top:0,textStyle:{color:SUB}, data:['Z-Score','金額比']},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:[
    {type:'value', name:'Z', nameTextStyle:{color:SUB}, ...axisCommon},
    {type:'value', name:'金額比', position:'right', nameTextStyle:{color:SUB},
     axisLine:{lineStyle:{color:AX}}, axisLabel:{color:SUB,fontSize:11}, splitLine:{show:false}}
  ],
  series:[
    {name:'Z-Score', type:'line', data:D.ratio_z, smooth:true, symbol:'none',
     connectNulls:true, lineStyle:{width:2,color:'#40a9ff'},
     areaStyle:{color:'rgba(64,169,255,.08)'},
     markLine:{symbol:'none', label:{color:SUB,fontSize:10},
       lineStyle:{type:'dashed'}, data:[
         {yAxis:2, name:'+2σ 恐慌', lineStyle:{color:'#ff4d4f'}},
         {yAxis:-2, name:'-2σ 樂觀', lineStyle:{color:'#52c41a'}},
         {yAxis:0, lineStyle:{color:'#8b93a7',opacity:.4}}]}},
    {name:'金額比', type:'line', yAxisIndex:1, data:D.ratio, smooth:true, symbol:'none',
     connectNulls:true, lineStyle:{width:1.4,color:'#ffa940',opacity:.7}}
  ]
});

// 卡片漲跌上色
(function(){
  [['cv-etotal'],['cv-spot'],['cv-ratioz'],['cv-tpex']].forEach(([id])=>{
    const el=document.getElementById(id); if(!el)return;
    const v=parseFloat(el.textContent);
    if(!isNaN(v)) el.classList.add(v>=0?'pos':'neg');
  });
})();
window.addEventListener('resize',()=>{
  document.querySelectorAll('.chart').forEach(c=>{
    const i=echarts.getInstanceByDom(c); if(i)i.resize();});
});
</script>
<script src="reversal_panel.js"></script>
</body>
</html>"""


if __name__ == "__main__":
    main()
