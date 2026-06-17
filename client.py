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
from terminal import (
    RESET, BOLD, DIM, REVERSE,
    FG_RED, FG_GREEN, FG_YELLOW, FG_CYAN, FG_GREY, FG_WHITE, FG_MAGENTA,
    FG_BRIGHT_GREEN, FG_BRIGHT_CYAN, FG_BRIGHT_RED, BG_RED,
    KEY_ENTER, KEY_BACKSPACE, KEY_ESC, KEY_CTRL_C,
)


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
    def __init__(self, uri, admin_token=None, host_hint="", prefill_username=""):
        self.uri = uri
        self.admin_token = admin_token
        self.host_hint = host_hint

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

        # view: "login" until authenticated, then game frames or "leaderboard"
        self.view = "login"

        # login form state
        self.login_stage = "choose"     # choose | username | password | submitting
        self.login_action = None        # login | register | guest
        self.field_username = prefill_username or ""
        self.field_password = ""
        self.login_error = None

        # leaderboard overlay
        self.leaderboard_rows = []
        self.leaderboard_metric = "best_wpm"

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
                    self.leaderboard_rows = msg.get("rows", [])
                    if self.authed:
                        self.view = "leaderboard"
                elif mtype == P.S_ERROR:
                    self.error_msg = msg.get("msg")
                    return
        except ConnectionClosed:
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
        if phase == P.PHASE_COUNTDOWN and self.prev_phase != P.PHASE_COUNTDOWN:
            self._reset_typing(text)
        if phase == P.PHASE_RACING and self.prev_phase != P.PHASE_RACING:
            if text:
                self.text = text
            if self.local_start is None:
                self.local_start = time.monotonic()
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
        self.my_id = msg.get("id")
        self.is_admin = bool(msg.get("is_admin"))
        self.is_guest = bool(msg.get("is_guest"))
        self.account = msg.get("account")
        self.username = msg.get("name", self.field_username)
        self.my_stats = msg.get("stats")
        self.authed = True
        self.view = "game"
        self.login_error = None

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

        if self.view == "leaderboard":
            # any key dismisses the leaderboard overlay
            self.view = "game"
            return

        phase = self._phase()
        if phase == P.PHASE_RACING and self._am_racing() and not self.finished_local:
            await self._handle_typing(kind, val)
            return

        if phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            if kind == "char" and val.lower() == "r":
                await self._toggle_ready()
                return
            if kind == "char" and val.lower() == "l":
                await self._send({"type": P.C_LEADERBOARD,
                                  "metric": self.leaderboard_metric})
                return
            if kind == "special" and val == KEY_ENTER and self.is_admin:
                await self._send({"type": P.C_START})
                return

        if kind == "char" and val.lower() == "q":
            self.exit_reason = "Thanks for racing!"
            self.running = False
        elif kind == "special" and val == KEY_ESC:
            self.exit_reason = "Thanks for racing!"
            self.running = False

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
            if self.t_pos >= n:
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
        if self.login_action == "guest":
            await self._send({"type": P.C_GUEST, "name": self.field_username.strip(),
                              "token": token, "version": P.PROTOCOL_VERSION})
        elif self.login_action == "register":
            await self._send({"type": P.C_REGISTER,
                              "username": self.field_username.strip(),
                              "password": self.field_password,
                              "token": token, "version": P.PROTOCOL_VERSION})
        else:  # login
            await self._send({"type": P.C_LOGIN,
                              "username": self.field_username.strip(),
                              "password": self.field_password,
                              "token": token, "version": P.PROTOCOL_VERSION})

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
        if self.view == "leaderboard":
            self._draw(self._frame_leaderboard(cols), cols, rows)
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
        T.write("".join(out))

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

    def _controls(self):
        parts = ["R = ready", "L = leaderboard"]
        if self.is_admin:
            parts.append("Enter = start now")
        parts.append("Q = quit")
        return ["  " + FG_GREY + "    ".join(parts) + RESET]

    def _identity_line(self):
        if self.is_guest:
            return ("  " + FG_GREY + "Playing as guest " + RESET
                    + BOLD + self.username + RESET
                    + FG_GREY + " (stats are not saved)" + RESET)
        s = self.my_stats or {}
        extra = ""
        if s:
            extra = (FG_GREY + f"  -  best {s.get('best_wpm', 0)} wpm, "
                     f"{s.get('races', 0)} races, {s.get('wins', 0)} wins" + RESET)
        return ("  " + FG_GREY + "Logged in as " + RESET
                + BOLD + FG_BRIGHT_CYAN + self.username + RESET + extra)

    # -- lobby --------------------------------------------------------------
    def _frame_lobby(self, cols):
        L = self._banner()
        L.append("")
        L.append(self._identity_line())
        L.append("")
        L.append("  " + BOLD + FG_CYAN + "LOBBY" + RESET
                 + "   tell other players on the LAN to run:")
        L.append("    " + FG_YELLOW + self.host_hint + RESET)
        L.append("")
        L.append("  " + BOLD + "Players:" + RESET)
        for p in self.state["players"]:
            L.append("    " + self._lobby_row(p))
        L.append("")
        L += self._controls()
        return L

    def _lobby_row(self, p):
        you = p["id"] == self.my_id
        name = (BOLD + p["name"] + RESET) if you else p["name"]
        tag = FG_GREY + " (you)" + RESET if you else ""
        host = " " + FG_MAGENTA + "[host]" + RESET if p["is_admin"] else ""
        status = (FG_BRIGHT_GREEN + "READY" + RESET) if p["ready"] \
            else (FG_GREY + "not ready" + RESET)
        stats = p.get("stats")
        if p.get("is_guest"):
            extra = FG_GREY + "  guest" + RESET
        elif stats:
            extra = (FG_GREY + f"  best {stats['best_wpm']}wpm, "
                     f"{stats['races']} races" + RESET)
        else:
            extra = ""
        return f"{name}{tag}{host}  -  {status}{extra}"

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
        L.append(
            "  " + BOLD + "TIME " + RESET + f"{elapsed:5.1f}s"
            + "   " + BOLD + "WPM " + RESET + FG_BRIGHT_CYAN + f"{wpm:3d}" + RESET
            + "   " + BOLD + "ACC " + RESET + f"{acc:5.1f}%"
            + "   " + BOLD + "DONE " + RESET + f"{pct:3d}%"
        )
        L.append("")
        L += self._text_block(cols)
        L.append("")
        L.append("  " + FG_GREY + "Race:" + RESET)
        racers = [p for p in self.state["players"] if p["in_race"]]
        L += self._racetrack(cols, racers)
        L.append("")
        if self.finished_local:
            L.append("  " + FG_BRIGHT_GREEN + "You finished!" + RESET
                     + "  Waiting for the others...   " + FG_GREY + "Q = quit" + RESET)
        else:
            L.append("  " + FG_GREY
                     + "Type the text. Backspace fixes the current spot. Ctrl-C quits."
                     + RESET)
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
            return (1, -p["pos"])

        players = sorted(players, key=sort_key)
        if not players:
            return ["    " + FG_GREY + "(no racers yet)" + RESET]
        namew = min(16, max(6, max(len(p["name"]) for p in players)))
        # Leave room for the prefix, name, spaces and the (longest) finished
        # tail so a full row fits within the terminal width.
        barw = max(8, min(cols - namew - 34, 44))
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
        if p["finished"]:
            tail = (self._place_label(p["place"])
                    + f"  {p['wpm']:3d}wpm {p['acc']:5.1f}%  {p['finish_time']:5.1f}s")
            color = FG_BRIGHT_GREEN
        else:
            tail = f"{int(frac * 100):3d}% {wpm:3d}wpm"
            color = FG_BRIGHT_CYAN if you else FG_WHITE
        return f"{name} {color}{bar}{RESET} {tail}"

    # -- results ------------------------------------------------------------
    def _frame_results(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "RACE RESULTS" + RESET)
        L.append("")
        players = sorted(
            [p for p in self.state["players"] if p["in_race"]],
            key=lambda p: (p["place"] or 999),
        )
        L.append("  " + BOLD
                 + f"{'Rank':<5} {'Player':<18} {'WPM':>4} {'Acc':>6} {'Time':>8}"
                 + RESET)
        for p in players:
            you = p["id"] == self.my_id
            rank = self._place_label(p["place"])
            name = p["name"][:18]
            t = f"{p['finish_time']:.1f}s" if p["finish_time"] is not None else "--"
            row = f"{rank:<5} {name:<18} {p['wpm']:>4} {p['acc']:>5.1f}% {t:>8}"
            if you:
                row = BOLD + FG_BRIGHT_CYAN + row + RESET
            elif p["place"] == 1:
                row = FG_YELLOW + row + RESET
            L.append("  " + row)
        L.append("")
        ready = sum(1 for p in self.state["players"] if p["connected"] and p["ready"])
        total = sum(1 for p in self.state["players"] if p["connected"])
        L.append("  " + FG_GREY + f"Ready for another race: {ready}/{total}" + RESET)
        L.append("")
        L += self._controls()
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
    def _frame_leaderboard(self, cols):
        L = self._banner()
        L.append("")
        L.append("  " + BOLD + FG_BRIGHT_CYAN + "LEADERBOARD" + RESET
                 + FG_GREY + "   ranked by best WPM" + RESET)
        L.append("")
        L.append("  " + BOLD
                 + f"{'#':<3} {'Player':<16} {'Best':>5} {'Avg':>5} "
                 + f"{'Races':>6} {'Wins':>5} {'Acc':>6}" + RESET)
        if not self.leaderboard_rows:
            L.append("  " + FG_GREY + "No ranked players yet - finish a race!" + RESET)
        for i, r in enumerate(self.leaderboard_rows, 1):
            you = self.account and r["username"].lower() == self.account.lower()
            row = (f"{i:<3} {r['username'][:16]:<16} {r['best_wpm']:>5.0f} "
                   f"{r['avg_wpm']:>5.0f} {r['races_played']:>6} "
                   f"{r['races_won']:>5} {r['avg_accuracy']:>5.0f}%")
            if you:
                row = BOLD + FG_BRIGHT_CYAN + row + RESET
            elif i == 1:
                row = FG_YELLOW + row + RESET
            L.append("  " + row)
        L.append("")
        L.append("  " + FG_GREY + "Press any key to go back" + RESET)
        return L
