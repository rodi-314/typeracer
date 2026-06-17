"""Typing passages, categorized by content and difficulty.

All passages are ASCII-only with no tabs or newlines, so every character maps to
a single keystroke on every platform. Each record is ``{id, text, category,
difficulty}`` where difficulty is 1 (easy) .. 3 (hard). ``select()`` filters by
category/difficulty/length with a deterministic widening fallback so a sparse
filter never returns nothing mid-countdown.
"""

import random

from modes import LENGTHS

CAT_QUOTES = "quotes"
CAT_PROVERBS = "proverbs"
CAT_CODE = "code"
CAT_PANGRAMS = "pangrams"
CAT_NUMBERS = "numbers"
CATEGORIES = (CAT_QUOTES, CAT_PROVERBS, CAT_CODE, CAT_PANGRAMS, CAT_NUMBERS)

MIN_CUSTOM_LEN = 30
MAX_CUSTOM_LEN = 600

# (text, category, difficulty)
_RAW = [
    # -- quotes / prose ----------------------------------------------------
    ("Programming is not about typing fast, it is about thinking clearly, but a "
     "little speed at the keyboard never hurt anyone racing against a friend.",
     CAT_QUOTES, 2),
    ("Code is read far more often than it is written, so write it for the next "
     "person who has to understand it, because that person might just be you.",
     CAT_QUOTES, 2),
    ("In the middle of difficulty lies opportunity, and the people who change "
     "the world are usually the ones who are crazy enough to think they can.",
     CAT_QUOTES, 2),
    ("The best way to predict the future is to invent it, so stop waiting for "
     "permission and start building the small thing that only you can imagine.",
     CAT_QUOTES, 2),
    ("Stay hungry, stay foolish, keep your eyes open for the next big idea, and "
     "never let the fear of looking silly stop you from asking a question.",
     CAT_QUOTES, 2),
    ("Talk is cheap, show me the code.", CAT_QUOTES, 1),
    ("Simplicity is the soul of efficiency, and a clear mind writes clear code.",
     CAT_QUOTES, 1),
    ("Good design is as little design as possible, because less is more when "
     "every extra feature is one more thing that can break on a quiet Friday "
     "night, so keep the surface small and the core honest and easy to test.",
     CAT_QUOTES, 3),
    # -- proverbs ----------------------------------------------------------
    ("A journey of a thousand miles begins with a single step.", CAT_PROVERBS, 1),
    ("The early bird catches the worm, but the second mouse gets the cheese.",
     CAT_PROVERBS, 1),
    ("Practice does not make perfect; only perfect practice makes perfect, so "
     "slow down, focus on every single keystroke, and the speed will follow.",
     CAT_PROVERBS, 2),
    ("Measure twice and cut once, because a moment of careful planning will save "
     "you an entire afternoon of frustrating rework that nobody ever enjoys.",
     CAT_PROVERBS, 2),
    ("A smooth sea never made a skilled sailor.", CAT_PROVERBS, 1),
    ("When in doubt, leave it out; a clean and simple solution that works today "
     "beats a clever and complicated one that might possibly work tomorrow.",
     CAT_PROVERBS, 2),
    # -- pangrams ----------------------------------------------------------
    ("The quick brown fox jumps over the lazy dog.", CAT_PANGRAMS, 1),
    ("Pack my box with five dozen liquor jugs.", CAT_PANGRAMS, 1),
    ("How vexingly quick daft zebras jump!", CAT_PANGRAMS, 1),
    ("The five boxing wizards jump quickly while a jovial fox grabs the prize.",
     CAT_PANGRAMS, 2),
    ("Sphinx of black quartz, judge my vow; the jay, pig, fox, zebra and wolves "
     "quack and frolic in the bright meadow at dawn.", CAT_PANGRAMS, 2),
    # -- code (single-line, ASCII) ----------------------------------------
    ("for i in range(len(items)): total += items[i] * weights[i]",
     CAT_CODE, 2),
    ("def add(a, b): return a + b  # the simplest possible function",
     CAT_CODE, 1),
    ("if x > 0 and y < 10 or not done: queue.append((x, y)); count += 1",
     CAT_CODE, 3),
    ("const sum = arr.reduce((acc, n) => acc + n, 0); console.log(sum);",
     CAT_CODE, 3),
    ("result = {k: v for k, v in pairs if v is not None and k not in seen}",
     CAT_CODE, 3),
    ("git commit -m \"fix: handle empty input\" && git push origin main",
     CAT_CODE, 2),
    # -- numbers / punctuation --------------------------------------------
    ("The totals were 3, 14, 159, 26535 and 89793 across 4 quiet days.",
     CAT_NUMBERS, 2),
    ("Call 555-0142 before 9:30, then meet at 1600 Elm St, Apt 27B, by noon.",
     CAT_NUMBERS, 3),
    ("Pi is about 3.14159 and e is about 2.71828; both show up everywhere.",
     CAT_NUMBERS, 2),
    ("Order #4471: 2 x $19.99, 5 x $3.50, tax 8.25%, total due $58.21 today.",
     CAT_NUMBERS, 3),
    ("In 1969, 3 astronauts traveled 240,000 miles in about 76 hours total.",
     CAT_NUMBERS, 2),
]

