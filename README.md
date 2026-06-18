# LAN TypeRacer

A full-featured multiplayer typing race for the terminal. One player **hosts** a
game on the LAN, everyone else **logs in and joins**, and you race to type the
same passage — or race the built-in **AI bots** when no one else is around. All
in the CLI:

- **Three race modes** — Classic (first to finish), Timed (most words before the
  clock), and Survival (typos cost lives, last typist standing).
- **AI bot opponents** — the host adds CPU racers at five difficulty tiers
  (Easy … Insane, plus a **Rival** tier that auto-calibrates to your skill), so a
  solo player gets a real race instead of a walkover.
- **Host race setup** — mode, passage length, category (quotes, proverbs, code,
  pangrams, numbers), difficulty, custom text, **countdown length**,
  **quick-start**, **min players**, and **auto-rematch**.
- **Progression** — XP & **levels**, a smoothed **skill rating → tiers**
  (Bronze … Grandmaster), **daily play streaks**, **personal-best** callouts,
  **head-to-head rivalries**, plus best/avg/raw WPM, accuracy, consistency, win
  streaks, per-mode bests, a **match history**, and a **per-race WPM timeline**.
- **Session scoreboard** — F1-style points across an evening of races ("who won
  the night"), a **win-celebration banner**, and 24 **achievements/badges**.
- **Social** — lobby chat, one-key **quick-chat emotes** (usable mid-race as a
  spectator), persistent **accent colors**, player **profiles**, and a global
  **leaderboard**.
- **Robust hosting** — server-authoritative **anti-cheat**, **mid-race
  reconnection grace**, an optional **room password**, persistent **host config +
  ban list** (with unban), configurable **max players**, and host **kick**.
- **Accessibility** — **color themes** (default / high-contrast /
  colorblind-safe / mono), **WPM ⇄ CPM** units, optional **sound** cues,
  persisted client preferences, and an in-app **help** overlay.

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

It prints the exact command other players should run, plus where data is stored:

```
Hosting 'TypeRacer @ 192.168.x.x' on 192.168.x.x:8765
Other players join with:  python typeracer.py join 192.168.x.x
Accounts/stats stored in: /home/alice/typeracer_data.json
```

The host plays too and is the admin (the `[host]` tag in the lobby). The host
keeps everyone's accounts/stats (§7) and the room config + ban list (§9).

**No one else online?** Open race setup (`M`), add a couple of bots (`A`), ready
up — you'll race them solo. The **Rival** tier matches your recent pace.

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

If the host set a room password, add `--room-password <pw>`.

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
least 4 chars. The username pre-fills from your last sign-in (or `--name`).

## 5. Controls

In the **lobby** and **results** screens (press `?` any time for this list):

| Key        | Action                                               |
|------------|------------------------------------------------------|
| `R`        | toggle **ready** (auto-starts when all ready) / **rematch** |
| `T`        | type a **chat** message (`Enter` sends, `Esc` cancels) |
| `1`–`8`    | fire a **quick-chat emote** (also works mid-race as a spectator) |
| `TAB`      | **select** a player (for profile / kick)             |
| `P`        | view the selected player's **profile** + badges      |
| `H`        | your own **match history**                           |
| `L`        | **leaderboard** ( `[` / `]` cycle the metric )        |
| `O`        | cycle your **accent color** (accounts only)          |
| `C`        | cycle **color theme** (default / high-contrast / colorblind / mono) |
| `U`        | **units** — WPM / CPM / both                          |
| `S`        | **sound** cues on/off                                |
| `?`        | **help** overlay                                     |
| `Q` / `Esc`| quit                                                |
| **Host:** `Enter` | **start / rematch** now                       |
| **Host:** `M`     | **race setup** (modes, bots, flow — see §6)   |
| **Host:** `K`     | **kick** the selected player (twice to confirm) |
| **Host:** `B`     | view + manage the **ban list** (un-ban by number) |

While **racing**, just type the passage; `Backspace` fixes the current spot. You
must type each character correctly to advance — a wrong key is an error and the
cursor turns red until you fix it. `Ctrl-C` always quits. (Number keys type
normally while *you* are racing; emotes via `1`–`8` are for spectators and the
lobby, so a numbers passage is never hijacked.)

## 6. Race setup (host, `M`)

| Key | Setting |
|-----|---------|
| `M` | **Mode** — Classic / Timed / Survival |
| `L` | **Length** — short / medium / long |
| `G` | **Category** — quotes / proverbs / code / pangrams / numbers / any |
| `D` | **Difficulty** — any / easy / medium / hard |
| `T` | **Time limit** (Timed) — 15 / 30 / 60 / 120 s |
| `V` | **Lives** (Survival) — 1 / 2 / 3 / 5 |
| `X` | **Custom text** — paste your own passage (sanitized to single-line ASCII) |
| `B` `A` `Z` | **Bots** — `B` pick a tier, `A` add a bot, `Z` remove the last |
| `O` | **Countdown** — instant / 3 / 5 / 10 s |
| `I` | **Quick start** — skip the countdown dwell for a lone human |
| `N` | **Min players** — ready humans required before auto-start |
| `E` | **Auto-rematch** — off / 5 / 10 / 20 s after results |

Modes:

- **Classic** — type the whole passage; first to finish wins, then by progress.
- **Timed** — the passage refills endlessly; type as much as you can before the
  clock runs out. Ranked by characters typed (net WPM).
- **Survival** — every typo costs a life (1 = sudden death). Run out and you're
  eliminated; the last typist standing (or first to finish) wins.

**AI bots** are virtual racers driven by the server: Easy ~30 WPM, Medium ~55,
Hard ~85, Insane ~120, and **Rival** which calibrates to the strongest human in
the race. Bots never appear on the leaderboard or the session scoreboard.

## 7. Accounts, progression, leaderboard & achievements

- **Accounts** live on the **host** (`typeracer_data.json`; change with
  `--data-file`). Passwords are never stored in plain text — salted and hashed
  with PBKDF2-HMAC-SHA256, and never sent back to any client.
- After every race, each signed-in racer's result updates their stats:
  races/wins, **best & average WPM** (net and raw), accuracy, **consistency**,
  **win streaks**, **per-mode/category bests**, **XP & level**, a smoothed
  **skill rating** and **tier**, a **daily play streak**, **head-to-head**
  records vs everyone they've faced, and a rolling **match history** (`H`).
- **Personal bests** and **level-ups** pop up on the results screen, alongside
  **new achievements** and a **win-celebration banner** (flawless / photo-finish
  / on-a-streak / upset). The results screen also shows a **per-race WPM
  timeline** sparkline and the **session scoreboard** (F1 points across the
  evening's races; the host clears it deliberately, not on a stray abort).
- Press `L` for the **leaderboard**; `[` / `]` cycle the metric (best WPM,
  average WPM, wins, races, longest streak, consistency, **skill rating**,
  **level**). Press `P` on a selected player for their full **profile** —
  level/tier, milestones, top rivals, and badges.
- **24 achievements** unlock automatically (e.g. *First Blood*, *Ton Up* at
  100 WPM, *Blistering* at 150, *Flawless*, *Centurion*, *Regular* for a 7-day
  streak, *Elite* for Diamond tier).
- **Guests** play normally — including bots, emotes and the session board — but
  are never persisted and never reach the leaderboard.

**Anti-cheat:** the server clamps reported progress to a realistic ceiling
(~300 WPM), flags implausible jumps, and computes WPM itself, so a hacked client
can't poison the standings or the leaderboard.

## 8. Reconnecting & one-session-per-account

One account is live once at a time; logging in again **takes over** the session.
If your connection drops **mid-race**, the host holds your spot for a few seconds
— reconnect within the grace window and you resume your position, standings and
progress instead of being dropped.

## 9. Hosting controls (moderation, privacy, persistence)

- **Kick / ban / un-ban** — `K` kicks the selected player (a kicked account
  can't immediately rejoin); `B` opens the **ban list** to pardon someone.
- **Room password** — `--room-password <pw>` requires a password to join;
  discovering clients see the room is locked. Joiners pass the same flag.
- **Persistent host state** — the room config and ban list are saved to
  `typeracer_host.json` (`--host-config`) and restored on restart, so a host
  reboot doesn't reset your setup or un-ban everyone.
- **Max players** — `--max-players` caps concurrent **humans** (bots are exempt;
  the whole roster is still bounded).

> **Security note:** built for a trusted LAN. Connections are plain `ws://` (not
> TLS), so passwords and the room password travel unencrypted over your local
> network. Don't reuse an important password, and don't expose the host port to
> the open internet.

## 10. Accessibility & preferences

Press `C` to cycle **color themes** — *default*, *high-contrast*,
*colorblind-safe* (shifts the typed/error pair onto a blue/yellow axis), and
*mono* (no color). Press `U` for **WPM / CPM / both** units, `S` for **sound**
cues. These (plus your last username) persist per-machine in
`~/.typeracer/settings.json` (override the dir with `$TYPERACER_CONFIG_DIR`).
`--no-color` / `$NO_COLOR` force mono regardless.

## 11. Options

```
python typeracer.py host --name alice --game-name "Friday Race"
python typeracer.py host --room-password hunter2 --max-players 8
python typeracer.py host --data-file /srv/tr/players.json --host-config /srv/tr/room.json
python typeracer.py join 192.168.x.x --name bob --room-password hunter2 --no-color
```

| Option              | Applies to | Meaning                                        |
|---------------------|------------|------------------------------------------------|
| `--name`            | host, join | pre-fill the login username (else last/`$USER`) |
| `--port`            | host, join | WebSocket port (default `8765`)                |
| `--room-password`   | host, join | require / supply a room password               |
| `--no-color`        | host, join | force mono (also respects `$NO_COLOR`)         |
| `--game-name`       | host       | name shown to discovering players              |
| `--data-file`       | host       | accounts/stats JSON (default `typeracer_data.json`) |
| `--host-config`     | host       | room config + ban-list JSON (default `typeracer_host.json`) |
| `--max-players`     | host       | max concurrent **human** players (default 16)  |
| `--subnet`          | all        | LAN subnet hint (also `$TYPERACER_SUBNET`)     |
| `--discovery-port`  | all        | UDP discovery port (default `8766`)            |
| `--no-discovery`    | host       | disable the UDP discovery responder            |

**Choosing the LAN interface.** By default the host auto-detects its address
from the default route — no subnet is assumed. If a multi-homed machine picks
the wrong IP, pin the one to advertise (accepts `192.168.20`,
`192.168.20.0/24`, etc.):

```bash
python typeracer.py host --subnet 192.168.20      # or 10.0.0.0/24, etc.
export TYPERACER_SUBNET=192.168.20                # or set it once via the env var
```

The hint also adds that subnet's broadcast address to auto-discovery probes.

## 12. Networking & firewall notes

- The host listens on **TCP `<port>`** (default `8765`) for the game and
  **UDP `<discovery-port>`** (default `8766`) for auto-discovery. Allow both
  through the host's firewall for LAN connections.
  - **Windows:** the first run usually pops a Defender Firewall prompt — choose
    *Allow on private networks*. Or pre-allow it:
    `New-NetFirewallRule -DisplayName "TypeRacer" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow`
  - **Linux (ufw):** `sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp`
- If auto-discovery finds nothing (some networks block broadcast), just join by
  the host's IP directly — that always works.

## 13. Project layout

| File                | Responsibility                                          |
|---------------------|---------------------------------------------------------|
| `typeracer.py`      | CLI entry point — `host` / `join` / `discover`          |
| `server.py`         | authoritative game server: modes, bots, anti-cheat, chat, sessions |
| `client.py`         | WebSocket client + full-screen terminal UI (login/game/overlays) |
| `accounts.py`       | persistent accounts + stats store (PBKDF2-hashed passwords) |
| `progression.py`    | XP/level curve + skill-rating → tier ladders            |
| `milestones.py`     | cumulative progress-bar milestones for the profile      |
| `achievements.py`   | achievement definitions + evaluation                    |
| `modes.py`          | race-mode constants, bot tiers, flow + scoring config   |
| `config_store.py`   | persistent host room config + ban list (atomic writes)  |
| `client_settings.py`| per-machine UI preferences (color/theme/units/sound)    |
| `terminal.py`       | cross-platform raw key input + ANSI / themes / no-color |
| `netutil.py`        | LAN IP detection + UDP discovery                        |
| `protocol.py`       | shared JSON message contract (protocol **v4**)          |
| `texts.py`          | categorized passages + custom-text validation           |
| `selftest.py`       | headless end-to-end tests (`python selftest.py`)        |
| `ptytest.py`        | interactive TUI tests over a PTY (`python ptytest.py`)  |

The host also writes `typeracer_data.json` (accounts/stats) and
`typeracer_host.json` (room config + bans); delete them to reset.

## 14. Test

```bash
python selftest.py     # headless server/protocol tests (bots, no terminal)
python ptytest.py      # interactive TUI tests driven through a pseudo-terminal
```

`selftest.py` drives the real server over real WebSocket connections and checks
every subsystem: lobby/ready/auto-start, countdown, racing, finish ordering,
results, replay, spectators, mid-race disconnects + **reconnection grace**,
duplicate names, admin authority, account register/login + stat persistence,
guest play, **timed** and **survival** modes, **AI bots**, **anti-cheat**
clamping, chat + **emotes**, **profiles/history**, leaderboard metric/mode
cycling (incl. **skill rating/level**), **XP/level/PB/day-streak/rivalry**,
**session scoreboard**, **win celebration**, **quick-start/countdown/re-race**,
**accent colors**, **room password**, **max players**, **un-ban**, **host config
persistence**, and the **WPM-timeline** splits. `ptytest.py` exercises the real
terminal UI (login, overlays, setup, bots, emotes, theme/units toggles, racing).

## 15. Troubleshooting

- **"needs an interactive terminal (a real TTY)"** — run the game directly in a
  terminal window, not through a pipe, `nohup`, CI runner, or IDE "run" panel.
- **Colors look like `[0m` garbage** — use Windows Terminal / PowerShell / a
  recent Windows 10/11 build, or run with `--no-color` / press `C` to mono.
- **Can't connect** — confirm the host IP, the same subnet, the firewall (§12),
  and the room password if one is set.
- **Forgot a password / want to reset** — stop the host and delete its
  `typeracer_data.json` (accounts) or `typeracer_host.json` (room config + bans).
