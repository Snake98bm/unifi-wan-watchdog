"""Small UniFi Network client using an API key (X-API-KEY header).

Status comes from the classic endpoint stat/device (the gateway's wan1/wan2 blocks and its
port_table). Power cycles use the official Integration API, falling back to the classic
devmgr command if that fails.
"""
import ipaddress

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
    "100.64.0.0/10",                                   # carrier-grade NAT
    "169.254.0.0/16",                                  # link-local (no DHCP answer)
)]


def is_private(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in PRIVATE_NETS)


class UniFiError(Exception):
    pass


class UniFi:
    def __init__(self, url, api_key, site="default", verify=False, timeout=10):
        self.url = url.rstrip("/")
        self.site = site
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})
        self._site_id = None
        self._device_ids = {}

    def _request(self, method, path, **kwargs):
        try:
            r = self.session.request(method, self.url + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise UniFiError(f"{method} {path}: {e.__class__.__name__}: {e}") from e
        if r.status_code >= 400:
            raise UniFiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json() if r.content else {}
        except ValueError as e:
            raise UniFiError(f"{method} {path}: response is not JSON") from e

    # ---- status (classic API) ----
    def gateway(self):
        data = self._request("GET", f"/proxy/network/api/s/{self.site}/stat/device").get("data", [])
        for dev in data:
            if dev.get("type") in ("udm", "ugw", "uxg"):
                return dev
        raise UniFiError("no gateway found in stat/device")

    def wans(self):
        """What UniFi has for each WAN: {"wan1": {"name", "port", "ifname"}, "wan2": {...}}.
        Names come from the WAN networks (WAN -> wan1, WAN2 -> wan2), ports from the gateway."""
        gw = self.gateway()
        nets = self._request("GET", f"/proxy/network/api/s/{self.site}/rest/networkconf").get("data", [])
        names = {n.get("wan_networkgroup"): n.get("name") for n in nets if n.get("purpose") == "wan"}
        out = {}
        for key, group in (("wan1", "WAN"), ("wan2", "WAN2")):
            info = gw.get(key) or {}
            if info.get("port_idx") or names.get(group):
                out[key] = {"name": names.get(group), "port": info.get("port_idx"), "ifname": info.get("ifname")}
        return out

    # ---- actions (Integration API, classic fallback) ----
    def site_id(self):
        if not self._site_id:
            sites = self._request("GET", "/proxy/network/integration/v1/sites").get("data", [])
            match = [s for s in sites if s.get("internalReference") == self.site] or sites
            if not match:
                raise UniFiError("Integration API returned no sites")
            self._site_id = match[0]["id"]
        return self._site_id

    def device_id(self, mac):
        mac = mac.lower()
        if mac not in self._device_ids:
            devices = self._request(
                "GET", f"/proxy/network/integration/v1/sites/{self.site_id()}/devices?limit=200").get("data", [])
            for dev in devices:
                if str(dev.get("macAddress", "")).lower() == mac:
                    self._device_ids[mac] = dev["id"]
                    break
            else:
                raise UniFiError(f"gateway {mac} not found in Integration API device list")
        return self._device_ids[mac]

    def power_cycle(self, mac, port_idx):
        """PoE power-cycle one gateway port. Returns which API did it."""
        errors = []
        try:
            path = (f"/proxy/network/integration/v1/sites/{self.site_id()}/devices/"
                    f"{self.device_id(mac)}/interfaces/ports/{port_idx}/actions")
            self._request("POST", path, json={"action": "POWER_CYCLE"})
            return "Integration API"
        except UniFiError as e:
            errors.append(f"Integration API: {e}")
        try:
            self._request("POST", f"/proxy/network/api/s/{self.site}/cmd/devmgr",
                          json={"cmd": "power-cycle", "mac": mac, "port_idx": int(port_idx)})
            return "classic API"
        except UniFiError as e:
            errors.append(f"classic API: {e}")
        raise UniFiError("; ".join(errors))

    def diagnostics(self, ports):
        """Raw facts for the Diagnostics page (no secrets)."""
        out = {}
        try:
            gw = self.gateway()
            out["gateway"] = {k: gw.get(k) for k in ("name", "model", "mac", "version", "state")}
            out["wan1"] = gw.get("wan1")
            out["wan2"] = gw.get("wan2")
            out["ports"] = [p for p in gw.get("port_table", []) if p.get("port_idx") in ports]
        except UniFiError as e:
            out["classic_api_error"] = str(e)
            return out
        try:
            out["integration_site_id"] = self.site_id()
            out["integration_device_id"] = self.device_id(gw["mac"])
        except UniFiError as e:
            out["integration_api_error"] = str(e)
        return out
