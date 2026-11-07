"""Kalshi paper trading: every bot starts with $100 of pretend money and trades real, live Kalshi prices.

    py run.py tick      settle finished markets, look for trades, write a report (run this every 30 min)
    py run.py report    just write the report from what's saved

Nothing here can place a real order: it only reads Kalshi's public data.
"""
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import NormalDist

import kalshi_api as api
from fees import SUPPORTED, plan_buy
from strategies import ARMY, BOTS, CORE, category

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "paper.db")
REPORTS = os.path.join(HERE, "reports")

# ---- Limits (same for every bot) -------------------------------------------------------------------
START_CASH = 100.00
MAX_PER_TRADE = 3.00   # one bet can lose at most 3% of the starting money
MAX_PER_EVENT = 6.00   # markets in one event move together (e.g. temperature brackets), so cap them together
MAX_DEPLOYED = 50.00   # at most half the money is ever at risk at once; even if every open bet lost, half is left
MIN_BUDGET = 0.50      # skip trades too small to matter after the fee rounds up
MIN_PRICE = 0.21       # 2026-11-03: no bot buys a side priced 20c or less. Those longshots lost $161 across bots
                       # (random won 2 of 60, reversion 1 of 26): Kalshi longshots win even less than their price says.

# ---- Which markets count (same for every bot) ------------------------------------------------------
MIN_HOURS, MAX_HOURS = 0.5, 48   # not about to close, not weeks away
MAX_SPREAD = 0.06                # wide spreads mean the posted price isn't a real price
MIN_VOLUME_24H = 50              # contracts traded in the last day
MIN_SETTLED_FOR_VERDICT = 100    # fewer settled trades than this is luck, not evidence
SNAPSHOT_DAYS = 7


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS bots (key TEXT PRIMARY KEY, name TEXT, version INT, params TEXT,
        fingerprint TEXT, start_cash REAL, started_at TEXT);
      CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, bot TEXT, ticker TEXT, event TEXT, title TEXT,
        side TEXT, contracts INT, avg_price REAL, cost REAL, fee REAL, opened_at TEXT,
        status TEXT DEFAULT 'open', result TEXT, payout REAL, settled_at TEXT, UNIQUE (bot, ticker));
      CREATE TABLE IF NOT EXISTS equity (bot TEXT, ts TEXT, cash REAL, open_value REAL, equity REAL);
      CREATE TABLE IF NOT EXISTS snapshots (ts INT, ticker TEXT, yes_mid REAL);
      CREATE INDEX IF NOT EXISTS snap_idx ON snapshots (ticker, ts);
      CREATE TABLE IF NOT EXISTS series_cache (event TEXT PRIMARY KEY, series TEXT, fee_type TEXT, fee_multiplier REAL);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
      CREATE INDEX IF NOT EXISTS equity_idx ON equity (bot, ts);
      CREATE INDEX IF NOT EXISTS pos_bot_idx ON positions (bot, status);
    """)
    # Record-keeping only (added 2026-10-28): the exact fills and the fee table used at entry, so a fee can be
    # re-derived exactly later. Doesn't change what any bot buys or pays.
    have = {r[1] for r in db.execute("PRAGMA table_info(positions)")}
    for col, kind in (("fills", "TEXT"), ("fee_type", "TEXT"), ("fee_multiplier", "REAL"), ("note", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE positions ADD COLUMN {col} {kind}")
    return db


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def freeze_bots(db):
    """First run of a bot version records its rules. If they change later, stop instead of mixing records."""
    for b in BOTS:
        row = db.execute("SELECT fingerprint FROM bots WHERE key = ?", (b.key,)).fetchone()
        fp = b.fingerprint()
        if row is None:
            db.execute("INSERT INTO bots VALUES (?,?,?,?,?,?,?)",
                       (b.key, b.name, b.version, repr(sorted(b.params.items())), fp, START_CASH, now_iso()))
        elif row["fingerprint"] != fp:
            sys.exit(f"STOP: the rules of {b.key} changed after its test started. Give it a new version number "
                     f"in strategies.py (version = {b.version + 1}) so it gets a fresh record.")
    db.commit()


def cash(db, bot):
    r = db.execute("SELECT coalesce(sum(cost + fee), 0) AS out, coalesce(sum(payout), 0) AS back FROM positions WHERE bot = ?",
                   (bot,)).fetchone()
    return START_CASH - r["out"] + r["back"]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def shape(m):
    close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    return {
        "ticker": m["ticker"], "event": m["event_ticker"], "title": m.get("title", ""),
        "yes_bid": num(m.get("yes_bid_dollars")), "yes_ask": num(m.get("yes_ask_dollars")),
        "no_bid": num(m.get("no_bid_dollars")), "no_ask": num(m.get("no_ask_dollars")),
        "volume_24h": num(m.get("volume_24h_fp")), "hours_left": (close - time.time()) / 3600, "now": time.time(),
        # what the market is about (used by the weather bot; other bots ignore these)
        "strike_type": m.get("strike_type"), "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"),
        "rules": m.get("rules_primary", ""),
    }


def tradable(m):
    return (m["yes_bid"] > 0 and 0 < m["yes_ask"] < 1 and m["yes_ask"] - m["yes_bid"] <= MAX_SPREAD
            and m["volume_24h"] >= MIN_VOLUME_24H and MIN_HOURS <= m["hours_left"] <= MAX_HOURS)


def fees_for(db, event):
    row = db.execute("SELECT * FROM series_cache WHERE event = ?", (event,)).fetchone()
    if row:
        return row["fee_type"], row["fee_multiplier"]
    series = api.event_series(event)
    f = api.series_fees(series) if series else None
    fee_type, mult = (f["fee_type"], f["fee_multiplier"]) if f else ("unknown", 1.0)
    db.execute("INSERT OR REPLACE INTO series_cache VALUES (?,?,?,?)", (event, series, fee_type, mult))
    return fee_type, mult


def settle(db, log):
    """Pay out finished markets. Returns {ticker: market} for everything still held, for marking to market."""
    held = {}
    for (ticker,) in db.execute("SELECT DISTINCT ticker FROM positions WHERE status = 'open'").fetchall():
        m = api.market(ticker)
        if not m:
            continue
        result = (m.get("result") or "").lower()
        if result in ("yes", "no"):
            for p in db.execute("SELECT * FROM positions WHERE status = 'open' AND ticker = ?", (ticker,)).fetchall():
                payout = float(p["contracts"]) if p["side"] == result else 0.0
                db.execute("UPDATE positions SET status='settled', result=?, payout=?, settled_at=? WHERE id=?",
                           (result, payout, now_iso(), p["id"]))
                log.append(f"settled {p['bot']} {ticker} {p['side']} -> {result}, paid ${payout:.2f}")
        elif result:
            # Not a plain yes/no (e.g. cancelled and settled at a fair price). Flag it for a human, don't guess.
            db.execute("UPDATE positions SET status='review', result=? WHERE status='open' AND ticker=?", (result, ticker))
            log.append(f"REVIEW {ticker}: result '{result}', check by hand")
        else:
            held[ticker] = shape(m)
    db.commit()
    return held


FETCH_WORKERS = 4  # parallel reads; each still pauses between requests, and 429s back off (kalshi_api.get)


def trade(db, universe, log):
    """Same rules as before (one entry per market, the same caps and fees), done in three passes so 200 bots
    stay fast: 1) every bot decides, 2) the order books and fee tables they need are fetched in parallel,
    3) buys are applied in the same order as before. Paper note: every bot sees the full book, so two bots buying
    the same market don't use up each other's contracts."""
    now = int(time.time())
    active = [b for b in BOTS if b.name != "cash" and not b.retired]
    held = {(r[0], r[1]) for r in db.execute("SELECT bot, ticker FROM positions")}
    hist = {}
    for r in db.execute("SELECT ticker, ts, yes_mid FROM snapshots WHERE ts < ? ORDER BY ts", (now - 60,)):
        hist.setdefault(r[0], []).append((r[1], r[2]))

    wishes = []
    for b in active:
        for m in universe:
            if (b.key, m["ticker"]) in held:
                continue  # one entry per market, no adding to positions
            wish = b.decide(m, hist.get(m["ticker"], []))
            if wish and m[f"{wish[0]}_ask"] >= MIN_PRICE:
                wishes.append((b, m, wish))

    events = sorted({m["event"] for _, m, _ in wishes})
    cached = {r[0] for r in db.execute("SELECT event FROM series_cache")}
    todo = [e for e in events if e not in cached]

    def series_and_fees(event):
        series = api.event_series(event)
        f = api.series_fees(series) if series else None
        return event, series, f

    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        for event, series, f in pool.map(series_and_fees, todo):
            fee_type, mult = (f["fee_type"], f["fee_multiplier"]) if f else ("unknown", 1.0)
            db.execute("INSERT OR REPLACE INTO series_cache VALUES (?,?,?,?)", (event, series, fee_type, mult))
        fee_of = {e: fees_for(db, e) for e in events}
        tickers = sorted({m["ticker"] for _, m, _ in wishes if fee_of[m["event"]][0] in SUPPORTED})
        books = dict(zip(tickers, pool.map(api.orderbook, tickers)))

    deployed, in_event = {}, {}
    for r in db.execute("SELECT bot, event, sum(cost+fee) FROM positions WHERE status='open' GROUP BY bot, event"):
        deployed[r[0]] = deployed.get(r[0], 0.0) + r[2]
        in_event[(r[0], r[1])] = r[2]
    money = {b.key: cash(db, b.key) for b in active}

    for b, m, wish in wishes:
        side, max_price = wish[0], wish[1]
        stake = wish[2] if len(wish) > 2 else b.max_per_trade
        note = wish[3] if len(wish) > 3 else None
        ev = (b.key, m["event"])
        budget = min(stake, b.max_per_trade, MAX_PER_EVENT - in_event.get(ev, 0.0),
                     MAX_DEPLOYED - deployed.get(b.key, 0.0), money[b.key])
        if budget < MIN_BUDGET:
            continue
        fee_type, mult = fee_of[m["event"]]
        if fee_type not in SUPPORTED:
            continue  # fee table we haven't verified: skip rather than guess
        plan = plan_buy(side, books.get(m["ticker"]), budget, max_price, mult)
        if not plan:
            continue
        fills, cost, fee = plan
        n = sum(c for _, c in fills)
        db.execute("""INSERT INTO positions (bot, ticker, event, title, side, contracts, avg_price, cost, fee, opened_at,
                        fills, fee_type, fee_multiplier, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (b.key, m["ticker"], m["event"], m["title"][:120], side, n, round(cost / n, 4), cost, fee, now_iso(),
                    json.dumps(fills), fee_type, mult, note))
        deployed[b.key] = deployed.get(b.key, 0.0) + cost + fee
        in_event[ev] = in_event.get(ev, 0.0) + cost + fee
        money[b.key] -= cost + fee
        if not getattr(b, "army", False):
            log.append(f"{b.key} bought {n} {side.upper()} {m['ticker']} at avg ${cost / n:.2f} + ${fee:.2f} fee")
    army_buys = db.execute("SELECT count(*) FROM positions WHERE opened_at >= ? AND bot IN (%s)" % ",".join("?" * len(ARMY)),
                           (datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *[b.key for b in ARMY])).fetchone()[0]
    if army_buys:
        log.append(f"army: {army_buys} buys across {len(ARMY)} bots (listed in positions.csv)")
    db.commit()


