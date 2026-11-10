"""Training ground: do LIMIT (maker) orders beat buying at the ask (taker)?

Why: live army + 500 replayed bots all lose about the fee. Favorites roughly break even BEFORE fees, so prices are
about fair and the taker fee + spread is what kills them. On Kalshi a resting order on a plain 'quadratic' series
pays NO fee (maker fees exist only on 'quadratic_with_maker_fees' series: 0.0175 x C x P x (1-P)), and it buys at
the bid instead of the ask.

The catch (adverse selection): a resting buy only fills when someone sells to you, which tends to happen when the
price is falling. This study keeps that catch in:
  - The order is placed AFTER the hour's snapshot that triggered it, at the side's bid ("join") or bid+1c ("improve").
  - It counts as filled only in a LATER hour whose closing ask for that side is at or below our price
    (sellers came all the way down to us). Hourly snapshots miss touches inside the hour, so this undercounts fills.
  - Fill size <= 10% of the contracts traded in that hour, like replay.py. No fill inside the window = cancelled.
The taker baseline buys the same signal at the same moment at the ask with the taker fee.

One $3 bet per (setting, market), first time the market qualifies. Same live filter: spread <= 6c, 24h volume >= 50,
0.5-48h left, nothing under 21c. t-scores are counted per EVENT (brackets in one event move together).
Split: TRAIN closes before 2026-10-16, TEST from then on.

Run:  py trainer/maker_study.py      -> trainer/maker_results.csv + maker_results.txt
"""
import csv
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import replay  # noqa: E402
import run  # noqa: E402
from fees import SUPPORTED, taker_fee  # noqa: E402
from strategies import category  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STAKE = 3.0
MAKER_RATE = 0.0175
BANDS = [(0.21, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95)]
HOURS = [(0.5, 6), (6, 24), (24, 48)]
MODES = ["join", "improve"]
WINDOWS = [2, 8]  # hours an order rests before we cancel it
CATS = ["all", "sports", "crypto", "finance", "weather", "other"]


def maker_fee(n, p, fee_type, mult):
    if fee_type == "quadratic":
        return 0.0
    return math.ceil(MAKER_RATE * n * p * (1 - p) * mult * 100 - 1e-9) / 100


def size(price, hour_vol, fee_fn):
    n = min(int(STAKE / price), int(hour_vol * replay.FILL_SHARE))
    while n > 0 and n * price + fee_fn(n) > STAKE + 1e-9:
        n -= 1
    return n


def load_trades():
    import sqlite3
    db = sqlite3.connect(os.path.join(HERE, "data", "trades.db"))
    ok = {r[0] for r in db.execute("SELECT ticker FROM done WHERE complete = 1")}
    tr = defaultdict(list)
    for tk, ts, yp, cnt, side in db.execute("SELECT ticker, ts, yes_price, count, taker_side FROM trades ORDER BY ticker, ts"):
        if tk in ok:
            tr[tk].append((ts, yp, cnt, side))
    for tk in ok:
        tr.setdefault(tk, [])
    return tr


def trade_fill(prints, side, L, start, end, through):
    """Contracts a resting buy of `side` at L could have got from real trades in (start, end].
    A YES buy fills when a taker SELLS yes (taker_side 'no') at yes_price <= L; a NO buy when a taker buys yes at
    no price (1 - yes_price) <= L. touch = trades at our price count (ignores queue, optimistic);
    through = only trades strictly better than our price (we were surely filled first, pessimistic)."""
    i = replay.bisect.bisect_right(prints, (start, 9e9, 9e9, "~"))
    got = 0.0
    for ts, yp, cnt, ts_side in prints[i:]:
        if ts > end:
            break
        if side == "yes":
            if ts_side != "no":
                continue
            px = yp
        else:
            if ts_side != "yes":
                continue
            px = round(1 - yp, 4)
        if px < L - 1e-9 or (not through and px <= L + 1e-9):
            got += cnt
    return got


