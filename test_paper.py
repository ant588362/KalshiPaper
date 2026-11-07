"""py -m unittest test_paper.py"""
import unittest

from fees import asks_for, plan_buy, taker_fee
from run import START_CASH, worst_drawdown
from strategies import BOTS, Favorite, Momentum, Reversion


class Fees(unittest.TestCase):
    def test_known_example(self):
        # 100 contracts at 50c: 0.07 x 100 x 0.5 x 0.5 = $1.75
        self.assertEqual(taker_fee([(0.50, 100)]), 1.75)

    def test_rounds_up_to_the_cent(self):
        # 1 contract at 50c = $0.0175 -> $0.02
        self.assertEqual(taker_fee([(0.50, 1)]), 0.02)

    def test_multiplier(self):
        self.assertEqual(taker_fee([(0.50, 100)], 0.5), 0.88)


class Fills(unittest.TestCase):
    book = {"yes": [(0.40, 10), (0.42, 5)], "no": [(0.55, 3), (0.50, 100)]}

    def test_asks_come_from_the_other_side(self):
        # NO bids at 55c and 50c are YES asks at 45c and 50c
        self.assertEqual(asks_for("yes", self.book), [(0.45, 3), (0.5, 100)])
        self.assertEqual(asks_for("no", self.book)[0], (0.58, 5))

    def test_walks_the_book_and_stays_in_budget(self):
        fills, cost, fee = plan_buy("yes", self.book, budget=3.00, max_price=0.60)
        self.assertEqual(fills[0], (0.45, 3))  # cheapest level first, only what's posted
        self.assertLessEqual(cost + fee, 3.00)
        self.assertGreater(sum(c for _, c in fills), 3)

    def test_respects_max_price(self):
        fills, _, _ = plan_buy("yes", self.book, budget=50, max_price=0.46)
        self.assertEqual(fills, [(0.45, 3)])

    def test_nothing_posted_nothing_bought(self):
        self.assertIsNone(plan_buy("yes", {"yes": [], "no": []}, 10, 0.9))


def market(**kw):
    m = {"ticker": "T", "yes_bid": 0.5, "yes_ask": 0.52, "no_bid": 0.48, "no_ask": 0.5, "hours_left": 10, "now": 100000}
    m.update(kw)
    return m


class Bots(unittest.TestCase):
    def test_favorite_buys_the_side_priced_85_to_95(self):
        self.assertEqual(Favorite().decide(market(yes_ask=0.90), []), ("yes", 0.95))
        self.assertEqual(Favorite().decide(market(yes_ask=0.10, no_ask=0.91), []), ("no", 0.95))
        self.assertIsNone(Favorite().decide(market(yes_ask=0.90, hours_left=30), []))

    def test_momentum_and_reversion_are_opposites(self):
        hist = [(100000 - 6 * 3600, 0.30)]  # mid was 30c six hours ago, now 51c
        self.assertEqual(Momentum().decide(market(), hist)[0], "yes")
        self.assertEqual(Reversion().decide(market(), hist)[0], "no")
        self.assertIsNone(Momentum().decide(market(), []), "no history, no trade")

    def test_unique_keys_and_stable_fingerprints(self):
        self.assertEqual(len({b.key for b in BOTS}), len(BOTS))
        self.assertEqual(BOTS[2].fingerprint(), BOTS[2].fingerprint())


