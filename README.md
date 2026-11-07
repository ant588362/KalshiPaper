# Kalshi paper trading

Pretend money, real Kalshi prices. Nothing in here can place a real order: it only reads Kalshi's public
market data (no account or API key).

```
py run.py tick        # settle, trade, report. Runs every 30 min via tick.bat (Windows Task Scheduler)
py run.py report      # rewrite the report from what's saved
py -m unittest test_paper.py
```

**In the cloud:** `.github/workflows/tick.yml` runs a tick every 5 minutes on GitHub (always-on loop). The ledger and the latest
report are on the `state` branch (`latest/report.txt`). The first cloud run starts from `seed/paper.db`.

Reports land in `reports/<date_time>/` and `reports/latest/`: `report.txt` (one line per bot),
`summary.csv`, `positions.csv` (every trade), `equity.csv` (every tick).

## The bots (strategies.py)

| Bot | Rule |
| --- | --- |
| cash | never trades: the bar every bot must beat after fees |
| random-v2 | buys a random side on ~1 in 20 markets (the control, under the 20c floor) |
| favorite | buys the side priced 85-95c in the last 24h (favorite-longshot bias) |
| weather | tomorrow's high-temp markets: NWS forecast for the settlement station vs. what the event's prices imply. Trades only when they disagree by 1.5F+, takes the spread from the market, needs 10c edge after fees, bet size grows with edge (quarter-Kelly, max $5) |
| retired | random-v1 (out of cash), momentum-v1, reversion-v1: records kept, open bets settle, no new trades |

**No longshots (2026-11-03):** no bot buys a side priced under 21c. Across ~400 settled trades, 20c-and-under buys lost $161.

**Ready for more money?** The report checks each live bot: 100+ settled trades, profit after fees, and a t-score of 2+
(average profit per trade at least twice its own noise). Only a bot that passes should get bigger bets or real money.

**Frozen versions.** The first run of each bot fingerprints its params and code. If they change, the run
stops and asks for a new `version`. A new version starts its own $100 record; old ones are kept.

## How trades are simulated

- Same market filter for every bot: closes in 0.5-48h, 24h volume >= 50, spread <= 6c, not a combo market.
- Buys walk the real posted asks (a NO bid at x = a YES ask at 1-x), only up to the size posted, never above the bot's max price.
- Fees: each series' `fee_type` and `fee_multiplier` come from `GET /series`. Every trade pays the taker fee
  `0.07 x C x P x (1-P) x multiplier`, rounded up to the cent per order (slightly pessimistic). Series with
  `flat` fees are skipped until that table is verified.
- Hold to settlement. Early exits aren't simulated: you can only sell into someone's posted bid.
- Equity marks open positions at the current BID (what you could sell for), so drawdown is honest.
- Results other than yes/no (e.g. cancelled at a fair price) are marked `review`, not guessed.

## Limits (run.py)

| Limit | Value | Why |
| --- | --- | --- |
| Per trade | $3 | one wrong bet costs at most 3% of the start |
| Per event | $6 | markets in one event move together (brackets), so cap them together |
| Deployed | $50 | at most half is ever at risk; if every open bet lost, half is left |
| Per market | 1 entry | no adding to a loser |

## Judging

Net profit after fees vs cash, worst drawdown, settled trade count. No verdict before 100 settled trades.
Win rate alone means nothing: favorite wins most bets and can still lose money.

## Checked against Kalshi's docs on 2026-10-26

- Public market data needs no auth: docs.kalshi.com/getting_started/quick_start_market_data
- Demo environment has fake funds and prices that "may not be reflective of real markets", so we read production prices.
- Fee rounding: docs.kalshi.com/getting_started/fee_rounding. Fee PDF (kalshi.com/docs/kalshi-fee-schedule.pdf) still to re-check: it was rate-limited.
- Data is stored locally for this test only. Don't publish or resell it.
- Real-money trading needs an adult's own account. Paper only here.
