"""WAN watchdog: web UI + background monitor.

Secrets and the gateway address come from environment variables; everything else is edited on the
Settings page and saved in /data/settings.json (see config.py).
"""
import hmac
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from waitress import serve

import config
import pages as modem_pages
from atparse import fix_old_qcainfo
from bounce import Bouncer
from capture import Capturer, schedule_slot
from modem import SSH_SENSORS, ModemSensors, ModemStore, ModemWatch
from monitor import Monitor
from speedtest import SpeedStore, SpeedTester, by_setup, link_text
from store import Events, Probes, Settings
from unifi import UniFi, UniFiError
from version import VERSION

DATA = os.environ.get("DATA_DIR", "/data")
UNIFI_URL = os.environ.get("UNIFI_URL", "https://192.168.1.1")

missing = [n for n in ("UNIFI_API_KEY", "ADMIN_PASSWORD") if not os.environ.get(n)]
if missing:
    sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")


def secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


os.makedirs(DATA, exist_ok=True)
events = Events(os.path.join(DATA, "events.db"))
settings = Settings(os.path.join(DATA, "settings.json"), config.initial_values())
if settings.data.get("settings_version", 1) < 2:   # v2 added Verizon's (Quectel) AT-command capture
    if not str(settings.get("capture_wan1") or "").strip():
        settings.data["capture_wan1"] = config.FIELDS["capture_wan1"][3]
    settings.data["settings_version"] = 2
if settings.data.get("settings_version", 1) < 3:   # v3 added the toolkit's uptime and watchcat pages
    cur = str(settings.data.get("capture_wan1") or "")
    if ("{QUECTEL_GATEWAY}" in cur + str(settings.data.get("at_url_wan1") or "") and "get_uptime" not in cur
            and "/quecmanager/" not in cur):
        settings.data["capture_wan1"] = "\n".join([cur.rstrip("\n")] + config.TOOLKIT_PAGES)
    settings.data["settings_version"] = 3
if settings.data.get("settings_version", 1) < 4:   # v4 added the cause-code AT commands (CEER, CEREG, ...)
    cur = str(settings.data.get("capture_wan1") or "")
    if "{QUECTEL_GATEWAY}" in cur + str(settings.data.get("at_url_wan1") or "") and "AT+CEER" not in cur:
        lines = cur.rstrip("\n").split("\n")
        at_end = max((i for i, ln in enumerate(lines) if ln.lower().startswith("at:")), default=len(lines) - 1)
        lines[at_end + 1:at_end + 1] = ["at:" + c for c in config.CAUSE_AT]   # after the other AT lines
        settings.data["capture_wan1"] = "\n".join(lines)
    settings.data["settings_version"] = 4
if settings.data.get("settings_version", 1) < 5:   # v5: the RM520N-GL's web UI is QManager, not the toolkit
    cur = str(settings.data.get("capture_wan1") or "")
    at = str(settings.data.get("at_url_wan1") or "")
    if "/cgi-bin/get_atcommand" in at or any(p in cur for p in config.TOOLKIT_PAGES):
        if "/cgi-bin/get_atcommand" in at:
            settings.data["at_url_wan1"] = config.QUECTEL_AT_URL
        lines = [ln for ln in cur.split("\n") if ln.strip() and ln.strip() not in config.TOOLKIT_PAGES]
        settings.data["capture_wan1"] = "\n".join(lines + [p for p in config.QUECTEL_PAGES if p not in lines])
    settings.data["settings_version"] = 5
if settings.data.get("settings_version", 1) < 6:   # v6: AT+CBC (supply voltage) with the QManager AT commands
    cur = str(settings.data.get("capture_wan1") or "")
    if "/quecmanager/" in cur and "AT+CBC" not in cur:
        lines = cur.split("\n")
        at = [i for i, ln in enumerate(lines) if ln.strip().upper().startswith("AT:AT+QTEMP")]
        lines.insert(at[0] + 1 if at else 0, "at:AT+CBC")
        settings.data["capture_wan1"] = "\n".join(lines)
    settings.data["settings_version"] = 6
