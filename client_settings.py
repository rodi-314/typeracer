"""Per-machine client preferences, persisted as JSON.

Pure client-side: the server never sees these. Remembers UI choices (color on/off,
theme, speed units, sound, the last username typed) so they survive across
launches. A missing or corrupt file silently falls back to defaults -- settings
are a convenience, never load-bearing.

Location: ``$TYPERACER_CONFIG_DIR/settings.json`` if that env var is set
(used by tests), else ``~/.typeracer/settings.json``.
"""

import json
import os
import tempfile

DEFAULTS = {
    "color": True,
    "theme": "default",       # see client THEMES
    "units": "wpm",           # wpm | cpm | both
    "sound": False,           # terminal-bell cues on countdown/finish
    "last_username": "",
}

VALID_THEMES = ("default", "high-contrast", "colorblind", "mono")
VALID_UNITS = ("wpm", "cpm", "both")


def config_dir():
    override = os.environ.get("TYPERACER_CONFIG_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".typeracer")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def load():
    """Return a settings dict, always complete (defaults merged in)."""
    merged = dict(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data and isinstance(data[k], type(DEFAULTS[k])):
                    merged[k] = data[k]
    except (FileNotFoundError, ValueError, OSError):
        pass
    # Coerce enums back to a valid choice.
    if merged["theme"] not in VALID_THEMES:
        merged["theme"] = "default"
    if merged["units"] not in VALID_UNITS:
        merged["units"] = "wpm"
    return merged


def save(settings):
    """Atomically persist ``settings`` (best-effort; never raises)."""
    try:
        directory = config_dir()
        os.makedirs(directory, exist_ok=True)
        clean = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, settings_path())
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass   # read-only home, etc. -- preferences just won't persist
