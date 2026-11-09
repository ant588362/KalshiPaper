"""Training ground: does live BTC/ETH spot + volatility beat Kalshi's crypto price markets?

For every hourly Kalshi price of a KXBTCD/KXBTC/KXETHD/KXETH market (0.5-48h before close), the model says:
    chance it ends above K = Phi( ln(spot/K) / (sigma_hour * sqrt(hours_left)) )      (no drift)
with sigma_hour from the last W hourly Coinbase returns. Ranges = P(above floor) - P(above cap).
Optional fat tails: Student-t (df 4) with the same spread.

Three checks:
 1. BASIS: does Coinbase's price at close agree with how Kalshi settled (it settles on CF Benchmarks' BRTI)?
 2. CALIBRATION: Brier score of the model vs the market's own mid price (lower = better forecaster).
 3. TRADING: per market, buy the FIRST hour the model's chance beats the ask by more than the fee + a threshold,
    $3 a bet, live rules (21c floor, 6c spread, 50 vol/24h, fill <= 10% of the hour's volume), hold to settlement.
    t-scores are computed over EVENTS (all ranges of one event ride on one price, so they're not independent).
Usage: py trainer/crypto_study.py
"""
import bisect
import math
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run  # noqa: E402
from fees import taker_fee  # noqa: E402
from replay import SPLIT_TS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERIES = {"KXBTCD": "BTC-USD", "KXBTC": "BTC-USD", "KXETHD": "ETH-USD", "KXETH": "ETH-USD"}


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _t4(t):
    # exact CDF of Student-t with 4 degrees of freedom
    return 0.5 + t * (t * t + 6) / (2 * (t * t + 4) ** 1.5)


def t4_cdf(x):
    # Student-t df=4 scaled to unit variance (raw t4 variance = 2)
    return _t4(x * math.sqrt(2))


def load():
    spot = defaultdict(dict)
    sdb = sqlite3.connect(os.path.join(HERE, "data", "spot.db"))
    for p, ts, c in sdb.execute("SELECT product, ts, close FROM spot ORDER BY ts"):
        spot[p][ts] = c
    db = sqlite3.connect(os.path.join(HERE, "data", "history.db"))
    fee = {s: m for s, t, m in db.execute("SELECT series, fee_type, fee_multiplier FROM fees")}
    mk = {}
    q = "SELECT ticker, event, series, close_ts, result, strike_type, floor_strike, cap_strike FROM markets WHERE series IN (%s)"
    for row in db.execute(q % ",".join("?" * len(SERIES)), list(SERIES)):
        mk[row[0]] = dict(zip(("ticker", "event", "series", "close", "result", "st", "fl", "cap"), row))
    cs = defaultdict(list)
    q = "SELECT ticker, ts, yes_bid, yes_ask, volume FROM candles WHERE ticker IN (SELECT ticker FROM markets WHERE series IN (%s)) ORDER BY ticker, ts"
    for t, ts, b, a, v in db.execute(q % ",".join("?" * len(SERIES)), list(SERIES)):
        cs[t].append((ts, b, a, v or 0.0))
    return spot, fee, mk, cs


def sigma(series_px, ts, w):
    """stdev of the last w hourly log returns ending at ts (inclusive), or None."""
    xs = [series_px.get(ts - k * 3600) for k in range(w + 1)]
    if any(x is None for x in xs):
        return None
    r = [math.log(xs[k] / xs[k + 1]) for k in range(w)]
    m = sum(r) / w
    return math.sqrt(sum((x - m) ** 2 for x in r) / (w - 1))


def p_above(S, K, sig, h, cdf):
    if K is None:
        return None
    return cdf(math.log(S / K) / (sig * math.sqrt(h)))


def p_yes(m, S, sig, h, cdf):
    if m["st"] == "greater":
        return p_above(S, m["fl"], sig, h, cdf)
    if m["st"] == "less":
        return 1 - p_above(S, m["cap"], sig, h, cdf)
    if m["st"] == "between":
        return p_above(S, m["fl"], sig, h, cdf) - p_above(S, m["cap"] + 0.01, sig, h, cdf)
    return None


