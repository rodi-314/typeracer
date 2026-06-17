"""LAN helpers: detect this host's address and discover hosts over UDP.

Discovery is best-effort and entirely optional -- a player can always join by
typing the host's IP directly. The host answers UDP probe datagrams broadcast
by joining clients so that ``join`` with no address can find a game.
"""

import asyncio
import os
import socket
import time

from protocol import (
    DISCOVERY_PROBE,
    DISCOVERY_REPLY_PREFIX,
    encode,
    decode,
)

# Optional LAN subnet hint. No subnet is hardcoded: by default the host's IP is
# auto-detected from the default route. A hint only helps disambiguate
# multi-homed machines and adds one extra discovery broadcast target. Configure
# it with the ``--subnet`` flag or the TYPERACER_SUBNET environment variable.
SUBNET_ENV = "TYPERACER_SUBNET"


def normalize_prefix(value):
    """Normalize a subnet hint to a 3-octet ``"a.b.c."`` prefix, or None.

    Accepts forms like ``192.168.20``, ``192.168.20.``, ``192.168.20.0`` or a
    CIDR such as ``192.168.20.0/24`` (the mask is treated as /24).
    """
    if not value:
        return None
    value = value.strip().split("/")[0]
    parts = [p for p in value.split(".") if p != ""]
    if len(parts) >= 3 and all(
        p.isdigit() and 0 <= int(p) <= 255 for p in parts[:3]
    ):
        return ".".join(parts[:3]) + "."
    return None


def _resolve_prefix(value):
    """Caller value, else the env var, normalized to a prefix or None."""
    if value is None:
        value = os.environ.get(SUBNET_ENV)
    return normalize_prefix(value)


def _is_private(ip):
    return (ip.startswith("10.") or ip.startswith("192.168.")
            or any(ip.startswith(f"172.{n}.") for n in range(16, 32)))


def _outbound_ip(target):
    """Source IP the OS would use to reach ``target`` (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


def _all_ipv4():
    ips = set()
    try:
        host = socket.gethostname()
        for res in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(res[4][0])
    except socket.gaierror:
        pass
    return ips


def detect_lan_ip(prefer_prefix=None):
    """Best guess at the address other players on the LAN should connect to.

    With a ``prefer_prefix`` hint (or TYPERACER_SUBNET), prefer an interface
    address on that subnet. Otherwise use the OS's outbound source address
    toward the internet (the default-route interface), falling back to any
    private address, then loopback.
    """
    prefer_prefix = _resolve_prefix(prefer_prefix)
    candidates = _all_ipv4()
    outbound = None
    try:
        outbound = _outbound_ip("8.8.8.8")
        candidates.add(outbound)
    except OSError:
        pass

    if prefer_prefix:
        try:
            candidates.add(_outbound_ip(prefer_prefix + "1"))
        except OSError:
            pass
        for ip in sorted(candidates):
            if ip.startswith(prefer_prefix):
                return ip

    if outbound and not outbound.startswith("127."):
        return outbound
    private = sorted(ip for ip in candidates if _is_private(ip))
    if private:
        return private[0]
    return "127.0.0.1"


def broadcast_addresses(prefer_prefix=None):
    """UDP broadcast targets to probe when discovering hosts."""
    prefer_prefix = _resolve_prefix(prefer_prefix)
    targets = ["255.255.255.255"]
    if prefer_prefix:
        targets.append(prefer_prefix + "255")
    try:
        ip = detect_lan_ip(prefer_prefix)
        octets = ip.split(".")
        if len(octets) == 4:
            targets.append(".".join(octets[:3] + ["255"]))
    except Exception:
        pass
    # De-duplicate while preserving order.
    seen = set()
    out = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Host side: answer discovery probes
# ---------------------------------------------------------------------------
class _DiscoveryResponder(asyncio.DatagramProtocol):
    def __init__(self, info_provider):
        self._info_provider = info_provider
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not data.startswith(DISCOVERY_PROBE):
            return
        try:
            payload = encode(self._info_provider()).encode("utf-8")
            self.transport.sendto(DISCOVERY_REPLY_PREFIX + payload, addr)
        except Exception:
            pass


async def start_discovery_responder(port, info_provider):
    """Bind a UDP responder; returns the transport (close it to stop)."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryResponder(info_provider),
        local_addr=("0.0.0.0", port),
        allow_broadcast=True,
    )
    return transport


# ---------------------------------------------------------------------------
# Client side: probe for hosts (synchronous, used at startup before the TUI)
# ---------------------------------------------------------------------------
def discover_hosts(port, timeout=1.5, prefer_prefix=None):
    """Broadcast a probe and collect host replies for ``timeout`` seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    sock.settimeout(0.3)
    hosts = {}
    try:
        for target in broadcast_addresses(prefer_prefix):
            try:
                sock.sendto(DISCOVERY_PROBE, (target, port))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(DISCOVERY_REPLY_PREFIX):
                continue
            try:
                info = decode(data[len(DISCOVERY_REPLY_PREFIX):])
            except Exception:
                continue
            info["ip"] = addr[0]
            hosts[(addr[0], info.get("port"))] = info
    finally:
        sock.close()
    return list(hosts.values())
