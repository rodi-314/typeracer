"""Authoritative TypeRacer game server.

One event loop, one shared :class:`GameServer`. Each websocket connection runs a
:meth:`GameServer.handler` coroutine; all of them mutate the same in-memory
state. Because asyncio is single-threaded and the mutations between ``await``
points are synchronous, no locks are required.

The server owns the clock (``loop.time()``), the phase, finish ordering, and now
also race configuration (mode/length/category), anti-cheat clamping, chat, and
achievements -- so clients can neither cheat the standings nor each other.

Every new feature degrades to CLASSIC behaviour when ``config['mode']`` is
classic, so the original game is preserved byte-for-byte.
"""

import asyncio
import random
import secrets
from collections import deque

from websockets.exceptions import ConnectionClosed

import protocol as P
import accounts
import achievements
import modes
import texts


COUNTDOWN_SECONDS = 3
MAX_RACE_SECONDS = 300        # hard safety cap on any race
BROADCAST_MIN_INTERVAL = 0.03  # coalesce bursts of state changes (~33 Hz)
MAX_PLAYERS = 16

# Anti-cheat: cap how fast reported progress may advance from race start.
MAX_CPS = 25.0                # ~300 WPM ceiling
ANTICHEAT_GRACE = 12          # chars of head-start before the rate cap bites
RATE_WINDOW = 1.0
RATE_MAX_MSGS = 60            # progress messages/sec before excess is dropped

# Chat / presence.
CHAT_MAXLEN = 30
CHAT_RATE = 0.5
CHAT_TEXT_MAX = 200
IDLE_SECONDS = 30
TIMED_REFILL_MARGIN = 60      # grow a timed passage when a racer nears its end
MAX_TIMED_TEXT = 8000        # hard ceiling on a timed passage (defense-in-depth)

CONFIG_OPTIONS = {
    "modes": list(modes.MODES),
    "lengths": list(modes.LENGTH_NAMES),
    "categories": ["any"] + list(texts.CATEGORIES),
    "difficulties": [None, 1, 2, 3],
    "time_limits": list(modes.TIME_LIMITS),
    "lives": list(modes.LIVES_OPTIONS),
}


