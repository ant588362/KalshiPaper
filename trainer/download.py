"""Training ground, step 1: download past Kalshi markets and their hourly prices into trainer/data/history.db.

Public market data only (no account, no key). Kept on this PC, never committed (see .gitignore).

What we keep, per finished market (closed with a plain yes/no result):
  - the market itself: event, close time, result, strike info, title, total volume
  - one candle per hour for its last 49 hours: YES bid and YES ask at the end of the hour, contracts traded
  - the fee table of its series

To keep it small and fast we keep markets with 500+ contracts traded in total (the live bots need 50+ in the
last day anyway), and a fixed 30% sample of EVENTS (all markets of a sampled event, so per-event caps and
"read the whole event" bots work). The sample is picked by a hash of the event name, so it never changes.

Usage:  py trainer/download.py 2026-09-09 2026-11-07     (days are close dates, UTC; re-running skips done days)
"""
import hashlib
import http.client
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kalshi_api as api  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "history.db")
MIN_VOLUME = 500
SAMPLE_PCT = 30
HOURS_BACK = 49
BATCH = 100  # Kalshi's max markets per candlestick request


def get(path, params=None):
    """api.get, plus retries for connections that drop mid-download (seen 2026-11-08: IncompleteRead)."""
    for attempt in range(5):
        try:
            return api.get(path, params)
        except (OSError, ValueError, http.client.HTTPException, api.KalshiError):
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB, check_same_thread=False, timeout=120)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, event TEXT, series TEXT, close_ts INT, result TEXT,
        strike_type TEXT, floor_strike REAL, cap_strike REAL, title TEXT, volume REAL, day TEXT);
      CREATE TABLE IF NOT EXISTS candles (ticker TEXT, ts INT, yes_bid REAL, yes_ask REAL, volume REAL,
        PRIMARY KEY (ticker, ts)) WITHOUT ROWID;
      CREATE TABLE IF NOT EXISTS fees (series TEXT PRIMARY KEY, fee_type TEXT, fee_multiplier REAL);
      CREATE TABLE IF NOT EXISTS days_done (day TEXT PRIMARY KEY, markets INT, candles INT);
      CREATE INDEX IF NOT EXISTS m_day ON markets (day);
    """)
    return db


def sampled(event):
    return int(hashlib.md5(event.encode()).hexdigest(), 16) % 100 < SAMPLE_PCT


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts_of(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def settled_markets(day_start):
    out, cursor = [], None
    while True:
        page = get("/markets", {"status": "settled", "limit": 1000, "mve_filter": "exclude", "cursor": cursor,
                                    "min_close_ts": day_start, "max_close_ts": day_start + 86400 - 1})
        if not page:
            break
        for m in page.get("markets", []):
            if (m.get("result") in ("yes", "no") and (num(m.get("volume_fp")) or 0) >= MIN_VOLUME
                    and sampled(m["event_ticker"])):
                out.append(m)
        cursor = page.get("cursor")
        if not cursor or not page.get("markets"):
            break
    return out


def pick(d, key):
    """Candle fields are 'close_dollars' on the live endpoint and 'close' on some others."""
    d = d or {}
    return num(d.get(f"{key}_dollars", d.get(key)))


def candles_for(batch, start, end):
    r = get("/markets/candlesticks", {"market_tickers": ",".join(m["ticker"] for m in batch),
                                          "start_ts": start, "end_ts": end, "period_interval": 60})
    rows = []
    close_of = {m["ticker"]: ts_of(m["close_time"]) for m in batch}
    for item in (r or {}).get("markets", []):
        t = item.get("market_ticker") or item.get("ticker")
        if t not in close_of:
            continue
        for c in item.get("candlesticks", []):
            ts = c.get("end_period_ts")
            if ts is None or not close_of[t] - HOURS_BACK * 3600 <= ts <= close_of[t] + 3600:
                continue
            bid, ask = pick(c.get("yes_bid"), "close"), pick(c.get("yes_ask"), "close")
            vol = num(c.get("volume_fp", c.get("volume"))) or 0.0
            rows.append((t, ts, bid, ask, vol))
    return rows


def series_of(event):
    return event.partition("-")[0]


def fees_for(db, series_list):
    have = {r[0] for r in db.execute("SELECT series FROM fees")}
    todo = [s for s in series_list if s not in have]
    with ThreadPoolExecutor(4) as pool:
        for s, f in zip(todo, pool.map(api.series_fees, todo)):
            db.execute("INSERT OR REPLACE INTO fees VALUES (?,?,?)",
                       (s, f["fee_type"] if f else "unknown", f["fee_multiplier"] if f else 1.0))
    db.commit()


def do_day(db, day):
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    ms = settled_markets(start)
    batches = [ms[i:i + BATCH] for i in range(0, len(ms), BATCH)]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda b: candles_for(b, start - HOURS_BACK * 3600, start + 86400 + 3600), batches))
    rows = [r for res in results for r in res]
    db.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (m["ticker"], m["event_ticker"], series_of(m["event_ticker"]), ts_of(m["close_time"]), m["result"],
         m.get("strike_type"), num(m.get("floor_strike")), num(m.get("cap_strike")), (m.get("title") or "")[:160],
         num(m.get("volume_fp")), day) for m in ms])
    db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?)", rows)
    fees_for(db, sorted({series_of(m["event_ticker"]) for m in ms}))
    db.execute("INSERT OR REPLACE INTO days_done VALUES (?,?,?)", (day, len(ms), len(rows)))
    db.commit()
    return len(ms), len(rows)


def claim(db, day):
    """Two downloaders can run at once (one forward, one --reverse): each day is claimed first so it's done once.
    A claim older than 30 minutes on a day that never finished is treated as abandoned."""
    db.execute("CREATE TABLE IF NOT EXISTS claims (day TEXT PRIMARY KEY, at INT)")
    db.execute("DELETE FROM claims WHERE at < ? AND day NOT IN (SELECT day FROM days_done)", (int(time.time()) - 1800,))
    if db.execute("SELECT 1 FROM days_done WHERE day = ?", (day,)).fetchone():
        return False
    cur = db.execute("INSERT OR IGNORE INTO claims VALUES (?, ?)", (day, int(time.time())))
    db.commit()
    return cur.rowcount == 1


def main(first, last, reverse=False):
    db = connect()
    days, d = [], datetime.strptime(first, "%Y-%m-%d")
    while d <= datetime.strptime(last, "%Y-%m-%d"):
        days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    for day in (reversed(days) if reverse else days):
        if claim(db, day):
            t = time.time()
            n, c = do_day(db, day)
            print(f"{day}: {n} markets, {c} candles, {time.time() - t:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--reverse" in sys.argv)
