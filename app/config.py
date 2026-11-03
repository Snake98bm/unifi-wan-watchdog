"""Editable settings: defaults, limits and help text, saved in /data/settings.json and changed on the
Settings page. Environment variables with the same name in capitals (e.g. DOWN_MINUTES) only seed a
setting the first time, when settings.json doesn't have it yet. Secrets (API key, password, access
code) and addresses (UNIFI_URL, ATT_GATEWAY) stay in the environment; page URLs refer to an address
as {ATT_GATEWAY}.
"""
import os
import re
from urllib.parse import quote

WAN_KEYS = ("wan1", "wan2")
# RM520N-GL toolkit read-only status endpoints: uptime shows when the modem last rebooted; watchcat is the
# toolkit's own ping watchdog, which reboots the modem when it's set up
QUECTEL_PAGES = ["https://{QUECTEL_GATEWAY}/cgi-bin/get_uptime", "https://{QUECTEL_GATEWAY}/cgi-bin/get_watchcat_status"]
# why it dropped: last disconnect reason, LTE/5G registration (+ reject cause), data session state and details
CAUSE_AT = ("AT+CEER", "AT+CEREG?", "AT+C5GREG?", "AT+CGACT?", "AT+CGCONTRDP")
SCHEDULES = {"hourly": "Hourly (on the hour)", "daily": "Daily (23:55)", "off": "Off"}

# key: (group, label, kind, default, min, max, help)
FIELDS = {
    "enabled": ("General", "Automation", "bool", True, None, None,
                "Off = only watch and log, never bounce or power-cycle on its own."),
    "dry_run": ("General", "Dry run", "bool", True, None, None,
                "On = the watchdog only logs what it would do on its own. The Bounce and Power-cycle "
                "buttons always really act."),
    "check_interval": ("General", "Check every (seconds)", "int", 30, 10, 300,
                       "How often the gateway is read."),
    "down_minutes": ("Reset rules", "Down for (minutes) before acting", "int", 5, 1, 120,
                     "A modem's own quick reboot takes about 1 minute, so keep this above 2."),
    "private_minutes": ("Reset rules", "Private IP for (minutes) before acting", "int", 5, 1, 120,
                        "A WAN with a private IP is re-checked this long before acting."),
    "recovery_minutes": ("Reset rules", "Recovery window after a power cycle (minutes)", "int", 10, 1, 60,
                         "No other WAN is touched until this one is healthy again or this time has passed."),
    "cooldown_minutes": ("Reset rules", "Cooldown between power cycles of one WAN (minutes)", "int", 30, 5, 1440, ""),
    "max_cycles_per_day": ("Reset rules", "Max power cycles per WAN per day", "int", 6, 0, 48,
                           "0 = never power-cycle automatically."),
    "first_wan": ("Reset rules", "If both need it, reset first", "wan", "wan2", None, None,
                  "Only one WAN is ever reset at a time."),
    "bounce_wait_minutes": ("Port bounce", "Wait after a bounce before the power cycle (minutes)", "int", 3, 1, 30, ""),
    "capture_schedule": ("Modem logs", "Save modem logs", "schedule", "hourly", None, None,
                         "Pages are always saved before a power cycle as well."),
    "capture_keep_days": ("Modem logs", "Keep saved logs (days)", "int", 45, 1, 365, ""),
    "wan1_label": ("WANs", "WAN 1 name", "text", "Verizon", None, None, ""),
    "wan1_port": ("WANs", "WAN 1 gateway port", "int", 8, 1, 11, "Port number on the UniFi gateway (the WAN cable's port)."),
    "capture_wan1": ("WANs", "WAN 1 modem pages to save", "urls",
                     "\n".join("at:" + c for c in ('AT+QENG="servingcell"', "AT+QCAINFO", "AT+QTEMP", "AT+QNWINFO",
                                                   "AT+COPS?", "AT+CSQ", "AT+CFUN?", "AT+CPIN?", 'AT+QMAP="WWAN"',
                                                   "AT+QUIMSLOT?", "AT+QGMR") + CAUSE_AT)
                     + "\n" + "\n".join(QUECTEL_PAGES),
                     None, None, "One per line: a URL, or at:<AT command> (run through the AT URL below). "
                                 "Uses QUECTEL_USER / QUECTEL_PASSWORD."),
    "at_url_wan1": ("WANs", "WAN 1 AT-command URL", "aturl",
                    "https://{QUECTEL_GATEWAY}/cgi-bin/get_atcommand?atcmd={AT}", None, None,
                    "{AT} is replaced by the command. RM520N-GL toolkit (SimpleAdmin)."),
    "wan2_label": ("WANs", "WAN 2 name", "text", "AT&T", None, None, ""),
    "wan2_port": ("WANs", "WAN 2 gateway port", "int", 1, 1, 11, "Port number on the UniFi gateway (the WAN cable's port)."),
    "capture_wan2": ("WANs", "WAN 2 modem pages to save", "urls",
                     "\n".join("http://{ATT_GATEWAY}/cgi-bin/" + p + ".ha" for p in
                               ("home", "broadbandstatistics", "sysinfo", "logs", "syslog", "events")),
                     None, None, "One per line. {ATT_GATEWAY} is filled in from the ATT_GATEWAY variable "
                                 "(any {NAME_GATEWAY} variable works). Syslog and Events need ATT_ACCESS_CODE."),
    "at_url_wan2": ("WANs", "WAN 2 AT-command URL", "aturl", "", None, None, "Empty = no at: lines for this WAN."),
}
GATEWAY_DEFAULTS = {"ATT_GATEWAY": "192.168.1.254", "QUECTEL_GATEWAY": "192.168.225.1"}
PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+_GATEWAY)\}")


