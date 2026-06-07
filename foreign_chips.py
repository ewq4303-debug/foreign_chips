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
    """解析三大法人-選擇權買賣權分計 CSV，回傳買權/賣權 外資未平倉(口數,金額元)"""
    reader = csv.reader(StringIO(text))
    rows = [r for r in reader if len(r) > 3]
    if not rows:
        return None
    header = rows[0]
    c_prod = find_col(header, "商品")
    c_cp = find_col(header, "買賣權")
    c_role = find_col(header, "身分別") if find_col(header, "身分別") >= 0 \
        else find_col(header, "身份別")
    c_oi = find_col(header, "未平倉", "口數")
    c_amt = find_col(header, "未平倉", "金額")
    if c_oi < 0:
        c_oi = find_col(header, "未平倉")
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


def compute_record(date_iso, spot, tx, ssf, opt, history):
    """組裝單日完整指標。tx/ssf 為當日未平倉金額；Δ 需與前一日相比。"""
    prev = history[-1] if history else {}
    rec = {
        "date": date_iso,
        "spot_net": spot,                 # 現貨買賣超（元）
        "tx_oi_amount": tx,               # 大台未平倉淨額金額（元）
        "ssf_oi_amount": ssf,             # 股票期貨未平倉淨額金額（元）
    }

    # ── 模組一：總體淨曝險變化 ──
    d_tx = (tx - prev["tx_oi_amount"]) if (tx is not None and prev.get("tx_oi_amount") is not None) else 0.0
    d_ssf = (ssf - prev["ssf_oi_amount"]) if (ssf is not None and prev.get("ssf_oi_amount") is not None) else 0.0
    rec["delta_tx"] = d_tx
    rec["delta_ssf"] = d_ssf
    rec["e_total"] = (spot or 0.0) + d_tx + d_ssf

    # ── 模組二：選擇權 ──
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

    # ── Z-Score（金額比）──
    ar_series = [r.get("amount_ratio") for r in history]
    rec["amount_ratio_z"] = (zscore(ar_series, rec["amount_ratio"])
                             if rec["amount_ratio"] is not None else None)
    return rec


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
    base = dt.date(2026, 2, 17)
    d = base
    for i in range(n):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        spot = random.gauss(0, 1) * 1.2e10
        tx += random.gauss(0, 1) * 6e9
        ssf += random.gauss(0, 1) * 2e9
        call_oi = random.uniform(2.0e5, 3.2e5)
        put_oi = random.uniform(2.2e5, 4.0e5)
        p_call = random.uniform(15, 110)
        p_put = random.uniform(18, 180)
        call_amt = call_oi * p_call * TXO_MULTIPLIER
        put_amt = put_oi * p_put * TXO_MULTIPLIER
        # 製造一段情緒飆升
        if 55 <= i <= 63:
            put_amt *= 1.6
        opt = {"call": [call_oi, call_amt], "put": [put_oi, put_amt]}
        rec = compute_record(d.isoformat(), spot, tx, ssf, opt, hist)
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
    history = hist["history"]
    if not history:
        raise SystemExit("無資料可產生 HTML")
    latest = history[-1]
    updated = dt.datetime.now(TPE_TZ).strftime("%Y-%m-%d %H:%M")
    sig_color, sig_text, sig_desc = signal_for(latest)

    def col(key, scale=1.0):
        return [round((r.get(key) or 0) / scale, 2) for r in history]

    payload = {
        "dates": [r["date"] for r in history],
        "e_total": col("e_total", 1e8),          # 億元
        "spot": col("spot_net", 1e8),
        "delta_tx": col("delta_tx", 1e8),
        "delta_ssf": col("delta_ssf", 1e8),
        "p_call": [round(r.get("p_avg_call"), 1) if r.get("p_avg_call") else None for r in history],
        "p_put": [round(r.get("p_avg_put"), 1) if r.get("p_avg_put") else None for r in history],
        "ratio": [round(r.get("amount_ratio"), 3) if r.get("amount_ratio") else None for r in history],
        "ratio_z": [round(r.get("amount_ratio_z"), 2) if r.get("amount_ratio_z") is not None else None for r in history],
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
    """抓取單一日期的三來源原始值，回傳 (spot, tx, ssf, opt)"""
    ymd = date_obj.strftime("%Y%m%d")
    slash = date_obj.strftime("%Y/%m/%d")
    spot = fetch_spot_net(ymd)
    tx, ssf = fetch_futures(slash)
    opt = fetch_options(slash)
    return spot, tx, ssf, opt


def run_single(date_obj):
    """抓一天、更新歷史、重產 HTML"""
    iso = date_obj.isoformat()
    log(f"開始抓取 {iso}")
    spot, tx, ssf, opt = fetch_one_day(date_obj)
    log(f"  現貨買賣超：{spot}")
    log(f"  大台未平倉金額：{tx}　股票期貨：{ssf}")
    log(f"  選擇權：{opt}")
    if spot is None and tx is None and opt is None:
        log("三個來源皆無資料，視為非交易日，結束（不更新）")
        return
    hist = load_history()
    rec = compute_record(iso, spot, tx, ssf, opt, hist["history"])
    hist["history"] = upsert(hist["history"], rec)
    save_history(hist)
    log(f"  E_Total={rec['e_total']/1e8:+.2f} 億　金額比={rec.get('amount_ratio')}")
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
        spot, tx, ssf, opt = fetch_one_day(d)
        if spot is None and tx is None and opt is None:
            log(f"  {d} 無資料（假日/未公布），略過")
            skip += 1
        else:
            rec = compute_record(d.isoformat(), spot, tx, ssf, opt, hist["history"])
            hist["history"] = upsert(hist["history"], rec)
            ok += 1
            log(f"  {d}　E_Total={rec['e_total']/1e8:+.1f}億"
                f"　金額比={rec.get('amount_ratio')}　共 {len(hist['history'])} 筆")
        save_history(hist)                   # 每天即時存檔，中斷可續跑
        time.sleep(1.0)                      # 對伺服器友善
        d += dt.timedelta(days=1)
    render_html(hist)
    log(f"回補完成：成功 {ok} 天、略過 {skip} 天，歷史共 {len(hist['history'])} 筆")


def run_inspect(date_obj):
    """傾印 TAIFEX 原始 CSV 表頭與前幾列，用來校對欄位比對關鍵字（除錯用）"""
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
    args = ap.parse_args()

    if args.mock:
        log("模擬模式：產生 UI 預覽")
        render_html(build_mock_history())
        return

    if args.inspect:
        run_inspect(dt.datetime.strptime(args.inspect, "%Y%m%d").date())
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
  @media(max-width:560px){
    .cards{grid-template-columns:repeat(2,1fr)}
    .chart{height:260px}
    h1{font-size:17px}
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
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組一</span>總體淨曝險（現貨＋期貨變化）
      <span class="hint">單位：億元　正＝偏多</span></div>
    <div id="c1" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組二A</span>選擇權平均單價 — 部位意圖
      <span class="hint">＞150 價內攻擊　＜20 價外避險</span></div>
    <div id="c2" class="chart"></div>
  </div>

  <div class="panel">
    <div class="ptitle"><span class="tag">模組二B</span>極端情緒：Put/Call 金額比 Z-Score
      <span class="hint">±2σ 外＝極端區</span></div>
    <div id="c3" class="chart"></div>
  </div>

  <footer>
    資料來源：TWSE 證交所、TAIFEX 期交所（官方公開資料，每日約 15:00 後更新）<br>
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

// ── 模組一：E_Total 長條 + 堆疊組成 ──
echarts.init(document.getElementById('c1'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
    data:['現貨','大台Δ','股期Δ','E_Total']},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:{type:'value', name:'億', nameTextStyle:{color:SUB}, ...axisCommon},
  series:[
    {name:'現貨', type:'bar', stack:'comp', data:D.spot, itemStyle:{color:'#3b6fb0'}},
    {name:'大台Δ', type:'bar', stack:'comp', data:D.delta_tx, itemStyle:{color:'#8e54c9'}},
    {name:'股期Δ', type:'bar', stack:'comp', data:D.delta_ssf, itemStyle:{color:'#c98e54'}},
    {name:'E_Total', type:'line', data:D.e_total, smooth:true, symbol:'none',
     lineStyle:{width:2,color:'#40a9ff'}, z:5}
  ]
});

// ── 模組二A：買權/賣權平均單價 + 門檻線 ──
echarts.init(document.getElementById('c2'),'dark',{renderer:'canvas'}).setOption({
  backgroundColor:'transparent', tooltip:tip(), legend:{top:0,textStyle:{color:SUB},
    data:['買權平均單價','賣權平均單價']},
  grid:baseGrid, dataZoom:dataZoom,
  xAxis:{type:'category', data:D.dates, ...axisCommon},
  yAxis:{type:'value', name:'點', nameTextStyle:{color:SUB}, ...axisCommon},
  series:[
    {name:'買權平均單價', type:'line', data:D.p_call, smooth:true, symbol:'none',
     connectNulls:true, lineStyle:{width:2,color:'#52c41a'}},
    {name:'賣權平均單價', type:'line', data:D.p_put, smooth:true, symbol:'none',
     connectNulls:true, lineStyle:{width:2,color:'#ff4d4f'},
     markLine:{symbol:'none', label:{color:SUB,fontSize:10,position:'insideEndTop'},
       lineStyle:{type:'dashed'}, data:[
         {yAxis:D.thr_dir, name:'價內', lineStyle:{color:'#ffa940'}},
         {yAxis:D.thr_otm, name:'價外', lineStyle:{color:'#8b93a7'}}]}}
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
  [['cv-etotal'],['cv-spot'],['cv-ratioz']].forEach(([id])=>{
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
</body>
</html>"""


if __name__ == "__main__":
    main()
