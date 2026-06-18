"""Cross-platform terminal helpers: raw keystroke input and ANSI screen control.

The input layer runs a small *polling* background thread (so it can be told to
stop without being stuck in a blocking read) and bridges every keypress onto the
asyncio event loop with ``loop.call_soon_threadsafe``. Keypresses are delivered
as 2-tuples::

    ("char", "a")          # a printable character
    ("special", "ENTER")   # a named control key (see KEY_* constants)

Works on Linux/macOS (termios + os.read + select) and Windows (msvcrt).
"""

import os
import re
import sys
import shutil
import threading

IS_WINDOWS = os.name == "nt"

# Named special keys emitted as ("special", NAME)
KEY_ENTER = "ENTER"
KEY_BACKSPACE = "BACKSPACE"
KEY_ESC = "ESC"
KEY_CTRL_C = "CTRL_C"
KEY_TAB = "TAB"

# ---------------------------------------------------------------------------
# ANSI escape sequences
# ---------------------------------------------------------------------------
ESC = "\x1b"
RESET = ESC + "[0m"
BOLD = ESC + "[1m"
DIM = ESC + "[2m"
UNDERLINE = ESC + "[4m"
REVERSE = ESC + "[7m"

FG_RED = ESC + "[31m"
FG_GREEN = ESC + "[32m"
FG_YELLOW = ESC + "[33m"
FG_BLUE = ESC + "[34m"
FG_MAGENTA = ESC + "[35m"
FG_CYAN = ESC + "[36m"
FG_WHITE = ESC + "[37m"
FG_GREY = ESC + "[90m"
FG_BRIGHT_GREEN = ESC + "[92m"
FG_BRIGHT_RED = ESC + "[91m"
FG_BRIGHT_CYAN = ESC + "[96m"

BG_RED = ESC + "[41m"
BG_GREEN = ESC + "[42m"

CURSOR_HOME = ESC + "[H"
CLEAR_EOL = ESC + "[K"        # clear from cursor to end of line
CLEAR_BELOW = ESC + "[J"      # clear from cursor to end of screen
HIDE_CURSOR = ESC + "[?25l"
SHOW_CURSOR = ESC + "[?25h"
ENTER_ALT = ESC + "[?1049h"   # alternate screen buffer
LEAVE_ALT = ESC + "[?1049l"


def get_size():
    """Return (columns, rows) with a sane fallback for dumb terminals.

    Some terminals/PTYs report a 0x0 size; treat that as a standard 80x24 so the
    UI never collapses to a couple of rows.
    """
    size = shutil.get_terminal_size(fallback=(80, 24))
    cols = size.columns if size.columns > 0 else 80
    rows = size.lines if size.lines > 0 else 24
    return max(20, cols), max(8, rows)


def is_interactive():
    """True only when both stdin and stdout are attached to a real terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
# Keep structural attributes (reset/bold/dim/underline/reverse + their resets);
# drop foreground/background colors. Cursor/clear escapes (\x1b[...K/H/J) are not
# 'm' sequences, so they pass through untouched.
_KEEP_SGR = {"0", "1", "2", "4", "7", "22", "23", "24", "27"}


def strip_color(text):
    """Remove ANSI color codes but keep bold/dim/underline/reverse (no-color mode)."""
    def repl(m):
        kept = [p for p in m.group(1).split(";") if p in _KEEP_SGR]
        return "\x1b[" + ";".join(kept) + "m" if kept else ""
    return _SGR_RE.sub(repl, text)


def remap_sgr(text, mapping):
    """Rewrite SGR color parameters through ``mapping`` (theme support).

    ``mapping`` is a dict of SGR-parameter strings, e.g. {"32": "94"} to turn
    green into bright blue. Parameters not in the mapping pass through unchanged,
    so structural attributes (bold/reverse/reset) are untouched.
    """
    if not mapping:
        return text

    def repl(m):
        params = [mapping.get(p, p) for p in m.group(1).split(";")]
        return "\x1b[" + ";".join(params) + "m"
    return _SGR_RE.sub(repl, text)


def enable_ansi():
    """Enable ANSI/VT processing. No-op on POSIX; uses the Win32 console API."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
        return True
    except Exception:
        return False


