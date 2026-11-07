"""The bots. Each one is FROZEN: its rules are fingerprinted the first time it runs, and if the rules
change later the run stops and asks for a new version number. A new version starts a fresh $100 record;
old versions keep theirs.

Every bot sees the same markets (the shared filter in run.py) and pays the same fees. A bot returns
(side, max_price) to buy, or None. Bots only BUY and hold to settlement: selling early needs someone
posting a bid at a price you like, which a paper test can't promise.
"""
import hashlib
import inspect
import math
import random

import weather


def mid(m):
    return (m["yes_bid"] + m["yes_ask"]) / 2


class Bot:
    name = "base"
    version = 1
    params = {}
    max_per_trade = 3.00  # the engine's per-trade cap for this bot; a bot may ask for less, never more

    def __init__(self, version=None, retired=False):
        # retired: keeps its record in the report and its open bets still settle, but it never buys again.
        if version is not None:
            self.version = version
        self.retired = retired

    # decide() returns None, (side, max_price), or (side, max_price, stake_dollars, note). Don't edit decide()'s
    # text below: cash-v1's fingerprint is that exact source, so any change would void cash's record.

    def decide(self, m, history):
        """m: market dict from run.py. history: [(ts, yes_mid)] older snapshots of this market, oldest first."""
        return None

    @property
    def key(self):
        return f"{self.name}-v{self.version}"

    def fingerprint(self):
        # The helpers are part of the rules too, so editing them also counts as a rule change.
        src = inspect.getsource(type(self).decide) + inspect.getsource(mid) + inspect.getsource(move)
        src = src.replace("\r\n", "\n")  # same rules on Windows and on the GitHub runner
        return hashlib.sha256(f"{sorted(self.params.items())}|{src}".encode()).hexdigest()[:16]


class Cash(Bot):
    """Never trades. Every other bot has to beat this after fees, or it's worse than doing nothing."""
    name = "cash"


class RandomPick(Bot):
    """Buys a random side on about 1 in 20 markets. If a 'real' strategy can't beat this, it has no edge."""
    name = "random"
    params = {"chance": 0.05, "max_price": 0.90}

    def decide(self, m, history):
        rng = random.Random(f"{self.key}|{m['ticker']}")  # same answer for a market every tick
        if rng.random() >= self.params["chance"]:
            return None
        side = rng.choice(["yes", "no"])
        return side, self.params["max_price"]


class Favorite(Bot):
    """Favorite-longshot bias: in betting markets, heavy favorites tend to win a bit more often than their
    price says and longshots a bit less. Buys the side priced 85-95c in the last 24h before close."""
    name = "favorite"
    params = {"min_ask": 0.85, "max_ask": 0.95, "hours_left": 24}

    def decide(self, m, history):
        if m["hours_left"] > self.params["hours_left"]:
            return None
        for side in ("yes", "no"):
            ask = m[f"{side}_ask"]
            if self.params["min_ask"] <= ask <= self.params["max_ask"]:
                return side, self.params["max_ask"]
        return None


def move(history, now_mid, min_age_h, max_age_h, now_ts):
    """Price change since the snapshot between min_age_h and max_age_h ago, or None if we don't have one."""
    old = [mid_ for ts, mid_ in history if min_age_h * 3600 <= now_ts - ts <= max_age_h * 3600]
    return None if not old else now_mid - old[0]


class Momentum(Bot):
    """Follows a move: if the YES price rose 10c+ over the last ~6 hours, buy YES (fell: buy NO)."""
    name = "momentum"
    params = {"min_move": 0.10, "from_h": 5, "to_h": 8, "max_price": 0.85}

    def decide(self, m, history):
        d = move(history, mid(m), self.params["from_h"], self.params["to_h"], m["now"])
        if d is None or abs(d) < self.params["min_move"]:
            return None
        return ("yes" if d > 0 else "no"), self.params["max_price"]


