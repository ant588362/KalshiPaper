"""weather-v2 (2026-11-09): SAME-DAY high-temperature bots that use what the thermometer already says.

Why (GitHub study 2026-11-09, JHenzi/weatherbots + suislanchez weather bot): serious weather bots
  1) blend many forecasts (no single source beat the blend; 7 of 8 sources ran COLD),
  2) trade the same day, when forecasts are much sharper (MAE ~3.3F in the morning -> ~1.5F by 2pm),
  3) and the day's high can't end up below the highest temperature already recorded.
weather-v1 used one forecast (NWS) for TOMORROW only. weather.py is left untouched: it's in v1's fingerprint.

Model: today's high H = max(already-recorded max O, rest-of-day X), X ~ Normal(blend + bias, sd).
So P(H <= x) = 0 if x < O, else Phi((x - mu) / sd). Kalshi settles on The Weather Company's whole-degree reading
for the CLI station; we read NWS observations for the same station, so O is lowered by a safety margin.
After the late afternoon (local 17:00) the rest of the day adds ~nothing: X collapses onto O.

Data (all free, no key, checked live 2026-11-09):
  - api.weather.gov /stations/K???/observations?start=... (temperature in C, hourly METAR has tenths)
  - api.weather.gov NWS forecast (via weather.forecast_highs, read-only use)
  - api.open-meteo.com 6 weather models' daily max in F (free tier: non-commercial, <10k calls/day; cached 30 min)
Can't be backtested: old forecasts/observation snapshots aren't kept anywhere free. The live army is the test.
"""
import hashlib
import inspect
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import weather
from strategies import Bot, bracket_prob, market_view, phi

MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_seamless", "jma_seamless", "ukmo_seamless")
UA = {"user-agent": "kalshi-paper (github.com/ant588362/KalshiPaper)", "accept": "application/geo+json"}
_cache = {}  # (kind, key) -> (fetched_at, value)
_dead = set()


def _get(url, ttl, headers=UA):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if url in _dead:
        return None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
                val = json.loads(r.read())
            _cache[url] = (time.time(), val)
            return val
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1 + attempt)
    _dead.add(url)
    return None


def station_info(station):
    """(lat, lon, tz) for a K??? station, or None."""
    s = _get(f"{weather.ROOT}/stations/{station}", 86400)
    if not s:
        return None
    lon, lat = s["geometry"]["coordinates"]
    tz = s["properties"].get("timeZone")
    return (lat, lon, tz) if tz else None


def model_highs(lat, lon):
    """{date: [daily max F from each model that answered]} from Open-Meteo."""
    q = urllib.parse.urlencode({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "daily": "temperature_2m_max",
                                "temperature_unit": "fahrenheit", "timezone": "auto", "forecast_days": 2,
                                "models": ",".join(MODELS)})
    d = _get(f"https://api.open-meteo.com/v1/forecast?{q}", 1800, {"user-agent": UA["user-agent"]})
    if not d or "daily" not in d:
        return {}
    out = {}
    for i, day in enumerate(d["daily"]["time"]):
        vals = [d["daily"].get(f"temperature_2m_max_{m}", [None] * 9)[i] for m in MODELS]
        out[day] = [v for v in vals if v is not None]
    return out