class Weather(unittest.TestCase):
    def test_bracket_chances(self):
        from strategies import bracket_prob
        # forecast 73F, sd 3.5: "<70" (69 or lower) is about 16%; a range and the two sides around it add to 1
        self.assertAlmostEqual(bracket_prob("less", None, 70, 73, 3.5), 0.159, places=2)
        lo = bracket_prob("less", None, 72, 73, 3)
        mid_ = bracket_prob("between", 72, 73, 73, 3)
        hi = bracket_prob("greater", 73, None, 73, 3)
        self.assertAlmostEqual(lo + mid_ + hi, 1.0, places=6)
        self.assertIsNone(bracket_prob("weird", 1, 2, 73, 3))

    def test_reads_station_and_date(self):
        import weather
        self.assertEqual(weather.station_from_rules("recorded at New York City (CLINYC) for Nov 4"), "KNYC")
        self.assertEqual(weather.market_date("KXHIGHNY-26NOV04-T77"), "2026-11-04")
        self.assertIsNone(weather.station_from_rules("no station here"))

    # The market's view of one city-day: centred near 70F, about +/-2F.
    COOL = [{"strike_type": "less", "floor_strike": None, "cap_strike": 70, "mid": 0.45},
            {"strike_type": "between", "floor_strike": 70, "cap_strike": 71, "mid": 0.30},
            {"strike_type": "between", "floor_strike": 72, "cap_strike": 73, "mid": 0.15},
            {"strike_type": "greater", "floor_strike": 73, "cap_strike": None, "mid": 0.10}]

    def wx(self, day="2099-01-02", code="99JAN02", nws=73, siblings=None, **kw):
        import weather
        from strategies import WeatherForecast
        weather._forecasts["KNYC"] = (10 ** 12, {day: nws}, "America/New_York")  # never expires in the test
        m = market(event=f"KXHIGHNY-{code}", ticker=f"KXHIGHNY-{code}-T70", rules="recorded at NYC (CLINYC)",
                   strike_type="less", floor_strike=None, cap_strike=70, siblings=self.COOL if siblings is None else siblings)
        m.update(kw)
        return WeatherForecast().decide(m, [])

    def test_market_view(self):
        from strategies import market_view
        center, spread = market_view(self.COOL)
        self.assertAlmostEqual(center, 70.3, places=1)
        self.assertAlmostEqual(spread, 2.0, places=1)
        self.assertIsNone(market_view(self.COOL[:2]), "too few ranges to read")

    def test_buys_when_nws_disagrees_with_the_market(self):
        # Market centres ~70F and prices "under 70" at ~46%. NWS says 73F, so under 70 is unlikely: buy NO at 55c.
        side, max_price, stake, note = self.wx(yes_ask=0.47, yes_bid=0.45, no_ask=0.55, no_bid=0.53)
        self.assertEqual(side, "no")
        self.assertLessEqual(max_price, 0.90)
        self.assertTrue(0.5 <= stake <= 5.0)
        self.assertIn("NWS 73F", note)

    def test_no_trade_when_nws_agrees(self):
        self.assertIsNone(self.wx(nws=70, yes_ask=0.47, yes_bid=0.45, no_ask=0.55, no_bid=0.53))

    def test_no_trade_without_the_event_prices(self):
        self.assertIsNone(self.wx(siblings=[], yes_ask=0.47, no_ask=0.55))

    def test_only_future_days_and_only_highs(self):
        self.assertIsNone(self.wx(event="KXLOWTNYC-99JAN02", yes_ask=0.47, no_ask=0.55))
        self.assertIsNone(self.wx(day="2000-01-02", code="00JAN02", yes_ask=0.47, no_ask=0.55),
                          "a day that isn't in the future is skipped")


class ScaleCheck(unittest.TestCase):
    def test_needs_100_trades_profit_and_t_score(self):
        import sqlite3
        from run import scale_check
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE positions (bot TEXT, status TEXT, payout REAL, cost REAL, fee REAL)")
        rows = [("a", "settled", 1.0, 0.9, 0.0)] * 60 + [("a", "settled", 1.0, 0.8, 0.0)] * 60  # small, steady profit
        rows += [("b", "settled", 1.0, 0.9, 0.0)] * 30                                         # too few trades
        rows += [("c", "settled", 3.0, 1.5, 0.0), ("c", "settled", 0.0, 1.5, 0.0)] * 60        # exactly breakeven
        db.executemany("INSERT INTO positions VALUES (?,?,?,?,?)", rows)
        self.assertTrue(scale_check(db, "a")[0])
        self.assertFalse(scale_check(db, "b")[0])
        self.assertFalse(scale_check(db, "c")[0])


class Lineup(unittest.TestCase):
    def test_retired_bots_kept_new_ones_added(self):
        keys = {b.key: b for b in BOTS}
        for k in ("random-v1", "momentum-v1", "reversion-v1"):
            self.assertTrue(keys[k].retired)
        for k in ("favorite-v1", "random-v2", "weather-v1"):
            self.assertFalse(keys[k].retired)


class Drawdown(unittest.TestCase):
    def test_peak_to_trough(self):
        rows = [{"equity": e} for e in (START_CASH, 110, 95, 105, 90, 120)]
        self.assertEqual(worst_drawdown(rows), 20)


class DailyUpdate(unittest.TestCase):
    def test_posts_once_on_the_first_run_after_13_utc(self):
        import sqlite3
        from datetime import datetime, timezone
        from digest import due, posted
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        at = lambda h, d=28: datetime(2026, 10, d, h, 24, tzinfo=timezone.utc)
        self.assertFalse(due(db, at(12)), "too early")
        self.assertTrue(due(db, at(14)), "13:xx run was skipped, the 14:24 run still posts")
        posted(db, at(14))
        self.assertFalse(due(db, at(20)), "already posted today")
        self.assertTrue(due(db, at(13, 29)), "next day")


