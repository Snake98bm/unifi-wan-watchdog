"""Quectel AT answers (RM520N-GL) -> labelled tables, so the Modem logs pages can show and compare them.

at_response(): pulls the AT text out of whatever the toolkit's web endpoint returned (plain text, HTML or JSON).
at_html(): one <h2> + "grid" table per command: Field | Value. Known commands get real field names
(QENG servingcell for SA / LTE / NSA, QCAINFO, QTEMP, QMAP, CSQ, COPS, QNWINFO, ...); anything else is listed as
value 1, value 2, ...
"""
import csv
import html
import json
import re

QENG_SA = ["state", "tech", "duplex", "MCC", "MNC", "cell ID", "PCI", "TAC", "ARFCN", "band", "DL bandwidth",
           "RSRP", "RSRQ", "SINR", "SCS", "srxlev"]
QENG_LTE = ["state", "tech", "duplex", "MCC", "MNC", "cell ID", "PCI", "EARFCN", "band", "UL bandwidth",
            "DL bandwidth", "TAC", "RSRP", "RSRQ", "RSSI", "SINR", "CQI", "TX power", "srxlev"]
NSA_LTE = ["duplex", "MCC", "MNC", "cell ID", "PCI", "EARFCN", "band", "UL bandwidth", "DL bandwidth", "TAC",
           "RSRP", "RSRQ", "RSSI", "SINR", "CQI", "TX power", "srxlev"]
NSA_NR = ["MCC", "MNC", "PCI", "RSRP", "SINR", "RSRQ", "ARFCN", "band", "DL bandwidth", "SCS"]
QCAINFO = ["channel", "bandwidth", "band", "state", "PCI", "RSRP", "RSRQ", "RSSI", "SINR"]
LABELS = {"+CSQ": ["RSSI (0-31)", "BER"], "+COPS": ["mode", "format", "operator", "access tech"],
          "+QNWINFO": ["access tech", "operator", "band", "channel"], "+CFUN": ["radio (1 = on)"],
          "+CPIN": ["SIM"], "+QUIMSLOT": ["SIM slot"], "+CEER": ["last disconnect reason"]}
REG = ["URC mode", "status", "TAC", "cell ID", "access tech", "cause type", "reject cause"]
REG_STATUS = {"0": "not registered", "1": "registered (home)", "2": "searching", "3": "registration DENIED",
              "4": "unknown", "5": "registered (roaming)"}
CONTRDP = ["bearer", "APN", "address/mask", "gateway", "DNS 1", "DNS 2"]


def at_response(body):
    """The AT answer as text lines (command echo and the final OK removed)."""
    text = body or ""
    s = text.strip()
    if s.startswith("{") or s.startswith("["):
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
            rows += [(f"{tag} {n}", v) for n, v in zip(QCAINFO, vals[1:])]
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


def text_html(title, body):
    """A plain-text or JSON answer (toolkit endpoints like get_uptime, get_watchcat_status) as a Field | Value table."""
    s = (body or "").strip()
    rows = []
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            rows = [(str(k), json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in data.items()]
            rows = rows or [("status", "empty (not configured)")]
        else:
            rows = [("answer", json.dumps(data))]
    except ValueError:
        m = re.search(r"up\s+(.+?),\s+(?:\d+\s+users?,\s+)?load average:\s*(.+)$", s)
        if m:     # uptime: "17:21:14 up  2:11,  load average: 2.73, 2.60, 2.53"
            rows = [("modem time", s.split()[0]), ("uptime", re.sub(r"\s+", " ", m.group(1)).strip(", ")),
                    ("load average", m.group(2).strip())]
        else:
            rows = [("answer", ln.strip()) for ln in s.splitlines() if ln.strip()]
    out = [f"<h2>{html.escape(title)}</h2>", '<table class="grid"><tr><th>Field</th><th>Value</th></tr>']
    for i, (k, v) in enumerate(_unique(rows)):
        out.append(f'<tr class="{"ab"[i % 2]}"><td class="heading">{html.escape(k)}</td><td>{html.escape(v)}</td></tr>')
    out.append("</table>")
    return "\n".join(out)


LOG_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+(?:[+-]\d\d:?\d\d|Z)?)\s+\S+\s+([^:\[]+)(?:\[\d+\])?:\s*(.*)$")


def gateway_log_html(title, text):
    """Dream Machine syslog lines ('2026-11-03T03:00:26-08:00 host proc[pid]: msg') as a Time | Process | Message
    table, so the 30-day view merges them into one WAN event log."""
    out = [f"<h2>{html.escape(title)}</h2>", '<table class="grid"><tr><th>Time</th><th>Process</th><th>Message</th></tr>']
    n = 0
    for ln in (text or "").splitlines():
        m = LOG_LINE_RE.match(ln.strip())
        if not m:
            continue
        t, proc, msg = m.group(1)[:19].replace("T", " "), m.group(2).strip(), m.group(3)
        out.append(f'<tr class="{"ab"[n % 2]}"><td>{html.escape(t)}</td><td>{html.escape(proc)}</td>'
                   f'<td class="left">{html.escape(msg)}</td></tr>')
        n += 1
    out.append("</table>")
    if not n:
        out.append("<h3>no matching log lines</h3>")
    return "\n".join(out)


def at_html(cmd, lines):
    rows = _unique(_rows(cmd, lines))
    out = [f"<h2>{html.escape(cmd)}</h2>", '<table class="grid"><tr><th>Field</th><th>Value</th></tr>']
    for i, (k, v) in enumerate(rows):
        out.append(f'<tr class="{"ab"[i % 2]}"><td class="heading">{html.escape(k)}</td><td>{html.escape(v)}</td></tr>')
    out.append("</table>")
    return "\n".join(out)
