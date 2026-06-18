"""Account progression: experience/levels and a smoothed skill rating -> tiers.

Pure functions over plain numbers, with no imports, so both the account store
and any display layer can use them without cycles. Two independent ladders:

* XP / level -- a monotonic "you played" number that only ever goes up, so even
  a losing race feels like progress. The curve is quadratic-ish: each level
  costs a bit more than the last.
* Skill rating -- a smoothed (EWMA) estimate of accuracy-gated net WPM, mapped
  to named tiers (Bronze .. Grandmaster). Unlike a one-off peak WPM, it tracks
  current form and is deterministic (no pairwise Elo bookkeeping).
"""

# ---------------------------------------------------------------------------
# Experience points and levels
# ---------------------------------------------------------------------------
# XP awarded for a single race. Tuned so a typical ~60 WPM win nets a few
# hundred XP and early levels arrive within a handful of races.
XP_PER_CHAR = 1.0
XP_FINISH_BONUS = 40.0
XP_WIN_BONUS = 60.0
XP_PODIUM_BONUS = 25.0
XP_FLAWLESS_BONUS = 50.0
XP_ACCURACY_BONUS = 60.0          # scaled by accuracy fraction

# Level N requires this many *cumulative* XP. Level 1 starts at 0.
_LEVEL_BASE = 300.0
_LEVEL_GROWTH = 1.18


def xp_for_race(*, chars, accuracy, won, place, flawless):
    """XP earned by one finished race. Always >= 0; never punishes a loss."""
    xp = max(0, int(chars)) * XP_PER_CHAR
    xp += XP_ACCURACY_BONUS * max(0.0, min(1.0, accuracy / 100.0))
    if won:
        xp += XP_WIN_BONUS
    if place is not None and place <= 3:
        xp += XP_PODIUM_BONUS
    if flawless:
        xp += XP_FLAWLESS_BONUS
    if chars > 0:
        xp += XP_FINISH_BONUS
    return round(xp, 1)


def _cumulative_xp(level):
    """Total XP needed to *reach* ``level`` (level 1 == 0 XP)."""
    if level <= 1:
        return 0.0
    total = 0.0
    step = _LEVEL_BASE
    for _ in range(level - 1):
        total += step
        step *= _LEVEL_GROWTH
    return total


def level_for(total_xp):
    """Return the integer level reached at ``total_xp`` (starts at 1)."""
    total_xp = max(0.0, float(total_xp or 0.0))
    level = 1
    while _cumulative_xp(level + 1) <= total_xp and level < 999:
        level += 1
    return level


def level_progress(total_xp):
    """Return (level, xp_into_level, xp_needed_for_next_level)."""
    total_xp = max(0.0, float(total_xp or 0.0))
    level = level_for(total_xp)
    floor = _cumulative_xp(level)
    ceil = _cumulative_xp(level + 1)
    into = total_xp - floor
    need = ceil - floor
    return level, round(into, 1), round(need, 1)


# ---------------------------------------------------------------------------
# Skill rating and tiers
# ---------------------------------------------------------------------------
RATING_SMOOTHING = 0.25           # weight of the newest race in the EWMA
RATING_MAX = 250.0

# (name, lower-bound rating). Highest first match wins in tier_for().
TIERS = (
    ("Grandmaster", 130.0),
    ("Master", 110.0),
    ("Diamond", 90.0),
    ("Platinum", 72.0),
    ("Gold", 55.0),
    ("Silver", 38.0),
    ("Bronze", 0.0),
)


def update_rating(old_rating, net_wpm, accuracy):
    """Blend a race's accuracy-gated WPM into the running skill rating.

    Pass ``old_rating=None`` for the first (unseeded) race. A stored rating of
    exactly 0.0 is a *real* value (e.g. a DNF race) and still blends -- it is not
    treated as unseeded, so smoothing isn't silently lost.
    """
    sample = max(0.0, float(net_wpm)) * max(0.0, min(1.0, accuracy / 100.0))
    if old_rating is None:
        new = sample                      # first race seeds the rating
    else:
        new = (1.0 - RATING_SMOOTHING) * old_rating + RATING_SMOOTHING * sample
    return round(max(0.0, min(RATING_MAX, new)), 1)


def tier_for(rating):
    """Map a skill rating to a tier name."""
    rating = max(0.0, float(rating or 0.0))
    for name, lo in TIERS:
        if rating >= lo:
            return name
    return "Bronze"


def tier_index(rating):
    """0 (Bronze) .. len(TIERS)-1 (Grandmaster), for compact display/sorting."""
    name = tier_for(rating)
    order = [t[0] for t in reversed(TIERS)]   # Bronze .. Grandmaster
    return order.index(name) if name in order else 0
