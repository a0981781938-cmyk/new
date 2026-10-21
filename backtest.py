#!/usr/bin/env python3
"""回測：用歷史日行情，每天重跑策略，只用當天以前的資料產生訊號，
再用之後的實際日 K 模擬進出場，計入手續費、證交稅與滑價。

用法
  python backtest.py                 預設回測 126 個交易日（約 6 個月）
  python backtest.py --days 250      回測約 12 個月
  python backtest.py --demo --days 5 用模擬資料測試程式

輸出
  data/backtest.json                 摘要、分組統計與每筆交易
"""
import argparse, datetime as dt, json, os, sys

import build_data as B

# 成本假設（單邊）。實際手續費依券商折讓而定。
FEE = 0.001425     # 手續費 0.1425%
TAX = 0.003        # 證交稅 0.3%（賣出）
SLIP = 0.001       # 滑價 0.1%（買賣各一次）

FORWARD_DAYS = 15  # 每個訊號日之後保留的觀察天數，確保能結案
DEDUP_DAYS = 10    # 同一檔同一策略，幾個交易日內只取一次訊號
MODES = ("full", "half", "half_be")
MODE_NAME = {"full": "全部目標一出場", "half": "目標一出一半、其餘守止損", "half_be": "目標一出一半、其餘止損移到進場價"}
STATS = {"limit_blocked": 0}  # 因漲停鎖住而買不到的 K 棒數（只在 full 模式計一次）


def net_pct(gross_pct):
    """毛報酬（相對成交價的 %）換成扣除成本後的淨報酬 %。"""
    g = 1 + gross_pct / 100
    return ((g * (1 - SLIP) * (1 - FEE - TAX)) / ((1 + SLIP) * (1 + FEE)) - 1) * 100


def sim_exit(entry, stop, t1, t2, typ, bars, mode):
    """依出場方式模擬一筆交易。回傳 (毛報酬%, 淨報酬%, 狀態)；未成交或尚未結案回傳 None。"""
    fill = fill_k = None
    left, cur_stop, t1_done = 1.0, stop, False
    legs = []                                   # (比例, 成交價, 原因)
    for k, b in enumerate(bars):
        if fill is None:
            if k >= B.NO_FILL_DAYS:
                return None, None, "no_fill"
            if b.get("lim") and b["l"] >= b["lim"]:      # 當日成交全在漲停價（鎖住）：買不到，略過這根 K
                if mode == "full":
                    STATS["limit_blocked"] += 1
                continue
            if typ == "pullback":
                if b["l"] > entry:
                    continue
                fill = min(b["o"], entry)
            else:
                if b["h"] < entry:
                    continue
                fill = max(b["o"], entry)
            fill_k = k
        held = k - fill_k + 1
        if b["l"] <= cur_stop:                  # 先判斷止損（較保守）
            legs.append((left, cur_stop if b["o"] > cur_stop else b["o"], "stopped"))
            left = 0
            break
        if mode == "full":
            if b["h"] >= t1:
                legs.append((1.0, t1 if b["o"] < t1 else b["o"], "target"))
                left = 0
                break
        else:
            if t1_done and b["h"] >= t2:
                legs.append((left, t2 if b["o"] < t2 else b["o"], "target2"))
                left = 0
                break
            if not t1_done and b["h"] >= t1:
                legs.append((0.5, t1 if b["o"] < t1 else b["o"], "target1"))
                left = 0.5
                t1_done = True
                if mode == "half_be":
                    cur_stop = fill
        if held >= B.HOLD_MAX:
            legs.append((left, b["c"], "expired"))
            left = 0
            break
    else:
        return None, None, ("pending" if fill is None else "holding")
    gross = sum(f * (px / fill - 1) for f, px, _ in legs) * 100
    net = sum(f * net_pct((px / fill - 1) * 100) for f, px, _ in legs)
    return round(gross, 2), round(net, 2), legs[-1][2]


def metrics(rows, key="net"):
    done = [r for r in rows if r.get(key) is not None]
    if not done:
        return {"signals": len(rows), "resolved": 0}
    nets = [r[key] for r in done]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in sorted(done, key=lambda r: (r["date"], r["code"])):
        cum += x[key]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "signals": len(rows),
        "filled": sum(1 for r in rows if r["status"] != "no_fill"),
        "resolved": len(done),
        "win_rate": round(100 * len(wins) / len(done), 1),
        "avg_net": round(sum(nets) / len(nets), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "total_net_pts": round(cum, 1),
        "max_drawdown_pts": round(mdd, 1),
    }


def rank_bucket(r):
    if r is None:
        return "無"
    return "<60" if r < 60 else "60–69" if r < 70 else "70–79" if r < 80 else "80–89" if r < 90 else "90–100"


