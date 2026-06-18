"""TypeRacer client: websocket connection plus a full-screen terminal UI.

The client renders at ~20 fps from the latest server snapshot, but drives its
*own* typing cursor from local keystrokes for zero-latency feedback. The server
remains authoritative for standings; the client only reports its progress.
"""

import asyncio
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

import protocol as P
import terminal as T
import client_settings
from terminal import (
    RESET, BOLD, DIM, REVERSE,
    FG_RED, FG_GREEN, FG_YELLOW, FG_BLUE, FG_CYAN, FG_GREY, FG_WHITE, FG_MAGENTA,
    FG_BRIGHT_GREEN, FG_BRIGHT_CYAN, FG_BRIGHT_RED, BG_RED,
    KEY_ENTER, KEY_BACKSPACE, KEY_ESC, KEY_CTRL_C, KEY_TAB,
)

# Accent-color name -> ANSI foreground (matches protocol.PLAYER_COLORS).
COLOR_CODES = {
    "cyan": FG_CYAN, "green": FG_GREEN, "yellow": FG_YELLOW,
    "magenta": FG_MAGENTA, "red": FG_BRIGHT_RED, "blue": FG_BLUE,
    "white": FG_WHITE,
}

# Color themes as SGR-parameter remaps applied at the render seam. "mono" routes
# through strip_color (handled in _draw); "colorblind" shifts the green/red typed
# /error pair onto a deuteranopia-safe blue/yellow axis.
THEME_MAPS = {
    "default": {},
    "high-contrast": {"90": "37", "34": "94", "36": "96", "32": "92",
                      "33": "93", "31": "91", "35": "95"},
    "colorblind": {"32": "94", "92": "94", "31": "33", "91": "93",
                   "41": "43", "42": "44"},
    "mono": {},
}
THEME_ORDER = ("default", "high-contrast", "colorblind", "mono")
UNIT_ORDER = ("wpm", "cpm", "both")


def wrap_indices(text, width):
    """Split ``text`` into contiguous (start, end) slices of <= width chars.

    Breaks on spaces where possible so every character is preserved and the
    typing cursor maps cleanly onto a display column.
    """
    if width < 1:
        width = 1
    lines = []
    i, n = 0, len(text)
    while i < n:
        end = min(i + width, n)
        if end < n:
            br = text.rfind(" ", i, end)  # space strictly before the hard break
            if br > i:
                end = br + 1
        lines.append((i, end))
        i = end
    return lines or [(0, 0)]


