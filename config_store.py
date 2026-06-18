"""Persist the host's room configuration and ban list across restarts.

A tiny JSON sidecar (default ``typeracer_host.json``) holding the current race
config and the set of banned accounts, written atomically. The game server is
given an instance and calls :meth:`save` whenever config or bans change; a host
loads it once at startup to restore the room. Corrupt/missing files yield empty
defaults rather than raising -- a lost host file just means a fresh room.
"""

import json
import os
import tempfile

import modes

SCHEMA_VERSION = 1
DEFAULT_HOST_FILE = "typeracer_host.json"

# Config keys that may be restored from disk (runtime-only keys are ignored).
_PERSISTED_KEYS = (
    "mode", "length", "category", "difficulty", "time_limit", "lives",
    "custom_text", "countdown", "quick_start", "min_players", "rematch_secs",
)


class HostConfigStore:
    def __init__(self, path=DEFAULT_HOST_FILE):
        self.path = path

    def load(self):
        """Return (config_dict, banned_set), merged over current defaults."""
        config = modes.default_config()
        banned = set()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return config, banned
        if not isinstance(data, dict):
            return config, banned
        saved = data.get("config")
        if isinstance(saved, dict):
            for k in _PERSISTED_KEYS:
                if k in saved:
                    config[k] = saved[k]
        raw_bans = data.get("banned")
        if isinstance(raw_bans, list):
            banned = {str(b).lower() for b in raw_bans if isinstance(b, str)}
        return config, banned

    def save(self, config, banned):
        """Atomically write the persisted config keys + ban set."""
        payload = {
            "schema": SCHEMA_VERSION,
            "config": {k: config.get(k) for k in _PERSISTED_KEYS},
            "banned": sorted(banned),
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".typeracer_host_",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
