#!/usr/bin/env python3
"""Headless end-to-end tests for the TypeRacer server and wire protocol.

These drive the real :class:`GameServer` over real websocket connections using
lightweight bot clients (no terminal UI), so the full state machine -- lobby,
ready/auto-start, countdown, racing, finish ordering, results, replay,
spectators, disconnects, name de-duplication and admin authority -- is exercised
exactly as the interactive client would exercise it.

Run:  python selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile

from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import protocol as P
import server as server_mod
from server import GameServer
from accounts import AccountStore

# Skip the real 3-2-1 wait so the suite runs quickly, and lift the anti-cheat
# rate cap so the instant-finish bots aren't clamped (a dedicated scenario tests
# the cap with a realistic value).
server_mod.COUNTDOWN_SECONDS = 0
server_mod.MAX_CPS = 1e9

TOKEN = "TESTTOKEN"
_failures = []


def check(cond, label):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        _failures.append(label)


class Bot:
    """Minimal scripted client: authenticates, tracks the latest snapshot."""

    def __init__(self, name):
        self.name = name
        self.ws = None
        self.id = None
        self.is_admin = False
        self.is_guest = True
        self.account = None
        self.stats = None
        self.auth_error = None
        self.latest = None
        self.leaderboard_rows = None
        self.profile = None
        self.history_rows = None
        self.closed_code = None
        self.celebration = None
        self.celebration_count = 0
        self.banlist = None
        self._authed = asyncio.Event()
        self._updated = asyncio.Event()
        self._lb = asyncio.Event()
        self._profile_ev = asyncio.Event()
        self._history_ev = asyncio.Event()
        self._ban_ev = asyncio.Event()
        self._reader = None

    async def _auth(self, uri, msg):
        self.ws = await connect(uri)
        await self.ws.send(P.encode(msg))
        self._reader = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._authed.wait(), timeout=5)
        return self.auth_error is None

    async def start(self, uri, token=None, room_password=None):
        """Join as a guest (keeps gameplay scenarios simple)."""
        return await self._auth(uri, {"type": P.C_GUEST, "name": self.name,
                                      "token": token, "room_password": room_password,
                                      "version": P.PROTOCOL_VERSION})

    async def register(self, uri, username, password, token=None,
                       room_password=None):
        return await self._auth(uri, {"type": P.C_REGISTER, "username": username,
                                      "password": password, "token": token,
                                      "room_password": room_password,
                                      "version": P.PROTOCOL_VERSION})

    async def login(self, uri, username, password, token=None, room_password=None):
        return await self._auth(uri, {"type": P.C_LOGIN, "username": username,
                                      "password": password, "token": token,
                                      "room_password": room_password,
                                      "version": P.PROTOCOL_VERSION})

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                msg = P.decode(raw)
                t = msg.get("type")
                if t == P.S_AUTH_OK:
                    self.id = msg["id"]
                    self.is_admin = msg["is_admin"]
                    self.is_guest = msg["is_guest"]
                    self.account = msg["account"]
                    self.name = msg["name"]
                    self.stats = msg["stats"]
                    self.auth_error = None
                    self._authed.set()
                elif t == P.S_AUTH_FAIL:
                    self.auth_error = msg.get("msg")
                    self._authed.set()
                elif t == P.S_STATE:
                    self.latest = msg
                    if "celebration" in msg:
                        self.celebration = msg["celebration"]
                        self.celebration_count += 1
                    self._updated.set()
                elif t == P.S_LEADERBOARD:
                    self.leaderboard_rows = msg.get("rows", [])
                    self._lb.set()
                elif t == P.S_PROFILE:
                    self.profile = msg
                    self._profile_ev.set()
                elif t == P.S_HISTORY:
                    self.history_rows = msg.get("rows", [])
                    self._history_ev.set()
                elif t == P.S_BANLIST:
                    self.banlist = msg.get("rows", [])
                    self._ban_ev.set()
        except ConnectionClosed:
            self.closed_code = getattr(self.ws, "close_code", None)

    async def request_leaderboard(self, metric="best_wpm", mode=None, category=None):
        self._lb.clear()
        await self.send({"type": P.C_LEADERBOARD, "metric": metric,
                         "mode": mode, "category": category})
        await asyncio.wait_for(self._lb.wait(), timeout=5)
        return self.leaderboard_rows

    async def request_profile(self, target_id=None):
        self._profile_ev.clear()
        await self.send({"type": P.C_PROFILE, "target_id": target_id})
        await asyncio.wait_for(self._profile_ev.wait(), timeout=5)
        return self.profile

    async def request_history(self):
        self._history_ev.clear()
        await self.send({"type": P.C_HISTORY})
        await asyncio.wait_for(self._history_ev.wait(), timeout=5)
        return self.history_rows

    async def chat(self, text):
        await self.send({"type": P.C_CHAT, "text": text})

    async def config(self, **fields):
        await self.send({"type": P.C_CONFIG, **fields})

    async def kick(self, target_id):
        await self.send({"type": P.C_KICK, "target_id": target_id})

    async def add_bot(self, difficulty="medium"):
        await self.send({"type": P.C_ADD_BOT, "difficulty": difficulty})

    async def remove_bot(self, target_id=None):
        await self.send({"type": P.C_REMOVE_BOT, "target_id": target_id})

    async def emote(self, code):
        await self.send({"type": P.C_EMOTE, "code": code})

    async def setcolor(self, color):
        await self.send({"type": P.C_SETCOLOR, "color": color})

    async def unban(self, username):
        await self.send({"type": P.C_UNBAN, "username": username})

    async def request_banlist(self):
        self._ban_ev.clear()
        await self.send({"type": P.C_BANLIST})
        await asyncio.wait_for(self._ban_ev.wait(), timeout=5)
        return self.banlist

    async def send(self, obj):
        await self.ws.send(P.encode(obj))

    async def ready(self, val=True):
        await self.send({"type": P.C_READY, "ready": val})

    async def progress(self, pos, errors=0):
        await self.send({"type": P.C_PROGRESS, "pos": pos,
                         "errors": errors, "keystrokes": pos + errors})

    async def finish(self, text, errors=0):
        # a couple of intermediate updates, then the final position
        for frac in (0.34, 0.67):
            await self.progress(int(len(text) * frac), errors)
            await asyncio.sleep(0.01)
        await self.progress(len(text), errors)

    async def wait_for(self, predicate, timeout=8):
        async def _loop():
            while True:
                if self.latest is not None and predicate(self.latest):
                    return self.latest
                self._updated.clear()
                await self._updated.wait()
        return await asyncio.wait_for(_loop(), timeout=timeout)

    def player(self, name=None):
        name = name or self.name
        for p in (self.latest or {}).get("players", []):
            if p["name"] == name:
                return p
        return None

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass
        if self._reader:
            self._reader.cancel()


class Harness:
    """Spin up a fresh server (with an account store) on an ephemeral port."""

    def __init__(self, seed=1, with_store=True, host_store=None, max_players=16,
                 room_password=None):
        self.store = None
        self.data_path = None
        if with_store:
            fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_test_")
            os.close(fd)
            os.unlink(path)  # let AccountStore create it on first save
            self.data_path = path
            self.store = AccountStore(path)
        self.gs = GameServer(admin_token=TOKEN, seed=seed, store=self.store,
                             host_store=host_store, max_players=max_players,
                             room_password=room_password)
        # countdown is now host-configurable; keep it instant for the suite
        # (the module-level COUNTDOWN_SECONDS override no longer wins on its own)
        self.gs.config["countdown"] = 0
        self.srv = None
        self.uri = None

    async def __aenter__(self):
        self.srv = await serve(self.gs.handler, "127.0.0.1", 0)
        port = self.srv.sockets[0].getsockname()[1]
        self.uri = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.gs.shutdown()
        self.srv.close()
        await self.srv.wait_closed()
        if self.data_path and os.path.exists(self.data_path):
            os.unlink(self.data_path)


def racing(s):
    return s["phase"] == P.PHASE_RACING


def results(s):
    return s["phase"] == P.PHASE_RESULTS


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
async def scenario_basic_race():
    print("scenario: basic two-player race")
    async with Harness() as h:
        alice = Bot("Alice")
        bob = Bot("Bob")
        await alice.start(h.uri, TOKEN)
        await bob.start(h.uri)
        check(alice.is_admin, "first joiner with token is admin")
        check(not bob.is_admin, "joiner without token is not admin")

        await alice.ready(True)
        await bob.ready(True)
        state = await alice.wait_for(racing)
        text = state["text"]
        check(bool(text), "race text is delivered")

        await bob.finish(text, errors=0)     # Bob finishes first, clean
        await alice.wait_for(lambda s: (s["players"] and
                                        any(p["name"] == "Bob" and p["finished"]
                                            for p in s["players"])))
        await alice.finish(text, errors=3)   # Alice second, 3 errors

        final = await alice.wait_for(results)
        bobp = next(p for p in final["players"] if p["name"] == "Bob")
        alip = next(p for p in final["players"] if p["name"] == "Alice")
        check(bobp["place"] == 1, "first finisher gets place 1")
        check(alip["place"] == 2, "second finisher gets place 2")
        check(bobp["finished"] and alip["finished"], "both marked finished")
        check(bobp["wpm"] > 0, "winner has a positive WPM")
        check(abs(bobp["acc"] - 100.0) < 0.01, "clean typist has 100% accuracy")
        check(alip["acc"] < 100.0, "typist with errors has <100% accuracy")
        check(bobp["finish_time"] is not None, "finish time recorded")
        await alice.close()
        await bob.close()


async def scenario_solo_and_replay():
    print("scenario: solo race then replay with a different passage")
    async with Harness() as h:
        solo = Bot("Solo")
        await solo.start(h.uri, TOKEN)
        await solo.ready(True)
        s1 = await solo.wait_for(racing)
        text1 = s1["text"]
        await solo.finish(text1)
        r1 = await solo.wait_for(results)
        check(solo.player("Solo")["place"] == 1, "solo racer wins")

        # replay
        await solo.ready(True)
        s2 = await solo.wait_for(lambda s: racing(s) and s["text"] != text1)
        check(s2["text"] != text1, "replay uses a different passage")
        await solo.finish(s2["text"])
        await solo.wait_for(results)
        check(solo.player("Solo")["finished"], "replay completes")
        await solo.close()


async def scenario_name_dedup():
    print("scenario: duplicate names are disambiguated")
    async with Harness() as h:
        bots = [Bot("Sam") for _ in range(3)]
        await bots[0].start(h.uri, TOKEN)
        await bots[1].start(h.uri)
        await bots[2].start(h.uri)
        names = {b.name for b in bots}
        check(names == {"Sam", "Sam (2)", "Sam (3)"},
              f"names de-duplicated -> {sorted(names)}")
        for b in bots:
            await b.close()


async def scenario_admin_force_start():
    print("scenario: only the admin can force-start")
    async with Harness() as h:
        admin = Bot("Boss")
        peon = Bot("Peon")
        await admin.start(h.uri, TOKEN)
        await peon.start(h.uri)

        # Non-admin force-start must be ignored (nobody is ready).
        await peon.send({"type": P.C_START})
        await asyncio.sleep(0.3)
        check(admin.latest["phase"] == P.PHASE_LOBBY,
              "non-admin force-start is ignored")

        # Admin force-start works even though nobody readied up.
        await admin.send({"type": P.C_START})
        state = await admin.wait_for(racing)
        racers = [p for p in state["players"] if p["in_race"]]
        check(len(racers) == 2, "force-start enrolls all connected players")
        await admin.close()
        await peon.close()


async def scenario_spectator_join_midrace():
    print("scenario: joining mid-race makes you a spectator")
    async with Harness() as h:
        a = Bot("Racer1")
        b = Bot("Racer2")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.ready(True)
        await b.ready(True)
        state = await a.wait_for(racing)
        text = state["text"]

        late = Bot("Latecomer")
        await late.start(h.uri)
        late_state = await late.wait_for(lambda s: s["players"] and
                                         any(p["name"] == "Latecomer" for p in s["players"]))
        lp = next(p for p in late_state["players"] if p["name"] == "Latecomer")
        check(not lp["in_race"], "latecomer is not in the current race")

        await a.finish(text)
        await b.finish(text)
        final = await a.wait_for(results)
        in_race_names = {p["name"] for p in final["players"] if p["in_race"]}
        check(in_race_names == {"Racer1", "Racer2"},
              "only original racers are scored")
        for bot in (a, b, late):
            await bot.close()


async def scenario_disconnect_midrace():
    print("scenario: a disconnect mid-race does not hang the game")
    async with Harness() as h:
        a = Bot("Stayer")
        b = Bot("Quitter")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.ready(True)
        await b.ready(True)
        state = await a.wait_for(racing)
        text = state["text"]

        await b.close()                       # Quitter bails mid-race
        await a.finish(text)                  # Stayer finishes alone
        final = await a.wait_for(results, timeout=8)
        names = {p["name"] for p in final["players"]}
        check("Quitter" not in names, "disconnected player removed from roster")
        check(a.player("Stayer")["place"] == 1, "remaining racer is scored")
        await a.close()


async def scenario_reject_bad_auth():
    print("scenario: a non-auth first message is rejected")
    async with Harness() as h:
        ws = await connect(h.uri)
        await ws.send(P.encode({"type": P.C_PROGRESS, "pos": 1}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = P.decode(raw)
        check(msg.get("type") == P.S_AUTH_FAIL, "server rejects a non-auth opener")
        await ws.close()


async def scenario_malformed_input_survives():
    print("scenario: garbage messages don't drop a player or break the race")
    async with Harness() as h:
        a = Bot("A")
        b = Bot("B")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.ready(True)
        await b.ready(True)
        state = await a.wait_for(racing)
        text = state["text"]

        # Hostile / buggy frames that used to crash the handler coroutine.
        await b.send({"type": "progress", "pos": "abc", "errors": None,
                      "keystrokes": [1, 2]})
        await b.send({"type": "progress", "pos": 1e500})   # -> inf
        await b.send({"type": "progress"})                  # missing fields
        await b.send({"type": "totally-unknown"})
        await b.ws.send('"a bare json string"')             # non-dict frame
        await b.ws.send("12345")                            # non-dict frame
        await asyncio.sleep(0.3)

        bp = a.player("B")
        check(bp is not None and bp["connected"],
              "garbage input does not drop the player")
        await b.finish(text)
        await a.finish(text)
        final = await a.wait_for(results)
        check(all(p["finished"] for p in final["players"]),
              "race still completes after garbage input")
        await a.close()
        await b.close()


async def scenario_non_dict_auth_rejected():
    print("scenario: a non-dict JSON auth frame is rejected, not crashed")
    async with Harness() as h:
        ws = await connect(h.uri)
        await ws.send("5")  # valid JSON, but not an object
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        check(P.decode(raw).get("type") == P.S_AUTH_FAIL,
              "non-dict auth gets a clean failure reply")
        await ws.close()


async def scenario_place_renumber_on_disconnect():
    print("scenario: leader disconnecting renumbers the remaining places")
    async with Harness() as h:
        a, b, c = Bot("A"), Bot("B"), Bot("C")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await c.start(h.uri)
        for bot in (a, b, c):
            await bot.ready(True)
        state = await a.wait_for(racing)
        text = state["text"]

        await a.finish(text)
        await b.wait_for(lambda s: any(p["name"] == "A" and p["finished"]
                                       for p in s["players"]))
        await b.finish(text)
        await b.wait_for(lambda s: any(p["name"] == "B" and p["finished"]
                                       for p in s["players"]))
        await c.finish(text)
        await b.wait_for(results)
        check(b.player("A")["place"] == 1 and b.player("B")["place"] == 2
              and b.player("C")["place"] == 3, "initial places 1,2,3")

        await a.close()  # the leader leaves
        await b.wait_for(lambda s: all(p["name"] != "A" for p in s["players"])
                         and any(p["name"] == "B" and p["place"] == 1
                                 for p in s["players"]))
        check(b.player("B")["place"] == 1, "second place is renumbered to 1")
        check(b.player("C")["place"] == 2, "third place is renumbered to 2")
        await b.close()
        await c.close()


async def scenario_register_login_persist():
    print("scenario: register, log out, log back in (account persists)")
    async with Harness() as h:
        a = Bot("Acct")
        ok = await a.register(h.uri, "speedy_sam", "hunter2", token=TOKEN)
        check(ok and a.account == "speedy_sam" and not a.is_guest,
              "registration succeeds and is a real account")
        await a.close()

        # duplicate registration is rejected
        dup = Bot("Dup")
        ok2 = await dup.register(h.uri, "speedy_sam", "other")
        check(not ok2 and "taken" in (dup.auth_error or ""),
              "duplicate username is rejected")
        await dup.close()

        # wrong password rejected, correct password accepted
        bad = Bot("Bad")
        ok3 = await bad.login(h.uri, "speedy_sam", "wrong")
        check(not ok3 and bad.auth_error, "wrong password is rejected")
        await bad.close()

        good = Bot("Good")
        ok4 = await good.login(h.uri, "speedy_sam", "hunter2")
        check(ok4 and good.account == "speedy_sam", "correct password logs in")
        await good.close()


async def scenario_reconnection_takeover():
    print("scenario: re-logging into an account takes over the live session")
    async with Harness() as h:
        one = Bot("One")
        await one.register(h.uri, "dupe_user", "pw1234", token=TOKEN)
        await one.wait_for(lambda s: any(p["name"] == "dupe_user"
                                         for p in s["players"]))
        two = Bot("Two")
        ok = await two.login(h.uri, "dupe_user", "pw1234")
        check(ok and two.account == "dupe_user",
              "second login succeeds (takeover, not rejected)")
        # the original connection is closed by the server with CLOSE_REPLACED
        await asyncio.sleep(0.3)
        check(one.closed_code == P.CLOSE_REPLACED,
              "original connection is evicted with the 'replaced' close code")
        # the account appears exactly once in the roster after takeover
        snap = await two.wait_for(lambda s: any(p["name"] == "dupe_user"
                                                for p in s["players"]))
        same = [p for p in snap["players"] if p["name"] == "dupe_user"]
        check(len(same) == 1, "account appears once after takeover")
        await two.close()


async def scenario_stats_and_leaderboard():
    print("scenario: race results update stats and the leaderboard")
    async with Harness() as h:
        fast = Bot("Fast")
        slow = Bot("Slow")
        await fast.register(h.uri, "fast_fred", "pw1234", token=TOKEN)
        await slow.register(h.uri, "slow_sue", "pw1234")
        check(fast.stats["races"] == 0, "new account starts with zero races")

        await fast.ready(True)
        await slow.ready(True)
        state = await fast.wait_for(racing)
        text = state["text"]

        await fast.finish(text, errors=0)     # Fast wins, clean
        await slow.wait_for(lambda s: any(p["name"] == "fast_fred" and p["finished"]
                                          for p in s["players"]))
        await slow.finish(text, errors=5)     # Slow second, with errors
        await fast.wait_for(results)

        # Stats are persisted in the store.
        fs = h.store.stats_for("fast_fred")
        ss = h.store.stats_for("slow_sue")
        check(fs["races_played"] == 1 and fs["races_won"] == 1,
              "winner: 1 race played, 1 won")
        check(ss["races_played"] == 1 and ss["races_won"] == 0,
              "runner-up: 1 race played, 0 won")
        check(fs["best_wpm"] > 0, "winner has a recorded best WPM")
        check(abs(fs["best_accuracy"] - 100.0) < 0.01,
              "clean winner has 100% best accuracy")

        # Snapshot exposes compact stats for the lobby UI.
        fp = next(p for p in fast.latest["players"] if p["name"] == "fast_fred")
        check(fp["stats"] and fp["stats"]["races"] == 1,
              "snapshot carries per-player stats")

        # Leaderboard ranks by best WPM, winner on top.
        rows = await fast.request_leaderboard("best_wpm")
        check(len(rows) == 2, "both ranked players appear on the leaderboard")
        check(rows[0]["username"] == "fast_fred",
              "leaderboard is ordered by best WPM (winner first)")
        await fast.close()
        await slow.close()


async def scenario_guest_no_stats():
    print("scenario: guests play but are not persisted")
    async with Harness() as h:
        g = Bot("Ghost")
        await g.start(h.uri, TOKEN)   # guest
        check(g.is_guest and g.account is None, "guest has no account")
        await g.ready(True)
        state = await g.wait_for(racing)
        await g.finish(state["text"])
        await g.wait_for(results)
        rows = await g.request_leaderboard()
        check(rows == [], "guest does not appear on the leaderboard")
        gp = g.player("Ghost")
        check(gp["stats"] is None and gp["is_guest"], "guest snapshot has no stats")
        await g.close()


async def scenario_config_and_timed_mode():
    print("scenario: host config + TIMED mode (deadline-ranked by chars)")
    async with Harness() as h:
        admin = Bot("Admin")
        peon = Bot("Peon")
        await admin.start(h.uri, TOKEN)
        await peon.start(h.uri)

        # C_CONFIG: admin-only field validation
        await admin.config(mode="timed", length="short", category="quotes",
                           time_limit=15)
        await admin.wait_for(lambda s: s.get("config", {}).get("mode") == "timed")
        cfg = admin.latest["config"]
        check(cfg["mode"] == "timed" and cfg["length"] == "short"
              and cfg["category"] == "quotes" and cfg["time_limit"] == 15,
              "admin config is applied and broadcast")
        await admin.config(time_limit=999)   # invalid -> ignored
        await asyncio.sleep(0.1)
        check(admin.latest["config"]["time_limit"] == 15,
              "out-of-range config value is ignored")
        await peon.config(mode="classic")    # non-admin -> ignored
        await asyncio.sleep(0.1)
        check(admin.latest["config"]["mode"] == "timed",
              "non-admin config is ignored")

        # shrink the deadline for a fast test, then force-start
        h.gs.config["time_limit"] = 1
        await admin.send({"type": P.C_START})
        state = await admin.wait_for(racing)
        check(state.get("mode") == "timed" and "time_left" in state,
              "timed race exposes mode + time_left")
        await admin.progress(90)             # admin types more chars
        await peon.progress(30)
        final = await admin.wait_for(results, timeout=5)
        ap = next(p for p in final["players"] if p["name"] == "Admin")
        pp = next(p for p in final["players"] if p["name"] == "Peon")
        check(not ap["finished"] and not pp["finished"],
              "nobody 'finishes' a timed race")
        check(ap["place"] == 1 and pp["place"] == 2,
              "timed race ranks by chars typed")
        await admin.close()
        await peon.close()


async def scenario_survival_mode():
    print("scenario: SURVIVAL sudden-death (typo eliminates)")
    async with Harness() as h:
        a = Bot("Alive")
        b = Bot("Doomed")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        h.gs.config["mode"] = "survival"
        h.gs.config["lives"] = 1
        await a.send({"type": P.C_START})
        state = await a.wait_for(racing)
        text = state["text"]
        check(state["players"][0]["lives"] == 1, "survival exposes lives")

        await b.progress(8, errors=1)        # a typo with 1 life => eliminated
        await a.wait_for(lambda s: any(p["name"] == "Doomed" and p["eliminated"]
                                       for p in s["players"]))
        await a.finish(text)                 # survivor finishes
        final = await a.wait_for(results)
        ap = next(p for p in final["players"] if p["name"] == "Alive")
        bp = next(p for p in final["players"] if p["name"] == "Doomed")
        check(bp["eliminated"] and bp["place"] == 2, "eliminated player ranks last")
        check(ap["place"] == 1, "survivor wins")
        await a.close()
        await b.close()


async def scenario_anticheat_clamp():
    print("scenario: anti-cheat clamps an impossible instant finish")
    saved = server_mod.MAX_CPS
    server_mod.MAX_CPS = 25.0     # realistic ~300 WPM ceiling for this test
    try:
        async with Harness() as h:
            cheat = Bot("Cheater")
            await cheat.start(h.uri, TOKEN)
            await cheat.ready(True)
            state = await cheat.wait_for(racing)
            n = state["text_len"]
            await cheat.progress(n)          # claim the whole passage instantly
            await asyncio.sleep(0.2)
            me = cheat.player("Cheater")
            check(me["pos"] < n and me["pos"] <= server_mod.ANTICHEAT_GRACE + 8,
                  f"instant pos is clamped ({me['pos']} << {n})")
            check(me["flagged"], "implausible progress is flagged")
            check(not me["finished"] and cheat.latest["phase"] == P.PHASE_RACING,
                  "cheater does not finish instantly")
            await cheat.close()
    finally:
        server_mod.MAX_CPS = saved


async def scenario_chat():
    print("scenario: lobby chat with sanitization + rate limit")
    async with Harness() as h:
        a = Bot("Ann")
        b = Bot("Bob")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.chat("hello everyone")
        await b.wait_for(lambda s: any(c.get("text") == "hello everyone"
                                       for c in s.get("chat", [])))
        check(True, "chat message reaches other players")
        sysmsgs = [c for c in b.latest.get("chat", []) if c.get("kind") == "system"]
        check(any("joined" in c["text"] for c in sysmsgs), "join system message present")
        # ANSI/control characters are stripped
        await asyncio.sleep(0.6)
        await a.chat("x\x1b[31mY\x07Z")
        await b.wait_for(lambda s: any(c.get("name") == "Ann" and "Y" in c.get("text", "")
                                       and "\x1b" not in c.get("text", "")
                                       for c in s.get("chat", []) if c.get("kind") == "user"))
        check(True, "control characters are stripped from chat")
        # rate limit: a second immediate message is dropped
        await asyncio.sleep(0.6)              # clear the previous send's cooldown
        await a.chat("first_fast")
        await a.chat("second_fast")
        await asyncio.sleep(0.2)
        texts_seen = [c["text"] for c in b.latest.get("chat", [])]
        check("first_fast" in texts_seen and "second_fast" not in texts_seen,
              "rapid second chat is rate-limited")
        await a.close()
        await b.close()


async def scenario_profile_and_history():
    print("scenario: profile + match history request/response")
    async with Harness() as h:
        a = Bot("A")
        g = Bot("G")
        await a.register(h.uri, "profiler", "pw1234", token=TOKEN)
        await g.start(h.uri)                 # a guest
        await a.ready(True)
        await g.ready(True)
        state = await a.wait_for(racing)
        await a.finish(state["text"])
        await g.finish(state["text"])
        await a.wait_for(results)

        prof = await a.request_profile()     # own profile
        check(prof["found"] and prof["stats"] and prof["stats"]["races_played"] == 1,
              "own profile returns full stats")
        check("salt" not in str(prof) and "hash" not in prof.get("stats", {}),
              "profile never leaks password material")
        hist = await a.request_history()
        check(len(hist) == 1 and hist[0]["mode"] == "classic",
              "match history records the race")
        # guest profile via target id
        gid = next(p["id"] for p in a.latest["players"] if p["name"] == "G")
        gprof = await a.request_profile(gid)
        check(gprof["found"] and gprof["is_guest"] and gprof["stats"] is None,
              "guest profile is found but has no saved stats")
        await a.close()
        await g.close()


async def scenario_leaderboard_metrics():
    print("scenario: leaderboard cycles metrics and scopes by mode")
    async with Harness() as h:
        a = Bot("A")
        b = Bot("B")
        await a.register(h.uri, "winner_w", "pw1234", token=TOKEN)
        await b.register(h.uri, "loser_l", "pw1234")
        await a.ready(True)
        await b.ready(True)
        state = await a.wait_for(racing)
        await a.finish(state["text"])
        await b.wait_for(lambda s: any(p["name"] == "winner_w" and p["finished"]
                                       for p in s["players"]))
        await b.finish(state["text"])
        await a.wait_for(results)

        wins = await a.request_leaderboard(metric="races_won")
        check(wins and wins[0]["username"] == "winner_w",
              "leaderboard sorts by wins")
        bymode = await a.request_leaderboard(metric="best_wpm", mode="classic")
        check(len(bymode) == 2, "per-mode leaderboard scopes to classic")
        empty = await a.request_leaderboard(metric="best_wpm", mode="timed")
        check(empty == [], "per-mode leaderboard is empty for an unplayed mode")
        await a.close()
        await b.close()


async def scenario_kick():
    print("scenario: host kicks a player; kicked account can't rejoin")
    async with Harness() as h:
        admin = Bot("Boss")
        victim = Bot("Victim")
        await admin.start(h.uri, TOKEN)
        await victim.register(h.uri, "victim_v", "pw1234")
        await admin.wait_for(lambda s: any(p["name"] == "victim_v"
                                           for p in s["players"]))
        # non-admin kick is ignored
        vid = next(p["id"] for p in admin.latest["players"] if p["name"] == "victim_v")
        await victim.kick(admin.id)
        await asyncio.sleep(0.2)
        check(any(p["id"] == admin.id for p in victim.latest["players"]),
              "non-admin kick is ignored")
        # admin kick removes the victim
        await admin.kick(vid)
        await asyncio.sleep(0.3)
        check(victim.closed_code == P.CLOSE_KICKED, "victim closed with kicked code")
        check(all(p["name"] != "victim_v" for p in admin.latest["players"]),
              "victim removed from the roster")
        # kicked account can't immediately rejoin
        again = Bot("Victim2")
        ok = await again.login(h.uri, "victim_v", "pw1234")
        check(not ok and "removed" in (again.auth_error or ""),
              "kicked account is refused re-login")
        await admin.close()
        await again.close()


async def scenario_timed_growth_bounded():
    print("scenario: TIMED text growth is bounded by the anti-cheat clamp")
    saved = server_mod.MAX_CPS
    server_mod.MAX_CPS = 25.0
    try:
        async with Harness() as h:
            a = Bot("A")
            await a.start(h.uri, TOKEN)
            h.gs.config["mode"] = "timed"
            h.gs.config["time_limit"] = 120     # long deadline; we end via cleanup
            await a.send({"type": P.C_START})
            await a.wait_for(racing)
            base_len = len(h.gs.text)
            for _ in range(40):                 # spam impossible positions/errors
                await a.progress(10 ** 9, errors=10 ** 9)
                await asyncio.sleep(0.004)
            await asyncio.sleep(0.2)
            grown = len(h.gs.text)
            check(grown <= base_len + 600,
                  f"timed text stays bounded ({base_len}->{grown}, not unbounded)")
            me = a.player("A")
            check(me["pos"] <= 60, "pos stays clamped despite huge raw_pos")
            check(me["errors"] <= 60, "errors stay clamped despite huge raw value")
            check(me["flagged"], "the cheating client is flagged")
            await a.close()
    finally:
        server_mod.MAX_CPS = saved


async def scenario_guest_name_sanitized():
    print("scenario: guest names are stripped of ANSI/control sequences")
    async with Harness() as h:
        ws = await connect(h.uri)
        await ws.send(P.encode({"type": P.C_GUEST, "name": "ev\x1b[31mil\x07",
                                "version": P.PROTOCOL_VERSION}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = P.decode(raw)
        check(msg.get("type") == P.S_AUTH_OK, "guest with control chars still admitted")
        check("\x1b" not in msg.get("name", "") and "\x07" not in msg.get("name", ""),
              f"escape/control chars stripped from guest name -> {msg.get('name')!r}")
        await ws.close()


async def scenario_v1_migration():
    print("scenario: a v1 accounts file migrates without corruption")
    fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_v1_")
    os.close(fd)
    try:
        v1 = {"users": {"oldie": {
            "username": "oldie", "salt": os.urandom(16).hex(), "hash": "x" * 64,
            "stats": {"races_played": 10, "races_won": 3, "best_wpm": 80.0,
                      "avg_wpm": 75.0, "best_accuracy": 95.0, "avg_accuracy": 90.0,
                      "total_time": 100.0, "total_chars": 500,
                      "created": 1.0, "last_played": 2.0}}}}
        with open(path, "w") as f:
            json.dump(v1, f)
        store = AccountStore(path)
        s = store.stats_for("oldie")
        check(s["races_played"] == 10 and s["best_wpm"] == 80.0,
              "v1 stats are preserved through migration")
        check("by_mode" in s and "history" in s and "achievements" in s,
              "new schema fields are added")
        check(s["wpm_sumsq"] > 0,
              "consistency variance baseline is backfilled for old accounts")
        # a malformed scope entry must not crash the leaderboard
        store.users["oldie"]["stats"]["by_mode"] = {"classic": {}}
        rows = store.leaderboard("best_wpm", mode="classic")
        check(rows == [], "malformed by_mode scope is handled, not crashed")
    finally:
        os.path.exists(path) and os.unlink(path)


async def scenario_client_instant_race_reset():
    print("scenario: client resets typing on an instant (countdown=0) race start")
    from client import GameClient
    import os as _os
    import shutil as _sh
    d = tempfile.mkdtemp(prefix="typeracer_cli_")
    old = _os.environ.get("TYPERACER_CONFIG_DIR")
    _os.environ["TYPERACER_CONFIG_DIR"] = d
    try:
        gc = GameClient("ws://127.0.0.1:1")
        gc.my_id = 1
        # pretend we just finished the previous race
        gc.finished_local = True
        gc.t_pos = 99
        gc.t_errors = 5
        gc.prev_phase = P.PHASE_RESULTS
        # an instant countdown jumps straight to RACING with NO countdown frame
        gc._on_state({"phase": P.PHASE_RACING, "text": "a brand new passage here",
                      "players": [{"id": 1, "in_race": True}], "mode": "classic"})
        check(gc.finished_local is False and gc.t_pos == 0 and gc.t_errors == 0,
              "RESULTS->RACING with no countdown resets typing state")
        check(gc.text == "a brand new passage here", "new race text is adopted")
        # the normal countdown path still resets too
        gc.finished_local = True
        gc.t_pos = 50
        gc.prev_phase = P.PHASE_LOBBY
        gc._on_state({"phase": P.PHASE_COUNTDOWN, "text": "another passage of text",
                      "players": [{"id": 1, "in_race": True}], "mode": "classic"})
        check(gc.finished_local is False and gc.t_pos == 0,
              "normal COUNTDOWN transition still resets typing")
    finally:
        if old is None:
            _os.environ.pop("TYPERACER_CONFIG_DIR", None)
        else:
            _os.environ["TYPERACER_CONFIG_DIR"] = old
        _sh.rmtree(d, ignore_errors=True)


async def scenario_grace_timed_eviction_consistency():
    print("scenario: a timed deadline ending a race during grace stays consistent")
    saved = server_mod.RECONNECT_GRACE
    server_mod.RECONNECT_GRACE = 30.0    # keep the dropped racer in grace past the deadline
    try:
        async with Harness() as h:
            h.gs.config["mode"] = "timed"
            h.gs.config["time_limit"] = 1
            a = Bot("A")
            b = Bot("B")
            await a.register(h.uri, "tg_a", "pw1234", token=TOKEN)
            await b.register(h.uri, "tg_b", "pw1234")
            await a.send({"type": P.C_START})
            await a.wait_for(racing)
            await a.progress(40)
            await b.progress(20)
            await b.close()                  # B drops mid-race -> grace hold (timed, unfinished)
            final = await a.wait_for(results, timeout=6)
            names = [p["name"] for p in final["players"]]
            check("tg_b" not in names,
                  "an in-grace racer is evicted at race end (no place=None ghost)")
            ap = next(p for p in final["players"] if p["name"] == "tg_a")
            check(ap["place"] == 1, "the remaining racer is placed correctly")
            check(all(p["place"] is not None for p in final["players"] if p["in_race"]),
                  "no scored racer is left with a null place")
    finally:
        server_mod.RECONNECT_GRACE = saved


async def scenario_celebration_not_lost_without_targets():
    print("scenario: a no-recipient broadcast does not consume the one-shot celebration")
    async with Harness() as h:
        h.gs.loop = asyncio.get_running_loop()
        h.gs.phase = P.PHASE_RESULTS
        h.gs._celebration = {"winner": "Ghost", "wpm": 80, "flags": ["flawless"],
                             "is_bot": False}
        h.gs._pending_announcements = [{"kind": "badge", "name": "Ghost", "badge": "X"}]
        await h.gs._broadcast_now()          # no connected clients -> zero targets
        check(h.gs._celebration is not None and len(h.gs._pending_announcements) == 1,
              "one-shot celebration/announcements survive a zero-recipient broadcast")


async def scenario_session_guest_pruned():
    print("scenario: a departed guest is pruned from the session scoreboard")
    async with Harness() as h:
        admin = Bot("Admin")
        guest = Bot("Guest")
        await admin.register(h.uri, "host_h2", "pw1234", token=TOKEN)
        await guest.start(h.uri)
        await admin.ready(True)
        await guest.ready(True)
        st = await admin.wait_for(racing)
        await admin.finish(st["text"])
        await guest.finish(st["text"], errors=3)
        r = await admin.wait_for(results)
        names = {e["name"] for e in r["session"]["standings"]}
        check("Guest" in names, "guest appears on the session board after racing")
        await guest.close()
        snap = await admin.wait_for(
            lambda s: all(e["name"] != "Guest"
                          for e in s.get("session", {}).get("standings", [])))
        check(any(e["name"] == "host_h2" for e in snap["session"]["standings"]),
              "the account's session standing is retained after the guest leaves")
        await admin.close()


async def scenario_tier_and_rating_fixes():
    print("scenario: tier-achievement thresholds + rating seeding (store/pure)")
    import progression as pr
    # rating seeding: None seeds; a real 0.0 blends instead of re-seeding
    check(pr.update_rating(None, 60, 100) == 60.0, "None seeds the skill rating")
    check(abs(pr.update_rating(0.0, 60, 100) - 15.0) < 0.01,
          "a genuine 0.0 rating blends (smoothing not lost)")
    fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_tier_")
    os.close(fd)
    os.unlink(path)
    try:
        store = AccountStore(path)
        store.create("gold_g", "pw1234")
        store.record_race("gold_g", net_wpm=60.0, raw_wpm=66.0, accuracy=100.0,
                          seconds=10.0, chars=60, keystrokes=60, errors=0,
                          won=True, place=1, racers=1)
        sg = store.stats_for("gold_g")
        check(sg["tier"] == "Gold" and sg["tier_index"] == 2,
              f"a 60-rating account is Gold (tier={sg['tier']})")
        check("ranked_up" in sg["achievements"],
              "Gold tier unlocks Climbing (threshold >= Gold, not Diamond)")
        check("elite" not in sg["achievements"],
              "Elite is not unlocked merely at Gold")
        store.create("dia_d", "pw1234")
        store.record_race("dia_d", net_wpm=95.0, raw_wpm=99.0, accuracy=100.0,
                          seconds=10.0, chars=95, keystrokes=95, errors=0,
                          won=True, place=1, racers=1)
        sd = store.stats_for("dia_d")
        check(sd["tier_index"] >= 4 and "elite" in sd["achievements"],
              f"Diamond+ unlocks Elite (tier={sd['tier']})")
    finally:
        os.path.exists(path) and os.unlink(path)


async def scenario_wpm_timeline_splits():
    print("scenario: results carry a per-racer WPM timeline (splits)")
    async with Harness() as h:
        h.gs.config["length"] = "long"
        a = Bot("A")
        await a.start(h.uri, TOKEN)
        await a.ready(True)
        st = await a.wait_for(racing)
        text = st["text"]
        # type in paced steps so several WPM samples accrue (>0.5s apart)
        for frac in (0.2, 0.45, 0.7):
            await a.progress(int(len(text) * frac))
            await asyncio.sleep(0.55)
        await a.progress(len(text))          # finish
        r = await a.wait_for(results)
        ap = next(p for p in r["players"] if p["name"] == "A")
        check(ap.get("splits") and len(ap["splits"]) >= 2,
              f"results carry a WPM timeline ({len(ap.get('splits') or [])} samples)")
        check(all(isinstance(v, (int, float)) for v in ap["splits"]),
              "timeline samples are numeric")
        # splits are results-only: a racing snapshot must not carry them
        await a.ready(True)
        s2 = await a.wait_for(racing)
        rp = next(p for p in s2["players"] if p["name"] == "A")
        check(rp.get("splits") is None, "splits are omitted from racing snapshots")
        await a.close()


async def scenario_units_and_theme_helpers():
    print("scenario: client units formatting + theme SGR remap (pure)")
    import terminal as term
    # theme remap: green(32) -> bright blue(94); structural codes untouched
    src = term.FG_GREEN + "x" + term.RESET + term.BOLD + "y"
    remapped = term.remap_sgr(src, {"32": "94"})
    check("\x1b[94m" in remapped and "\x1b[32m" not in remapped,
          "remap_sgr swaps green for bright blue")
    check(term.BOLD in remapped and term.RESET in remapped,
          "remap_sgr leaves structural SGR (bold/reset) intact")
    check(term.remap_sgr("plain", {"32": "94"}) == "plain",
          "remap_sgr is a no-op on text without SGR")
    # units conversion via a throwaway client instance (no network/TTY needed)
    from client import GameClient
    import os as _os
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="typeracer_unit_")
    old = _os.environ.get("TYPERACER_CONFIG_DIR")
    _os.environ["TYPERACER_CONFIG_DIR"] = d
    try:
        gc = GameClient("ws://127.0.0.1:1")
        gc.units = "wpm"
        check(gc._fmt_speed(60) == "60" and gc._table_speed(60) == 60,
              "WPM units render unchanged")
        gc.units = "cpm"
        check(gc._fmt_speed(60) == "300" and gc._table_speed(60) == 300,
              "CPM units multiply by five")
        gc.units = "both"
        check(gc._fmt_speed(60) == "60w/300c" and gc._table_speed(60) == 60,
              "both shows WPM/CPM and tables use the WPM primary")
        check(gc._sparkline([10, 20, 30, 40]) and len(gc._sparkline([10, 20, 30])) == 3,
              "sparkline renders one glyph per sample")
        check(gc._sparkline([]) == "", "empty sparkline is blank")
    finally:
        if old is None:
            _os.environ.pop("TYPERACER_CONFIG_DIR", None)
        else:
            _os.environ["TYPERACER_CONFIG_DIR"] = old
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


async def scenario_emotes():
    print("scenario: quick-chat emotes (lobby log + mid-race bubble + rate limit)")
    async with Harness() as h:
        a = Bot("A")
        b = Bot("B")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.emote("gg")
        await b.wait_for(lambda s: any(c.get("kind") == "emote"
                                       and c.get("text") == "gg!"
                                       for c in s.get("chat", [])))
        check(True, "an emote reaches the lobby chat log")
        # invalid code is ignored
        await a.emote("definitely_not_a_code")
        await asyncio.sleep(0.1)
        check(sum(1 for c in b.latest.get("chat", []) if c.get("kind") == "emote") == 1,
              "an unknown emote code is ignored")
        # rate limit: a rapid second emote is dropped
        await a.emote("nice")
        await asyncio.sleep(0.1)
        codes = [c.get("text") for c in b.latest.get("chat", [])
                 if c.get("kind") == "emote"]
        check("nice!" not in codes, "a rapid second emote is rate-limited")
        # mid-race: emote shows as a transient per-player bubble, not chat
        await a.ready(True)
        await b.ready(True)
        st = await a.wait_for(racing)
        await a.finish(st["text"])           # A finishes; now a non-typist
        await asyncio.sleep(1.6)             # clear the emote cooldown (> EMOTE_RATE)
        await a.emote("wow")
        snap = await b.wait_for(lambda s: any(p["name"] == "A"
                                              and p.get("recent_emote") == "wow"
                                              for p in s["players"]))
        check(True, "a finished player's emote shows as a racetrack bubble")
        await a.close()
        await b.close()


async def scenario_player_color():
    print("scenario: accounts pick a persistent accent color; guests cannot")
    async with Harness() as h:
        a = Bot("A")
        g = Bot("G")
        await a.register(h.uri, "color_c", "pw1234", token=TOKEN)
        await g.start(h.uri)
        await a.setcolor("green")
        snap = await g.wait_for(lambda s: any(p["name"] == "color_c"
                                              and p.get("color") == "green"
                                              for p in s["players"]))
        check(True, "chosen color appears on the player view")
        check(h.store.stats_for("color_c")["color"] == "green",
              "color is persisted to the account")
        await a.setcolor("chartreuse")       # invalid -> ignored
        await asyncio.sleep(0.1)
        check(h.store.stats_for("color_c")["color"] == "green",
              "an invalid color is rejected")
        # guests get a deterministic hash color and can't set one
        await g.setcolor("red")
        await asyncio.sleep(0.1)
        gp = next(p for p in g.latest["players"] if p["name"] == "G")
        check(gp.get("color") in P.PLAYER_COLORS,
              "guest has a deterministic accent color")
        await a.close()
        await g.close()


async def scenario_reconnect_grace():
    print("scenario: an accounted racer that drops mid-race keeps its slot")
    async with Harness() as h:
        h.gs.config["length"] = "long"       # don't finish before we test
        a = Bot("A")
        b = Bot("B")
        await a.register(h.uri, "drop_a", "pw1234", token=TOKEN)
        await b.register(h.uri, "stay_b", "pw1234")
        await a.ready(True)
        await b.ready(True)
        st = await a.wait_for(racing)
        await a.progress(40)
        await b.wait_for(lambda s: any(p["name"] == "drop_a" and p["pos"] >= 40
                                       for p in s["players"]))
        await a.close()                      # drop mid-race
        await asyncio.sleep(0.3)
        held = next((p for p in b.latest["players"] if p["name"] == "drop_a"), None)
        check(held is not None and not held["connected"],
              "dropped racer is held (not evicted) during grace")
        check(b.latest["phase"] == P.PHASE_RACING,
              "the race continues through the grace window")
        a2 = Bot("A2")
        ok = await a2.login(h.uri, "drop_a", "pw1234")
        check(ok, "the account reconnects within the grace window")
        snap = await a2.wait_for(lambda s: any(p["name"] == "drop_a" and p["connected"]
                                               for p in s["players"]))
        ap = next(p for p in snap["players"] if p["name"] == "drop_a")
        check(ap["pos"] >= 40 and ap["in_race"],
              f"position and race slot are preserved on reconnect (pos={ap['pos']})")
        await a2.close()
        await b.close()


async def scenario_grace_expiry():
    print("scenario: grace expiry evicts an absent racer and resolves the race")
    saved = server_mod.RECONNECT_GRACE
    server_mod.RECONNECT_GRACE = 0.4
    try:
        async with Harness() as h:
            h.gs.config["length"] = "long"
            a = Bot("A")
            b = Bot("B")
            await a.register(h.uri, "ghost_a", "pw1234", token=TOKEN)
            await b.register(h.uri, "real_b", "pw1234")
            await a.ready(True)
            await b.ready(True)
            st = await a.wait_for(racing)
            await a.progress(30)
            await a.close()
            await b.wait_for(lambda s: all(p["name"] != "ghost_a"
                                           for p in s["players"]), timeout=5)
            check(True, "an absent racer is evicted once grace expires")
            await b.finish(st["text"])
            await b.wait_for(results, timeout=8)
            check(b.player("real_b")["place"] == 1,
                  "the remaining racer is scored after eviction")
    finally:
        server_mod.RECONNECT_GRACE = saved


async def scenario_room_password():
    print("scenario: a room password gates joining")
    async with Harness(room_password="sekret") as h:
        bad = Bot("Bad")
        ok = await bad.start(h.uri, room_password="nope")
        check(not ok and "password" in (bad.auth_error or ""),
              "a wrong room password is refused")
        await bad.close()
        missing = Bot("Missing")
        ok2 = await missing.start(h.uri)
        check(not ok2, "a missing room password is refused")
        await missing.close()
        good = Bot("Good")
        ok3 = await good.start(h.uri, room_password="sekret")
        check(ok3, "the correct room password is admitted")
        await good.close()


async def scenario_max_players():
    print("scenario: max-players refuses extra humans (bots are exempt)")
    async with Harness(max_players=2) as h:
        a = Bot("A")
        b = Bot("B")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        c = Bot("C")
        ok = await c.start(h.uri)
        check(not ok and "full" in (c.auth_error or ""),
              "the third human is refused when max-players is 2")
        await c.close()
        # bots don't count against the human cap
        await a.add_bot("easy")
        snap = await a.wait_for(lambda s: any(p.get("is_bot") for p in s["players"]))
        check(any(p.get("is_bot") for p in snap["players"]),
              "a bot can still be added past the human cap")
        await a.close()
        await b.close()


async def scenario_unban():
    print("scenario: admin can un-ban a kicked account so it rejoins")
    async with Harness() as h:
        admin = Bot("Boss")
        victim = Bot("Victim")
        await admin.start(h.uri, TOKEN)
        await victim.register(h.uri, "banned_b", "pw1234")
        await admin.wait_for(lambda s: any(p["name"] == "banned_b"
                                           for p in s["players"]))
        vid = next(p["id"] for p in admin.latest["players"]
                   if p["name"] == "banned_b")
        await admin.kick(vid)
        await asyncio.sleep(0.3)
        bans = await admin.request_banlist()
        check("banned_b" in bans, "kicked account shows on the ban list")
        # a non-admin un-ban is ignored (the kicked victim's socket is gone, so
        # use a fresh guest connection to attempt it)
        guest = Bot("Guest")
        await guest.start(h.uri)
        await guest.unban("banned_b")
        await asyncio.sleep(0.2)
        bans2 = await admin.request_banlist()
        check("banned_b" in bans2, "a non-admin un-ban is ignored")
        # admin un-ban works; the account can rejoin
        await admin.unban("banned_b")
        await asyncio.sleep(0.2)
        again = Bot("Again")
        ok = await again.login(h.uri, "banned_b", "pw1234")
        check(ok and again.account == "banned_b",
              "the un-banned account can log in again")
        await admin.close()
        await guest.close()
        await again.close()


async def scenario_host_config_persistence():
    print("scenario: host config + ban list persist to disk and restore")
    import config_store as cs
    import modes as modes_mod
    fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_host_")
    os.close(fd)
    os.unlink(path)
    try:
        store = cs.HostConfigStore(path)
        cfg = modes_mod.default_config()
        cfg.update({"mode": "survival", "lives": 5, "countdown": 5,
                    "min_players": 2})
        store.save(cfg, {"baddie", "troll"})
        cfg2, banned = store.load()
        check(cfg2["mode"] == "survival" and cfg2["lives"] == 5
              and cfg2["countdown"] == 5 and cfg2["min_players"] == 2,
              "saved config is restored from disk")
        check(banned == {"baddie", "troll"}, "ban set is restored from disk")
        with open(path, "w") as f:
            f.write("{ not valid json")
        cfg3, banned3 = store.load()
        check(cfg3["mode"] == "classic" and banned3 == set(),
              "a corrupt host file falls back to defaults")
        # server integration: a live kick persists through the store
        async with Harness(host_store=store) as h:
            admin = Bot("Admin")
            v = Bot("V")
            await admin.start(h.uri, TOKEN)
            await v.register(h.uri, "persist_v", "pw1234")
            await admin.wait_for(lambda s: any(p["name"] == "persist_v"
                                               for p in s["players"]))
            vid = next(p["id"] for p in admin.latest["players"]
                       if p["name"] == "persist_v")
            await admin.kick(vid)
            await asyncio.sleep(0.3)
            _, banned4 = store.load()
            check("persist_v" in banned4, "a live kick is persisted to the host store")
            await admin.close()
    finally:
        os.path.exists(path) and os.unlink(path)


async def scenario_session_scoreboard():
    print("scenario: session points accumulate across races, reset on command")
    async with Harness() as h:
        a = Bot("A")
        b = Bot("B")
        await a.register(h.uri, "sess_a", "pw1234", token=TOKEN)
        await b.register(h.uri, "sess_b", "pw1234")
        await a.ready(True)
        await b.ready(True)
        st = await a.wait_for(racing)
        await a.finish(st["text"])           # A wins race 1
        await b.wait_for(lambda s: any(p["name"] == "sess_a" and p["finished"]
                                       for p in s["players"]))
        await b.finish(st["text"], errors=3)
        r1 = await a.wait_for(results)
        sess = r1.get("session", {})
        check(sess.get("race_no") == 1, "session race counter increments")
        st1 = {e["name"]: e for e in sess.get("standings", [])}
        check(st1["sess_a"]["points"] == 10 and st1["sess_b"]["points"] == 6,
              "points awarded by place (1st=10, 2nd=6)")
        # race 2: B wins
        await a.ready(True)
        await b.ready(True)
        st2 = await b.wait_for(racing)
        await b.finish(st2["text"])
        await a.wait_for(lambda s: any(p["name"] == "sess_b" and p["finished"]
                                       for p in s["players"]))
        await a.finish(st2["text"], errors=3)
        r2 = await a.wait_for(results)
        st2d = {e["name"]: e for e in r2["session"]["standings"]}
        check(st2d["sess_a"]["points"] == 16 and st2d["sess_b"]["points"] == 16,
              "points accumulate across races")
        check(r2["session"]["race_no"] == 2, "race counter reaches 2")
        await a.send({"type": P.C_SESSION_RESET})
        snap = await a.wait_for(lambda s: s.get("session", {}).get("race_no", 1) == 0)
        check(snap["session"]["standings"] == [],
              "admin reset clears the scoreboard")
        await a.close()
        await b.close()


async def scenario_celebration_banner():
    print("scenario: a one-shot win-celebration banner on results")
    async with Harness() as h:
        a = Bot("Champ")
        b = Bot("Rival")
        await a.start(h.uri, TOKEN)
        await b.start(h.uri)
        await a.ready(True)
        await b.ready(True)
        st = await a.wait_for(racing)
        await a.finish(st["text"], errors=0)        # flawless winner
        await b.wait_for(lambda s: any(p["name"] == "Champ" and p["finished"]
                                       for p in s["players"]))
        await b.finish(st["text"], errors=2)
        await a.wait_for(results)
        # force a couple more broadcasts; the banner must not recur
        await a.chat("gg")
        await asyncio.sleep(0.15)
        await b.ready(True)
        await asyncio.sleep(0.15)
        check(a.celebration_count == 1,
              f"celebration is one-shot (count={a.celebration_count})")
        check(a.celebration and a.celebration["winner"] == "Champ",
              "celebration names the winner")
        check("flawless" in a.celebration.get("flags", []),
              "flawless flag is set for a clean win")
        check(a.celebration.get("wpm", 0) > 0, "celebration carries the WPM")
        await a.close()
        await b.close()


async def scenario_flow_config_and_rerace():
    print("scenario: countdown/quick-start/min-players config + instant re-race")
    async with Harness() as h:
        a = Bot("Solo")
        await a.start(h.uri, TOKEN)
        await a.config(countdown=5, quick_start=True, min_players=2, rematch_secs=10)
        await a.wait_for(lambda s: s.get("config", {}).get("countdown") == 5)
        cfg = a.latest["config"]
        check(cfg["countdown"] == 5 and cfg["quick_start"] is True
              and cfg["min_players"] == 2 and cfg["rematch_secs"] == 10,
              "flow config fields validate and broadcast")
        await a.config(countdown=999, rematch_secs=7)     # invalid -> ignored
        await asyncio.sleep(0.1)
        check(a.latest["config"]["countdown"] == 5
              and a.latest["config"]["rematch_secs"] == 10,
              "out-of-range flow config is ignored")
        check(h.gs._effective_countdown() == 0,
              "quick-start makes a lone human's countdown instant")
        # min_players=2 holds a single ready human in the lobby
        await a.ready(True)
        await asyncio.sleep(0.3)
        check(a.latest["phase"] == P.PHASE_LOBBY,
              "min_players gate keeps a solo human in the lobby")
        await a.config(min_players=1)
        await a.ready(True)
        st = await a.wait_for(racing, timeout=5)
        await a.finish(st["text"])
        await a.wait_for(results)
        await a.send({"type": P.C_RERACE})
        st2 = await a.wait_for(racing, timeout=5)
        check(st2["phase"] == P.PHASE_RACING, "C_RERACE re-racks from results")
        await a.close()


async def scenario_bots_join_and_race():
    print("scenario: host adds AI bots that race, place, and stay off the board")
    async with Harness() as h:
        admin = Bot("Host")
        await admin.register(h.uri, "host_h", "pw1234", token=TOKEN)
        # a short passage so the slowest bot still finishes quickly
        h.gs.config["custom_text"] = "The quick brown fox jumped over!"
        await admin.add_bot("hard")
        await admin.add_bot("insane")
        snap = await admin.wait_for(
            lambda s: sum(1 for p in s["players"] if p.get("is_bot")) == 2)
        bots = [p for p in snap["players"] if p.get("is_bot")]
        check(len(bots) == 2 and all(p["ready"] for p in bots),
              "two bots join and are perpetually ready")
        check(all(p["is_guest"] is False and p["stats"] is None for p in bots),
              "bots are accountless")
        check(snap["phase"] == P.PHASE_LOBBY,
              "a room of bots + one un-ready human does not self-start")
        # readying the lone human triggers the start
        await admin.ready(True)
        state = await admin.wait_for(racing, timeout=5)
        racers = [p for p in state["players"] if p["in_race"]]
        check(len(racers) == 3, "human + 2 bots all enrolled")
        await admin.finish(state["text"])
        final = await admin.wait_for(results, timeout=15)
        placed = [p for p in final["players"] if p["place"]]
        check(len(placed) == 3, "all three racers get a placement")
        check(all(p["finished"] for p in final["players"]
                  if p["in_race"] and p.get("is_bot")),
              "bots finish the race under their own power")
        rows = await admin.request_leaderboard()
        check(len(rows) == 1 and rows[0]["username"] == "host_h",
              "only the human account is ranked; bots are excluded")
        await admin.close()


async def scenario_bot_difficulty_scaling():
    print("scenario: a higher-difficulty bot out-types a lower one")
    async with Harness() as h:
        admin = Bot("Host")
        await admin.start(h.uri, TOKEN)
        h.gs.config["length"] = "long"      # nobody finishes in the sample window
        await admin.add_bot("easy")
        await admin.add_bot("insane")
        await admin.ready(True)
        await admin.wait_for(racing, timeout=5)
        await asyncio.sleep(2.0)
        snap = admin.latest
        by_diff = {p["difficulty"]: p for p in snap["players"] if p.get("is_bot")}
        check(by_diff["insane"]["pos"] > by_diff["easy"]["pos"],
              f"insane bot outpaces easy bot "
              f"({by_diff['insane']['pos']} > {by_diff['easy']['pos']})")
        await admin.close()


async def scenario_bot_add_remove_limits():
    print("scenario: bots can be removed and are capped; only between races")
    async with Harness() as h:
        admin = Bot("Host")
        await admin.start(h.uri, TOKEN)
        for _ in range(3):
            await admin.add_bot("medium")
        await admin.wait_for(
            lambda s: sum(1 for p in s["players"] if p.get("is_bot")) == 3)
        await admin.remove_bot()            # remove the most recent
        snap = await admin.wait_for(
            lambda s: sum(1 for p in s["players"] if p.get("is_bot")) == 2)
        check(sum(1 for p in snap["players"] if p.get("is_bot")) == 2,
              "removing a bot leaves the rest")
        # a non-admin cannot add bots
        peon = Bot("Peon")
        await peon.start(h.uri)
        await peon.add_bot("hard")
        await asyncio.sleep(0.2)
        check(sum(1 for p in admin.latest["players"] if p.get("is_bot")) == 2,
              "non-admin add_bot is ignored")
        await admin.close()
        await peon.close()


async def scenario_progression_xp_level():
    print("scenario: XP/level + skill rating + personal-best detection (store-level)")
    import accounts as acc_mod
    fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_prog_")
    os.close(fd)
    os.unlink(path)
    try:
        store = AccountStore(path)
        store.create("racer_r", "pw1234")
        out1 = store.record_race("racer_r", net_wpm=50.0, raw_wpm=55.0, accuracy=96.0,
                                 seconds=12.0, chars=60, keystrokes=63, errors=3,
                                 won=True, place=1, racers=2, mode="classic",
                                 category="quotes", flawless=False)
        s = store.stats_for("racer_r")
        check(s["total_xp"] > 0, "xp accrues from a race")
        check(s["level"] >= 1 and s["skill_rating"] > 0,
              "level and skill rating are set")
        check(s["tier"] in [t[0] for t in __import__("progression").TIERS],
              "a valid tier is derived")
        check(out1["pbs"] == [], "the very first race is never a personal best")
        out2 = store.record_race("racer_r", net_wpm=80.0, raw_wpm=85.0, accuracy=99.0,
                                 seconds=10.0, chars=80, keystrokes=82, errors=1,
                                 won=True, place=1, racers=2, mode="classic",
                                 category="quotes", flawless=False)
        kinds = {pb["kind"] for pb in out2["pbs"]}
        check("WPM" in kinds and "accuracy" in kinds,
              f"beating prior bests yields PB callouts -> {sorted(kinds)}")
        check(isinstance(out2["levels"], list), "level-ups are reported as a list")
        # skill rating ordering: a faster, cleaner account ranks above a slower one
        store.create("slow_s", "pw1234")
        store.record_race("slow_s", net_wpm=30.0, raw_wpm=40.0, accuracy=85.0,
                          seconds=20.0, chars=30, keystrokes=40, errors=10,
                          won=False, place=2, racers=2)
        rows = store.leaderboard("skill_rating")
        check(rows[0]["username"] == "racer_r" and "tier" in rows[0],
              "skill_rating leaderboard ranks the stronger account first")
    finally:
        os.path.exists(path) and os.unlink(path)


async def scenario_day_streak():
    print("scenario: daily play streak counts consecutive days")
    import accounts as acc_mod
    fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_day_")
    os.close(fd)
    os.unlink(path)
    real_time = acc_mod.time.time
    DAY = 86400
    base = 1_700_000_000
    try:
        store = AccountStore(path)
        store.create("daily_d", "pw1234")
        plan = [(0, 1), (0, 1), (DAY, 2), (2 * DAY, 3), (10 * DAY, 1)]
        s = None
        for offset, expected in plan:
            acc_mod.time.time = (lambda b=base + offset: float(b))
            store.record_race("daily_d", net_wpm=40.0, raw_wpm=44.0, accuracy=95.0,
                              seconds=10.0, chars=40, keystrokes=42, errors=2,
                              won=False, place=2, racers=2)
            s = store.stats_for("daily_d")
            check(s["day_streak"] == expected,
                  f"day_streak after +{offset // DAY}d -> {s['day_streak']} (want {expected})")
        check(s["longest_day_streak"] == 3, "longest day streak is retained")
    finally:
        acc_mod.time.time = real_time
        os.path.exists(path) and os.unlink(path)


async def scenario_h2h_rivals():
    print("scenario: head-to-head rivalry records accumulate per opponent")
    async with Harness() as h:
        a = Bot("A")
        b = Bot("B")
        await a.register(h.uri, "rival_a", "pw1234", token=TOKEN)
        await b.register(h.uri, "rival_b", "pw1234")
        await a.ready(True)
        await b.ready(True)
        st = await a.wait_for(racing)
        await a.finish(st["text"])           # rival_a wins
        await b.wait_for(lambda s: any(p["name"] == "rival_a" and p["finished"]
                                       for p in s["players"]))
        await b.finish(st["text"], errors=4)
        await a.wait_for(results)
        sa = h.store.stats_for("rival_a")
        sb = h.store.stats_for("rival_b")
        check(sa["rivals"].get("rival_b", {}).get("w") == 1,
              "winner records 1 h2h win vs the opponent")
        check(sb["rivals"].get("rival_a", {}).get("l") == 1,
              "loser records 1 h2h loss vs the opponent")
        prof = await a.request_profile()
        check(any(r.get("name") == "rival_b" for r in prof.get("rivals", [])),
              "profile surfaces the rivalry")
        await a.close()
        await b.close()


async def scenario_skill_leaderboard_metric():
    print("scenario: leaderboard accepts the new skill_rating / level metrics")
    async with Harness() as h:
        a = Bot("A")
        await a.register(h.uri, "metric_m", "pw1234", token=TOKEN)
        await a.ready(True)
        st = await a.wait_for(racing)
        await a.finish(st["text"])
        await a.wait_for(results)
        rows = await a.request_leaderboard(metric="skill_rating")
        check(rows and all("tier" in r and "skill_rating" in r for r in rows),
              "skill_rating leaderboard carries tier + rating")
        lvl = await a.request_leaderboard(metric="level")
        check(lvl and "level" in lvl[0], "level leaderboard works")
        # auth_ok carried progression
        check(a.stats and a.stats.get("level") is not None,
              "auth_ok stats include level")
        await a.close()


async def scenario_client_settings_roundtrip():
    print("scenario: client settings persist and survive corruption")
    import client_settings as cs
    import shutil
    d = tempfile.mkdtemp(prefix="typeracer_cfg_")
    old = os.environ.get("TYPERACER_CONFIG_DIR")
    os.environ["TYPERACER_CONFIG_DIR"] = d
    try:
        s = cs.load()
        check(s["color"] is True and s["units"] == "wpm",
              "defaults load when no file exists")
        s.update({"color": False, "units": "cpm", "theme": "colorblind",
                  "last_username": "neo"})
        cs.save(s)
        s2 = cs.load()
        check(s2["color"] is False and s2["units"] == "cpm"
              and s2["theme"] == "colorblind" and s2["last_username"] == "neo",
              "settings round-trip through disk")
        with open(cs.settings_path(), "w") as f:
            f.write("{ not valid json ")
        s3 = cs.load()
        check(s3["color"] is True and s3["theme"] == "default",
              "corrupt settings fall back to defaults without raising")
    finally:
        if old is None:
            os.environ.pop("TYPERACER_CONFIG_DIR", None)
        else:
            os.environ["TYPERACER_CONFIG_DIR"] = old
        shutil.rmtree(d, ignore_errors=True)


async def main():
    scenarios = [
        scenario_basic_race,
        scenario_solo_and_replay,
        scenario_name_dedup,
        scenario_admin_force_start,
        scenario_spectator_join_midrace,
        scenario_disconnect_midrace,
        scenario_reject_bad_auth,
        scenario_malformed_input_survives,
        scenario_non_dict_auth_rejected,
        scenario_place_renumber_on_disconnect,
        scenario_register_login_persist,
        scenario_reconnection_takeover,
        scenario_stats_and_leaderboard,
        scenario_guest_no_stats,
        scenario_config_and_timed_mode,
        scenario_survival_mode,
        scenario_anticheat_clamp,
        scenario_chat,
        scenario_profile_and_history,
        scenario_leaderboard_metrics,
        scenario_kick,
        scenario_timed_growth_bounded,
        scenario_guest_name_sanitized,
        scenario_v1_migration,
        scenario_bots_join_and_race,
        scenario_bot_difficulty_scaling,
        scenario_bot_add_remove_limits,
        scenario_session_scoreboard,
        scenario_celebration_banner,
        scenario_flow_config_and_rerace,
        scenario_client_instant_race_reset,
        scenario_grace_timed_eviction_consistency,
        scenario_celebration_not_lost_without_targets,
        scenario_session_guest_pruned,
        scenario_tier_and_rating_fixes,
        scenario_wpm_timeline_splits,
        scenario_units_and_theme_helpers,
        scenario_emotes,
        scenario_player_color,
        scenario_reconnect_grace,
        scenario_grace_expiry,
        scenario_room_password,
        scenario_max_players,
        scenario_unban,
        scenario_host_config_persistence,
        scenario_progression_xp_level,
        scenario_day_streak,
        scenario_h2h_rivals,
        scenario_skill_leaderboard_metric,
        scenario_client_settings_roundtrip,
    ]
    for scn in scenarios:
        try:
            await scn()
        except Exception as exc:  # a thrown scenario is itself a failure
            import traceback
            traceback.print_exc()
            _failures.append(f"{scn.__name__}: {exc!r}")
        print()

    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
