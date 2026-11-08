"""New idea families (2026-11-08). Every one is tried in the training ground (trainer/) first; only the ones that
survive there go live, through strategies.RECRUITS. Same frozen-rules deal as every bot: params + the source of
decide() and its helpers are fingerprinted, so changing one means a new version and a fresh $100 record.

Nothing here makes network calls: a bot only reads the market dict and its price history.
"""
import hashlib
import inspect
from datetime import datetime, timezone

from strategies import CRYPTO, FINANCE, SPORTS, Bot, category


def mid(m):
    return (m["yes_bid"] + m["yes_ask"]) / 2


def price_then(history, now_ts, hours):
    """The YES mid from the snapshot closest to `hours` ago (within 40% of that), or None."""
    want, best = now_ts - hours * 3600, None
    for ts, x in history:
        if abs(ts - want) <= 0.4 * hours * 3600 and (best is None or abs(ts - want) < abs(best[0] - want)):
            best = (ts, x)
    return None if best is None else best[1]


def in_band(m, lo, hi, side=None):
    """The cheapest side priced lo..hi (only `side` if given) -> (ask, side) or None."""
    picks = [(m[f"{s}_ask"], s) for s in ((side,) if side else ("yes", "no")) if lo <= m[f"{s}_ask"] <= hi]
    return min(picks) if picks else None


class Recruit(Bot):
    army = True
    recruit = True
    helpers = (mid, in_band, category)

    def __init__(self, name, params):
        super().__init__()
        self.name, self.params = name, dict(params)

    def fingerprint(self):
        src = "".join(inspect.getsource(f) for f in (type(self).decide, *self.helpers)).replace("\r\n", "\n")
        blob = f"{sorted(self.params.items())}|{self.max_per_trade}|{CRYPTO}|{FINANCE}|{SPORTS}|{src}"
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def family(self):
        return {"SideBand": "one side only", "LiquidBand": "busy markets", "SeriesBand": "one market type",
                "Trend": "moving favorite", "Mover": "price jump", "EventTop": "bracket favorite",
                "EventNotTop": "bracket fade", "Clock": "time of day"}.get(type(self).__name__, "new idea")

    def describe(self):
        P, c = self.params, lambda x: f"{x * 100:.0f}c"
        where = "any market" if P.get("cat", "any") == "any" else f"{P['cat']} markets"
        k = type(self).__name__
        if k == "SideBand":
            return f"Buys only {P['side'].upper()}, priced {c(P['lo'])}-{c(P['hi'])}, in {where}, within {P['hours_left']}h of closing."
        if k == "LiquidBand":
            return (f"Buys the side priced {c(P['lo'])}-{c(P['hi'])} only in busy markets ({P['min_vol']:,}+ traded in a day, "
                    f"spread {c(P['max_spread'])} or less), within {P['hours_left']}h of closing.")
        if k == "SeriesBand":
            return f"Buys the side priced {c(P['lo'])}-{c(P['hi'])} only in {self.name.split('-')[0].upper()} markets, within {P['hours_left']}h of closing."
        if k == "Trend":
            how = "has risen" if P["dir"] == "with" else "has slipped"
            return f"Buys a {c(P['lo'])}-{c(P['hi'])} favorite whose price {how} {c(P['move'])}+ in the last {P['lookback_h']}h."
        if k == "Mover":
            how = "the side that rose" if P["mode"] == "follow" else "the side that fell"
            return f"When a price moves {c(P['move'])}+ in {P['lookback_h']}h, buys {how} (if it's {c(P['lo'])}-{c(P['hi'])})."
        if k == "EventTop":
            return (f"In events with {P['min_brackets']}+ brackets, buys YES on the market's favourite bracket if it's "
                    f"{c(P['lo'])}-{c(P['hi'])}, within {P['hours_left']}h of closing.")
        if k == "EventNotTop":
            return (f"In events with {P['min_brackets']}+ brackets, buys NO on a bracket that isn't the favourite, "
                    f"if that NO is {c(P['lo'])}-{c(P['hi'])}, within {P['hours_left']}h of closing.")
        if k == "Clock":
            when = "weekends" if P["weekend"] else "weekdays"
            return f"Buys {c(P['lo'])}-{c(P['hi'])} favorites only {P['from_h']:02d}:00-{P['to_h']:02d}:00 UTC on {when}."
        return self.__doc__.split("\n")[0]


