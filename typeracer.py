#!/usr/bin/env python3
"""LAN CLI multiplayer TypeRacer.

Usage
-----
  python typeracer.py host                 # host a game and play (you are admin)
  python typeracer.py join 192.168.20.42   # join a game by host IP
  python typeracer.py join                  # auto-discover a host on the LAN
  python typeracer.py discover              # just list hosts found on the LAN

Run ``python typeracer.py <command> -h`` for per-command options.
"""

import argparse
import asyncio
import os
import re
import secrets
import sys

from websockets.asyncio.server import serve

import protocol as P
import netutil
from accounts import AccountStore, DEFAULT_DATA_FILE
from server import GameServer
from client import GameClient


def default_username(provided=None):
    """A sensible prefill for the login screen's username field."""
    candidates = [provided] + [os.environ.get(v) for v in
                               ("TYPERACER_NAME", "USER", "USERNAME", "LOGNAME")]
    for val in candidates:
        if val:
            cleaned = re.sub(r"[^A-Za-z0-9_]", "", val)[:16]
            if cleaned:
                return cleaned
    return ""


# ---------------------------------------------------------------------------
# host
# ---------------------------------------------------------------------------
async def run_host(args):
    lan_ip = netutil.detect_lan_ip(args.subnet)
    admin_token = secrets.token_hex(8)
    store = AccountStore(args.data_file)
    server = GameServer(game_name=args.game_name or f"TypeRacer @ {lan_ip}",
                        admin_token=admin_token, store=store)

    join_hint = f"python typeracer.py join {lan_ip}"
    if args.port != P.DEFAULT_WS_PORT:
        join_hint += f" --port {args.port}"

    print(f"Hosting '{server.game_name}' on {lan_ip}:{args.port}")
    print(f"Other players join with:  {join_hint}")
    print(f"Accounts/stats stored in: {os.path.abspath(args.data_file)}")
    if not args.no_discovery:
        print("LAN auto-discovery is on (players can also just run: "
              "python typeracer.py join)")
    print("Starting...")

    discovery_transport = None
    async with serve(server.handler, "0.0.0.0", args.port):
        if not args.no_discovery:
            try:
                discovery_transport = await netutil.start_discovery_responder(
                    args.discovery_port, server.discovery_info
                )
            except OSError as exc:
                print(f"(discovery responder unavailable: {exc})")
        client = GameClient(
            uri=f"ws://127.0.0.1:{args.port}",
            admin_token=admin_token,
            host_hint=join_hint,
            prefill_username=default_username(args.name),
        )
        try:
            await client.run()
        finally:
            if discovery_transport is not None:
                discovery_transport.close()
            server.shutdown()


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------
def choose_host(args):
    """Resolve the host IP, discovering on the LAN when none was given."""
    if args.host:
        return args.host
    print("Searching the LAN for hosts...")
    hosts = netutil.discover_hosts(args.discovery_port, prefer_prefix=args.subnet)
    if not hosts:
        try:
            manual = input("No hosts found. Enter host IP (blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            manual = ""
        return manual or None
    if len(hosts) == 1:
        h = hosts[0]
        print(f"Found '{h.get('name')}' at {h['ip']}:{h.get('port')}")
        return h["ip"]
    print("Hosts found:")
    for i, h in enumerate(hosts, 1):
        print(f"  {i}. {h.get('name')}  ({h['ip']}:{h.get('port')}, "
              f"{h.get('players', '?')} players)")
    try:
        sel = input("Pick a number: ").strip()
        idx = int(sel) - 1
    except (ValueError, EOFError, KeyboardInterrupt):
        return None
    if 0 <= idx < len(hosts):
        return hosts[idx]["ip"]
    return None


async def run_join(args):
    host = choose_host(args)
    if not host:
        print("No host to connect to. Bye.")
        return
    join_hint = f"python typeracer.py join {host}"
    if args.port != P.DEFAULT_WS_PORT:
        join_hint += f" --port {args.port}"
    client = GameClient(
        uri=f"ws://{host}:{args.port}",
        host_hint=join_hint,
        prefill_username=default_username(args.name),
    )
    await client.run()


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------
def run_discover(args):
    print(f"Probing the LAN (UDP/{args.discovery_port}) for ~2s...")
    hosts = netutil.discover_hosts(args.discovery_port, timeout=2.0,
                                   prefer_prefix=args.subnet)
    if not hosts:
        print("No hosts found.")
        return
    print(f"Found {len(hosts)} host(s):")
    for h in hosts:
        print(f"  - {h.get('name')}  {h['ip']}:{h.get('port')}  "
              f"[{h.get('phase')}, {h.get('players', '?')} players]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="typeracer",
        description="LAN CLI multiplayer typing race.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    host = sub.add_parser("host", help="host a game and play in it")
    host.add_argument("--name", help="prefill for your login username")
    host.add_argument("--game-name", help="name shown to discovering players")
    host.add_argument("--data-file", default=DEFAULT_DATA_FILE,
                      help=f"accounts/stats JSON file (default {DEFAULT_DATA_FILE})")
    host.add_argument("--port", type=int, default=P.DEFAULT_WS_PORT,
                      help=f"websocket port (default {P.DEFAULT_WS_PORT})")
    host.add_argument("--discovery-port", type=int, default=P.DEFAULT_DISCOVERY_PORT,
                      help=f"UDP discovery port (default {P.DEFAULT_DISCOVERY_PORT})")
    host.add_argument("--subnet", default=None,
                      help="LAN subnet hint to pick the right interface, e.g. "
                           "192.168.20 or 10.0.0.0/24 (also $TYPERACER_SUBNET)")
    host.add_argument("--no-discovery", action="store_true",
                      help="disable UDP auto-discovery responder")

    join = sub.add_parser("join", help="join a game by IP or auto-discovery")
    join.add_argument("host", nargs="?", help="host IP (omit to auto-discover)")
    join.add_argument("--name", help="prefill for your login username")
    join.add_argument("--port", type=int, default=P.DEFAULT_WS_PORT,
                      help=f"websocket port (default {P.DEFAULT_WS_PORT})")
    join.add_argument("--discovery-port", type=int, default=P.DEFAULT_DISCOVERY_PORT,
                      help=f"UDP discovery port (default {P.DEFAULT_DISCOVERY_PORT})")
    join.add_argument("--subnet", default=None,
                      help="LAN subnet hint for auto-discovery, e.g. 192.168.20 "
                           "or 10.0.0.0/24 (also $TYPERACER_SUBNET)")

    disc = sub.add_parser("discover", help="list hosts found on the LAN")
    disc.add_argument("--discovery-port", type=int, default=P.DEFAULT_DISCOVERY_PORT,
                      help=f"UDP discovery port (default {P.DEFAULT_DISCOVERY_PORT})")
    disc.add_argument("--subnet", default=None,
                      help="LAN subnet hint for auto-discovery, e.g. 192.168.20 "
                           "or 10.0.0.0/24 (also $TYPERACER_SUBNET)")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "host":
            asyncio.run(run_host(args))
        elif args.command == "join":
            asyncio.run(run_join(args))
        elif args.command == "discover":
            run_discover(args)
    except KeyboardInterrupt:
        # Belt-and-suspenders: the raw-input layer normally handles Ctrl-C, but
        # if it fires before the TUI starts, exit cleanly anyway.
        print("\nInterrupted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
