# LAN TypeRacer

A multiplayer typing race for the terminal. One player **hosts** a game on the
LAN, everyone else **logs in and joins**, and you race to type the same passage.
Live progress bars, WPM, accuracy, a finishing-order results screen, **per-player
accounts with saved stats**, and a **global leaderboard** — all in the CLI.

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

| Where        | Key            | Action                                   |
|--------------|----------------|------------------------------------------|
| Lobby/Results| `R`            | toggle **ready**                          |
| Lobby/Results| `L`            | open the **leaderboard**                  |
| Lobby/Results| `Enter`        | **start now** (host only)                 |
| Racing       | any letter     | type the passage                          |
| Racing       | `Backspace`    | step back to fix the current spot         |
| Anywhere     | `Q` / `Esc`    | quit (outside of typing)                  |
| Anywhere     | `Ctrl-C`       | quit                                      |

- The race starts automatically once **everyone** in the lobby is **ready**
  (a solo player can ready up to practice). The host can also force-start with
  `Enter`.
- You must type each character correctly to advance; a wrong key is counted as
  an error and the cursor turns red until you type the right character.
- When everyone finishes, the **results** screen shows the standings. Press
  `R` to ready up for another race with a fresh passage.

## 6. Accounts, stats & leaderboard

- **Accounts** live on the **host** in a JSON file (`typeracer_data.json` by
  default; change it with `--data-file`). Passwords are never stored in plain
  text — they're salted and hashed with PBKDF2-HMAC-SHA256.
- After every race, each logged-in racer's result updates their saved stats:
  races played, races won, best WPM, average WPM, best/average accuracy, total
  time and characters typed. The lobby shows each player's best WPM and race
  count; **guests** show `guest` and are never saved.
- Press `L` in the lobby or results to see the **global leaderboard**, ranked by
  best WPM (your row is highlighted). Press any key to return.
- One account can only be logged in once at a time.

> **Security note:** this is built for a trusted LAN. Connections are plain
> `ws://` (not TLS), so passwords travel unencrypted over your local network and
> stats aren't server-verified. Don't reuse an important password, and don't
> expose the host port to the open internet.

## 7. Options

```
python typeracer.py host --name alice --port 8765 --game-name "Friday Race"
python typeracer.py host --data-file /srv/typeracer/players.json
python typeracer.py host --no-discovery        # turn off UDP auto-discovery
python typeracer.py join 192.168.x.x --name bob --port 8765
```

| Option              | Applies to     | Meaning                                        |
|---------------------|----------------|------------------------------------------------|
| `--name`            | host, join     | pre-fill the login username (else `$USER`)     |
| `--port`            | host, join     | WebSocket port (default `8765`)                |
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

## 8. Networking & firewall notes

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

## 9. Project layout

| File           | Responsibility                                              |
|----------------|-------------------------------------------------------------|
| `typeracer.py` | CLI entry point — `host` / `join` / `discover`              |
| `server.py`    | authoritative game server & state machine                   |
| `client.py`    | WebSocket client + full-screen terminal UI (login/game/leaderboard) |
| `accounts.py`  | persistent accounts + stats store (PBKDF2-hashed passwords) |
| `terminal.py`  | cross-platform raw key input + ANSI/screen control          |
| `netutil.py`   | LAN IP detection + UDP discovery                            |
| `protocol.py`  | shared JSON message contract                                |
| `texts.py`     | typing passages                                             |
| `selftest.py`  | headless end-to-end tests (`python selftest.py`)            |

The host also writes `typeracer_data.json` (the account/stats database) at
runtime; delete it to reset all accounts.

## 10. Test

```bash
python selftest.py
```

Drives the real server over real WebSocket connections with bot clients and
checks lobby/ready/auto-start, countdown, racing, finish ordering, results,
replay, spectators, mid-race disconnects, duplicate-name handling, admin
authority, account register/login, stat persistence, the leaderboard and guest
play.

## 11. Troubleshooting

- **"needs an interactive terminal (a real TTY)"** — run the game directly in a
  terminal window, not through a pipe, `nohup`, CI runner, or IDE "run" panel
  that doesn't allocate a TTY.
- **Colors look like `[0m` garbage on Windows** — use Windows Terminal,
  PowerShell, or a recent Windows 10/11 build; ANSI is enabled automatically via
  the Win32 console API but very old consoles don't support it.
- **Can't connect** — confirm the host IP, that both machines are on the same
  subnet, and that the host's firewall allows the port (see §8).
- **Forgot a password / want to reset** — stop the host and delete its
  `typeracer_data.json` (or edit it by hand; it's plain JSON).
