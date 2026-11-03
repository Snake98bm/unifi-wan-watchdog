"""WAN watchdog logic: watch both WANs and reset a WAN that is stuck.

Rules
- A WAN is "down" when UniFi says it isn't up or it has no IP. Down for DOWN_MINUTES -> reset.
- A WAN with a private IP (modem lost passthrough) is re-checked; still private after
  PRIVATE_MINUTES -> reset.
- An automatic reset first BOUNCES the gateway port (link renegotiation over the restricted SSH key, modem stays
  powered) when bounce access works. If the WAN is still bad BOUNCE_WAIT_MINUTES later, the port's PoE
  is power-cycled. Without working bounce access it goes straight to the PoE power cycle.
- Only ONE WAN is ever being reset: after a bounce or cycle, nothing else is touched until that WAN
  is healthy again (at least 2 minutes later) or its wait window has passed.
- If both need a reset, the WAN listed first in WANS (AT&T) goes first.
- Per WAN: COOLDOWN_MINUTES between PoE cycles and at most MAX_CYCLES_PER_DAY.
- Bounce access is checked at startup and whenever the gateway firmware changes, so a firmware update
  that drops the key is noticed and logged. A failed bounce also marks the access as not working.
- Dry run logs what it WOULD do on its own without touching the port. The Bounce / Power-cycle
  buttons on the web page always really act (they still follow the one-WAN-at-a-time rule).
- The Bounce button only bounces: the PoE power cycle is the automation's backup step, never a
  follow-up to a manual bounce.
"""
import threading
import time
from datetime import date

from atparse import gateway_log_html, text_html
from unifi import UniFiError, is_private
from version import VERSION

MIN_RECOVERY_SECONDS = 120
EVENT_CAPTURE_CHECKS = 2          # bad for this many checks in a row (~30 s) -> save the modem's state now
EVENT_CAPTURE_EVERY = 3600        # ... but at most once an hour per WAN (AT&T's latency flapping)
EXPECTED_NETWORK = {"wan1": "wan", "wan2": "wan2"}   # UniFi's port_table network_name for each WAN


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

    @property
    def fingerprint(self):
        return {k: v for k, v in (("poe_class", self.poe_class), ("vendor", self.modem_vendor)) if v}

    @property
    def private(self):
        return bool(self.ip) and is_private(self.ip)

    @property
    def healthy(self):
        return bool(self.up) and bool(self.ip) and not self.private

    @property
    def state(self):
        if not self.seen:
            return "unknown"
        if not self.up or not self.ip:
            return "down"
        return "private" if self.private else "up"

    def as_dict(self, now):
        return {
            "key": self.key, "label": self.label, "port": self.port, "state": self.state,
            "ip": self.ip, "ifname": self.ifname, "link": self.link, "poe": self.poe,
            "down_for": int(now - self.down_since) if self.down_since else None,
            "private_for": int(now - self.private_since) if self.private_since else None,
            "last_cycle": self.last_cycle, "last_bounce": self.last_bounce,
            "cycles_today": self.cycles_today, "note": self.note,
            "problems": list(self.problems), "fingerprint": self.fingerprint,
            "port_network": self.port_network, "port_ifname": self.port_ifname, "is_uplink": self.is_uplink,
        }