def write(text):
    """Write straight to the real stdout and flush immediately."""
    sys.stdout.write(text)
    sys.stdout.flush()


def enter_fullscreen():
    enable_ansi()
    write(ENTER_ALT + HIDE_CURSOR + CURSOR_HOME + CLEAR_BELOW)


def leave_fullscreen():
    write(SHOW_CURSOR + LEAVE_ALT + RESET)


# ---------------------------------------------------------------------------
# Raw keystroke reader
# ---------------------------------------------------------------------------
class RawInput:
    """Background keystroke reader that feeds an asyncio.Queue.

    Call :meth:`start` to put the terminal in raw mode and begin reading, and
    :meth:`stop` to restore the terminal. ``stop`` is idempotent and safe to
    call from a ``finally`` block or atexit handler.
    """

    POLL_INTERVAL = 0.04  # seconds between stop-flag checks while idle

    def __init__(self, loop, queue):
        self._loop = loop
        self._queue = queue
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        # POSIX terminal restore state
        self._fd = None
        self._saved_attrs = None

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._started:
            return
        self._started = True
        if not IS_WINDOWS:
            self._enter_raw_posix()
        self._thread = threading.Thread(
            target=self._run, name="typeracer-input", daemon=True
        )
        self._thread.start()

    def stop(self):
        if not self._started:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if not IS_WINDOWS:
            self._restore_posix()
        self._started = False

    # -- emit ---------------------------------------------------------------
    def _emit(self, event):
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except RuntimeError:
            # Loop already closed during shutdown; drop the keystroke.
            pass

    # -- POSIX --------------------------------------------------------------
    def _enter_raw_posix(self):
        import termios

        self._fd = sys.stdin.fileno()
        self._saved_attrs = termios.tcgetattr(self._fd)
        new = termios.tcgetattr(self._fd)
        # lflags: drop canonical mode, echo, signal generation and extended input
        new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        # iflags: drop software flow control and CR->NL translation
        new[0] &= ~(termios.IXON | termios.ICRNL)
        termios.tcsetattr(self._fd, termios.TCSADRAIN, new)

    def _restore_posix(self):
        if self._fd is not None and self._saved_attrs is not None:
            import termios

            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
            except Exception:
                pass
            self._saved_attrs = None

    def _run(self):
        if IS_WINDOWS:
            self._run_windows()
        else:
            self._run_posix()

    def _run_posix(self):
        import select
        import codecs

        fd = self._fd
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], self.POLL_INTERVAL)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 1024)
            except OSError:
                break
            if not data:
                break  # EOF on stdin
            text = decoder.decode(data)
            for event in _parse_posix(text):
                self._emit(event)

    def _run_windows(self):
        import msvcrt
        import time

        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(self.POLL_INTERVAL)
                continue
            ch = msvcrt.getwch()
            # Function/arrow keys arrive as a two-character sequence.
            if ch in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()  # discard the scan code
                continue
            event = _classify_char(ch)
            if event is not None:
                self._emit(event)


def _classify_char(ch):
    """Map a single decoded character to an input event, or None to ignore."""
    if ch in ("\r", "\n"):
        return ("special", KEY_ENTER)
    if ch in ("\x7f", "\x08"):
        return ("special", KEY_BACKSPACE)
    if ch == "\x03" or ch == "\x04":  # Ctrl-C / Ctrl-D both mean "quit"
        return ("special", KEY_CTRL_C)
    if ch == "\x1b":
        return ("special", KEY_ESC)
    if ch == "\t":
        return ("special", KEY_TAB)
    if ch >= " " and ch != "\x7f":
        return ("char", ch)
    return None  # other control bytes ignored


def _parse_posix(text):
    """Yield input events from a chunk of decoded stdin text.

    Handles CSI/SS3 escape sequences (arrow keys etc.) by swallowing them so a
    stray arrow press never gets typed into the race.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in ("[", "O"):
                # Consume until a final byte in the 0x40-0x7e range.
                j = i + 2
                while j < n and not ("\x40" <= text[j] <= "\x7e"):
                    j += 1
                i = j + 1  # skip the final byte too
                continue
            # Lone ESC.
            yield ("special", KEY_ESC)
            i += 1
            continue
        event = _classify_char(ch)
        if event is not None:
            yield event
        i += 1
