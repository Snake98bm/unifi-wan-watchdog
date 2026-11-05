"""Save a modem's status/log pages before its port is power-cycled (the reboot wipes them).

Pages come from CAPTURE_<WAN KEY> (e.g. CAPTURE_WAN2), a comma-separated list of URLs. If a page answers
with the AT&T/Arris "Device Access Code" login form and <WAN KEY>_ACCESS_CODE is set (e.g.
ATT_ACCESS_CODE for AT&T, mapped in main.py), it logs in once (md5(code + nonce), like the box's own page)
and fetches the page again. Only GET requests are made after login: nothing on the modem is changed.
A Quectel RM520N-GL running QManager (any page under /cgi-bin/quecmanager/) is logged in to once per capture
with QUECTEL_PASSWORD (JSON POST, session cookie), its AT commands are POSTed to send_command.sh, and the
session is logged out afterwards. A refused password is not tried again until it changes (QManager locks
logins out after a few failures).
Each capture is a folder under /data/captures with the raw HTML and a plain-text copy for the web page.
"""
import hashlib
import html
import json
import os
import re
import shutil
import time
from urllib.parse import urljoin

import requests
import urllib3

from atparse import at_error, at_html, at_response, text_html

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9]+$")
LOGIN_FORM_RE = re.compile(r'action="(/cgi-bin/login\.ha)"', re.I)
NONCE_RE = re.compile(r'name="nonce"\s+value="([^"]+)"', re.I)
QM_PATH = "/cgi-bin/quecmanager/"


def qmanager_base(items):
    """'https://host' when any of these pages is a QManager one, else None."""
    for it in items:
        url = it["url"] if isinstance(it, dict) else it
        i = url.find(QM_PATH)
        if i > 0:
            return url[:i]
    return None