class Records(unittest.TestCase):
    def test_same_underlying_is_grouped(self):
        from run import underlying
        self.assertEqual(underlying("KXBTCD-26OCT2817"), underlying("KXBTC-26OCT2817"))
        self.assertEqual(underlying("KXWTI-26OCT2914"), underlying("KXBRENTD-26OCT2917"))
        self.assertEqual(underlying("KXNCAAFSPREAD-26OCT28KENNMTU"), underlying("KXNCAAFTOTAL-26OCT28KENNMTU"))
        self.assertNotEqual(underlying("KXHIGHTLV-26OCT28"), underlying("KXHIGHPHIL-26OCT28"))

    def test_old_ledgers_get_the_new_columns(self):
        import os, sqlite3, tempfile
        import run
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE positions (id INTEGER PRIMARY KEY, bot TEXT, ticker TEXT, event TEXT, title TEXT,
            side TEXT, contracts INT, avg_price REAL, cost REAL, fee REAL, opened_at TEXT,
            status TEXT DEFAULT 'open', result TEXT, payout REAL, settled_at TEXT, UNIQUE (bot, ticker))""")
        old.commit()
        old.close()
        saved, run.DB = run.DB, path
        try:
            cols = {r[1] for r in run.connect().execute("PRAGMA table_info(positions)")}
        finally:
            run.DB = saved
        self.assertTrue({"fills", "fee_type", "fee_multiplier"} <= cols)


class Army(unittest.TestCase):
    def test_size_names_and_core_untouched(self):
        from strategies import ARMY, CORE
        self.assertGreaterEqual(len(ARMY), 150)
        self.assertEqual([b.key for b in CORE], ["cash-v1", "random-v1", "favorite-v1", "momentum-v1", "reversion-v1",
                                                 "random-v2", "weather-v1"])
        rules = [b for b in ARMY if not b.key.startswith("luck-")]  # luck bots share rules; their seed is their name
        self.assertEqual(len({b.fingerprint() for b in rules}), len(rules), "every strategy bot has different rules")
        self.assertEqual(sum(b.key.startswith("luck-") for b in ARMY), 30)

    def test_core_fingerprints_did_not_move(self):
        # The live ledger stores these. If one changes, the cloud run stops with STOP: rules changed.
        import os, sqlite3, subprocess
        raw = subprocess.run(["git", "show", "origin/state:paper.db"], capture_output=True).stdout
        if not raw:
            self.skipTest("no cloud ledger here")
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fp-check.db")
        open(path, "wb").write(raw)
        con = sqlite3.connect(path)
        try:
            saved = dict(con.execute("SELECT key, fingerprint FROM bots"))
        finally:
            con.close()
            os.remove(path)
        from strategies import CORE
        for b in CORE:
            if b.key in saved:
                self.assertEqual(saved[b.key], b.fingerprint(), b.key)

    def test_category(self):
        from strategies import category
        self.assertEqual(category("KXHIGHNY-26NOV07"), "weather")
        self.assertEqual(category("KXBTCD-26NOV0717"), "crypto")
        self.assertEqual(category("KXINXU-26NOV06H1600"), "finance")
        self.assertEqual(category("KXNCAAMBTOTAL-26NOV06NEUVA"), "sports")
        self.assertEqual(category("KXBIGGESTQUAKE-06NOV26"), "other")

    def test_band_picks_the_cheaper_side_in_range(self):
        from strategies import Band
        fav = Band("t", {"lo": 0.85, "hi": 0.95, "hours_left": 24, "cat": "any"})
        self.assertEqual(fav.decide(market(event="KXBTCD-1", yes_ask=0.90, no_ask=0.12), []), ("yes", 0.95))
        flip = Band("t", {"lo": 0.45, "hi": 0.55, "hours_left": 24, "cat": "crypto"})
        self.assertEqual(flip.decide(market(event="KXBTCD-1", yes_ask=0.52, no_ask=0.50), []), ("no", 0.55))
        self.assertIsNone(flip.decide(market(event="KXNFLGAME-1", yes_ask=0.52, no_ask=0.50), []), "wrong market type")
        self.assertIsNone(fav.decide(market(event="KXBTCD-1", yes_ask=0.90, hours_left=30), []), "too far from close")

    def test_bar_rises_with_more_bots(self):
        from run import t_bar
        self.assertEqual(t_bar(1), 2.0)
        self.assertAlmostEqual(t_bar(200), 3.66, places=1)


if __name__ == "__main__":
    unittest.main()
