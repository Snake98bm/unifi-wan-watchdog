"""WAN watchdog: web UI + background monitor.

Secrets and the gateway address come from environment variables; everything else is edited on the
Settings page and saved in /data/settings.json (see config.py).
"""
import hmac
import os
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
from bounce import Bouncer
from capture import Capturer, schedule_slot
from monitor import Monitor
from store import Events, Settings
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
    if "{QUECTEL_GATEWAY}" in cur + str(settings.data.get("at_url_wan1") or "") and "get_uptime" not in cur:
        settings.data["capture_wan1"] = "\n".join([cur.rstrip("\n")] + config.QUECTEL_PAGES)
    settings.data["settings_version"] = 3
if settings.data.get("settings_version", 1) < 4:   # v4 added the cause-code AT commands (CEER, CEREG, ...)
    cur = str(settings.data.get("capture_wan1") or "")
    if "{QUECTEL_GATEWAY}" in cur + str(settings.data.get("at_url_wan1") or "") and "AT+CEER" not in cur:
        lines = cur.rstrip("\n").split("\n")
        at_end = max((i for i, ln in enumerate(lines) if ln.lower().startswith("at:")), default=len(lines) - 1)
        lines[at_end + 1:at_end + 1] = ["at:" + c for c in config.CAUSE_AT]   # after the other AT lines
        settings.data["capture_wan1"] = "\n".join(lines)
    settings.data["settings_version"] = 4
settings.set()                                   # write any newly added settings to the file
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
    """HTTP login for modem pages: QUECTEL_USER / QUECTEL_PASSWORD for the WAN whose pages use {QUECTEL_GATEWAY}
    (the RM520N-GL toolkit), or <KEY>_USER / <KEY>_PASSWORD for any WAN."""
    out = {}
    for k in config.WAN_KEYS:
        user, pw = os.environ.get(f"{k.upper()}_USER"), os.environ.get(f"{k.upper()}_PASSWORD")
        uses = str(settings.get(f"capture_{k}") or "") + str(settings.get(f"at_url_{k}") or "")
        if not user and "{QUECTEL_GATEWAY}" in uses:
            user, pw = os.environ.get("QUECTEL_USER"), os.environ.get("QUECTEL_PASSWORD")
        if user:
            out[k] = (user, pw or "")
    return out


capturer = Capturer(os.path.join(DATA, "captures"), config.capture_urls(settings.data), capture_codes(),
                    keep_days=cfg["capture_keep_days"], auth_by_wan=capture_auth())
# Port bounce: the watchdog's own SSH key, locked on the gateway to wan-bounce.sh (gateway/install-bounce.sh)
bouncer = Bouncer(os.environ.get("BOUNCE_SSH_HOST", UNIFI_URL.split("//")[-1].split(":")[0].split("/")[0]),
                  os.environ.get("BOUNCE_SSH_KEY", os.path.join(DATA, "ssh", "id_ed25519")),
                  os.path.join(DATA, "ssh", "known_hosts"))
monitor = Monitor(cfg, client, events, settings, capture=capturer, bouncer=bouncer)


def apply_settings():
    new_cfg = config.monitor_cfg(settings.data)
    monitor.apply_config(new_cfg)
    capturer.urls_by_wan = config.capture_urls(settings.data)
    capturer.codes_by_wan = capture_codes()
    capturer.auth_by_wan = capture_auth()
    capturer.keep_days = new_cfg["capture_keep_days"]

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
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        recent.append(now)
        events.add("warn", None, f"Failed login from {ip}")
        return render_template("login.html", error="Wrong password."), 401
    return render_template("login.html", error=None)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", s=monitor.snapshot(), log=events.recent(15),
                           schedules=config.SCHEDULES, has_capture=bool(capture_wans()))


@app.get("/api/status")
@login_required
def api_status():
    return jsonify(monitor.snapshot())


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
            if config.FIELDS[k][2] == "urls":
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


@app.post("/bounce-check")
@login_required
def bounce_check():
    if not bouncer.configured():
        flash("No SSH key at /data/ssh/id_ed25519", "error")
    else:
        with monitor.lock:
            ok = monitor.check_bounce_access()
        flash("Port-bounce access works" if ok else "Port-bounce access is NOT working - see the log",
              "ok" if ok else "error")
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
    return [w for w in monitor.wans if capturer.has(w.key)]


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
    body = modem_pages.clean_html(capturer.raw(name, page)) if page else ""
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
    days = max(1, min(int(request.args.get("days", 30) or 30), cfg["capture_keep_days"]))
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
        with_page = [(n, capturer.raw(n, page)) for n in names]
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


@app.get("/healthz")
def healthz():
    last = monitor.last_check
    stale = last is None or time.time() - last > cfg["check_interval"] * 4
    return ("stale", 503) if stale and time.time() - START > 120 else ("ok", 200)


START = time.time()

if __name__ == "__main__":
    monitor.start()
    threading.Thread(target=capture_schedule, daemon=True, name="capture-schedule").start()
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threads=4, ident="wan-watchdog")