def study(data, trades=None):
    # bets[key] = list of (close_ts, event, pnl, price, won); fills[key] = [placed, filled]
    bets = defaultdict(list)
    tries = defaultdict(lambda: [0, 0])
    for t, m in data.mk.items():
        c = data.c.get(t)
        if not c or m["result"] not in ("yes", "no"):
            continue
        if trades is not None and t not in trades:
            continue
        fee_type, mult = data.fee.get(m["series"], ("unknown", 1.0))
        if fee_type not in SUPPORTED:
            continue
        ts, bid, ask, cum = c
        cat = category(m["event"])
        first = {}  # (band, hrs) -> (i, side)
        for i in range(len(ts)):
            b, a = bid[i], ask[i]
            if b <= 0 or not 0 < a < 1 or a - b > run.MAX_SPREAD:
                continue
            j = replay.bisect.bisect_right(ts, ts[i] - 86400) - 1
            if cum[i] - (cum[j] if j >= 0 else 0.0) < run.MIN_VOLUME_24H:
                continue
            left = (m["close"] - ts[i]) / 3600
            if not run.MIN_HOURS <= left <= run.MAX_HOURS:
                continue
            ymid = (b + a) / 2
            for bi, (lo, hi) in enumerate(BANDS):
                if lo <= ymid < hi:
                    side = "yes"
                elif lo <= 1 - ymid < hi:
                    side = "no"
                else:
                    continue
                for hi_, (hlo, hhi) in enumerate(HOURS):
                    if hlo <= left < hhi and (bi, hi_) not in first:
                        first[(bi, hi_)] = (i, side)
        for (bi, hi_), (i, side) in first.items():
            won = side == m["result"]
            sbid = bid[i] if side == "yes" else round(1 - ask[i], 4)
            sask = ask[i] if side == "yes" else round(1 - bid[i], 4)
            row = (bi, hi_)
            # taker baseline
            hv = cum[i] - (cum[i - 1] if i > 0 else 0.0)
            if sask >= run.MIN_PRICE:
                n = size(sask, hv, lambda k: taker_fee([(sask, k)], mult))
                if n > 0:
                    pnl = (n if won else 0) - n * sask - taker_fee([(sask, n)], mult)
                    for ct in (cat, "all"):
                        bets[("taker", "-", 0, ct) + row].append((m["close"], m["event"], pnl, sask, won))
            # maker variants
            for mode in MODES:
                L = sbid if mode == "join" else round(sbid + 0.01, 4)
                if L >= sask or L < run.MIN_PRICE:
                    continue
                for w in WINDOWS:
                    end = min(ts[i] + w * 3600, m["close"] - int(run.MIN_HOURS * 3600))
                    if trades is not None:
                        for fill in ("touch", "through"):
                            avail = trade_fill(trades[t], side, L, ts[i], end, fill == "through")
                            n = min(int(STAKE / L), int(avail))
                            while n > 0 and n * L + maker_fee(n, L, fee_type, mult) > STAKE + 1e-9:
                                n -= 1
                            key = ("maker", f"{mode}-{fill}", w, )
                            for ct in (cat, "all"):
                                tr = tries[key + (ct,) + row]
                                tr[0] += 1
                                tr[1] += n > 0
                            if n > 0:
                                pnl = (n if won else 0) - n * L - maker_fee(n, L, fee_type, mult)
                                for ct in (cat, "all"):
                                    bets[key + (ct,) + row].append((m["close"], m["event"], pnl, L, won))
                        continue
                    key_rest = (mode, w)
                    got = None
                    for k in range(i + 1, len(ts)):
                        if ts[k] > end:
                            break
                        kb, ka = bid[k], ask[k]
                        if kb < 0 or ka < 0:
                            continue
                        k_sask = ka if side == "yes" else round(1 - kb, 4)
                        if k_sask <= L + 1e-9:
                            hvk = cum[k] - cum[k - 1]
                            n = size(L, hvk, lambda q: maker_fee(q, L, fee_type, mult))
                            got = n if n > 0 else 0
                            break
                    for ct in (cat, "all"):
                        tr = tries[("maker",) + key_rest + (ct,) + row]
                        tr[0] += 1
                        if got:
                            tr[1] += 1
                    if got:
                        pnl = (got if won else 0) - got * L - maker_fee(got, L, fee_type, mult)
                        for ct in (cat, "all"):
                            bets[("maker",) + key_rest + (ct,) + row].append((m["close"], m["event"], pnl, L, won))
    return bets, tries


