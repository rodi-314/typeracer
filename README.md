# LAN TypeRacer

A full-featured multiplayer typing race for the terminal. One player **hosts** a
game on the LAN, everyone else **logs in and joins**, and you race to type the
same passage. All in the CLI:

- **Three race modes** — Classic (first to finish), Timed (most words before the
  clock), and Survival (typos cost lives, last typist standing).
- **Host race setup** — pick mode, passage length, category (quotes, proverbs,
  code, pangrams, numbers), difficulty, or paste your own custom text.
- **Accounts & saved stats** — best/avg/raw WPM, accuracy, consistency, win
  streaks, per-mode bests and a **match history**.
- **Global leaderboard** with cycleable metrics, **achievements/badges**,
  player **profiles**, and **lobby chat**.
- **Anti-cheat** (server-authoritative WPM), graceful **reconnection**, host
  **moderation** (kick), a **no-color** accessibility mode, and an in-app **help**
  overlay.

Runs on **Linux/macOS terminals** and **Windows PowerShell / cmd**. Networking is
plain WebSockets (the `websockets` library); the server binds `0.0.0.0`, so any
player on your `192.168.x.0/24` LAN can connect.

---

## 1. Install

You need **Python 3.8+** and one dependency:

```bash
pip install websockets
# or:  pip install -r requirements.txt
```

Copy the whole `typeracer/` folder to each machine that wants to play (or share
it over the network drive). All files must stay together.

## 2. Host a game

On the host machine:

```bash
python typeracer.py host
```

It prints the exact command other players should run, plus where accounts are
stored, e.g.:

```
Hosting 'TypeRacer @ 192.168.x.x' on 192.168.x.x:8765
Other players join with:  python typeracer.py join 192.168.x.x
Accounts/stats stored in: /home/alice/typeracer_data.json
```

The host plays too and is the admin (the `[host]` tag in the lobby). The host
also keeps everyone's accounts and stats (see §6).

## 3. Join a game

On every other machine on the LAN:

```bash
python typeracer.py join 192.168.x.x      # use the IP the host printed
```

Or let it find the host automatically over the LAN (UDP broadcast discovery):

```bash
python typeracer.py join                      # auto-discover
python typeracer.py discover                  # just list hosts, don't join
```

## 4. Sign in

Everyone (the host included) starts at a **sign-in screen**:

| Key | Action |
|-----|--------|
| `L` | log in to an existing account (username + password) |
| `R` | register a new account |
| `G` | play as a guest (no saved stats) |
| `Q` | quit |

Type into the fields, `Enter` to continue, `Backspace` to edit, `Esc` to go
back. Usernames are 3–16 chars (letters/digits/underscore); passwords are at
least 4 chars. Pass `--name <username>` to pre-fill the field.

## 5. How to play

In the **lobby** and **results** screens (press `?` any time for this list):

| Key        | Action                                               |
|------------|------------------------------------------------------|
| `R`        | toggle **ready** (auto-starts when all are ready) / **rematch** |
| `T`        | type a **chat** message (`Enter` sends, `Esc` cancels) |
| `TAB`      | **select** a player (for profile / kick)             |
| `P`        | view the selected player's **profile** + badges      |
| `H`        | your own **match history**                           |
| `L`        | **leaderboard** ( `[` / `]` cycle the metric )        |
| `C`        | toggle **colors** on/off                             |
| `?`        | **help** overlay                                     |
| `Q` / `Esc`| quit                                                |
| **Host:** `Enter` | **start / rematch** now                       |
| **Host:** `M`     | **race setup** (mode, length, category, custom text) |
| **Host:** `K`     | **kick** the selected player (press twice to confirm) |

While **racing**, just type the passage; `Backspace` fixes the current spot.
You must type each character correctly to advance — a wrong key counts as an
error and the cursor turns red until you type the right one. `Ctrl-C` always
quits.

## 6. Game modes

The host picks the mode in the **setup** screen (`M`); it shows under
"Next race:" for everyone.

- **Classic** — type the whole passage; first to finish wins, then by progress.
- **Timed** — the passage refills endlessly; type as much as you can before the
  clock (15/30/60/120 s) runs out. Ranked by characters typed (net WPM).
- **Survival** — every typo costs a life (`--lives`, 1 = sudden death). Run out
  and you're eliminated; the last typist standing (or first to finish) wins.