def basis_check(spot, mk):
    ok = n = near_ok = near_n = 0
    for m in mk.values():
        S = spot[SERIES[m["series"]]].get(m["close"])
        if S is None or m["result"] not in ("yes", "no"):
            continue
        pred = p_yes(m, S, 1e-9, 1.0, lambda x: 1.0 if x > 0 else 0.0)
        if pred is None:
            continue
        hit = (pred > 0.5) == (m["result"] == "yes")
        n += 1
        ok += hit
        K = m["fl"] if m["fl"] is not None else m["cap"]
        if abs(S - K) / S < 0.001:
            near_n += 1
            near_ok += hit
    print(f"BASIS: Coinbase close-hour price agrees with Kalshi's result on {ok}/{n} = {ok / max(n, 1):.1%} of markets;"
          f" within 0.1% of the strike: {near_ok}/{near_n} = {near_ok / max(near_n, 1):.1%}")


def obs_iter(spot, mk, cs, w, cdf):
    """yield (market, ts, hours_left, bid, ask, hour_vol, vol24, model p_yes)"""
    for t, rows in cs.items():
        m = mk.get(t)
        if not m or m["result"] not in ("yes", "no"):
            continue
        px = spot[SERIES[m["series"]]]
        tss = [r[0] for r in rows]
        cum = []
        s = 0.0
        for r in rows:
            s += r[3]
            cum.append(s)
        for i, (ts, b, a, v) in enumerate(rows):
            h = (m["close"] - ts) / 3600
            if not (run.MIN_HOURS <= h <= run.MAX_HOURS) or b is None or a is None:
                continue
            S = px.get(ts)
            sig = sigma(px, ts, w)
            if S is None or not sig:
                continue
            p = p_yes(m, S, sig, h, cdf)
            if p is None:
                continue
            j = bisect.bisect_right(tss, ts - 86400) - 1
            vol24 = cum[i] - (cum[j] if j >= 0 else 0.0)
            yield m, ts, h, b, a, v, vol24, min(max(p, 0.0), 1.0)


def calibration(spot, mk, cs, w, cdf, label):
    sm = smk = n = 0
    for m, ts, h, b, a, v, vol24, p in obs_iter(spot, mk, cs, w, cdf):
        y = 1.0 if m["result"] == "yes" else 0.0
        mid = (b + a) / 2
        sm += (p - y) ** 2
        smk += (mid - y) ** 2
        n += 1
    print(f"CALIBRATION {label}: {n} hourly prices | Brier model {sm / n:.4f} vs market mid {smk / n:.4f}"
          f" ({'model better' if sm < smk else 'market better'})")


def trade(spot, mk, cs, fee, w, cdf, thr, min_price=run.MIN_PRICE):
    taken = set()
    bets = []  # (close, event, pnl, cost, won, price, side)
    obs = sorted(obs_iter(spot, mk, cs, w, cdf), key=lambda o: o[1])
    for m, ts, h, b, a, v, vol24, p in obs:
        if m["ticker"] in taken or a - b > run.MAX_SPREAD or vol24 < run.MIN_VOLUME_24H or b <= 0 or a >= 1:
            continue
        mult = fee.get(m["series"], 1.0)
        best = None
        for side, prob, ask in (("yes", p, a), ("no", 1 - p, 1 - b)):
            if ask < min_price or ask >= 1:
                continue
            edge = prob - ask - 0.07 * ask * (1 - ask) * mult
            if edge > thr and (best is None or edge > best[0]):
                best = (edge, side, ask)
        if not best:
            continue
        _, side, ask = best
        n = min(int(run.MAX_PER_TRADE / ask), int(v * 0.10))
        while n > 0 and n * ask + taker_fee([(ask, n)], mult) > run.MAX_PER_TRADE + 1e-9:
            n -= 1
        if n <= 0:
            continue
        cost = n * ask + taker_fee([(ask, n)], mult)
        won = m["result"] == side
        bets.append((m["close"], m["event"], (n if won else 0) - cost, cost, won, ask, side))
        taken.add(m["ticker"])
    return bets


