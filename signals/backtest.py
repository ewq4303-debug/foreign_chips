#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外資籌碼「轉折確認」回測工具  signals/backtest.py
====================================================

依據 reversal_signals.py 的 flow + stock 燈號邏輯，對 history.json 做逐日 walk-forward
回放：每個交易日只使用當日以前資料計算 verdict，再用未來加權指數報酬評估訊號。

輸出重點：
  * 每日事件列：日期、verdict、流量分數、守門員、警告、未來報酬與最大有利/不利變動。
  * verdict 摘要：各燈號出現次數、勝率、平均/中位數未來報酬、平均最大不利變動。
  * 參數掃描：針對 REV_* 對應門檻做笛卡兒組合，依自訂 score 排名。

執行方式：
  python -m signals.backtest
  python -m signals.backtest --horizon 5 --output docs/data/backtest_report.json
  python -m signals.backtest --scan --z-extreme 1.8,2.0,2.2 --flow-min 2,3
"""

import argparse
import csv
import datetime as dt
import json
import os
from itertools import product
from statistics import median

from signals import reversal_signals as rs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HISTORY = os.path.join(ROOT, "data", "history.json")
DEFAULT_OUTPUT = os.path.join(ROOT, "docs", "data", "backtest_report.json")
TPE_TZ = dt.timezone(dt.timedelta(hours=8))

LONG_VERDICTS = {"REVERSAL_CONFIRMED", "BOUNCE_BREWING"}
RISK_VERDICTS = {"PANIC_CONTINUES"}


def _load_history(path):
    with open(path, encoding="utf-8") as f:
        rows = json.load(f).get("history", [])
    return sorted(rows, key=lambda r: r["date"])


def _series_for_prefix(rows, end_index):
    """以 rows[:end_index+1] 建立 reversal_signals.compute() 需要的標準序列。"""
    return rs.load_series(rows[:end_index + 1])


def _cfg(base, overrides):
    out = dict(base)
    for key, value in overrides.items():
        if value is not None:
            out[key] = value
    return out


def _future_stats(rows, idx, horizon):
    """計算 idx 後 horizon 個交易日的 forward return / MFE / MAE。"""
    current = rows[idx].get("taiex_close")
    if current in (None, 0):
        return None
    future = [r.get("taiex_close") for r in rows[idx + 1:idx + 1 + horizon]
              if r.get("taiex_close") is not None]
    if not future:
        return None
    end = future[-1]
    returns = [(x / current - 1.0) * 100.0 for x in future]
    return {
        "forward_days": len(future),
        "forward_return_pct": round((end / current - 1.0) * 100.0, 4),
        "max_favorable_pct": round(max(returns), 4),
        "max_adverse_pct": round(min(returns), 4),
    }


def _make_event(rows, idx, horizon, cfg):
    out = rs.compute(_series_for_prefix(rows, idx), cfg)
    verdict = out["verdict"]
    stats = _future_stats(rows, idx, horizon)
    row = rows[idx]
    event = {
        "date": row["date"],
        "taiex_close": row.get("taiex_close"),
        "verdict": verdict["code"],
        "label": verdict["label"],
        "flow_score": verdict["flow_score"],
        "stock_confirmed": verdict["stock_confirmed"],
        "warning_active": verdict["warning_active"],
        "insufficient_data": out["insufficient_data"],
    }
    if stats:
        event.update(stats)
        event["long_win"] = bool(event["forward_return_pct"] > 0)
        event["risk_off_win"] = bool(event["forward_return_pct"] < 0)
    else:
        event.update({
            "forward_days": 0,
            "forward_return_pct": None,
            "max_favorable_pct": None,
            "max_adverse_pct": None,
            "long_win": None,
            "risk_off_win": None,
        })
    return event


def walk_forward(rows, horizon, cfg):
    """逐日 walk-forward 回放；保留最後不足 horizon 的事件但不納入摘要。"""
    return [_make_event(rows, idx, horizon, cfg) for idx in range(len(rows))]


def _summarize_group(events, verdict):
    done = [e for e in events if e["verdict"] == verdict and e["forward_return_pct"] is not None
            and not e["insufficient_data"]]
    if not done:
        return {
            "count": 0, "win_rate_pct": None, "avg_return_pct": None,
            "median_return_pct": None, "avg_max_favorable_pct": None,
            "avg_max_adverse_pct": None,
        }

    returns = [e["forward_return_pct"] for e in done]
    mfe = [e["max_favorable_pct"] for e in done]
    mae = [e["max_adverse_pct"] for e in done]
    if verdict in LONG_VERDICTS:
        wins = sum(1 for e in done if e["long_win"])
    elif verdict in RISK_VERDICTS:
        wins = sum(1 for e in done if e["risk_off_win"])
    else:
        wins = sum(1 for e in done if abs(e["forward_return_pct"]) < 1.0)

    return {
        "count": len(done),
        "win_rate_pct": round(wins / len(done) * 100.0, 2),
        "avg_return_pct": round(sum(returns) / len(returns), 4),
        "median_return_pct": round(median(returns), 4),
        "avg_max_favorable_pct": round(sum(mfe) / len(mfe), 4),
        "avg_max_adverse_pct": round(sum(mae) / len(mae), 4),
    }


def summarize(events):
    verdicts = ["PANIC_CONTINUES", "BOUNCE_BREWING", "REVERSAL_CONFIRMED", "NEUTRAL"]
    by_verdict = {v: _summarize_group(events, v) for v in verdicts}
    valid = [e for e in events if e["forward_return_pct"] is not None and not e["insufficient_data"]]
    reversals = [e for e in valid if e["verdict"] == "REVERSAL_CONFIRMED"]
    brewing = [e for e in valid if e["verdict"] == "BOUNCE_BREWING"]
    panic = [e for e in valid if e["verdict"] == "PANIC_CONTINUES"]
    return {
        "total_events": len(events),
        "valid_events": len(valid),
        "by_verdict": by_verdict,
        "signal_counts": {
            "reversal_confirmed": len(reversals),
            "bounce_brewing": len(brewing),
            "panic_continues": len(panic),
        },
    }


def score_summary(summary):
    """參數掃描排名分數；高轉折勝率/報酬加分，恐慌後下跌命中加分，訊號過少扣分。"""
    rev = summary["by_verdict"]["REVERSAL_CONFIRMED"]
    panic = summary["by_verdict"]["PANIC_CONTINUES"]
    rev_count = rev["count"]
    if rev_count == 0:
        return -9999.0
    rev_win = rev["win_rate_pct"] or 0.0
    rev_ret = rev["avg_return_pct"] or 0.0
    panic_win = panic["win_rate_pct"] or 0.0
    sample_penalty = 15.0 if rev_count < 3 else 0.0
    return round(rev_win * 0.55 + rev_ret * 8.0 + panic_win * 0.20 - sample_penalty, 4)


def _parse_values(raw, cast):
    if raw is None:
        return [None]
    return [cast(x.strip()) for x in raw.split(",") if x.strip()]


def scan(rows, horizon, base_cfg, args):
    dimensions = {
        "Z_EXTREME": _parse_values(args.z_extreme, float),
        "Z_EXIT": _parse_values(args.z_exit, float),
        "FLOW_MIN": _parse_values(args.flow_min, int),
        "CUM_FLAT_EPS": _parse_values(args.cum_flat_eps, float),
        "ETOTAL_WARN": _parse_values(args.etotal_warn, float),
    }
    keys = list(dimensions.keys())
    results = []
    for values in product(*(dimensions[k] for k in keys)):
        overrides = {k: v for k, v in zip(keys, values) if v is not None}
        cfg = _cfg(base_cfg, overrides)
        events = walk_forward(rows, horizon, cfg)
        summary = summarize(events)
        results.append({
            "score": score_summary(summary),
            "overrides": overrides,
            "summary": summary,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def write_csv(events, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not events:
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(events[0].keys()))
        writer.writeheader()
        writer.writerows(events)


def build_report(rows, horizon, cfg, events, summary, scan_results=None):
    return {
        "generated_at": dt.datetime.now(TPE_TZ).isoformat(),
        "history_start": rows[0]["date"] if rows else None,
        "history_end": rows[-1]["date"] if rows else None,
        "horizon_days": horizon,
        "config": {
            "Z_EXTREME": cfg["Z_EXTREME"],
            "Z_EXIT": cfg["Z_EXIT"],
            "PEAK_WIN": cfg["PEAK_WIN"],
            "SLOPE_LAG": cfg["SLOPE_LAG"],
            "SPREAD_STREAK": cfg["SPREAD_STREAK"],
            "SHRINK_WIN": cfg["SHRINK_WIN"],
            "SHRINK_FACTOR": cfg["SHRINK_FACTOR"],
            "CUM_SLOPE_WIN": cfg["CUM_SLOPE_WIN"],
            "CUM_FLAT_EPS": cfg["CUM_FLAT_EPS"],
            "ETOTAL_WARN": cfg["ETOTAL_WARN"],
            "FLOW_MIN": cfg["FLOW_MIN"],
            "MIN_SAMPLE": cfg["MIN_SAMPLE"],
        },
        "summary": summary,
        "events": events,
        "scan_results": scan_results or [],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="回測外資籌碼轉折確認訊號")
    parser.add_argument("--history", default=DEFAULT_HISTORY, help="history.json 路徑")
    parser.add_argument("--horizon", type=int, default=5, help="往後評估交易日數")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="JSON 報告輸出路徑")
    parser.add_argument("--csv", default=None, help="另存每日事件 CSV")
    parser.add_argument("--scan", action="store_true", help="啟用參數掃描")
    parser.add_argument("--top", type=int, default=10, help="參數掃描輸出前 N 名")
    parser.add_argument("--z-extreme", default=None, help="掃描 REV_Z_EXTREME，例如 1.8,2.0,2.2")
    parser.add_argument("--z-exit", default=None, help="掃描 REV_Z_EXIT，例如 1.6,1.8,2.0")
    parser.add_argument("--flow-min", default=None, help="掃描 REV_FLOW_MIN，例如 2,3")
    parser.add_argument("--cum-flat-eps", default=None, help="掃描 REV_CUM_FLAT_EPS，例如 0,5,10")
    parser.add_argument("--etotal-warn", default=None, help="掃描 REV_ETOTAL_WARN，例如 -300,-500,-700")
    args = parser.parse_args(argv)

    if args.horizon <= 0:
        raise SystemExit("--horizon 必須大於 0")

    rows = _load_history(args.history)
    cfg = dict(rs.CFG)
    events = walk_forward(rows, args.horizon, cfg)
    summary = summarize(events)
    scan_results = scan(rows, args.horizon, cfg, args)[:args.top] if args.scan else []
    report = build_report(rows, args.horizon, cfg, events, summary, scan_results)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if args.csv:
        write_csv(events, args.csv)

    print(f"[backtest] history={report['history_start']}..{report['history_end']} "
          f"horizon={args.horizon} valid={summary['valid_events']}")
    for code, item in summary["by_verdict"].items():
        print(f"[backtest] {code:20s} count={item['count']:3d} "
              f"win={item['win_rate_pct']} avg_ret={item['avg_return_pct']}")
    if scan_results:
        best = scan_results[0]
        print(f"[backtest] best_score={best['score']} overrides={best['overrides']}")
    print(f"[backtest] 已輸出 → {args.output}")


if __name__ == "__main__":
    main()