settings.set()                                # write any newly added settings to the file
cfg = config.monitor_cfg(settings.data)
client = UniFi(UNIFI_URL, os.environ["UNIFI_API_KEY"], os.environ.get("UNIFI_SITE", "default"),
               os.environ.get("UNIFI_VERIFY_SSL", "false").lower() in ("1", "true", "yes"))


def is_att(key, label):
    """The AT&T WAN: its name contains AT&T/ATT, or its modem pages use {ATT_GATEWAY}."""
    return "ATT" in label.upper().replace("&", "") or "{ATT_GATEWAY}" in str(settings.get(f"capture_{key}") or "")


def capture_codes():
    """<KEY>_ACCESS_CODE, or ATT_ACCESS_CODE for the AT&T WAN: logs in to pages behind the access-code form."""
    return {k: os.environ.get(f"{k.upper()}_ACCESS_CODE") or
            (os.environ.get("ATT_ACCESS_CODE") if is_att(k, label) else None)
            for k, label, _ in cfg["wans"]}


def capture_auth():
    """Login for modem pages: QUECTEL_PASSWORD (QManager needs only the password; QUECTEL_USER is for older web
    UIs behind HTTP Basic) for the WAN whose pages use {QUECTEL_GATEWAY}, or <KEY>_USER / <KEY>_PASSWORD."""
    out = {}
    for k in config.WAN_KEYS:
        user, pw = os.environ.get(f"{k.upper()}_USER"), os.environ.get(f"{k.upper()}_PASSWORD")
        uses = str(settings.get(f"capture_{k}") or "") + str(settings.get(f"at_url_{k}") or "")
        if not (user or pw) and "{QUECTEL_GATEWAY}" in uses:
            user, pw = os.environ.get("QUECTEL_USER"), os.environ.get("QUECTEL_PASSWORD")
        if user or pw:
            out[k] = (user or "", pw or "")
    return out


def reboot_hosts():
    """{wan: modem host} from each WAN's capture pages ({NAME_GATEWAY} placeholder), for the Device -> Restart reboot."""
    out = {}
    for k in config.WAN_KEYS:
        m = config.PLACEHOLDER_RE.search(str(settings.get(f"capture_{k}") or ""))
        host = config.gateway_value(m.group(1)) if m else None
        if host:
            out[k] = host
    return out


capturer = Capturer(os.path.join(DATA, "captures"), config.capture_urls(settings.data), capture_codes(),
                    keep_days=cfg["capture_keep_days"], auth_by_wan=capture_auth(), reboot_hosts=reboot_hosts())
# Port bounce: the watchdog's own SSH key, locked on the gateway to wan-bounce.sh (gateway/install-bounce.sh)
bouncer = Bouncer(os.environ.get("BOUNCE_SSH_HOST", UNIFI_URL.split("//")[-1].split(":")[0].split("/")[0]),
                  os.environ.get("BOUNCE_SSH_KEY", os.path.join(DATA, "ssh", "id_ed25519")),
                  os.path.join(DATA, "ssh", "known_hosts"))
# Internet probes: one container per WAN on a VLAN routed out of that WAN only writes probes/<wan>.json here
PROBE_DIR = os.environ.get("PROBE_DIR", os.path.join(DATA, "probes"))
probe_history = Probes(os.path.join(DATA, "probes.db"))
# Modem health (signal per antenna, temperatures, voltage, PoE draw): modem.db, the QManager sampler, the SSH sensors
modem_store = ModemStore(os.path.join(DATA, "modem.db"), keep_days=cfg["capture_keep_days"])
modem_sensors = ModemSensors(os.environ.get("MODEM_SSH_HOST") or config.gateway_value("QUECTEL_GATEWAY"),
                             os.environ.get("BOUNCE_SSH_KEY", os.path.join(DATA, "ssh", "id_ed25519")),
                             os.path.join(DATA, "ssh", "known_hosts_modem"))
