"""WAN watchdog logic: watch both WANs and reset a WAN that is stuck.

Rules
- A WAN is "down" when UniFi says it isn't up or it has no IP. Down for DOWN_MINUTES -> reset.
- A WAN is "offline" when its link and IP look fine but its internet probe (a small container on a VLAN that
  the gateway routes out of that WAN only, see probe.py) got no reply at all PROBE_FAILS_OFFLINE results in a
  row. Offline counts like down (same DOWN_MINUTES timer). A probe result is ignored when it is stale, when its
  traffic left through another WAN (policy route broken), or when every probe fails at once (more likely a
  local problem than every ISP).
- The probe also works the other way: when UniFi reports a WAN down (no link / no IP) but its probe just got
  out through that WAN's own public IP, UniFi's data is treated as stale (e.g. the Network app restarting) and
  the down timer doesn't start.
- A WAN with a private IP (modem lost passthrough) is re-checked; still private after
  PRIVATE_MINUTES -> reset.
- An automatic reset first BOUNCES the gateway port (link renegotiation over the restricted SSH key, modem stays
  powered) when bounce access works. If the WAN is still bad BOUNCE_WAIT_MINUTES later, the port's PoE
  is power-cycled. Without working bounce access it goes straight to the PoE power cycle.
- Only ONE WAN is ever being reset: after a bounce or cycle, nothing else is touched until that WAN
  is healthy again (at least 2 minutes later) or its wait window has passed.
- If both need a reset, the WAN listed first in WANS (AT&T) goes first.
- Per WAN: COOLDOWN_MINUTES between PoE cycles and at most MAX_CYCLES_PER_DAY (dry-run cycles are counted
  separately, so they never use up the real limit).
- Bounce access is checked at startup and whenever the gateway firmware changes, so a firmware update
  that drops the key is noticed and logged. If it stops working while running, it is re-checked every
  BOUNCE_RETRY_SECONDS until it works again.
- Dry run logs what it WOULD do on its own without touching the port. The Bounce / Power-cycle
  buttons on the web page always really act (they still follow the one-WAN-at-a-time rule).
- The Bounce button only bounces: the PoE power cycle is the automation's backup step, never a
  follow-up to a manual bounce.

VPN (optional, see Vpn below): a probe on a VLAN that the gateway routes through a UniFi OpenVPN client only.
- No reply VPN_DOWN_MINUTES long, or a ban-check site showing Cloudflare's "banned your IP address" page ->
  reconnect the OpenVPN client over the restricted SSH key (vpnrestart). The reconnect looks the server name up
  again, which usually gives a new exit IP.
- Not while a WAN is being reset, not while the primary WAN is unhealthy, not while every probe fails at once
  (the VPN rides on the WANs, so those are fixed first).
- VPN_COOLDOWN_MINUTES between reconnects, at most VPN_MAX_RESTARTS_PER_DAY; dry run applies as for the WANs.
- Probe traffic leaving through a WAN's own IP means the VPN's kill switch / policy route is broken: logged as an
  error, no reconnect (a reconnect wouldn't fix that).

Locking: decisions and state changes happen under self.lock; slow I/O (SSH, modem pages, the UniFi power-cycle
call) runs without it, so the web pages stay responsive. The one-WAN-at-a-time slot (self.busy) is reserved
under the lock before the I/O starts.
"""
import json
import os
import threading
import time
from datetime import date
from urllib.parse import urlparse

from atparse import gateway_log_html, text_html
from unifi import UniFiError, is_private
from version import VERSION

MIN_RECOVERY_SECONDS = 120
EVENT_CAPTURE_CHECKS = 2          # bad for this many checks in a row (~30 s) -> save the modem's state now
EVENT_CAPTURE_EVERY = 3600        # ... but at most once an hour per WAN (AT&T's latency flapping)
BOUNCE_RETRY_SECONDS = 1800       # bounce access lost while running -> re-check this often until it works
PENDING_MAX_SECONDS = 900         # a reserved slot whose action never finished is freed after this
EXPECTED_NETWORK = {"wan1": "wan", "wan2": "wan2"}   # UniFi's port_table network_name for each WAN
PROBE_FAILS_OFFLINE = 2           # probe results in a row with no reply at all -> "offline"
PROBE_STALE_SECONDS = 90          # an older probe result (or 3 probe intervals) is ignored
VPN_KEY = "vpn"                   # the VPN probe writes probes/vpn.json; events and history use this key
VPN_SETTLE_SECONDS = 15           # probe results from the first seconds after a reconnect are ignored


def mac_vendor(mac):
    """Manufacturer part of a MAC (aa:bb:cc), or None for a locally administered MAC, which can change."""
    try:
        return None if int(mac.split(":")[0], 16) & 0x02 else mac.lower()[:8]
    except (AttributeError, ValueError):
        return None


class Wan:
    def __init__(self, key, label, port, priority):
        self.key, self.label, self.port, self.priority = key, label, int(port), priority
        self.seen = False
        self.up = None
        self.ip = None
        self.ifname = None
        self.link = None
        self.poe = None
        self.down_since = None
        self.private_since = None
        self.bounced_at = None     # bounce already tried for the current problem
        self.last_cycle = None
        self.last_bounce = None
        self.cycle_day = None
        self.cycles_today = 0
        self.dry = {"last_cycle": None, "day": None, "cycles": 0}   # dry-run cycles, kept apart from real ones
        self.note = ""
        # what UniFi says about the WAN and its port, for the safety checks
        self.is_uplink = False
        self.wan_port_idx = None
        self.port_network = None
        self.port_ifname = None
        self.poe_class = None
        self.modem_vendor = None
        self.problems = []
        self.problems_logged = None
        self.bad_checks = 0               # checks in a row that were down / private
        self.last_event_capture = None
        self.capturing = None             # background capture thread
        # internet probe (probe.py): latest result, what the watchdog made of it, and no-reply results in a row
        self.probe = None
        self.probe_verdict = "off"        # off / none / unknown / ok / fail / wrong-path
        self.probe_note = ""
        self.probe_ts = None
        self.probe_fails = 0
        self.wrong_path_logged = False
        self.stale_unifi = False          # UniFi says down but the probe proves the WAN works

    @property
    def fingerprint(self):
        return {k: v for k, v in (("poe_class", self.poe_class), ("vendor", self.modem_vendor)) if v}

    @property
    def private(self):
        return bool(self.ip) and is_private(self.ip)

    @property
    def offline(self):
        return self.probe_fails >= PROBE_FAILS_OFFLINE

    @property
    def healthy(self):
        return self.state == "up"

    @property
    def state(self):
        if not self.seen:
            return "unknown"
        if not self.up or not self.ip:
            return "down"
        if self.offline:
            return "offline"
        return "private" if self.private else "up"

    def as_dict(self, now):
        return {
            "key": self.key, "label": self.label, "port": self.port, "state": self.state,
            "name": "WAN " + self.key[3:] if self.key.startswith("wan") else self.key,
            "ip": self.ip, "ifname": self.ifname, "link": self.link, "poe": self.poe,
            "down_for": int(now - self.down_since) if self.down_since else None,
            "private_for": int(now - self.private_since) if self.private_since else None,
            "last_cycle": self.last_cycle, "last_bounce": self.last_bounce,
            "cycles_today": self.cycles_today, "note": self.note,
            "problems": list(self.problems), "fingerprint": self.fingerprint,
            "port_network": self.port_network, "port_ifname": self.port_ifname, "is_uplink": self.is_uplink,
            "probe": self.probe, "probe_verdict": self.probe_verdict, "probe_note": self.probe_note,
            "probe_age": int(now - self.probe["ts"]) if self.probe and self.probe.get("ts") else None,
        }


