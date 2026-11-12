"""Quectel AT answers (RM520N-GL) -> labelled tables, so the Modem logs pages can show and compare them.

at_response(): pulls the AT text out of whatever the modem's web endpoint returned (QManager's JSON, plain text
or HTML).
at_html(): one <h2> + "grid" table per command: Field | Value. Known commands get real field names
(QENG servingcell for SA / LTE / NSA, QCAINFO, QTEMP, QMAP, CSQ, COPS, QNWINFO, ...); anything else is listed as
value 1, value 2, ...
"""
import csv
import html
import json
import re
import time

QENG_SA = ["state", "tech", "duplex", "MCC", "MNC", "cell ID", "PCI", "TAC", "ARFCN", "band", "DL bandwidth",
           "RSRP", "RSRQ", "SINR", "SCS", "srxlev"]
QENG_LTE = ["state", "tech", "duplex", "MCC", "MNC", "cell ID", "PCI", "EARFCN", "band", "UL bandwidth",
            "DL bandwidth", "TAC", "RSRP", "RSRQ", "RSSI", "SINR", "CQI", "TX power", "srxlev"]
NSA_LTE = ["duplex", "MCC", "MNC", "cell ID", "PCI", "EARFCN", "band", "UL bandwidth", "DL bandwidth", "TAC",
           "RSRP", "RSRQ", "RSSI", "SINR", "CQI", "TX power", "srxlev"]
NSA_NR = ["MCC", "MNC", "PCI", "RSRP", "SINR", "RSRQ", "ARFCN", "band", "DL bandwidth", "SCS"]
# +QCAINFO rows (QManager docs/reference/carrier-aggregation.md). LTE: bandwidth in resource blocks, SINR in dB.
# NR has no RSSI and reports SNR x100; its short form has no state, the long form adds the uplink config.
QCAINFO_LTE = ["channel", "bandwidth", "band", "state", "PCI", "RSRP", "RSRQ", "RSSI", "SINR"]
QCAINFO_NR = ["channel", "bandwidth", "band", "PCI", "RSRP", "RSRQ", "SINR"]
QCAINFO_NR_LONG = ["channel", "bandwidth", "band", "state", "PCI", "UL config", "UL bandwidth", "UL channel",
                   "RSRP", "RSRQ", "SINR"]
LTE_RB_MHZ = {"6": "1.4", "15": "3", "25": "5", "50": "10", "75": "15", "100": "20"}
NR_BW_MHZ = {"0": 5, "1": 10, "2": 15, "3": 20, "4": 25, "5": 30, "6": 40, "7": 50, "8": 60, "9": 70, "10": 80,
             "11": 90, "12": 100, "13": 200, "14": 400, "15": 35, "16": 45}
LABELS = {"+CSQ": ["RSSI (0-31)", "BER"], "+COPS": ["mode", "format", "operator", "access tech"],
          "+CBC": ["charge state (unused)", "battery % (fake, from the voltage)", "supply voltage (mV)"],
          "+QNWINFO": ["access tech", "operator", "band", "channel"], "+CFUN": ["radio (1 = on)"],
          "+CPIN": ["SIM"], "+QUIMSLOT": ["SIM slot"], "+CEER": ["last disconnect reason"]}
REG = ["URC mode", "status", "TAC", "cell ID", "access tech", "cause type", "reject cause"]
REG_STATUS = {"0": "not registered", "1": "registered (home)", "2": "searching", "3": "registration DENIED",
              "4": "unknown", "5": "registered (roaming)"}
CONTRDP = ["bearer", "APN", "address/mask", "gateway", "DNS 1", "DNS 2"]


def _json(body):
    s = (body or "").strip()
    if s[:1] not in ("{", "["):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def at_error(body):
    """QManager's refusal of an AT command ({"success": false, "error": "..."}) as text, else None."""
    data = _json(body)
    if isinstance(data, dict) and data.get("success") is False:
        return str(data.get("error") or data.get("detail") or "an error")
    return None


def at_response(body):
    """The AT answer as text lines (command echo and the final OK removed)."""
    text = body or ""
    s = text.strip()
    data = _json(s)
    if isinstance(data, dict) and isinstance(data.get("response"), str):     # QManager send_command.sh
        text = data["response"]
    elif s.startswith("{") or s.startswith("["):
        try:
            data = json.loads(s)
            vals = []

            def walk(v):
                if isinstance(v, dict):
                    for x in v.values():
                        walk(x)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
                elif isinstance(v, str):
                    vals.append(v)
            walk(data)
            text = "\n".join(v for v in vals if "+" in v or "OK" in v or "ERROR" in v) or "\n".join(vals)
        except ValueError:
            pass
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text)).replace("\r", "")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return [ln for ln in lines if ln.upper() != "OK" and not re.match(r"^AT[+&]", ln, re.I)]


def _split(rest):
    return [v.strip() for v in next(csv.reader([rest], skipinitialspace=True))]


