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
import hmac
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
WPM_SAMPLE_INTERVAL = 0.5    # min seconds between intra-race WPM samples
WPM_SAMPLE_MAX = 40          # cap on samples per racer (bounds the splits array)
EMOTE_RATE = 1.5              # min seconds between a player's emotes
EMOTE_DECAY = 2.5            # how long a fired emote shows on the racetrack
PHOTO_FINISH_GAP = 0.75      # top-two finish gap that counts as a photo finish
RECONNECT_GRACE = 8.0        # seconds an account keeps its slot after a drop
SESSION_SCORES_MAX = 200     # hard cap on the session scoreboard (anti-churn)
TIMED_REFILL_MARGIN = 60      # grow a timed passage when a racer nears its end
MAX_TIMED_TEXT = 8000        # hard ceiling on a timed passage (defense-in-depth)

CONFIG_OPTIONS = {
    "modes": list(modes.MODES),
    "lengths": list(modes.LENGTH_NAMES),
    "categories": ["any"] + list(texts.CATEGORIES),
    "difficulties": [None, 1, 2, 3],
    "time_limits": list(modes.TIME_LIMITS),
    "lives": list(modes.LIVES_OPTIONS),
    "bot_difficulties": list(modes.BOT_DIFFICULTY_ORDER),
    "countdowns": list(modes.COUNTDOWN_OPTIONS),
    "rematch_secs": list(modes.REMATCH_OPTIONS),
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
        "is_bot", "difficulty", "ready", "connected",
        "in_race", "pos", "errors", "keystrokes",
        "finished", "finish_time", "place",
        "lives", "eliminated", "last_pos", "flagged",
        "msg_count", "msg_window", "last_chat", "last_seen",
        "recent_emote", "recent_emote_at", "disconnect_at",
        "wpm_samples", "last_sample_at",
    )

    def __init__(self, pid, name, ws, is_admin, account=None, is_guest=True,
                 is_bot=False, difficulty=None):
        self.id = pid
        self.name = name
        self.ws = ws
        self.is_admin = is_admin
        self.account = account
        self.is_guest = is_guest
        self.is_bot = is_bot
        self.difficulty = difficulty
        self.session = 1
        self.ready = bool(is_bot)        # bots are always ready
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
        # transient social / reliability state
        self.recent_emote = None
        self.recent_emote_at = 0.0
        self.disconnect_at = None
        # per-race WPM timeline (sampled during the race, shown on results)
        self.wpm_samples = []
        self.last_sample_at = 0.0

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
        self.disconnect_at = None
        self.wpm_samples = []
        self.last_sample_at = 0.0


