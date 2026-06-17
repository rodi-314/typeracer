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


def default_config():
    return {
        "mode": MODE_CLASSIC,
        "length": "medium",
        "category": "any",
        "difficulty": None,        # None = any (1=easy, 2=medium, 3=hard)
        "time_limit": DEFAULT_TIME_LIMIT,
        "lives": DEFAULT_LIVES,
        "custom_text": None,
    }