class SideBand(Recruit):
    """Buys only YES (or only NO) when it's priced lo-hi near the close. Tests whether one side is mispriced."""

    def decide(self, m, history):
        P = self.params
        if m["hours_left"] > P["hours_left"] or (P["cat"] != "any" and category(m["event"]) != P["cat"]):
            return None
        pick = in_band(m, P["lo"], P["hi"], P["side"])
        return pick and (pick[1], P["hi"])


class LiquidBand(Recruit):
    """Favorites only in busy markets with a tight spread, where the price is most likely to be 'real'."""

    def decide(self, m, history):
        P = self.params
        if (m["hours_left"] > P["hours_left"] or m["volume_24h"] < P["min_vol"]
                or m["yes_ask"] - m["yes_bid"] > P["max_spread"]):
            return None
        pick = in_band(m, P["lo"], P["hi"])
        return pick and (pick[1], P["hi"])


class SeriesBand(Recruit):
    """Favorites in ONE kind of market only (e.g. NBA games, Bitcoin price ranges), to find where the bias lives."""

    def decide(self, m, history):
        P = self.params
        if m["hours_left"] > P["hours_left"] or not m["event"].startswith(P["series"]):
            return None
        pick = in_band(m, P["lo"], P["hi"])
        return pick and (pick[1], P["hi"])


class Trend(Recruit):
    """A favorite that has been moving: 'with' buys it if the price rose toward it, 'against' if it slipped."""
    helpers = (mid, in_band, category, price_then)

    def decide(self, m, history):
        P = self.params
        if m["hours_left"] > P["hours_left"]:
            return None
        pick = in_band(m, P["lo"], P["hi"])
        if not pick:
            return None
        then = price_then(history, m["now"], P["lookback_h"])
        if then is None:
            return None
        rise = mid(m) - then if pick[1] == "yes" else then - mid(m)  # how much THIS side gained
        ok = rise >= P["move"] if P["dir"] == "with" else rise <= -P["move"]
        return (pick[1], P["hi"]) if ok else None


class Mover(Recruit):
    """Any market whose price jumped by `move` in `lookback_h` hours: 'follow' buys the side that rose, 'fade' the
    side that fell. The old momentum/reversion bots starved on sparse runs; the 5-minute loop fixes that."""
    helpers = (mid, price_then)

    def decide(self, m, history):
        P = self.params
        then = price_then(history, m["now"], P["lookback_h"])
        if then is None:
            return None
        d = mid(m) - then
        if abs(d) < P["move"]:
            return None
        rose = "yes" if d > 0 else "no"
        side = rose if P["mode"] == "follow" else ("no" if rose == "yes" else "yes")
        if not P["lo"] <= m[f"{side}_ask"] <= P["hi"]:
            return None
        return side, P["hi"]


class EventTop(Recruit):
    """In events with several brackets (temperature, BTC ranges, totals), buys YES on the bracket the market
    thinks is most likely, if it's priced lo-hi. Tests whether crowds under-back their own favourite outcome."""

    def decide(self, m, history):
        P = self.params
        sib = m.get("siblings") or []
        if len(sib) < P["min_brackets"] or m["hours_left"] > P["hours_left"]:
            return None
        if mid(m) < max(s["mid"] for s in sib) - 1e-9:
            return None
        return ("yes", P["hi"]) if P["lo"] <= m["yes_ask"] <= P["hi"] else None


class EventNotTop(Recruit):
    """In multi-bracket events, buys NO on a bracket that ISN'T the favourite, when that NO is priced lo-hi."""

    def decide(self, m, history):
        P = self.params
        sib = m.get("siblings") or []
        if len(sib) < P["min_brackets"] or m["hours_left"] > P["hours_left"]:
            return None
        if mid(m) >= max(s["mid"] for s in sib) - 1e-9:
            return None
        return ("no", P["hi"]) if P["lo"] <= m["no_ask"] <= P["hi"] else None


class Clock(Recruit):
    """Favorites only at certain times (UTC hour window, weekday or weekend). Prices may be sloppier at night."""

    def decide(self, m, history):
        P = self.params
        t = datetime.fromtimestamp(m["now"], timezone.utc)
        if not P["from_h"] <= t.hour < P["to_h"] or (t.weekday() >= 5) != P["weekend"]:
            return None
        if m["hours_left"] > P["hours_left"]:
            return None
        pick = in_band(m, P["lo"], P["hi"])
        return pick and (pick[1], P["hi"])