modem = ModemWatch(modem_store, events, settings, capture=capturer, sensors=modem_sensors)
monitor = Monitor(cfg, client, events, settings, capture=capturer, bouncer=bouncer, probe_dir=PROBE_DIR,
                  probe_history=probe_history, modem=modem)
# Speed tests: run by the probe containers on request, saved with the line's channel widths / sync rates
speed_store = SpeedStore(os.path.join(DATA, "speed.db"))
speed = SpeedTester(speed_store, events, settings, monitor, modem=modem, probe_dir=PROBE_DIR)


def apply_settings():
    new_cfg = config.monitor_cfg(settings.data)
    monitor.apply_config(new_cfg)
    capturer.urls_by_wan = config.capture_urls(settings.data)
    capturer.codes_by_wan = capture_codes()
    capturer.auth_by_wan = capture_auth()
    capturer.reboot_hosts = reboot_hosts()
    capturer.keep_days = new_cfg["capture_keep_days"]
    modem_store.keep_days = new_cfg["capture_keep_days"]

app = Flask(__name__)
app.config.update(SECRET_KEY=secret_key(), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
failed_logins = {}  # ip -> [timestamps]


@app.template_filter("when")
def fmt_when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


@app.template_filter("dur")
def fmt_dur(seconds):
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["version"] = VERSION


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint != "login":
        sent, expected = request.form.get("csrf", ""), session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "bad CSRF token - reload the page")


def safe_next(nxt):
    """Only a path on this site: browsers treat '//host' and '/\\host' as another site, so those go to '/'."""
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(c) < 32 for c in nxt):
        return "/"
    return nxt


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    if request.method == "POST":
        now = time.time()
        recent = [t for t in failed_logins.get(ip, []) if now - t < 900]
        failed_logins[ip] = recent
        if len(recent) >= 5:
            events.add("warn", None, f"Login blocked for {ip}: too many failed attempts")
            return render_template("login.html", error="Too many failed attempts - wait 15 minutes."), 429
        if hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session["auth"] = True
            session.permanent = True
            failed_logins.pop(ip, None)
            events.add("info", None, f"Login from {ip}")
            return redirect(safe_next(request.args.get("next", "/")))
        recent.append(now)
        events.add("warn", None, f"Failed login from {ip}")
        return render_template("login.html", error="Wrong password."), 401
    return render_template("login.html", error=None)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


def spark(points, limit=None, height=30):
    """[[ts, value], ...] -> SVG polyline points (viewBox 0 0 100 height) and the y of a limit line."""
    pts = [(p[0], p[1]) for p in points if p[1] is not None]
    if len(pts) < 2:
        return None
    t0, t1 = pts[0][0], pts[-1][0]
    vals = [v for _, v in pts] + ([limit] if limit is not None else [])
    lo, hi = min(vals) - 2, max(vals) + 2

    def y(v):
        return round(height - (v - lo) / (hi - lo) * height, 2)
    line = " ".join(f"{round((t - t0) / max(1, t1 - t0) * 100, 2)},{y(v)}" for t, v in pts)
    return {"points": line, "limit_y": y(limit) if limit is not None else None, "h": height,
            "min": min(v for _, v in pts), "max": max(v for _, v in pts)}


def modem_snaps():
    out, now = {}, time.time()
    for k in modem.qm_wans():
        snap = modem.snapshot(k, now)
        ser = modem_store.series(k, ["temp", "cpu_temp"], now - 86400, now, points=96)
        merged = {}
        for name in ("temp", "cpu_temp"):
            for b, avg, _lo, hi in ser[name]:
                merged[b] = max(merged.get(b, hi), hi)
        snap["spark"] = spark(sorted(merged.items()), snap["warn"])
        out[k] = snap
    return out


@app.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", s=monitor.snapshot(), log=events.recent(15), page="status",
                           schedules=config.SCHEDULES, has_capture=bool(capture_wans()), modems=modem_snaps())


