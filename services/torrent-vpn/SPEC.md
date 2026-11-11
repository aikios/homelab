# torrent-vpn

Mullvad stays connected 24/7 (Mullvad auto-connect on, plus the watcher
reconnecting it), torrent traffic can never leave outside the tunnel, and
Tailscale keeps working alongside it. Runs on the media host as root
(`deploy/install.sh torrent-vpn`); `journalctl -u torrent-vpn -f`.

## Behaviour (poll every 5 s)
1. qBittorrent adds every torrent **stopped** (`add_stopped_enabled`, re-asserted
   by the watcher).
2. Stopped, incomplete torrents (`stoppedDL`) are pending: `mullvad connect --wait`,
   check `mullvad status --json` reports connected on `wg0-mullvad`, then start
   them. So the VPN is up before a torrent starts.
3. **Kill switch:** chain `TORRENT_VPN`, jumped to from `DOCKER-USER` for the
   qBittorrent container's IP(s): private destinations and `-o wg0-mullvad`
   return, everything else drops. Re-synced every poll (a recreated container
   gets a new IP; the new jump goes in before the stale one is removed).
4. `TORRENT_VPN_IDLE_SECS` = 0 (default): Mullvad is kept connected. Set > 0 to
   go back to VPN-only-while-torrenting: no active or pending torrent for that
   long disconnects.
5. **Tailscale:** Mullvad's firewall drops `tailscale0` traffic, and its routing
   rule (priority 5209, ahead of Tailscale's table 52) sends 100.64/10 into the
   tunnel. nft table `inet tailscale-mullvad` (loaded if missing, every poll)
   gives tailnet traffic Mullvad's exclusion marks (ct 0x00000f41, meta
   0x6d6f6c65), so it is accepted and routed via `tailscale0`. tailscaled's own
   encrypted packets (socket mark 0x80000) and inbound UDP 41641 get the same
   marks and use the home connection. Through Mullvad, inbound UDP never arrives,
   so peers were stuck on a DERP relay inside the tunnel, too slow to load media.
   Each (re)load flushes tailscaled's UDP conntrack entries (needs `conntrack`):
   flows opened through the tunnel keep an un-NAT'd tunnel source address.

Tag a stopped torrent `hold` to keep it stopped.

## Residual gaps
- After a container recreate, up to one poll passes before its new IP is
  covered; torrents still start stopped and wait for the VPN.
- qBittorrent's DHT node runs while the app runs; with the VPN down the kill
  switch drops its packets.

## Config (env, optional)
`TORRENT_VPN_QBIT_URL` (http://localhost:8080; WebUI localhost auth bypass),
`TORRENT_VPN_CONTAINER` (qbittorrent), `TORRENT_VPN_IDLE_SECS`, `TORRENT_VPN_POLL_SECS`.

## LAN multicast (2026-11-11)

With Mullvad always on, mDNS (224.0.0.251) had no route in the main table and fell through to
Mullvad's policy table, so it went into the tunnel. Home Assistant's HomeKit bridge was never
advertised on the LAN and Apple Home showed every plug as "No Response". The watcher now keeps
`224.0.0.0/4 dev <LAN>` in the main table (LAN = the main default route's device, or
`TORRENT_VPN_LAN_IF`). Home Assistant's network settings are also pinned to `enp5s0`, because its
automatic choice had become `wg0-mullvad`, which made it advertise the tunnel address.