def _to_int(value, default=0):
    """Coerce an untrusted JSON value to int, never raising on junk input."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class Player:
    __slots__ = (
        "id", "name", "ws", "is_admin", "account", "is_guest", "session",
        "ready", "connected",
        "in_race", "pos", "errors", "keystrokes",
        "finished", "finish_time", "place",
        "lives", "eliminated", "last_pos", "flagged",
        "msg_count", "msg_window", "last_chat", "last_seen",
    )

    def __init__(self, pid, name, ws, is_admin, account=None, is_guest=True):
        self.id = pid
        self.name = name
        self.ws = ws
        self.is_admin = is_admin
        self.account = account
        self.is_guest = is_guest
        self.session = 1
        self.ready = False
        self.connected = True
        # per-race fields
        self.in_race = False
        self.pos = 0
        self.errors = 0
        self.keystrokes = 0
        self.finished = False
        self.finish_time = None
        self.place = None
        self.lives = 0
        self.eliminated = False
        self.last_pos = 0
        self.flagged = False
        # anti-cheat / presence counters
        self.msg_count = 0
        self.msg_window = 0.0
        self.last_chat = 0.0
        self.last_seen = 0.0

    def reset_race(self, in_race, lives=0):
        self.in_race = in_race
        self.pos = 0
        self.errors = 0
        self.keystrokes = 0
        self.finished = False
        self.finish_time = None
        self.place = None
        self.lives = lives
        self.eliminated = False
        self.last_pos = 0
        self.flagged = False
        self.msg_count = 0
        self.msg_window = 0.0


class GameServer:
    def __init__(self, game_name="TypeRacer", admin_token=None, seed=None, store=None):
        self.game_name = game_name
        self.admin_token = admin_token or secrets.token_hex(8)
        self.rng = random.Random(seed)
        self.store = store         # AccountStore or None (None => guests only)

        self.players = {}          # id -> Player
        self._next_id = 1
        self.phase = P.PHASE_LOBBY
        self.text = ""
        self.text_id = None
        self.text_meta = {}
        self.countdown = 0
        self.race_start = None     # loop.time() at GO
        self.race_deadline = None  # loop.time() deadline (TIMED mode)
        self.finish_order = []     # player ids in finishing order
        self.elim_order = []       # player ids in elimination order (SURVIVAL)

        self.config = modes.default_config()
        self.chat = deque(maxlen=CHAT_MAXLEN)
        self._chat_seq = 0
        self._pending_announcements = []
        self.kicked_accounts = set()

        self.loop = None
        self._dirty = asyncio.Event()
        self._countdown_task = None
        self._watchdog_task = None
        self._broadcaster_task = None

    # ------------------------------------------------------------------ info
    def discovery_info(self):
        return {
            "app": "typeracer",
            "name": self.game_name,
            "version": P.PROTOCOL_VERSION,
            "players": sum(1 for p in self.players.values() if p.connected),
            "phase": self.phase,
            "mode": self.config["mode"],
        }

    # ----------------------------------------------------------- connection
    async def handler(self, websocket):
        self._ensure_started()
        player = session = None
        try:
            result = await self._authenticate(websocket)
            if result is None:
                return
            player, session = result
            await websocket.send(P.encode(self._auth_ok(player)))
            self._mark_dirty()
            self._maybe_autostart()
            async for raw in websocket:
                await self._dispatch(player, raw)
        except ConnectionClosed:
            pass
        finally:
            if player is not None:
                self._unregister(player, session)

    def _auth_ok(self, player):
        return {
            "type": P.S_AUTH_OK,
            "id": player.id,
            "name": player.name,
            "account": player.account,
            "is_admin": player.is_admin,
            "is_guest": player.is_guest,
            "stats": self.store.public_stats(player.account)
                     if (player.account and self.store) else None,
            "version": P.PROTOCOL_VERSION,
        }

    async def _authenticate(self, websocket):
        """Loop until the client authenticates (allowing retries) or disconnects.

        Returns (player, session) on success. Failures get S_AUTH_FAIL and the
        loop continues so the login screen can retry on the same connection.
        """
        while True:
            try:
                raw = await websocket.recv()
            except ConnectionClosed:
                return None
            try:
                msg = P.decode(raw)
            except Exception:
                msg = None
            if not isinstance(msg, dict):
                await self._send_auth_fail(websocket, "malformed message")
                continue

            mtype = msg.get("type")
            if mtype not in (P.C_REGISTER, P.C_LOGIN, P.C_GUEST):
                await self._send_auth_fail(websocket, "please log in, register, or play as guest")
                continue
            version = msg.get("version")
            if version is not None and version != P.PROTOCOL_VERSION:
                await self._send_auth_fail(
                    websocket,
                    f"server runs protocol v{P.PROTOCOL_VERSION}; update your client")
                continue
            if len([p for p in self.players.values() if p.connected]) >= MAX_PLAYERS:
                await self._send_auth_fail(websocket, "the game is full")
                continue

            is_admin = bool(self.admin_token) and msg.get("token") == self.admin_token
            name, account, is_guest, err = self._resolve_auth(msg, mtype)
            if err:
                await self._send_auth_fail(websocket, err)
                continue
            return self._admit(name, websocket, is_admin, account, is_guest)

    def _resolve_auth(self, msg, mtype):
        """Return (display_name, account_or_None, is_guest, error_or_None)."""
        if mtype == P.C_GUEST:
            # Strip ESC/control chars so a guest name can't inject ANSI into
            # every other player's terminal (account usernames are regex-safe).
            raw = "".join(ch for ch in str(msg.get("name") or "") if " " <= ch <= "~")
            name = raw.strip()[:16] or "Guest"
            return name, None, True, None

        if self.store is None:
            return None, None, None, "accounts are disabled on this server"
        username = str(msg.get("username") or "").strip()
        password = str(msg.get("password") or "")

        if mtype == P.C_REGISTER:
            record, err = self.store.create(username, password)
        else:
            record, err = self.store.authenticate(username, password)
        if err:
            return None, None, None, err
        account = record["username"]
        if account.lower() in self.kicked_accounts:
            return None, None, None, "you were removed by the host"
        return account, account, False, None

    def _admit(self, name, websocket, is_admin, account, is_guest):
        """Create a Player, or take over an existing account's session."""
        if account:
            for p in self.players.values():
                if p.account and p.account.lower() == account.lower():
                    # Reconnection / takeover: reuse the Player (keep id + stats).
                    if p.connected and p.ws is not websocket:
                        self.loop.create_task(
                            self._close_ws(p.ws, P.CLOSE_REPLACED, "replaced"))
                    p.ws = websocket
                    p.connected = True
                    p.is_admin = is_admin
                    p.session += 1
                    if self.phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
                        p.ready = False
                    self._mark_dirty()
                    return p, p.session

        player = Player(self._next_id, self._unique_name(name), websocket,
                        is_admin, account=account, is_guest=is_guest)
        self._next_id += 1
        self.players[player.id] = player
        self._system_chat(f"{player.name} joined")
        self._mark_dirty()
        return player, player.session

    def _unique_name(self, name):
        existing = {p.name for p in self.players.values() if p.connected}
        if name not in existing:
            return name
        i = 2
        while f"{name} ({i})" in existing:
            i += 1
        return f"{name} ({i})"

    def _unregister(self, player, session):
        # A newer session (reconnection) owns this account; the old handler's
        # finally must not evict the live player.
        if self.players.get(player.id) is not player or player.session != session:
            return
        player.connected = False
        self.players.pop(player.id, None)
        self.finish_order = [pid for pid in self.finish_order if pid in self.players]
        for i, pid in enumerate(self.finish_order, 1):
            self.players[pid].place = i
        self._system_chat(f"{player.name} left")
        self._mark_dirty()
        if self.phase == P.PHASE_RACING:
            self._maybe_end_race()
        else:
            self._maybe_autostart()

    # ------------------------------------------------------------- dispatch
    async def _dispatch(self, player, raw):
        try:
            msg = P.decode(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        player.last_seen = self.loop.time()
        mtype = msg.get("type")
        if mtype == P.C_READY:
            self._handle_ready(player, bool(msg.get("ready")))
        elif mtype == P.C_PROGRESS:
            self._handle_progress(player, msg)
        elif mtype == P.C_START:
            if player.is_admin:
                self._force_start()
        elif mtype == P.C_LOBBY:
            if player.is_admin:
                self._to_lobby()
        elif mtype == P.C_CONFIG:
            err = self._handle_config(player, msg)
            if err:
                await self._send_error(player.ws, err)
        elif mtype == P.C_CHAT:
            self._handle_chat(player, msg)
        elif mtype == P.C_PROFILE:
            await self._send_profile(player, msg.get("target_id"))
        elif mtype == P.C_HISTORY:
            await self._send_history(player)
        elif mtype == P.C_KICK:
            if player.is_admin:
                self._handle_kick(player, msg.get("target_id"))
        elif mtype == P.C_LEADERBOARD:
            await self._send_leaderboard(player, msg.get("metric"),
                                         msg.get("mode"), msg.get("category"))
        elif mtype == P.C_PING:
            await self._safe_send(player.ws, {"type": P.S_PONG})

    def _handle_ready(self, player, ready):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        player.ready = ready
        self._mark_dirty()
        self._maybe_autostart()

    def _handle_progress(self, player, msg):
        if (self.phase != P.PHASE_RACING or not player.in_race
                or player.finished or player.eliminated):
            return
        now = self.loop.time()
        # Rate limit: drop (don't disconnect) bursts beyond the budget.
        if now - player.msg_window >= RATE_WINDOW:
            player.msg_window = now
            player.msg_count = 0
        player.msg_count += 1
        if player.msg_count > RATE_MAX_MSGS:
            return

        raw_pos = max(0, _to_int(msg.get("pos", 0)))
        errors = max(0, _to_int(msg.get("errors", 0)))
        keystrokes = max(0, _to_int(msg.get("keystrokes", 0)))
        mode = self.config["mode"]

        # Plausibility clamp FIRST: pos may not exceed an absolute rate cap from
        # race start. Everything downstream (incl. the TIMED refill trigger) must
        # use the clamped value, never the untrusted raw_pos.
        n = len(self.text)
        elapsed = max(0.0, now - self.race_start) if self.race_start else 0.0
        allowed = int(elapsed * MAX_CPS) + ANTICHEAT_GRACE
        pos = max(0, min(raw_pos, n, allowed))
        if raw_pos > allowed:
            player.flagged = True
        # Errors/keystrokes are bounded by the same rate cap so a hacked client
        # can't inflate raw WPM or lifetime error counts.
        errors = min(errors, allowed)
        keystrokes = max(pos, min(keystrokes, allowed))

        # TIMED mode grows the passage as the racer nears its end -- keyed on the
        # CLAMPED pos (so it tracks real typing rate) and hard-capped in length.
        if (mode == modes.MODE_TIMED and pos >= n - TIMED_REFILL_MARGIN
                and len(self.text) < MAX_TIMED_TEXT):
            self._extend_timed_text()
            n = len(self.text)

        if mode == modes.MODE_SURVIVAL:
            new_errors = errors - player.errors
            if new_errors > 0 and player.lives > 0:
                player.lives -= new_errors
                if player.lives <= 0:
                    player.lives = 0
                    self._eliminate(player)

        player.pos = pos
        player.errors = errors
        player.keystrokes = keystrokes
        player.last_pos = pos

        if not player.eliminated and self._is_finish_event(player, n):
            self._finish_player(player)
        self._mark_dirty()
        self._maybe_end_race()

    # ---------------------------------------------------------- race phases
    def _maybe_autostart(self):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        connected = [p for p in self.players.values() if p.connected]
        if connected and all(p.ready for p in connected):
            self._begin_countdown(connected)

    def _force_start(self):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        connected = [p for p in self.players.values() if p.connected]
        if connected:
            self._begin_countdown(connected)

    def _begin_countdown(self, racers):
        if self.phase in (P.PHASE_COUNTDOWN, P.PHASE_RACING):
            return
        if self._countdown_task is not None and not self._countdown_task.done():
            return
        racer_ids = {p.id for p in racers}
        self._countdown_task = self.loop.create_task(self._run_countdown(racer_ids))

    async def _run_countdown(self, racer_ids):
        try:
            self.phase = P.PHASE_COUNTDOWN
            self._resolve_text()
            self.countdown = COUNTDOWN_SECONDS
            self._mark_dirty()
            for n in range(COUNTDOWN_SECONDS, 0, -1):
                self.countdown = n
                self._mark_dirty()
                await asyncio.sleep(1.0)
            self._start_race(racer_ids)
        except asyncio.CancelledError:
            pass

    def _resolve_text(self):
        cfg = self.config
        if cfg.get("custom_text"):
            self.text = cfg["custom_text"]
            self.text_id = None
            self.text_meta = {"category": "custom", "difficulty": None}
            return
        rec = texts.select(self.rng, category=cfg["category"],
                           difficulty=cfg["difficulty"], length=cfg["length"],
                           avoid_id=self.text_id)
        self.text = rec["text"]
        self.text_id = rec["id"]
        self.text_meta = {"category": rec["category"], "difficulty": rec["difficulty"]}

    def _extend_timed_text(self):
        if len(self.text) >= MAX_TIMED_TEXT:
            return
        sep = " " if self.text and not self.text.endswith(" ") else ""
        rec = texts.select(self.rng, category=self.config.get("category"),
                           avoid_id=self.text_id)
        self.text = self.text + sep + rec["text"]
        self.text_id = rec["id"]

    def _start_race(self, racer_ids):
        self.phase = P.PHASE_RACING
        self.countdown = 0
        self.finish_order = []
        self.elim_order = []
        self.race_start = self.loop.time()
        mode = self.config["mode"]
        self.race_deadline = (self.race_start + self.config["time_limit"]
                              if mode == modes.MODE_TIMED else None)
        lives = self.config["lives"] if mode == modes.MODE_SURVIVAL else 0
        for p in self.players.values():
            p.reset_race(in_race=(p.id in racer_ids and p.connected), lives=lives)
            p.ready = False
        self._watchdog_task = self.loop.create_task(self._race_watchdog())
        self._mark_dirty()
        # If everyone disconnected during the countdown, resolve immediately
        # instead of hanging in RACING until the watchdog fires.
        self._maybe_end_race()

    def _is_finish_event(self, player, n):
        if self.config["mode"] == modes.MODE_TIMED:
            return False
        return player.pos >= n

    def _finish_player(self, player):
        if player.finished:
            return
        player.finished = True
        player.pos = len(self.text)
        player.finish_time = max(0.0, self.loop.time() - self.race_start)
        self.finish_order.append(player.id)
        player.place = len(self.finish_order)

    def _eliminate(self, player):
        if player.eliminated:
            return
        player.eliminated = True
        if player.id not in self.elim_order:
            self.elim_order.append(player.id)
        self._system_chat(f"{player.name} was eliminated")

    def _maybe_end_race(self):
        if self.phase != P.PHASE_RACING:
            return
        racers = [p for p in self.players.values() if p.in_race and p.connected]
        if not racers:
            self._end_race()
            return
        mode = self.config["mode"]
        if mode == modes.MODE_TIMED:
            return  # only the deadline (watchdog) ends a timed race
        active = [p for p in racers if not p.finished and not p.eliminated]
        if not active:
            self._end_race()
        elif mode == modes.MODE_SURVIVAL and len(racers) > 1 and len(active) <= 1:
            self._end_race()  # last typist standing

    async def _race_watchdog(self):
        try:
            if self.config["mode"] == modes.MODE_TIMED and self.race_deadline:
                delay = min(max(0.0, self.race_deadline - self.loop.time()),
                            MAX_RACE_SECONDS)
            else:
                delay = MAX_RACE_SECONDS
            await asyncio.sleep(delay)
            if self.phase == P.PHASE_RACING:
                self._end_race()
        except asyncio.CancelledError:
            pass

    def _end_race(self):
        if self.phase != P.PHASE_RACING:
            return
        self._cancel_watchdog()
        racers = [p for p in self.players.values() if p.in_race and p.connected]
        placed = set(self.finish_order)
        survivors = [p for p in racers
                     if not p.finished and not p.eliminated and p.id not in placed]
        survivors.sort(key=lambda q: (-q.pos, q.errors))
        eliminated = [p for p in racers if p.eliminated and p.id not in placed]
        # earliest eliminated ranks lowest -> sort by elim order descending
        eliminated.sort(
            key=lambda q: self.elim_order.index(q.id) if q.id in self.elim_order else -1,
            reverse=True)
        order = list(self.finish_order) + [p.id for p in survivors] + [p.id for p in eliminated]
        self.finish_order = order
        for i, pid in enumerate(order, 1):
            if pid in self.players:
                self.players[pid].place = i
        self._record_results()
        self.phase = P.PHASE_RESULTS
        for p in self.players.values():
            p.ready = False
        self._mark_dirty()

    def _record_results(self):
        if self.store is None:
            return
        now = self.loop.time()
        elapsed = max(0.0, now - self.race_start) if self.race_start else 0.0
        mode = self.config["mode"]
        category = self.text_meta.get("category")
        if category == "custom":
            category = None
        racers = sum(1 for q in self.players.values() if q.in_race and q.connected)
        for p in self.players.values():
            if not (p.in_race and p.connected and p.account):
                continue
            if mode == modes.MODE_TIMED:
                seconds = float(self.config["time_limit"])
            elif p.finished and p.finish_time:
                seconds = p.finish_time
            else:
                seconds = elapsed
            minutes = seconds / 60.0
            net = round((p.pos / 5.0) / minutes) if minutes > 0 else 0
            raw = round(((p.pos + p.errors) / 5.0) / minutes) if minutes > 0 else 0
            typed = p.pos + p.errors
            acc = round(100.0 * p.pos / typed, 1) if typed else 100.0
            flawless = (p.errors == 0 and p.pos > 0
                        and (p.finished or mode == modes.MODE_TIMED))
            newly = self.store.record_race(
                p.account, net_wpm=float(net), raw_wpm=float(raw), accuracy=acc,
                seconds=seconds, chars=p.pos, keystrokes=p.keystrokes,
                errors=p.errors, won=(p.place == 1), place=p.place, racers=racers,
                mode=mode, category=category, flawless=flawless)
            for aid in newly:
                self._pending_announcements.append(
                    {"name": p.name, "badge": achievements.label_for(aid)})

    def _to_lobby(self):
        self._cancel_countdown()
        self._cancel_watchdog()
        self.phase = P.PHASE_LOBBY
        self.text = ""
        self.text_meta = {}
        self.countdown = 0
        self.race_start = None
        self.race_deadline = None
        self.finish_order = []
        self.elim_order = []
        for p in self.players.values():
            p.reset_race(in_race=False)
            p.ready = False
        self._mark_dirty()

    def _cancel_countdown(self):
        if self._countdown_task is not None:
            self._countdown_task.cancel()
            self._countdown_task = None

    def _cancel_watchdog(self):
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    # --------------------------------------------------------------- config
    def _handle_config(self, player, msg):
        if not player.is_admin:
            return None
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return None
        cfg = self.config
        if msg.get("mode") in modes.MODES:
            cfg["mode"] = msg["mode"]
        if msg.get("length") in modes.LENGTHS:
            cfg["length"] = msg["length"]
        category = msg.get("category")
        if category == "any" or category in texts.CATEGORIES:
            cfg["category"] = category
        if "difficulty" in msg:
            d = msg.get("difficulty")
            cfg["difficulty"] = d if d in (1, 2, 3) else None
        if "time_limit" in msg:
            tl = _to_int(msg.get("time_limit"))
            if tl in modes.TIME_LIMITS:
                cfg["time_limit"] = tl
        if "lives" in msg:
            lv = _to_int(msg.get("lives"))
            if lv in modes.LIVES_OPTIONS:
                cfg["lives"] = lv
        err = None
        if "custom_text" in msg:
            raw = msg.get("custom_text")
            if not raw:
                cfg["custom_text"] = None
            else:
                clean, cerr = texts.sanitize_custom(raw)
                if cerr:
                    err = cerr
                else:
                    cfg["custom_text"] = clean
        self._mark_dirty()
        return err

    # ----------------------------------------------------------------- chat
    def _handle_chat(self, player, msg):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        now = self.loop.time()
        if now - player.last_chat < CHAT_RATE:
            return
        text = "".join(ch for ch in str(msg.get("text", "")) if " " <= ch <= "~")
        text = text[:CHAT_TEXT_MAX].strip()
        if not text:
            return
        player.last_chat = now
        self._push_chat(player.id, player.name, text, "user")

    def _push_chat(self, pid, name, text, kind):
        self._chat_seq += 1
        self.chat.append({"seq": self._chat_seq, "id": pid, "name": name,
                          "text": text, "kind": kind})
        self._mark_dirty()

    def _system_chat(self, text):
        self._push_chat(0, "", text, "system")

    # ------------------------------------------------------- profile / kick
    async def _send_profile(self, player, target_id):
        target = player if target_id is None else self.players.get(_to_int(target_id))
        payload = {"type": P.S_PROFILE, "found": False}
        if target is not None:
            if target.account and self.store:
                prof = self.store.profile_payload(target.account)
                if prof:
                    payload = {"type": P.S_PROFILE, "found": True,
                               "name": target.name, "is_guest": False,
                               "stats": prof["stats"], "badges": prof["badges"],
                               "recent": prof["recent"]}
            else:
                payload = {"type": P.S_PROFILE, "found": True, "name": target.name,
                           "is_guest": True, "stats": None, "badges": [], "recent": []}
        await self._safe_send(player.ws, payload)

    async def _send_history(self, player):
        rows = (self.store.history_rows(player.account)
                if (player.account and self.store) else [])
        await self._safe_send(player.ws, {"type": P.S_HISTORY, "rows": rows})

    def _handle_kick(self, player, target_id):
        target = self.players.get(_to_int(target_id))
        if not target or target is player or target.is_admin:
            return
        if target.account:
            self.kicked_accounts.add(target.account.lower())
        self._system_chat(f"{target.name} was removed by the host")
        self.loop.create_task(self._kick_ws(target))

    async def _kick_ws(self, target):
        await self._safe_send(target.ws, {"type": P.S_ERROR,
                                          "msg": "you were removed by the host"})
        await self._close_ws(target.ws, P.CLOSE_KICKED, "kicked")

    async def _send_leaderboard(self, player, metric, mode, category):
        metric = metric if metric in accounts.LEADERBOARD_METRICS else "best_wpm"
        mode = mode if mode in modes.MODES else None
        category = category if category in texts.CATEGORIES else None
        rows = (self.store.leaderboard(metric, mode=mode, category=category)
                if self.store else [])
        await self._safe_send(player.ws, {
            "type": P.S_LEADERBOARD, "metric": metric, "mode": mode,
            "category": category, "rows": rows})

    # --------------------------------------------------------- broadcasting
    def _ensure_started(self):
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        if self._broadcaster_task is None:
            self._broadcaster_task = self.loop.create_task(self._broadcaster())

    def _mark_dirty(self):
        self._dirty.set()

    async def _broadcaster(self):
        try:
            while True:
                await self._dirty.wait()
                self._dirty.clear()
                await self._broadcast_now()
                await asyncio.sleep(BROADCAST_MIN_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _broadcast_now(self):
        snapshot = P.encode(self.snapshot())
        self._pending_announcements = []   # announced once, then cleared
        targets = [p for p in self.players.values() if p.connected]
        if not targets:
            return
        await asyncio.gather(*(p.ws.send(snapshot) for p in targets),
                             return_exceptions=True)

    def snapshot(self):
        now = self.loop.time()
        elapsed = 0.0
        time_left = None
        if self.phase == P.PHASE_RACING and self.race_start is not None:
            elapsed = max(0.0, now - self.race_start)
            if self.config["mode"] == modes.MODE_TIMED and self.race_deadline:
                time_left = max(0.0, self.race_deadline - now)
        include_text = self.phase in (
            P.PHASE_COUNTDOWN, P.PHASE_RACING, P.PHASE_RESULTS)
        players = [self._player_view(p, elapsed, now) for p in self._ordered_players()]
        snap = {
            "type": P.S_STATE,
            "phase": self.phase,
            "countdown": self.countdown,
            "elapsed": round(elapsed, 2),
            "text": self.text if include_text else "",
            "text_len": len(self.text),
            "players": players,
            "mode": self.config["mode"],
            "config": self._config_view(),
        }
        if time_left is not None:
            snap["time_left"] = round(time_left, 1)
        if include_text:
            snap["text_category"] = self.text_meta.get("category")
            snap["text_difficulty"] = self.text_meta.get("difficulty")
        if self.phase in (P.PHASE_LOBBY, P.PHASE_COUNTDOWN, P.PHASE_RESULTS):
            snap["config_options"] = CONFIG_OPTIONS
            snap["chat"] = list(self.chat)
            if self._pending_announcements:
                snap["announcements"] = list(self._pending_announcements)
        return snap

    def _config_view(self):
        c = self.config
        return {"mode": c["mode"], "length": c["length"], "category": c["category"],
                "difficulty": c["difficulty"], "time_limit": c["time_limit"],
                "lives": c["lives"], "has_custom": bool(c["custom_text"])}

    def _ordered_players(self):
        return [self.players[pid] for pid in sorted(self.players)]

    def _player_view(self, p, elapsed, now):
        wpm, acc = self._stats(p, elapsed)
        survival = self.config["mode"] == modes.MODE_SURVIVAL
        idle = (self.phase in (P.PHASE_LOBBY, P.PHASE_RESULTS)
                and p.last_seen > 0 and (now - p.last_seen) > IDLE_SECONDS)
        return {
            "id": p.id,
            "name": p.name,
            "is_admin": p.is_admin,
            "is_guest": p.is_guest,
            "ready": p.ready,
            "connected": p.connected,
            "in_race": p.in_race,
            "pos": p.pos,
            "errors": p.errors,
            "finished": p.finished,
            "place": p.place,
            "finish_time": round(p.finish_time, 2) if p.finish_time is not None else None,
            "wpm": wpm,
            "acc": acc,
            "lives": p.lives if survival else None,
            "eliminated": p.eliminated,
            "flagged": p.flagged,
            "idle": idle,
            "stats": self.store.public_stats(p.account)
                     if (p.account and self.store) else None,
        }

    def _stats(self, p, elapsed):
        typed = p.pos + p.errors
        acc = round(100.0 * p.pos / typed, 1) if typed else 100.0
        if p.finished and p.finish_time:
            minutes = p.finish_time / 60.0
        else:
            minutes = elapsed / 60.0
        wpm = round((p.pos / 5.0) / minutes) if minutes > 0 else 0
        return wpm, acc

    # ------------------------------------------------------------ utilities
    async def _safe_send(self, ws, obj):
        try:
            await ws.send(P.encode(obj))
        except ConnectionClosed:
            pass

    async def _send_error(self, websocket, message):
        await self._safe_send(websocket, {"type": P.S_ERROR, "msg": message})

    async def _send_auth_fail(self, websocket, message):
        await self._safe_send(websocket, {"type": P.S_AUTH_FAIL, "msg": message})

    async def _close_ws(self, ws, code, reason):
        try:
            await ws.close(code, reason)
        except Exception:
            pass

    def shutdown(self):
        self._cancel_countdown()
        self._cancel_watchdog()
        if self._broadcaster_task is not None:
            self._broadcaster_task.cancel()
            self._broadcaster_task = None