def ev_stats(bs):
    """n bets, total, per bet, event-level t, win rate, avg price."""
    if not bs:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0
    per_ev = defaultdict(float)
    for _, ev, p, _, _ in bs:
        per_ev[ev] += p
    x = list(per_ev.values())
    n = len(x)
    avg = sum(x) / n
    sd = math.sqrt(sum((v - avg) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    t = avg / (sd / math.sqrt(n)) if sd else 0.0
    tot = sum(b[2] for b in bs)
    return len(bs), tot, tot / len(bs), t, sum(b[4] for b in bs) / len(bs), sum(b[3] for b in bs) / len(bs)


def main():
    print("loading history...", flush=True)
    data = replay.Data()
    use_trades = "--trades" in sys.argv
    trades = load_trades() if use_trades else None
    tag = "_trades" if use_trades else ""
    print(f"{len(data.mk)} markets{f', {len(trades)} with full trade prints' if use_trades else ''}; studying...",
          flush=True)
    bets, tries = study(data, trades)
    rows = []
    for key, bs in bets.items():
        kind, mode, w, cat, bi, hi_ = key
        tr_ = [b for b in bs if b[0] < replay.SPLIT_TS]
        te_ = [b for b in bs if b[0] >= replay.SPLIT_TS]
        a, s_tr, s_te = ev_stats(bs), ev_stats(tr_), ev_stats(te_)
        tri = tries.get(key)
        rows.append(dict(kind=kind, mode=mode, window_h=w, cat=cat, band=f"{BANDS[bi][0]:.2f}-{BANDS[bi][1]:.2f}",
                         hours=f"{HOURS[hi_][0]}-{HOURS[hi_][1]}",
                         fill_rate=round(tri[1] / tri[0], 3) if tri and tri[0] else "",
                         bets=a[0], total=round(a[1], 2), per_bet=round(a[2], 4), t_all=round(a[3], 2),
                         win=round(a[4], 3), avg_price=round(a[5], 3),
                         train_bets=s_tr[0], train_per_bet=round(s_tr[2], 4), train_t=round(s_tr[3], 2),
                         test_bets=s_te[0], test_per_bet=round(s_te[2], 4), test_t=round(s_te[3], 2)))
    rows.sort(key=lambda r: -r["t_all"])
    with open(os.path.join(HERE, f"maker_results{tag}.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    lines = []
    tot = defaultdict(lambda: [0, 0.0])
    for r in rows:
        if r["cat"] == "all":
            k = r["kind"] if r["kind"] == "taker" else f"maker-{r['mode']}-{r['window_h']}h"
            tot[k][0] += r["bets"]
            tot[k][1] += r["total"]
    lines.append("Overall (cat=all, every band/hours bucket):")
    for k, (n, s) in sorted(tot.items()):
        lines.append(f"  {k:20s} {n:7d} bets  {s:+10.2f}  {100 * s / max(n, 1):+.2f}c/bet")
    passed = [r for r in rows if r["train_bets"] >= 100 and r["test_bets"] >= 50 and r["train_t"] >= 2
              and r["test_t"] >= 1 and r["train_per_bet"] > 0 and r["test_per_bet"] > 0]
    lines.append(f"\n{len(rows)} settings tested. PASSED (train t>=2 AND test t>=1, both profitable, enough bets): {len(passed)}")
    for r in passed[:40]:
        lines.append(f"  {r['kind']:5s} {r['mode']:7s} {r['window_h']}h {r['cat']:8s} {r['band']} {r['hours']:>6}h "
                     f"fill {r['fill_rate']}  train {r['train_bets']} @ {100 * r['train_per_bet']:+.1f}c t{r['train_t']}  "
                     f"test {r['test_bets']} @ {100 * r['test_per_bet']:+.1f}c t{r['test_t']}")
    lines.append(f"\nBonferroni bar for {len(rows)} settings is about t {bar(len(rows)):.1f} on ALL data.")
    strong = [r for r in passed if r["t_all"] >= bar(len(rows))]
    lines.append(f"Passed AND above that bar: {len(strong)}")
    for r in strong[:20]:
        lines.append(f"  {r['kind']} {r['mode']} {r['window_h']}h {r['cat']} {r['band']} {r['hours']}h  t_all {r['t_all']}")
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(HERE, f"maker_results{tag}.txt"), "w") as f:
        f.write(out + "\n")


def bar(n, alpha=0.05):
    # two-sided normal quantile for alpha/n, by bisection on erfc
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha / n:
            lo = mid
        else:
            hi = mid
    return lo


if __name__ == "__main__":
    main()