class Monitor(threading.Thread):
    def __init__(self, cfg, client, events, settings, clock=time.time, capture=None, bouncer=None):
        super().__init__(daemon=True, name="wan-monitor")
        self.cfg, self.client, self.events, self.settings, self.clock = cfg, client, events, settings, clock
        self.capture = capture    # optional: saves a modem's pages before its port is cycled
        self.bouncer = bouncer    # optional: link bounce over the restricted SSH key
        self.wans = [Wan(k, label, port, i) for i, (k, label, port) in enumerate(cfg["wans"])]
        self.lock = threading.RLock()
        self.busy = None          # {"wan", "since", "until", "what"} while a reset recovers
        self.gw_mac = None
        self.gw_version = None
        self.last_check = None
        self.last_error = None
        self.bounce_access = {"ok": None, "checked": None, "detail": "not checked yet"}
        self.async_captures = True
        self._waiting_logged = None
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
        with self.lock:
            if msg != self.last_error:
                self.events.add("error", None, msg)
            self.last_error = msg

    # ---------- one check ----------
    def check(self):
        try:
            gw = self.client.gateway()
        except UniFiError as e:
            self._error(f"Can't read the gateway: {e} (no action taken)")
            return
        now = self.clock()
        with self.lock:
            if self.last_error:
                self.events.add("info", None, "Gateway API reachable again")
            self.last_error = None
            self.last_check = now
            self.gw_mac = gw.get("mac")
            self._watch_firmware(gw.get("version"), now)
            ports = {p.get("port_idx"): p for p in gw.get("port_table", [])}
            for w in self.wans:
                self._update(w, gw.get(w.key) or {}, ports.get(w.port) or {}, now)
            self._release_busy(now)
            if self.settings.get("enabled"):
                self._decide(now)

    # ---------- bounce access / firmware survival ----------
    def _watch_firmware(self, version, now):
        known = self.settings.get("gw_firmware")
        changed = bool(version and known and version != known)
        if version and version != known:
            self.settings.set(gw_firmware=version)
        self.gw_version = version
        startup = self.bounce_access["checked"] is None
        if self.bouncer and self.bouncer.configured() and (startup or changed):
            self.check_bounce_access(now, firmware_change=(known, version) if changed else None)
        elif changed:
            self.events.add("info", None, f"Gateway firmware changed {known} -> {version}")

    def check_bounce_access(self, now=None, firmware_change=None):
        now = now or self.clock()
        was_ok = self.bounce_access["ok"]
        ok, detail = self.bouncer.check()
        self.bounce_access = {"ok": ok, "checked": now, "detail": detail}
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

    # ---------- WAN state ----------
    def _update(self, w, info, port, now):
        up = bool(info.get("up")) if "up" in info else bool(port.get("up"))
        ip = info.get("ip") or None
        if ip in ("0.0.0.0", ""):
            ip = None
        old_state, old_ip = (w.state if w.seen else None), w.ip
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

        if w.state == "down":
            w.down_since = w.down_since or now
        else:
            w.down_since = None
        if w.state == "private":
            w.private_since = w.private_since or now
        else:
            w.private_since = None
        if w.healthy:
            w.bounced_at = None
        w.bad_checks = w.bad_checks + 1 if w.state in ("down", "private") else 0
        if w.bad_checks == EVENT_CAPTURE_CHECKS and (
                w.last_event_capture is None or now - w.last_event_capture >= EVENT_CAPTURE_EVERY):
            w.last_event_capture = now
            self.capture_async(w, f"(went {w.state})")

        if old_state != w.state:
            level = "info" if w.state == "up" else "warn"
            text = {"up": f"is up ({ip})", "down": "is DOWN",
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
            return f"down for {int((now - w.down_since) // 60)} min"
        if w.private_since and now - w.private_since >= self.cfg["private_minutes"] * 60:
            return f"still a private IP ({w.ip}) after {int((now - w.private_since) // 60)} min"
        return None

    def _can_bounce(self, w):
        return bool(self.bouncer and self.bounce_access["ok"] and w.ifname and w.bounced_at is None)

    def _blocked(self, w, now):
        """Why this WAN may not be PoE-cycled right now (cooldown / daily cap), else None."""
        self._roll_day(w)
        if w.last_cycle and now - w.last_cycle < self.cfg["cooldown_minutes"] * 60:
            left = int((self.cfg["cooldown_minutes"] * 60 - (now - w.last_cycle)) // 60) + 1
            return f"cooldown ({left} min left)"
        if w.cycles_today >= self.cfg["max_cycles_per_day"]:
            return f"daily limit reached ({w.cycles_today})"
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

    def _decide(self, now):
        due = []
        for w in self.wans:  # self.wans is already in priority order (AT&T first)
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
            return
        if self.busy:
            busy = self._wan(self.busy["wan"])
            key = tuple(w.key for w, _, _ in due)
            if self._waiting_logged != key:
                names = ", ".join(w.label for w, _, _ in due)
                self.events.add("info", None, f"{names} waiting: only one WAN at a time and {busy.label} is still recovering")
                self._waiting_logged = key
            return
        w, step, why = due[0]
        if step == "bounce":
            self._bounce(w, why, now)
        else:
            self._cycle(w, why, now, manual=False)

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
        """A manual bounce only bounces: it never counts as the automatic bounce step (so it can't lead to
        a PoE cycle) and it restarts the automatic timers, giving the WAN a fresh chance."""
        who = "Manual" if manual else "Automatic"
        dry = self.settings.get("dry_run") and not manual    # dry run only covers automatic actions
        if manual:
            w.down_since = w.private_since = None
        else:
            w.bounced_at = now
        if dry:
            self.events.add("action", w.key, f"DRY RUN: would bounce {w.label}'s gateway port ({w.ifname}) - {reason}")
        else:
            code, out = self.bouncer.bounce(w.ifname)
            if code != 0:
                if code == 255:          # ssh itself failed: key refused or gateway unreachable
                    self.bounce_access = {"ok": False, "checked": now, "detail": out}
                    what = "couldn't run"
                else:
                    what = {2: "was refused by the gateway", 3: "ran but the link did NOT come back up",
                            4: "ran but the link never dropped"}.get(code, f"failed (exit {code})")
                self.events.add("error", w.key, f"{who} port bounce of {w.label} ({w.ifname}) {what}: {out}"
                                                + ("" if manual else "; going to the PoE power cycle instead"))
                if not manual:
                    w.bounced_at = now - self.cfg["bounce_wait_minutes"] * 60   # skip the wait
                return False, f"Bounce {what}: {out}"
            self.events.add("action", w.key, f"{who} port bounce of {w.label} - verified on the gateway: {out} - {reason}")
        w.last_bounce = now
        self.busy = {"wan": w.key, "since": now, "what": "bounce",
                     "until": now + self.cfg["bounce_wait_minutes"] * 60}
        self._waiting_logged = None
        return True, "dry run logged" if dry else f"Bounce verified: {out}"

    def _cycle(self, w, reason, now, manual):
        dry = self.settings.get("dry_run") and not manual    # dry run only covers automatic actions
        who = "Manual" if manual else "Automatic"
        self.save_pages(w, "before the power cycle")
        if dry:
            self.events.add("action", w.key, f"DRY RUN: would power-cycle {w.label} (gateway port {w.port}) - {reason}")
        else:
            try:
                via = self.client.power_cycle(self.gw_mac, w.port)
            except UniFiError as e:
                self.events.add("error", w.key, f"{who} power cycle of {w.label} port {w.port} FAILED: {e}")
                w.last_cycle = now  # don't retry every check; cooldown applies
                return False, str(e)
            self.events.add("action", w.key, f"{who} power cycle of {w.label} (gateway port {w.port}) via {via} - {reason}")
        self._roll_day(w)
        w.cycles_today += 1
        w.last_cycle = now
        w.down_since = w.private_since = None   # timers restart from the next check
        w.bounced_at = None
        self.busy = {"wan": w.key, "since": now, "what": "power cycle",
                     "until": now + self.cfg["recovery_minutes"] * 60}
        self._waiting_logged = None
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

    @staticmethod
    def _roll_day(w):
        today = date.today().isoformat()
        if w.cycle_day != today:
            w.cycle_day, w.cycles_today = today, 0

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

    def manual_cycle(self, key):
        with self.lock:
            w, err = self._manual_guard(key)
            if err:
                return False, err
            if not self.gw_mac:
                return False, "gateway not read yet - try again in a moment"
            return self._cycle(w, "requested from the web page", self.clock(), manual=True)

    def manual_bounce(self, key):
        with self.lock:
            w, err = self._manual_guard(key)
            if err:
                return False, err
            if not (self.bouncer and self.bounce_access["ok"]):
                return False, "port-bounce access isn't working - see the status card"
            if not w.ifname:
                return False, f"{w.label}'s gateway interface isn't known yet"
            return self._bounce(w, "requested from the web page", self.clock(), manual=True)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            busy = None
            if self.busy:
                busy = dict(self.busy, label=self._wan(self.busy["wan"]).label,
                            left=max(0, int(self.busy["until"] - now)))
            return {"wans": [w.as_dict(now) for w in self.wans], "busy": busy,
                    "last_check": self.last_check, "last_error": self.last_error, "now": now,
                    "settings": dict(self.settings.data), "cfg": self.cfg, "gw_version": self.gw_version,
                    "bounce": dict(self.bounce_access, configured=bool(self.bouncer and self.bouncer.configured())),
                    "capture_keys": [w.key for w in self.wans if self.capture and self.capture.has(w.key)]}