def qcainfo_fields(vals):
    """One +QCAINFO row without its PCC/SCC tag -> [(field, value)], with the bandwidth in MHz and the NR SNR in
    dB. LTE and NR rows have different columns (NR has no RSSI, no state in its short form)."""
    band = vals[2] if len(vals) > 2 else ""
    nr = band.upper().startswith(("NR5G", "NRDC"))
    names = (QCAINFO_NR_LONG if len(vals) >= 8 else QCAINFO_NR) if nr else QCAINFO_LTE
    out = []
    for n, v in zip(names, vals):
        if n == "bandwidth":
            mhz = NR_BW_MHZ.get(v) if nr else LTE_RB_MHZ.get(v)
            out.append(("bandwidth (MHz)", str(mhz) if mhz is not None else v))
        elif n == "SINR" and nr:
            try:
                f = int(v)
                v = "" if f == -32768 else f"{f / 100:.1f}"
            except ValueError:
                pass
            out.append(("SINR", v))
        else:
            out.append((n, v))
    return out


QCAINFO_ROW_RE = re.compile(r"^(PCC|SCC(?: \d+)?) (.+)$")


def fix_old_qcainfo(page_html):
    """QCAINFO pages saved before v3.2 put NR rows in the LTE columns (5G RSRQ showing 2246). Rebuild the rows
    from the values in their saved order; pages saved since are left alone."""
    if "AT+QCAINFO" not in (page_html or "") or "bandwidth (MHz)" in page_html:
        return page_html
    cells = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", page_html, re.S)
    groups, order = {}, []
    for k, v in cells:
        m = QCAINFO_ROW_RE.match(html.unescape(k).strip())
        if not m:
            continue
        tag = m.group(1)
        if tag not in groups:
            groups[tag] = []
            order.append(tag)
        groups[tag].append(html.unescape(v).strip())
    if not order:
        return page_html
    lines = ["+QCAINFO: " + ",".join([t.split()[0]] + [f'"{v}"' if " " in v else v for v in groups[t]])
             for t in order]
    return at_html("AT+QCAINFO", lines)


def _rows(cmd, lines):
    rows = []
    qcai_n = {}
    for ln in lines:
        m = re.match(r"^(\+[A-Z0-9]+):\s*(.*)$", ln, re.I)
        if not m:
            rows.append(("firmware" if cmd.upper() == "AT+QGMR" else "answer", ln))
            continue
        prefix, vals = m.group(1).upper(), _split(m.group(2))
        if prefix == "+QENG":
            if vals and vals[0] == "servingcell":
                names = QENG_SA if len(vals) > 2 and vals[2] == "NR5G-SA" else QENG_LTE
                rows += list(zip(names, vals[1:]))
            elif vals and vals[0] == "LTE":
                rows += [(f"LTE {n}", v) for n, v in zip(NSA_LTE, vals[1:])]
            elif vals and vals[0].startswith("NR5G"):
                rows += [(f"NR {n}", v) for n, v in zip(NSA_NR, vals[1:])]
            else:
                rows += [(f"value {i}", v) for i, v in enumerate(vals, 1)]
        elif prefix == "+QCAINFO" and vals:
            kind = vals[0]                                  # PCC / SCC
            qcai_n[kind] = qcai_n.get(kind, 0) + 1
            tag = kind if kind == "PCC" else f"{kind} {qcai_n[kind]}"
            rows += [(f"{tag} {n}", v) for n, v in qcainfo_fields(vals[1:])]
        elif prefix == "+QTEMP" and len(vals) >= 2:
            rows.append((vals[0], vals[1]))
        elif prefix == "+QMAP" and vals and vals[0].upper() == "WWAN":
            # +QMAP: "WWAN",<connected>,<profile>,"IPV4"|"IPV6",<address>
            ipver = vals[3] if len(vals) > 3 else ""
            rows += [(f"WWAN {ipver} connected", vals[1] if len(vals) > 1 else ""),
                     (f"WWAN {ipver} address", vals[4] if len(vals) > 4 else "")]
        elif prefix in ("+CREG", "+CEREG", "+C5GREG"):
            tech = {"+CREG": "", "+CEREG": "LTE ", "+C5GREG": "5G "}[prefix]
            for i, v in enumerate(vals):
                rows.append((tech + (REG[i] if i < len(REG) else f"value {i + 1}"), v))
                if i == 1:
                    rows.append((tech + "status meaning", REG_STATUS.get(v, "?")))
        elif prefix == "+CGACT" and len(vals) >= 2:
            rows.append((f"context {vals[0]} active", vals[1]))
        elif prefix == "+CGCONTRDP" and vals:
            rows += [(f"context {vals[0]} {n}", v) for n, v in zip(CONTRDP, vals[1:])]
        elif prefix in LABELS:
            names = LABELS[prefix]
            rows += [(names[i] if i < len(names) else f"value {i + 1}", v) for i, v in enumerate(vals)]
        else:
            rows += [((prefix[1:] if len(vals) == 1 else f"{prefix[1:]} {i}"), v) for i, v in enumerate(vals, 1)]
    return rows


