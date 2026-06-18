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
    # -- volume / endurance ------------------------------------------------
    Achievement("centurion", "Centurion", "Finish 100 races",
                lambda s: s["races_played"] >= 100),
    Achievement("marathoner", "Marathoner", "Type 100,000 characters",
                lambda s: s.get("total_chars", 0) >= 100_000),
    Achievement("scribe", "Scribe", "Type 1,000,000 characters",
                lambda s: s.get("total_chars", 0) >= 1_000_000),
    # -- consistency / precision -------------------------------------------
    Achievement("metronome", "Metronome", "Reach 90% consistency over 10+ races",
                lambda s: s.get("consistency", 0) >= 90 and s["races_played"] >= 10),
    Achievement("perfectionist", "Perfectionist", "Finish 10 flawless races",
                lambda s: s.get("flawless_races", 0) >= 10),
    # -- speed -------------------------------------------------------------
    Achievement("blistering", "Blistering", "Reach 150 WPM",
                lambda s: s["best_wpm"] >= 150),
    # -- progression -------------------------------------------------------
    Achievement("level_10", "Seasoned", "Reach level 10",
                lambda s: s.get("level", 1) >= 10),
    Achievement("level_25", "Devoted", "Reach level 25",
                lambda s: s.get("level", 1) >= 25),
    Achievement("ranked_up", "Climbing", "Reach Gold tier or higher",
                lambda s: s.get("tier_index", 0) >= 2),   # Bronze0 Silver1 Gold2
    Achievement("elite", "Elite", "Reach Diamond tier or higher",
                lambda s: s.get("tier_index", 0) >= 4),   # Platinum3 Diamond4
    # -- habit -------------------------------------------------------------
    Achievement("regular", "Regular", "Play 7 days in a row",
                lambda s: s.get("longest_day_streak", 0) >= 7),
    Achievement("dedicated", "Dedicated", "Play 30 days in a row",
                lambda s: s.get("longest_day_streak", 0) >= 30),
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
