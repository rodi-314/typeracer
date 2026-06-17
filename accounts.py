"""Persistent accounts + player stats for the TypeRacer host.

A small JSON-backed store with atomic writes. Every access happens from the
host's single asyncio thread, so no locking is needed. Passwords are salted and
hashed with PBKDF2-HMAC-SHA256 (plaintext is never stored).

Note: this lives only on the host. Clients keep nothing on disk.
"""

import hashlib
import hmac
import json
import os
import re
import tempfile
import time

PBKDF2_ROUNDS = 120_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MIN_PASSWORD = 4
MAX_PASSWORD = 128
LEADERBOARD_METRICS = ("best_wpm", "avg_wpm", "races_won", "races_played")
DEFAULT_DATA_FILE = "typeracer_data.json"


def _new_stats(now):
    return {
        "races_played": 0,
        "races_won": 0,
        "best_wpm": 0.0,
        "avg_wpm": 0.0,
        "best_accuracy": 0.0,
        "avg_accuracy": 0.0,
        "total_time": 0.0,
        "total_chars": 0,
        "created": now,
        "last_played": None,
    }


class AccountStore:
    def __init__(self, path=DEFAULT_DATA_FILE):
        self.path = path
        self.users = {}   # username_lower -> {username, salt, hash, stats}
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

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".typeracer_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": self.users}, f, indent=2)
            os.replace(tmp, self.path)  # atomic on POSIX and Windows
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
        """Return (record, None) on success or (None, error_message)."""
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
        """Return (record, None) on success or (None, error_message)."""
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

    def record_race(self, username, wpm, accuracy, seconds, chars, won):
        record = self.users.get((username or "").lower())
        if not record:
            return
        s = record["stats"]
        n = s["races_played"]
        s["avg_wpm"] = (s["avg_wpm"] * n + wpm) / (n + 1)
        s["avg_accuracy"] = (s["avg_accuracy"] * n + accuracy) / (n + 1)
        s["races_played"] = n + 1
        s["races_won"] += 1 if won else 0
        s["best_wpm"] = max(s["best_wpm"], wpm)
        s["best_accuracy"] = max(s["best_accuracy"], accuracy)
        s["total_time"] += max(0.0, seconds)
        s["total_chars"] += max(0, int(chars))
        s["last_played"] = time.time()
        self._save()

    def leaderboard(self, metric="best_wpm", limit=15):
        if metric not in LEADERBOARD_METRICS:
            metric = "best_wpm"
        rows = []
        for record in self.users.values():
            s = record["stats"]
            if s["races_played"] <= 0:
                continue
            rows.append({
                "username": record["username"],
                "best_wpm": round(s["best_wpm"], 1),
                "avg_wpm": round(s["avg_wpm"], 1),
                "races_played": s["races_played"],
                "races_won": s["races_won"],
                "avg_accuracy": round(s["avg_accuracy"], 1),
            })
        rows.sort(key=lambda r: r.get(metric, 0), reverse=True)
        return rows[:limit]

    def public_stats(self, username):
        """A compact snapshot of one account's stats for the lobby UI."""
        s = self.stats_for(username)
        if not s:
            return None
        return {
            "races": s["races_played"],
            "wins": s["races_won"],
            "best_wpm": round(s["best_wpm"], 1),
            "avg_wpm": round(s["avg_wpm"], 1),
            "avg_acc": round(s["avg_accuracy"], 1),
        }