RANGES = {"6h": 6, "day": 24, "week": 168, "month": 720, "45d": 1080}     # chart ranges (hours)
RANGE_LABELS = {"6h": "6 hours", "day": "Day", "week": "Week", "month": "Month", "45d": "45 days"}
RANGE_ALIASES = {"24h": "day", "7d": "week", "30d": "month"}               # links from v3.2
HEALTH_CHARTS = [
    ("Temperature", "°C", [("temp", "QManager"), ("cpu_temp", "CPU"), ("ambient_temp", "Ambient"),
                           ("pa_temp", "Amplifier (AT)"), ("case_temp", "Case (SSH)"), ("pa1_temp", "Amplifier 1 (SSH)"),
                           ("pa2_temp", "Amplifier 2 (SSH)")]),
    ("PoE power", "W", [("poe_w", "Watts")]),
    ("PoE current", "mA", [("poe_ma", "mA")]),
    ("PoE voltage", "V", [("poe_v", "Volts")]),
    ("Supply voltage", "mV", [("supply_mv", "AT+CBC"), ("vph_mv", "Supply rail (SSH)")]),
    ("Carriers and antennas", "", [("carriers", "Carriers"), ("lte_chains", "LTE antennas"),
                                   ("nr_chains", "5G antennas")]),
]


@app.get("/modem")
@login_required
def modem_page():
    """QManager-style signal page plus health charts for the QManager WAN. ?wan=&tab=signal|health&range="""
    wans = modem.qm_wans()
    s = monitor.snapshot()
    if not wans:
        return render_template("modem.html", s=s, page="modem", wan=None, wans=[])
    wan = request.args.get("wan") if request.args.get("wan") in wans else wans[0]
    tab = "health" if request.args.get("tab") == "health" else "signal"
    rng = _range()
    labels = {w["key"]: w["label"] for w in s["wans"]}
    return render_template("modem.html", s=s, page="modem", wan=wan, wans=[(k, labels.get(k, k)) for k in wans],
                           tab=tab, rng=rng, ranges=RANGE_LABELS, hours=RANGES[rng], m=modem.snapshot(wan), charts=HEALTH_CHARTS,
                           sensors=SSH_SENSORS)


def _range():
    """The chosen chart range (?range=); a day when missing or unknown."""
    r = request.args.get("range") or ""
    r = RANGE_ALIASES.get(r, r)
    return r if r in RANGES else "day"


def _hours():
    return RANGES[_range()]


@app.get("/api/modem/<wan>/signal")
@login_required
def api_modem_signal(wan):
    if wan not in modem.qm_wans():
        abort(404)
    now = time.time()
    return jsonify(modem_store.signal_series(wan, now - _hours() * 3600, now))


@app.get("/api/modem/<wan>/stamp")
@login_required
def api_modem_stamp(wan):
    """Newest data time for a tab: the page polls this and redraws only when it changes."""
    if wan not in config.WAN_KEYS:
        abort(404)
    return jsonify(stamp=modem_store.stamp(wan, "signal" if request.args.get("tab") != "health" else "health"))


@app.get("/api/modem/<wan>/series")
@login_required
def api_modem_series(wan):
    if wan not in config.WAN_KEYS:
        abort(404)
    names = [n for n in request.args.get("names", "").split(",") if re.fullmatch(r"[a-z0-9_]{1,24}", n)][:24]
    now = time.time()
    return jsonify(modem_store.series(wan, names, now - _hours() * 3600, now))


@app.get("/api/status")
@login_required
def api_status():
    return jsonify(monitor.snapshot())


@app.get("/api/last-check")
@login_required
def api_last_check():
    """For the header's "Last check" on every page (small: not the whole status)."""
    return jsonify(last_check=monitor.last_check, interval=cfg["check_interval"])


@app.post("/cycle/<key>")
@login_required
def cycle(key):
    ok, msg = monitor.manual_cycle(key)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("dashboard"))


