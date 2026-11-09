"""Training ground: download hourly BTC/ETH spot prices (Coinbase Exchange public candles, no key, no account).

Kalshi settles KXBTC/KXBTCD/KXETH/KXETHD on CF Benchmarks' BRTI/ERTI (60-second average before close). BRTI is
built from several big USD exchanges, Coinbase among them, so Coinbase's price is a close stand-in (checked in
crypto_study.py against how the markets actually settled).

Stored in trainer/data/spot.db, table spot(product, ts, close): ts = END of the hour, so `close` is the last
price anyone could have known at time ts. No look-ahead.

Coinbase limits: 300 candles per request, public endpoints 10 requests/sec per IP; we go 1 request per 0.4s.
Usage: py trainer/spot_download.py [days=65]
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "spot.db")
BASE = "https://api.exchange.coinbase.com/products/{}/candles?granularity=3600&start={}&end={}"
PRODUCTS = ("BTC-USD", "ETH-USD")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-research"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            print("retry", e)
            time.sleep(2 + attempt * 3)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(days=65):
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS spot (product TEXT, ts INT, close REAL, PRIMARY KEY (product, ts)) WITHOUT ROWID")
    now = int(time.time()) // 3600 * 3600
    start = now - days * 86400
    for p in PRODUCTS:
        n = 0
        t = start
        while t < now:
            end = min(t + 299 * 3600, now)
            rows = get(BASE.format(p, iso(t), iso(end)))
            # row = [bucket start, low, high, open, close, volume]
            db.executemany("INSERT OR REPLACE INTO spot VALUES (?,?,?)", [(p, r[0] + 3600, r[4]) for r in rows])
            n += len(rows)
            t = end
            time.sleep(0.4)
        db.commit()
        lo, hi, c = db.execute("SELECT min(ts), max(ts), count(*) FROM spot WHERE product=?", (p,)).fetchone()
        print(p, "got", n, "| stored", c, iso(lo), "->", iso(hi))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 65)