PASSAGES = [
    {"id": i, "text": t, "category": c, "difficulty": d}
    for i, (t, c, d) in enumerate(_RAW)
]


def _id_of(text):
    for r in PASSAGES:
        if r["text"] == text:
            return r["id"]
    return None


def select(rng=None, category=None, difficulty=None, length=None,
           max_len=None, avoid_id=None):
    """Pick a passage record matching the filters, widening on demand.

    Tries (category+difficulty+length), then drops length, then difficulty,
    then category, so a sparse combination always yields something.
    """
    rng = rng or random
    lo = hi = None
    if length in LENGTHS:
        lo, hi = LENGTHS[length]
    if max_len is not None:
        hi = max_len if hi is None else min(hi, max_len)

    def matches(cat, diff, use_len):
        out = []
        for r in PASSAGES:
            if cat and cat != "any" and r["category"] != cat:
                continue
            if diff and r["difficulty"] != diff:
                continue
            if use_len:
                n = len(r["text"])
                if lo is not None and n < lo:
                    continue
                if hi is not None and n > hi:
                    continue
            if avoid_id is not None and r["id"] == avoid_id:
                continue
            out.append(r)
        return out

    for cat, diff, use_len in (
        (category, difficulty, True),
        (category, difficulty, False),
        (category, None, False),
        (None, None, False),
    ):
        candidates = matches(cat, diff, use_len)
        if candidates:
            return rng.choice(candidates)
    return rng.choice(PASSAGES)  # last resort: ignore avoid_id too


def pick_text(rng=None, avoid=None):
    """Back-compat helper: a random passage string, avoiding ``avoid`` text."""
    return select(rng, avoid_id=_id_of(avoid))["text"]


def make_marathon(rng=None, category=None, n=4):
    """Concatenate several passages into one long stream (for endless modes)."""
    rng = rng or random
    parts = []
    avoid = None
    for _ in range(max(1, n)):
        rec = select(rng, category=category, avoid_id=avoid)
        parts.append(rec["text"])
        avoid = rec["id"]
    return " ".join(parts)


def validate_passage(text):
    """Return an error string if ``text`` is unusable as a passage, else None."""
    if not text or len(text) < MIN_CUSTOM_LEN:
        return f"text must be at least {MIN_CUSTOM_LEN} characters"
    if len(text) > MAX_CUSTOM_LEN:
        return f"text must be at most {MAX_CUSTOM_LEN} characters"
    for ch in text:
        if not (" " <= ch <= "~"):
            return "use plain ASCII only (no tabs, newlines or unicode)"
    return None


def sanitize_custom(raw):
    """Normalize host-supplied custom text. Returns (clean_text, error)."""
    if raw is None:
        return None, "no text provided"
    clean = " ".join(str(raw).split())
    clean = "".join(ch for ch in clean if " " <= ch <= "~")
    err = validate_passage(clean)
    if err:
        return None, err
    return clean, None
