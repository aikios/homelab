# torrent-vpn

Mullvad is connected only while qBittorrent has work, and torrent traffic can
never leave outside the tunnel. Runs on the media host as root
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
4. No active or pending torrent for `TORRENT_VPN_IDLE_SECS` (300): disconnect.

Tag a stopped torrent `hold` to keep it stopped. The watcher owns the Mullvad
connection: a manual connect is undone after the idle time.

## Residual gaps
- After a container recreate, up to one poll passes before its new IP is
  covered; torrents still start stopped and wait for the VPN.
- qBittorrent's DHT node runs while the app runs; with the VPN down the kill
  switch drops its packets.

## Config (env, optional)
`TORRENT_VPN_QBIT_URL` (http://localhost:8080; WebUI localhost auth bypass),
`TORRENT_VPN_CONTAINER` (qbittorrent), `TORRENT_VPN_IDLE_SECS`, `TORRENT_VPN_POLL_SECS`.