Setup also controls **length** (short/medium/long), **category** (quotes,
proverbs, code, pangrams, numbers, or *any*), **difficulty**, and **custom
text** (`X` — paste your own passage; it's sanitized to single-line ASCII).

## 7. Accounts, stats, leaderboard & achievements

- **Accounts** live on the **host** in a JSON file (`typeracer_data.json` by
  default; change it with `--data-file`). Passwords are never stored in plain
  text — they're salted and hashed with PBKDF2-HMAC-SHA256.
- After every race, each logged-in racer's result updates their saved stats:
  races/wins, **best & average WPM** (net and raw), **best/avg accuracy**,
  **consistency**, **win streaks**, **per-mode bests**, and a rolling
  **match history** (press `H`). Press `P` on a selected player to see their
  full **profile** and **badges**. **Guests** play normally but are never saved.
- Press `L` for the **global leaderboard**; `[` / `]` cycle the ranking metric
  (best WPM, average WPM, wins, races, longest streak, consistency).
- **Achievements** (e.g. *First Blood*, *Ton Up* for 100 WPM, *Flawless*,
  *Unstoppable* for a 5-win streak) unlock automatically and pop up on the
  results screen when earned.
- **Anti-cheat:** the server clamps reported progress to a realistic ceiling
  (~300 WPM) and computes WPM itself, so a hacked client can't poison the
  leaderboard. One account can be logged in once at a time; logging in again
  **takes over** the session (handy when Wi-Fi drops).

> **Security note:** this is built for a trusted LAN. Connections are plain
> `ws://` (not TLS), so passwords travel unencrypted over your local network.
> Don't reuse an important password, and don't expose the host port to the open
> internet. The kick "ban" is a per-session convenience, not a security control.

## 8. Options

```
python typeracer.py host --name alice --port 8765 --game-name "Friday Race"
python typeracer.py host --data-file /srv/typeracer/players.json
python typeracer.py host --no-discovery        # turn off UDP auto-discovery
python typeracer.py join 192.168.x.x --name bob --no-color
```

| Option              | Applies to     | Meaning                                        |
|---------------------|----------------|------------------------------------------------|
| `--name`            | host, join     | pre-fill the login username (else `$USER`)     |
| `--port`            | host, join     | WebSocket port (default `8765`)                |
| `--no-color`        | host, join     | disable colors (also respects `$NO_COLOR`; toggle in-app with `C`) |
| `--game-name`       | host           | name shown to discovering players              |
| `--data-file`       | host           | accounts/stats JSON (default `typeracer_data.json`) |
| `--subnet`          | all            | LAN subnet hint (also `$TYPERACER_SUBNET`)     |
| `--discovery-port`  | all            | UDP discovery port (default `8766`)            |
| `--no-discovery`    | host           | disable the UDP discovery responder            |

The username field is pre-filled from `--name` or the `TYPERACER_NAME`, `USER`
or `USERNAME` env var; you can always edit it on the sign-in screen.

**Choosing the LAN interface.** By default the host auto-detects its address
from the default route — no subnet is assumed. If a machine has several
networks and the wrong IP is picked, pin the one to advertise with a subnet
hint (accepts `192.168.20`, `192.168.20.0/24`, etc.):

```bash
python typeracer.py host --subnet 192.168.20      # or 10.0.0.0/24, etc.
export TYPERACER_SUBNET=192.168.20                # or set it once via the env var
```

The hint also adds that subnet's broadcast address to auto-discovery probes.

## 9. Networking & firewall notes

- The host listens on **TCP `<port>`** (default `8765`) for the game and
  **UDP `<discovery-port>`** (default `8766`) for auto-discovery. Allow both
  through the host's firewall for LAN connections.
  - **Windows:** the first run usually pops a Windows Defender Firewall prompt —
    choose *Allow on private networks*. Or pre-allow it:
    `New-NetFirewallRule -DisplayName "TypeRacer" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow`
  - **Linux (ufw):** `sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp`
- If auto-discovery finds nothing (some networks block broadcast), just join by
  the host's IP directly — that always works.
- Everyone must be on the same LAN/subnet (e.g. `192.168.x.0/24`). The host
  auto-detects its address from the default route; if it guesses wrong on a
  multi-homed machine, pin it with `--subnet` / `$TYPERACER_SUBNET` (see §7), or
  players can still join using the correct IP directly.

## 10. Project layout

| File             | Responsibility                                            |
|------------------|----------------------------------------------------------|
| `typeracer.py`   | CLI entry point — `host` / `join` / `discover`           |
| `server.py`      | authoritative game server, modes, anti-cheat, chat       |
| `client.py`      | WebSocket client + full-screen terminal UI (login/game/overlays) |
| `accounts.py`    | persistent accounts + stats store (PBKDF2-hashed passwords) |
| `achievements.py`| achievement definitions + evaluation                     |
| `modes.py`       | race-mode constants + default config                     |
| `terminal.py`    | cross-platform raw key input + ANSI / no-color           |
| `netutil.py`     | LAN IP detection + UDP discovery                         |
| `protocol.py`    | shared JSON message contract (protocol v3)               |
| `texts.py`       | categorized passages + custom-text validation            |
| `selftest.py`    | headless end-to-end tests (`python selftest.py`)         |

The host also writes `typeracer_data.json` (the account/stats database) at
runtime; delete it to reset all accounts.

## 11. Test

```bash
python selftest.py
```

Drives the real server over real WebSocket connections with bot clients and
checks every subsystem headlessly: lobby/ready/auto-start, countdown, racing,
finish ordering, results, replay, spectators, mid-race disconnects, duplicate
names, admin authority, account register/login, stat persistence, guest play,
the **timed** and **survival** modes, host **config**, **anti-cheat** clamping,
**chat** (sanitization + rate limit), **profiles/history**, **leaderboard**
metric/mode cycling, **reconnection takeover**, and host **kick**.

## 12. Troubleshooting

- **"needs an interactive terminal (a real TTY)"** — run the game directly in a
  terminal window, not through a pipe, `nohup`, CI runner, or IDE "run" panel
  that doesn't allocate a TTY.
- **Colors look like `[0m` garbage** — use Windows Terminal, PowerShell, or a
  recent Windows 10/11 build (ANSI is enabled automatically via the Win32 console
  API but very old consoles don't support it), or run with `--no-color` /
  press `C` in-app.
- **Can't connect** — confirm the host IP, that both machines are on the same
  subnet, and that the host's firewall allows the port (see §9).
- **Forgot a password / want to reset** — stop the host and delete its
  `typeracer_data.json` (or edit it by hand; it's plain JSON).