def mark(db, prices):
    """Equity = cash + what open positions could be SOLD for right now (the bid), not what we hope they're worth.
    A missing quote still counts as $0 (unchanged), but it's now counted and shown in the report, not silent."""
    ts = now_iso()
    flags = {"missing": 0, "no_bid": 0}
    for b in BOTS:
        open_value = 0.0
        for p in db.execute("SELECT * FROM positions WHERE bot=? AND status='open'", (b.key,)).fetchall():
            m = prices.get(p["ticker"])
            if m is None:
                flags["missing"] += 1
            elif not m[f"{p['side']}_bid"]:
                flags["no_bid"] += 1
            open_value += p["contracts"] * (m[f"{p['side']}_bid"] if m else 0.0)
        c = cash(db, b.key)
        db.execute("INSERT INTO equity VALUES (?,?,?,?,?)", (b.key, ts, round(c, 4), round(open_value, 4), round(c + open_value, 4)))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('mark_flags', ?)", (json.dumps(flags),))
    db.commit()


# Different series that track the same thing (a coin, oil, an index, one game) can all lose together,
# which the per-event cap doesn't see. Report-only: no limit uses this.
SAME_UNDERLYING = {"KXBTCD": "BTC", "KXBTC": "BTC", "KXETHD": "ETH", "KXETH": "ETH", "KXWTI": "OIL", "KXBRENTD": "OIL",
                   "KXINXU": "S&P 500", "KXINX": "S&P 500", "KXNASDAQ100U": "NASDAQ", "KXNASDAQ100": "NASDAQ"}