def p2(x):
    return f"{round(x * 100):02d}"


# Series groups for SeriesBand: (label, event prefixes). Chosen from what trades most on Kalshi, before looking at
# any results, so the choice itself can't be overfit.
SERIES = [("nba", ("KXNBA",)), ("nfl", ("KXNFL",)), ("nhl", ("KXNHL",)), ("mlb", ("KXMLB",)),
          ("ncaaf", ("KXNCAAF", "KXCFB")), ("ncaab", ("KXNCAAB", "KXCBB")), ("tennis", ("KXATP", "KXWTA")),
          ("soccer", ("KXEPL", "KXUCL", "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA", "KXLIGUE1", "KXMLS")),
          ("esports", ("KXCS2", "KXLOL", "KXVALORANT", "KXDOTA")), ("btc", ("KXBTC",)), ("eth", ("KXETH",)),
          ("sp500", ("KXINX",)), ("nasdaq", ("KXNASDAQ",)), ("oil", ("KXWTI", "KXBRENT")), ("fx", ("KXUSDJPY", "KXEURUSD"))]


def build_recruits():
    out = []
    for side in ("yes", "no"):                                                     # 150
        for lo, hi in ((0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.92), (0.92, 0.97)):
            for h in (6, 24, 48):
                for cat in ("any", "sports", "crypto", "finance", "other"):
                    out.append(SideBand(f"{side}only-{cat}-{p2(lo)}-{p2(hi)}-{h}h",
                                        {"side": side, "lo": lo, "hi": hi, "hours_left": h, "cat": cat}))
    for lo, hi in ((0.65, 0.75), (0.75, 0.85), (0.85, 0.95)):                       # 36
        for vol in (1000, 10000, 100000):
            for spread in (0.01, 0.03):
                for h in (12, 48):
                    out.append(LiquidBand(f"liquid-{p2(lo)}-{p2(hi)}-v{vol}-s{p2(spread)}-{h}h",
                                          {"lo": lo, "hi": hi, "min_vol": vol, "max_spread": spread, "hours_left": h}))
    for label, prefixes in SERIES:                                                  # 90
        for lo, hi in ((0.70, 0.80), (0.80, 0.90), (0.88, 0.96)):
            for h in (12, 48):
                out.append(SeriesBand(f"{label}-{p2(lo)}-{p2(hi)}-{h}h",
                                      {"series": prefixes, "lo": lo, "hi": hi, "hours_left": h}))
    for lo, hi in ((0.65, 0.80), (0.80, 0.95)):                                     # 24
        for lb in (2, 6, 12):
            for move in (0.03, 0.08):
                for d in ("with", "against"):
                    out.append(Trend(f"trend-{d}-{p2(lo)}-{p2(hi)}-{lb}h-m{p2(move)}",
                                     {"lo": lo, "hi": hi, "lookback_h": lb, "move": move, "dir": d, "hours_left": 48}))
    for lb in (1, 3, 6, 12):                                                        # 24
        for move in (0.05, 0.10, 0.20):
            for mode in ("follow", "fade"):
                out.append(Mover(f"{mode}-{lb}h-m{p2(move)}",
                                 {"lookback_h": lb, "move": move, "mode": mode, "lo": 0.25, "hi": 0.85}))
    for k in (3, 5):                                                                # 12 + 12
        for lo, hi in ((0.40, 0.60), (0.60, 0.80), (0.80, 0.95)):
            for h in (12, 48):
                out.append(EventTop(f"top-k{k}-{p2(lo)}-{p2(hi)}-{h}h",
                                    {"min_brackets": k, "lo": lo, "hi": hi, "hours_left": h}))
                out.append(EventNotTop(f"nottop-k{k}-{p2(lo)}-{p2(hi)}-{h}h",
                                       {"min_brackets": k, "lo": lo, "hi": hi, "hours_left": h}))
    for f, t in ((0, 6), (6, 12), (12, 18), (18, 24)):                              # 16
        for weekend in (False, True):
            for lo, hi in ((0.65, 0.80), (0.80, 0.92)):
                out.append(Clock(f"clock-{f:02d}-{t:02d}-{'wkend' if weekend else 'wkday'}-{p2(lo)}-{p2(hi)}",
                                 {"from_h": f, "to_h": t, "weekend": weekend, "lo": lo, "hi": hi, "hours_left": 24}))
    return out