class GameClient:
    def __init__(self, uri, admin_token=None, host_hint="", prefill_username="",
                 color=True, room_password=None):
        self.uri = uri
        self.admin_token = admin_token
        self.host_hint = host_hint
        self.room_password = room_password

        # Persisted per-machine preferences (color/theme/units/sound/last user).
        self.settings = client_settings.load()
        self.base_color = color           # CLI/env gate: False forces mono
        self.theme = self.settings["theme"]
        self.units = self.settings["units"]
        self.sound = self.settings["sound"]
        self._last_sound_phase = None     # de-dupe terminal-bell cues

        self.ws = None
        self.loop = None
        self.queue = asyncio.Queue()
        self.rawinput = None
        self.running = False

        # identity (filled by the auth_ok message)
        self.my_id = None
        self.is_admin = False
        self.is_guest = True
        self.account = None
        self.username = ""
        self.my_stats = None
        self.authed = False

        # view: "login" until authenticated; then "game" or an overlay
        # ("leaderboard" | "setup" | "profile" | "history" | "help")
        self.view = "login"

        # login form state
        self.login_stage = "choose"     # choose | username | password | submitting
        self.login_action = None        # login | register | guest
        self.field_username = prefill_username or self.settings.get("last_username", "")
        self.field_password = ""
        self.login_error = None

        # leaderboard overlay
        self.leaderboard_rows = []
        self.leaderboard_metric = "best_wpm"
        self.leaderboard_mode = None
        self.lb_metrics = ["best_wpm", "avg_wpm", "races_won", "races_played",
                           "longest_streak", "consistency"]

        # profile / history overlays + player selection cursor
        self.profile_data = None
        self.history_data = None
        self.banlist = []               # banned usernames (admin overlay)
        self.sel_id = None              # selected player id (TAB cycles)

        # text compose mode (chat or custom race text)
        self.compose = None             # None | "chat" | "custom"
        self.compose_draft = ""
        self.kick_armed = None          # id pending kick confirmation
        self.setup_bot_difficulty = "medium"   # host bot tier to spawn next

        # live config / chat / mode info from snapshots
        self.config = {}
        self.config_options = {}
        self.mode = "classic"
        self.time_left = None
        self.text_category = None
        self.chat_lines = []
        self.announcements = []
        self.session = None             # running scoreboard across races
        self.celebration = None         # one-shot winner banner

        # latest server snapshot
        self.state = None
        self.prev_phase = None
        self.error_msg = None
        self.exit_reason = None

        # local typing state
        self.text = ""
        self.t_pos = 0
        self.t_errors = 0
        self.t_keystrokes = 0
        self.t_error_flag = False
        self.local_start = None
        self.finished_local = False
        self.local_finish_time = None
        self.last_progress_sent = 0.0

    # ===================================================================
    # Lifecycle
    # ===================================================================
    async def run(self):
        self.loop = asyncio.get_running_loop()
        if not T.is_interactive():
            print("TypeRacer needs an interactive terminal (a real TTY) to play.")
            print("Run it directly in a Linux terminal or PowerShell/cmd window.")
            self.exit_reason = "no tty"
            return
        T.enable_ansi()
        try:
            async with connect(
                self.uri, open_timeout=10, ping_interval=20, ping_timeout=20
            ) as ws:
                self.ws = ws
                # No auto-join: the login screen (run inside _game_loop) collects
                # credentials and sends the register/login/guest message.
                await self._game_loop()
        except asyncio.TimeoutError:
            print(f"Could not connect to {self.uri}: timed out.")
            return
        except (OSError, WebSocketException) as exc:
            print(f"Could not connect to {self.uri}: {exc}")
            return
        except KeyboardInterrupt:
            # On Windows, Ctrl-C surfaces here as KeyboardInterrupt rather than a
            # keystroke; treat it as a normal quit (terminal already restored by
            # _game_loop's finally).
            self.exit_reason = "Thanks for racing!"
        self._print_exit()

    async def _game_loop(self):
        self.running = True
        try:
            T.enter_fullscreen()
            self.rawinput = T.RawInput(self.loop, self.queue)
            self.rawinput.start()
            tasks = [
                self.loop.create_task(self._receiver()),
                self.loop.create_task(self._input_consumer()),
                self.loop.create_task(self._renderer()),
            ]
            _, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            if self.rawinput is not None:
                self.rawinput.stop()
            T.leave_fullscreen()

    def _print_exit(self):
        if self.error_msg:
            print(f"Server refused the connection: {self.error_msg}")
        elif self.exit_reason:
            print(self.exit_reason)
        else:
            print("Disconnected. Thanks for racing!")

    # ===================================================================
    # Networking tasks
    # ===================================================================
    async def _receiver(self):
        try:
            async for raw in self.ws:
                try:
                    msg = P.decode(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == P.S_AUTH_OK:
                    self._on_auth_ok(msg)
                elif mtype == P.S_AUTH_FAIL:
                    self.login_error = msg.get("msg") or "authentication failed"
                    if self.login_stage == "submitting":
                        self.login_stage = "username"
                    self.field_password = ""
                elif mtype == P.S_STATE:
                    self._on_state(msg)
                elif mtype == P.S_LEADERBOARD:
                    self.leaderboard_metric = msg.get("metric", "best_wpm")
                    self.leaderboard_mode = msg.get("mode")
                    self.leaderboard_rows = msg.get("rows", [])
                    if self.authed:
                        self.view = "leaderboard"
                elif mtype == P.S_PROFILE:
                    self.profile_data = msg
                    if self.authed:
                        self.view = "profile"
                elif mtype == P.S_HISTORY:
                    self.history_data = msg.get("rows", [])
                    if self.authed:
                        self.view = "history"
                elif mtype == P.S_BANLIST:
                    self.banlist = msg.get("rows", [])
                    if self.authed:
                        self.view = "banlist"
                elif mtype == P.S_ERROR:
                    self.error_msg = msg.get("msg")
                    return
        except ConnectionClosed:
            code = getattr(self.ws, "close_code", None)
            if code == P.CLOSE_REPLACED:
                self.exit_reason = "You logged in from another window."
            elif code == P.CLOSE_KICKED:
                self.exit_reason = "You were removed by the host."
            else:
                self.exit_reason = "Connection to the host was closed."
        finally:
            self.running = False

    async def _send(self, obj):
        try:
            await self.ws.send(P.encode(obj))
        except ConnectionClosed:
            self.running = False

    def _on_state(self, msg):
        phase = msg.get("phase")
        text = msg.get("text", "")
        self.mode = msg.get("mode", "classic")
        self.time_left = msg.get("time_left")
        self.text_category = msg.get("text_category")
        if "config" in msg:
            self.config = msg["config"]
        if "config_options" in msg:
            self.config_options = msg["config_options"]
        if "chat" in msg:
            self.chat_lines = msg["chat"]
        if "announcements" in msg:
            self.announcements = msg["announcements"]
        if "session" in msg:
            self.session = msg["session"]
        # celebration arrives once (the first results frame); latch it so it
        # stays visible for the whole results screen, not a single 30ms frame.
        if msg.get("celebration"):
            self.celebration = msg["celebration"]

        # A new race begins at the COUNTDOWN transition -- but an instant
        # countdown (countdown=0 / quick-start) jumps straight to RACING with no
        # COUNTDOWN frame ever broadcast, so reset on that direct LOBBY/RESULTS ->
        # RACING jump too. Without this the previous race's finished_local/t_pos
        # leak in and the next race is untypable.
        entering_race = (phase == P.PHASE_COUNTDOWN
                         and self.prev_phase != P.PHASE_COUNTDOWN)
        instant_race = (phase == P.PHASE_RACING
                        and self.prev_phase not in (P.PHASE_COUNTDOWN,
                                                    P.PHASE_RACING))
        if entering_race or instant_race:
            self._reset_typing(text)
            self.compose = None         # don't let a draft swallow the first char
            self.announcements = []     # clear last race's badge popups
            self.celebration = None     # and last race's winner banner
            self.view = "game"          # close any overlay so the race takes over
        if phase == P.PHASE_RACING and self.prev_phase != P.PHASE_RACING:
            if text:
                self.text = text
            if self.local_start is None:
                self.local_start = time.monotonic()
            self._beep()                # "GO!"
        if phase == P.PHASE_RESULTS and self.prev_phase != P.PHASE_RESULTS:
            self._beep()                # race over
        # TIMED mode grows the passage mid-race; accept the longer text without
        # disturbing the local cursor.
        if phase == P.PHASE_RACING and text and len(text) > len(self.text):
            self.text = text
        if phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            self.local_start = None
        self.state = msg
        self.prev_phase = phase

    def _reset_typing(self, text):
        self.text = text or ""
        self.t_pos = 0
        self.t_errors = 0
        self.t_keystrokes = 0
        self.t_error_flag = False
        self.local_start = None
        self.finished_local = False
        self.local_finish_time = None

    def _on_auth_ok(self, msg):
        version = msg.get("version")
        if version is not None and version != P.PROTOCOL_VERSION:
            self.error_msg = (f"server runs protocol v{version}, this client is "
                              f"v{P.PROTOCOL_VERSION} - update to match")
            self.running = False
            return
        self.my_id = msg.get("id")
        self.is_admin = bool(msg.get("is_admin"))
        self.is_guest = bool(msg.get("is_guest"))
        self.account = msg.get("account")
        self.username = msg.get("name", self.field_username)
        self.my_stats = msg.get("stats")
        self.authed = True
        self.view = "game"
        self.sel_id = self.my_id
        self.login_error = None
        if self.username:
            self.settings["last_username"] = self.username
            self._save_settings()

    # ===================================================================
    # Input handling
    # ===================================================================
    async def _input_consumer(self):
        while self.running:
            kind, val = await self.queue.get()
            await self._handle_key(kind, val)

    async def _handle_key(self, kind, val):
        if kind == "special" and val == KEY_CTRL_C:
            self.exit_reason = "Thanks for racing!"
            self.running = False
            return

        if not self.authed:
            await self._handle_login_key(kind, val)
            return

        # Text compose (chat / custom passage) captures all keys while active.
        if self.compose is not None:
            await self._handle_compose_key(kind, val)
            return

        # Full-screen overlays.
        if self.view == "leaderboard":
            await self._handle_leaderboard_key(kind, val)
            return
        if self.view == "banlist":
            await self._handle_banlist_key(kind, val)
            return
        if self.view in ("profile", "history", "help"):
            self.view = "game"      # any key dismisses
            return
        if self.view == "setup":
            await self._handle_setup_key(kind, val)
            return

        phase = self._phase()
        if (phase == P.PHASE_RACING and self._am_racing()
                and not self.finished_local and not self._am_eliminated()):
            await self._handle_typing(kind, val)
            return

        if kind == "special" and val == KEY_ENTER and self.is_admin \
                and phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            # From results, a clean re-rack; from the lobby, a normal start.
            kind_msg = P.C_RERACE if phase == P.PHASE_RESULTS else P.C_START
            await self._send({"type": kind_msg})
            return
        if kind == "special" and val == KEY_TAB:
            await self._cycle_selection()
            return
        if kind == "char":
            await self._handle_command(val.lower(), phase)
            return
        if kind == "special" and val == KEY_ESC:
            self.exit_reason = "Thanks for racing!"
            self.running = False

    async def _handle_command(self, c, phase):
        in_lobby = phase in (P.PHASE_LOBBY, P.PHASE_RESULTS)
        # Quick-chat emotes: number keys, usable in the lobby/results and (for
        # spectators/eliminated/finished players) mid-race. Active typists never
        # reach here -- their keys go to the typing handler.
        if c.isdigit():
            idx = int(c) - 1
            if 0 <= idx < len(P.EMOTE_ORDER):
                await self._send({"type": P.C_EMOTE, "code": P.EMOTE_ORDER[idx]})
            return
        if c == "q":
            self.exit_reason = "Thanks for racing!"
            self.running = False
        elif c == "?":
            self.view = "help"
        elif c == "o" and not self.is_guest:
            await self._cycle_my_color()
        elif c == "l":
            await self._send({"type": P.C_LEADERBOARD,
                              "metric": self.leaderboard_metric,
                              "mode": self.leaderboard_mode})
        elif c == "p":
            await self._send({"type": P.C_PROFILE, "target_id": self.sel_id})
        elif c == "h":
            await self._send({"type": P.C_HISTORY})
        elif c == "c":
            i = THEME_ORDER.index(self.theme) if self.theme in THEME_ORDER else 0
            self.theme = THEME_ORDER[(i + 1) % len(THEME_ORDER)]
            self.settings["theme"] = self.theme
            self._save_settings()
        elif c == "u":
            i = UNIT_ORDER.index(self.units) if self.units in UNIT_ORDER else 0
            self.units = UNIT_ORDER[(i + 1) % len(UNIT_ORDER)]
            self.settings["units"] = self.units
            self._save_settings()
        elif c == "s":
            self.sound = not self.sound
            self.settings["sound"] = self.sound
            self._save_settings()
        elif in_lobby and c == "r":
            await self._toggle_ready()
        elif in_lobby and c == "t":
            self.compose = "chat"
            self.compose_draft = ""
        elif in_lobby and c == "m" and self.is_admin:
            self.view = "setup"
        elif in_lobby and c == "k" and self.is_admin:
            await self._kick_selected()
        elif in_lobby and c == "b" and self.is_admin:
            await self._send({"type": P.C_BANLIST})

    async def _cycle_my_color(self):
        me = self._my_player()
        cur = me.get("color") if me else None
        colors = list(P.PLAYER_COLORS)
        try:
            i = colors.index(cur)
        except ValueError:
            i = -1
        await self._send({"type": P.C_SETCOLOR,
                          "color": colors[(i + 1) % len(colors)]})

    async def _handle_banlist_key(self, kind, val):
        if kind == "char" and val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(self.banlist):
                await self._send({"type": P.C_UNBAN, "username": self.banlist[idx]})
                await self._send({"type": P.C_BANLIST})     # refresh the list
            return
        self.view = "game"

    async def _cycle_selection(self):
        ids = [p["id"] for p in (self.state or {}).get("players", [])
               if p.get("connected")]
        if not ids:
            return
        if self.sel_id not in ids:
            self.sel_id = ids[0]
        else:
            self.sel_id = ids[(ids.index(self.sel_id) + 1) % len(ids)]

    async def _kick_selected(self):
        if self.sel_id is None or self.sel_id == self.my_id:
            return
        if self.kick_armed == self.sel_id:
            await self._send({"type": P.C_KICK, "target_id": self.sel_id})
            self.kick_armed = None
        else:
            self.kick_armed = self.sel_id   # require a confirming second press

    async def _handle_typing(self, kind, val):
        n = len(self.text)
        if kind == "special" and val == KEY_BACKSPACE:
            if self.t_pos > 0:
                self.t_pos -= 1
                self.t_error_flag = False
                await self._send_progress()
            return
        if kind != "char":
            return
        if self.t_pos >= n:
            return
        expected = self.text[self.t_pos]
        self.t_keystrokes += 1
        if val == expected:
            self.t_pos += 1
            self.t_error_flag = False
            # In TIMED mode the passage keeps growing; reaching the current end
            # is not a finish -- wait for more text.
            if self.t_pos >= n and self.mode != "timed":
                await self._finish_local()
            else:
                await self._send_progress()
        else:
            self.t_errors += 1
            self.t_error_flag = True
            await self._send_progress()

    async def _finish_local(self):
        self.finished_local = True
        if self.local_start is not None:
            self.local_finish_time = time.monotonic() - self.local_start
        self._beep()
        await self._send_progress(force=True)

    async def _send_progress(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress_sent < 0.03:
            return
        self.last_progress_sent = now
        await self._send({
            "type": P.C_PROGRESS,
            "pos": self.t_pos,
            "errors": self.t_errors,
            "keystrokes": self.t_keystrokes,
        })

    async def _toggle_ready(self):
        me = self._my_player()
        current = bool(me and me["ready"])
        await self._send({"type": P.C_READY, "ready": not current})

    # -- preferences / units / sound ---------------------------------------
    def _save_settings(self):
        client_settings.save(self.settings)

    def _fmt_speed(self, wpm):
        """Format a speed for the HUD/racetrack, honoring the units preference."""
        w = int(round(wpm))
        if self.units == "cpm":
            return f"{w * 5}"
        if self.units == "both":
            return f"{w}w/{w * 5}c"
        return f"{w}"

    def _table_speed(self, wpm):
        """Numeric speed for fixed-width tables (both -> primary WPM)."""
        return int(round(wpm)) * (5 if self.units == "cpm" else 1)

    def _speed_tail(self, wpm):
        """Speed + lowercase unit suffix for the free-form racetrack tail."""
        s = self._fmt_speed(wpm)
        if self.units == "both":
            return s
        return s + ("cpm" if self.units == "cpm" else "wpm")

    def _unit_label(self):
        return "CPM" if self.units == "cpm" else "WPM"

    def _beep(self):
        if self.sound:
            T.write("\a")

    _SPARK = " .:-=+*#%@"

    def _sparkline(self, samples, width=24):
        """ASCII WPM-over-time sparkline (cross-platform; no unicode blocks)."""
        vals = [float(v) for v in (samples or []) if isinstance(v, (int, float))]
        if not vals:
            return ""
        if len(vals) > width:                # downsample to fit
            step = len(vals) / width
            vals = [vals[int(i * step)] for i in range(width)]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1.0
        ramp = self._SPARK
        return "".join(
            ramp[max(0, min(len(ramp) - 1, int((v - lo) / rng * (len(ramp) - 1))))]
            for v in vals)

    # -- compose (chat / custom text) --------------------------------------
    async def _handle_compose_key(self, kind, val):
        if kind == "special" and val == KEY_ESC:
            self.compose = None
            self.compose_draft = ""
        elif kind == "special" and val == KEY_BACKSPACE:
            self.compose_draft = self.compose_draft[:-1]
        elif kind == "special" and val == KEY_ENTER:
            await self._submit_compose()
        elif kind == "char":
            self.compose_draft = (self.compose_draft + val)[:200]

    async def _submit_compose(self):
        mode, draft = self.compose, self.compose_draft.strip()
        self.compose = None
        self.compose_draft = ""
        if mode == "chat" and draft:
            await self._send({"type": P.C_CHAT, "text": draft})
        elif mode == "custom":
            await self._send({"type": P.C_CONFIG, "custom_text": draft})

    # -- leaderboard overlay -----------------------------------------------
    async def _handle_leaderboard_key(self, kind, val):
        if kind == "char" and val in ("[", "]"):
            step = 1 if val == "]" else -1
            i = (self.lb_metrics.index(self.leaderboard_metric)
                 if self.leaderboard_metric in self.lb_metrics else 0)
            self.leaderboard_metric = self.lb_metrics[(i + step) % len(self.lb_metrics)]
            await self._send({"type": P.C_LEADERBOARD,
                              "metric": self.leaderboard_metric,
                              "mode": self.leaderboard_mode})
        else:
            self.view = "game"

    # -- setup overlay (admin) ---------------------------------------------
    async def _handle_setup_key(self, kind, val):
        if kind == "special" and val in (KEY_ESC, KEY_ENTER):
            self.view = "game"
            return
        if kind != "char":
            return
        c = val.lower()
        opts = self.config_options or {}
        if c == "m":
            await self._cycle_config("mode", opts.get("modes", []))
        elif c == "l":
            await self._cycle_config("length", opts.get("lengths", []))
        elif c == "g":   # 'g' = genre/category ('c' is reserved for color)
            await self._cycle_config("category", opts.get("categories", []))
        elif c == "d":
            await self._cycle_config("difficulty", opts.get("difficulties", []))
        elif c == "t":
            await self._cycle_config("time_limit", opts.get("time_limits", []))
        elif c == "v":
            await self._cycle_config("lives", opts.get("lives", []))
        elif c == "x":
            self.compose = "custom"
            self.compose_draft = ""
        elif c == "b":
            self._cycle_bot_difficulty(opts.get("bot_difficulties", []))
        elif c == "a":
            await self._send({"type": P.C_ADD_BOT,
                              "difficulty": self.setup_bot_difficulty})
        elif c == "z":
            await self._send({"type": P.C_REMOVE_BOT})
        elif c == "o":
            await self._cycle_config("countdown", opts.get("countdowns", []))
        elif c == "i":
            await self._send({"type": P.C_CONFIG,
                              "quick_start": not self.config.get("quick_start")})
        elif c == "n":
            await self._cycle_config("min_players", [1, 2, 3, 4, 5, 6, 7, 8])
        elif c == "e":
            await self._cycle_config("rematch_secs", opts.get("rematch_secs", []))
        elif c == "q":
            self.view = "game"

    def _cycle_bot_difficulty(self, options):
        if not options:
            options = ["easy", "medium", "hard", "insane", "rival"]
        try:
            i = options.index(self.setup_bot_difficulty)
        except ValueError:
            i = -1
        self.setup_bot_difficulty = options[(i + 1) % len(options)]

    async def _cycle_config(self, field, options):
        if not options:
            return
        cur = self.config.get(field)
        try:
            i = options.index(cur)
        except ValueError:
            i = -1
        nxt = options[(i + 1) % len(options)]
        await self._send({"type": P.C_CONFIG, field: nxt})

    # ===================================================================
    # Login form
    # ===================================================================
    async def _handle_login_key(self, kind, val):
        stage = self.login_stage
        if stage == "submitting":
            return  # waiting on the server; ignore everything but Ctrl-C

        if stage == "choose":
            if kind == "char":
                c = val.lower()
                if c == "l":
                    self._start_login_action("login")
                elif c == "r":
                    self._start_login_action("register")
                elif c == "g":
                    self._start_login_action("guest")
                elif c == "q":
                    self.exit_reason = "Bye!"
                    self.running = False
            return

        # username / password text entry
        if kind == "special":
            if val == KEY_ESC:
                self._reset_login()
            elif val == KEY_BACKSPACE:
                self._edit_login_field(backspace=True)
            elif val == KEY_ENTER:
                await self._submit_login_stage()
            return
        if kind == "char":
            self._edit_login_field(char=val)

    def _start_login_action(self, action):
        self.login_action = action
        self.login_error = None
        self.field_password = ""
        self.login_stage = "username"

    def _reset_login(self):
        self.login_stage = "choose"
        self.login_action = None
        self.field_password = ""
        self.login_error = None

    def _edit_login_field(self, char=None, backspace=False):
        if self.login_stage == "username":
            if backspace:
                self.field_username = self.field_username[:-1]
            elif char and char.isprintable() and char != " ":
                self.field_username = (self.field_username + char)[:16]
        elif self.login_stage == "password":
            if backspace:
                self.field_password = self.field_password[:-1]
            elif char and char.isprintable():
                self.field_password = (self.field_password + char)[:64]

    async def _submit_login_stage(self):
        if self.login_stage == "username":
            if not self.field_username.strip():
                self.login_error = "enter a username"
                return
            if self.login_action == "guest":
                await self._submit_login()
            else:
                self.login_error = None
                self.login_stage = "password"
        elif self.login_stage == "password":
            if not self.field_password:
                self.login_error = "enter a password"
                return
            await self._submit_login()

    async def _submit_login(self):
        self.login_stage = "submitting"
        self.login_error = None
        token = self.admin_token
        rp = self.room_password
        if self.login_action == "guest":
            await self._send({"type": P.C_GUEST, "name": self.field_username.strip(),
                              "token": token, "room_password": rp,
                              "version": P.PROTOCOL_VERSION})
        elif self.login_action == "register":
            await self._send({"type": P.C_REGISTER,
                              "username": self.field_username.strip(),
                              "password": self.field_password,
                              "token": token, "room_password": rp,
                              "version": P.PROTOCOL_VERSION})
        else:  # login
            await self._send({"type": P.C_LOGIN,
                              "username": self.field_username.strip(),
                              "password": self.field_password,
                              "token": token, "room_password": rp,
                              "version": P.PROTOCOL_VERSION})

    # ===================================================================
    # State helpers
    # ===================================================================
    def _phase(self):
        return self.state.get("phase") if self.state else None

    def _my_player(self):
        if not self.state:
            return None
        for p in self.state["players"]:
            if p["id"] == self.my_id:
                return p
        return None

    def _am_racing(self):
        me = self._my_player()
        return bool(me and me["in_race"])

    def _am_eliminated(self):
        me = self._my_player()
        return bool(me and me.get("eliminated"))

    def _selected_name(self):
        for p in (self.state or {}).get("players", []):
            if p["id"] == self.sel_id:
                return p["name"]
        return None

    def _local_elapsed(self):
        if self.local_start is None:
            return float(self.state.get("elapsed", 0.0)) if self.state else 0.0
        if self.finished_local and self.local_finish_time is not None:
            return self.local_finish_time
        return time.monotonic() - self.local_start

    def _local_stats(self):
        n = len(self.text) or 1
        minutes = self._local_elapsed() / 60.0
        wpm = round((self.t_pos / 5.0) / minutes) if minutes > 0 else 0
        typed = self.t_pos + self.t_errors
        acc = round(100.0 * self.t_pos / typed, 1) if typed else 100.0
        pct = int(100 * self.t_pos / n)
        return wpm, acc, pct

    # ===================================================================
    # Rendering
    # ===================================================================
    async def _renderer(self):
        while self.running:
            self._render()
            await asyncio.sleep(0.05)

    def _render(self):
        cols, rows = T.get_size()
        if not self.authed:
            self._draw(self._frame_login(cols), cols, rows)
            return
        overlay = {
            "leaderboard": self._frame_leaderboard,
            "setup": self._frame_setup,
            "profile": self._frame_profile,
            "history": self._frame_history,
            "help": self._frame_help,
            "banlist": self._frame_banlist,
        }.get(self.view)
        if overlay is not None:
            self._draw(overlay(cols), cols, rows)
            return
        phase = self._phase()
        if self.state is None:
            lines = ["", "  " + FG_GREY + f"Connecting to {self.uri} ..." + RESET]
        elif phase == P.PHASE_LOBBY:
            lines = self._frame_lobby(cols)
        elif phase == P.PHASE_COUNTDOWN:
            lines = self._frame_countdown(cols)
        elif phase == P.PHASE_RACING:
            lines = self._frame_race(cols)
        elif phase == P.PHASE_RESULTS:
            lines = self._frame_results(cols)
        else:
            lines = []
        self._draw(lines, cols, rows)

    def _draw(self, lines, cols, rows):
        out = [T.CURSOR_HOME]
        for ln in lines[: rows - 1]:
            out.append(self._clip(ln, cols))
            out.append(T.CLEAR_EOL)
            out.append("\r\n")
        out.append(T.CLEAR_BELOW)
        frame = "".join(out)
        if not self.base_color or self.theme == "mono":
            frame = T.strip_color(frame)
        else:
            mapping = THEME_MAPS.get(self.theme)
            if mapping:
                frame = T.remap_sgr(frame, mapping)
        T.write(frame)

    def _clip(self, line, cols):
        """Truncate a styled line to ``cols`` *visible* columns.

        The in-place redraw assumes one logical line == one terminal row; a line
        wider than the terminal would auto-wrap and corrupt the frame. ANSI
        escape sequences are copied verbatim and don't count toward the width.
        """
        out = []
        visible = 0
        i, n = 0, len(line)
        truncated = False
        while i < n:
            ch = line[i]
            if ch == "\x1b":
                j = i + 1
                if j < n and line[j] == "[":
                    j += 1
                    while j < n and not ("\x40" <= line[j] <= "\x7e"):
                        j += 1
                    j += 1  # include the final byte
                else:
                    j = i + 2
                out.append(line[i:j])
                i = j
                continue
            if visible >= cols:
                truncated = True
                break
            out.append(ch)
            visible += 1
            i += 1
        if truncated:
            out.append(RESET)
        return "".join(out)

    # -- shared pieces ------------------------------------------------------
    def _banner(self):
        return [
            "",
            "  " + BOLD + FG_BRIGHT_CYAN + "T Y P E R A C E R" + RESET
            + "   " + FG_GREY + "LAN multiplayer typing race" + RESET,
        ]

    def _center(self, text, cols, visible_len):
        pad = max(0, (cols - visible_len) // 2)
        return " " * pad + text

    def _bar(self, frac, width):
        filled = max(0, min(width, int(round(frac * width))))
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _place_label(self, place):
        return f"#{place}" if place else "  -"

    def _controls(self, results=False):
        ready = "rematch" if results else "ready"
        base = (f"R {ready}   T chat   1-8 emote   TAB select   P profile   "
                "H history   L board   ?=help   Q quit")
        L = ["  " + FG_GREY + base + RESET]
        if not self.is_guest:
            L.append("  " + FG_GREY + "O accent color" + RESET)
        if self.is_admin:
            startlbl = "rematch now" if results else "start now"
            L.append("  " + FG_GREY + f"host:  Enter {startlbl}   M setup   "
                     "K kick   B bans" + RESET)
        return L

    def _identity_line(self):
        if self.is_guest:
            return ("  " + FG_GREY + "Playing as guest " + RESET
                    + BOLD + self.username + RESET
                    + FG_GREY + " (stats are not saved)" + RESET)
        s = self.my_stats or {}
        extra = ""
        if s:
            extra = (FG_GREY + f"  -  best {s.get('best_wpm', 0)} wpm, "
                     f"{s.get('races', 0)} races, {s.get('wins', 0)} wins, "
                     f"{s.get('badges', 0)} badges" + RESET)
        return ("  " + FG_GREY + "Logged in as " + RESET
                + BOLD + FG_BRIGHT_CYAN + self.username + RESET + extra)

    def _config_summary(self):
        c = self.config or {}
        mode = c.get("mode", "classic")
        parts = [{"classic": "Classic", "timed": "Timed",
                  "survival": "Survival"}.get(mode, mode)]
        if mode == "timed":
            parts.append(f"{c.get('time_limit', 30)}s")
        if mode == "survival":
            parts.append(f"{c.get('lives', 3)} lives")
        parts.append("custom text" if c.get("has_custom") else c.get("category", "any"))
        parts.append(c.get("length", "medium"))
        diff = c.get("difficulty")
        if diff:
            parts.append({1: "easy", 2: "medium", 3: "hard"}.get(diff, str(diff)))
        if c.get("quick_start"):
            parts.append("quick")
        cd = c.get("countdown")
        if cd == 0:
            parts.append("instant start")
        return " - ".join(str(p) for p in parts)

    def _chat_panel(self, cols, max_lines=6):
        L = ["  " + BOLD + "Chat" + RESET + FG_GREY + "  (T to type)" + RESET]
        lines = self.chat_lines[-max_lines:]
        if not lines:
            L.append("    " + FG_GREY + "(no messages yet)" + RESET)
        for c in lines:
            if c.get("kind") == "system":
                L.append("    " + FG_GREY + "* " + c.get("text", "") + RESET)
            elif c.get("kind") == "emote":
                you = c.get("id") == self.my_id
                col = FG_BRIGHT_CYAN if you else FG_CYAN
                L.append("    " + col + c.get("name", "?") + RESET
                         + FG_YELLOW + " " + c.get("text", "") + RESET)
            else:
                you = c.get("id") == self.my_id
                col = FG_BRIGHT_CYAN if you else FG_CYAN
                L.append("    " + col + c.get("name", "?") + RESET
                         + FG_GREY + ": " + RESET + c.get("text", ""))
        if self.compose == "chat":
            L.append("  " + FG_YELLOW + "> " + self.compose_draft + REVERSE
                     + " " + RESET)
        return L

    # -- session scoreboard / celebration / popups --------------------------
    def _session_summary_line(self):
        s = self.session or {}
        st = s.get("standings", [])
        if not st or s.get("race_no", 0) < 1:
            return None
        leader = st[0]
        return ("  " + FG_GREY + "Session: " + RESET + BOLD
                + leader.get("name", "") + RESET + FG_GREY
                + f" leads, {leader.get('points', 0)} pts after "
                f"{s.get('race_no', 0)} race(s)" + RESET)

    def _session_panel(self, max_rows=6):
        s = self.session or {}
        standings = s.get("standings", [])
        if not standings or s.get("race_no", 0) < 1:
            return []
        L = ["  " + BOLD + "Session standings" + RESET + FG_GREY
             + f"   after {s.get('race_no', 0)} race(s)" + RESET,
             "  " + BOLD
             + f"{'#':<3} {'Player':<16} {'Pts':>4} {'W':>3} {'Best':>5}" + RESET]
        for i, e in enumerate(standings[:max_rows], 1):
            you = e.get("name") == self.username
            bp = e.get("best_place")
            best = ("#" + str(bp)) if bp else "-"
            row = (f"{i:<3} {e.get('name', '')[:16]:<16} {e.get('points', 0):>4} "
                   f"{e.get('wins', 0):>3} {best:>5}")
            if you:
                row = BOLD + FG_BRIGHT_CYAN + row + RESET
            elif i == 1:
                row = FG_YELLOW + row + RESET
            L.append("  " + row)
        return L

    def _celebration_lines(self):
        c = self.celebration
        if not c:
            return []
        flagtxt = {"flawless": "FLAWLESS", "photo_finish": "PHOTO FINISH",
                   "streak": "ON A STREAK", "upset": "UPSET"}
        tags = "  ".join(flagtxt[f] for f in c.get("flags", []) if f in flagtxt)
        botmark = FG_GREY + " [bot]" + RESET if c.get("is_bot") else ""
        out = ["  " + BOLD + FG_YELLOW + "WINNER  " + RESET + BOLD
               + c.get("winner", "") + RESET + botmark + FG_BRIGHT_CYAN
               + f"   {c.get('wpm', 0)} WPM" + RESET]
        if tags:
            out.append("  " + FG_YELLOW + "* " + tags + " *" + RESET)
        return out

    def _announcement_line(self, a):
        kind = a.get("kind", "badge")
        name = a.get("name", "")
        if kind == "level":
            return ("  " + FG_BRIGHT_CYAN + "^ " + RESET + BOLD + name + RESET
                    + FG_BRIGHT_CYAN + f" reached level {a.get('level')}!" + RESET)
        if kind == "pb":
            return ("  " + FG_YELLOW + "* " + RESET + BOLD + name + RESET
                    + FG_YELLOW + f" new {a.get('pb_kind')} best: "
                    + f"{a.get('new')} (was {a.get('old')})" + RESET)
        if kind == "daystreak":
            return ("  " + FG_MAGENTA + "+ " + RESET + BOLD + name + RESET
                    + FG_MAGENTA + f" on a {a.get('days')}-day streak!" + RESET)
        return ("  " + FG_YELLOW + "* " + RESET + BOLD + name + RESET
                + FG_YELLOW + " earned " + BOLD + a.get("badge", "") + RESET
                + FG_YELLOW + "!" + RESET)

    # -- lobby --------------------------------------------------------------
    def _frame_lobby(self, cols):
        L = self._banner()
        L.append("")
        L.append(self._identity_line())
        L.append("  " + FG_GREY + "Next race:  " + RESET + BOLD
                 + self._config_summary() + RESET)
        sess = self._session_summary_line()
        if sess:
            L.append(sess)
        L.append("")
        L.append("  " + BOLD + FG_CYAN + "LOBBY" + RESET
                 + "   others join with:  " + FG_YELLOW + self.host_hint + RESET)
        L.append("")
        L.append("  " + BOLD + "Players:" + RESET)
        for p in self.state["players"]:
            L.append("  " + self._lobby_row(p))
        L.append("")
        L += self._chat_panel(cols)
        L.append("")
        L += self._controls()
        return L

    def _lobby_row(self, p):
        you = p["id"] == self.my_id
        sel = (FG_YELLOW + "> " + RESET) if p["id"] == self.sel_id else "  "
        if you:
            name = BOLD + p["name"] + RESET
        else:
            name = self._color_code(p.get("color")) + p["name"] + RESET
        tag = FG_GREY + " (you)" + RESET if you else ""
        host = " " + FG_MAGENTA + "[host]" + RESET if p["is_admin"] else ""
        status = (FG_BRIGHT_GREEN + "READY" + RESET) if p["ready"] \
            else (FG_GREY + "not ready" + RESET)
        if p.get("idle"):
            status += FG_GREY + " (idle)" + RESET
        stats = p.get("stats")
        if p.get("is_bot"):
            diff = (p.get("difficulty") or "bot").capitalize()
            host = " " + FG_MAGENTA + f"[bot:{diff}]" + RESET
            extra = ""
        elif p.get("is_guest"):
            extra = FG_GREY + "  guest" + RESET
        elif stats:
            badge = self._rank_badge(p)
            extra = (FG_GREY + f"  {badge}{stats['best_wpm']}wpm best, "
                     f"{stats['races']}r" + RESET)
        else:
            extra = ""
        return f"{sel}{name}{tag}{host}  -  {status}{extra}"

    def _rank_badge(self, p):
        """Compact 'Lv7 Gold ' prefix for an accounted player, when present."""
        stats = p.get("stats") or {}
        lvl = p.get("level") or stats.get("level")
        tier = p.get("tier") or stats.get("tier")
        bits = []
        if lvl:
            bits.append(f"Lv{lvl}")
        if tier:
            bits.append(tier)
        return (" ".join(bits) + " ") if bits else ""

    # -- countdown ----------------------------------------------------------
    def _frame_countdown(self, cols):
        L = self._banner()
        L.append("")
        n = self.state.get("countdown", 0)
        label = "GO!" if n <= 0 else str(n)
        plain = f"Get ready...  {label}"
        L.append("")
        L.append(self._center(BOLD + FG_BRIGHT_CYAN + plain + RESET, cols, len(plain)))
        L.append("")
        L.append("  " + FG_GREY + "Passage:" + RESET)
        width = min(cols - 4, 92)
        for ln in self._wrap_plain(self.state.get("text", ""), width):
            L.append("  " + DIM + ln + RESET)
        L.append("")
        connected = [p for p in self.state["players"] if p["connected"]]
        L += self._racetrack(cols, connected)
        return L

    # -- race ---------------------------------------------------------------
    def _frame_race(self, cols):
        L = []
        elapsed = self._local_elapsed()
        wpm, acc, pct = self._local_stats()
        ulbl = self._unit_label()
        if self.mode == "timed" and self.time_left is not None:
            L.append(
                "  " + BOLD + "TIME LEFT " + RESET + FG_BRIGHT_CYAN
                + f"{self.time_left:5.1f}s" + RESET
                + "   " + BOLD + ulbl + " " + RESET + self._fmt_speed(wpm)
                + "   " + BOLD + "ACC " + RESET + f"{acc:5.1f}%"
                + "   " + BOLD + "CHARS " + RESET + f"{self.t_pos}")
        else:
            head = ("  " + BOLD + "TIME " + RESET + f"{elapsed:5.1f}s"
                    + "   " + BOLD + ulbl + " " + RESET + FG_BRIGHT_CYAN
                    + self._fmt_speed(wpm) + RESET
                    + "   " + BOLD + "ACC " + RESET + f"{acc:5.1f}%"
                    + "   " + BOLD + "DONE " + RESET + f"{pct:3d}%")
            if self.mode == "survival":
                me = self._my_player()
                lives = me.get("lives") if me else None
                if lives is not None:
                    hearts = (FG_BRIGHT_RED + ("o " * lives).strip() + RESET
                              if lives else FG_GREY + "none" + RESET)
                    head += "   " + BOLD + "LIVES " + RESET + hearts
            L.append(head)
        L.append("")
        L += self._text_block(cols)
        L.append("")
        L.append("  " + FG_GREY + "Race:" + RESET)
        racers = [p for p in self.state["players"] if p["in_race"]]
        L += self._racetrack(cols, racers)
        specs = [p["name"] for p in self.state["players"]
                 if p["connected"] and not p["in_race"]]
        if specs:
            L.append("  " + FG_GREY + "Watching: " + ", ".join(specs[:6]) + RESET)
        L.append("")
        if self._am_eliminated():
            L.append("  " + FG_BRIGHT_RED + "Eliminated!" + RESET
                     + "  Watching the rest...   " + FG_GREY + "Q = quit" + RESET)
        elif self.finished_local:
            L.append("  " + FG_BRIGHT_GREEN + "You finished!" + RESET
                     + "  Waiting for the others...   " + FG_GREY + "Q = quit" + RESET)
        else:
            hint = "Type the text. Backspace fixes the current spot."
            if self.mode == "survival":
                hint = "Type carefully - every mistake costs a life!"
            elif self.mode == "timed":
                hint = "Type as much as you can before the clock runs out!"
            L.append("  " + FG_GREY + hint + "  Ctrl-C quits." + RESET)
        return L

    def _text_block(self, cols):
        width = min(cols - 4, 92)
        lines = []
        for (s, e) in wrap_indices(self.text, width):
            lines.append("  " + "".join(self._styled_char(i) for i in range(s, e)) + RESET)
        return lines

    def _styled_char(self, idx):
        ch = self.text[idx]
        if idx < self.t_pos:
            return FG_GREEN + ch
        if idx == self.t_pos and not self.finished_local:
            if self.t_error_flag:
                return BG_RED + FG_WHITE + ch + RESET
            return REVERSE + ch + RESET
        return FG_GREY + ch

    # -- shared race track --------------------------------------------------
    def _racetrack(self, cols, players):
        n = self.state.get("text_len", 1) or 1

        def sort_key(p):
            if p["finished"]:
                return (0, p["place"] or 999)
            if p.get("eliminated"):
                return (2, p["place"] or 999)
            return (1, -p["pos"])

        players = sorted(players, key=sort_key)
        if not players:
            return ["    " + FG_GREY + "(no racers yet)" + RESET]
        namew = min(16, max(6, max(len(p["name"]) for p in players)))
        barw = max(8, min(cols - namew - 36, 44))
        return ["    " + self._track_row(p, namew, barw, n) for p in players]

    def _track_row(self, p, namew, barw, n):
        you = p["id"] == self.my_id
        if you and self._phase() == P.PHASE_RACING and not p["finished"]:
            pos = self.t_pos
            wpm = self._local_stats()[0]
        else:
            pos = p["pos"]
            wpm = p["wpm"]
        frac = pos / n if n else 0.0
        name = p["name"][:namew].ljust(namew)
        if you:
            name = BOLD + name + RESET
        bar = self._bar(frac, barw)
        flag = (" " + FG_BRIGHT_RED + "!" + RESET) if p.get("flagged") else ""
        bubble = self._emote_bubble(p)
        if p.get("eliminated"):
            return f"{name} {FG_GREY}{bar}{RESET} {FG_GREY}OUT{RESET}{flag}{bubble}"
        if p["finished"]:
            tail = (self._place_label(p["place"])
                    + f"  {self._speed_tail(p['wpm'])} {p['acc']:5.1f}%  "
                    f"{p['finish_time']:5.1f}s")
            return f"{name} {FG_BRIGHT_GREEN}{bar}{RESET} {tail}{flag}{bubble}"
        tail = f"{int(frac * 100):3d}% {self._speed_tail(wpm)}"
        if self.mode == "survival" and p.get("lives") is not None:
            tail += " " + FG_BRIGHT_RED + ("o" * p["lives"]) + RESET
        # Each racer's bar takes their accent color (you stay bright cyan).
        color = FG_BRIGHT_CYAN if you else self._color_code(p.get("color"))
        return f"{name} {color}{bar}{RESET} {tail}{flag}{bubble}"

    # -- results ------------------------------------------------------------
    def _frame_results(self, cols):
        L = self._banner()
        L.append("")
        cel = self._celebration_lines()
        if cel:
            L += cel
            L.append("")
        for a in self.announcements:
            L.append(self._announcement_line(a))
        if self.announcements:
            L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "RACE RESULTS" + RESET
                 + FG_GREY + "   " + self._config_summary() + RESET)
        L.append("")
        players = sorted(
            [p for p in self.state["players"] if p["in_race"]],
            key=lambda p: (p["place"] or 999),
        )
        L.append("  " + BOLD
                 + f"{'Rank':<5} {'Player':<18} {self._unit_label():>4} "
                 + f"{'Acc':>6} {'Time':>8}"
                 + RESET)
        for p in players:
            you = p["id"] == self.my_id
            rank = self._place_label(p["place"])
            name = p["name"][:18]
            if p.get("eliminated"):
                t = "OUT"
            elif p["finish_time"] is not None:
                t = f"{p['finish_time']:.1f}s"
            else:
                t = "--"
            row = (f"{rank:<5} {name:<18} {self._table_speed(p['wpm']):>4} "
                   f"{p['acc']:>5.1f}% {t:>8}")
            if you:
                row = BOLD + FG_BRIGHT_CYAN + row + RESET
            elif p["place"] == 1:
                row = FG_YELLOW + row + RESET
            L.append("  " + row)
        # Per-racer WPM-over-time sparklines (results-only data).
        paced = [p for p in players if p.get("splits")]
        if paced:
            L.append("")
            L.append("  " + BOLD + "Pace" + RESET + FG_GREY
                     + "  (WPM over the race, slow .. fast)" + RESET)
            for p in paced[:6]:
                sp = p["splits"]
                L.append("    " + f"{p['name'][:12]:<12} " + FG_BRIGHT_CYAN
                         + self._sparkline(sp) + RESET + FG_GREY
                         + f"  {int(min(sp))}-{int(max(sp))}" + RESET)
        L.append("")
        ready = sum(1 for p in self.state["players"]
                    if p["connected"] and not p.get("is_bot") and p["ready"])
        total = sum(1 for p in self.state["players"]
                    if p["connected"] and not p.get("is_bot"))
        L.append("  " + FG_GREY + f"Ready for another race: {ready}/{total}" + RESET)
        session = self._session_panel(max_rows=5)
        if session:
            L.append("")
            L += session
        L.append("")
        L += self._chat_panel(cols, max_lines=3)
        L.append("")
        L += self._controls(results=True)
        return L

    def _wrap_plain(self, text, width):
        return [text[s:e] for (s, e) in wrap_indices(text, width)]

    # -- login --------------------------------------------------------------
    def _frame_login(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_CYAN + "SIGN IN" + RESET
                 + FG_GREY + f"   connected to {self.uri}" + RESET)
        L.append("")
        if self.login_stage == "choose":
            L.append("  Choose how to play:")
            L.append("")
            L.append("    " + BOLD + FG_BRIGHT_CYAN + "[L]" + RESET
                     + " Log in to an existing account")
            L.append("    " + BOLD + FG_BRIGHT_CYAN + "[R]" + RESET
                     + " Register a new account")
            L.append("    " + BOLD + FG_BRIGHT_CYAN + "[G]" + RESET
                     + " Play as guest " + FG_GREY + "(stats not saved)" + RESET)
            L.append("    " + BOLD + FG_BRIGHT_CYAN + "[Q]" + RESET + " Quit")
        else:
            titles = {"login": "Log in", "register": "Register",
                      "guest": "Guest play"}
            L.append("  " + BOLD + titles.get(self.login_action, "") + RESET)
            L.append("")
            user_active = self.login_stage == "username"
            L.append("    " + self._field_line("Username", self.field_username,
                                                user_active, mask=False))
            if self.login_action != "guest":
                pw_active = self.login_stage == "password"
                L.append("    " + self._field_line("Password", self.field_password,
                                                    pw_active, mask=True))
            L.append("")
            if self.login_stage == "submitting":
                L.append("    " + FG_YELLOW + "Submitting..." + RESET)
            else:
                L.append("    " + FG_GREY
                         + "Enter = continue    Backspace = edit    Esc = back"
                         + RESET)
        if self.login_error:
            L.append("")
            L.append("  " + FG_BRIGHT_RED + "! " + self.login_error + RESET)
        return L

    def _field_line(self, label, value, active, mask):
        shown = ("*" * len(value)) if mask else value
        cursor = (REVERSE + " " + RESET) if active else ""
        color = FG_BRIGHT_CYAN if active else FG_WHITE
        return (f"{label:<9} " + color + (shown or "") + RESET + cursor)

    # -- leaderboard --------------------------------------------------------
    _METRIC_LABELS = {
        "best_wpm": "best WPM", "avg_wpm": "average WPM", "races_won": "wins",
        "races_played": "races", "longest_streak": "longest streak",
        "consistency": "consistency",
    }

    def _frame_leaderboard(self, cols):
        L = self._banner()
        L.append("")
        label = self._METRIC_LABELS.get(self.leaderboard_metric, self.leaderboard_metric)
        scope = f" - {self.leaderboard_mode} mode" if self.leaderboard_mode else ""
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "LEADERBOARD" + RESET
                 + FG_GREY + f"   ranked by {label}{scope}" + RESET)
        L.append("")
        L.append("  " + BOLD
                 + f"{'#':<3} {'Player':<16} {'Best':>5} {'Avg':>5} "
                 + f"{'Races':>6} {'Wins':>5} {'Strk':>5} {'Cons':>5} {'Acc':>6}"
                 + RESET)
        if not self.leaderboard_rows:
            L.append("  " + FG_GREY + "No ranked players yet - finish a race!" + RESET)
        for i, r in enumerate(self.leaderboard_rows, 1):
            you = self.account and r["username"].lower() == self.account.lower()
            row = (f"{i:<3} {r['username'][:16]:<16} {r['best_wpm']:>5.0f} "
                   f"{r['avg_wpm']:>5.0f} {r['races_played']:>6} "
                   f"{r['races_won']:>5} {r.get('longest_streak', 0):>5} "
                   f"{r.get('consistency', 0):>5.0f} {r['avg_accuracy']:>5.0f}%")
            if you:
                row = BOLD + FG_BRIGHT_CYAN + row + RESET
            elif i == 1:
                row = FG_YELLOW + row + RESET
            L.append("  " + row)
        L.append("")
        L.append("  " + FG_GREY + "[ / ] cycle metric    any other key to go back"
                 + RESET)
        return L

    # -- setup / profile / history / help overlays -------------------------
    def _frame_setup(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "RACE SETUP" + RESET
                 + FG_GREY + "   (host only)" + RESET)
        L.append("")
        c = self.config or {}
        mode = c.get("mode", "classic")

        def row(key, lbl, value):
            return ("    " + BOLD + f"[{key}]" + RESET + f" {lbl:<12} "
                    + FG_BRIGHT_CYAN + str(value) + RESET)

        L.append(row("M", "Mode", {"classic": "Classic", "timed": "Timed",
                                    "survival": "Survival"}.get(mode, mode)))
        L.append(row("L", "Length", c.get("length", "medium")))
        cat = "custom" if c.get("has_custom") else c.get("category", "any")
        L.append(row("G", "Category", cat))
        diff = c.get("difficulty")
        L.append(row("D", "Difficulty",
                     {None: "any", 1: "easy", 2: "medium", 3: "hard"}.get(diff, diff)))
        if mode == "timed":
            L.append(row("T", "Time limit", f"{c.get('time_limit', 30)}s"))
        if mode == "survival":
            L.append(row("V", "Lives", c.get("lives", 3)))
        L.append(row("X", "Custom text", "(set)" if c.get("has_custom") else "none"))
        L.append("")
        # AI bots ---------------------------------------------------------
        nbots = sum(1 for p in (self.state or {}).get("players", [])
                    if p.get("is_bot"))
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "AI bots" + RESET
                 + FG_GREY + f"   {nbots} in the room" + RESET)
        L.append(row("B", "Add tier", self.setup_bot_difficulty.capitalize()))
        L.append("    " + FG_GREY + "[A] add a bot   [Z] remove the last bot"
                 + RESET)
        L.append("")
        # Flow knobs --------------------------------------------------------
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "Flow" + RESET)
        cd = c.get("countdown", 3)
        L.append(row("O", "Countdown", "instant" if cd == 0 else f"{cd}s"))
        L.append(row("I", "Quick start", "on" if c.get("quick_start") else "off"))
        L.append(row("N", "Min players", c.get("min_players", 1)))
        rs = c.get("rematch_secs", 0)
        L.append(row("E", "Auto-rematch", "off" if not rs else f"{rs}s"))
        L.append("")
        if self.compose == "custom":
            L.append("  " + FG_YELLOW + "Type a custom passage (Enter saves, "
                     "Esc cancels; blank clears):" + RESET)
            L.append("  " + FG_YELLOW + "> " + self.compose_draft + REVERSE + " "
                     + RESET)
        else:
            L.append("  " + FG_GREY + "Press a letter to change a setting.   "
                     "Enter/Esc closes." + RESET)
        return L

    def _frame_profile(self, cols):
        L = self._banner()
        L.append("")
        p = self.profile_data or {}
        if not p.get("found"):
            L.append("  " + FG_GREY + "No profile available." + RESET)
            L.append("")
            L.append("  " + FG_GREY + "Any key to go back" + RESET)
            return L
        guest = FG_GREY + "  (guest)" + RESET if p.get("is_guest") else ""
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "PROFILE: " + p.get("name", "")
                 + RESET + guest)
        L.append("")
        s = p.get("stats")
        if not s:
            L.append("  " + FG_GREY + "Guest player - no saved stats." + RESET)
        else:
            L.append(f"    Races {s.get('races_played', 0)}    "
                     f"Wins {s.get('races_won', 0)}    "
                     f"Podiums {s.get('podiums', 0)}")
            L.append(f"    Best WPM {round(s.get('best_wpm', 0))}    "
                     f"Avg WPM {round(s.get('avg_wpm', 0))}    "
                     f"Raw best {round(s.get('raw_wpm_best', 0))}")
            L.append(f"    Best acc {s.get('best_accuracy', 0):.0f}%    "
                     f"Avg acc {s.get('avg_accuracy', 0):.0f}%    "
                     f"Consistency {s.get('consistency', 0):.0f}%")
            L.append(f"    Streak {s.get('cur_streak', 0)} "
                     f"(best {s.get('longest_streak', 0)})    "
                     f"Flawless {s.get('flawless_races', 0)}")
            bymode = s.get("by_mode", {})
            if bymode:
                seg = "   ".join(f"{m}: {d.get('best_wpm', 0):.0f}wpm/{d.get('races', 0)}r"
                                 for m, d in bymode.items())
                L.append("    " + FG_GREY + "By mode: " + RESET + seg)
        L.append("")
        badges = p.get("badges", [])
        L.append("  " + BOLD + f"Badges ({len(badges)})" + RESET)
        if not badges:
            L.append("    " + FG_GREY + "none yet" + RESET)
        for b in badges:
            L.append("    " + FG_YELLOW + "* " + RESET + BOLD + b.get("label", "")
                     + RESET + FG_GREY + " - " + b.get("desc", "") + RESET)
        L.append("")
        L.append("  " + FG_GREY + "Any key to go back" + RESET)
        return L

    def _frame_history(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "YOUR MATCH HISTORY" + RESET)
        L.append("")
        rows = self.history_data or []
        if not rows:
            L.append("  " + FG_GREY + "No races recorded yet." + RESET)
        else:
            L.append("  " + BOLD
                     + f"{'Mode':<9} {'Category':<9} {'WPM':>4} {'Acc':>6} {'Place':>7}"
                     + RESET)
            for r in rows[:14]:
                place = f"{r.get('place', '-')}/{r.get('racers', '-')}"
                cat = r.get("category") or "-"
                col = FG_BRIGHT_GREEN if r.get("won") else FG_WHITE
                L.append("  " + col
                         + f"{r.get('mode', '?'):<9} {cat:<9} {r.get('wpm', 0):>4.0f} "
                         + f"{r.get('acc', 0):>5.0f}% {place:>7}" + RESET)
        L.append("")
        L.append("  " + FG_GREY + "Any key to go back" + RESET)
        return L

    def _frame_help(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "HELP / CONTROLS" + RESET)
        rows = [
            ("Lobby & Results", ""),
            ("R", "ready up / rematch"),
            ("T", "type a chat message"),
            ("1-8", "quick-chat emote (also mid-race as a spectator)"),
            ("TAB", "select a player (for profile / kick)"),
            ("P", "view selected player's profile + badges"),
            ("H", "your own match history"),
            ("L", "leaderboard  ( [ ] cycle metric )"),
            ("O", "cycle your accent color (accounts only)"),
            ("C", "cycle color theme (default/contrast/colorblind/mono)"),
            ("U", "WPM / CPM / both units"),
            ("S", "sound cues on/off"),
            ("?", "this help screen"),
            ("Q / Esc", "quit"),
            ("Host only", ""),
            ("Enter", "start / rematch now"),
            ("M", "setup: mode, length, bots, countdown, quick-start, flow"),
            ("K", "kick the selected player (press twice to confirm)"),
            ("B", "view + manage the ban list"),
            ("While racing", ""),
            ("type", "match the text; Backspace fixes the current spot"),
        ]
        for k, desc in rows:
            if not desc:
                L.append("")
                L.append("  " + BOLD + FG_CYAN + k + RESET)
            else:
                L.append("    " + BOLD + f"{k:<9}" + RESET + " " + FG_GREY + desc + RESET)
        L.append("")
        L.append("  " + FG_GREY + "Any key to go back" + RESET)
        return L

    def _frame_banlist(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "BANNED ACCOUNTS" + RESET
                 + FG_GREY + "   (host only)" + RESET)
        L.append("")
        rows = self.banlist or []
        if not rows:
            L.append("  " + FG_GREY + "No one is banned." + RESET)
        for i, name in enumerate(rows[:9], 1):
            L.append("    " + BOLD + f"[{i}]" + RESET + " " + name)
        L.append("")
        L.append("  " + FG_GREY + "Press a number to un-ban   any other key to "
                 "go back" + RESET)
        return L

    def _color_code(self, name):
        return COLOR_CODES.get(name, FG_WHITE)

    def _emote_bubble(self, p):
        code = p.get("recent_emote")
        if not code or code not in P.EMOTES:
            return ""
        return "  " + FG_YELLOW + "(" + P.EMOTES[code] + ")" + RESET