class Reversion(Bot):
    """Bets a big fast move overshot: if YES rose 15c+ in ~6 hours, buy NO (fell: buy YES).
    The opposite of Momentum on purpose. At most one of the two can be right."""
    name = "reversion"
    params = {"min_move": 0.15, "from_h": 5, "to_h": 8, "max_price": 0.85}

    def decide(self, m, history):
        d = move(history, mid(m), self.params["from_h"], self.params["to_h"], m["now"])
        if d is None or abs(d) < self.params["min_move"]:
            return None
        return ("no" if d > 0 else "yes"), self.params["max_price"]


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bracket_prob(strike_type, floor, cap, mu, sd):
    """Chance the day's high lands in the market's range, if the high ~ Normal(mu, sd) and is reported as a
    whole degree. Kalshi's wording (checked 2026-11-03): 'greater' floor=77 means >77 (78+), 'less' cap=70
    means <70 (69 or lower), 'between' 76-77 includes both ends."""
    if strike_type == "greater" and floor is not None:
        return 1 - phi((floor + 0.5 - mu) / sd)
    if strike_type == "less" and cap is not None:
        return phi((cap - 0.5 - mu) / sd)
    if strike_type == "between" and floor is not None and cap is not None:
        return phi((cap + 0.5 - mu) / sd) - phi((floor - 0.5 - mu) / sd)
    return None


def market_view(siblings):
    """What the whole event's prices say: (center, spread) in degrees, or None if the prices don't add up.
    siblings: every market in the event as dicts with strike_type, floor_strike, cap_strike, mid (YES mid)."""
    pts = []
    for s in siblings:
        t, f, c = s.get("strike_type"), s.get("floor_strike"), s.get("cap_strike")
        x = (f + c) / 2 if t == "between" and f is not None and c is not None else \
            c - 1.5 if t == "less" and c is not None else f + 1.5 if t == "greater" and f is not None else None
        if x is not None:
            pts.append((x, s["mid"]))
    total = sum(p for _, p in pts)
    if len(pts) < 4 or not 0.8 <= total <= 1.3:
        return None
    center = sum(x * p for x, p in pts) / total
    spread = (sum(p * (x - center) ** 2 for x, p in pts) / total) ** 0.5
    return center, spread


class WeatherForecast(Bot):
    """The first bot that bets on information instead of price patterns. For tomorrow's high-temperature
    markets, it reads the free National Weather Service forecast for the exact station the market settles on.

    It takes HOW SURE to be from Kalshi's own prices (traders know tomorrow's weather well; a first draft that
    used its own fixed +/-3.5F kept betting against the bracket the forecast pointed at) and only WHERE the
    middle is from the NWS forecast. So it trades only when NWS and the market disagree on the temperature by
    1.5F or more, and buys the side that disagreement favours if it's clearly cheap after the fee.
    Bigger edge = bigger bet (quarter-Kelly on a $100 bankroll, max $5)."""
    name = "weather"
    max_per_trade = 5.00
    params = {"min_shift_f": 1.5, "min_sd_f": 1.5, "min_edge": 0.10, "min_ask": 0.21, "max_ask": 0.90,
              "kelly": 0.25, "bank": 100.0, "max_stake": 5.00}

    def decide(self, m, history):
        P = self.params
        if not m["event"].startswith("KXHIGH"):
            return None
        station, date = weather.station_from_rules(m.get("rules")), weather.market_date(m["ticker"])
        if not station or not date:
            return None
        highs, tz = weather.forecast_highs(station)
        if not highs or date not in highs or date <= weather.local_today(tz):
            return None  # tomorrow only: today's high may already be on the thermometer, which a forecast ignores
        view = market_view(m.get("siblings") or [])
        if not view:
            return None
        mu, (mkt_mu, mkt_sd) = highs[date], view
        if abs(mu - mkt_mu) < P["min_shift_f"]:
            return None  # NWS agrees with the market: nothing to bet on
        p_yes = bracket_prob(m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), mu,
                             max(mkt_sd, P["min_sd_f"]))
        if p_yes is None:
            return None
        best = None
        for side, p in (("yes", p_yes), ("no", 1 - p_yes)):
            ask = m[f"{side}_ask"]
            if not P["min_ask"] <= ask <= P["max_ask"]:
                continue
            edge = p - ask - 0.07 * ask * (1 - ask)
            if edge >= P["min_edge"] and (best is None or edge > best[0]):
                best = (edge, side, p, ask)
        if not best:
            return None
        edge, side, p, ask = best
        stake = min(P["max_stake"], P["bank"] * P["kelly"] * (p - ask) / (1 - ask))
        max_price = round(min(P["max_ask"], p - P["min_edge"]), 2)
        return side, max_price, round(stake, 2), (f"{station} {date} NWS {mu}F vs market {mkt_mu:.1f}F "
                                                  f"(+/-{mkt_sd:.1f}), chance {p:.2f}, edge {edge:.2f}")

    def fingerprint(self):
        src = "".join(inspect.getsource(f) for f in (type(self).decide, phi, bracket_prob, market_view,
                                                     weather.station_from_rules,
                                                     weather.market_date, weather.forecast_highs, weather.local_today))
        src = src.replace("\r\n", "\n")
        return hashlib.sha256(f"{sorted(self.params.items())}|{self.max_per_trade}|{src}".encode()).hexdigest()[:16]


