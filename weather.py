"""Free National Weather Service forecasts for the stations Kalshi's high-temperature markets settle on.

Checked live 2026-11-03:
- Each KXHIGH* market's rules name the station, e.g. "recorded at New York City (CLINYC) ... according to
  The Weather Company". CLI + 3 letters is the NWS climate-report code; the weather station is K + those letters
  (CLINYC -> KNYC Central Park, CLIMDW -> KMDW Chicago Midway).
- api.weather.gov needs no key, only a User-Agent. /stations/K??? gives the coordinates, /points/lat,lon gives
  the forecast URL and time zone, and the forecast lists daytime periods with the expected high in F.
- Kalshi settles on The Weather Company's reading, not the NWS forecast. That mismatch is one reason the
  forecast is treated as uncertain (see SD_F in strategies.py).

Only HIGH temperature markets are used. A day's LOW can fall at 1am or 11pm, which the forecast periods
don't line up with, so lows are skipped rather than guessed.
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

ROOT = "https://api.weather.gov"
HEADERS = {"user-agent": "kalshi-paper (github.com/ant588362/KalshiPaper)", "accept": "application/geo+json"}
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}

_forecasts = {}  # station -> (fetched_at, {date: high_f}, tz name); one fetch per station per run
_points = {}     # station -> (forecast_url, tz name); stations don't move


_failed = set()  # URLs that already failed this run: with ~30 weather bots, retrying a dead station for each one
                 # would cost minutes. Same answer either way (no forecast = no trade), just faster.


def _get(url, tries=3):
    if url in _failed:
        return None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1 + attempt)
    _failed.add(url)
    return None


def station_from_rules(rules):
    """'... recorded at New York City (CLINYC) ...' -> 'KNYC'. None if the rules don't name a CLI station."""
    m = re.search(r"\(CLI([A-Z]{3})\)", rules or "")
    return f"K{m.group(1)}" if m else None


def market_date(ticker):
    """'KXHIGHNY-26NOV04-T77' -> '2026-11-04' (the day whose high the market is about)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    return f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}-{m.group(3)}"


def forecast_highs(station):
    """({'2026-11-04': 73, ...}, 'America/New_York') from the NWS daytime periods, or (None, None)."""
    if station in _forecasts and time.time() - _forecasts[station][0] < 1800:
        return _forecasts[station][1], _forecasts[station][2]
    if station not in _points:
        s = _get(f"{ROOT}/stations/{station}")
        if not s:
            return None, None
        lon, lat = s["geometry"]["coordinates"]
        p = _get(f"{ROOT}/points/{lat:.4f},{lon:.4f}")
        if not p:
            return None, None
        _points[station] = (p["properties"]["forecast"], p["properties"]["timeZone"])
    url, tz = _points[station]
    f = _get(url)
    if not f:
        return None, None
    highs = {}
    for period in f["properties"]["periods"]:
        if period.get("isDaytime") and period.get("temperatureUnit") == "F":
            highs[period["startTime"][:10]] = period["temperature"]
    _forecasts[station] = (time.time(), highs, tz)
    return highs, tz


def local_today(tz):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
