"""Persistent accounts + player stats for the TypeRacer host.

A small JSON-backed store with atomic writes. Every access happens from the
host's single asyncio thread, so no locking is needed. Passwords are salted and
hashed with PBKDF2-HMAC-SHA256 (plaintext is never stored).

Schema is versioned. Loading an older file migrates each account by merging its
stats over a fresh defaults dict, so new fields appear and old fields survive.
"""

import hashlib
import hmac
import json
import os
import re
import tempfile
import time

import achievements
import milestones
import progression
import protocol as P

PBKDF2_ROUNDS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MIN_PASSWORD = 4
MAX_PASSWORD = 128
MAX_HISTORY = 20
MAX_RIVALS = 40
SCHEMA_VERSION = 3
DEFAULT_DATA_FILE = "typeracer_data.json"

LEADERBOARD_METRICS = (
    "best_wpm", "avg_wpm", "races_won", "races_played",
    "longest_streak", "consistency", "skill_rating", "level",
)
METRIC_LABELS = {
    "best_wpm": "best WPM",
    "avg_wpm": "average WPM",
    "races_won": "wins",
    "races_played": "races",
    "longest_streak": "longest streak",
    "consistency": "consistency",
    "skill_rating": "skill rating",
    "level": "level",
}


def _new_stats(now):
    return {
        "races_played": 0,
        "races_won": 0,
        "best_wpm": 0.0,        # net WPM (kept name for back-compat)
        "avg_wpm": 0.0,
        "raw_wpm_best": 0.0,
        "raw_wpm_avg": 0.0,
        "best_accuracy": 0.0,
        "avg_accuracy": 0.0,
        "total_time": 0.0,
        "total_chars": 0,
        "total_keystrokes": 0,
        "total_errors": 0,
        "wpm_sumsq": 0.0,
        "consistency": 0.0,
        "cur_streak": 0,
        "longest_streak": 0,
        "flawless_races": 0,
        "podiums": 0,
        "last_wpm": 0.0,
        # progression (v4)
        "total_xp": 0.0,
        "level": 1,
        "skill_rating": 0.0,
        "tier": "Bronze",
        "tier_index": 0,
        # daily play streak (v4)
        "day_streak": 0,
        "longest_day_streak": 0,
        "last_play_day": None,
        # cosmetics + social (v4)
        "color": None,
        "rivals": {},
        "by_mode": {},
        "by_category": {},
        "history": [],
        "achievements": {},
        "created": now,
        "last_played": None,
    }