def run(hist, ex, n_signal, n_fwd=FORWARD_DAYS):
    ds = sorted(hist["days"])
    n = len(ds)
    first, last = n - n_signal - n_fwd, n - n_fwd - 1
    if first < B.MIN_BARS:
        sys.exit("歷史資料不足：需要至少 %d 個交易日，目前 %d 個" % (n_signal + n_fwd + B.MIN_BARS, n))
    trades, recent = [], {}
    for i in range(first, last + 1):
        stocks, _, _ = B.analyse(hist, ex, upto=i)          # 只用第 i 天以前的資料
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
                prev_c = hist["days"][ds[i]].get(s["code"], [0, 0, 0, 0])[3]     # 訊號日收盤
                for d in ds[i + 1:i + 1 + B.NO_FILL_DAYS + B.HOLD_MAX + 5]:
                    r = hist["days"][d].get(s["code"])
                    if r:
                        lim = B.r_down(prev_c * 1.10) if prev_c > 0 else None    # 漲停價：前收 ×1.1，依升降單位無條件捨去
                        bars.append({"d": d, "o": r[0], "h": r[1], "l": r[2], "c": r[3], "lim": lim})
                        prev_c = r[3]
                t2 = p["t2"]
                row = {"date": ds[i], "code": s["code"], "name": s["name"], "strategy": k, "type": sg["type"],
                       "score": sg["score"], "rank": sg.get("rank"), "bucket": rank_bucket(sg.get("rank")),
                       "ext": s["ext"], "ok": p["ok"], "rr": p["rr"]}
                skip = False
                for mode in MODES:
                    g, net, st = sim_exit(p["entry"], p["stop"], p["t1"], t2, sg["type"], bars, mode)
                    if st in ("pending", "holding"):
                        skip = True
                        break
                    row["net_" + mode] = net
                    row["gross_" + mode] = g
                    if mode == "full":
                        row["status"] = st
                if not skip:
                    row["net"] = row["net_full"]
                    trades.append(row)
    return trades, ds[first], ds[last]


def summarize(trades):
    out = {"all": metrics(trades), "ok_only": metrics([t for t in trades if t["ok"]])}
    out["by_strategy"] = {k: metrics([t for t in trades if t["strategy"] == k]) for k in "ABC"}
    out["by_ext"] = {"未拉升": metrics([t for t in trades if not t["ext"]]),
                     "已拉升": metrics([t for t in trades if t["ext"]])}
    out["by_rank"] = {b: metrics([t for t in trades if t["bucket"] == b]) for b in ("<60", "60–69", "70–79", "80–89", "90–100")}
    out["exit_variants"] = {m: {"name": MODE_NAME[m], **metrics(trades, key="net_" + m)} for m in MODES}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=int, default=126, help="回測的訊號交易日數（126≈6 個月，250≈12 個月）")
    ap.add_argument("--out", default=os.path.join(B.DATA, "backtest.json"))
    args = ap.parse_args()

    if args.demo:
        hist = B.demo_history()
        ex = {"punish": set(), "attention": set(), "full_delivery": set(), "manual": set()}
    else:
        B.HISTORY_DAYS = args.days + FORWARD_DAYS + B.MIN_BARS + 20
        hist = B.load_json(os.path.join(B.DATA, "backtest_history.json"), {})
        today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).date()
        hist = B.collect_history(hist, today)
        B.save_json(os.path.join(B.DATA, "backtest_history.json"), hist)
        ex, _ = B.fetch_excluded()

    trades, start, end = run(hist, ex, args.days)
    result = {
        "window": [start, end],
        "params": {"fee": FEE, "tax": TAX, "slip": SLIP, "signal_days": args.days,
                   "forward_days": FORWARD_DAYS, "dedup_days": DEDUP_DAYS, "min_rr": B.MIN_RR},
        "note": "淨報酬已扣除手續費、證交稅與滑價。盈虧為每筆等額，最大回撤為淨報酬累加的百分點，不是複利。"
                "處置股名單為今天的版本，未還原歷史；除權息未還原，單日漲跌超過 10% 的股票已排除。",
        "summary": summarize(trades),
        "trades": trades,
    }
    B.save_json(args.out, result, compact=False)
    s = result["summary"]
    print("window", start, "~", end, "| trades", len(trades), "| limit_blocked_bars", STATS["limit_blocked"], file=sys.stderr)
    for name in ("all", "ok_only"):
        print(name, json.dumps(s[name], ensure_ascii=False), file=sys.stderr)
    for k, v in s["by_strategy"].items():
        print("strategy", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)
    for k, v in s["by_ext"].items():
        print("ext", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)
    for k, v in s["by_rank"].items():
        print("rank", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)
    for k, v in s["exit_variants"].items():
        print("exit", k, json.dumps(v, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