# ---- The army (2026-11-06) ---------------------------------------------------------------------------------
# Hundreds of small variations, each with its own $100, the same limits and the same fees as the core bots.
# The point isn't that more bots make more money (bots with no edge just pay more fees). It's to search lots of
# ideas at once and see which ones survive. Because so many are tested, a few WILL look great by luck, so:
#   - 30 "luck" bots pick at random. Whatever the best of them does is what pure luck looks like.
#   - The "ready for more money" bar gets stricter with the number of bots (see run.t_bar).

CRYPTO = ("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXSHIBA", "KXBNB", "KXADA", "KXLTC", "KXAVAX", "KXLINK")
FINANCE = ("KXINX", "KXNASDAQ", "KXDOW", "KXWTI", "KXBRENT", "KXGOLD", "KXSILVER", "KXCOPPER", "KXNATGAS", "KXUSDJPY",
           "KXEURUSD", "KXGBPUSD", "KXTNOTE", "KXTREAS", "KXVIX", "KXRUT")
SPORTS = ("KXNFL", "KXNBA", "KXNHL", "KXMLB", "KXNCAA", "KXWNBA", "KXMLS", "KXEPL", "KXUCL", "KXUEL", "KXATP", "KXWTA",
          "KXUFC", "KXPGA", "KXLPGA", "KXF1", "KXNASCAR", "KXCS2", "KXLOL", "KXVALORANT", "KXDOTA", "KXARGPREM",
          "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA", "KXLIGUE1", "KXBOXING", "KXEUROLEAGUE", "KXKBO", "KXNPB", "KXAFL",
          "KXNRL", "KXCRICKET", "KXIPL", "KXT20", "KXBRASILEIRO", "KXLIGAMX", "KXEREDIVISIE", "KXSCOTPREM", "KXSUPERLIG",
          "KXCBA", "KXKHL", "KXSHL", "KXAHL", "KXCOLLEGE", "KXCFB", "KXCBB", "KXMMA", "KXDARTS", "KXSNOOKER",
          "KXJLEAGUE", "KXALEAGUE", "KXKLEAGUE", "KXNWSL", "KXUSL", "KXCSL", "KXSAUDI", "KXCHAMPIONSHIP", "KXWCQ")


def category(event):
    """Rough market type from the series code: weather, crypto, finance, sports, or other."""
    series = event.partition("-")[0]
    if series.startswith(("KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP")):
        return "weather"
    if series.startswith(CRYPTO):
        return "crypto"
    if series.startswith(FINANCE):
        return "finance"
    if series.startswith(SPORTS):
        return "sports"
    return "other"


