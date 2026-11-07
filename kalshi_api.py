"""Read-only client for Kalshi's PUBLIC market-data endpoints. No account, no key, no orders.

Docs checked 2026-10-26: docs.kalshi.com/getting_started/quick_start_market_data says these endpoints
need no authentication and live at the production root below. The demo root has fake markets whose
prices "may not be reflective of real markets", so paper trading reads production prices instead.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = "https://external-api.kalshi.com/trade-api/v2"
PAUSE = 0.15  # seconds between requests, well under the public read limits


class KalshiError(Exception):
    pass


def get(path, params=None, tries=4):
    url = f"{ROOT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    for attempt in range(tries):
        time.sleep(PAUSE)
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "kalshi-paper/1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise KalshiError(f"{e.code} on {path}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            time.sleep(2 ** attempt)
            last = e
    raise KalshiError(f"gave up on {path}")


def open_markets_closing_within(hours, max_pages=15):
    """Single (non-combo) open markets that close within the next `hours`."""
    now = int(time.time())
    out, cursor = [], None
    for _ in range(max_pages):
        page = get("/markets", {"status": "open", "limit": 1000, "mve_filter": "exclude",
                                "min_close_ts": now, "max_close_ts": now + int(hours * 3600), "cursor": cursor})
        if not page:
            break
        out += page.get("markets", [])
        cursor = page.get("cursor")
        if not cursor:
            break
    return out


def market(ticker):
    r = get(f"/markets/{urllib.parse.quote(ticker)}")
    return r and r.get("market")


def orderbook(ticker):
    """Returns {'yes': [(price, qty), ...], 'no': [...]} of resting BIDS, prices in dollars."""
    r = get(f"/markets/{urllib.parse.quote(ticker)}/orderbook")
    book = (r or {}).get("orderbook_fp") or {}
    side = lambda key: [(float(p), float(q)) for p, q in (book.get(key) or [])]
    return {"yes": side("yes_dollars"), "no": side("no_dollars")}


def event_series(event_ticker):
    r = get(f"/events/{urllib.parse.quote(event_ticker)}", {"with_nested_markets": "false"})
    return r and (r.get("event") or {}).get("series_ticker")


def series_fees(series_ticker):
    r = get(f"/series/{urllib.parse.quote(series_ticker)}")
    s = (r or {}).get("series") or {}
    if "fee_type" not in s:
        return None
    return {"fee_type": s["fee_type"], "fee_multiplier": float(s.get("fee_multiplier") or 1)}
