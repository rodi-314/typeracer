"""Authoritative TypeRacer game server.

One event loop, one shared :class:`GameServer`. Each websocket connection runs a
:meth:`GameServer.handler` coroutine; all of them mutate the same in-memory
state. Because asyncio is single-threaded and the mutations between ``await``
points are synchronous, no locks are required.

The server owns the clock (``loop.time()``), the phase, and finish ordering, so
clients can never cheat the standings even though they report their own typing
progress.
"""

import asyncio
import random
import secrets

from websockets.exceptions import ConnectionClosed

import protocol as P
import accounts
from texts import pick_text


COUNTDOWN_SECONDS = 3
MAX_RACE_SECONDS = 240        # safety net so a stuck racer can't hang the game
BROADCAST_MIN_INTERVAL = 0.03  # coalesce bursts of state changes (~33 Hz)
MAX_PLAYERS = 16


def _to_int(value, default=0):
    """Coerce an untrusted JSON value to int, never raising on junk input."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class Player:
    __slots__ = (
        "id", "name", "ws", "is_admin", "account", "is_guest",
        "ready", "connected",
        "in_race", "pos", "errors", "keystrokes",
        "finished", "finish_time", "place",
    )

    def __init__(self, pid, name, ws, is_admin, account=None, is_guest=True):
        self.id = pid
        self.name = name
        self.ws = ws
        self.is_admin = is_admin
        self.account = account     # account username (stat key), or None for guests
        self.is_guest = is_guest
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

    def reset_race(self, in_race):
        self.in_race = in_race
        self.pos = 0
        self.errors = 0
        self.keystrokes = 0
        self.finished = False
        self.finish_time = None
        self.place = None


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
        self.countdown = 0
        self.race_start = None     # loop.time() at GO
        self.finish_order = []     # list of player ids in finishing order

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
        }

    # ----------------------------------------------------------- connection
    async def handler(self, websocket):
        self._ensure_started()
        player = None
        try:
            player = await self._authenticate(websocket)
            if player is None:
                return
            await websocket.send(P.encode({
                "type": P.S_AUTH_OK,
                "id": player.id,
                "name": player.name,
                "account": player.account,
                "is_admin": player.is_admin,
                "is_guest": player.is_guest,
                "stats": self.store.public_stats(player.account)
                         if (player.account and self.store) else None,
                "version": P.PROTOCOL_VERSION,
            }))
            self._mark_dirty()
            await self._mark_dirty_and_autostart()
            async for raw in websocket:
                await self._dispatch(player, raw)
        except ConnectionClosed:
            pass
        finally:
            if player is not None:
                self._unregister(player)

    async def _authenticate(self, websocket):
        """Loop until the client authenticates (allowing retries) or disconnects.

        The first message must be register/login/guest. Failures get an
        S_AUTH_FAIL and the loop continues so the login screen can retry on the
        same connection.
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
            if len([p for p in self.players.values() if p.connected]) >= MAX_PLAYERS:
                await self._send_auth_fail(websocket, "the game is full")
                continue

            is_admin = bool(self.admin_token) and msg.get("token") == self.admin_token
            name, account, is_guest, err = self._resolve_auth(msg, mtype)
            if err:
                await self._send_auth_fail(websocket, err)
                continue
            return self._make_player(name, websocket, is_admin, account, is_guest)

    def _resolve_auth(self, msg, mtype):
        """Return (display_name, account_or_None, is_guest, error_or_None)."""
        if mtype == P.C_GUEST:
            name = str(msg.get("name") or "Guest").strip()[:16] or "Guest"
            return name, None, True, None

        # login / register require a store
        if self.store is None:
            return None, None, None, "accounts are disabled on this server"
        username = str(msg.get("username") or "").strip()
        password = str(msg.get("password") or "")

        if mtype == P.C_REGISTER:
            record, err = self.store.create(username, password)
            if err:
                return None, None, None, err
            account = record["username"]
        else:  # C_LOGIN
            record, err = self.store.authenticate(username, password)
            if err:
                return None, None, None, err
            account = record["username"]

        # one live connection per account
        if any(p.connected and p.account and p.account.lower() == account.lower()
               for p in self.players.values()):
            return None, None, None, "that account is already logged in"
        return account, account, False, None

    def _make_player(self, name, websocket, is_admin, account, is_guest):
        player = Player(self._next_id, self._unique_name(name), websocket,
                        is_admin, account=account, is_guest=is_guest)
        self._next_id += 1
        self.players[player.id] = player
        return player

    def _unique_name(self, name):
        existing = {p.name for p in self.players.values() if p.connected}
        if name not in existing:
            return name
        i = 2
        while f"{name} ({i})" in existing:
            i += 1
        return f"{name} ({i})"

    def _unregister(self, player):
        player.connected = False
        self.players.pop(player.id, None)
        self.finish_order = [pid for pid in self.finish_order if pid in self.players]
        # Renumber places so they stay consistent with the pruned finish order.
        for i, pid in enumerate(self.finish_order, 1):
            self.players[pid].place = i
        self._mark_dirty()
        if self.phase == P.PHASE_RACING:
            self._check_race_complete()
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
        elif mtype == P.C_LEADERBOARD:
            await self._send_leaderboard(player, msg.get("metric"))
        elif mtype == P.C_PING:
            try:
                await player.ws.send(P.encode({"type": P.S_PONG}))
            except ConnectionClosed:
                pass

    def _handle_ready(self, player, ready):
        if self.phase not in (P.PHASE_LOBBY, P.PHASE_RESULTS):
            return
        player.ready = ready
        self._mark_dirty()
        self._maybe_autostart()

    def _handle_progress(self, player, msg):
        if self.phase != P.PHASE_RACING or not player.in_race or player.finished:
            return
        n = len(self.text)
        player.pos = max(0, min(_to_int(msg.get("pos", 0)), n))
        player.errors = max(0, _to_int(msg.get("errors", 0)))
        player.keystrokes = max(0, _to_int(msg.get("keystrokes", 0)))
        if player.pos >= n:
            self._finish_player(player)
            self._check_race_complete()
        self._mark_dirty()

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
        # Guard against a countdown already scheduled this tick: the phase isn't
        # flipped to COUNTDOWN until _run_countdown actually runs, so two callers
        # in the same loop tick would otherwise both schedule a countdown.
        if self._countdown_task is not None and not self._countdown_task.done():
            return
        racer_ids = {p.id for p in racers}
        self._countdown_task = self.loop.create_task(self._run_countdown(racer_ids))

    async def _run_countdown(self, racer_ids):
        try:
            self.phase = P.PHASE_COUNTDOWN
            self.text = pick_text(self.rng, avoid=self.text)
            self.countdown = COUNTDOWN_SECONDS
            self._mark_dirty()
            for n in range(COUNTDOWN_SECONDS, 0, -1):
                self.countdown = n
                self._mark_dirty()
                await asyncio.sleep(1.0)
            self._start_race(racer_ids)
        except asyncio.CancelledError:
            pass

    def _start_race(self, racer_ids):
        self.phase = P.PHASE_RACING
        self.countdown = 0
        self.finish_order = []
        self.race_start = self.loop.time()
        for p in self.players.values():
            p.reset_race(in_race=(p.id in racer_ids and p.connected))
            p.ready = False
        self._watchdog_task = self.loop.create_task(self._race_watchdog())
        self._mark_dirty()

    def _finish_player(self, player):
        if player.finished:
            return
        player.finished = True
        player.pos = len(self.text)
        player.finish_time = max(0.0, self.loop.time() - self.race_start)
        self.finish_order.append(player.id)
        player.place = len(self.finish_order)

    def _check_race_complete(self):
        if self.phase != P.PHASE_RACING:
            return
        racers = [p for p in self.players.values() if p.in_race and p.connected]
        if not racers:
            self._end_race()
        elif all(p.finished for p in racers):
            self._end_race()

    async def _race_watchdog(self):
        try:
            await asyncio.sleep(MAX_RACE_SECONDS)
            if self.phase == P.PHASE_RACING:
                self._end_race()
        except asyncio.CancelledError:
            pass

    def _end_race(self):
        if self.phase != P.PHASE_RACING:
            return
        self._cancel_watchdog()
        # Rank any racer who did not finish, by progress then keystroke economy.
        unfinished = [
            p for p in self.players.values()
            if p.in_race and p.connected and not p.finished
        ]
        for p in sorted(unfinished, key=lambda q: (-q.pos, q.errors)):
            self.finish_order.append(p.id)
            p.place = len(self.finish_order)
        self._record_results()
        self.phase = P.PHASE_RESULTS
        for p in self.players.values():
            p.ready = False
        self._mark_dirty()

    def _record_results(self):
        """Persist each logged-in racer's result to their account."""
        if self.store is None:
            return
        now = self.loop.time()
        elapsed = max(0.0, now - self.race_start) if self.race_start else 0.0
        for p in self.players.values():
            if not (p.in_race and p.connected and p.account):
                continue
            wpm, acc = self._stats(p, elapsed)
            seconds = p.finish_time if (p.finished and p.finish_time) else elapsed
            self.store.record_race(
                p.account, float(wpm), float(acc), seconds, p.pos, p.place == 1
            )

    def _to_lobby(self):
        self._cancel_countdown()
        self._cancel_watchdog()
        self.phase = P.PHASE_LOBBY
        self.text = ""
        self.countdown = 0
        self.race_start = None
        self.finish_order = []
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

    # --------------------------------------------------------- broadcasting
    def _ensure_started(self):
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        if self._broadcaster_task is None:
            self._broadcaster_task = self.loop.create_task(self._broadcaster())

    def _mark_dirty(self):
        self._dirty.set()

    async def _mark_dirty_and_autostart(self):
        # Used right after welcome so a solo "ready" player can start.
        self._maybe_autostart()
        self._mark_dirty()

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
        targets = [p for p in self.players.values() if p.connected]
        if not targets:
            return
        results = await asyncio.gather(
            *(p.ws.send(snapshot) for p in targets), return_exceptions=True
        )
        # Connections that errored on send are dead; the handler's finally will
        # clean them up, so nothing to do here.
        del results

    def snapshot(self):
        elapsed = 0.0
        if self.phase == P.PHASE_RACING and self.race_start is not None:
            elapsed = max(0.0, self.loop.time() - self.race_start)
        include_text = self.phase in (
            P.PHASE_COUNTDOWN, P.PHASE_RACING, P.PHASE_RESULTS
        )
        players = [self._player_view(p, elapsed) for p in self._ordered_players()]
        return {
            "type": P.S_STATE,
            "phase": self.phase,
            "countdown": self.countdown,
            "elapsed": round(elapsed, 2),
            "text": self.text if include_text else "",
            "text_len": len(self.text),
            "players": players,
        }

    def _ordered_players(self):
        # Stable display order: by id (join order); finishers sort by place in UI.
        return [self.players[pid] for pid in sorted(self.players)]

    def _player_view(self, p, elapsed):
        wpm, acc = self._stats(p, elapsed)
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

    async def _send_leaderboard(self, player, metric):
        metric = metric if metric in accounts.LEADERBOARD_METRICS else "best_wpm"
        rows = self.store.leaderboard(metric) if self.store else []
        try:
            await player.ws.send(P.encode({
                "type": P.S_LEADERBOARD, "metric": metric, "rows": rows,
            }))
        except ConnectionClosed:
            pass

    # ------------------------------------------------------------ utilities
    async def _send_error(self, websocket, message):
        try:
            await websocket.send(P.encode({"type": P.S_ERROR, "msg": message}))
        except ConnectionClosed:
            pass

    async def _send_auth_fail(self, websocket, message):
        try:
            await websocket.send(P.encode({"type": P.S_AUTH_FAIL, "msg": message}))
        except ConnectionClosed:
            pass

    def shutdown(self):
        self._cancel_countdown()
        self._cancel_watchdog()
        if self._broadcaster_task is not None:
            self._broadcaster_task.cancel()
            self._broadcaster_task = None
