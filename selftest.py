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

# Skip the real 3-2-1 wait so the suite runs quickly.
server_mod.COUNTDOWN_SECONDS = 0

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
        self._authed = asyncio.Event()
        self._updated = asyncio.Event()
        self._lb = asyncio.Event()
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
        except ConnectionClosed:
            pass

    async def request_leaderboard(self, metric="best_wpm"):
        self._lb.clear()
        await self.send({"type": P.C_LEADERBOARD, "metric": metric})
        await asyncio.wait_for(self._lb.wait(), timeout=5)
        return self.leaderboard_rows

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


async def scenario_double_login_blocked():
    print("scenario: the same account cannot be logged in twice")
    async with Harness() as h:
        one = Bot("One")
        await one.register(h.uri, "dupe_user", "pw1234", token=TOKEN)
        two = Bot("Two")
        ok = await two.login(h.uri, "dupe_user", "pw1234")
        check(not ok and "already logged in" in (two.auth_error or ""),
              "second concurrent login is blocked")
        await one.close()
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
        scenario_double_login_blocked,
        scenario_stats_and_leaderboard,
        scenario_guest_no_stats,
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
