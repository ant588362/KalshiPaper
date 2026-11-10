"""Download Kalshi's public trade prints for a random sample of studied markets -> trainer/data/trades.db.

Used by maker_study.py --trades to decide limit-order fills from real trades instead of hourly snapshots.
Public endpoint, no key: GET /trade-api/v2/markets/trades?ticker=&min_ts=&max_ts= (cursor paging, 1000 per page).
Only the last 48h before close are fetched (the study never trades earlier), at most MAX_PAGES pages per market.

Run:  py trainer/trades_download.py [N_MARKETS]   (default 4000; resumable)
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "data", "history.db")
OUT = os.path.join(HERE, "data", "trades.db")
API = "https://external-api.kalshi.com/trade-api/v2/markets/trades"
MAX_PAGES = 20
WORKERS = 4


def get(params):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-paper-research"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("gave up on " + url)


def fetch(ticker, close):
    rows, cursor = [], None
    for _ in range(MAX_PAGES):
        p = {"ticker": ticker, "limit": 1000, "min_ts": close - 49 * 3600, "max_ts": close}
        if cursor:
            p["cursor"] = cursor
        d = get(p)
        for t in d.get("trades", []):
            ts = int(time.mktime(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
            rows.append((ticker, ts, float(t["yes_price_dollars"]), float(t["count_fp"]), t["taker_side"]))
        cursor = d.get("cursor")
        if not cursor:
            return rows, True
    return rows, False


def main():
    n_want = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    out = sqlite3.connect(OUT)
    out.execute("CREATE TABLE IF NOT EXISTS trades (ticker TEXT, ts INT, yes_price REAL, count REAL, taker_side TEXT)")
    out.execute("CREATE INDEX IF NOT EXISTS trades_t ON trades (ticker, ts)")
    out.execute("CREATE TABLE IF NOT EXISTS done (ticker TEXT PRIMARY KEY, complete INT)")
    done = {r[0] for r in out.execute("SELECT ticker FROM done")}
    h = sqlite3.connect(HIST)
    mk = h.execute("SELECT ticker, close_ts FROM markets WHERE result IN ('yes','no')").fetchall()
    # deterministic random sample by event-independent hash of the ticker
    mk.sort(key=lambda r: hashlib.md5(r[0].encode()).hexdigest())
    todo = [r for r in mk[:n_want] if r[0] not in done]
    print(f"{n_want} sampled, {len(todo)} to fetch", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(WORKERS) as ex:
        for k, ((tk, close), res) in enumerate(zip(todo, ex.map(lambda r: fetch(*r), todo))):
            rows, complete = res
            out.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", rows)
            out.execute("INSERT OR REPLACE INTO done VALUES (?,?)", (tk, int(complete)))
            if k % 100 == 0:
                out.commit()
                print(f"  {k}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    out.commit()
    print("done", flush=True)


if __name__ == "__main__":
    main()