def save_settings(form, partial):
    new, errors = config.validate(form, partial=partial)
    if errors:
        return errors
    old = dict(settings.data)
    changed = {k: v for k, v in new.items() if old.get(k) != v}
    if changed:
        settings.set(**changed)
        apply_settings()
        parts = []
        for k, v in changed.items():
            label = config.FIELDS[k][1]
            if config.FIELDS[k][2] in ("urls", "weburls"):
                parts.append(f"{label} changed")
            else:
                parts.append(f"{label}: {old.get(k)} -> {v}")
        events.add("info", None, "Settings changed from the web page: " + "; ".join(parts))
    return []


@app.post("/settings")
@login_required
def update_settings():
    """Quick switches on the dashboard."""
    form = {"enabled": request.form.get("enabled") == "on", "dry_run": request.form.get("dry_run") == "on"}
    if "capture_schedule" in request.form:
        form["capture_schedule"] = request.form["capture_schedule"]
    errors = save_settings(form, partial=True)
    flash("; ".join(errors) if errors else "Settings saved", "error" if errors else "ok")
    return redirect(url_for("dashboard"))


@app.post("/wan/<key>/enabled")
@login_required
def set_wan_enabled(key):
    """Dashboard checkbox: turn watchdog management of one WAN on or off."""
    if key not in config.WAN_KEYS:
        abort(404)
    save_settings({f"{key}_enabled": request.form.get("enabled") == "on"}, partial=True)
    return redirect(url_for("dashboard"))


@app.post("/vpn/enabled")
@login_required
def set_vpn_enabled():
    """Dashboard checkbox: turn watchdog watching/reconnecting of the VPN on or off (vpn_check)."""
    save_settings({"vpn_check": request.form.get("enabled") == "on"}, partial=True)
    return redirect(url_for("dashboard"))


def public_key():
    """The watchdog's SSH public key (for the modem sensors setup), or None."""
    try:
        with open(modem_sensors.key_file + ".pub") as f:
            key = f.read().strip()
        return key if re.fullmatch(r"ssh-[a-z0-9-]+ [A-Za-z0-9+/=]+( [\w@.-]+)?", key) else None
    except OSError:
        return None


@app.route("/settings/all", methods=["GET", "POST"])
@login_required
def all_settings():
    values, errors = settings.data, []
    if request.method == "POST":
        form = {k: v for k, v in request.form.items() if k in config.FIELDS}
        errors = save_settings(form, partial=False)
        if not errors:
            flash("Settings saved - they apply from the next check", "ok")
            return redirect(url_for("all_settings"))
        values = {**settings.data, **form}
        flash("Not saved: " + "; ".join(errors), "error")
    return render_template("settings.html", s=monitor.snapshot(), values=values, fields=config.FIELDS,
                           groups=config.GROUPS, schedules=config.SCHEDULES,
                           wan_names={k: settings.data[f"{k}_label"] for k in config.WAN_KEYS},
                           defaults={k: f[3] for k, f in config.FIELDS.items()},
                           codes={k: bool(v) or k in capture_auth() for k, v in capture_codes().items()},
                           pubkey=public_key(),
                           addresses={"UNIFI_URL": UNIFI_URL,
                                      "ATT_GATEWAY": config.expand("{ATT_GATEWAY}"),
                                      "QUECTEL_GATEWAY": config.expand("{QUECTEL_GATEWAY}")})


@app.route("/settings/from-gateway", methods=["GET", "POST"])
@login_required
def settings_from_gateway():
    """Preview, then copy each WAN's name and port from the Dream Machine into the settings."""
    try:
        found = client.wans()
    except UniFiError as e:
        flash(f"Couldn't read the Dream Machine: {e}", "error")
        return redirect(url_for("all_settings"))
    rows, form = [], {}
    for key in config.WAN_KEYS:
        g = found.get(key) or {}
        for field, new in (("label", g.get("name")), ("port", g.get("port"))):
            cur = settings.get(f"{key}_{field}")
            if new is None:
                continue
            rows.append({"wan": key, "field": "name" if field == "label" else "gateway port",
                         "current": cur, "new": new, "ifname": g.get("ifname"), "changed": str(cur) != str(new)})
            if str(cur) != str(new):
                form[f"{key}_{field}"] = new
    if request.method == "POST":
        if not form:
            flash("Already matches the Dream Machine - nothing changed", "ok")
        else:
            errors = save_settings(form, partial=True)
            flash("; ".join(errors) if errors else "WAN names and ports updated from the Dream Machine",
                  "error" if errors else "ok")
        return redirect(url_for("all_settings"))
    return render_template("from_gateway.html", s=monitor.snapshot(), rows=rows, changes=bool(form))


