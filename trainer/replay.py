"""Training ground, step 2: replay bots over the downloaded past (trainer/data/history.db).

Every bot gets its own $100 and the SAME rules as the live run (run.py): the same market filter (spread, volume,
0.5-48h left), the 21c floor, $3/trade, $6/event, $50 deployed, one entry per market, hold to settlement,
Kalshi's taker fee for the market's series. It steps through time one hour at a time, like a live bot that only
woke up once an hour.

How it is kinder than real life (so a bot that fails HERE is very unlikely to work live):
  - It buys at the hour's closing ask, with no order book depth. To stop it pretending it could buy thousands of
    contracts in a quiet market, one buy can take at most 10% of the contracts traded in that hour.
  - It only sees markets that ended up with 500+ contracts traded, a 30% sample of events.
Weather bots are NOT replayed: they need old NWS forecasts, which the free API doesn't keep.

Splits (the overfitting guard): TRAIN = markets that closed before 2026-10-16, TEST = from 2026-10-16 on.
Pick ideas on TRAIN, then trust only the ones that also hold up on TEST, which they never "saw".
"""
import bisect
import math
from array import array
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run  # noqa: E402  (only its constants and filter; importing it makes no network calls)
from fees import SUPPORTED, taker_fee  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "history.db")
SPLIT_TS = int(datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp())
FILL_SHARE = 0.10
STALE_S = 2 * 3600  # a price older than 2 hours isn't a price


class Data:
    """Everything the replay needs, loaded once per process."""

    def __init__(self, path=DB):
        db = sqlite3.connect(path)
        self.fee = {s: (t, m) for s, t, m in db.execute("SELECT series, fee_type, fee_multiplier FROM fees")}
        self.mk = {}
        for t, ev, series, close, result, st, fl, cap, title in db.execute(
                "SELECT ticker, event, series, close_ts, result, strike_type, floor_strike, cap_strike, title FROM markets"):
            self.mk[t] = dict(ticker=t, event=ev, series=series, close=close, result=result, strike_type=st,
                              floor_strike=fl, cap_strike=cap, title=title or "")
        # ticker -> (ts, bid, ask, cumulative volume) as compact arrays (5.7M candles: ~200MB instead of ~1GB).
        # A missing bid/ask is stored as -1.
        self.c = {}
        cur = None
        for t, ts, bid, ask, vol in db.execute("SELECT ticker, ts, yes_bid, yes_ask, volume FROM candles ORDER BY ticker, ts"):
            if t != cur:
                cur, row = t, (array("q"), array("d"), array("d"), array("d"))
                self.c[t] = row
            row[0].append(ts)
            row[1].append(-1.0 if bid is None else bid)
            row[2].append(-1.0 if ask is None else ask)
            row[3].append((row[3][-1] if row[3] else 0.0) + (vol or 0.0))
        self.c = {t: v for t, v in self.c.items() if t in self.mk}
        if not self.c:
            raise SystemExit("no data yet: run trainer/download.py first")
        first = min(v[0][0] for v in self.c.values())
        last = max(m["close"] for m in self.mk.values())
        self.hours = list(range(first - first % 3600 + 3600, last + 1, 3600))
        # which markets could be live at each hour: close within the next 48h
        self.by_close = sorted(self.mk.values(), key=lambda m: m["close"])
        self.closes = [m["close"] for m in self.by_close]

    def universe(self, T):
        """The tradable markets at hour T, shaped like run.shape() makes them live."""
        lo = bisect.bisect_left(self.closes, T + int(run.MIN_HOURS * 3600))
        hi = bisect.bisect_right(self.closes, T + int(run.MAX_HOURS * 3600))
        out, events = [], {}
        for m in self.by_close[lo:hi]:
            c = self.c.get(m["ticker"])
            if not c:
                continue
            i = bisect.bisect_right(c[0], T) - 1
            if i < 0 or T - c[0][i] > STALE_S or c[1][i] < 0 or c[2][i] < 0:
                continue
            bid, ask = c[1][i], c[2][i]
            j = bisect.bisect_right(c[0], T - 86400) - 1
            vol24 = c[3][i] - (c[3][j] if j >= 0 else 0.0)
            hour_vol = c[3][i] - (c[3][i - 1] if i > 0 else 0.0)
            x = {"ticker": m["ticker"], "event": m["event"], "title": m["title"], "yes_bid": bid, "yes_ask": ask,
                 "no_bid": round(1 - ask, 4), "no_ask": round(1 - bid, 4), "volume_24h": vol24,
                 "hours_left": (m["close"] - T) / 3600, "now": T, "strike_type": m["strike_type"],
                 "floor_strike": m["floor_strike"], "cap_strike": m["cap_strike"], "rules": "",
                 "_hist": [(c[0][k], (c[1][k] + c[2][k]) / 2) for k in range(max(0, i - 48), i)
                           if c[1][k] >= 0 and c[2][k] >= 0],
                 "_hour_vol": hour_vol, "_series": m["series"], "_close": m["close"], "_result": m["result"]}
            if ask > 0:
                events.setdefault(m["event"], []).append(
                    {"strike_type": m["strike_type"], "floor_strike": m["floor_strike"], "cap_strike": m["cap_strike"],
                     "mid": (bid + ask) / 2})
            out.append(x)
        for x in out:
            x["siblings"] = events.get(x["event"], [])
        return [x for x in out if run.tradable(x)]