def underlying(event):
    series, _, rest = event.partition("-")
    if series in SAME_UNDERLYING:
        return SAME_UNDERLYING[series]
    # Sports: spread/total/1st-half markets on one game share the game code (e.g. 26OCT28KENNMTU).
    for sport in ("NCAAF", "NCAAB", "WNBA", "NFL", "NBA", "NHL", "MLB"):
        if series.startswith(f"KX{sport}") and rest:
            return f"{sport} {rest}"
    return series


def exposure(db, bot, top=3):
    groups = {}
    for p in db.execute("SELECT event, cost + fee AS out FROM positions WHERE bot=? AND status='open'", (bot,)):
        g = underlying(p["event"])
        groups[g] = groups.get(g, 0.0) + p["out"]
    return sorted(groups.items(), key=lambda kv: -kv[1])[:top]


def t_bar(n_bots):
    """The t-score a bot needs before it can get more money. With one bot, 2 (about 1-in-40 odds of luck). With
    200 bots, some will reach 2 by pure luck, so the bar rises until the chance that ANY of them gets there by luck
    is still about 1 in 40 (Bonferroni). 200 bots -> about 3.7."""
    return max(2.0, NormalDist().inv_cdf(1 - 0.025 / max(1, n_bots)))


def pnl_stats(db, bot):
    """(settled count, total profit, profit per trade, t-score) on settled trades after fees."""
    pnl = [r[0] for r in db.execute(
        "SELECT payout - cost - fee FROM positions WHERE bot=? AND status='settled'", (bot,))]
    n = len(pnl)
    if n < 2:
        return n, sum(pnl), (pnl[0] if pnl else 0.0), 0.0
    avg = sum(pnl) / n
    sd = (sum((x - avg) ** 2 for x in pnl) / (n - 1)) ** 0.5
    return n, sum(pnl), avg, (avg / (sd / n ** 0.5) if sd else 0.0)