class AccountStore:
    def __init__(self, path=DEFAULT_DATA_FILE):
        self.path = path
        self.users = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            self.users = {}
            return
        users = data.get("users") if isinstance(data, dict) else None
        self.users = users if isinstance(users, dict) else {}
        for key, rec in list(self.users.items()):
            if isinstance(rec, dict):
                self._migrate(rec)
            else:
                self.users.pop(key, None)

    def _migrate(self, rec):
        existing = rec.get("stats")
        base = _new_stats(time.time())
        if isinstance(existing, dict):
            base.update(existing)
            base["created"] = existing.get("created", base["created"])
            # Backfill the variance baseline for accounts from before consistency
            # tracking, so consistency starts sane (treat prior races as on-avg)
            # instead of producing a corrupt value from a zero sum-of-squares.
            if base["races_played"] > 0 and not existing.get("wpm_sumsq"):
                base["wpm_sumsq"] = base["races_played"] * base["avg_wpm"] ** 2
            # Backfill v4 progression for accounts created before it existed, so
            # level/tier/XP start from a plausible place instead of zero.
            if base["races_played"] > 0 and not existing.get("total_xp"):
                base["total_xp"] = float(base.get("total_chars", 0))
            if base["races_played"] > 0 and not existing.get("skill_rating"):
                base["skill_rating"] = progression.update_rating(
                    None, base.get("avg_wpm", 0.0), base.get("avg_accuracy", 100.0))
        for k in ("by_mode", "by_category", "achievements", "rivals"):
            if not isinstance(base.get(k), dict):
                base[k] = {}
        if not isinstance(base.get("history"), list):
            base["history"] = []
        # Keep derived progression fields coherent with whatever XP/rating we hold.
        self._refresh_progression(base)
        rec["stats"] = base
        return rec

    @staticmethod
    def _refresh_progression(s):
        """Recompute the derived level/tier fields from total_xp + skill_rating."""
        s["level"] = progression.level_for(s.get("total_xp", 0.0))
        s["tier"] = progression.tier_for(s.get("skill_rating", 0.0))
        s["tier_index"] = progression.tier_index(s.get("skill_rating", 0.0))

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".typeracer_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"schema": SCHEMA_VERSION, "users": self.users},
                          f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- validation ---------------------------------------------------------
    @staticmethod
    def validate_username(username):
        if not USERNAME_RE.match(username or ""):
            return "username must be 3-16 chars (letters, digits, underscore)"
        return None

    @staticmethod
    def validate_password(password):
        if not password or len(password) < MIN_PASSWORD:
            return f"password must be at least {MIN_PASSWORD} characters"
        if len(password) > MAX_PASSWORD:
            return "password is too long"
        return None

    def exists(self, username):
        return (username or "").lower() in self.users

    # -- auth ---------------------------------------------------------------
    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt, PBKDF2_ROUNDS
        ).hex()

    def create(self, username, password):
        err = self.validate_username(username) or self.validate_password(password)
        if err:
            return None, err
        key = username.lower()
        if key in self.users:
            return None, "username already taken"
        salt = os.urandom(16)
        record = {
            "username": username,
            "salt": salt.hex(),
            "hash": self._hash(password, salt),
            "stats": _new_stats(time.time()),
        }
        self.users[key] = record
        self._save()
        return record, None

    def authenticate(self, username, password):
        record = self.users.get((username or "").lower())
        if not record:
            return None, "no such account"
        try:
            salt = bytes.fromhex(record["salt"])
        except (ValueError, KeyError):
            return None, "corrupt account record"
        if not hmac.compare_digest(self._hash(password, salt), record.get("hash", "")):
            return None, "wrong password"
        return record, None

    # -- stats --------------------------------------------------------------
    def stats_for(self, username):
        record = self.users.get((username or "").lower())
        return record["stats"] if record else None

    def record_race(self, username, *, net_wpm, raw_wpm, accuracy, seconds,
                    chars, keystrokes, errors, won, place, racers,
                    mode="classic", category=None, flawless=False):
        """Update an account after a race.

        Returns a dict describing what just happened, for the results banner::

            {"achievements": [id, ...],   # newly-unlocked badge ids
             "levels": [n, ...],          # any level numbers reached this race
             "pbs": [{"kind","old","new"}, ...],   # personal bests beaten
             "day_streak": int|None}      # the new day-streak, if it grew today
        """
        record = self.users.get((username or "").lower())
        if not record:
            return {"achievements": [], "levels": [], "pbs": [], "day_streak": None}
        s = record["stats"]
        n = s["races_played"]

        # Snapshot pre-race bests so we can detect (and celebrate) new records.
        prev_best_wpm = s["best_wpm"]
        prev_best_raw = s.get("raw_wpm_best", 0.0)
        prev_best_acc = s["best_accuracy"]
        prev_level = s.get("level", 1)

        s["avg_wpm"] = (s["avg_wpm"] * n + net_wpm) / (n + 1)
        s["raw_wpm_avg"] = (s["raw_wpm_avg"] * n + raw_wpm) / (n + 1)
        s["avg_accuracy"] = (s["avg_accuracy"] * n + accuracy) / (n + 1)
        s["wpm_sumsq"] = s.get("wpm_sumsq", 0.0) + net_wpm * net_wpm
        s["races_played"] = n + 1
        s["races_won"] += 1 if won else 0
        s["best_wpm"] = max(s["best_wpm"], net_wpm)
        s["raw_wpm_best"] = max(s.get("raw_wpm_best", 0.0), raw_wpm)
        s["best_accuracy"] = max(s["best_accuracy"], accuracy)
        s["total_time"] += max(0.0, seconds)
        s["total_chars"] += max(0, int(chars))
        s["total_keystrokes"] += max(0, int(keystrokes))
        s["total_errors"] += max(0, int(errors))
        s["last_wpm"] = net_wpm
        if flawless:
            s["flawless_races"] += 1
        if place is not None and place <= 3:
            s["podiums"] += 1

        if won:
            s["cur_streak"] += 1
            s["longest_streak"] = max(s["longest_streak"], s["cur_streak"])
        else:
            s["cur_streak"] = 0

        cnt = s["races_played"]
        mean = s["avg_wpm"]
        var = max(0.0, s["wpm_sumsq"] / cnt - mean * mean)
        std = var ** 0.5
        s["consistency"] = round(100.0 * (1.0 - std / mean), 1) if mean > 0 else 0.0
        s["consistency"] = max(0.0, min(100.0, s["consistency"]))

        # XP / level + skill rating / tier (v4 progression ladders).
        s["total_xp"] = s.get("total_xp", 0.0) + progression.xp_for_race(
            chars=chars, accuracy=accuracy, won=won, place=place, flawless=flawless)
        s["skill_rating"] = progression.update_rating(
            None if n == 0 else s.get("skill_rating", 0.0), net_wpm, accuracy)
        self._refresh_progression(s)
        levels = list(range(prev_level + 1, s["level"] + 1))

        day_streak = self._update_day_streak(s)

        m = s["by_mode"].setdefault(mode, {"races": 0, "wins": 0, "best_wpm": 0.0})
        m["races"] += 1
        m["wins"] += 1 if won else 0
        m["best_wpm"] = max(m["best_wpm"], net_wpm)

        if category:
            c = s["by_category"].setdefault(category, {"races": 0, "best_wpm": 0.0})
            c["races"] += 1
            c["best_wpm"] = max(c["best_wpm"], net_wpm)

        s["history"].insert(0, {
            "ts": time.time(), "mode": mode, "category": category,
            "wpm": round(net_wpm, 1), "raw": round(raw_wpm, 1),
            "acc": round(accuracy, 1), "place": place, "racers": racers,
            "won": bool(won),
        })
        del s["history"][MAX_HISTORY:]
        s["last_played"] = time.time()

        pbs = []
        if n > 0 and chars > 0:   # never call a first race a "personal best"
            if net_wpm > prev_best_wpm:
                pbs.append({"kind": "WPM", "old": round(prev_best_wpm, 1),
                            "new": round(net_wpm, 1)})
            if raw_wpm > prev_best_raw:
                pbs.append({"kind": "raw WPM", "old": round(prev_best_raw, 1),
                            "new": round(raw_wpm, 1)})
            if accuracy > prev_best_acc:
                pbs.append({"kind": "accuracy", "old": round(prev_best_acc, 1),
                            "new": round(accuracy, 1)})

        newly = self._update_achievements(record)
        self._save()
        return {"achievements": newly, "levels": levels, "pbs": pbs,
                "day_streak": day_streak}

    @staticmethod
    def _update_day_streak(s):
        """Advance the consecutive-days-played counter. Returns the new streak
        value only on the first race of a new day (so the caller can announce
        it once), else None."""
        today = int(time.time() // 86400)
        last = s.get("last_play_day")
        if last == today:
            return None                          # already counted today
        if last is not None and today - last == 1:
            s["day_streak"] = s.get("day_streak", 0) + 1
        else:
            s["day_streak"] = 1                  # first ever, or a gap
        s["last_play_day"] = today
        s["longest_day_streak"] = max(s.get("longest_day_streak", 0), s["day_streak"])
        return s["day_streak"]

    def record_h2h(self, results):
        """Record pairwise win/loss for a finished race.

        ``results`` is a list of ``(account, place)`` for accounted racers. A
        lower place beats a higher one; equal places (e.g. a timed tie) count
        for neither. Symmetric, capped, and persisted once.
        """
        entries = [(a, p) for (a, p) in results if a and p is not None]
        if len(entries) < 2:
            return
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a_acc, a_place = entries[i]
                b_acc, b_place = entries[j]
                if a_place == b_place:
                    continue
                winner, loser = ((a_acc, b_acc) if a_place < b_place
                                 else (b_acc, a_acc))
                self._bump_rival(winner, loser, won=True)
                self._bump_rival(loser, winner, won=False)
        self._save()

    def _bump_rival(self, owner, opponent, *, won):
        rec = self.users.get((owner or "").lower())
        opp = self.users.get((opponent or "").lower())
        if not rec or not opp:
            return
        rivals = rec["stats"].setdefault("rivals", {})
        key = opponent.lower()
        entry = rivals.setdefault(key, {"name": opp["username"], "w": 0, "l": 0})
        entry["name"] = opp["username"]
        entry["w" if won else "l"] += 1
        # Prune to the most-played rivals so the map can't grow without bound.
        if len(rivals) > MAX_RIVALS:
            ranked = sorted(rivals.items(),
                            key=lambda kv: kv[1]["w"] + kv[1]["l"], reverse=True)
            rec["stats"]["rivals"] = dict(ranked[:MAX_RIVALS])

    def set_color(self, username, color):
        """Set an account's accent color (validated against PLAYER_COLORS)."""
        record = self.users.get((username or "").lower())
        if not record:
            return False
        if color is not None and color not in P.PLAYER_COLORS:
            return False
        record["stats"]["color"] = color
        self._save()
        return True

    def _update_achievements(self, record):
        s = record["stats"]
        earned = achievements.qualifying(s)
        have = s["achievements"]
        newly = []
        for a in achievements.ACHIEVEMENTS:   # stable display order
            if a.id in earned and a.id not in have:
                have[a.id] = time.time()
                newly.append(a.id)
        return newly

    # -- read models --------------------------------------------------------
    def leaderboard(self, metric="best_wpm", limit=15, mode=None, category=None):
        if metric not in LEADERBOARD_METRICS:
            metric = "best_wpm"
        rows = []
        for record in self.users.values():
            s = record["stats"]
            if mode:
                scope = s["by_mode"].get(mode)
                races = scope.get("races", 0) if isinstance(scope, dict) else 0
                if races <= 0:
                    continue
                best, wins = scope.get("best_wpm", 0.0), scope.get("wins", 0)
            elif category:
                scope = s["by_category"].get(category)
                races = scope.get("races", 0) if isinstance(scope, dict) else 0
                if races <= 0:
                    continue
                best, wins = scope.get("best_wpm", 0.0), 0
            else:
                if s["races_played"] <= 0:
                    continue
                best, races, wins = s["best_wpm"], s["races_played"], s["races_won"]
            rows.append({
                "username": record["username"],
                "best_wpm": round(best, 1),
                "avg_wpm": round(s["avg_wpm"], 1),
                "races_played": races,
                "races_won": wins,
                "avg_accuracy": round(s["avg_accuracy"], 1),
                "longest_streak": s["longest_streak"],
                "consistency": s["consistency"],
                "skill_rating": round(s.get("skill_rating", 0.0), 1),
                "level": s.get("level", 1),
                "tier": s.get("tier", "Bronze"),
                "color": s.get("color"),
            })
        rows.sort(key=lambda r: r.get(metric, 0), reverse=True)
        return rows[:limit]

    def public_stats(self, username):
        """Compact stats for the lobby snapshot."""
        s = self.stats_for(username)
        if not s:
            return None
        return {
            "races": s["races_played"],
            "wins": s["races_won"],
            "best_wpm": round(s["best_wpm"], 1),
            "avg_wpm": round(s["avg_wpm"], 1),
            "avg_acc": round(s["avg_accuracy"], 1),
            "streak": s["cur_streak"],
            "badges": len(s["achievements"]),
            "level": s.get("level", 1),
            "tier": s.get("tier", "Bronze"),
            "color": s.get("color"),
            "day_streak": s.get("day_streak", 0),
        }

    def profile_payload(self, username):
        """Full (whitelisted) profile for the S_PROFILE response."""
        record = self.users.get((username or "").lower())
        if not record:
            return None
        s = record["stats"]
        whitelist = (
            "races_played", "races_won", "best_wpm", "avg_wpm", "raw_wpm_best",
            "best_accuracy", "avg_accuracy", "consistency", "cur_streak",
            "longest_streak", "flawless_races", "podiums", "total_time",
            "total_chars", "total_keystrokes", "total_xp", "level",
            "skill_rating", "tier", "tier_index", "day_streak",
            "longest_day_streak", "color", "by_mode", "by_category",
            "created", "last_played",
        )
        badges = []
        for aid, ts in sorted(s["achievements"].items(), key=lambda kv: kv[1]):
            badge = achievements.info(aid)
            badge["ts"] = ts
            badges.append(badge)
        level, into, need = progression.level_progress(s.get("total_xp", 0.0))
        # Top rivals by total games played against.
        rivals = sorted(
            (v for v in s.get("rivals", {}).values() if isinstance(v, dict)),
            key=lambda v: v.get("w", 0) + v.get("l", 0), reverse=True)[:6]
        return {
            "username": record["username"],
            "stats": {k: s[k] for k in whitelist if k in s},
            "level_progress": {"level": level, "into": into, "need": need},
            "badges": badges,
            "milestones": milestones.progress(s),
            "rivals": rivals,
            "recent": s["history"][:10],
        }

    def history_rows(self, username):
        s = self.stats_for(username)
        return list(s["history"]) if s else []
