"""Bonded-DSL health checks from the AT&T/Arris box's saved Broadband Status pages.

A sample is {"ts": save time, "boot": when the box last booted (ts - its uptime) or None, "lines": [...]} with the
lines from pages.dsl_lines(). checks() looks at the samples (oldest first, the newest = now) and returns
{key: message} for everything that is wrong right now:

- rates since the reboot: total sync (all lines that are up, added up; the box re-profiles the pairs, so one
  pair alone can sync at what two did) and each line's max attainable, each against the first save after the
  box's latest boot. Down more than drop_pct % -> flagged.
- after a reboot: a line whose max attainable downstream/upstream in that first save is under the minimum.
- errors: a line's FEC or CRC errors in the last hour (the counters run since the line trained, so this is the
  rise between two saves) more than error_factor times its hourly average over the last 7 days, and at least
  ERROR_FLOOR an hour so a near-zero average doesn't flag a handful.
"""
import time

WEEK = 7 * 86400
SAME_BOOT = 300                 # boot times this close are the same boot (uptime and save time are rounded)
WINDOW_MIN = 45 * 60            # the "last hour" is from the newest save at least this much older
PAIR_MAX = 3 * 3600             # saves further apart don't make an error rate (a gap in the saves)
BASELINE_MIN = 86400            # need a day of error history before calling anything "above normal"
ERROR_FLOOR = {"fec": 1000, "crc": 10}   # per hour


def _up(line):
    """The line is in sync ("Up")."""
    return bool(line) and (line.get("state") or "").strip().lower() == "up"


def _line(sample, name):
    return next((ln for ln in sample["lines"] if ln["line"] == name), None)


def _mbps(kbps):
    return f"{(kbps or 0) / 1000:.1f}"


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def _total(sample, key):
    return sum(ln.get(key) or 0 for ln in sample["lines"] if _up(ln))


def _same_boot(a, b):
    return a.get("boot") is not None and b.get("boot") is not None and abs(a["boot"] - b["boot"]) <= SAME_BOOT


def rate_checks(samples, label, drop_pct=10, min_down=60000, min_up=10000):
    out = {}
    cur = samples[-1]
    if cur.get("boot") is None:
        return out
    boot = [s for s in samples if _same_boot(s, cur)]
    first = next((s for s in boot if any(_up(ln) for ln in s["lines"])), None)
    keep = 1 - drop_pct / 100
    if first is not None and first is not cur and any(_up(ln) for ln in cur["lines"]):
        for key, name in (("down", "downstream"), ("up", "upstream")):
            was, now = _total(first, key), _total(cur, key)
            if was and now < was * keep:
                out[f"sync_{key}"] = (f"{label} DSL {name} sync down {100 - now * 100 / was:.0f}% since the reboot: "
                                      f"{_mbps(now)} Mbps now, {_mbps(was)} Mbps after the reboot ({_when(first['ts'])})")
    for ln in cur["lines"]:
        name = ln["line"]
        base = next((s for s in boot if _up(_line(s, name))), None)
        if base is None:
            continue
        b = _line(base, name)
        if (min_down and b.get("ds_max") is not None and b["ds_max"] < min_down) or \
                (min_up and b.get("us_max") is not None and b["us_max"] < min_up):
            out[f"floor_{name}"] = (f"{label} DSL {name} max attainable after the reboot ({_when(base['ts'])}) is only "
                                    f"{_mbps(b.get('ds_max'))}/{_mbps(b.get('us_max'))} Mbps "
                                    f"(want at least {_mbps(min_down)}/{_mbps(min_up)})")
        if base is cur or not _up(ln):
            continue
        for key, word in (("ds_max", "downstream"), ("us_max", "upstream")):
            was, now = b.get(key), ln.get(key)
            if was and now is not None and now < was * keep:
                out[f"att_{name}_{key}"] = (f"{label} DSL {name} max attainable {word} down {100 - now * 100 / was:.0f}% "
                                            f"since the reboot: {_mbps(now)} Mbps now, {_mbps(was)} Mbps after it")
    return out


def _errors(a, b, name, kind):
    """Errors on a line between two saves, or None when they can't be compared (line down, retrained, gap)."""
    la, lb = _line(a, name), _line(b, name)
    if not (_up(la) and _up(lb)) or not la.get(kind) or not lb.get(kind):
        return None
    if a.get("boot") is not None and b.get("boot") is not None and not _same_boot(a, b):
        return None
    if None in la[kind] or None in lb[kind] or not 0 < b["ts"] - a["ts"] <= PAIR_MAX:
        return None
    d = sum(lb[kind]) - sum(la[kind])
    return d if d >= 0 else None              # counters went back: the line retrained


def error_checks(samples, label, factor=3):
    out = {}
    cur = samples[-1]
    prev = next((s for s in reversed(samples[:-1]) if cur["ts"] - s["ts"] >= WINDOW_MIN), None)
    if prev is None:
        return out
    week = [s for s in samples if s["ts"] >= cur["ts"] - WEEK and s["ts"] <= prev["ts"]]
    for ln in cur["lines"]:
        name = ln["line"]
        for kind in ("fec", "crc"):
            now = _errors(prev, cur, name, kind)
            if now is None:
                continue
            per_hour = now * 3600 / (cur["ts"] - prev["ts"])
            total = span = 0
            for a, b in zip(week, week[1:]):
                d = _errors(a, b, name, kind)
                if d is not None:
                    total, span = total + d, span + (b["ts"] - a["ts"])
            if span < BASELINE_MIN:
                continue
            avg = total * 3600 / span
            if per_hour >= ERROR_FLOOR[kind] and per_hour > factor * avg:
                out[f"{kind}_{name}"] = (f"{label} DSL {name} {kind.upper()} errors high: {per_hour:.0f}/hour in the "
                                         f"last hour, {avg:.0f}/hour on average over the last 7 days")
    return out


def checks(samples, label, drop_pct=10, min_down=60000, min_up=10000, error_factor=3):
    """{key: message} for what is wrong now (samples oldest first, the newest = now)."""
    if not samples:
        return {}
    out = rate_checks(samples, label, drop_pct, min_down, min_up)
    out.update(error_checks(samples, label, error_factor))
    return out