@app.post("/settings/reset")
@login_required
def reset_settings():
    defaults = {k: f[3] for k, f in config.FIELDS.items() if k not in ("enabled", "dry_run")}
    save_settings(defaults, partial=True)
    flash("Settings reset to the defaults (Automation and Dry run left as they were)", "ok")
    return redirect(url_for("all_settings"))


@app.get("/log")
@login_required
def log():
    wan, level = request.args.get("wan") or None, request.args.get("level") or None
    return render_template("log.html", s=monitor.snapshot(), log=events.recent(1000, wan, level), wan=wan, level=level)


@app.get("/diag")
@login_required
def diag():
    return render_template("diag.html", s=monitor.snapshot(),
                           diag=client.diagnostics({w[2] for w in cfg["wans"]}))


@app.post("/fingerprint/<key>/relearn")
@login_required
def relearn(key):
    if key not in config.WAN_KEYS:
        abort(404)
    monitor.relearn(key)
    flash("Modem fingerprint cleared; it's learned again the next time that WAN is healthy", "ok")
    return redirect(url_for("all_settings"))


@app.post("/bounce/<key>")
@login_required
def bounce(key):
    ok, msg = monitor.manual_bounce(key)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.post("/vpn/restart")
@login_required
def vpn_restart():
    ok, msg = monitor.manual_vpn_restart()
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.post("/bounce-check")
@login_required
def bounce_check():
    if not bouncer.configured():
        flash("No SSH key at /data/ssh/id_ed25519", "error")
    else:
        ok = monitor.check_bounce_access()
        flash("Port-bounce access works" if ok else "Port-bounce access is NOT working - see the log",
              "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.post("/bounce-forget-host-key")
@login_required
def bounce_forget_host_key():
    bouncer.forget_host_key()
    events.add("warn", None, "Stored gateway SSH host key forgotten from the web page; the next connection "
                             "trusts the gateway's current key")
    ok = monitor.check_bounce_access() if bouncer.configured() else False
    flash("Host key forgotten - port-bounce access works again" if ok else
          "Host key forgotten, but port-bounce access still isn't working - see the log", "ok" if ok else "error")
    return redirect(url_for("dashboard"))


@app.post("/capture/<key>")
@login_required
def capture_now(key):
    w = next((w for w in monitor.wans if w.key == key), None)
    if not w or not capturer.has(key):
        flash("No modem pages are configured for that WAN", "error")
        return redirect(url_for("dashboard"))
    name = monitor.save_pages(w, "on request from the web page")
    flash(f"Saved as {name}" if name else "Nothing could be saved - see the log", "ok" if name else "error")
    if not name:
        return redirect(url_for("dashboard"))
    return redirect(url_for("captures", wan=key, date=f"{name[:4]}-{name[4:6]}-{name[6:8]}", name=name))


def capture_wans():
    return sorted((w for w in monitor.wans if capturer.has(w.key)), key=lambda w: w.key)   # WAN 1 first


def raw_page(name, page):
    """A saved page; QCAINFO pages from before v3.2 get their 5G rows re-labelled."""
    raw = capturer.raw(name, page)
    return fix_old_qcainfo(raw) if raw and "QCAINFO" in page else raw


def nice_time(name):
    return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").strftime("%H:%M:%S")


@app.get("/captures")
@login_required
def captures():
    """One capture, looking like the modem's own page. ?wan=&date=YYYY-MM-DD&name=&page="""
    wans = capture_wans()
    if not wans:
        return render_template("captures.html", s=monitor.snapshot(), wans=[], view="none")
    wan = request.args.get("wan") or wans[0].key
    all_names = capturer.list(wan)
    dates = sorted({f"{n[:4]}-{n[4:6]}-{n[6:8]}" for n in all_names}, reverse=True)
    day = request.args.get("date") or (dates[0] if dates else datetime.now().strftime("%Y-%m-%d"))
    names = capturer.list(wan, day.replace("-", ""))
    name = request.args.get("name") if request.args.get("name") in names else (names[0] if names else None)
    page_keys = [k for k, _ in (capturer.pages(name) or [])] if name else []
    page = request.args.get("page") if request.args.get("page") in page_keys else (page_keys[0] if page_keys else None)
    body = modem_pages.clean_html(raw_page(name, page)) if page else ""
    return render_template("captures.html", s=monitor.snapshot(), view="one", wans=wans, wan=wan, dates=dates,
                           day=day, names=[(n, nice_time(n), capturer.meta(n).get("reason", "")) for n in names],
                           name=name, page_keys=page_keys, page=page, body=body)


@app.get("/captures/combined")
@login_required
def captures_combined():
    """Last N days merged: log pages -> unique rows newest first; status pages -> one column per day."""
    wans = capture_wans()
    if not wans:
        return redirect(url_for("captures"))
    wan = request.args.get("wan") or wans[0].key
    try:
        days = int(request.args.get("days") or 30)
    except ValueError:
        days = 30
    days = max(1, min(days, cfg["capture_keep_days"]))
    cutoff = (datetime.now().timestamp() - days * 86400)
    names = [n for n in capturer.list(wan) if datetime.strptime(n[:15], "%Y%m%d-%H%M%S").timestamp() >= cutoff]
    page_keys = []
    for n in names:
        for k, _ in capturer.pages(n) or []:
            if k not in page_keys:
                page_keys.append(k)
    page = request.args.get("page") if request.args.get("page") in page_keys else (page_keys[0] if page_keys else None)
    log = sections = columns = None
    if page:
        with_page = [(n, raw_page(n, page)) for n in names]
        with_page = [(n, raw) for n, raw in with_page if raw]
        log = modem_pages.merge_logs(with_page)
        if log is None:
            daily, seen = [], set()            # newest capture of each day, oldest day first
            for n, raw in with_page:
                if n[:8] not in seen:
                    seen.add(n[:8])
                    daily.append((n, raw))
            sections, columns = modem_pages.history(list(reversed(daily)))
            columns = [f"{c[4:6]}/{c[6:8]}" for c in columns]
    return render_template("captures.html", s=monitor.snapshot(), view="combined", wans=wans, wan=wan, days=days,
                           count=len(names), page_keys=page_keys, page=page, log=log, sections=sections,
                           columns=columns)


@app.get("/internet")
@login_required
def internet():
    """Probe history per WAN: one row per hour (loss, latency, failed probes). ?days=1|7|30"""
    try:
        days = int(request.args.get("days") or 1)
    except ValueError:
        days = 1
    days = max(1, min(days, probe_history.keep_days))
    since = time.time() - days * 86400
    s = monitor.snapshot()
    hist = {w["key"]: probe_history.hourly(w["key"], since) for w in s["wans"]}
    if s["vpn"]["watched"]:
        hist["vpn"] = probe_history.hourly("vpn", since)
    totals = {}
    for key, rows in hist.items():
        sent, recv = sum(r["sent"] for r in rows), sum(r["recv"] for r in rows)
        rtts = [r["avg_ms"] for r in rows if r["avg_ms"] is not None]
        totals[key] = {"checks": sum(r["checks"] for r in rows), "failed": sum(r["failed"] for r in rows),
                       "loss": round(100 * (1 - recv / sent), 2) if sent else None,
                       "avg_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
                       "worst_ms": max((r["worst_ms"] for r in rows if r["worst_ms"] is not None), default=None)}
    return render_template("internet.html", s=s, hist=hist, totals=totals, days=days)


@app.get("/speed")
@login_required
def speed_page():
    """Speed-test results (newest first) and the averages per carrier setup. ?days=7|30|90|365"""
    try:
        days = int(request.args.get("days") or 30)
    except ValueError:
        days = 30
    days = max(1, min(days, speed_store.keep_days))
    rows = speed_store.recent(time.time() - days * 86400)
    for r in rows:
        r["link_text"] = link_text(r.get("link"))
        link = r.get("link") or {}
        r["per_mhz"] = round(r["down_mbps"] / link["total_mhz"], 2)             if r["down_mbps"] and link.get("kind") == "cell" and link.get("total_mhz") else None
        r["of_sync"] = round(100 * r["down_mbps"] * 1000 / link["sync_down"])             if r["down_mbps"] and link.get("kind") == "dsl" and link.get("sync_down") else None
    return render_template("speed.html", s=monitor.snapshot(), rows=rows, setups=by_setup(rows), days=days,
                           running=speed.snapshot(), next_run=speed.next_run(datetime.now()),
                           targets=speed.targets())


@app.post("/speed/run")
@login_required
def speed_run():
    only = request.form.get("wan") or None
    ok, msg = speed.start("on request from the web page", only=only)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("speed_page"))