def qmanager_login(session, base, password, timeout=10):
    """Log in to QManager: POST {"password"} to auth/login.sh, which sets the qm_session cookie on the session.
    Raises PermissionError when it's refused (the message says why) or requests errors when unreachable."""
    if not password:
        raise PermissionError("needs a login (QUECTEL_PASSWORD not set)")
    r = session.post(base + QM_PATH + "auth/login.sh", json={"password": password}, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    err = data.get("error")
    if r.status_code == 429 or err == "rate_limited":
        raise PermissionError(f"QManager has locked logins for {data.get('retry_after', 'a few')} s "
                              "(too many wrong passwords)")
    if data.get("success") is True:
        return
    if err == "invalid_password":
        raise PermissionError("login refused - check QUECTEL_PASSWORD")
    if err == "setup_required":
        raise PermissionError("QManager has no password yet - finish its setup first")
    raise PermissionError(f"login failed ({err or f'HTTP {r.status_code}'})")


def reboot_modem(host, code, timeout=10):
    """Reboot an AT&T/Arris modem through its Restart Device page: GET login.ha (sets the SessionID cookie),
    read the access-code nonce from restart.ha, POST login.ha to authenticate (md5(code+nonce), like the box's
    own page), then POST Restart to restart.ha. The box drops its web server as it reboots, so a connection
    error on the final POST means the reboot started. Raises PermissionError (bad/missing code) or
    RuntimeError (the expected page/button wasn't there, e.g. the firmware changed)."""
    if not code:
        raise PermissionError("needs the device access code")
    base = f"http://{host}/cgi-bin/"
    with requests.Session() as s:
        s.verify = False
        s.get(base + "login.ha", timeout=timeout)                       # sets the SessionID cookie
        r = s.get(base + "restart.ha", timeout=timeout)                 # access-code form (nonce)
        m = NONCE_RE.search(r.text)
        if not m:
            raise RuntimeError("restart page has no login form (cookies or firmware changed?)")
        s.post(base + "login.ha", timeout=timeout, data={
            "nonce": m.group(1), "password": "*" * len(code),
            "hashpassword": hashlib.md5((code + m.group(1)).encode()).hexdigest(), "Continue": "Continue"})
        r = s.get(base + "restart.ha", timeout=timeout)                 # authenticated restart page
        if "Access Code Required" in r.text:
            raise PermissionError("login failed - check the access code")
        m = NONCE_RE.search(r.text)
        if not m or 'name="Restart"' not in r.text:
            raise RuntimeError("Restart button not found on the restart page (firmware changed?)")
        try:
            s.post(base + "restart.ha", timeout=timeout,
                   data={"nonce": m.group(1), "Restart": "Restart"})    # triggers the reboot
        except (requests.ConnectionError, requests.Timeout):
            pass                                                        # box dropped as it rebooted: expected


def schedule_slot(schedule, now, last_key):
    """Key of the scheduled-capture slot that is due at `now` (a datetime), or None.
    hourly: once per clock hour; daily: once a day from 23:55; anything else: never."""
    if schedule == "hourly":
        key = now.strftime("%Y%m%d%H")
    elif schedule == "daily" and (now.hour, now.minute) >= (23, 55):
        key = now.strftime("%Y%m%d")
    else:
        return None
    return key if key != last_key else None


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(tr|p|div|h[1-6]|li|table)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    lines = (re.sub(r"[ \t]+", " ", line).strip(" |") for line in text.splitlines())
    return "\n".join(line for line in lines if line)


class Capturer:
    """urls_by_wan: {wan: [{"url", "name", "at"}]} (or plain URL strings). codes_by_wan: AT&T-style access code.
    auth_by_wan: (user, password) for modems behind a login: HTTP Basic, or just the password for QManager.
    Modem LAN pages use self-signed certificates, so certificates aren't verified."""

    def __init__(self, base_dir, urls_by_wan, codes_by_wan=None, keep_days=45, timeout=10, auth_by_wan=None,
                 reboot_hosts=None):
        self.base_dir, self.urls_by_wan, self.keep_days, self.timeout = base_dir, urls_by_wan, keep_days, timeout
        self.codes_by_wan = codes_by_wan or {}
        self.auth_by_wan = auth_by_wan or {}
        self.reboot_hosts = reboot_hosts or {}   # {wan: modem host} for the Device -> Restart reboot
        self.qm_refused = {}                     # {wan: password QManager refused} - not retried
        os.makedirs(base_dir, exist_ok=True)

    def has(self, wan_key):
        return bool(self.urls_by_wan.get(wan_key))

    def can_reboot(self, wan_key):
        return bool(self.reboot_hosts.get(wan_key) and self.codes_by_wan.get(wan_key))

    def reboot(self, wan_key):
        """Reboot this WAN's modem (AT&T/Arris Device -> Restart). Raises if the host or access code is missing."""
        host = self.reboot_hosts.get(wan_key)
        if not host:
            raise RuntimeError("no modem address to reboot for this WAN")
        reboot_modem(host, self.codes_by_wan.get(wan_key), self.timeout)


    def _get(self, session, url, state, code, post=None):
        """GET a page (or POST `post` as JSON); if it is the access-code login form, log in once and GET it again."""
        r = session.post(url, json=post, timeout=self.timeout) if post is not None else \
            session.get(url, timeout=self.timeout)
        if r.status_code == 401:
            if state.get("qm"):
                raise PermissionError("QManager refused the session (logged out?)")
            raise PermissionError("login refused - check QUECTEL_USER / QUECTEL_PASSWORD" if session.auth
                                  else "needs a login (QUECTEL_USER / QUECTEL_PASSWORD not set)")
        r.raise_for_status()
        if not LOGIN_FORM_RE.search(r.text) and "Access Code Required" not in r.text:
            return r.text
        if not code:
            raise PermissionError("needs the device access code (not set)")
        if state.get("login_failed"):
            raise PermissionError("login already failed in this capture")
        if not NONCE_RE.search(r.text):          # first visit only sets the session cookie
            r = session.get(url, timeout=self.timeout)
        form = LOGIN_FORM_RE.search(r.text)
        nonce = NONCE_RE.search(r.text)
        if not (form and nonce):
            raise PermissionError("login form not found")
        session.post(urljoin(url, form.group(1)), timeout=self.timeout, data={
            "nonce": nonce.group(1), "password": "*" * len(code),
            "hashpassword": hashlib.md5((code + nonce.group(1)).encode()).hexdigest(), "Continue": "Continue"})
        r = session.get(url, timeout=self.timeout)
        r.raise_for_status()
        if LOGIN_FORM_RE.search(r.text) or "Access Code Required" in r.text:
            state["login_failed"] = True
            raise PermissionError("login failed - check the access code")
        return r.text

    def __call__(self, wan_key, reason="", extra=None):
        """Fetch every page for this WAN, plus `extra` [(page name, html)] gathered elsewhere (the gateway's
        WAN log). Returns (folder name, saved count, errors). If the modem can't be reached at all, the rest
        of its pages are skipped instead of each timing out."""
        urls = self.urls_by_wan.get(wan_key) or []
        code = self.codes_by_wan.get(wan_key)
        name = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]", "", wan_key.lower())
        folder = os.path.join(self.base_dir, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "meta.json"), "w") as f:
            json.dump({"wan": wan_key, "reason": reason, "time": time.time()}, f)
        saved, errors, state = 0, [], {}
        auth = self.auth_by_wan.get(wan_key)
        qm = qmanager_base(urls)
        with requests.Session() as session:
            session.verify = False
            session.auth = None if qm else (auth or None)
            if qm:
                state["qm"] = True
                if self._qm_login(session, qm, wan_key, (auth or ("", ""))[1], errors):
                    state["qm_logged_in"] = True
                else:
                    urls = []                       # every QManager page would only answer 401
            for i, item in enumerate(urls, 1):
                item = item if isinstance(item, dict) else {"url": item, "name": None, "at": None}
                url = item["url"]
                page = item["name"] or re.sub(r"[^A-Za-z0-9._-]", "_", url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or "page"
                try:
                    body = self._get(session, url, state, code, item.get("post"))
                except (requests.RequestException, PermissionError) as e:
                    errors.append(f"{page}: {e if isinstance(e, PermissionError) else e.__class__.__name__}")
                    if (session.auth and "login refused" in str(e)) or "refused the session" in str(e):
                        break                       # don't hammer the modem with a wrong password
                    if isinstance(e, (requests.ConnectionError, requests.Timeout)) and not saved:
                        errors.append(f"modem not reachable - skipped the other {len(urls) - i} page(s)")
                        break
                    continue
                if item["at"]:
                    err = at_error(body)
                    if err:
                        errors.append(f"{page}: modem answered {err}")
                        continue
                    text = at_response(body)
                    if not text:
                        errors.append(f"{page}: empty answer")
                        continue
                    body = at_html(item["at"], text)
                elif body.lstrip()[:1] in ("{", "[") or "<" not in body:   # JSON / plain text: show as a table
                    body = text_html(page, body)
                self._write(folder, i, page, body, item["at"] or url)
                saved += 1
            if state.get("qm_logged_in"):
                try:                                # free the session (the modem keeps them in its small RAM)
                    session.post(qm + QM_PATH + "auth/logout.sh", timeout=self.timeout)
                except requests.RequestException:
                    pass
        for j, (page, body) in enumerate(extra or [], len(urls) + 1):
            self._write(folder, j, page, body, page)
            saved += 1
        if not saved:
            shutil.rmtree(folder, ignore_errors=True)
            name = None
        self._prune()
        return name, saved, errors

    def _qm_login(self, session, base, wan_key, password, errors):
        """Log in to QManager for this capture. Returns True when logged in, else adds the reason to errors.
        A password QManager refused isn't sent again (hourly retries would keep its lockout going)."""
        if password and self.qm_refused.get(wan_key) == password:
            errors.append("login: QManager refused this password before - not retried; fix QUECTEL_PASSWORD "
                          "and restart the watchdog")
            return False
        try:
            qmanager_login(session, base, password, self.timeout)
        except PermissionError as e:
            if "check QUECTEL_PASSWORD" in str(e):
                self.qm_refused[wan_key] = password
            errors.append(f"login: {e}")
            return False
        except requests.RequestException as e:
            errors.append(f"login: {e.__class__.__name__} - modem not reachable, skipped its pages")
            return False
        self.qm_refused.pop(wan_key, None)
        return True

    @staticmethod
    def _write(folder, index, page, body, source):
        """One saved page: NN-<page>.html (for the views) and NN-<page>.txt (plain text, first line = source)."""
        base = os.path.join(folder, f"{index:02d}-{page}")
        with open(base + ".html", "w", encoding="utf-8", errors="replace") as f:
            f.write(body)
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(f"{source}\n\n{html_to_text(body)}\n")

    def _prune(self):
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - self.keep_days * 86400))
        for old in self.list():
            if old[:8] < cutoff:
                shutil.rmtree(os.path.join(self.base_dir, old), ignore_errors=True)

    def list(self, wan_key=None, day=None):
        """Capture names, newest first; optionally only one WAN and/or one day (YYYYMMDD)."""
        names = (d for d in os.listdir(self.base_dir) if NAME_RE.match(d))
        if wan_key:
            names = (n for n in names if n.split("-", 2)[2] == re.sub(r"[^a-z0-9]", "", wan_key.lower()))
        if day:
            names = (n for n in names if n.startswith(day))
        return sorted(names, reverse=True)

    def _folder(self, name):
        if not NAME_RE.match(name or ""):
            return None
        folder = os.path.join(self.base_dir, name)
        return folder if os.path.isdir(folder) else None

    def meta(self, name):
        folder = self._folder(name)
        try:
            with open(os.path.join(folder, "meta.json")) as f:
                return json.load(f)
        except (OSError, TypeError, ValueError):
            return {}

    def pages(self, name):
        """[(page key like 'logs.ha', html path)] in capture order."""
        folder = self._folder(name)
        if not folder:
            return None
        return [(f[3:-5], os.path.join(folder, f)) for f in sorted(os.listdir(folder))
                if f.endswith(".html") and re.match(r"^[0-9]{2}-", f)]

    def raw(self, name, page):
        for key, path in self.pages(name) or []:
            if key == page:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
        return None

    def files(self, name):
        folder = self._folder(name)
        if not folder:
            return None
        out = []
        for f in sorted(os.listdir(folder)):
            if f.endswith(".txt"):
                with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                    out.append((f[:-4], fh.read()))
        return out
