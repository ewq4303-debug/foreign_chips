#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外資籌碼「轉折確認」訊號燈模組  signals/reversal_signals.py
============================================================
在既有「外資籌碼總體監控」儀表板尾端，新增一個**確定性、可回放、門檻可調**
的燈號，把「恐慌會持續還是開始轉折」從人工讀圖變成可量化的 verdict：

  🔴 PANIC_CONTINUES    恐慌持續
  🟡 BOUNCE_BREWING     醞釀反彈（結構未轉）
  🟢 REVERSAL_CONFIRMED 確認轉折
  ⚪ NEUTRAL            中性／觀察

核心規則：真正的「轉折」必須 **流量(flow) + 存量(stock) 同時成立**。
流量分數夠但存量（累積 E_Total 守門員 S4）沒翻 → 最高只能到 🟡。

設計原則
  * 本模組不重抓資料，只讀既有 pipeline 已算好的 data/history.json。
  * 所有門檻只走環境變數（見 §7 / CFG），嚴禁 hard-code，方便日後在
    Actions 直接調參回測。
  * 序列一律以交易日對齊、由舊到新排序、同長度；缺值前向填補。

執行方式
  python -m signals.reversal_signals            # 讀 history.json，輸出 JSON
  python -m signals.reversal_signals --selftest # 不寫檔，跑 §12 反向自測