@app.get("/captures/<name>/text")
@login_required
def capture_text(name):
    files = capturer.files(name)
    if files is None:
        abort(404)
    return render_template("captures_text.html", s=monitor.snapshot(), name=name, files=files)


def capture_schedule():
    """Save each configured modem's pages hourly or daily (web-page setting), for the 30-day view."""
    last_key = None
    time.sleep(60)
    while True:
        key = schedule_slot(settings.get("capture_schedule"), datetime.now(), last_key)
        if key:
            last_key = key
            for w in capture_wans():
                monitor.save_pages(w, f"({settings.get('capture_schedule')})", quiet=True)
        time.sleep(30)


def signal_sampler():
    """Every signal_sample_minutes: QManager's antenna history and status into modem.db (0 = off). First, the
    numbers from the saves already on disk, so the charts reach back 45 days straight away."""
    try:
        n = modem.backfill()
        if n:
            print(f"Modem charts: read {n} saved page set(s)", flush=True)
    except Exception as e:
        events.add("error", None, f"Reading the saved modem pages for the charts failed: {e}")
    time.sleep(45)
    last = 0.0
    while True:
        every = int(settings.get("signal_sample_minutes") or 0) * 60
        if every and time.time() - last >= every:
            last = time.time()
            for wan in modem.qm_wans():
                try:
                    modem.sample(wan)
                except Exception as e:
                    events.add("error", wan, f"Modem signal sampler failed: {e}")
        time.sleep(20)