def _unique(rows):
    """Repeated labels (e.g. QNWINFO's LTE and NR lines) get #2, #3 so no value hides another."""
    seen, out = {}, []
    for k, v in rows:
        seen[k] = seen.get(k, 0) + 1
        out.append((k if seen[k] == 1 else f"{k} #{seen[k]}", v))
    return out


def _grid_html(title, headers, rows, classes=(), empty=None):
    """<h2> + the AT&T-style "grid" table with alternating a/b rows; classes = optional class per column."""
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    out = [f"<h2>{html.escape(title)}</h2>", f'<table class="grid"><tr>{head}</tr>']
    for i, row in enumerate(rows):
        tds = "".join((f'<td class="{classes[j]}">' if j < len(classes) and classes[j] else "<td>")
                      + html.escape(str(v)) + "</td>" for j, v in enumerate(row))
        out.append(f'<tr class="{"ab"[i % 2]}">{tds}</tr>')
    out.append("</table>")
    if not rows and empty:
        out.append(f"<h3>{html.escape(empty)}</h3>")
    return "\n".join(out)


TIME_KEYS = ("timestamp", "time", "ts", "date")


def _when(v):
    """Epoch seconds -> 'YYYY-MM-DD HH:MM:SS' local time (sorts as text, like the other log tables)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f > 1e12:                 # milliseconds
        f /= 1000
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f)) if f > 1e9 else str(v)


def _is_records(v):
    return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)


def _flat(data, prefix=""):
    """Nested dict -> [(dotted key, value)]; lists of records are left out (they get their own table)."""
    rows = []
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows += _flat(v, key + ".") or [(key, "")]
        elif _is_records(v):
            continue
        elif isinstance(v, list):
            rows.append((key, ", ".join(json.dumps(x) if isinstance(x, (dict, list)) else str(x) for x in v)))
        else:
            rows.append((key, "" if v is None else str(v)))
    return rows


def _records_html(title, records):
    """A list of JSON objects (QManager events / log entries) as one table, its time column first and named
    Time, so the 30-day view merges them into one log."""
    keys = []
    for r in records:
        keys += [k for k in r if k not in keys]
    tkey = next((k for k in keys if k.lower() in TIME_KEYS), None)
    if tkey:
        keys = [tkey] + [k for k in keys if k != tkey]
    header = ["Time" if k == tkey else k for k in keys]
    rows = []
    for r in records:
        rows.append([_when(r.get(k)) if k == tkey else
                     (json.dumps(r[k]) if isinstance(r.get(k), (dict, list)) else str(r.get(k, "") or ""))
                     for k in keys])
    if tkey:
        rows.sort(key=lambda row: row[0], reverse=True)
    return _grid_html(title, header, rows, tuple("left" if k in ("message", "detail") else "" for k in keys))


def _json_html(title, data):
    if _is_records(data):
        return _records_html(title, data)
    if isinstance(data, dict):
        parts = []
        rows = _flat(data)
        if rows or not data:
            parts.append(_grid_html(title, ("Field", "Value"), _unique(rows or [("status", "empty (not configured)")]),
                                    ("heading",)))
        parts += [_records_html(f"{title} - {k}", v) for k, v in data.items() if _is_records(v)]
        return "\n".join(parts)
    return _grid_html(title, ("Field", "Value"), [("answer", json.dumps(data))], ("heading",))


def text_html(title, body):
    """A plain-text or JSON answer (QManager's status/events/log endpoints, the toolkit's get_uptime) as tables:
    nested fields flattened to Field | Value, lists of records (events, log entries) as their own table."""
    s = (body or "").strip()
    data = _json(s)
    if data is not None:
        return _json_html(title, data)
    m = re.search(r"up\s+(.+?),\s+(?:\d+\s+users?,\s+)?load average:\s*(.+)$", s)
    if m:     # uptime: "17:21:14 up  2:11,  load average: 2.73, 2.60, 2.53"
        rows = [("modem time", s.split()[0]), ("uptime", re.sub(r"\s+", " ", m.group(1)).strip(", ")),
                ("load average", m.group(2).strip())]
    else:
        rows = [("answer", ln.strip()) for ln in s.splitlines() if ln.strip()]
    return _grid_html(title, ("Field", "Value"), _unique(rows), ("heading",))


LOG_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+(?:[+-]\d\d:?\d\d|Z)?)\s+\S+\s+([^:\[]+)(?:\[\d+\])?:\s*(.*)$")


def gateway_log_html(title, text):
    """Dream Machine syslog lines ('2026-11-03T03:00:26-08:00 host proc[pid]: msg') as a Time | Process | Message
    table, so the 30-day view merges them into one WAN event log."""
    rows = []
    for ln in (text or "").splitlines():
        m = LOG_LINE_RE.match(ln.strip())
        if m:
            rows.append((m.group(1)[:19].replace("T", " "), m.group(2).strip(), m.group(3)))
    return _grid_html(title, ("Time", "Process", "Message"), rows, ("", "", "left"), empty="no matching log lines")


def at_html(cmd, lines):
    return _grid_html(cmd, ("Field", "Value"), _unique(_rows(cmd, lines)), ("heading",))
