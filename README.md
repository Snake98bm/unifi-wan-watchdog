# UniFi WAN Watchdog

A small Docker web app that watches the WAN connections of a UniFi gateway (built on a UDM SE) and fixes a stuck
WAN on its own: it **bounces the WAN port** first and, if that doesn't help, **PoE power-cycles** the port the
modem is powered from. Before resetting anything it saves the modem's status pages, so you can see afterwards
why the line went down.

## What it does

- **Watches both WANs** through the UniFi API (API key) every 30 s.
- **Down for 5 min** or **stuck on a private IP for 5 min** (a modem that lost IP passthrough) → reset:
  1. bounce the gateway port (link down 5 s, modem stays powered), verified on the gateway;
  2. still bad after 3 min → PoE power-cycle the port.
- **Never resets both WANs at once**; if both need it, the WAN you choose goes first.
- **Safety checks before every reset:** the port must belong to that WAN in UniFi, match the WAN's interface,
  and the modem on it must match the fingerprint learned while it was healthy (PoE class and MAC vendor), so
  swapped cables are caught instead of power-cycling the wrong device.
- **Cooldown and daily limit** per WAN, **dry run** for the automation (the buttons always really act).
- **Modem logs:** saves modem pages before each power cycle, ~30 s after a WAN goes down, and hourly or daily:
  - **AT&T** gateways (BGW/NVG, `192.168.1.254`): status, broadband stats, logs; Syslog and Events with the
    device access code.
  - **Quectel RM520N-GL** with the RMIITools toolkit web UI: AT commands (signal, cell, carrier aggregation,
    temperatures, registration and reject causes, data session, IP passthrough) plus uptime.
  - the gateway's own syslog lines for that WAN port.

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
4. **Install the bounce access on the gateway.** It allows only check / bounce / wanlog for your WAN interfaces,
   locked with `command=` in `authorized_keys`:
   ```sh
   ssh root@<gateway> "WATCHDOG_PUBKEY='$(cat /docker/wan-watchdog/ssh/id_ed25519.pub)' WAN_IFACES='eth8 eth9' sh" < gateway/install-bounce.sh
   ```
5. **`.env`:** copy `.env.example` to `.env` and fill it in. `.env` is in `.gitignore`; never commit it.
6. **Start:** `docker compose up -d`, then open `http://<WATCHDOG_IP>:8080`. It starts in **dry run**.
7. **Modems behind the gateway:** the watchdog has to reach the modem's own address (e.g. `192.168.1.254`)
   through the right WAN. Add a UniFi policy-based route for that address via that WAN, with the watchdog's
   network as a source.

## Tests

```sh
docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python local/wan-watchdog:1 /tests/test_monitor.py
```
(also `test_config`, `test_capture`, `test_pages`, `test_atparse`, `test_unifi`, `test_bounce`, `test_main`).

## Notes

- A PoE power cycle only restarts a modem that is powered from that gateway port (PoE or a PoE splitter).
- UDM SE switch ports don't support `ethtool -r`, so the bounce is a 5 s link down/up.
- Use at your own risk: it power-cycles network equipment.

## License

[MIT](LICENSE)