def replay(bots, data, every_h=1):
    """Run each bot over the past. Returns {bot.key: [(close_ts, pnl, price, won)]} for settled bets."""
    st = {b.key: {"cash": run.START_CASH, "open": [], "held": set(), "dep": 0.0, "ev": {}, "done": []} for b in bots}
    for h, T in enumerate(data.hours):
        for b in bots:  # settle first
            s = st[b.key]
            if s["open"]:
                keep = []
                for p in s["open"]:
                    if p["close"] <= T:
                        pay = p["n"] if p["side"] == p["result"] else 0.0
                        s["cash"] += pay
                        s["dep"] -= p["cost"]
                        s["ev"][p["event"]] -= p["cost"]
                        s["done"].append((p["close"], pay - p["cost"], p["price"], pay > 0))
                    else:
                        keep.append(p)
                s["open"] = keep
        if h % every_h:
            continue
        uni = data.universe(T)
        for b in bots:
            s = st[b.key]
            if run.MAX_DEPLOYED - s["dep"] < run.MIN_BUDGET or s["cash"] < run.MIN_BUDGET:
                continue
            for m in uni:
                if m["ticker"] in s["held"]:
                    continue
                wish = b.decide(m, m["_hist"])
                if not wish:
                    continue
                side, max_price = wish[0], wish[1]
                ask = m[f"{side}_ask"]
                if ask < run.MIN_PRICE or ask > max_price or ask >= 1:
                    continue
                fee_type, mult = data.fee.get(m["_series"], ("unknown", 1.0))
                if fee_type not in SUPPORTED:
                    continue
                stake = wish[2] if len(wish) > 2 else b.max_per_trade
                budget = min(stake, b.max_per_trade, run.MAX_PER_EVENT - s["ev"].get(m["event"], 0.0),
                             run.MAX_DEPLOYED - s["dep"], s["cash"])
                if budget < run.MIN_BUDGET:
                    continue
                n = min(int(budget / ask), int(m["_hour_vol"] * FILL_SHARE))
                while n > 0 and n * ask + taker_fee([(ask, n)], mult) > budget + 1e-9:
                    n -= 1
                if n <= 0:
                    continue
                cost = n * ask + taker_fee([(ask, n)], mult)
                s["cash"] -= cost
                s["dep"] += cost
                s["ev"][m["event"]] = s["ev"].get(m["event"], 0.0) + cost
                s["held"].add(m["ticker"])
                s["open"].append({"close": m["_close"], "result": m["_result"], "side": side, "n": n, "cost": cost,
                                  "event": m["event"], "price": ask})
                if run.MAX_DEPLOYED - s["dep"] < run.MIN_BUDGET:
                    break
    return {k: v["done"] for k, v in st.items()}


def stats(bets):
    """(n, total, per bet, t-score, win rate, avg price)"""
    n = len(bets)
    if n == 0:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0
    pnl = [b[1] for b in bets]
    tot, avg = sum(pnl), sum(pnl) / n
    sd = math.sqrt(sum((x - avg) ** 2 for x in pnl) / (n - 1)) if n > 1 else 0.0
    t = avg / (sd / math.sqrt(n)) if sd else 0.0
    return n, tot, avg, t, sum(b[3] for b in bets) / n, sum(b[2] for b in bets) / n


def split(bets):
    return [b for b in bets if b[0] < SPLIT_TS], [b for b in bets if b[0] >= SPLIT_TS]
