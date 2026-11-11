# watch.py -- torrent-vpn daemon (runs on the media host, as root).
# Keeps Mullvad connected (always, or only while qBittorrent has work when
# TORRENT_VPN_IDLE_SECS > 0), and makes sure no torrent traffic ever leaves
# outside the tunnel:
#   1. qBittorrent adds every torrent stopped (add_stopped_enabled).
#   2. Stopped, incomplete torrents are "pending": connect Mullvad, wait until it
#      reports connected on its tunnel interface, then start them.
#   3. Kill switch: iptables lets the qBittorrent container reach only private
#      addresses and the Mullvad tunnel; everything else is dropped, so a VPN drop
#      or a race can't leak the home IP.
#   4. IDLE_SECS > 0 and no active or pending torrents for that long: disconnect.
#      IDLE_SECS = 0 (default): Mullvad stays connected and is reconnected if it drops.
#   5. Tailscale bypass: Mullvad's firewall drops tailscale0 traffic and its policy
#      routing sends 100.64/10 into the tunnel. An nft table gives tailnet traffic
#      Mullvad's exclusion marks so it uses tailscale0. tailscaled's own encrypted
#      packets (socket mark 0x80000) are excluded too and use the home connection:
#      through Mullvad, inbound UDP never arrives, so peers fall back to a DERP relay
#      tunnelled inside Mullvad, too slow to load Jellyfin.
#   6. LAN multicast: the main table has no multicast route, so mDNS (224.0.0.251)
#      fell through to Mullvad's table and went into the tunnel. Home Assistant's
#      HomeKit bridge then never reached the LAN and Apple Home showed every plug
#      as "No Response". A 224.0.0.0/4 route on the LAN interface keeps it local.
# Tag a stopped torrent "hold" to keep it stopped.

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

QBIT_URL = os.environ.get("TORRENT_VPN_QBIT_URL", "http://localhost:8080")
CONTAINER = os.environ.get("TORRENT_VPN_CONTAINER", "qbittorrent")
IDLE_SECS = int(os.environ.get("TORRENT_VPN_IDLE_SECS", "0"))  # 0 = always on
POLL_SECS = int(os.environ.get("TORRENT_VPN_POLL_SECS", "5"))
TUNNEL = "wg0-mullvad"
CHAIN = "TORRENT_VPN"
HOLD_TAG = "hold"
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
MULTICAST = "224.0.0.0/4"
LAN_IF = os.environ.get("TORRENT_VPN_LAN_IF", "")  # default: the main table's default-route device
TS_TABLE = "tailscale-mullvad"
TS_PORT = 41641  # tailscaled's WireGuard port
# Mullvad's split-tunnel marks: ct mark lets its firewall pass a flow, meta mark
# skips its "into the tunnel" routing rule.
EXCLUDE = "ct mark set 0x00000f41 meta mark set 0x6d6f6c65"
TS_RULESET = f"""table inet {TS_TABLE} {{
  chain output {{
    type route hook output priority -100; policy accept;
    ip daddr 100.64.0.0/10 {EXCLUDE}
    ip6 daddr fd7a:115c:a1e0::/48 {EXCLUDE}
    meta mark & 0x00ff0000 == 0x00080000 {EXCLUDE}
  }}
  chain input {{
    type filter hook input priority -100; policy accept;
    iifname "tailscale0" {EXCLUDE}
    iifname != "{TUNNEL}" udp dport {TS_PORT} {EXCLUDE}
  }}
}}
"""
# qBittorrent 5 states that move data or talk to peers/trackers
ACTIVE = {"downloading", "stalledDL", "metaDL", "forcedMetaDL", "forcedDL", "queuedDL",
          "checkingDL", "allocating", "uploading", "stalledUP", "forcedUP", "queuedUP",
          "checkingUP", "checkingResumeData", "moving"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg, flush=True)


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# --- kill switch -------------------------------------------------------------

def chain_rules():
    """The TORRENT_VPN chain, as `iptables -S` prints it."""
    rules = [f"-A {CHAIN} -d {net} -j RETURN" for net in PRIVATE]
    return [f"-N {CHAIN}"] + rules + [f"-A {CHAIN} -o {TUNNEL} -j RETURN", f"-A {CHAIN} -j DROP"]


def jump_rule(ip):
    return f"-A DOCKER-USER -s {ip}/32 -j {CHAIN}"


def kill_switch_plan(chain_now, docker_user_now, ips):
    """iptables commands (argv lists) that bring the chain and the DOCKER-USER
    jumps to the wanted state. Missing DROP is added before anything is removed."""
    cmds = []
    if chain_now != chain_rules():
        if not chain_now:
            cmds.append(["-N", CHAIN])
        else:
            cmds.append(["-F", CHAIN])
        for r in chain_rules()[1:]:
            cmds.append(r.split())
    want = {jump_rule(ip) for ip in ips}
    have = {r for r in docker_user_now if r.endswith("-j " + CHAIN)}
    for r in sorted(want - have):
        cmds.append(["-I", "DOCKER-USER", "1"] + r.split()[2:])
    for r in sorted(have - want):
        cmds.append(["-D"] + r.split()[1:])
    return cmds


