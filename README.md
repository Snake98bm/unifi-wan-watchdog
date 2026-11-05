# UniFi WAN Watchdog

A small Docker web app that watches the WAN connections of a UniFi gateway (built on a UDM SE) and fixes a stuck
WAN on its own: it **bounces the WAN port** first and, if that doesn't help, **PoE power-cycles** the port the
modem is powered from. Before resetting anything it saves the modem's status pages, so you can see afterwards
why the line went down.

## What it does

- **Watches both WANs** through the UniFi API (API key) every 30 s.
- **Real internet check per WAN:** a small probe container on a VLAN that the gateway routes out of that WAN
  only pings 1.1.1.1 / 8.8.8.8 and asks Cloudflare which public IP it left from. A WAN whose link and IP look
  fine but whose probe gets no reply is **offline**. Results are ignored when the probe's traffic left through
  the other WAN, when they're stale, or when every probe fails at once. Loss and latency are kept per hour
  (Internet page). It also catches bad UniFi data: if UniFi reports a WAN down while its probe is getting out
  through that WAN's own public IP (e.g. the Network app restarting), no down timer starts.
- **Down, offline, or stuck on a private IP for 5 min** (a modem that lost IP passthrough) → reset:
  1. bounce the gateway port (link down 5 s, modem stays powered), verified on the gateway;
  2. still bad after 3 min → PoE power-cycle the port.
- **VPN watch (optional):** a third probe on a VLAN that the gateway routes through a UniFi OpenVPN client only.
  When it gets no reply for 2 min, or when a ban-check site (Settings page, checked hourly) shows Cloudflare's
  "The owner of this website has banned your IP address" page (error 1006), the watchdog reconnects the OpenVPN
  client over the restricted SSH key. The reconnect looks the VPN server's name up again, which usually gives a
  new exit IP; the sites are checked again right after. It waits while a WAN is being reset or the primary WAN
  is unhealthy, has its own cooldown and daily limit, and logs an error (no reconnect) if the probe's traffic
  ever leaves through a WAN directly (broken kill switch).
- **Never resets both WANs at once**; if both need it, the WAN you choose goes first.
- **Safety checks before every reset:** the port must belong to that WAN in UniFi, match the WAN's interface,
  and the modem on it must match the fingerprint learned while it was healthy (PoE class and MAC vendor), so
  swapped cables are caught instead of power-cycling the wrong device.
- **Cooldown and daily limit** per WAN, **dry run** for the automation (the buttons always really act).
- **Modem logs:** saves modem pages before each power cycle, ~30 s after a WAN goes down, and hourly or daily:
  - **AT&T** gateways (BGW/NVG, `192.168.1.254`): status, broadband stats, logs; Syslog and Events with the
    device access code.
  - **Quectel RM520N-GL** with the [QManager](https://github.com/dr-dolomite/QManager-RM520N) web UI
    (`QUECTEL_PASSWORD`): AT commands (signal, cell, carrier aggregation, temperatures, registration and reject
    causes, data session, IP passthrough), QManager's status (uptime), network events, watchcat state and its
    own log. Read-only: the watchdog logs in, reads, and logs out; a refused password isn't retried, so
    QManager's login lockout never kicks in. (Older web UIs that take the AT command in the URL still work:
    put `{AT}` in the AT-command URL.)
  - the gateway's own syslog lines for that WAN port.

  **Bonded DSL checks** (AT&T, from the saved Broadband Status and System Information pages), shown on the WAN's
  dashboard card and logged once when they start and when they clear: a pair down while the other carries the
  line; total sync or a pair's max attainable down more than 10% since the box's last reboot; a pair whose max
  attainable after a reboot is under 60/10 Mbps; FEC or CRC errors in the last hour well above the pair's 7-day
  average. The limits are on the Settings page (DSL).

  Shown in the modem's own table style, by date, or merged over the last 30 days.
- **Web UI** with a password: status, confirmation pop-ups, event log, settings (saved in `/data`, applied
  live), diagnostics.

## Setup

1. **UniFi API key:** UniFi Network → Settings → Control Plane → Integrations → Create API Key
   (on the gateway itself, not unifi.ui.com).
2. **Build the image** on the Docker host:
   ```sh
   docker build -t local/wan-watchdog:1 .
   ```
3. **Data folder and SSH key** for the port bounce:
   ```sh
   mkdir -p /docker/wan-watchdog/ssh && chmod 700 /docker/wan-watchdog /docker/wan-watchdog/ssh
   ssh-keygen -t ed25519 -N "" -C wan-watchdog -f /docker/wan-watchdog/ssh/id_ed25519
   chown -R 1000:1000 /docker/wan-watchdog
   ```
4. **Install the bounce access on the gateway.** It allows only check / bounce / wanlog for your WAN interfaces
   (and vpnrestart for the VPN tunnels in `VPN_IFACES`, if any), locked with `command=` in `authorized_keys`:
   ```sh
   ssh root@<gateway> "WATCHDOG_PUBKEY='$(cat /docker/wan-watchdog/ssh/id_ed25519.pub)' WAN_IFACES='eth8 eth9' VPN_IFACES='' sh" < gateway/install-bounce.sh
   ```
5. **`.env`:** copy `.env.example` to `.env` and fill it in. `.env` is in `.gitignore`; never commit it.
6. **Start:** `docker compose up -d`, then open `http://<WATCHDOG_IP>:8080`. It starts in **dry run**.
7. **Internet probes (optional):** in UniFi, create one VLAN per WAN with DHCP on, and a policy-based route
   sending that VLAN's internet traffic out of that WAN only, **kill switch on**. Give the Docker host a VLAN
   interface for each (no IP needed), and set `WAN1_PROBE_PARENT` / `WAN2_PROBE_PARENT` in `.env`. The
   `wan1-probe` / `wan2-probe` containers get a DHCP address there, listen on nothing, and write
   `probes/<wan>.json` into the data folder. Without them, leave the two services out of the compose file.
   **VPN probe:** the same for a VLAN whose policy-based route goes through your UniFi VPN client (kill switch
   on): set `VPN_PROBE_PARENT`, then on the Settings page the VPN's tunnel interface (`ip link` on the gateway,
   e.g. `tunovpnc1`) and the ban-check sites. To let the watchdog reconnect it, run install-bounce.sh with
   `VPN_IFACES='tunovpnc1'` as well. Without a VPN, leave `vpn-probe` out.
8. **Modems behind the gateway:** the watchdog has to reach the modem's own address (e.g. `192.168.1.254`)
   through the right WAN. Add a UniFi policy-based route for that address via that WAN, with the watchdog's
   network as a source.

## Tests

```sh
docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python local/wan-watchdog:1 /tests/test_monitor.py
```
(also `test_probe`, `test_vpn`, `test_config`, `test_capture`, `test_pages`, `test_atparse`, `test_unifi`, `test_bounce`, `test_main`).

## Notes

- A PoE power cycle only restarts a modem that is powered from that gateway port (PoE or a PoE splitter).
- UDM SE switch ports don't support `ethtool -r`, so the bounce is a 5 s link down/up.
- Use at your own risk: it power-cycles network equipment.

## License

[MIT](LICENSE)
