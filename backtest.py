#!/usr/bin/env python3
"""回測：用歷史日行情，每天重跑策略，只用當天以前的資料產生訊號，
再用之後的實際日 K 模擬進出場，計入手續費、證交稅與滑價。

用法
  python backtest.py            抓證交所歷史行情（需在 GitHub Actions 或能連上證交所的環境）
  python backtest.py --demo     用模擬資料測試程式

輸出
  data/backtest.json            摘要與每筆交易（只保留已結案或已成交的）
"""
import argparse, datetime as dt, json, os, sys

import build_data as B

# 成本假設（單邊）。實際手續費依券商折讓而定，可在這裡調整。
FEE = 0.001425     # 手續費 0.1425%
TAX = 0.003        # 證交稅 0.3%（賣出）
SLIP = 0.001       # 滑價 0.1%（買賣各一次）

SIGNAL_DAYS = 21   # 回測訊號日數（約一個月的交易日）
FORWARD_DAYS = 15  # 每個訊號日之後保留的觀察天數，確保能結案
DEDUP_DAYS = 10    # 同一檔同一策略，幾個交易日內只取一次訊號


def net_pct(gross_pct):
    """把毛報酬（相對成交價的 %）換成扣除成本後的淨報酬 %。
    成交價在買賣式子中會約掉，所以只需要毛報酬。"""
    g = 1 + gross_pct / 100
    return ((g * (1 - SLIP) * (1 - FEE - TAX)) / ((1 + SLIP) * (1 + FEE)) - 1) * 100


def metrics(rows):
    done = [r for r in rows if r["net"] is not None]
    if not done:
        return {"signals": len(rows), "resolved": 0}
    nets = [r["net"] for r in done]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in sorted(done, key=lambda r: (r["date"], r["code"])):
        cum += x["net"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "signals": len(rows),
        "filled": sum(1 for r in rows if r["status"] != "no_fill"),
        "no_fill": sum(1 for r in rows if r["status"] == "no_fill"),
        "resolved": len(done),
        "win_rate": round(100 * len(wins) / len(done), 1),
        "avg_net": round(sum(nets) / len(nets), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "total_net_pts": round(cum, 1),
        "max_drawdown_pts": round(mdd, 1),
    }


def run(hist, ex, n_signal=SIGNAL_DAYS, n_fwd=FORWARD_DAYS):
    ds = sorted(hist["days"])
    n = len(ds)
    first, last = n - n_signal - n_fwd, n - n_fwd - 1          # 訊號日索引範圍
    if first < B.MIN_BARS:
        sys.exit("歷史資料不足：需要至少 %d 個交易日，目前 %d 個" % (n_signal + n_fwd + B.MIN_BARS, n))
    trades, recent = [], {}
    for i in range(first, last + 1):
        stocks, _, _ = B.analyse(hist, ex, upto=i)               # 只用第 i 天以前的資料
        for s in stocks:
            for k in "ABC":
                sg = s["sig"][k]
                if not sg or not sg.get("plan"):
                    continue
                key = (s["code"], k)
                if key in recent and i - recent[key] < DEDUP_DAYS:
                    continue
                recent[key] = i
                p = sg["plan"]
                bars = []
                for d in ds[i + 1:i + 1 + B.NO_FILL_DAYS + B.HOLD_MAX + 5]:
                    r = hist["days"][d].get(s["code"])
                    if r:
                        bars.append({"d": d, "o": r[0], "h": r[1], "l": r[2], "c": r[3]})
                res = B.simulate({"entry": p["entry"], "stop": p["stop"], "t1": p["t1"], "type": sg["type"]}, bars)
                if res["status"] == "pending":
                    continue
                net = None if res["ret"] is None else round(net_pct(res["ret"]), 2)
                trades.append({"date": ds[i], "code": s["code"], "strategy": k, "type": sg["type"],
                               "score": sg["score"], "rank": sg.get("rank"), "ext": s["ext"],
                               "ok": p["ok"], "rr": p["rr"], "status": res["status"],
                               "gross": res["ret"], "net": net, "days": res["days"]})
    return trades, ds[first], ds[last]


def summarize(trades):
    out = {"all": metrics(trades), "ok_only": metrics([t for t in trades if t["ok"]])}
    out["by_strategy"] = {k: metrics([t for t in trades if t["strategy"] == k]) for k in "ABC"}
    out["by_ext"] = {"未拉升": metrics([t for t in trades if not t["ext"]]),
                     "已拉升": metrics([t for t in trades if t["ext"]])}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default=os.path.join(B.DATA, "backtest.json"))
    args = ap.parse_args()

    if args.demo:
        hist = B.demo_history()
        ex = {"punish": set(), "attention": set(), "full_delivery": set(), "manual": set()}
    else:
        B.HISTORY_DAYS = SIGNAL_DAYS + FORWARD_DAYS + B.MIN_BARS + 20   # 足夠的歷史，供指標與結案使用
        hist = B.load_json(os.path.join(B.DATA, "backtest_history.json"), {})
        today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date()
        hist = B.collect_history(hist, today)
        B.save_json(os.path.join(B.DATA, "backtest_history.json"), hist)
        ex, _ = B.fetch_excluded()

    trades, start, end = run(hist, ex)
    result = {
        "window": [start, end],
        "params": {"fee": FEE, "tax": TAX, "slip": SLIP, "signal_days": SIGNAL_DAYS,
                   "forward_days": FORWARD_DAYS, "dedup_days": DEDUP_DAYS, "min_rr": B.MIN_RR},
        "note": "淨報酬已扣除手續費、證交稅與滑價。盈虧為每筆等額，最大回撤為淨報酬累加的百分點，不是複利。"
                "使用的處置股名單為今天的版本，未還原歷史，屬於已知限制。",
        "summary": summarize(trades),
        "trades": trades,
    }
    B.save_json(args.out, result, compact=False)
    s = result["summary"]
    print("window", start, "~", end, "| trades", len(trades), file=sys.stderr)
    for name in ("all", "ok_only"):
        print(name, json.dumps(s[name], ensure_ascii=False), file=sys.stderr)
    for k, v in s["by_strategy"].items():
        print("strategy", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)
    for k, v in s["by_ext"].items():
        print("ext", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
