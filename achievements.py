"""Server-side achievement definitions and evaluation.

Each achievement has a stable id, a label/description for display, and a
predicate over an account's stats dict. Evaluation is pure and idempotent: it
returns the set of ids the account currently qualifies for, and the caller
diffs that against already-unlocked ids to find newly-earned ones.
"""


class Achievement:
    __slots__ = ("id", "label", "desc", "predicate")

    def __init__(self, id, label, desc, predicate):
        self.id = id
        self.label = label
        self.desc = desc
        self.predicate = predicate


# Ordered roughly easy -> hard for display.
ACHIEVEMENTS = [
    Achievement("first_blood", "First Blood", "Win your first race",
                lambda s: s["races_won"] >= 1),
    Achievement("rookie", "Rookie", "Finish 5 races",
                lambda s: s["races_played"] >= 5),
    Achievement("veteran", "Veteran", "Finish 50 races",
                lambda s: s["races_played"] >= 50),
    Achievement("flawless", "Flawless", "Finish a race with 100% accuracy",
                lambda s: s.get("flawless_races", 0) >= 1),
    Achievement("sharpshooter", "Sharpshooter", "Reach 99% best accuracy",
                lambda s: s["best_accuracy"] >= 99.0),
    Achievement("half_century", "Half Century", "Reach 50 WPM",
                lambda s: s["best_wpm"] >= 50),
    Achievement("ton_up", "Ton Up", "Reach 100 WPM",
                lambda s: s["best_wpm"] >= 100),
    Achievement("speed_demon", "Speed Demon", "Reach 120 WPM",
                lambda s: s["best_wpm"] >= 120),
    Achievement("champion", "Champion", "Win 10 races",
                lambda s: s["races_won"] >= 10),
    Achievement("streak_3", "On a Roll", "Win 3 races in a row",
                lambda s: s.get("longest_streak", 0) >= 3),
    Achievement("streak_5", "Unstoppable", "Win 5 races in a row",
                lambda s: s.get("longest_streak", 0) >= 5),
]

_BY_ID = {a.id: a for a in ACHIEVEMENTS}


def label_for(achievement_id):
    a = _BY_ID.get(achievement_id)
    return a.label if a else achievement_id


def info(achievement_id):
    a = _BY_ID.get(achievement_id)
    if not a:
        return {"id": achievement_id, "label": achievement_id, "desc": ""}
    return {"id": a.id, "label": a.label, "desc": a.desc}


def qualifying(stats):
    """Return the set of achievement ids this stats dict currently earns."""
    earned = set()
    for a in ACHIEVEMENTS:
        try:
            if a.predicate(stats):
                earned.add(a.id)
        except Exception:
            pass
    return earned
