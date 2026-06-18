"""Wire protocol for LAN TypeRacer.

All messages are JSON objects with a ``type`` field. This module is shared by
both the server and the client so the two can never disagree on the contract.
"""

import json

PROTOCOL_VERSION = 4

DEFAULT_WS_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766

# WebSocket close codes for application-level disconnects.
CLOSE_REPLACED = 4000   # this account logged in from elsewhere
CLOSE_KICKED = 4001     # removed by the host

# ---------------------------------------------------------------------------
# v4 additive contract
# ---------------------------------------------------------------------------
# v4 is a pure superset of v3: every new wire field is OPTIONAL and defaults to
# off/None, so a classic race emits the same observable state as v3. New surface:
#
#   auth (additive field on register/login/guest):
#       room_password        - required only when the host set one
#   auth_ok (additive fields):
#       level, xp, xp_into, xp_next, tier, color
#   config (additive race-setup fields, all default to the v3 behaviour):
#       countdown            - seconds (0/3/5/10); was the fixed COUNTDOWN_SECONDS
#       quick_start          - skip the lobby dwell for a lone human
#       min_players          - ready players needed before auto-start
#       rematch_secs         - auto-rematch delay after results (0 = off)
#       bots                 - [{id,name,difficulty}] virtual racers in the room
#   snapshot (additive, guarded to the phases that need them):
#       session              - running points scoreboard across races
#       celebration          - one-shot winner banner (RESULTS only)
#       splits               - per-player WPM timeline (RESULTS only)
#   per-player view (additive fields):
#       is_bot, level, tier, color, session_points, recent_emote
#   new client->server messages: C_ADD_BOT C_REMOVE_BOT C_RERACE C_SESSION_RESET
#       C_EMOTE C_SETCOLOR C_UNBAN C_BANLIST
#   new server->client message:  S_BANLIST

# ---------------------------------------------------------------------------
# Game phases (server authoritative, echoed in every state snapshot)
# ---------------------------------------------------------------------------
PHASE_LOBBY = "lobby"
PHASE_COUNTDOWN = "countdown"
PHASE_RACING = "racing"
PHASE_RESULTS = "results"

# ---------------------------------------------------------------------------
# Client -> Server message types
# ---------------------------------------------------------------------------
# Authentication (first message on a connection must be one of these three):
C_REGISTER = "register"     # {type, username, password, token?, version}
C_LOGIN = "login"           # {type, username, password, token?, version}
C_GUEST = "guest"           # {type, name, token?, version}
# In-game:
C_READY = "ready"           # {type, ready: bool}
C_PROGRESS = "progress"     # {type, pos, errors, keystrokes}
C_START = "start"           # admin only: force the countdown to begin now
C_LOBBY = "lobby"           # admin only: abort current race back to the lobby
C_CONFIG = "config"         # admin only: set next-race config {mode?,length?,...}
C_CHAT = "chat"             # {type, text}: lobby/results chat message
C_PROFILE = "profile"       # {type, target_id?}: request a player's profile
C_HISTORY = "history"       # {type}: request your own match history
C_KICK = "kick"             # admin only: {type, target_id}
C_LEADERBOARD = "leaderboard"  # {type, metric?, mode?, category?}
C_PING = "ping"             # {type}
# v4 additions:
C_ADD_BOT = "add_bot"       # admin only: {type, difficulty}
C_REMOVE_BOT = "remove_bot" # admin only: {type, target_id?}  (omit = remove last)
C_RERACE = "rerace"         # admin only: instant re-rack from results
C_SESSION_RESET = "session_reset"  # admin only: clear the session scoreboard
C_EMOTE = "emote"           # {type, code}: fire a canned quick-chat emote
C_SETCOLOR = "setcolor"     # {type, color}: pick a persistent accent color
C_UNBAN = "unban"           # admin only: {type, username}
C_BANLIST = "banlist"       # admin only: {type}: request the current ban list

# ---------------------------------------------------------------------------
# Server -> Client message types
# ---------------------------------------------------------------------------
S_AUTH_OK = "auth_ok"       # {type, id, name, account, is_admin, is_guest, stats, level, tier, color, version}
S_AUTH_FAIL = "auth_fail"   # {type, msg}
S_STATE = "state"           # full snapshot, see GameServer.snapshot()
S_LEADERBOARD = "leaderboard"  # {type, metric, mode, category, rows: [...]}
S_PROFILE = "profile"       # {type, found, name, is_guest, stats, badges, recent}
S_HISTORY = "history"       # {type, rows: [...]}
S_ERROR = "error"           # {type, msg}
S_PONG = "pong"             # {type}
S_BANLIST = "banlist"       # {type, rows: [username, ...]}

# ---------------------------------------------------------------------------
# Quick-chat emotes (canned, sanitized phrases fired with a single key).
# Codes are stable wire tokens; the text is what other players see.
# ---------------------------------------------------------------------------
EMOTES = {
    "gg": "gg!",
    "nice": "nice!",
    "close": "so close!",
    "gl": "good luck!",
    "wow": "wow!",
    "rematch": "rematch?",
    "oops": "oops",
    "go": "let's go!",
}
EMOTE_ORDER = ("gg", "nice", "close", "gl", "wow", "rematch", "oops", "go")

# ---------------------------------------------------------------------------
# Persistent player accent colors. Names are the wire tokens; the client maps
# them to ANSI. Kept here so server and client agree on the valid set.
# ---------------------------------------------------------------------------
PLAYER_COLORS = ("cyan", "green", "yellow", "magenta", "red", "blue", "white")

# Discovery (UDP datagrams, not websocket frames)
DISCOVERY_PROBE = b"TYPERACER_DISCOVER?v1"
DISCOVERY_REPLY_PREFIX = b"TYPERACER_HOST!v1 "


def encode(obj):
    """Serialize a message dict to a compact JSON string."""
    return json.dumps(obj, separators=(",", ":"))


def decode(data):
    """Parse an inbound websocket payload (str or bytes) into a dict."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    return json.loads(data)
