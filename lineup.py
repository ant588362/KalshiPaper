"""Who is live. strategies.py has the original bots, recruits.py the new idea families; this file decides which
of them trade. The run, the digest and the dashboard all read the lineup from here.

2026-11-08: the training ground (trainer/) replayed every idea over ~2 months of past Kalshi markets.
RECRUITS = new ideas that passed it. RETIRE = live army bots that clearly failed it (records kept, open bets
still settle, they just stop buying). Luck bots are never retired: they're the control.
"""
from recruits import build_recruits
from strategies import ARMY as ORIGINAL_ARMY
from strategies import CORE
from weather2 import build_weather2

# 2026-11-08 run (trainer/results.txt): 0 of 500 strategies passed the full rule. These 13 made money in BOTH
# halves (40+ bets) but aren't proven; they go live as a watchlist so real trading can prove or kill them.
LIVE_RECRUITS = {
    "yesonly-crypto-92-97-6h-v1", "yesonly-finance-92-97-6h-v1", "btc-70-80-48h-v1", "noonly-crypto-85-92-6h-v1",
    "eth-70-80-12h-v1", "oil-88-96-48h-v1", "yesonly-crypto-65-75-48h-v1", "eth-70-80-48h-v1",
    "yesonly-crypto-65-75-24h-v1", "btc-88-96-48h-v1", "yesonly-crypto-55-65-48h-v1", "top-k5-60-80-12h-v1",
    "yesonly-crypto-55-65-24h-v1",
}
# Live army bots that lost money in BOTH halves of the replay (mostly cheap longshots and coin flips).
RETIRE = {
    "crypto-21-33-48h-v1", "crypto-21-33-6h-v1", "crypto-33-45-48h-v1", "crypto-45-55-48h-v1", "crypto-45-55-6h-v1",
    "crypto-70-78-12h-v1", "crypto-80-88-12h-v1", "finance-21-33-48h-v1", "finance-21-33-6h-v1", "finance-33-45-48h-v1",
    "finance-70-78-12h-v1", "finance-70-78-48h-v1", "finance-70-78-4h-v1", "finance-80-88-12h-v1", "finance-80-88-48h-v1",
    "finance-80-88-4h-v1", "other-80-88-48h-v1", "sports-88-96-12h-v1", "weather-70-78-12h-v1", "weather-70-78-48h-v1",
    "weather-80-88-12h-v1", "weather-80-88-48h-v1", "weather-88-96-12h-v1", "weather-88-96-48h-v1",
}

for _b in ORIGINAL_ARMY:
    if _b.key in RETIRE and not _b.key.startswith("luck-"):
        _b.retired = True

RECRUITS = [b for b in build_recruits() if b.key in LIVE_RECRUITS]
# 2026-11-09: weather-v2 = same-day highs using the recorded max so far + a 7-source forecast blend (weather2.py).
# Not backtestable (no free archive of old forecasts), so all 16 settings go live and the army judges them.
WEATHER2 = build_weather2()
ARMY = ORIGINAL_ARMY + RECRUITS + WEATHER2
BOTS = CORE + ARMY
