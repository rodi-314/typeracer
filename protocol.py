"""Wire protocol for LAN TypeRacer.

All messages are JSON objects with a ``type`` field. This module is shared by
both the server and the client so the two can never disagree on the contract.
"""

import json

PROTOCOL_VERSION = 3

DEFAULT_WS_PORT = 8765
DEFAULT_DISCOVERY_PORT = 8766

# WebSocket close codes for application-level disconnects.
CLOSE_REPLACED = 4000   # this account logged in from elsewhere
CLOSE_KICKED = 4001     # removed by the host

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

# ---------------------------------------------------------------------------
# Server -> Client message types
# ---------------------------------------------------------------------------
S_AUTH_OK = "auth_ok"       # {type, id, name, account, is_admin, is_guest, stats, version}
S_AUTH_FAIL = "auth_fail"   # {type, msg}
S_STATE = "state"           # full snapshot, see GameServer.snapshot()
S_LEADERBOARD = "leaderboard"  # {type, metric, mode, category, rows: [...]}
S_PROFILE = "profile"       # {type, found, name, is_guest, stats, badges, recent}
S_HISTORY = "history"       # {type, rows: [...]}
S_ERROR = "error"           # {type, msg}
S_PONG = "pong"             # {type}

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