"""

import os
import sys
import json
import datetime as dt

# ── 路徑（相對於 repo 根目錄）──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(ROOT, "data", "history.json")
OUTPUT_FILE = os.path.join(ROOT, "docs", "data", "reversal_signals.json")

TPE_TZ = dt.timezone(dt.timedelta(hours=8))     # 台北時區


# ───────────────────────────────────────────────────────────────────
#  §7 參數表（全部走環境變數，附預設）
# ───────────────────────────────────────────────────────────────────
def env_f(k, d):
    return float(os.getenv(k, d))


def env_i(k, d):
    return int(os.getenv(k, d))


CFG = {
    "Z_EXTREME":     env_f("REV_Z_EXTREME", 2.0),     # 極端區門檻（σ）
    "Z_EXIT":        env_f("REV_Z_EXIT", 2.0),        # 退出極端區門檻
    "PEAK_WIN":      env_i("REV_PEAK_WIN", 10),       # 找近期高/低點視窗
    "SLOPE_LAG":     env_i("REV_SLOPE_LAG", 2),       # 斜率回看天數
    "SPREAD_STREAK": env_i("REV_SPREAD_STREAK", 2),   # 價差連跌天數門檻
    "SHRINK_WIN":    env_i("REV_SHRINK_WIN", 5),      # 現貨急縮比較視窗
    "SHRINK_FACTOR": env_f("REV_SHRINK_FACTOR", 2.0), # 賣超縮小倍數門檻
    "CUM_SLOPE_WIN": env_i("REV_CUM_SLOPE_WIN", 5),   # 累積 E_Total 斜率視窗 ⭐
    "CUM_FLAT_EPS":  env_f("REV_CUM_FLAT_EPS", 5.0),  # 視為走平的斜率絕對值（億）
    "ETOTAL_WARN":   env_f("REV_ETOTAL_WARN", -500.0),# 當日 E_Total 翻空警告線（億）
    "FLOW_MIN":      env_i("REV_FLOW_MIN", 2),        # 升級轉折所需最低流量分數
    "MIN_SAMPLE":    env_i("REV_MIN_SAMPLE", 20),     # 最低有效樣本數
    "TPEX_ALIGN_N":  env_i("REV_TPEX_ALIGN_N", 2),    # C1 上櫃同步淨買的回看天數
    "TPEX_MIN_SAMPLE": env_i("REV_TPEX_MIN_SAMPLE", 20),  # C1 生效所需 spot_net_tpex 最低有效樣本數
}


# ───────────────────────────────────────────────────────────────────
#  §4 計算工具函式
# ───────────────────────────────────────────────────────────────────
def slope(series, lag):
    """series[-1] - series[-1-lag]；不足長度或含 None 回 None。"""
    if series is None or len(series) < lag + 1:
        return None
    a, b = series[-1], series[-1 - lag]
    if a is None or b is None:
        return None
    return a - b


def peak(series, win):
    """近 win 日最大值（忽略 None）；無有效值回 None。"""
    sample = [x for x in series[-win:] if x is not None]
    return max(sample) if sample else None


def trough(series, win):
    """近 win 日最小值（忽略 None）；無有效值回 None。"""
    sample = [x for x in series[-win:] if x is not None]
    return min(sample) if sample else None


def declining_streak(series):
    """從最新往回連續下降（嚴格遞減）的天數。"""
    streak = 0
    for i in range(len(series) - 1, 0, -1):
        cur, prev = series[i], series[i - 1]
        if cur is None or prev is None:
            break
        if cur < prev:
            streak += 1
        else:
            break
    return streak


def ffill(series):
    """前向填補 None：以「最後一筆有效值」往後補。開頭的 None 維持 None。"""
    out = []
    last = None
    for x in series:
        if x is not None:
            last = x
        out.append(last)
    return out


def mean(values):
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


# ───────────────────────────────────────────────────────────────────
#  §3 輸入資料契約 — adapter
# ───────────────────────────────────────────────────────────────────
#  把 history.json 的實際欄位名稱對應到 SPEC §3 的標準鍵：
#    dates        ← date
#    zscore       ← amount_ratio_z      （模組二B 藍線）
#    pc_ratio     ← amount_ratio        （模組二B / Put/Call 金額比）
#    put_price    ← p_avg_put           （模組二A 紅線）
#    call_price   ← p_avg_call          （模組二A 綠線）
#    cum_etotal   ← Σ e_total（億）       （籌碼背離 黃線；逐日累積）
#    etotal_daily ← e_total（億）         （模組一 藍線）
#    spot_net     ← spot_net（億）        （模組一 現貨 bar）
#    spot_net_tpex← spot_net_tpex（億）    （Context 面板 bar；缺值保留 None，
#                                          舊資料無此 key → 整列 None）
#  單位：cum_etotal / etotal_daily / spot_net / spot_net_tpex 皆換算為「億」
#  (÷1e8)，與 CUM_FLAT_EPS、ETOTAL_WARN 同單位。
YI = 1e8  # 億


def load_series(history=None):
    """讀既有 history.json，套 adapter，回傳 §3 標準鍵的 dict。
    序列由舊到新、同長度。缺值以 None 佔位（計算時再 ffill）。"""
    if history is None:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f).get("history", [])
    rows = sorted(history, key=lambda r: r["date"])

    def g(r, k, scale=1.0):
        v = r.get(k)
        return (v / scale) if v is not None else None

    cum, acc = [], 0.0
    for r in rows:
        acc += (r.get("e_total") or 0.0) / YI
        # 只有當該日確有 e_total 才算有效累積點；全程仍逐日累加
        cum.append(round(acc, 4) if r.get("e_total") is not None else None)

    return {
        "dates":        [r["date"] for r in rows],
        "zscore":       [g(r, "amount_ratio_z") for r in rows],
        "pc_ratio":     [g(r, "amount_ratio") for r in rows],
        "put_price":    [g(r, "p_avg_put") for r in rows],
        "call_price":   [g(r, "p_avg_call") for r in rows],
        "cum_etotal":   cum,
        "etotal_daily": [g(r, "e_total", YI) for r in rows],
        "spot_net":     [g(r, "spot_net", YI) for r in rows],
        "spot_net_tpex": [g(r, "spot_net_tpex", YI) for r in rows],
    }


# ───────────────────────────────────────────────────────────────────
#  §5 訊號定義 + §6 綜合燈號邏輯
# ───────────────────────────────────────────────────────────────────
VERDICT_META = {
    "PANIC_CONTINUES":    ("恐慌持續", "red"),
    "BOUNCE_BREWING":     ("醞釀反彈", "yellow"),
    "REVERSAL_CONFIRMED": ("確認轉折", "green"),
    "NEUTRAL":            ("中性／觀察", "grey"),
}


def _sig(triggered, value, threshold, bucket, detail):
    return {
        "triggered": bool(triggered),
        "value": value,
        "threshold": threshold,
        "bucket": bucket,
        "detail": detail,
    }


def compute(series, cfg):
    """實作 §4~§6，回傳 §8 schema。"""
    C = cfg
    dates = series.get("dates", [])
    aligned_n = min((len(series[k]) for k in (
        "zscore", "pc_ratio", "put_price", "call_price",
        "cum_etotal", "etotal_daily", "spot_net")), default=0)
    aligned_n = min(aligned_n, len(dates))

    # 前向填補各序列（保留原始 dates）
    z = ffill(series["zscore"])
    pc = ffill(series["pc_ratio"])
    put = ffill(series["put_price"])
    call = ffill(series["call_price"])
    cum = ffill(series["cum_etotal"])
    etd = ffill(series["etotal_daily"])
    spot = ffill(series["spot_net"])

    # 有效樣本：以 zscore 與 cum_etotal 皆有值的天數為準
    valid_n = sum(1 for i in range(aligned_n)
                  if z[i] is not None and cum[i] is not None)
    insufficient = aligned_n == 0 or valid_n < C["MIN_SAMPLE"]

    data_date = dates[-1] if dates else None

    if insufficient:
        verdict = _make_verdict("NEUTRAL", 0, 4, False, False)
        return _envelope(data_date, True, verdict,
                         _empty_signals(), _empty_warnings(), C, aligned_n)

    # ── 轉折側 flow / stock ──────────────────────────────────────────
    # S1 — Z-Score 退出極端區（flow）
    z_today = z[-1]
    z_peak = peak(z, C["PEAK_WIN"])
    z_slope = slope(z, C["SLOPE_LAG"])
    S1 = bool(z_peak is not None and z_today is not None and z_slope is not None
              and z_peak >= C["Z_EXTREME"] and z_today < C["Z_EXIT"] and z_slope < 0)
    s1 = _sig(S1, _r(z_today), C["Z_EXIT"], "flow",
              _detail_s1(S1, z_today, z_peak, z_slope, C))

    # S2 — Put/Call 反折 + 賣權單價停漲（flow）
    pc_peak = peak(pc, C["PEAK_WIN"])
    pc_slope = slope(pc, C["SLOPE_LAG"])
    put_slope = slope(put, C["SLOPE_LAG"])
    pc_falling = bool(pc[-1] is not None and pc_peak is not None
                      and pc_slope is not None
                      and pc[-1] < pc_peak and pc_slope < 0)
    put_stalled = bool(put_slope is not None and put_slope <= 0)
    S2 = pc_falling and put_stalled
    s2 = _sig(S2, _r(pc[-1]), None, "flow",
              _detail_s2(S2, pc_falling, put_stalled))

    # S3 — 賣權/買權單價交叉回（flow）
    spread = [(put[i] - call[i]) if (put[i] is not None and call[i] is not None)
              else None for i in range(aligned_n)]
    sp_slope = slope(spread, C["SLOPE_LAG"])
    streak = declining_streak(spread)
    crossed = bool(aligned_n >= 2 and spread[-1] is not None
                   and spread[-2] is not None
                   and spread[-2] > 0 >= spread[-1])      # 由正轉負（向下穿越 0）
    S3 = bool((sp_slope is not None and sp_slope < 0
               and streak >= C["SPREAD_STREAK"]) or crossed)
    s3 = _sig(S3, _r(spread[-1]), 0, "flow",
              _detail_s3(S3, spread[-1], streak, crossed, C))

    # S5 — 現貨賣超由賣轉買 / 急縮（flow）
    turned = bool(aligned_n >= 2 and spot[-1] is not None and spot[-2] is not None
                  and spot[-1] > 0 and spot[-2] < 0)
    prev_abs = mean([abs(x) for x in spot[-1 - C["SHRINK_WIN"]:-1]
                     if x is not None])
    shrunk = bool(spot[-1] is not None and spot[-1] < 0
                  and prev_abs not in (None, 0)
                  and abs(spot[-1]) < prev_abs / C["SHRINK_FACTOR"])
    S5 = turned or shrunk
    s5 = _sig(S5, _r(spot[-1]), None, "flow",
              _detail_s5(S5, turned, shrunk, spot[-1]))

    # S4 — 累積 E_Total 翻揚（stock，守門員 ⭐）
    cum_slope = slope(cum, C["CUM_SLOPE_WIN"])
    S4_flat = bool(cum_slope is not None and abs(cum_slope) <= C["CUM_FLAT_EPS"])
    S4_up = bool(cum_slope is not None and cum_slope > C["CUM_FLAT_EPS"])
    S4 = S4_flat or S4_up
    s4 = _sig(S4, _r(cum_slope), 0, "stock",
              _detail_s4(S4, S4_up, S4_flat, cum_slope, C))

    # ── 持續側 warning ───────────────────────────────────────────────
    # W1 — Z-Score 續創新高
    z_peak_prev = peak(z[:-1], C["PEAK_WIN"])
    W1 = bool(z[-1] is not None and z_peak_prev is not None
              and z[-1] >= z_peak_prev and z[-1] >= C["Z_EXTREME"])
    w1 = {"triggered": W1, "value": _r(z[-1]), "threshold": C["Z_EXTREME"],
          "detail": ("Z-Score 逼近/突破期間新高，恐慌續強"
                     if W1 else "Z-Score 未創新高")}

    # W2 — 賣權單價續高 + 累積線續破底（真出貨）
    put_newhigh = bool(put[-1] is not None
                       and (peak(put[:-1], C["PEAK_WIN"]) is not None)
                       and put[-1] >= peak(put[:-1], C["PEAK_WIN"]))
    cum_newlow = bool(cum[-1] is not None
                      and (trough(cum[:-1], C["PEAK_WIN"]) is not None)
                      and cum[-1] <= trough(cum[:-1], C["PEAK_WIN"]))
    W2 = put_newhigh and cum_newlow
    w2 = {"triggered": W2, "value": None, "threshold": None,
          "detail": ("賣權單價創高且累積線續破底 → 真出貨" if W2
                     else "賣權單價/累積線未同步破底")}

    # W3 — 期貨對沖翻空（避險→出貨）
    W3 = bool(etd[-1] is not None and etd[-1] < C["ETOTAL_WARN"])
    w3 = {"triggered": W3, "value": _r(etd[-1]), "threshold": C["ETOTAL_WARN"],
          "detail": ("當日 E_Total 大幅翻空，期貨不再偏多對沖" if W3
                     else "當日 E_Total 仍由期貨偏多對沖")}

    # ── §6 綜合燈號 ──────────────────────────────────────────────────
    flow_score = int(S1) + int(S2) + int(S3) + int(S5)   # 0~4
    stock_ok = S4
    warning_active = W1 or W2 or W3

    if warning_active and not stock_ok:
        code = "PANIC_CONTINUES"           # 🔴 持續警告主導
    elif stock_ok and flow_score >= C["FLOW_MIN"] and not warning_active:
        code = "REVERSAL_CONFIRMED"        # 🟢 流量+存量雙確認
    elif flow_score >= C["FLOW_MIN"] and not stock_ok:
        code = "BOUNCE_BREWING"            # 🟡 短線彈、結構未轉
    elif flow_score == 0 and not stock_ok:
        code = "PANIC_CONTINUES"           # 🔴 完全沒鬆動
    else:
        code = "NEUTRAL"                   # ⚪ 訊號分歧，觀察

    # ── Context 訊號 C1：上櫃外資同步確認（加分不否決）─────────────────
    # 只調整 confidence，不進 flow_score、不影響 code 的判定路徑。
    c1, confidence = _compute_c1(series, code, C)

    verdict = _make_verdict(code, flow_score, 4, stock_ok, warning_active,
                            confidence)
    signals = {
        "S1_zscore_exit": s1,
        "S2_pc_reversal": s2,
        "S3_premium_cross": s3,
        "S5_spot_turn": s5,
        "S4_cum_turnup": s4,
        "C1_tpex_confirm": c1,
    }
    warnings = {
        "W1_zscore_newhigh": w1,
        "W2_real_distribution": w2,
        "W3_futures_flip": w3,
    }
    return _envelope(data_date, False, verdict, signals, warnings, C, aligned_n)


# ── Context 訊號 C1（bucket="context"）───────────────────────────────
def _compute_c1(series, code, C):
    """C1_tpex_confirm：上櫃外資近 TPEX_ALIGN_N 日同步淨買，且當日 verdict 為
    🟡/🟢 時 confidence +1（僅此一途徑）。不足樣本 → insufficient，不扣分。
    向後相容：series 無 spot_net_tpex 鍵（舊資料/舊呼叫端）視同全 None。
    回傳 (signal_dict, confidence)。"""
    n = C["TPEX_ALIGN_N"]
    tpex = series.get("spot_net_tpex") or []
    valid_n = sum(1 for v in tpex if v is not None)

    if valid_n < C["TPEX_MIN_SAMPLE"]:
        c1 = _sig(False, None, 0, "context",
                  f"上櫃資料不足（有效 {valid_n} < {C['TPEX_MIN_SAMPLE']}），"
                  "C1 不參與評分")
        c1["insufficient"] = True
        return c1, 0

    tail = tpex[-n:] if len(tpex) >= n else []
    tail_ok = bool(tail) and all(v is not None for v in tail)
    tail_sum = sum(tail) if tail_ok else None
    in_scope = code in ("BOUNCE_BREWING", "REVERSAL_CONFIRMED")
    C1 = bool(in_scope and tail_ok and tail_sum is not None and tail_sum > 0)

    if C1:
        detail = f"上櫃外資近 {n} 日同步淨買（合計 {_r(tail_sum)} 億），中小型股風險偏好回溫"
    elif not in_scope:
        detail = "verdict 非 🟡/🟢，C1 不評分"
    elif not tail_ok:
        detail = f"上櫃近 {n} 日資料不完整，未確認"
    else:
        detail = f"上櫃外資近 {n} 日仍淨賣（合計 {_r(tail_sum)} 億），未同步確認"

    c1 = _sig(C1, _r(tail_sum), 0, "context", detail)
    c1["insufficient"] = False
    return c1, (1 if C1 else 0)


# ── 組裝小工具 ──────────────────────────────────────────────────────
def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) else v


def _make_verdict(code, flow_score, flow_max, stock_ok, warning_active,
                  confidence=0):
    label, color = VERDICT_META[code]
    return {
        "code": code, "label": label, "color": color,
        "flow_score": flow_score, "flow_max": flow_max,
        "stock_confirmed": bool(stock_ok), "warning_active": bool(warning_active),
        "confidence": int(confidence),     # 基準 0；目前僅 C1 可 +1
    }


def _envelope(data_date, insufficient, verdict, signals, warnings, cfg, aligned_n):
    return {
        "schema": 2,                       # v2：新增 C1_tpex_confirm 與 confidence
        "data_date": data_date,
        "updated_at": None,                # main() 填入
        "insufficient_data": bool(insufficient),
        "aligned_n": aligned_n,
        "verdict": verdict,
        "signals": signals,
        "warnings": warnings,
        "config": {
            "Z_EXTREME": cfg["Z_EXTREME"], "Z_EXIT": cfg["Z_EXIT"],
            "FLOW_MIN": cfg["FLOW_MIN"], "CUM_FLAT_EPS": cfg["CUM_FLAT_EPS"],
            "ETOTAL_WARN": cfg["ETOTAL_WARN"], "MIN_SAMPLE": cfg["MIN_SAMPLE"],
            "TPEX_ALIGN_N": cfg["TPEX_ALIGN_N"],
            "TPEX_MIN_SAMPLE": cfg["TPEX_MIN_SAMPLE"],
        },
    }


def _empty_signals():
    d = "資料不足，無法判讀"
    c1 = _sig(False, None, None, "context", d)
    c1["insufficient"] = True
    return {
        "S1_zscore_exit": _sig(False, None, None, "flow", d),
        "S2_pc_reversal": _sig(False, None, None, "flow", d),
        "S3_premium_cross": _sig(False, None, None, "flow", d),
        "S5_spot_turn": _sig(False, None, None, "flow", d),
        "S4_cum_turnup": _sig(False, None, None, "stock", d),
        "C1_tpex_confirm": c1,
    }


def _empty_warnings():
    d = "資料不足"
    return {
        "W1_zscore_newhigh": {"triggered": False, "value": None,
                              "threshold": None, "detail": d},
        "W2_real_distribution": {"triggered": False, "value": None,
                                 "threshold": None, "detail": d},
        "W3_futures_flip": {"triggered": False, "value": None,
                            "threshold": None, "detail": d},
    }


# ── detail 文字 ────────────────────────────────────────────────────
def _detail_s1(ok, z_today, z_peak, z_slope, C):
    if ok:
        return f"Z={_r(z_today)} 已退出極端區並下行（曾達 {_r(z_peak)}）"
    if z_today is not None and z_today >= C["Z_EXIT"]:
        return f"Z={_r(z_today)} 仍在極端區，未洩壓"
    if z_slope is not None and z_slope >= 0:
        return f"Z={_r(z_today)} 未轉弱（斜率未翻負）"
    return f"Z={_r(z_today)}：尚未滿足退出條件"


def _detail_s2(ok, pc_falling, put_stalled):
    if ok:
        return "Put/Call 反折且賣權單價停漲，避險買盤轉弱"
    if not pc_falling:
        return "Put/Call 仍在高檔，未見反折"
    return "Put/Call 反折但賣權單價仍漲，未確認"


def _detail_s3(ok, sp, streak, crossed, C):
    if crossed:
        return "賣權溢價由正轉負，避險溢價消失"
    if ok:
        return f"賣權-買權價差連跌 {streak} 日，溢價收斂中"
    if sp is not None and sp > 0:
        return f"賣權溢價仍高（{_r(sp)} 點），未收斂"
    return "賣權溢價未明確收斂"


def _detail_s5(ok, turned, shrunk, spot_last):
    if turned:
        return "現貨由賣超轉買超，賣壓鬆動"
    if shrunk:
        return f"現貨賣超急縮（今日 {_r(spot_last)} 億）"
    if spot_last is not None and spot_last < 0:
        return f"現貨大幅賣超（{_r(spot_last)} 億），未轉買亦未急縮"
    return "現貨賣壓未鬆動"


def _detail_s4(ok, up, flat, cum_slope, C):
    if up:
        return f"累積 E_Total 斜率翻揚（{_r(cum_slope)} 億/{C['CUM_SLOPE_WIN']}日），結構轉強"
    if flat:
        return f"累積 E_Total 走平（{_r(cum_slope)} 億），止跌待確認"
    return f"累積 E_Total 仍下行（{_r(cum_slope)} 億，守門員未過）"


# ───────────────────────────────────────────────────────────────────
#  主流程
# ───────────────────────────────────────────────────────────────────
def main():
    series = load_series()
    out = compute(series, CFG)
    out["updated_at"] = dt.datetime.now(TPE_TZ).isoformat()
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    v = out["verdict"]
    print(f"[reversal] {out['data_date']}  verdict={v['code']} "
          f"({v['label']})  flow={v['flow_score']}/{v['flow_max']} "
          f"stock_ok={v['stock_confirmed']}  warning={v['warning_active']}  "
          f"confidence={v['confidence']}")
    print(f"[reversal] 已輸出 → {OUTPUT_FILE}")


# ───────────────────────────────────────────────────────────────────
#  §12 反向自測（不寫檔，造一筆未來假資料驗證守門員邏輯）
# ───────────────────────────────────────────────────────────────────
def _synth(reversal_cum, tpex=None):
    """造一段乾淨的合成序列：先恐慌、尾端流量全面鬆動。
    reversal_cum=True → 累積線翻揚（S4 過）；False → 仍續破底（S4 不過）。
    tpex：None → 不含 spot_net_tpex 鍵（模擬舊呼叫端）；
          "buy"/"sell" → 尾端 TPEX_ALIGN_N 日上櫃同步淨買/續賣；
          "none" → 整列 None（模擬舊 history 資料）。"""
    days = [f"2026-05-{d:02d}" for d in range(1, 26)]   # 25 天，足夠 MIN_SAMPLE
    n = len(days)
    z, pc, put, call, cum, etd, spot = ([] for _ in range(7))
    acc = 0.0
    for i in range(n):
        # 前段恐慌（z 衝 3.0、pc 衝 2.4、put 衝 900、現貨大幅賣超、累積續跌）
        if i < n - 3:
            z.append(2.6 + 0.1 * (i % 3))
            pc.append(2.3 + 0.05 * (i % 2))
            put.append(850.0 + 10 * (i % 4))
            spot.append(-300.0)
            etd.append(-120.0)
            acc += -150.0
        else:                                            # 尾 3 日：流量鬆動
            tail = i - (n - 3)                            # 0,1,2
            z.append([1.8, 1.5, 1.2][tail])              # 退出極端區且下行 → S1
            pc.append([1.8, 1.6, 1.5][tail])             # 反折 → S2
            put.append([500.0, 400.0, 300.0][tail])      # 停漲/回落 → S2、價差收斂 S3
            spot.append([-250.0, -200.0, 200.0][tail])   # 由賣轉買 → S5
            etd.append(120.0)                            # 未破 -500 → W3 false
            acc += (400.0 if reversal_cum else -150.0)   # 翻揚 or 續破底 → S4
        call.append(1300.0)
        cum.append(round(acc, 4))
    out = {"dates": days, "zscore": z, "pc_ratio": pc, "put_price": put,
           "call_price": call, "cum_etotal": cum, "etotal_daily": etd,
           "spot_net": spot}
    if tpex == "none":
        out["spot_net_tpex"] = [None] * n
    elif tpex in ("buy", "sell"):
        align = CFG["TPEX_ALIGN_N"]
        sign = 1.0 if tpex == "buy" else -1.0
        out["spot_net_tpex"] = [(-5.0 if i < n - align else sign * 15.0)
                                for i in range(n)]
    return out


def selftest():
    # 1) 現況自測：以真實 history.json，今日應為 🔴 PANIC_CONTINUES
    base = load_series()
    n = len(base["dates"])
    cur = compute(base, CFG)
    print(f"== 現況自測（{base['dates'][-1] if n else 'N/A'}）==  "
          f"verdict={cur['verdict']['code']}  "
          f"flow={cur['verdict']['flow_score']}  "
          f"stock_ok={cur['verdict']['stock_confirmed']}  "
          f"warning={cur['verdict']['warning_active']}")
    assert cur["verdict"]["code"] == "PANIC_CONTINUES", "今日應為 🔴 PANIC_CONTINUES"

    # 2) 🟢 流量鬆動 + 存量翻揚 + 無 warning → REVERSAL_CONFIRMED
    g = compute(_synth(reversal_cum=True), CFG)
    print(f"== 反向自測 🟢 ==  verdict={g['verdict']['code']}  "
          f"flow={g['verdict']['flow_score']}  "
          f"stock_ok={g['verdict']['stock_confirmed']}  "
          f"warning={g['verdict']['warning_active']}")
    assert g["verdict"]["flow_score"] >= CFG["FLOW_MIN"], "流量分數應 >= FLOW_MIN"
    assert g["verdict"]["stock_confirmed"], "S4 守門員應通過"
    assert g["verdict"]["code"] == "REVERSAL_CONFIRMED", "應為 🟢 REVERSAL_CONFIRMED"

    # 3) 🟡 流量同上、但累積線仍下行 → BOUNCE_BREWING（驗證守門員）
    y = compute(_synth(reversal_cum=False), CFG)
    print(f"== 反向自測 🟡 ==  verdict={y['verdict']['code']}  "
          f"flow={y['verdict']['flow_score']}  "
          f"stock_ok={y['verdict']['stock_confirmed']}  "
          f"warning={y['verdict']['warning_active']}")
    assert y["verdict"]["flow_score"] >= CFG["FLOW_MIN"], "流量分數應 >= FLOW_MIN"
    assert not y["verdict"]["stock_confirmed"], "S4 守門員不應通過"
    assert y["verdict"]["code"] == "BOUNCE_BREWING", "應停在 🟡 BOUNCE_BREWING"

    # ── Context C1 三情境 ───────────────────────────────────────────
    # 4) 🟢 且上櫃同步買 → confidence == 1
    gb = compute(_synth(reversal_cum=True, tpex="buy"), CFG)
    print(f"== C1 自測 🟢+上櫃買 ==  verdict={gb['verdict']['code']}  "
          f"confidence={gb['verdict']['confidence']}  "
          f"C1={gb['signals']['C1_tpex_confirm']['triggered']}")
    assert gb["verdict"]["code"] == "REVERSAL_CONFIRMED", "verdict 不應被 C1 改變"
    assert gb["signals"]["C1_tpex_confirm"]["triggered"], "C1 應觸發"
    assert gb["verdict"]["confidence"] == 1, "🟢+上櫃同步買 → confidence 應為 1"

    # 5) 🟢 但上櫃續賣 → confidence == 0，verdict code 不變
    gs = compute(_synth(reversal_cum=True, tpex="sell"), CFG)
    print(f"== C1 自測 🟢+上櫃賣 ==  verdict={gs['verdict']['code']}  "
          f"confidence={gs['verdict']['confidence']}  "
          f"C1={gs['signals']['C1_tpex_confirm']['triggered']}")
    assert gs["verdict"]["code"] == "REVERSAL_CONFIRMED", "verdict code 不應改變"
    assert not gs["signals"]["C1_tpex_confirm"]["triggered"], "C1 不應觸發"
    assert gs["verdict"]["confidence"] == 0, "上櫃續賣 → confidence 應為 0"

    # 6) spot_net_tpex 全 None（模擬舊資料）→ C1 insufficient，
    #    其餘輸出與「無上櫃資料的改版前行為」逐欄位一致（回歸保護）
    gn = compute(_synth(reversal_cum=True, tpex="none"), CFG)
    print(f"== C1 自測 全None ==  verdict={gn['verdict']['code']}  "
          f"confidence={gn['verdict']['confidence']}  "
          f"C1_insufficient={gn['signals']['C1_tpex_confirm']['insufficient']}")
    assert gn["signals"]["C1_tpex_confirm"]["insufficient"], "C1 應標記 insufficient"
    assert not gn["signals"]["C1_tpex_confirm"]["triggered"], "C1 不應觸發"
    assert gn["verdict"]["confidence"] == 0, "資料不足不加分也不扣分"
    # 逐欄位回歸：除 C1 / confidence 外，輸出應與 tpex="buy" 情境完全一致
    def _strip(res):
        r = json.loads(json.dumps(res))                  # deep copy
        r["signals"].pop("C1_tpex_confirm")
        r["verdict"].pop("confidence")
        return r
    assert _strip(gn) == _strip(gb), "C1/confidence 以外的欄位不得受 tpex 資料影響"
    # 舊呼叫端（series 完全沒有 spot_net_tpex 鍵）也必須走 insufficient 路徑
    gm = compute(_synth(reversal_cum=True), CFG)
    assert gm["signals"]["C1_tpex_confirm"]["insufficient"]
    assert _strip(gm) == _strip(gb), "無 tpex 鍵的舊呼叫端輸出不得改變"

    print("\n✅ 全部自測通過（守門員邏輯正確：沒有 S4，最高只能到 🟡；"
          "C1 只加 confidence，不動 verdict code）")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