def gateway_value(name):
    """A *_GATEWAY variable as a bare host[:port]: 'https://192.168.225.1/' -> '192.168.225.1'."""
    v = (os.environ.get(name) or GATEWAY_DEFAULTS.get(name) or "").strip()
    return re.sub(r"^[a-z]+://", "", v, flags=re.I).rstrip("/")


def expand(url):
    """Fill {NAME_GATEWAY} placeholders from the environment (only *_GATEWAY names, so no secret can leak
    into a URL). Unknown names are left as they are, which makes the fetch fail visibly in the log."""
    return PLACEHOLDER_RE.sub(lambda m: gateway_value(m.group(1)) or m.group(0), url)
GROUPS = ["General", "Reset rules", "Port bounce", "Modem logs", "WANs"]


def _from_env(key, kind):
    if key in ("enabled", "dry_run"):
        name = "AUTOMATION" if key == "enabled" else "DRY_RUN"
    else:
        name = key.upper()
    raw = os.environ.get(name)
    if raw is None:
        return None
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "urls":
        return "\n".join(u.strip() for u in raw.split(",") if u.strip())
    return raw


def initial_values():
    """Defaults, overridden by the old environment variables (used only when settings.json lacks a key)."""
    values = {}
    old_wans = os.environ.get("WANS")            # "wan2:AT&T:1,wan1:Verizon:8" (order = priority)
    if old_wans:
        items = [i.split(":") for i in old_wans.split(",")]
        for key, label, port in (tuple(p.strip() for p in i) for i in items if len(i) == 3):
            if key in WAN_KEYS:
                values[f"{key}_label"], values[f"{key}_port"] = label, port
        if items and items[0][0].strip() in WAN_KEYS:
            values["first_wan"] = items[0][0].strip()
    for key, (_, _, kind, default, *_rest) in FIELDS.items():
        env = _from_env(key, kind)
        values.setdefault(key, env if env is not None else default)
    ok, _ = validate({k: v for k, v in values.items()}, partial=False)
    return {**{k: f[3] for k, f in FIELDS.items()}, **ok}


def validate(form, partial=True):
    """form: dict of raw values (strings or already-typed). Returns (clean values, errors)."""
    clean, errors = {}, []
    for key, (_, label, kind, default, lo, hi, _) in FIELDS.items():
        if key not in form:
            if kind == "bool" and not partial:
                clean[key] = False
            continue
        raw = form[key]
        if kind == "bool":
            clean[key] = raw if isinstance(raw, bool) else str(raw).lower() in ("on", "1", "true", "yes")
        elif kind == "int":
            try:
                v = int(str(raw).strip())
            except ValueError:
                errors.append(f"{label}: not a whole number")
                continue
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(f"{label}: must be {lo} to {hi}")
                continue
            clean[key] = v
        elif kind == "wan":
            if raw not in WAN_KEYS:
                errors.append(f"{label}: unknown WAN")
                continue
            clean[key] = raw
        elif kind == "schedule":
            if raw not in SCHEDULES:
                errors.append(f"{label}: unknown schedule")
                continue
            clean[key] = raw
        elif kind == "text":
            v = str(raw).strip()[:40]
            if not v:
                errors.append(f"{label}: can't be empty")
                continue
            clean[key] = v
        elif kind == "urls":
            items = [u.strip() for u in str(raw).replace("\r", "").split("\n") if u.strip()]
            bad = [u for u in items if not (re.match(r"^at:\s*AT[A-Z0-9+&%$#^*=?\",._-]*$", u, re.I) or (
                re.match(r"^https?://[^\s/]+(/\S*)?$", u) and not re.search(r"\{(?![A-Z0-9_]+_GATEWAY\})", u)))]
            if bad:
                errors.append(f"{label}: not a URL or at:AT command: {bad[0]}")
                continue
            clean[key] = "\n".join(items)
        elif kind == "aturl":
            v = str(raw).strip()
            if v and not (re.match(r"^https?://[^\s/]+(/\S*)?$", v) and "{AT}" in v
                          and not re.search(r"\{(?!AT\}|[A-Z0-9_]+_GATEWAY\})", v)):
                errors.append(f"{label}: must be a URL containing {{AT}}")
                continue
            clean[key] = v
    return clean, errors


def wans_of(values):
    """[(key, label, port)] in reset order (first_wan first)."""
    first = values.get("first_wan", "wan2")
    order = [first] + [k for k in WAN_KEYS if k != first]
    return [(k, values[f"{k}_label"], int(values[f"{k}_port"])) for k in order]


def monitor_cfg(values):
    keys = ("check_interval", "down_minutes", "private_minutes", "recovery_minutes", "cooldown_minutes",
            "max_cycles_per_day", "bounce_wait_minutes", "capture_keep_days")
    return {**{k: int(values[k]) for k in keys}, "wans": wans_of(values)}


def capture_urls(values):
    """What to save per WAN: [{"url", "name", "at"}] with {NAME_GATEWAY} filled in. "at" is the AT command for
    at: lines (their URL is the WAN's AT URL with {AT} replaced), else None."""
    out = {}
    for k in WAN_KEYS:
        items, at_url = [], str(values.get(f"at_url_{k}") or "")
        for line in str(values.get(f"capture_{k}", "")).split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("at:"):
                cmd = line[3:].strip()
                if at_url:
                    items.append({"url": expand(at_url).replace("{AT}", quote(cmd, safe="")),
                                  "name": re.sub(r"[^A-Za-z0-9]+", "_", cmd).strip("_"), "at": cmd})
            else:
                items.append({"url": expand(line), "name": None, "at": None})
        out[k] = items
    return out
