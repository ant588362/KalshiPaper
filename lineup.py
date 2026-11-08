"""Who is live. strategies.py has the original bots, recruits.py the new idea families; this file decides which
of them trade. The run, the digest and the dashboard all read the lineup from here.

2026-11-08: the training ground (trainer/) replayed every idea over ~2 months of past Kalshi markets.
RECRUITS = new ideas that passed it. RETIRE = live army bots that clearly failed it (records kept, open bets
still settle, they just stop buying). Luck bots are never retired: they're the control.
"""
from recruits import build_recruits
from strategies import ARMY as ORIGINAL_ARMY
from strategies import CORE

LIVE_RECRUITS = set()
RETIRE = set()

for _b in ORIGINAL_ARMY:
    if _b.key in RETIRE and not _b.key.startswith("luck-"):
        _b.retired = True

RECRUITS = [b for b in build_recruits() if b.key in LIVE_RECRUITS]
ARMY = ORIGINAL_ARMY + RECRUITS
BOTS = CORE + ARMY