class Soldier:
    """Mixin for army bots: its own name and params, version 1, never retired unless the lineup says so."""
    army = True

    def __init__(self, name, params):
        super().__init__()
        self.name, self.params = name, dict(params)


class Luck(Soldier, RandomPick):
    """Same coin flips as the random control, each with its own seed (the seed is its name)."""


class Band(Soldier, Bot):
    """Buys whichever side is priced between lo and hi (the cheaper one if both are), within `hours_left` of the
    close, in one market type or any. A band of 0.85-0.95 is the favorite idea; 0.21-0.33 tests whether some
    market types underprice longshots; 0.45-0.55 is a coin flip with fees, a second kind of luck baseline."""

    def decide(self, m, history):
        P = self.params
        if m["hours_left"] > P["hours_left"]:
            return None
        if P["cat"] != "any" and category(m["event"]) != P["cat"]:
            return None
        picks = [(m[f"{s}_ask"], s) for s in ("yes", "no") if P["lo"] <= m[f"{s}_ask"] <= P["hi"]]
        if not picks:
            return None
        ask, side = min(picks)
        return side, P["hi"]

    def fingerprint(self):
        src = "".join(inspect.getsource(f) for f in (type(self).decide, category)).replace("\r\n", "\n")
        return hashlib.sha256(f"{sorted(self.params.items())}|{CRYPTO}|{FINANCE}|{SPORTS}|{src}".encode()).hexdigest()[:16]


class Weatherman(Soldier, WeatherForecast):
    """The weather bot with different settings: how big a disagreement with the market it needs, how much edge
    after the fee, and the smallest spread it will assume."""


def pct(x):
    return f"{round(x * 100):02d}"


def build_army():
    army = [Luck(f"luck-{i:02d}", RandomPick.params) for i in range(1, 31)]
    for lo in (0.65, 0.70, 0.75, 0.80, 0.85, 0.90):              # favorites, any market: 60
        for width in (0.05, 0.10):
            hi = min(round(lo + width, 2), 0.97)
            for h in (2, 6, 12, 24, 48):
                army.append(Band(f"fav-{pct(lo)}-{pct(hi)}-{h}h", {"lo": lo, "hi": hi, "hours_left": h, "cat": "any"}))
    for cat in ("sports", "crypto", "finance", "weather", "other"):
        for lo in (0.70, 0.80, 0.88):                             # favorites by market type: 45
            for h in (4, 12, 48):
                hi = round(lo + 0.08, 2)
                army.append(Band(f"{cat}-{pct(lo)}-{pct(hi)}-{h}h", {"lo": lo, "hi": hi, "hours_left": h, "cat": cat}))
        for lo, hi in ((0.21, 0.33), (0.33, 0.45), (0.45, 0.55)):  # longshots + coin flips by market type: 30
            for h in (6, 48):
                army.append(Band(f"{cat}-{pct(lo)}-{pct(hi)}-{h}h", {"lo": lo, "hi": hi, "hours_left": h, "cat": cat}))
    for shift in (1.0, 1.5, 2.0, 2.5, 3.0):                       # weather settings: 30
        for edge in (0.05, 0.10, 0.15):
            for sd in (1.0, 2.0):
                army.append(Weatherman(f"wx-s{pct(shift / 10)}-e{pct(edge)}-sd{pct(sd / 10)}",
                                       WeatherForecast.params | {"min_shift_f": shift, "min_edge": edge, "min_sd_f": sd}))
    return army


# 2026-11-03 review (~400 settled trades): every bot that bought cheap longshots (20c or less) bled money on them,
# $161 in total. momentum and reversion lost and can't work on GitHub's sparse schedule anyway; random-v1 is out
# of cash. They're retired (records kept, open bets still settle). random-v2 is the control under the new 20c floor.
CORE = [Cash(), RandomPick(retired=True), Favorite(), Momentum(retired=True), Reversion(retired=True),
        RandomPick(version=2), WeatherForecast()]
ARMY = build_army()
BOTS = CORE + ARMY
