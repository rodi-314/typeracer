"""Cumulative milestone ledger -- long progress bars over an account's stats.

Where achievements are binary unlocks, milestones are *arcs*: "342,118 / 1,000,000
characters typed". They derive entirely from existing stat fields, so there is
nothing to persist -- ``progress(stats)`` recomputes them on demand for the
profile overlay.
"""


class _Milestone:
    __slots__ = ("id", "label", "stat", "target", "fmt")

    def __init__(self, id, label, stat, target, fmt=None):
        self.id = id
        self.label = label
        self.stat = stat          # stats key, or a callable(stats) -> number
        self.target = target
        self.fmt = fmt or (lambda v: str(int(v)))


def _races(s):
    return s.get("races_played", 0)


MILESTONES = [
    _Milestone("chars", "Characters typed", "total_chars", 1_000_000,
               lambda v: f"{int(v):,}"),
    _Milestone("races", "Races finished", "races_played", 500),
    _Milestone("wins", "Races won", "races_won", 100),
    _Milestone("time", "Time on the track", "total_time", 36_000,
               lambda v: f"{v / 3600.0:.1f}h"),
    _Milestone("keys", "Keystrokes", "total_keystrokes", 2_000_000,
               lambda v: f"{int(v):,}"),
    _Milestone("xp", "Experience", "total_xp", 100_000,
               lambda v: f"{int(v):,}"),
]


def _value(stat, stats):
    if callable(stat):
        try:
            return float(stat(stats) or 0)
        except Exception:
            return 0.0
    return float(stats.get(stat, 0) or 0)


def progress(stats):
    """Return a list of milestone progress dicts for ``stats``."""
    out = []
    for m in MILESTONES:
        cur = _value(m.stat, stats)
        pct = 0.0 if m.target <= 0 else max(0.0, min(100.0, 100.0 * cur / m.target))
        out.append({
            "id": m.id,
            "label": m.label,
            "current": m.fmt(cur),
            "target": m.fmt(m.target),
            "pct": round(pct, 1),
            "done": cur >= m.target,
        })
    return out