class GameServer:
    def __init__(self, game_name="TypeRacer", admin_token=None, seed=None,
                 store=None, room_password=None, max_players=MAX_PLAYERS,
                 host_store=None):
        self.game_name = game_name
        self.admin_token = admin_token or secrets.token_hex(8)
        self.rng = random.Random(seed)
        self.store = store         # AccountStore or None (None => guests only)
        self.room_password = room_password or None
        self.max_players = max(1, int(max_players))
        self.host_store = host_store   # optional persistence for config + bans

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

        # AI bots (virtual racers with no websocket)
        self._bot_tasks = []
        self._bot_seq = 0

        # Session scoreboard ("who won the night"), keyed by stable identity so
        # points survive disconnects/reconnects. Plus the one-shot win banner.
        self.session_scores = {}   # key -> {name, points, races, wins, best_place}
        self.session_race_no = 0
        self._celebration = None

        self.loop = None
        self._dirty = asyncio.Event()
        self._countdown_task = None
        self._watchdog_task = None
        self._broadcaster_task = None
        self._rematch_task = None

    # ------------------------------------------------------------------ info
    def discovery_info(self):
        return {
            "app": "typeracer",
            "name": self.game_name,
            "version": P.PROTOCOL_VERSION,
            "players": sum(1 for p in self.players.values()
                           if p.connected and not p.is_bot),
            "phase": self.phase,
            "mode": self.config["mode"],
            "locked": self.room_password is not None,
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
        pub = (self.store.public_stats(player.account)
               if (player.account and self.store) else None)
        return {
            "type": P.S_AUTH_OK,
            "id": player.id,
            "name": player.name,
            "account": player.account,
            "is_admin": player.is_admin,
            "is_guest": player.is_guest,
            "stats": pub,
            "level": (pub or {}).get("level"),
            "tier": (pub or {}).get("tier"),
            "color": (pub or {}).get("color"),
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
            if self.room_password is not None:
                supplied = str(msg.get("room_password") or "")
                if not hmac.compare_digest(supplied, self.room_password):
                    await self._send_auth_fail(
                        websocket, "this game requires a room password")
                    continue
            if len([p for p in self.players.values()
                    if p.connected and not p.is_bot]) >= self.max_players:
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
                    resumed = p.disconnect_at is not None
                    p.ws = websocket
                    p.connected = True
                    p.disconnect_at = None        # cancels the grace eviction
                    p.is_admin = is_admin
                    p.session += 1
                    if self.phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
                        p.ready = False
                    if resumed:
                        self._system_chat(f"{p.name} reconnected")
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
        # Mid-race grace: an accounted racer keeps its slot for a few seconds so a
        # flaky LAN connection that returns resumes its place, standings and pos.
        if (self.phase == P.PHASE_RACING and player.in_race and player.account
                and not player.finished and not player.eliminated):
            player.connected = False
            player.disconnect_at = self.loop.time()
            self._system_chat(f"{player.name} dropped - holding their spot")
            self._mark_dirty()
            self.loop.create_task(self._grace_evict(player, session))
            return
        self._evict(player)
        if self.phase == P.PHASE_RACING:
            self._maybe_end_race()
        else:
            self._maybe_autostart()

    def _evict(self, player):
        player.connected = False
        self.players.pop(player.id, None)
        # A departed guest can never reconnect to the same identity, so drop its
        # session-score entry to bound growth under guest churn. Accounts keep
        # theirs (they can reconnect and resume their standing).
        if not player.account and not player.is_bot:
            self.session_scores.pop(self._session_key(player), None)
        self.finish_order = [pid for pid in self.finish_order if pid in self.players]
        for i, pid in enumerate(self.finish_order, 1):
            self.players[pid].place = i
        self._system_chat(f"{player.name} left")
        self._mark_dirty()

    async def _grace_evict(self, player, session):
        try:
            await asyncio.sleep(RECONNECT_GRACE)
        except asyncio.CancelledError:
            return
        # Still the same player, still gone (no reconnection bumped the session)?
        if (self.players.get(player.id) is player and player.session == session
                and not player.connected):
            self._evict(player)
            if self.phase == P.PHASE_RACING:
                self._maybe_end_race()
            else:
                self._maybe_autostart()

    def _in_grace(self, p):
        return (p.in_race and not p.connected and p.disconnect_at is not None
                and not p.finished and not p.eliminated)

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
        elif mtype == P.C_RERACE:
            if player.is_admin and self.phase == P.PHASE_RESULTS:
                self._cancel_rematch()
                self._to_lobby()
                self._force_start()
        elif mtype == P.C_SESSION_RESET:
            if player.is_admin:
                self._reset_session()
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
        elif mtype == P.C_ADD_BOT:
            if player.is_admin:
                err = self._add_bot(msg.get("difficulty"))
                if err:
                    await self._send_error(player.ws, err)
        elif mtype == P.C_REMOVE_BOT:
            if player.is_admin:
                self._remove_bot(msg.get("target_id"))
        elif mtype == P.C_LEADERBOARD:
            await self._send_leaderboard(player, msg.get("metric"),
                                         msg.get("mode"), msg.get("category"))
        elif mtype == P.C_EMOTE:
            self._handle_emote(player, msg.get("code"))
        elif mtype == P.C_SETCOLOR:
            self._handle_setcolor(player, msg.get("color"))
        elif mtype == P.C_UNBAN:
            if player.is_admin:
                self._handle_unban(msg.get("username"))
        elif mtype == P.C_BANLIST:
            if player.is_admin:
                await self._send_banlist(player)
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
        self._sample_wpm(player, now)

        if not player.eliminated and self._is_finish_event(player, n):
            self._finish_player(player)
        self._mark_dirty()
        self._maybe_end_race()

    def _sample_wpm(self, p, now):
        """Append a WPM sample for the intra-race timeline (rate-limited, capped)."""
        if not self.race_start or len(p.wpm_samples) >= WPM_SAMPLE_MAX:
            return
        if now - p.last_sample_at < WPM_SAMPLE_INTERVAL:
            return
        elapsed = max(0.0, now - self.race_start)
        wpm, _ = self._stats(p, elapsed)
        p.wpm_samples.append(wpm)
        p.last_sample_at = now

    # ---------------------------------------------------------- race phases
    def _maybe_autostart(self):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        connected = [p for p in self.players.values() if p.connected]
        humans = [p for p in connected if not p.is_bot]
        ready_humans = [p for p in humans if p.ready]
        # Need at least one human (so a room of only-bots never self-starts) and
        # enough ready humans, and nobody human still un-ready.
        if (humans and len(ready_humans) >= max(1, self.config.get("min_players", 1))
                and all(p.ready for p in humans)):
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
            seconds = self._effective_countdown()
            self.countdown = seconds
            self._mark_dirty()
            for n in range(seconds, 0, -1):
                self.countdown = n
                self._mark_dirty()
                await asyncio.sleep(1.0)
            self._start_race(racer_ids)
        except asyncio.CancelledError:
            pass

    def _effective_countdown(self):
        """Host-configured countdown, or 0 for a lone human with quick-start on."""
        secs = self.config.get("countdown", COUNTDOWN_SECONDS)
        if secs not in modes.COUNTDOWN_OPTIONS:
            secs = COUNTDOWN_SECONDS
        humans = [p for p in self.players.values()
                  if p.connected and not p.is_bot]
        if self.config.get("quick_start") and len(humans) <= 1:
            return 0
        return secs

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
        self._cancel_bot_tasks()
        self._cancel_rematch()
        for p in self.players.values():
            p.reset_race(in_race=(p.id in racer_ids and p.connected), lives=lives)
            p.ready = p.is_bot          # bots are perpetually ready
        self._spawn_bot_drivers(racer_ids)
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
        # An account inside its reconnection grace still counts as a racer, so a
        # brief drop doesn't prematurely resolve the race.
        racers = [p for p in self.players.values()
                  if p.in_race and (p.connected or self._in_grace(p))]
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
        self._cancel_bot_tasks()
        # A racer still inside its reconnection grace when the race ends (e.g. the
        # TIMED deadline fired before they returned) didn't make it back in time.
        # Evict them now so the final standings/recording only contain players who
        # were actually present -- _maybe_end_race counts in-grace players, so this
        # keeps the two halves of the race-end path consistent.
        for gp in [p for p in self.players.values() if self._in_grace(p)]:
            self._evict(gp)
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
        self._award_session_points()
        self._celebration = self._build_celebration()
        self.phase = P.PHASE_RESULTS
        for p in self.players.values():
            p.ready = p.is_bot
        self._mark_dirty()
        self._arm_rematch()

    def _record_results(self):
        if self.store is None:
            return
        now = self.loop.time()
        elapsed = max(0.0, now - self.race_start) if self.race_start else 0.0
        mode = self.config["mode"]
        category = self.text_meta.get("category")
        if category == "custom":
            category = None
        # in-race accounted players (humans), not bots/spectators
        scored = [p for p in self.players.values()
                  if p.in_race and not p.is_bot and p.connected and p.account]
        racers = sum(1 for q in self.players.values()
                     if q.in_race and not q.is_bot and q.connected)
        for p in scored:
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
            outcome = self.store.record_race(
                p.account, net_wpm=float(net), raw_wpm=float(raw), accuracy=acc,
                seconds=seconds, chars=p.pos, keystrokes=p.keystrokes,
                errors=p.errors, won=(p.place == 1), place=p.place, racers=racers,
                mode=mode, category=category, flawless=flawless)
            self._announce_outcome(p, outcome)
        # Pairwise rivalry records across everyone who was scored.
        self.store.record_h2h([(p.account, p.place) for p in scored])

    def _announce_outcome(self, player, outcome):
        """Turn a record_race result into one-shot results-screen popups."""
        for aid in outcome.get("achievements", []):
            self._pending_announcements.append(
                {"kind": "badge", "name": player.name,
                 "badge": achievements.label_for(aid)})
        for lvl in outcome.get("levels", []):
            self._pending_announcements.append(
                {"kind": "level", "name": player.name, "level": lvl})
        for pb in outcome.get("pbs", []):
            self._pending_announcements.append(
                {"kind": "pb", "name": player.name, "pb_kind": pb["kind"],
                 "old": pb["old"], "new": pb["new"]})
        days = outcome.get("day_streak")
        if days and days >= 2:
            self._pending_announcements.append(
                {"kind": "daystreak", "name": player.name, "days": days})

    # ------------------------------------------------ session scoreboard
    def _session_key(self, p):
        """Stable identity for the session table (survives reconnects)."""
        return ("acct:" + p.account.lower()) if p.account else ("guest:" + str(p.id))

    def _award_session_points(self):
        """Add F1-style points to the running scoreboard for this race."""
        self.session_race_no += 1
        for p in self.players.values():
            if not (p.in_race and p.connected and not p.is_bot):
                continue
            key = self._session_key(p)
            entry = self.session_scores.setdefault(
                key, {"name": p.name, "points": 0, "races": 0, "wins": 0,
                      "best_place": None})
            entry["name"] = p.name
            entry["points"] += modes.points_for_place(p.place)
            entry["races"] += 1
            if p.place == 1:
                entry["wins"] += 1
            if p.place is not None:
                entry["best_place"] = (p.place if entry["best_place"] is None
                                       else min(entry["best_place"], p.place))
        # Backstop cap (defense in depth against any churn vector): keep the
        # highest-scoring entries if the table somehow grows past the limit.
        if len(self.session_scores) > SESSION_SCORES_MAX:
            kept = sorted(self.session_scores.items(),
                          key=lambda kv: kv[1]["points"], reverse=True)[:SESSION_SCORES_MAX]
            self.session_scores = dict(kept)

    def _reset_session(self):
        self.session_scores = {}
        self.session_race_no = 0
        self._mark_dirty()

    def _session_view(self):
        standings = sorted(
            self.session_scores.values(),
            key=lambda e: (-e["points"], e.get("best_place") or 999, -e["wins"]))
        return {"race_no": self.session_race_no, "standings": standings[:MAX_PLAYERS]}

    # ----------------------------------------------- win celebration banner
    def _build_celebration(self):
        winner = next((p for p in self.players.values()
                       if p.in_race and p.place == 1), None)
        if winner is None:
            return None
        elapsed = (max(0.0, self.loop.time() - self.race_start)
                   if self.race_start else 0.0)
        wpm, _ = self._stats(winner, elapsed)
        flags = []
        if winner.errors == 0 and winner.pos > 0:
            flags.append("flawless")
        finishers = sorted(
            (p for p in self.players.values()
             if p.in_race and p.finished and p.finish_time is not None),
            key=lambda q: q.finish_time)
        if (len(finishers) >= 2
                and finishers[1].finish_time - finishers[0].finish_time <= PHOTO_FINISH_GAP):
            flags.append("photo_finish")
        if winner.account and self.store:
            st = self.store.public_stats(winner.account) or {}
            if st.get("streak", 0) >= 3:
                flags.append("streak")
            runner = next((p for p in self.players.values()
                           if p.in_race and p.place == 2 and p.account), None)
            if runner:
                rs = self.store.public_stats(runner.account) or {}
                if st.get("best_wpm", 0) + 8 < rs.get("best_wpm", 0):
                    flags.append("upset")
        return {"winner": winner.name, "wpm": wpm, "flags": flags,
                "is_bot": winner.is_bot}

    # ------------------------------------------------------ auto-rematch
    def _arm_rematch(self):
        secs = self.config.get("rematch_secs", 0)
        if not secs:
            return
        self._cancel_rematch()
        self._rematch_task = self.loop.create_task(self._auto_rematch(secs))

    async def _auto_rematch(self, secs):
        try:
            await asyncio.sleep(secs)
            if self.phase != P.PHASE_RESULTS:
                return
            if any(p.connected and not p.is_bot for p in self.players.values()):
                self._force_start()
        except asyncio.CancelledError:
            pass

    def _to_lobby(self):
        self._cancel_countdown()
        self._cancel_watchdog()
        self._cancel_bot_tasks()
        self._cancel_rematch()
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
            p.ready = p.is_bot
        self._mark_dirty()

    def _cancel_countdown(self):
        if self._countdown_task is not None:
            self._countdown_task.cancel()
            self._countdown_task = None

    def _cancel_watchdog(self):
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    def _cancel_rematch(self):
        if self._rematch_task is not None:
            self._rematch_task.cancel()
            self._rematch_task = None

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
        if "countdown" in msg:
            cd = _to_int(msg.get("countdown"))
            if cd in modes.COUNTDOWN_OPTIONS:
                cfg["countdown"] = cd
        if "quick_start" in msg:
            cfg["quick_start"] = bool(msg.get("quick_start"))
        if "min_players" in msg:
            mp = _to_int(msg.get("min_players"))
            if 1 <= mp <= MAX_PLAYERS:
                cfg["min_players"] = mp
        if "rematch_secs" in msg:
            rs = _to_int(msg.get("rematch_secs"))
            if rs in modes.REMATCH_OPTIONS:
                cfg["rematch_secs"] = rs
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
        self._persist_host()
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

    # --------------------------------------------------------------- emotes
    def _handle_emote(self, player, code):
        """Fire a canned quick-chat emote (allowed in any phase, incl. racing)."""
        if code not in P.EMOTES:
            return
        now = self.loop.time()
        if player.recent_emote and now - player.recent_emote_at < EMOTE_RATE:
            return
        player.recent_emote = code
        player.recent_emote_at = now
        # Log it in lobby/results chat; mid-race it shows only as a track bubble.
        if self.phase in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            self._push_chat(player.id, player.name, P.EMOTES[code], "emote")
        else:
            self._mark_dirty()

    # ---------------------------------------------------------- accent color
    def _handle_setcolor(self, player, color):
        """Persist an account's accent color (guests use a deterministic hash)."""
        if not (player.account and self.store):
            return
        if color is not None and color not in P.PLAYER_COLORS:
            return
        if self.store.set_color(player.account, color):
            self._mark_dirty()

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
                               "recent": prof["recent"],
                               "level_progress": prof["level_progress"],
                               "milestones": prof["milestones"],
                               "rivals": prof["rivals"]}
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
        if target.is_bot:                       # "kicking" a bot just removes it
            self._remove_bot(target.id)
            return
        if target.account:
            self.kicked_accounts.add(target.account.lower())
            self._persist_host()
        self._system_chat(f"{target.name} was removed by the host")
        self.loop.create_task(self._kick_ws(target))

    async def _kick_ws(self, target):
        await self._safe_send(target.ws, {"type": P.S_ERROR,
                                          "msg": "you were removed by the host"})
        await self._close_ws(target.ws, P.CLOSE_KICKED, "kicked")

    def _handle_unban(self, username):
        key = str(username or "").lower()
        if key in self.kicked_accounts:
            self.kicked_accounts.discard(key)
            self._persist_host()
            self._system_chat(f"{username} was un-banned")

    async def _send_banlist(self, player):
        await self._safe_send(player.ws, {"type": P.S_BANLIST,
                                          "rows": sorted(self.kicked_accounts)})

    def _persist_host(self):
        """Save host config + ban set to disk, if a host store was provided."""
        if self.host_store is not None:
            try:
                self.host_store.save(self.config, self.kicked_accounts)
            except Exception:
                pass

    # -------------------------------------------------------------- AI bots
    def _bots(self):
        return [p for p in self.players.values() if p.is_bot]

    def _add_bot(self, difficulty):
        """Add a virtual racer. Only in the lobby/results; returns an error str."""
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return "bots can only be added between races"
        if difficulty not in modes.BOT_DIFFICULTIES:
            difficulty = modes.DEFAULT_BOT_DIFFICULTY
        if len(self._bots()) >= modes.MAX_BOTS:
            return f"bot limit reached ({modes.MAX_BOTS})"
        # Bots are exempt from the human cap, but the whole roster is still
        # bounded (human cap + bot cap) so it can never grow without limit.
        if len(self.players) >= self.max_players + modes.MAX_BOTS:
            return "the game is full"
        self._bot_seq += 1
        name = self._unique_name(self._bot_name())
        bot = Player(self._next_id, name, None, is_admin=False, account=None,
                     is_guest=False, is_bot=True, difficulty=difficulty)
        self._next_id += 1
        self.players[bot.id] = bot
        label = modes.BOT_DIFFICULTIES[difficulty]["label"]
        self._system_chat(f"{name} ({label} bot) joined")
        self._mark_dirty()
        return None

    def _bot_name(self):
        pool = modes.BOT_NAMES
        return pool[(self._bot_seq - 1) % len(pool)]

    def _remove_bot(self, target_id):
        """Remove a specific bot, or the most recently added one."""
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        bots = self._bots()
        if not bots:
            return
        if target_id is not None:
            target = self.players.get(_to_int(target_id))
            if not target or not target.is_bot:
                return
        else:
            target = max(bots, key=lambda b: b.id)
        self.players.pop(target.id, None)
        self._system_chat(f"{target.name} left")
        self._mark_dirty()
        self._maybe_autostart()

    def _bot_target(self, bot):
        """Return (chars-per-second, accuracy) for a bot in the current race."""
        spec = modes.BOT_DIFFICULTIES.get(bot.difficulty,
                                          modes.BOT_DIFFICULTIES["medium"])
        wpm = spec["wpm"]
        if wpm is None:                    # rival: calibrate to the best human
            ref = 0.0
            for p in self.players.values():
                if p.in_race and not p.is_bot and p.account and self.store:
                    st = self.store.public_stats(p.account) or {}
                    ref = max(ref, st.get("avg_wpm", 0) or 0,
                              (st.get("best_wpm", 0) or 0) * 0.75)
            wpm = ref if ref > 0 else 50.0
            wpm = max(20.0, min(150.0, wpm * 1.03))
        cps = max(0.5, wpm * 5.0 / 60.0)
        return cps, spec["acc"]

    def _spawn_bot_drivers(self, racer_ids):
        for bot in self._bots():
            if bot.id in racer_ids:
                cps, acc = self._bot_target(bot)
                self._bot_tasks.append(
                    self.loop.create_task(self._drive_bot(bot, cps, acc)))

    async def _drive_bot(self, bot, cps, acc):
        """Advance a bot's progress over time at ~cps, with jitter and slips."""
        try:
            err_prob = max(0.0, min(0.5, 1.0 - acc))
            # A short, varied reaction delay so bots don't move in lockstep.
            await asyncio.sleep(0.15 + self.rng.random() * 0.35)
            while True:
                if (self.phase != P.PHASE_RACING or not bot.in_race
                        or bot.finished or bot.eliminated):
                    return
                await asyncio.sleep(0.10 + self.rng.random() * 0.14)
                if (self.phase != P.PHASE_RACING or not bot.in_race
                        or bot.finished or bot.eliminated):
                    return
                n = len(self.text)
                elapsed = (max(0.0, self.loop.time() - self.race_start)
                           if self.race_start else 0.0)
                jitter = 0.85 + self.rng.random() * 0.30
                target = int(elapsed * cps * jitter)
                newpos = max(bot.pos, min(target, n))
                made_error = (newpos > bot.pos and self.rng.random() < err_prob)
                if made_error:
                    bot.errors += 1
                bot.pos = newpos
                bot.last_pos = newpos
                bot.keystrokes = bot.pos + bot.errors
                self._sample_wpm(bot, self.loop.time())
                if (self.config["mode"] == modes.MODE_SURVIVAL
                        and made_error and bot.lives > 0):
                    bot.lives -= 1
                    if bot.lives <= 0:
                        bot.lives = 0
                        self._eliminate(bot)
                if not bot.eliminated and self._is_finish_event(bot, n):
                    self._finish_player(bot)
                self._mark_dirty()
                self._maybe_end_race()
        except asyncio.CancelledError:
            pass

    def _cancel_bot_tasks(self):
        for t in self._bot_tasks:
            if not t.done():
                t.cancel()
        self._bot_tasks = []

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
        # Bots have no websocket; only real connections receive snapshots.
        targets = [p for p in self.players.values()
                   if p.connected and p.ws is not None]
        if not targets:
            return   # keep one-shot announcements/celebration pending for a human
        # Consume the one-shot state only now that it is actually being delivered,
        # so a banner/popups built while nobody was connected survive until the
        # first human (re)appears in PHASE_RESULTS.
        self._pending_announcements = []
        self._celebration = None
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
            snap["session"] = self._session_view()
            if self._pending_announcements:
                snap["announcements"] = list(self._pending_announcements)
        if self.phase == P.PHASE_RESULTS and self._celebration:
            snap["celebration"] = self._celebration
        return snap

    def _config_view(self):
        c = self.config
        return {"mode": c["mode"], "length": c["length"], "category": c["category"],
                "difficulty": c["difficulty"], "time_limit": c["time_limit"],
                "lives": c["lives"], "has_custom": bool(c["custom_text"]),
                "countdown": c.get("countdown", COUNTDOWN_SECONDS),
                "quick_start": bool(c.get("quick_start")),
                "min_players": c.get("min_players", 1),
                "rematch_secs": c.get("rematch_secs", 0)}

    def _ordered_players(self):
        return [self.players[pid] for pid in sorted(self.players)]

    def _player_view(self, p, elapsed, now):
        wpm, acc = self._stats(p, elapsed)
        survival = self.config["mode"] == modes.MODE_SURVIVAL
        idle = (self.phase in (P.PHASE_LOBBY, P.PHASE_RESULTS)
                and p.last_seen > 0 and (now - p.last_seen) > IDLE_SECONDS)
        pub = (self.store.public_stats(p.account)
               if (p.account and self.store) else None)
        emote = None
        if p.recent_emote and (now - p.recent_emote_at) <= EMOTE_DECAY:
            emote = p.recent_emote
        return {
            "id": p.id,
            "name": p.name,
            "is_admin": p.is_admin,
            "is_guest": p.is_guest,
            "is_bot": p.is_bot,
            "difficulty": p.difficulty,
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
            "level": (pub or {}).get("level"),
            "tier": (pub or {}).get("tier"),
            "color": self._color_for(p, pub),
            "session_points": self.session_scores.get(
                self._session_key(p), {}).get("points", 0),
            "recent_emote": emote,
            # The intra-race WPM timeline is heavy, so it ships only on results.
            "splits": list(p.wpm_samples) if self.phase == P.PHASE_RESULTS else None,
            "stats": pub,
        }

    def _color_for(self, p, pub):
        """Resolve a player's accent color: account choice, else a stable hash."""
        if pub and pub.get("color"):
            return pub["color"]
        if p.is_bot:
            return "magenta"
        # Deterministic per-name color so guests are still distinguishable
        # (built-in hash() is salted per process; a digit sum is stable).
        palette = P.PLAYER_COLORS
        return palette[sum(ord(c) for c in p.name) % len(palette)]

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
        self._cancel_bot_tasks()
        self._cancel_rematch()
        if self._broadcaster_task is not None:
            self._broadcaster_task.cancel()
            self._broadcaster_task = None
