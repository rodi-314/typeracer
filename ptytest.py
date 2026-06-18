#!/usr/bin/env python3
"""Drive the real TypeRacer terminal UI through a pseudo-terminal.

selftest.py exercises the server and protocol with headless bots; this exercises
the *interactive client* -- the login form, overlays, racetrack and key handling
-- by launching ``typeracer.py host`` inside a PTY, feeding it keystrokes, and
asserting on what gets rendered. It catches TUI regressions that a headless bot
never would (an overlay that traps input, a frame that never paints, a control
that sends the wrong message).

Run:  python ptytest.py
"""

import fcntl
import os
import pty
import re
import select
import socket
import struct
import sys
import tempfile
import termios
import time


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB0]")
_failures = []


def check(cond, label):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


def strip(text):
    return _ANSI.sub("", text)


class PtySession:
    """A child process attached to a PTY we can read frames from and type into."""

    def __init__(self, argv, env=None, size=(40, 120)):
        self.argv = argv
        self.env = env or dict(os.environ)
        self.size = size
        self.pid = None
        self.fd = None
        self.buf = ""

    def start(self):
        pid, fd = pty.fork()
        if pid == 0:                       # child
            try:
                rows, cols = self.size
                packed = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, packed)
            except Exception:
                pass
            os.execvpe(self.argv[0], self.argv, self.env)
            os._exit(127)
        self.pid = pid
        self.fd = fd
        return self

    def _pump(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if self.fd in r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                self.buf += data.decode("utf-8", "replace")
        return True

    def expect(self, needle, timeout=8.0):
        """Wait until ``needle`` appears in the (ANSI-stripped) output."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in strip(self.buf):
                return True
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if self.fd in r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.buf += data.decode("utf-8", "replace")
        return needle in strip(self.buf)

    def screen(self):
        self._pump(0.4)
        return strip(self.buf)

    def send(self, keys):
        os.write(self.fd, keys.encode("utf-8"))
        time.sleep(0.15)

    def close(self):
        try:
            os.write(self.fd, b"\x03")     # Ctrl-C
            time.sleep(0.2)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.pid:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass


def host_argv(data_file, port):
    return [sys.executable, "typeracer.py", "host", "--no-discovery",
            "--no-color", "--data-file", data_file, "--name", "neo",
            "--game-name", "PtyTest", "--port", str(port)]


def test_bots_through_tui():
    print("pty: register, open setup, add an AI bot, start a race with it")
    fd, data = tempfile.mkstemp(suffix=".json", prefix="typeracer_pty_")
    os.close(fd)
    os.unlink(data)
    env = dict(os.environ)
    env["TYPERACER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="typeracer_ptycfg_")
    s = PtySession(host_argv(data, free_port()), env=env).start()
    try:
        check(s.expect("SIGN IN", 10), "login screen renders")
        s.send("r")                        # register
        check(s.expect("Username", 4), "register form asks for a username")
        s.send("neo\r")
        check(s.expect("Password", 4), "register form asks for a password")
        s.send("secret\r")
        check(s.expect("LOBBY", 6), "authenticated into the lobby")
        s.send("1")                        # fire the first quick-chat emote
        check(s.expect("gg!", 4), "a number-key emote posts to the lobby chat")
        s.send("u")                        # cycle WPM/CPM units (no crash)
        s.send("c")                        # cycle color theme (no crash)
        check("LOBBY" in s.screen(), "lobby survives units/theme toggles")
        s.send("m")                        # open race setup
        check(s.expect("RACE SETUP", 4), "setup overlay opens")
        s.send("bb")                       # cycle bot tier medium -> hard -> insane
        check("Insane" in s.screen(), "bot tier cycles to Insane")
        s.send("a")                        # add a bot
        check(s.expect("1 in the room", 4), "bot is added to the room")
        s.send("q")                        # close setup
        check(s.expect("[bot:Insane]", 4), "bot shows on the lobby roster")
        s.send("r")                        # ready -> solo human + bot autostarts
        started = s.expect("TIME", 12) or s.expect("WPM", 12)
        check(started, "race starts with the bot enrolled")
        scr = s.screen()
        check(any(nm in scr for nm in ("Ada", "Grace", "Linus", "Dennis",
                                       "Turing", "Hopper")),
              "the bot appears on the racetrack")
    finally:
        s.close()


def main():
    if not hasattr(os, "fork"):
        print("PTY tests require POSIX fork(); skipping on this platform.")
        return 0
    test_bots_through_tui()
    print()
    if _failures:
        print(f"PTY FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL PTY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