class Vpn:
    """The VPN as its probe sees it, plus the reconnect counters."""

    def __init__(self):
        self.verdict = "off"              # off / none / unknown / ok / fail / banned / leak
        self.note = ""
        self.probe = None
        self.probe_ts = None
        self.fails = 0                    # no-reply results in a row
        self.offline_since = None
        self.last_restart = None          # last real reconnect (also failed ones: the cooldown applies)
        self.restart_day = None
        self.restarts_today = 0
        self.dry = {"last": None, "day": None, "count": 0}
        self.restarting = False
        self.recheck = 0.0                # asks the probe to check the ban sites again (after a reconnect)
        self.action_note = ""
        self.hold_logged = None
        self.logged_state = None

    @property
    def state(self):
        if self.verdict == "off":
            return "off"
        if self.verdict in ("none", "unknown"):
            return "unknown"
        if self.verdict in ("banned", "leak"):
            return self.verdict
        return "offline" if self.fails >= PROBE_FAILS_OFFLINE else "up"

    def as_dict(self, now):
        trace = (self.probe or {}).get("trace") or {}
        return {"state": self.state, "verdict": self.verdict, "note": self.note, "exit_ip": trace.get("exit_ip"),
                "probe": self.probe, "probe_age": int(now - self.probe["ts"]) if self.probe and self.probe.get("ts")
                else None, "ban": (self.probe or {}).get("ban"), "offline_for": int(now - self.offline_since)
                if self.offline_since else None, "last_restart": self.last_restart,
                "restarts_today": self.restarts_today, "restarting": self.restarting, "action_note": self.action_note}