def container_ips():
    r = sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", CONTAINER)
    return [ip for ip in r.stdout.split() if ip] if r.returncode == 0 else []


def ensure_kill_switch():
    chain = sh("iptables", "-S", CHAIN)
    docker_user = sh("iptables", "-S", "DOCKER-USER")
    if docker_user.returncode != 0:
        raise RuntimeError("no DOCKER-USER chain (is docker running?)")
    chain_now = chain.stdout.splitlines() if chain.returncode == 0 else []
    for c in kill_switch_plan(chain_now, docker_user.stdout.splitlines(), container_ips()):
        r = sh("iptables", *c)
        if r.returncode != 0:
            raise RuntimeError("iptables " + " ".join(c) + ": " + r.stderr.strip())
        log("iptables " + " ".join(c))


_ts_loaded = False


def ensure_tailscale_bypass():
    """(Re)load the table at startup and whenever it's missing. Then drop
    tailscaled's UDP conntrack entries: flows created while they still went into
    the tunnel keep its source address un-NAT'd, and disco pings keep them alive."""
    global _ts_loaded
    if _ts_loaded and sh("nft", "list", "table", "inet", TS_TABLE).returncode == 0:
        return
    ruleset = f"table inet {TS_TABLE}\ndelete table inet {TS_TABLE}\n" + TS_RULESET
    r = subprocess.run(["nft", "-f", "-"], input=ruleset, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError("nft: " + r.stderr.strip())
    sh("conntrack", "-D", "-p", "udp", "--sport", str(TS_PORT))
    _ts_loaded = True
    log(f"loaded nft table inet {TS_TABLE} (Tailscale alongside Mullvad)")


def lan_interface(main_default_route):
    """Device of the main table's default route ('default via 192.168.1.1 dev enp5s0 ...')."""
    parts = main_default_route.split()
    return parts[parts.index("dev") + 1] if "dev" in parts else ""


def multicast_plan(route_now, lan_if):
    """ip-route args that put multicast on the LAN interface, or [] if already there."""
    if not lan_if or f"dev {lan_if}" in route_now:
        return []
    return ["replace", MULTICAST, "dev", lan_if]


def ensure_lan_multicast():
    lan_if = LAN_IF or lan_interface(sh("ip", "route", "show", "table", "main", "default").stdout)
    plan = multicast_plan(sh("ip", "route", "show", "table", "main", MULTICAST).stdout, lan_if)
    if plan:
        r = sh("ip", "route", *plan)
        if r.returncode != 0:
            raise RuntimeError("ip route: " + r.stderr.strip())
        log(f"multicast {MULTICAST} routed on {lan_if} (mDNS stays off the tunnel)")


# --- qBittorrent / Mullvad ---------------------------------------------------

def qbit(path, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with urllib.request.urlopen(QBIT_URL + "/api/v2/" + path, data=body, timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"[", b"{") else raw.decode()


def classify(torrents):
    """(pending hashes, active?) — pending = stopped, incomplete, not on hold."""
    pending = [t["hash"] for t in torrents
               if t.get("state") == "stoppedDL" and HOLD_TAG not in (t.get("tags") or "").split(", ")]
    active = any(t.get("state") in ACTIVE for t in torrents)
    return pending, active


def mullvad_connected():
    r = sh("mullvad", "status", "--json")
    try:
        s = json.loads(r.stdout)
    except ValueError:
        return False
    return s.get("state") == "connected" and \
        (s.get("details") or {}).get("endpoint", {}).get("tunnel_interface") == TUNNEL


def main():
    mode = f"idle {IDLE_SECS}s" if IDLE_SECS else "always on"
    log(f"torrent-vpn up (qBittorrent {QBIT_URL}, container {CONTAINER}, Mullvad {mode})")
    idle_since = None
    while True:
        try:
            ensure_kill_switch()
            ensure_tailscale_bypass()
            ensure_lan_multicast()
            if not qbit("app/preferences").get("add_stopped_enabled"):
                qbit("app/setPreferences", {"json": json.dumps({"add_stopped_enabled": True})})
                log("qBittorrent now adds torrents stopped")
            pending, active = classify(qbit("torrents/info"))
            connected = mullvad_connected()
            if pending or active or not IDLE_SECS:
                idle_since = None
                if not connected:
                    log(f"connecting Mullvad ({len(pending)} pending, active: {active})")
                    sh("mullvad", "connect", "--wait")
                    connected = mullvad_connected()
                    log("Mullvad connected" if connected else "Mullvad did not connect; torrents stay stopped")
                if connected and pending:
                    qbit("torrents/start", {"hashes": "|".join(pending)})
                    log(f"started {len(pending)} torrent(s) inside the tunnel")
            else:
                idle_since = idle_since or time.time()
                if connected and time.time() - idle_since >= IDLE_SECS:
                    log(f"no torrents for {IDLE_SECS}s: disconnecting Mullvad")
                    sh("mullvad", "disconnect", "--wait")
        except Exception as e:  # noqa: BLE001 — the daemon loop never exits on error
            log("error: " + str(e)[:300])
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
