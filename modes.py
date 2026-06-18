"""Race-mode constants and the default race configuration.

Pure data with no imports, so both the server and the text layer can depend on
it without cycles. Every new mode must degrade to CLASSIC behaviour when not
explicitly selected.
"""

MODE_CLASSIC = "classic"      # type a fixed passage; first to finish wins
MODE_TIMED = "timed"          # type as much as possible before the clock; ranked by chars
MODE_SURVIVAL = "survival"    # each typo costs a life; survive and finish

MODES = (MODE_CLASSIC, MODE_TIMED, MODE_SURVIVAL)

MODE_LABELS = {
    MODE_CLASSIC: "Classic",
    MODE_TIMED: "Timed",
    MODE_SURVIVAL: "Survival",
}

# Passage length buckets (character-count ranges).
LENGTHS = {
    "short": (60, 140),
    "medium": (140, 260),
    "long": (260, 560),
}
LENGTH_NAMES = ("short", "medium", "long")

TIME_LIMITS = (15, 30, 60, 120)
DEFAULT_TIME_LIMIT = 30

LIVES_OPTIONS = (1, 2, 3, 5)
DEFAULT_LIVES = 3

# Host-settable countdown length (seconds). 0 == instant for rapid solo reps.
COUNTDOWN_OPTIONS = (0, 3, 5, 10)
DEFAULT_COUNTDOWN = 3

# Auto-rematch delay options after a results screen (0 == off).
REMATCH_OPTIONS = (0, 5, 10, 20)

# Session scoreboard: points awarded by finishing place (F1-style). Anyone who
# took part but placed outside this table still earns the participation point.
POINTS_BY_PLACE = {1: 10, 2: 6, 3: 4, 4: 3, 5: 2}
POINTS_PARTICIPATION = 1


def points_for_place(place):
    if place is None:
        return 0
    return POINTS_BY_PLACE.get(place, POINTS_PARTICIPATION)

# ---------------------------------------------------------------------------
# AI bot opponents
# ---------------------------------------------------------------------------
# Each difficulty is a target net WPM + per-keystroke accuracy. The "rival" tier
# has wpm=None: the server calibrates it to the strongest human in the race.
BOT_DIFFICULTIES = {
    "easy":   {"wpm": 30,   "acc": 0.92,  "label": "Easy"},
    "medium": {"wpm": 55,   "acc": 0.95,  "label": "Medium"},
    "hard":   {"wpm": 85,   "acc": 0.97,  "label": "Hard"},
    "insane": {"wpm": 120,  "acc": 0.985, "label": "Insane"},
    "rival":  {"wpm": None, "acc": 0.96,  "label": "Rival"},
}
BOT_DIFFICULTY_ORDER = ("easy", "medium", "hard", "insane", "rival")
DEFAULT_BOT_DIFFICULTY = "medium"
MAX_BOTS = 8
BOT_NAMES = ("Ada", "Grace", "Linus", "Dennis", "Turing", "Hopper", "Lovelace",
             "Knuth", "Babbage", "Tesla", "Curie", "Newton", "Euler", "Gauss")


def default_config():
    return {
        "mode": MODE_CLASSIC,
        "length": "medium",
        "category": "any",
        "difficulty": None,        # None = any (1=easy, 2=medium, 3=hard)
        "time_limit": DEFAULT_TIME_LIMIT,
        "lives": DEFAULT_LIVES,
        "custom_text": None,
        # v4 additive flow knobs (each defaults to the v3 behaviour)
        "countdown": DEFAULT_COUNTDOWN,
        "quick_start": False,
        "min_players": 1,
        "rematch_secs": 0,
    }