def scale_check(db, bot, bar=2.0):
    """Is this bot ready for more (or real) money? Bigger bets on a bot with no edge just lose faster, so it has to
    earn it: 100+ settled trades, profit on them after fees, and a t-score of 2+ (profit per trade at least twice
    its own noise, so it's very unlikely to be luck). Returns (ready, plain-words line)."""
    n, total, avg, t = pnl_stats(db, bot)
    if n < 2:
        return False, f"{n} settled, need {MIN_SETTLED_FOR_VERDICT}"
    ready = n >= MIN_SETTLED_FOR_VERDICT and total > 0 and t >= bar
    why = "READY to size up" if ready else ("not yet" if n < MIN_SETTLED_FOR_VERDICT else "NO, profit isn't proven")
    return ready, f"{n} settled, {total:+.2f} total, {avg:+.3f}/trade, t-score {t:.1f} -> {why}"


def worst_drawdown(rows):
    peak, worst = START_CASH, 0.0
    for r in rows:
        peak = max(peak, r["equity"])
        worst = max(worst, peak - r["equity"])
    return worst


def run_health(db, hours=24):
    """(runs in the last `hours`, longest gap between runs in hours). Momentum and reversion need a price
    from 5-8h ago, so gaps longer than ~3h starve them."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    ts = [datetime.fromisoformat(r[0]) for r in db.execute("SELECT DISTINCT ts FROM equity WHERE ts >= ? ORDER BY ts", (since,))]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:])]
    return len(ts), max(gaps, default=0.0)


def report(db, log=()):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = os.path.join(REPORTS, stamp)
    os.makedirs(out, exist_ok=True)
    summary = []
    for b in BOTS:
        eq = db.execute("SELECT * FROM equity WHERE bot=? ORDER BY ts", (b.key,)).fetchall()
        last = eq[-1] if eq else {"cash": START_CASH, "open_value": 0.0, "equity": START_CASH}
        s = db.execute("""SELECT count(*) AS trades, coalesce(sum(fee),0) AS fees,
                            sum(status='settled') AS settled, sum(status='open') AS open_n,
                            sum(status='settled' AND payout > 0) AS wins, sum(status='review') AS review,
                            coalesce(sum(CASE WHEN status='settled' THEN payout - cost - fee END), 0) AS realized,
                            coalesce(sum(CASE WHEN status='open' THEN cost + fee END), 0) AS open_cost
                          FROM positions WHERE bot=?""", (b.key,)).fetchone()
        settled = s["settled"] or 0
        net = last["equity"] - START_CASH
        if b.name == "cash":
            verdict = "the bar to beat"
        elif b.retired:
            verdict = "RETIRED (record kept)"
        elif settled < MIN_SETTLED_FOR_VERDICT:
            verdict = f"too early ({settled}/{MIN_SETTLED_FOR_VERDICT} settled)"
        else:
            verdict = "BEATING CASH" if net > 0 else "losing to cash"
        summary.append({
            "bot": b.key, "start": START_CASH, "cash": round(last["cash"], 2), "open_value_at_bid": round(last["open_value"], 2),
            "equity": round(last["equity"], 2), "net_profit_after_fees": round(net, 2), "realized_settled": round(s["realized"], 2),
            "unrealized_open": round(last["open_value"] - s["open_cost"], 2),
            "fees_paid": round(s["fees"], 2), "trades": s["trades"], "open": s["open_n"] or 0, "settled": settled,
            "wins": s["wins"] or 0, "losses": settled - (s["wins"] or 0), "needs_review": s["review"] or 0,
            "worst_drawdown": round(worst_drawdown(eq), 2), "verdict": verdict,
        })
    write_csv(os.path.join(out, "summary.csv"), summary)
    write_csv(os.path.join(out, "positions.csv"), [dict(r) for r in db.execute("SELECT * FROM positions ORDER BY id")])
    write_csv(os.path.join(out, "equity.csv"), [dict(r) for r in db.execute("SELECT * FROM equity ORDER BY ts, bot")])
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(f"Kalshi paper report {stamp} (pretend money, real Kalshi prices)\n")
        f.write(f"Limits: ${MAX_PER_TRADE}/trade, ${MAX_PER_EVENT}/event, ${MAX_DEPLOYED} deployed max, start ${START_CASH}\n")
        runs, gap = run_health(db)
        f.write(f"Runs in the last 24h: {runs}, longest gap {gap:.1f}h\n")
        f.write(f"No bot buys anything priced under ${MIN_PRICE:.2f}. Retired bots keep their record but don't trade.\n\n")
        for s in summary[:len(CORE)]:
            f.write(f"{s['bot']:<14} equity ${s['equity']:>7.2f}  net {s['net_profit_after_fees']:>+7.2f}  "
                    f"(settled {s['realized_settled']:+.2f}, open {s['unrealized_open']:+.2f})  fees ${s['fees_paid']:.2f}  "
                    f"settled {s['settled']} ({s['wins']}W/{s['losses']}L)  open {s['open']}  worst drop ${s['worst_drawdown']:.2f}  {s['verdict']}\n")
        bar = t_bar(sum(1 for b in BOTS if b.name != "cash" and not b.retired))
        f.write(f"\nReady for more money? (100+ settled, profit after fees, t-score {bar:.1f}+ because "
                f"{len(BOTS)} bots are being tested at once)\n")
        for b in CORE:
            if b.name != "cash" and not b.retired:
                f.write(f"  {b.key:<14} {scale_check(db, b.key, bar)[1]}\n")
        by_key = {s["bot"]: s for s in summary}
        board = []
        for b in ARMY:
            s = by_key[b.key]
            ready, line = scale_check(db, b.key, bar)
            board.append((s["realized_settled"], s["equity"], b.key, s, ready, line))
        board.sort(reverse=True)
        luck = [x for x in board if x[2].startswith("luck-")]
        f.write(f"\nARMY: {len(ARMY)} bots, {sum(1 for x in board if x[3]['trades'])} have traded, "
                f"{sum(x[3]['settled'] for x in board)} settled bets, {sum(x[3]['open'] for x in board)} open\n")
        if luck:
            best = luck[0]
            f.write(f"Luck line: the best of {len(luck)} random bots is {best[2]} at {best[0]:+.2f} settled "
                    f"({best[3]['settled']} bets). A strategy below this hasn't shown anything luck can't do.\n")
        f.write("Top 15 by settled profit (ties: by equity):\n")
        for pnl, eq, key, s, ready, line in board[:15]:
            f.write(f"  {key:<24} settled {pnl:+7.2f}  equity ${eq:>7.2f}  {s['wins']}W/{s['losses']}L  "
                    f"open {s['open']}  {line.split(', ', 3)[-1] if s['settled'] > 1 else ''}\n")
        f.write("Bottom 5:\n")
        for pnl, eq, key, s, ready, line in board[-5:]:
            f.write(f"  {key:<24} settled {pnl:+7.2f}  equity ${eq:>7.2f}  {s['wins']}W/{s['losses']}L\n")
        winners = [x[2] for x in board if x[4]]
        f.write(f"Army bots READY for more money: {', '.join(winners) if winners else 'none yet'}\n")
        write_dashboard(db, out, summary, bar)
        row = db.execute("SELECT value FROM meta WHERE key='mark_flags'").fetchone()
        flags = json.loads(row[0]) if row else {}
        if flags.get("missing") or flags.get("no_bid"):
            f.write(f"\nOpen marks: {flags.get('missing', 0)} position(s) had NO quote and {flags.get('no_bid', 0)} had no bid; "
                    "both are valued at $0 above. Settled P&L is the real number.\n")
        f.write("\nBiggest open exposure by underlying (same coin / commodity / game across markets):\n")
        for b in CORE:
            ex = exposure(db, b.key)
            if ex:
                f.write(f"  {b.key:<14} " + ", ".join(f"{g} ${v:.2f}" for g, v in ex) + "\n")
        if log:
            f.write("\nThis run:\n" + "\n".join(log) + "\n")
    latest = os.path.join(REPORTS, "latest")
    shutil.rmtree(latest, ignore_errors=True)
    shutil.copytree(out, latest)
    return os.path.join(latest, "report.txt")


def kind_of(b):
    """(family, market type) for the dashboard's filters."""
    if not getattr(b, "army", False):
        return "core", "any"
    if b.key.startswith("luck-"):
        return "luck", "any"
    if b.key.startswith("wx-"):
        return "weather model", "weather"
    lo, hi = b.params["lo"], b.params["hi"]
    return ("favorite" if lo >= 0.65 else "longshot" if hi <= 0.45 else "coin flip"), b.params["cat"]