def box_reboot_schedule():
    """Once a day, in the configured hour, reboot the chosen WAN's modem if its latency has crept up."""
    time.sleep(90)
    while True:
        try:
            monitor.box_reboot_tick(datetime.now())
        except Exception as e:
            events.add("error", None, f"Modem-reboot check failed: {e}")
        time.sleep(30)


def speed_schedule():
    """Start the scheduled speed tests (Settings: Speed test)."""
    time.sleep(60)
    while True:
        try:
            speed.tick(datetime.now())
        except Exception as e:
            events.add("error", None, f"Speed-test schedule failed: {e}")
        time.sleep(20)


@app.get("/healthz")
def healthz():
    last = monitor.last_check
    stale = last is None or time.time() - last > cfg["check_interval"] * 4
    return ("stale", 503) if stale and time.time() - START > 120 else ("ok", 200)


START = time.time()

if __name__ == "__main__":
    monitor.start()
    threading.Thread(target=capture_schedule, daemon=True, name="capture-schedule").start()
    threading.Thread(target=box_reboot_schedule, daemon=True, name="box-reboot").start()
    threading.Thread(target=signal_sampler, daemon=True, name="signal-sampler").start()
    threading.Thread(target=speed_schedule, daemon=True, name="speed-schedule").start()
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threads=4, ident="wan-watchdog")
