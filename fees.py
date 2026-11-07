"""Kalshi trading fees, and what a buy would really cost against the posted book.

Fee model (checked 2026-10-26):
- Each series reports its own fee_type and fee_multiplier (GET /series/{ticker}).
- 'quadratic' (General Trading Fees table): taker fee = 0.07 x C x P x (1 - P) x multiplier.
  The official PDF was rate-limiting us; the 0.07 rate comes from several fee guides that agree with it,
  and docs.kalshi.com/getting_started/fee_rounding.md.
- Maker fees only apply to resting orders. This simulator only takes posted prices, so every trade pays
  the taker fee, which is the higher one.
- We round the whole order's fee UP to the next cent. Kalshi rounds finer for some members and rebates
  overpayment, so this is a little pessimistic on purpose.
- 'flat' series use a different table we haven't verified, so they are skipped, not guessed.
"""
import math

TAKER_RATE = 0.07
SUPPORTED = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


def taker_fee(fills, fee_multiplier=1.0):
    """fills: [(price_dollars, contracts)]. Fee for one order, rounded up to the cent."""
    raw = sum(TAKER_RATE * c * p * (1 - p) for p, c in fills) * fee_multiplier
    return math.ceil(round(raw * 100, 6)) / 100


def asks_for(side, book):
    """Ask ladder to BUY `side`, cheapest first. A NO bid at x is a YES ask at 1 - x, and the reverse."""
    other = book["no" if side == "yes" else "yes"]
    return sorted((round(1 - p, 4), q) for p, q in other if q > 0)


def plan_buy(side, book, budget, max_price, fee_multiplier=1.0):
    """Buy whole contracts of `side`, walking up the asks, never above max_price, total cost incl. fee <= budget.
    Returns (fills, cost_without_fee, fee) or None if nothing can be bought. Only what's posted can fill."""
    fills, spent = [], 0.0
    for price, qty in asks_for(side, book):
        if price > max_price:
            break
        n = int(qty)
        while n > 0:
            trial = fills + [(price, n)]
            cost = spent + price * n
            if cost + taker_fee(trial, fee_multiplier) <= budget + 1e-9:
                fills, spent = trial, cost
                break
            n = min(n - 1, int((budget - spent) / price) if price else 0)
        if n < int(qty):  # budget ran out inside this level
            break
    if not fills:
        return None
    return fills, round(spent, 4), taker_fee(fills, fee_multiplier)
