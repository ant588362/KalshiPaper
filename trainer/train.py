"""Training ground, step 3: replay EVERY candidate over the past and pick who goes live.

Candidates: the live army (minus weather, which can't be replayed), the core favorite bot, 100 luck bots
(the 30 live ones + 70 replay-only, so we know what luck looks like here), and the new recruits (recruits.py).

Writes trainer/results.csv (one row per bot) and trainer/results.txt (plain words). Usage:
  py trainer/train.py            (all cores)
  py trainer/train.py --quick    (first 20 bots only, to test)

The pass rule (decided BEFORE seeing any results):
  TRAIN (closed before Oct 16): 30+ settled bets, profit after fees, t-score >= 2.0
  TEST  (Oct 16 on, never used to choose): 20+ settled bets, profit after fees, t-score >= 1.0
  and it beats the luck line: its TRAIN t-score is above what the best 5% of luck bots reached.
"""
import csv
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))


def candidates():
    import strategies as S
    import recruits as R
    live = [b for b in S.CORE if b.name == "favorite" and b.version == 1] + \
           [b for b in S.ARMY if not isinstance(b, S.WeatherForecast)]
    extra_luck = [S.Luck(f"luck-{i:02d}", S.RandomPick.params) for i in range(31, 101)]
    return live + extra_luck + R.build_recruits()


def family(b):
    import strategies as S
    if isinstance(b, S.RandomPick):
        return "luck"
    if getattr(b, "recruit", False):
        return "new:" + type(b).__name__
    return "live"


_data = None


def work(idx):
    global _data
    import replay
    if _data is None:
        _data = replay.Data()
    bots = candidates()
    chunk = [bots[i] for i in idx]
    return replay.replay(chunk, _data)


def main(quick=False):
    import replay
    bots = candidates()
    if quick:
        bots = bots[:20]
    n = len(bots)
    procs = max(1, min(3, (os.cpu_count() or 2) - 1))  # each worker holds the data (~550MB)
    chunks = [list(range(i, n, procs * 3)) for i in range(min(n, procs * 3))]
    t = time.time()
    results = {}
    with Pool(procs) as pool:
        for k, res in enumerate(pool.imap_unordered(work, chunks)):
            results.update(res)
            print(f"chunk {k + 1}/{len(chunks)} done, {time.time() - t:.0f}s", flush=True)

    rows = []
    for b in bots:
        bets = results.get(b.key, [])
        tr, te = replay.split(bets)
        a, s1, s2 = replay.stats(bets), replay.stats(tr), replay.stats(te)
        rows.append({"bot": b.key, "family": family(b), "class": type(b).__name__, "params": repr(sorted(b.params.items())),
                     "n": a[0], "profit": round(a[1], 2), "per_bet": round(a[2], 4), "t": round(a[3], 2),
                     "win_rate": round(a[4], 3), "avg_price": round(a[5], 3),
                     "train_n": s1[0], "train_profit": round(s1[1], 2), "train_t": round(s1[3], 2),
                     "test_n": s2[0], "test_profit": round(s2[1], 2), "test_t": round(s2[3], 2)})
    luck_t = sorted(r["train_t"] for r in rows if r["family"] == "luck")
    luck_line = luck_t[int(0.95 * (len(luck_t) - 1))] if luck_t else 0.0
    for r in rows:
        r["passes"] = (r["family"] != "luck" and r["train_n"] >= 30 and r["train_profit"] > 0 and r["train_t"] >= 2.0
                       and r["train_t"] > luck_line and r["test_n"] >= 20 and r["test_profit"] > 0 and r["test_t"] >= 1.0)
    with open(os.path.join(HERE, "results.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -r["t"]))
    print(f"{n} bots replayed in {time.time() - t:.0f}s. Luck line (95th pct of luck TRAIN t): {luck_line:.2f}")
    print(f"PASS: {sum(r['passes'] for r in rows)}")
    write_summary(rows, luck_line)
    return rows, luck_line


def write_summary(rows, luck_line):
    fams = {}
    for r in rows:
        fams.setdefault(r["family"], []).append(r)
    lines = [f"Training ground: {len(rows)} bots replayed. TRAIN = closed before Oct 16, TEST = Oct 16 on.",
             f"Luck line: the best 5% of luck bots reached a TRAIN t-score of {luck_line:.2f}.", "",
             "By family (settled profit after fees, all bots in the family added up):"]
    for f, rs in sorted(fams.items(), key=lambda kv: -sum(r["profit"] for r in kv[1]) / len(kv[1])):
        tot, bets = sum(r["profit"] for r in rs), sum(r["n"] for r in rs)
        lines.append(f"  {f:22s} {len(rs):3d} bots  {bets:6d} bets  {tot:+9.2f}  ({tot / max(1, bets) * 100:+.1f}c/bet)  "
                     f"passed {sum(r['passes'] for r in rs)}")
    lines += ["", "PASSED (best first by overall t-score):"]
    for r in sorted((r for r in rows if r["passes"]), key=lambda r: -r["t"]):
        lines.append(f"  {r['bot']:40s} train {r['train_profit']:+7.2f} (t {r['train_t']:.1f}, {r['train_n']})  "
                     f"test {r['test_profit']:+7.2f} (t {r['test_t']:.1f}, {r['test_n']})  win {r['win_rate']:.0%} at {r['avg_price']:.2f}")
    lines += ["", "LIVE army bots that LOST in both train and test:"]
    for r in sorted((r for r in rows if r["family"] == "live" and r["train_profit"] < 0 and r["test_profit"] < 0),
                    key=lambda r: r["profit"]):
        lines.append(f"  {r['bot']:40s} {r['profit']:+8.2f} on {r['n']} bets")
    with open(os.path.join(HERE, "results.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    main("--quick" in sys.argv)