CORE_DESC = {
    "cash": "Never bets. The bar every other bot has to beat after fees.",
    "random": "Buys a random side on about 1 in 20 markets. The original luck control.",
    "favorite": "Buys the side priced 85-95c in the last 24 hours before close.",
    "momentum": "Followed a 10c+ price move over ~6 hours. Retired: lost money and needed more frequent runs.",
    "reversion": "Bet against a 15c+ move over ~6 hours. Retired: lost money.",
    "weather": "Reads the free National Weather Service forecast and bets when it disagrees with the market by 1.5F+.",
}


def describe(b):
    """One plain-English line about what a bot does."""
    if not getattr(b, "army", False):
        return CORE_DESC.get(b.name, "")
    if b.key.startswith("luck-"):
        return "Flips a coin on about 1 in 20 markets. Pure luck: the line every idea has to beat."
    P = b.params
    if b.key.startswith("wx-"):
        return (f"Weather: bets when the forecast and the market disagree by {P['min_shift_f']:g}F+, only with "
                f"{P['min_edge'] * 100:.0f}c+ of edge after the fee, assuming at least +/-{P['min_sd_f']:g}F of error.")
    where = "any market" if P["cat"] == "any" else f"{P['cat']} markets"
    return f"Buys the side priced {P['lo'] * 100:.0f}-{P['hi'] * 100:.0f}c in {where}, within {P['hours_left']}h of closing."