def observed_max(station, tz):
    """(max F recorded so far today, local hour now, local date) from NWS observations, or (None, hour, date)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # the start time is in the URL, so one cache entry per station per local day; 10 min freshness
    o = _get(f"{weather.ROOT}/stations/{station}/observations?start={start}", 600)
    temps = []
    for f in (o or {}).get("features", []):
        c = (f.get("properties", {}).get("temperature") or {}).get("value")
        if c is not None:
            temps.append(c * 9 / 5 + 32)
    return (max(temps) if temps else None), now.hour + now.minute / 60, now.strftime("%Y-%m-%d")


def high_cdf(x, obs, mu, sd):
    """P(today's high <= x) when high = max(obs, X), X ~ Normal(mu, sd)."""
    if obs is not None and x < obs:
        return 0.0
    return phi((x - mu) / sd)


def today_bracket_prob(strike_type, floor, cap, obs, mu, sd):
    """Same Kalshi wording as strategies.bracket_prob ('greater' 77 = 78+, 'less' 70 = 69-, 'between' inclusive)."""
    F = lambda x: high_cdf(x, obs, mu, sd)  # noqa: E731
    if strike_type == "greater" and floor is not None:
        return 1 - F(floor + 0.5)
    if strike_type == "less" and cap is not None:
        return F(cap - 0.5)
    if strike_type == "between" and floor is not None and cap is not None:
        return F(cap + 0.5) - F(floor - 0.5)
    return None


class WeatherNow(Bot):
    """weather-v2 family. Params: bias_f (added to the blend; sources run cold), min_edge (after the fee),
    min_hour (local hour before which it doesn't trade today), days ('today' or 'both': tomorrow too, with the
    blend instead of NWS alone), margin_f (how far below the NWS reading the official max could be),
    tomorrow_sd_f + today_sd_f (error floors by local hour), late_hour (after it, the rest of the day can't add heat), min_sources."""
    army = True
    max_per_trade = 5.00
    base = {"bias_f": 0.0, "min_edge": 0.10, "min_hour": 10, "days": "today", "margin_f": 0.75, "tomorrow_sd_f": 2.8,
            "today_sd_f": ((12, 3.0), (15, 2.0), (25, 1.5)),
            "late_hour": 17, "late_sd_f": 0.6, "min_sources": 4, "min_ask": 0.21, "max_ask": 0.90,
            "kelly": 0.25, "bank": 100.0, "max_stake": 5.00}

    def __init__(self, name, params):
        super().__init__()
        self.name, self.params = name, dict(self.base) | dict(params)

    def decide(self, m, history):
        P = self.params
        if not m["event"].startswith("KXHIGH"):
            return None
        station, date = weather.station_from_rules(m.get("rules")), weather.market_date(m["ticker"])
        info = station_info(station) if station else None
        if not info or not date:
            return None
        lat, lon, tz = info
        obs, hour, today = observed_max(station, tz)
        if date < today or (date > today and P["days"] != "both"):
            return None
        if date == today and (hour < P["min_hour"] or obs is None):
            return None  # checked before fetching forecasts: keeps Open-Meteo calls well under its free daily limit
        srcs = list(model_highs(lat, lon).get(date, []))
        nws, _ = weather.forecast_highs(station)
        if nws and date in nws:
            srcs.append(float(nws[date]))
        if len(srcs) < P["min_sources"]:
            return None
        mu = statistics.mean(srcs) + P["bias_f"]
        # How far apart the sources are UNDERSTATES the real error (they share blind spots, e.g. fronts).
        # Floors from JHenzi's 6,864 graded forecasts: ~2.3F MAE the day before, ~1.5F by mid-afternoon.
        sd = max(P["tomorrow_sd_f"], statistics.pstdev(srcs))
        if date == today:
            if hour < P["min_hour"] or obs is None:
                return None
            sd = max(next(s for h, s in P["today_sd_f"] if hour < h), statistics.pstdev(srcs))
            o = obs - P["margin_f"]
            if hour >= P["late_hour"]:
                mu, sd = o, P["late_sd_f"]  # the rest of the evening won't beat the afternoon
            p_yes = today_bracket_prob(m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), o, mu, sd)
            why = f"obs max {obs:.1f}F at {hour:.0f}h, blend {mu:.1f}+/-{sd:.1f} ({len(srcs)} src)"
        else:
            view = market_view(m.get("siblings") or [])
            if view:
                sd = max(sd, view[1])
            p_yes = bracket_prob(m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), mu, sd)
            why = f"tomorrow blend {mu:.1f}+/-{sd:.1f} ({len(srcs)} src)"
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
        return side, max_price, round(stake, 2), f"{station} {date} {why}, chance {p:.2f}, edge {edge:.2f}"

    def fingerprint(self):
        fns = (type(self).decide, station_info, model_highs, observed_max, high_cdf, today_bracket_prob,
               phi, bracket_prob, market_view, weather.station_from_rules, weather.market_date,
               weather.forecast_highs)
        src = "".join(inspect.getsource(f) for f in fns).replace("\r\n", "\n")
        return hashlib.sha256(f"{sorted(self.params.items())}|{MODELS}|{self.max_per_trade}|{src}".encode()).hexdigest()[:16]


def build_weather2():
    # 2026-11-09 dry run: for TOMORROW the blend sat 2-4F BELOW the market in almost every city (grid-point models
    # run cold vs the station; weather-v1 lost Nov 5 the same way), so 'both' would bet against nearly every
    # bracket. All 16 trade TODAY only, where the recorded max is real information.
    out = []
    for bias in (0.0, 1.5):
        for edge in (0.08, 0.15):
            for min_hour in (10, 14):
                for margin in (0.5, 1.0):
                    out.append(WeatherNow(f"wx2-b{bias:g}-e{round(edge * 100):02d}-h{min_hour}-m{margin:g}",
                                          {"bias_f": bias, "min_edge": edge, "min_hour": min_hour,
                                           "margin_f": margin}))
    return out