def summarize(bets):
    if not bets:
        return "0 bets"
    ev = defaultdict(float)
    for c, e, pnl, *_ in bets:
        ev[e] += pnl
    xs = list(ev.values())
    k = len(xs)
    avg = sum(xs) / k
    sd = math.sqrt(sum((x - avg) ** 2 for x in xs) / (k - 1)) if k > 1 else 0
    t = avg / (sd / math.sqrt(k)) if sd else 0
    tot = sum(b[2] for b in bets)
    cost = sum(b[3] for b in bets)
    wr = sum(b[4] for b in bets) / len(bets)
    ap = sum(b[5] for b in bets) / len(bets)
    return (f"{len(bets):5d} bets / {k:4d} events | P&L {tot:+8.2f} | {100 * tot / cost:+5.1f}% per $ | "
            f"win {wr:.0%} @ avg {ap:.2f} | t(events) {t:+.2f}")


def scaled(cdf, k):
    return lambda x: cdf(x / k)


def brier_split(spot, mk, cs, w, cdf):
    """(train model, train market, test model, test market, by-horizon test rows)"""
    acc = {"tr": [0.0, 0.0, 0], "te": [0.0, 0.0, 0]}
    hz = defaultdict(lambda: [0.0, 0.0, 0])
    for m, ts, h, b, a, v, vol24, p in obs_iter(spot, mk, cs, w, cdf):
        y = 1.0 if m["result"] == "yes" else 0.0
        mid = (b + a) / 2
        k = "tr" if m["close"] < SPLIT_TS else "te"
        acc[k][0] += (p - y) ** 2
        acc[k][1] += (mid - y) ** 2
        acc[k][2] += 1
        if k == "te":
            bucket = "<3h" if h < 3 else "3-12h" if h < 12 else "12-48h"
            hz[bucket][0] += (p - y) ** 2
            hz[bucket][1] += (mid - y) ** 2
            hz[bucket][2] += 1
    f = lambda r: (r[0] / max(r[2], 1), r[1] / max(r[2], 1))  # noqa: E731
    return f(acc["tr"]), f(acc["te"]), {b: f(r) + (r[2],) for b, r in hz.items()}


def main():
    spot, fee, mk, cs = load()
    print(f"{len(mk)} crypto markets, {len(cs)} with candles; series {sorted(set(m['series'] for m in mk.values()))}")
    basis_check(spot, mk)
    # pick the volatility multiplier on TRAIN only (Brier), then judge it on TEST
    best = None
    for w in (24, 72):
        for base, cl in ((phi, "normal"), (t4_cdf, "fat-tail")):
            for k in (0.8, 1.0, 1.2, 1.4, 1.7):
                (trm, trk), (tem, tek), hz = brier_split(spot, mk, cs, w, scaled(base, k))
                print(f"W={w:3d} {cl:8s} vol x{k:.1f} | TRAIN Brier model {trm:.4f} vs mkt {trk:.4f} | "
                      f"TEST model {tem:.4f} vs mkt {tek:.4f}")
                if best is None or trm < best[0]:
                    best = (trm, w, base, cl, k, hz)
    _, w, base, cl, k, hz = best
    print(f"\nBEST ON TRAIN: W={w} {cl} vol x{k}. TEST Brier by time left (model vs market):")
    for b in ("<3h", "3-12h", "12-48h"):
        if b in hz:
            print(f"   {b:7s} model {hz[b][0]:.4f} vs mkt {hz[b][1]:.4f}  ({hz[b][2]} prices)")
    print()
    for w, cdf, cl in ((w, scaled(base, k), f"{cl} x{k}"),):
        for _ in (0,):
            for thr in (0.03, 0.06, 0.10, 0.15):
                bets = trade(spot, mk, cs, fee, w, cdf, thr)
                tr = [b for b in bets if b[0] < SPLIT_TS]
                te = [b for b in bets if b[0] >= SPLIT_TS]
                print(f"W={w:3d} {cl:8s} edge>{thr:.2f}")
                print("   TRAIN", summarize(tr))
                print("   TEST ", summarize(te))


if __name__ == "__main__":
    main()