def write_dashboard(db, out, summary, bar, points=90, recent_bets=15):
    """dashboard.json for the live site: every bot's numbers and profile, plus market-wide analytics."""
    by_key = {s["bot"]: s for s in summary}
    rows = db.execute("""SELECT bot, ticker, event, title, side, avg_price, cost, fee, payout, status, opened_at, settled_at
                         FROM positions ORDER BY coalesce(settled_at, opened_at)""").fetchall()
    per_bot = {}
    for r in rows:
        per_bot.setdefault(r["bot"], []).append(r)
    army_keys = {b.key for b in ARMY}
    luck_keys = {b.key for b in ARMY if b.key.startswith("luck-")}

    bots = []
    for b in BOTS:
        s = by_key[b.key]
        n, total, avg, t = pnl_stats(db, b.key)
        eq = [(r[0], r[1]) for r in db.execute("SELECT ts, equity FROM equity WHERE bot=? ORDER BY ts", (b.key,))]
        step = max(1, len(eq) // points)
        curve = eq[::step] + ([eq[-1]] if eq and (len(eq) - 1) % step else [])
        family, cat = kind_of(b)
        mine = per_bot.get(b.key, [])
        done = [r for r in mine if r["status"] == "settled"]
        pnl = [r["payout"] - r["cost"] - r["fee"] for r in done]
        streak_w = streak_l = cur_w = cur_l = 0
        for x in pnl:
            cur_w, cur_l = (cur_w + 1, 0) if x > 0 else (0, cur_l + 1)
            streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)
        by_cat = {}
        for r, x in zip(done, pnl):
            c = category(r["event"])
            by_cat.setdefault(c, [0.0, 0])
            by_cat[c][0] += x
            by_cat[c][1] += 1
        staked = sum(r["cost"] + r["fee"] for r in done)

        def bet(r, x=None):
            return [(r["settled_at"] or r["opened_at"])[5:16], (r["title"] or r["ticker"])[:70], r["side"],
                    round(r["avg_price"], 2), round(r["cost"] + r["fee"], 2), None if x is None else round(x, 2)]
        best = max(zip(done, pnl), key=lambda p: p[1], default=None)
        worst = min(zip(done, pnl), key=lambda p: p[1], default=None)
        bots.append({
            "key": b.key, "family": family, "cat": cat, "retired": b.retired, "desc": describe(b),
            "equity": s["equity"], "net": s["net_profit_after_fees"], "settled_pnl": round(total, 2),
            "per_trade": round(avg, 3), "t": round(t, 2), "settled": s["settled"], "wins": s["wins"],
            "losses": s["losses"], "open": s["open"], "fees": s["fees_paid"], "drawdown": s["worst_drawdown"],
            "verdict": s["verdict"], "ready": n >= MIN_SETTLED_FOR_VERDICT and total > 0 and t >= bar,
            "win_rate": round(sum(x > 0 for x in pnl) / len(pnl), 3) if pnl else None,
            "avg_price": round(sum(r["avg_price"] for r in done) / len(done), 3) if done else None,
            "roi": round(total / staked, 3) if staked else None,
            "streak_w": streak_w, "streak_l": streak_l,
            "best": bet(*best) if best else None, "worst": bet(*worst) if worst else None,
            "by_cat": {c: [round(v[0], 2), v[1]] for c, v in by_cat.items()},
            "bets": [bet(r, x) for r, x in list(zip(done, pnl))[-recent_bets:]][::-1],
            "open_bets": [bet(r) for r in mine if r["status"] == "open"][-10:][::-1],
            "curve": [[ts[5:16], round(e, 2)] for ts, e in curve],
        })

    # Are Kalshi's prices honest? Each market + side counted once (many bots buy the same thing), bucketed by the
    # price paid. If sides priced 85c win more than 85% of the time, favorites are underpriced (that's an edge).
    seen, buckets = set(), {}
    for r in rows:
        if r["status"] != "settled" or (r["ticker"], r["side"]) in seen:
            continue
        seen.add((r["ticker"], r["side"]))
        k = min(int(r["avg_price"] * 20), 19)  # 5c buckets
        bk = buckets.setdefault(k, [0, 0, 0.0])
        bk[0] += 1
        bk[1] += r["payout"] > 0
        bk[2] += r["avg_price"]
    calibration = [{"lo": k / 20, "n": v[0], "win_rate": round(v[1] / v[0], 3), "avg_price": round(v[2] / v[0], 3)}
                   for k, v in sorted(buckets.items())]

    # Settled profit per day and per market type: strategies (army minus luck) vs luck vs the original bots.
    daily, cats = {}, {}
    for r in rows:
        if r["status"] != "settled":
            continue
        x = r["payout"] - r["cost"] - r["fee"]
        group = "luck" if r["bot"] in luck_keys else "army" if r["bot"] in army_keys else "core"
        d = daily.setdefault(r["settled_at"][:10], {"army": 0.0, "luck": 0.0, "core": 0.0})
        d[group] += x
        c = cats.setdefault(category(r["event"]), {"army": [0.0, 0], "luck": [0.0, 0], "core": [0.0, 0]})
        c[group][0] += x
        c[group][1] += 1
    recent = [dict(r) for r in db.execute(
        """SELECT bot, ticker, title, side, round(cost+fee, 2) AS spent, status, round(payout, 2) AS payout,
                  coalesce(settled_at, opened_at) AS at FROM positions ORDER BY coalesce(settled_at, opened_at) DESC LIMIT 40""")]
    runs, gap = run_health(db)
    data = {"generated_at": now_iso(), "runs_24h": runs, "longest_gap_h": round(gap, 1), "bar": round(bar, 2),
            "min_settled": MIN_SETTLED_FOR_VERDICT, "start_cash": START_CASH, "bots": bots, "recent": recent,
            "calibration": calibration,
            "daily": [{"day": d, **{k: round(v, 2) for k, v in g.items()}} for d, g in sorted(daily.items())],
            "by_cat": {c: {g: [round(v[0], 2), v[1]] for g, v in gs.items()} for c, gs in cats.items()}}
    with open(os.path.join(out, "dashboard.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


SNAPSHOT_EVERY_S = 25 * 60
FULL_DETAIL_DAYS = 2


def compact(db):
    """Keep the ledger small enough for GitHub at ~288 runs a day: money history older than 2 days keeps one
    point per bot per hour (charts don't need more), and once a day the file is rewritten to drop the space."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FULL_DETAIL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("""DELETE FROM equity WHERE ts < ? AND rowid NOT IN
                  (SELECT min(rowid) FROM equity WHERE ts < ? GROUP BY bot, substr(ts, 1, 13))""", (cutoff, cutoff))
    db.commit()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.execute("SELECT value FROM meta WHERE key='last_vacuum'").fetchone()
    if not row or row[0] != today:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_vacuum', ?)", (today,))
        db.commit()
        db.execute("VACUUM")


def tick():
    db = connect()
    freeze_bots(db)
    log = []
    held = settle(db, log)
    universe = [shape(m) for m in api.open_markets_closing_within(MAX_HOURS)]
    # Every market also sees its whole event (e.g. all temperature ranges for one city-day, tradable or not), so a
    # bot can read what the event's prices say together. Bots that don't need it ignore it.
    events = {}
    for m in universe:
        if m["yes_ask"] > 0:
            events.setdefault(m["event"], []).append({k: m[k] for k in ("strike_type", "floor_strike", "cap_strike")}
                                                     | {"mid": (m["yes_bid"] + m["yes_ask"]) / 2})
    for m in universe:
        m["siblings"] = events.get(m["event"], [])
    universe = [m for m in universe if tradable(m)]
    trade(db, universe, log)
    now = int(time.time())
    # Runs every ~5 min now, so price snapshots are kept at most every 25 min (no active bot needs finer).
    last = db.execute("SELECT value FROM meta WHERE key='last_snapshot'").fetchone()
    if not last or now - int(last[0]) >= SNAPSHOT_EVERY_S:
        db.executemany("INSERT INTO snapshots VALUES (?,?,?)",
                       [(now, m["ticker"], round((m["yes_bid"] + m["yes_ask"]) / 2, 4)) for m in universe])
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_snapshot', ?)", (str(now),))
    db.execute("DELETE FROM snapshots WHERE ts < ?", (now - SNAPSHOT_DAYS * 86400,))
    db.commit()
    mark(db, {**{m["ticker"]: m for m in universe}, **held})
    compact(db)
    path = report(db, log)
    print(f"{len(universe)} tradable markets, {len(log)} events this run. Report: {path}")
    print(open(path, encoding="utf-8").read())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        tick()
    elif cmd == "report":
        print(open(report(connect()), encoding="utf-8").read())
    else:
        print(__doc__)