class Monitor(threading.Thread):
    def __init__(self, cfg, client, events, settings, clock=time.time, capture=None, bouncer=None,
                 probe_dir=None, probe_history=None):
        super().__init__(daemon=True, name="wan-monitor")
        self.cfg, self.client, self.events, self.settings, self.clock = cfg, client, events, settings, clock
        self.capture = capture    # optional: saves a modem's pages before its port is cycled
        self.bouncer = bouncer    # optional: link bounce over the restricted SSH key
        self.probe_dir = probe_dir            # optional: where the probe containers write <wan>.json
        self.probe_history = probe_history    # optional: store.Probes
        self._all_probes_failing = False
        self.wans = [Wan(k, label, port, i) for i, (k, label, port) in enumerate(cfg["wans"])]
        self.lock = threading.RLock()
        self.busy = None          # {"wan", "since", "until", "what", "pending"} while a reset runs / recovers
        self.gw_mac = None
        self.gw_version = None
        self.last_check = None
        self.last_error = None
        self._outage = None       # {"since", "version", "ids"} while the gateway API can't be read
        self.bounce_access = {"ok": None, "checked": None, "detail": "not checked yet", "lost": False}
        self.async_captures = True
        self._waiting_logged = None
        self.vpn = Vpn()
        self._vpn_conf = None                 # what was last written to probes/vpn.conf.json
        self._vpn_conf_error = None
        self._stop = threading.Event()

    # ---------- loop ----------
    def run(self):
        self.events.add("info", None, "Watchdog v%s started (dry run: %s, automation: %s)" % (
            VERSION, self.settings.get("dry_run"), self.settings.get("enabled")))
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as e:  # keep the loop alive no matter what
                self._error(f"check failed: {e}")
            self._stop.wait(self.cfg["check_interval"])

    def stop(self):
        self._stop.set()

    def _error(self, msg):
        """Log an error once (repeats are dropped); returns the event's row id, or None if not logged."""
        with self.lock:
            rowid = self.events.add("error", None, msg) if msg != self.last_error else None
            self.last_error = msg
            return rowid

    # ---------- one check ----------
    def check(self):
        try:
            gw = self.client.gateway()
        except UniFiError as e:
            rowid = self._error(f"Can't read the gateway: {e} (no action taken)")
            with self.lock:
                if self._outage is None:
                    self._outage = {"since": self.clock(), "ids": [],
                                    "version": self.gw_version or self.settings.get("gw_firmware")}
                self._outage["ids"].append(rowid)
            return
        now = self.clock()
        self._watch_firmware(gw.get("version"), now)          # may SSH: done before taking the lock
        probes = {w.key: self._read_probe(w.key) for w in self.wans}
        vpn_probe = self._read_probe(VPN_KEY)
        with self.lock:
            if self.last_error:
                self.events.add("info", None, "Gateway API reachable again")
            self.last_error = None
            self._explain_outage(gw.get("version"), now)
            self.last_check = now
            self.gw_mac = gw.get("mac")
            ports = {p.get("port_idx"): p for p in gw.get("port_table", [])}
            infos = {w.key: gw.get(w.key) or {} for w in self.wans}
            verdicts = self._probe_verdicts(infos, probes, now)
            for w in self.wans:
                self._update(w, infos[w.key], ports.get(w.port) or {}, now, verdicts.get(w.key))
            self._release_busy(now)
            self._update_vpn(vpn_probe, infos, now)
            plan = self._decide(now) if self.settings.get("enabled") else None
            vpn_plan = self._decide_vpn(now)                  # after _decide: a WAN reset holds the VPN back
        if plan:                                              # slow I/O runs without the lock
            w, step, why = plan
            if step == "bounce":
                self._bounce(w, why, now)
            else:
                self._cycle(w, why, now, manual=False)
        if vpn_plan:
            self._vpn_restart(vpn_plan, now, manual=False)
        self._sync_vpn_conf()

    def _explain_outage(self, version, now):
        """The API just came back. If the firmware changed meanwhile, the outage was the gateway installing
        it: say so, and turn the errors logged during it into warnings so the night doesn't read as a fault."""
        outage, self._outage = self._outage, None
        if not outage or not outage["version"] or not version or version == outage["version"]:
            return
        ids = [r for r in outage["ids"] if r]
        self.events.set_level(ids, "warn")
        hm = lambda t: time.strftime("%H:%M:%S", time.localtime(t))  # noqa: E731
        mins = max(1, round((now - outage["since"]) / 60))
        self.events.add("info", None, f"The gateway was rebooting for a firmware upgrade {outage['version']} -> "
                                      f"{version}: API unreachable for about {mins} min ({hm(outage['since'])} "
                                      f"to {hm(now)}). The {len(ids)} error(s) logged meanwhile were the upgrade, "
                                      f"not a fault, and are now marked warn")

    # ---------- bounce access / firmware survival ----------
    def _watch_firmware(self, version, now):
        known = self.settings.get("gw_firmware")
        changed = bool(version and known and version != known)
        if version and version != known:
            self.settings.set(gw_firmware=version)
        self.gw_version = version
        access = self.bounce_access
        startup = access["checked"] is None
        retry = access["lost"] and now - (access["checked"] or 0) >= BOUNCE_RETRY_SECONDS
        if self.bouncer and self.bouncer.configured() and (startup or changed or retry):
            self.check_bounce_access(now, firmware_change=(known, version) if changed else None)
        elif changed:
            self.events.add("info", None, f"Gateway firmware changed {known} -> {version}")

    def check_bounce_access(self, now=None, firmware_change=None):
        """SSH check of the restricted key (no lock held during the SSH call)."""
        now = now or self.clock()
        was_ok = self.bounce_access["ok"]
        ok, detail = self.bouncer.check()
        with self.lock:
            # "lost" = it worked earlier in this run, so keep retrying; never-installed isn't retried
            lost = not ok and (was_ok is True or self.bounce_access["lost"])
            self.bounce_access = {"ok": ok, "checked": now, "detail": detail, "lost": lost}
        if firmware_change:
            old, new = firmware_change
            if ok:
                self.events.add("info", None, f"Gateway firmware changed {old} -> {new} and the port-bounce "
                                              f"access SURVIVED the upgrade")
            else:
                self.events.add("error", None, f"Gateway firmware changed {old} -> {new} and the port-bounce "
                                               f"access was LOST ({detail}). Run gateway/install-bounce.sh again; "
                                               f"until then problems go straight to the PoE power cycle")
        elif ok and was_ok is not True:
            self.events.add("info", None, f"Port-bounce access works (gateway {detail})")
        elif not ok and was_ok is not False:
            self.events.add("error", None, f"Port-bounce access NOT working: {detail}. "
                                           f"Problems go straight to the PoE power cycle")
        return ok

    # ---------- internet probes ----------
    def _read_probe(self, key):
        if not self.probe_dir:
            return None
        try:
            with open(os.path.join(self.probe_dir, f"{key}.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _wan_ip(info):
        ip = info.get("ip") or None
        return None if ip in ("0.0.0.0", "") else ip

    def _probe_verdict(self, probe, ip, other_ips, now):
        """(verdict, note) for one WAN's latest probe result."""
        if not self.probe_dir or not self.settings.get("probe_check"):
            return "off", ""
        if not probe:
            return "none", "no probe result (probe container not running?)"
        age = now - float(probe.get("ts") or 0)
        if age > max(PROBE_STALE_SECONDS, 3 * int(probe.get("interval") or 30)):
            return "unknown", f"probe result is {int(age // 60)} min old - probe container stopped?"
        state, detail = probe.get("state"), probe.get("detail") or ""
        if state == "ok":
            exit_ip = (probe.get("trace") or {}).get("exit_ip")
            if exit_ip and exit_ip != ip and exit_ip in other_ips:
                return "wrong-path", (f"probe traffic left through another WAN ({exit_ip}): check the policy "
                                      f"route and kill switch for the probe's VLAN")
            return "ok", detail
        if state == "fail":
            return "fail", detail
        return "unknown", detail or "probe has no result yet"

    def _probe_verdicts(self, infos, probes, now):
        """{wan: (verdict, note, probe)}. If every WAN that UniFi shows up fails its probe at the same time,
        none of them is trusted: that points at the probes / Docker host / gateway rather than every ISP."""
        ips = {k: self._wan_ip(i) for k, i in infos.items()}
        out = {}
        for w in self.wans:
            others = {ip for k, ip in ips.items() if k != w.key and ip}
            out[w.key] = self._probe_verdict(probes.get(w.key), ips.get(w.key), others, now) + (probes.get(w.key),)
        linked = [k for k, i in infos.items() if i.get("up") and ips.get(k) and out[k][0] in ("ok", "fail")]
        failing = [k for k in linked if out[k][0] == "fail"]
        if len(linked) > 1 and len(failing) == len(linked):
            if not self._all_probes_failing:
                self._all_probes_failing = True
                self.events.add("warn", None, "Every internet probe is failing at once - more likely a local problem "
                                              "(probe VLANs, Docker host, gateway) than every ISP; not acting on "
                                              "probe results until one answers again")
            out = {k: ("unknown", "all probes failing at once: " + v[1], v[2]) if k in failing else v
                   for k, v in out.items()}
        elif self._all_probes_failing and len(failing) < len(linked):
            self._all_probes_failing = False
            self.events.add("info", None, "Internet probes are answering again")
        return out

    @staticmethod
    def _probe_vouches(w):
        """The probe's latest (fresh, trusted) result reached the internet through this WAN's last known IP."""
        if not (w.seen and w.ip and w.probe_verdict == "ok" and w.probe):
            return False
        return (w.probe.get("trace") or {}).get("exit_ip") == w.ip

    def _track_probe(self, w, verdict):
        verdict, note, probe = verdict or ("off", "", None)
        w.probe_verdict, w.probe_note = verdict, note
        w.probe = probe if verdict != "off" else None
        new = bool(probe) and verdict != "off" and probe.get("ts") != w.probe_ts
        if verdict != "fail":
            w.probe_fails = 0
        elif new:
            w.probe_fails += 1
        if not new:
            return
        w.probe_ts = probe.get("ts")
        if verdict == "wrong-path":
            if not w.wrong_path_logged:
                self.events.add("error", w.key, f"{w.label}'s internet probe is not testing {w.label}: {note}. "
                                                f"Ignoring it until that's fixed")
                w.wrong_path_logged = True
        elif w.wrong_path_logged:
            w.wrong_path_logged = False
            self.events.add("info", w.key, f"{w.label}'s internet probe goes out through {w.label} again")
        if self.probe_history and verdict in ("ok", "fail"):
            pings = probe.get("targets") or []
            rtts = [p["avg_ms"] for p in pings if p.get("avg_ms") is not None]
            self.probe_history.add(probe["ts"], w.key, verdict, sum(p.get("sent", 0) for p in pings),
                                   sum(p.get("recv", 0) for p in pings), (sum(rtts) / len(rtts)) if rtts else None,
                                   (probe.get("trace") or {}).get("exit_ip"))

    # ---------- VPN ----------
    @property
    def vpn_label(self):
        return self.settings.get("vpn_label") or "VPN"

    def _vpn_verdict(self, probe, wan_ips, now):
        """(verdict, note) for the VPN probe's latest result."""
        v = self.vpn
        if not self.probe_dir or not self.settings.get("vpn_check"):
            return "off", ""
        if not probe:
            return "none", "no VPN probe result (vpn-probe container not running?)"
        ts = float(probe.get("ts") or 0)
        age = now - ts
        if age > max(PROBE_STALE_SECONDS, 3 * int(probe.get("interval") or 30)):
            return "unknown", f"VPN probe result is {int(age // 60)} min old - probe container stopped?"
        if v.last_restart and ts < v.last_restart + VPN_SETTLE_SECONDS:
            return "unknown", "waiting for a probe result from after the reconnect"
        state, detail = probe.get("state"), probe.get("detail") or ""
        if state == "fail":
            return "fail", detail
        if state != "ok":
            return "unknown", detail or "probe has no result yet"
        exit_ip = (probe.get("trace") or {}).get("exit_ip")
        if exit_ip and exit_ip in wan_ips:
            return "leak", (f"probe traffic left through a WAN ({exit_ip}), not the VPN: check the VPN's policy "
                            f"route and its kill switch")
        ban = probe.get("ban") or {}
        fresh = bool(ban) and float(ban.get("ts") or 0) >= (v.last_restart or 0)
        if fresh and ban.get("banned"):
            sites = ", ".join(urlparse(s.get("url", "")).hostname or "?" for s in ban.get("sites") or []
                              if s.get("banned"))
            return "banned", f"{sites} banned the exit IP {exit_ip} (Cloudflare)"
        if ban:
            detail += "; ban check OK" if fresh else "; ban check pending"
        return "ok", detail

    def _update_vpn(self, probe, infos, now):
        """Track the VPN probe (call with the lock held)."""
        v = self.vpn
        wan_ips = {ip for ip in (self._wan_ip(i) for i in infos.values()) if ip}
        verdict, note = self._vpn_verdict(probe, wan_ips, now)
        v.verdict, v.note = verdict, note
        v.probe = probe if verdict != "off" else None
        new = bool(probe) and verdict != "off" and probe.get("ts") != v.probe_ts
        if verdict != "fail":
            v.fails = 0
        elif new:
            v.fails += 1
        if new:
            v.probe_ts = probe.get("ts")
            if self.probe_history and verdict in ("ok", "fail", "banned"):
                pings = probe.get("targets") or []
                rtts = [p["avg_ms"] for p in pings if p.get("avg_ms") is not None]
                self.probe_history.add(probe["ts"], VPN_KEY, verdict, sum(p.get("sent", 0) for p in pings),
                                       sum(p.get("recv", 0) for p in pings),
                                       (sum(rtts) / len(rtts)) if rtts else None,
                                       (probe.get("trace") or {}).get("exit_ip"))
        v.offline_since = (v.offline_since or now) if v.state == "offline" else None
        state = v.state
        if state == v.logged_state or state == "off" or (state == "unknown" and "after the reconnect" in note):
            return
        exit_ip = ((probe or {}).get("trace") or {}).get("exit_ip")
        label = self.vpn_label
        level, text = {"up": ("info", f"{label} is up (exit IP {exit_ip})"),
                       "offline": ("warn", f"{label} has NO INTERNET (VPN probe: {note})"),
                       "banned": ("warn", f"{label}'s exit IP is BANNED: {note}"),
                       "leak": ("error", f"{label}'s probe is NOT going through the VPN: {note}"),
                       "unknown": ("warn", f"{label}: {note}")}[state]
        self.events.add(level, VPN_KEY, text + (f" (was {v.logged_state})" if v.logged_state else ""))
        v.logged_state = state

    def _roll_vpn_day(self):
        v, today = self.vpn, date.today().isoformat()
        if v.restart_day != today:
            v.restart_day, v.restarts_today = today, 0
        if v.dry["day"] != today:
            v.dry["day"], v.dry["count"] = today, 0

    def _vpn_hold(self, now):
        """(key, why) when the VPN may not be reconnected automatically right now, else None."""
        v = self.vpn
        if not self.settings.get("vpn_iface"):
            return "iface", "no VPN tunnel interface is set on the Settings page"
        if not (self.bouncer and self.bounce_access["ok"]):
            return "access", "gateway access (install-bounce.sh) isn't working"
        if self.busy:
            return "busy", f"{self._wan(self.busy['wan']).label} is being reset first"
        for w in self.wans:
            if w.is_uplink and not w.healthy:
                return "wan", f"the primary WAN {w.label} is {w.state} (fixed first)"
        if v.state == "offline" and self._all_probes_failing:
            return "probes", "every internet probe is failing at once"
        self._roll_vpn_day()
        dry = self.settings.get("dry_run")
        last, count = (v.dry["last"], v.dry["count"]) if dry else (v.last_restart, v.restarts_today)
        cooldown = int(self.settings.get("vpn_cooldown_minutes") or 5) * 60
        if last and now - last < cooldown:
            return "cooldown", f"waiting {int((cooldown - (now - last)) // 60) + 1} min (cooldown)"
        cap = int(self.settings.get("vpn_max_restarts_per_day") or 0)
        if count >= cap:
            return "limit", f"daily limit reached ({count} of {cap})"
        return None

    def _decide_vpn(self, now):
        """The reason to reconnect the VPN now (and marks it as running), else None. Call with the lock held."""
        v = self.vpn
        reason = None
        if v.state == "banned":
            reason = v.note
        elif v.state == "offline" and v.offline_since and \
                now - v.offline_since >= int(self.settings.get("vpn_down_minutes") or 2) * 60:
            reason = f"no internet through it for {int((now - v.offline_since) // 60)} min"
        if not reason or v.restarting or not self.settings.get("enabled"):
            v.action_note = "" if not v.restarting else v.action_note
            v.hold_logged = None
            return None
        hold = self._vpn_hold(now)
        if hold:
            key, why = hold
            v.action_note = f"needs a reconnect ({reason}) but {why}"
            if v.hold_logged != key:
                self.events.add("info", VPN_KEY, f"{self.vpn_label} needs a reconnect ({reason}) but {why}")
                v.hold_logged = key
            return None
        v.restarting, v.hold_logged = True, None
        v.action_note = "reconnecting"
        return reason

    def _vpn_restart(self, reason, now, manual):
        """Reconnect the VPN (already marked as running). Called WITHOUT the lock: it takes up to ~60 s."""
        v, label, tun = self.vpn, self.vpn_label, self.settings.get("vpn_iface")
        who = "Manual" if manual else "Automatic"
        dry = self.settings.get("dry_run") and not manual    # dry run only covers automatic actions
        if dry:
            self.events.add("action", VPN_KEY, f"DRY RUN: would reconnect {label} ({tun}) - {reason}")
            code, out = 0, None
        else:
            code, out = self.bouncer.vpn_restart(tun)
            if code == 255:              # ssh itself failed: re-check once to tell a blip from lost access
                still_ok, detail = self.bouncer.check()
                if not still_ok:
                    with self.lock:
                        self.bounce_access = {"ok": False, "checked": self.clock(), "detail": detail, "lost": True}
        done = self.clock()
        with self.lock:
            v.restarting, v.action_note = False, ""
            self._roll_vpn_day()
            if dry:
                v.dry["last"], v.dry["count"] = now, v.dry["count"] + 1
                return True, "dry run logged"
            v.last_restart = done        # also after a failure: the cooldown applies
            if code != 0:
                what = {2: "was refused by the gateway (is it in VPN_IFACES of install-bounce.sh?)",
                        3: "ran but the tunnel did NOT come back within 60 s",
                        5: "found no running OpenVPN client for it", 255: "couldn't run"}.get(code, f"failed (exit {code})")
                self.events.add("error", VPN_KEY, f"{who} reconnect of {label} ({tun}) {what}: {out}")
                return False, f"Reconnect {what}: {out}"
            v.restarts_today += 1
            v.fails, v.offline_since = 0, None
            v.recheck = done             # the probe checks the ban sites again with the new exit IP
            self.events.add("action", VPN_KEY, f"{who} reconnect of {label}: {out} - {reason}")
            return True, f"Reconnected: {out}"

    def manual_vpn_restart(self):
        with self.lock:
            v = self.vpn
            if v.restarting:
                return False, "a reconnect is already running"
            if not self.settings.get("vpn_iface"):
                return False, "set the VPN tunnel interface on the Settings page first"
            if not (self.bouncer and self.bounce_access["ok"]):
                return False, "gateway access isn't working - see the Port bounce card"
            v.restarting = True
            now = self.clock()
        return self._vpn_restart("requested from the web page", now, manual=True)

    def _sync_vpn_conf(self):
        """Tell the VPN probe which sites to ban-check (probes/vpn.conf.json); written only when it changes."""
        if not self.probe_dir or not os.path.isdir(self.probe_dir):
            return
        urls = [u.strip() for u in str(self.settings.get("vpn_ban_urls") or "").split("\n") if u.strip()][:4]
        want = {"ban_urls": urls if self.settings.get("vpn_check") else [],
                "ban_every": int(self.settings.get("vpn_ban_minutes") or 60) * 60, "recheck": self.vpn.recheck}
        if want == self._vpn_conf:
            return
        path = os.path.join(self.probe_dir, f"{VPN_KEY}.conf.json")
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(want, f)
            os.replace(path + ".tmp", path)
        except OSError as e:
            if str(e) != self._vpn_conf_error:
                self.events.add("error", VPN_KEY, f"Can't write {path} for the VPN probe: {e}")
                self._vpn_conf_error = str(e)
            return
        self._vpn_conf, self._vpn_conf_error = want, None

    # ---------- WAN state ----------
    def _update(self, w, info, port, now, probe=None):
        up = bool(info.get("up")) if "up" in info else bool(port.get("up"))
        ip = self._wan_ip(info)
        old_state, old_ip = (w.state if w.seen else None), w.ip
        self._track_probe(w, probe)
        if (not up or not ip) and self._probe_vouches(w):
            # e.g. the UniFi Network app restarting: its first report has no WAN data. The probe just got out
            # through this WAN's own public IP, so the WAN works and UniFi's data is what's wrong.
            if not w.stale_unifi:
                w.stale_unifi = True
                self.events.add("warn", w.key, f"UniFi reports {w.label} down ({'no link' if not up else 'no IP'}) "
                                               f"but its probe is getting out through {w.ip}: treating UniFi's "
                                               f"data as stale, not starting the down timer")
            up, ip = True, w.ip
        elif w.stale_unifi:
            w.stale_unifi = False
            self.events.add("info", w.key, f"UniFi's data for {w.label} looks normal again")
        w.up, w.ip = up, ip
        w.ifname = info.get("ifname") or w.ifname
        w.link = port.get("up")
        w.poe = {"mode": port.get("poe_mode"), "enabled": port.get("poe_enable"),
                 "power": port.get("poe_power")} if port else None
        w.seen = True
        w.is_uplink = bool(info.get("is_uplink"))       # True = UniFi's primary (active) WAN
        w.wan_port_idx = info.get("port_idx")
        w.port_network = port.get("network_name")
        w.port_ifname = port.get("ifname")
        w.poe_class = info.get("poe_class") or port.get("poe_class")
        macs = info.get("mac_table") or []
        w.modem_vendor = mac_vendor(macs[0].get("mac")) if macs else None
        self._learn_fingerprint(w)
        w.problems = self.verify(w)
        if not w.problems and w.problems_logged:
            self.events.add("info", w.key, f"{w.label}'s port safety checks pass again")
            w.problems_logged = None

        if not self._enabled(w.key):
            w.down_since = w.private_since = None
            w.bad_checks = 0
            return                                      # disabled: shown, but not watched for action or alerts
        if w.state in ("down", "offline"):
            w.down_since = w.down_since or now
        else:
            w.down_since = None
        if w.state == "private":
            w.private_since = w.private_since or now
        else:
            w.private_since = None
        if w.healthy:
            w.bounced_at = None
        w.bad_checks = w.bad_checks + 1 if w.state in ("down", "offline", "private") else 0
        if w.bad_checks == EVENT_CAPTURE_CHECKS and (
                w.last_event_capture is None or now - w.last_event_capture >= EVENT_CAPTURE_EVERY):
            w.last_event_capture = now
            self.capture_async(w, f"(went {w.state})")

        if old_state != w.state:
            level = "info" if w.state == "up" else "warn"
            text = {"up": f"is up ({ip})", "down": "is DOWN",
                    "offline": f"has NO INTERNET though its link and IP look fine (probe: {w.probe_note})",
                    "private": f"has a PRIVATE IP {ip} - re-checking in {self.cfg['private_minutes']} min"}
            self.events.add(level, w.key, f"{w.label} {text[w.state]}" + (
                f" (was {old_state})" if old_state else ""))
        elif w.state == "up" and old_ip and ip != old_ip:
            self.events.add("info", w.key, f"{w.label} public IP changed {old_ip} -> {ip}")

    # ---------- safety checks: right port, still a WAN port, same modem ----------
    def _learn_fingerprint(self, w):
        """Remember the modem on this WAN (PoE class, MAC vendor) the first time it's seen healthy."""
        key = f"{w.key}_fingerprint"
        if w.healthy and not self.settings.get(key) and w.fingerprint:
            self.settings.set(**{key: w.fingerprint})
            fp = ", ".join(f"{k} {v}" for k, v in w.fingerprint.items())
            self.events.add("info", w.key, f"Learned {w.label}'s modem on port {w.port}: {fp}")

    def verify(self, w):
        """Problems that make it unsafe to bounce/power-cycle this WAN's port (empty list = OK)."""
        problems = []
        expected = EXPECTED_NETWORK.get(w.key)
        if not w.port_ifname:
            problems.append(f"gateway port {w.port} not found")
        else:
            if expected and w.port_network != expected:
                problems.append(f"gateway port {w.port} belongs to '{w.port_network or 'no WAN'}', "
                                f"not {expected.upper()} ({w.label}) - ports changed?")
            if w.ifname and w.port_ifname != w.ifname:
                problems.append(f"port {w.port} is {w.port_ifname} but {w.label} runs on {w.ifname}")
        if w.wan_port_idx and int(w.wan_port_idx) != w.port:
            problems.append(f"UniFi says {w.label} is on port {w.wan_port_idx}, the settings say port {w.port}")
        learned = self.settings.get(f"{w.key}_fingerprint") or {}
        for k, name in (("poe_class", "PoE"), ("vendor", "modem MAC vendor")):
            now_v = w.fingerprint.get(k)
            if learned.get(k) and now_v and learned[k] != now_v:
                problems.append(f"{w.label}'s modem looks different ({name} {learned[k]} -> {now_v}) - cables "
                                f"swapped? If the hardware changed on purpose, re-learn it on the Settings page")
        return problems

    def relearn(self, key):
        with self.lock:
            w = self._wan(key)
            self.settings.set(**{f"{key}_fingerprint": None})
            w.problems_logged = None
            self.events.add("info", key, f"{w.label}'s modem fingerprint cleared; it is re-learned the next time "
                                         f"{w.label} is healthy")

    def _release_busy(self, now):
        if not self.busy:
            return
        w = self._wan(self.busy["wan"])
        elapsed = now - self.busy["since"]
        if self.busy.get("pending"):          # the action is still running in another thread
            if elapsed >= PENDING_MAX_SECONDS:
                self.events.add("warn", w.key, f"{w.label}'s {self.busy['what']} never finished; freeing the slot")
                self.busy = None
            return
        if w.healthy and elapsed >= MIN_RECOVERY_SECONDS:
            self.events.add("info", w.key, f"{w.label} recovered {int(elapsed // 60)} min after the {self.busy['what']}")
            self.busy = None
        elif now >= self.busy["until"]:
            if self.busy["what"] == "bounce":
                self.events.add("warn", w.key, f"{w.label} is still {w.state} {int(elapsed // 60)} min after the "
                                               f"port bounce")
            else:
                self.events.add("warn", w.key, f"{w.label} recovery window ended without it coming back healthy")
            self.busy = None

    def _reason(self, w, now):
        if w.down_since and now - w.down_since >= self.cfg["down_minutes"] * 60:
            what = "no internet" if w.state == "offline" else "down"
            return f"{what} for {int((now - w.down_since) // 60)} min"
        if w.private_since and now - w.private_since >= self.cfg["private_minutes"] * 60:
            return f"still a private IP ({w.ip}) after {int((now - w.private_since) // 60)} min"
        return None

    def _can_bounce(self, w):
        return bool(self.bouncer and self.bounce_access["ok"] and w.ifname and w.bounced_at is None)

    def _counters(self, w):
        """(last cycle, cycles today) for the current mode: dry-run cycles never count against real ones."""
        self._roll_day(w)
        if self.settings.get("dry_run"):
            return w.dry["last_cycle"], w.dry["cycles"]
        return w.last_cycle, w.cycles_today

    def _blocked(self, w, now):
        """Why this WAN may not be PoE-cycled right now (cooldown / daily cap), else None."""
        last, count = self._counters(w)
        if last and now - last < self.cfg["cooldown_minutes"] * 60:
            left = int((self.cfg["cooldown_minutes"] * 60 - (now - last)) // 60) + 1
            return f"cooldown ({left} min left)"
        if count >= self.cfg["max_cycles_per_day"]:
            return f"daily limit reached ({count})"
        return None

    def _next_step(self, w, now):
        """(step, reason) for a WAN that needs help now, or (None, note)."""
        reason = self._reason(w, now)
        if not reason:
            return None, ""
        if self._can_bounce(w):
            return "bounce", reason
        if w.bounced_at and now - w.bounced_at < self.cfg["bounce_wait_minutes"] * 60:
            return None, f"port bounced, waiting to see if {w.label} recovers"
        blocked = self._blocked(w, now)
        if blocked:
            return None, f"needs a power cycle: {reason}; {blocked}"
        return "cycle", reason + (" (port bounce didn't help)" if w.bounced_at else "")

    def _reserve(self, w, what, now):
        """Take the one-WAN-at-a-time slot before slow I/O starts (call with the lock held)."""
        self.busy = {"wan": w.key, "since": now, "what": what, "until": now + PENDING_MAX_SECONDS, "pending": True}
        self._waiting_logged = None

    def _decide(self, now):
        """Pick the one action to run now and reserve the slot for it. Returns (wan, step, reason) or None."""
        due = []
        for w in self.wans:  # self.wans is already in priority order (AT&T first)
            if not self._enabled(w.key):
                w.note = "not managed (disabled on the Settings page)"
                continue
            step, why = self._next_step(w, now)
            w.note = f"needs a {'port bounce' if step == 'bounce' else 'power cycle'}: {why}" if step else why
            if step and w.problems:
                w.note = f"needs a {step} but WON'T touch the port: " + "; ".join(w.problems)
                if w.problems_logged != w.problems:
                    self.events.add("error", w.key, f"Not touching {w.label}'s port: " + "; ".join(w.problems))
                    w.problems_logged = list(w.problems)
                continue
            if step:
                due.append((w, step, why))
        if not due:
            self._waiting_logged = None
            return None
        if self.busy:
            busy = self._wan(self.busy["wan"])
            key = tuple(w.key for w, _, _ in due)
            if self._waiting_logged != key:
                names = ", ".join(w.label for w, _, _ in due)
                self.events.add("info", None, f"{names} waiting: only one WAN at a time and {busy.label} is still recovering")
                self._waiting_logged = key
            return None
        w, step, why = due[0]
        self._reserve(w, "bounce" if step == "bounce" else "power cycle", now)
        if step == "bounce":
            w.bounced_at = now                    # don't pick the bounce again while it runs
        return w, step, why

    # ---------- actions ----------
    def _gateway_log(self, w):
        """[(page, html)] with the Dream Machine's recent syslog lines about this WAN's port, if bounce access works."""
        if not (self.bouncer and self.bounce_access.get("ok") and w.ifname):
            return []
        code, out = self.bouncer.wanlog(w.ifname)
        if code != 0:
            return [("gateway_wan_log", text_html("gateway WAN log", f"not available: {out}"))]
        return [("gateway_wan_log", gateway_log_html(f"Dream Machine log for {w.ifname} ({w.label})", out))]

    def save_pages(self, w, why, quiet=False):
        """Save the modem's pages (if configured for this WAN) plus the gateway's WAN log. Never raises.
        quiet: only log problems (used for the scheduled captures)."""
        if not (self.capture and self.capture.has(w.key)):
            return None
        try:
            name, saved, errors = self.capture(w.key, why, extra=self._gateway_log(w))
        except Exception as e:
            self.events.add("error", w.key, f"Saving {w.label} modem pages failed: {e}")
            return None
        msg = f"Saved {saved} {w.label} modem page(s) {why}" + (f" as {name}" if name else "")
        if errors:
            msg += " - not saved: " + "; ".join(errors)
        if errors or not quiet:
            self.events.add("warn" if errors else "info", w.key, msg)
        return name

    def capture_async(self, w, why):
        """Save the modem's state in the background so the checks keep running (one at a time per WAN)."""
        if not (self.capture and self.capture.has(w.key)) or (w.capturing and w.capturing.is_alive()):
            return
        if not self.async_captures:          # tests
            self.save_pages(w, why)
            return
        w.capturing = threading.Thread(target=self.save_pages, args=(w, why), daemon=True, name=f"capture-{w.key}")
        w.capturing.start()

    def _bounce(self, w, reason, now, manual=False):
        """Run a bounce whose slot is already reserved. Called WITHOUT the lock (the SSH call can take ~46 s).

        A manual bounce only bounces: it never counts as the automatic bounce step (so it can't lead to
        a PoE cycle) and it restarts the automatic timers, giving the WAN a fresh chance."""
        who = "Manual" if manual else "Automatic"
        dry = self.settings.get("dry_run") and not manual    # dry run only covers automatic actions
        with self.lock:
            if manual:
                w.down_since = w.private_since = None
        if dry:
            self.events.add("action", w.key, f"DRY RUN: would bounce {w.label}'s gateway port ({w.ifname}) - {reason}")
            code, out = 0, None
        else:
            code, out = self.bouncer.bounce(w.ifname)
            if code == 255:              # ssh itself failed: re-check once to tell a blip from lost access
                still_ok, detail = self.bouncer.check()
                if not still_ok:
                    with self.lock:
                        self.bounce_access = {"ok": False, "checked": self.clock(), "detail": detail, "lost": True}
        done = self.clock()
        with self.lock:
            if code != 0:
                what = {2: "was refused by the gateway", 3: "ran but the link did NOT come back up",
                        4: "ran but the link never dropped", 255: "couldn't run"}.get(code, f"failed (exit {code})")
                self.events.add("error", w.key, f"{who} port bounce of {w.label} ({w.ifname}) {what}: {out}"
                                                + ("" if manual else "; going to the PoE power cycle instead"))
                if not manual:
                    w.bounced_at = now - self.cfg["bounce_wait_minutes"] * 60   # skip the wait
                self.busy = None
                return False, f"Bounce {what}: {out}"
            if not dry:
                self.events.add("action", w.key, f"{who} port bounce of {w.label} - verified on the gateway: {out} - {reason}")
            w.last_bounce = now
            self.busy = {"wan": w.key, "since": done, "what": "bounce",
                         "until": done + self.cfg["bounce_wait_minutes"] * 60}
            return True, "dry run logged" if dry else f"Bounce verified: {out}"

    def _cycle(self, w, reason, now, manual):
        """Run a power cycle whose slot is already reserved. Called WITHOUT the lock (pages are saved first)."""
        dry = self.settings.get("dry_run") and not manual    # dry run only covers automatic actions
        who = "Manual" if manual else "Automatic"
        self.save_pages(w, "before the power cycle")
        error = via = None
        if dry:
            self.events.add("action", w.key, f"DRY RUN: would power-cycle {w.label} (gateway port {w.port}) - {reason}")
        else:
            try:
                via = self.client.power_cycle(self.gw_mac, w.port)
            except UniFiError as e:
                error = str(e)
        done = self.clock()
        with self.lock:
            self._roll_day(w)
            if dry:
                w.dry["last_cycle"], w.dry["cycles"] = now, w.dry["cycles"] + 1
            else:
                w.last_cycle = now  # also after a failure: don't retry every check; cooldown applies
            if error:
                self.events.add("error", w.key, f"{who} power cycle of {w.label} port {w.port} FAILED: {error}")
                self.busy = None
                return False, error
            if not dry:
                w.cycles_today += 1
                self.events.add("action", w.key, f"{who} power cycle of {w.label} (gateway port {w.port}) via {via} - {reason}")
            w.down_since = w.private_since = None   # timers restart from the next check
            w.bounced_at = None
            self.busy = {"wan": w.key, "since": done, "what": "power cycle",
                         "until": done + self.cfg["recovery_minutes"] * 60}
            return True, "dry run logged" if dry else "power cycle sent"

    def apply_config(self, cfg):
        """Settings page saved: new limits apply from the next check; WAN names/ports/order update in place."""
        with self.lock:
            self.cfg.update({k: v for k, v in cfg.items() if k != "wans"})
            by_key = {w.key: w for w in self.wans}
            wans = []
            for i, (key, label, port) in enumerate(cfg["wans"]):
                w = by_key.get(key) or Wan(key, label, port, i)
                w.label, w.port, w.priority = label, int(port), i
                wans.append(w)
            self.wans = wans
            self.cfg["wans"] = cfg["wans"]

    # ---------- helpers / UI ----------
    def _wan(self, key):
        return next(w for w in self.wans if w.key == key)

    def _enabled(self, key):
        """A WAN the watchdog manages. Only an explicit False disables it (missing/True = enabled)."""
        return self.settings.get(f"{key}_enabled") is not False

    @staticmethod
    def _roll_day(w):
        today = date.today().isoformat()
        if w.cycle_day != today:
            w.cycle_day, w.cycles_today = today, 0
        if w.dry["day"] != today:
            w.dry["day"], w.dry["cycles"] = today, 0

    def _manual_guard(self, key):
        try:
            w = self._wan(key)
        except StopIteration:
            return None, "unknown WAN"
        if self.busy:
            other = self._wan(self.busy["wan"])
            return None, f"{other.label} is still recovering from a {self.busy['what']}; only one WAN at a time"
        if not w.seen:
            return None, f"{w.label} hasn't been read from the gateway yet - try again in a moment"
        if w.problems:
            return None, "Refused, safety check failed: " + "; ".join(w.problems)
        return w, None

    def reboot_box(self, wan_key, reason, manual=False):
        """Reboot a WAN's modem through its own Device -> Restart page. Reserves the one-at-a-time slot first so
        the watchdog doesn't also bounce/power-cycle while it reboots, then holds a recovery window until the WAN
        comes back. Dry run only logs. Returns (ok, detail)."""
        with self.lock:
            try:
                w = self._wan(wan_key)
            except StopIteration:
                return False, "unknown WAN"
            if self.busy:
                return False, f"{self._wan(self.busy['wan']).label} is being reset first"
            now = self.clock()
            self._reserve(w, "modem reboot", now)
        if self.settings.get("dry_run") and not manual:
            self.events.add("action", wan_key, f"DRY RUN: would reboot {w.label}'s modem (Device -> Restart) - {reason}")
            with self.lock:
                self.busy = None
            return True, "dry run"
        if not (self.capture and self.capture.can_reboot(wan_key)):
            self.events.add("error", wan_key, f"Can't reboot {w.label}'s modem: no address or access code configured")
            with self.lock:
                self.busy = None
            return False, "not configured"
        error = None
        try:
            self.capture.reboot(wan_key)
        except (PermissionError, RuntimeError) as e:
            error = str(e)
        except Exception as e:
            error = f"{e.__class__.__name__}: {e}"
        done = self.clock()
        with self.lock:
            if error:
                self.events.add("error", wan_key, f"Couldn't reboot {w.label}'s modem: {error} - {reason}")
                self.busy = None
                return False, error
            self.events.add("action", wan_key, f"Rebooted {w.label}'s modem (Device -> Restart) - {reason}")
            w.down_since = w.private_since = None     # timers restart from the next check
            self.busy = {"wan": w.key, "since": done, "what": "modem reboot",
                         "until": done + self.cfg["recovery_minutes"] * 60}
        return True, "reboot sent"

    def box_reboot_tick(self, now_dt):
        """Called about once a minute. Once a day, in the configured hour, reboot the chosen WAN's modem when its
        last-hour average probe latency is at least the threshold (0 = always). Returns True if a reboot was sent."""
        if not self.settings.get("box_reboot") or now_dt.hour != int(self.settings.get("box_reboot_hour") or 4):
            return False
        key = now_dt.strftime("%Y%m%d")
        if self.settings.get("box_reboot_last") == key:
            return False
        wan = self.settings.get("box_reboot_wan") or "wan2"
        try:
            label = self._wan(wan).label
        except StopIteration:
            self.settings.set(box_reboot_last=key)
            return False
        thresh = int(self.settings.get("box_reboot_latency_ms") or 0)
        avg = None
        if thresh > 0:
            if self.probe_history is None:
                self.settings.set(box_reboot_last=key)
                return False
            _n, avg, _loss = self.probe_history.recent(wan, self.clock() - 3600)
            if avg is None:
                self.settings.set(box_reboot_last=key)
                self.events.add("info", wan, f"{label} modem reboot skipped: no probe data to judge latency")
                return False
            if avg < thresh:
                self.settings.set(box_reboot_last=key)
                self.events.add("info", wan, f"{label} modem reboot skipped: latency OK ({avg:.0f} ms, under {thresh} ms)")
                return False
        self.settings.set(box_reboot_last=key)     # mark the day first: a failed reboot won't retry in a loop
        reason = f"scheduled {now_dt.hour:02d}:00" + (f", last hour averaged {avg:.0f} ms" if avg is not None else "")
        ok, _ = self.reboot_box(wan, reason)
        return ok

    def manual_cycle(self, key):
        with self.lock:
            w, err = self._manual_guard(key)
            if err:
                return False, err
            if not self.gw_mac:
                return False, "gateway not read yet - try again in a moment"
            now = self.clock()
            self._reserve(w, "power cycle", now)
        return self._cycle(w, "requested from the web page", now, manual=True)

    def manual_bounce(self, key):
        with self.lock:
            w, err = self._manual_guard(key)
            if err:
                return False, err
            if not (self.bouncer and self.bounce_access["ok"]):
                return False, "port-bounce access isn't working - see the status card"
            if not w.ifname:
                return False, f"{w.label}'s gateway interface isn't known yet"
            now = self.clock()
            self._reserve(w, "bounce", now)
        return self._bounce(w, "requested from the web page", now, manual=True)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            busy = None
            if self.busy:
                busy = dict(self.busy, label=self._wan(self.busy["wan"]).label,
                            left=max(0, int(self.busy["until"] - now)))
            # pages list the WANs by number (WAN 1, WAN 2); self.wans stays in reset-priority order
            return {"wans": [dict(w.as_dict(now), enabled=self._enabled(w.key))
                             for w in sorted(self.wans, key=lambda w: w.key)], "busy": busy,
                    "first_label": self.wans[0].label if self.wans else "",
                    "last_check": self.last_check, "last_error": self.last_error, "now": now,
                    "settings": dict(self.settings.data), "cfg": self.cfg, "gw_version": self.gw_version,
                    "bounce": dict(self.bounce_access, configured=bool(self.bouncer and self.bouncer.configured())),
                    "vpn": dict(self.vpn.as_dict(now), label=self.vpn_label, iface=self.settings.get("vpn_iface"),
                                watched=bool(self.probe_dir and self.settings.get("vpn_check"))),
                    "capture_keys": [w.key for w in self.wans if self.capture and self.capture.has(w.key)],
                    "box_reboot": self._box_reboot_status(now)}

    def _box_reboot_status(self, now):
        """What the scheduled modem reboot is watching, for the dashboard (call with the lock held)."""
        if not self.settings.get("box_reboot"):
            return {"on": False}
        wan = self.settings.get("box_reboot_wan") or "wan2"
        try:
            label = self._wan(wan).label
        except StopIteration:
            label = wan
        recent = None
        if self.probe_history is not None:
            _n, recent, _loss = self.probe_history.recent(wan, now - 3600)
        return {"on": True, "wan": wan, "label": label, "hour": int(self.settings.get("box_reboot_hour") or 4),
                "threshold": int(self.settings.get("box_reboot_latency_ms") or 0), "recent_ms": recent,
                "last": self.settings.get("box_reboot_last"),
                "can": bool(self.capture and self.capture.can_reboot(wan))}
