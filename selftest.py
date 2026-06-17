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
        self._authed = asyncio.Event()
        self._updated = asyncio.Event()
        self._lb = asyncio.Event()
        self._profile_ev = asyncio.Event()
        self._history_ev = asyncio.Event()
        self._reader = None

    async def _auth(self, uri, msg):
        self.ws = await connect(uri)
        await self.ws.send(P.encode(msg))
        self._reader = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._authed.wait(), timeout=5)
        return self.auth_error is None

    async def start(self, uri, token=None):
        """Join as a guest (keeps gameplay scenarios simple)."""
        return await self._auth(uri, {"type": P.C_GUEST, "name": self.name,
                                      "token": token,
                                      "version": P.PROTOCOL_VERSION})

    async def register(self, uri, username, password, token=None):
        return await self._auth(uri, {"type": P.C_REGISTER, "username": username,
                                      "password": password, "token": token,
                                      "version": P.PROTOCOL_VERSION})

    async def login(self, uri, username, password, token=None):
        return await self._auth(uri, {"type": P.C_LOGIN, "username": username,
                                      "password": password, "token": token,
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

    def __init__(self, seed=1, with_store=True):
        self.store = None
        self.data_path = None
        if with_store:
            fd, path = tempfile.mkstemp(suffix=".json", prefix="typeracer_test_")
            os.close(fd)
            os.unlink(path)  # let AccountStore create it on first save
            self.data_path = path
            self.store = AccountStore(path)
        self.gs = GameServer(admin_token=TOKEN, seed=seed, store=self.store)
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
