"""Daily update in plain words: how each bot is doing and whether anything needs a change.
Prints Markdown. The GitHub workflow posts it as a comment on the "Daily report" issue, which emails the owner.

    py digest.py          print the update
    py digest.py due      exit 0 if today's update hasn't been posted and it's 13:00 UTC (9am New York) or later
    py digest.py posted   remember that today's update went out

GitHub skips some scheduled runs, so "due" fires on the first run at or after 13:00, not only during that hour.
"""
import sys
from datetime import datetime, timedelta, timezone

from run import MIN_SETTLED_FOR_VERDICT, START_CASH, connect, run_health, scale_check, t_bar, worst_drawdown
from lineup import ARMY, BOTS, CORE

BIG_DROP = 25.0  # a bot down this much from its best point gets flagged
POST_HOUR_UTC = 13


def due(db, now=None):
    now = now or datetime.now(timezone.utc)
    row = db.execute("SELECT value FROM meta WHERE key = 'digest_posted'").fetchone()
    return now.hour >= POST_HOUR_UTC and (row is None or row[0] != now.strftime("%Y-%m-%d"))


def posted(db, now=None):
    now = now or datetime.now(timezone.utc)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('digest_posted', ?)", (now.strftime("%Y-%m-%d"),))
    db.commit()


def main():
    db = connect()
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows, flags = [], []
    for b in CORE:
        eq = db.execute("SELECT * FROM equity WHERE bot=? ORDER BY ts", (b.key,)).fetchall()
        equity = eq[-1]["equity"] if eq else START_CASH
        s = db.execute("""SELECT sum(status='settled') AS settled, sum(status='settled' AND payout > 0) AS wins,
                            sum(status='open') AS open_n, sum(status='review') AS review,
                            sum(opened_at >= ?) AS new_trades, sum(settled_at >= ?) AS new_settled
                          FROM positions WHERE bot=?""", (day_ago, day_ago, b.key)).fetchone()
        settled, wins = s["settled"] or 0, s["wins"] or 0
        dd = worst_drawdown(eq)
        net = equity - START_CASH
        rows.append(f"| {b.key} | ${equity:.2f} | {net:+.2f} | {settled} ({wins}W/{settled - wins}L) | {s['open_n'] or 0} | "
                    f"{s['new_trades'] or 0} | ${dd:.2f} |")
        if b.name == "cash" or b.retired:
            continue
        if settled >= MIN_SETTLED_FOR_VERDICT:
            flags.append(f"**{b.key} has {settled} settled trades, enough for a verdict: "
                         f"{'BEATING cash' if net > 0 else 'LOSING to cash'} ({net:+.2f}).**")
        if dd >= BIG_DROP:
            flags.append(f"{b.key} fell ${dd:.2f} from its best point. Not a reason to edit it mid-test, but worth a look.")
        if (s["review"] or 0):
            flags.append(f"{b.key} has {s['review']} trade(s) that settled in an unusual way (marked review). Check them by hand.")
        if len(eq) >= 24 and not (s["new_trades"] or 0) and not (s["open_n"] or 0):
            flags.append(f"{b.key} made no trades in the last day. Its filter may be too strict for current markets.")

    runs_today, gap = run_health(db)
    first = db.execute("SELECT min(ts) FROM equity").fetchone()[0]
    if first and first < day_ago and (runs_today < 12 or gap > 3):
        flags.append(f"Only {runs_today} runs in the last 24h (expected ~24), longest gap {gap:.1f}h. "
                     "Fewer runs = fewer chances to trade. Check the hourly trigger.")

    print(f"## Kalshi paper update, {datetime.now(timezone.utc):%a %d %b %Y}\n")
    print("Pretend money, real Kalshi prices, every bot started at $100.\n")
    print("| Bot | Equity | Net after fees | Settled | Open | New trades (24h) | Worst drop |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    print("\n".join(rows))
    bar = t_bar(sum(1 for b in BOTS if b.name != "cash" and not b.retired))
    army = []
    for b in ARMY:
        r = db.execute("""SELECT count(*), coalesce(sum(payout - cost - fee), 0), sum(payout > 0)
                          FROM positions WHERE bot=? AND status='settled'""", (b.key,)).fetchone()
        army.append((r[1], b.key, r[0], r[2] or 0))
    army.sort(reverse=True)
    luck = [a for a in army if a[1].startswith("luck-")]
    ready = [b.key for b in ARMY if scale_check(db, b.key, bar)[0]]
    print(f"\n### The army ({len(ARMY)} bots)\n")
    if luck:
        print(f"Luck line: the best random bot ({luck[0][1]}) is at {luck[0][0]:+.2f} settled. Anything below it proves nothing.\n")
    print("| Top 5 | Settled profit | Settled bets |")
    print("| --- | --- | --- |")
    print("\n".join(f"| {k} | {p:+.2f} | {n} ({w}W/{n - w}L) |" for p, k, n, w in army[:5]))
    if ready:
        flags.append(f"**Army bot(s) passed the bar (t-score {bar:.1f}+): {', '.join(ready)}. Look before trusting it.**")
    print("\n### Needs a change?\n")
    if flags:
        print("\n".join(f"- {f}" for f in flags))
    else:
        print("- No. Nothing unusual. Bots stay frozen until they reach "
              f"{MIN_SETTLED_FOR_VERDICT} settled trades, so the answer is usually \"keep waiting\".")
    print("\n<sub>Rules: never edit a bot mid-test. A change = a new version with a fresh $100 record.</sub>")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "due":
        sys.exit(0 if due(connect()) else 1)
    elif cmd == "posted":
        posted(connect())
    else:
        main()
