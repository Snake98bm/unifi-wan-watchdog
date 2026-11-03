"""Turn saved modem pages into safe, box-looking HTML and structured tables, and merge them over time.

- clean_html(): keeps only headings and tables (with the box's own table/row/cell classes, so the
  AT&T "grid" look can be recreated by CSS); scripts, links, forms and every other attribute are dropped.
- tables(): every table as {"title", "rows": [[cell, ...], ...], "header": [...]}.
- merge_logs(): log-style tables (a Date/Time column) from many captures -> unique rows, newest first.
- history(): status-style tables -> one row per label with the value from each capture.
"""
import html
import re
from html.parser import HTMLParser

KEEP = {"table", "thead", "tbody", "tr", "th", "td", "caption", "h1", "h2", "h3", "br", "b", "strong"}
DROP_CONTENT = {"script", "style", "noscript", "select", "textarea", "button", "head", "title"}
CLASSES = {"grid", "table100", "table85", "table80", "table75", "table66", "table60", "table50",
           "a", "b", "special", "heading", "col2", "disabled"}
SKIP_IDS = {"secondary-navigation", "footer", "help", "navigation", "nav", "main-nav"}
TIME_HEADERS = ("date/time", "time", "date", "timestamp")


class _Parser(HTMLParser):
    """Builds a tiny tree of the kept elements inside the page's content area."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "kids": []}
        self.stack = [self.root]
        self.drop = 0
        self.skip_depth = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in ("br", "img", "input", "meta", "link", "hr"):
            self.depth += 1
            if self.skip_depth is None and (a.get("id") in SKIP_IDS or a.get("class") in SKIP_IDS):
                self.skip_depth = self.depth
        if tag in DROP_CONTENT:
            self.drop += 1
        if self.drop or self.skip_depth is not None or tag not in KEEP:
            return
        keep = {}
        cls = " ".join(c for c in (a.get("class") or "").split() if c in CLASSES)
        if cls:
            keep["class"] = cls
        for k in ("colspan", "rowspan"):
            if (a.get(k) or "").isdigit():
                keep[k] = a[k]
        node = {"tag": tag, "attrs": keep, "kids": []}
        self.stack[-1]["kids"].append(node)
        if tag != "br":
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT and self.drop:
            self.drop -= 1
        if tag not in ("br", "img", "input", "meta", "link", "hr"):
            if self.skip_depth is not None and self.depth == self.skip_depth:
                self.skip_depth = None
            self.depth = max(0, self.depth - 1)
        if tag in KEEP and tag != "br":
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i]["tag"] == tag:
                    del self.stack[i:]
                    break

    def handle_data(self, data):
        if not self.drop and self.skip_depth is None and len(self.stack) > 1:
            self.stack[-1]["kids"].append(data)


def _parse(raw):
    p = _Parser()
    p.feed(raw)
    p.close()
    return p.root


def _text(node):
    if isinstance(node, str):
        return node
    if node["tag"] == "br":
        return " "
    return "".join(_text(k) for k in node["kids"])


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def _render(node):
    if isinstance(node, str):
        return html.escape(node)
    attrs = "".join(f' {k}="{html.escape(v)}"' for k, v in node["attrs"].items())
    if node["tag"] == "br":
        return "<br>"
    return f"<{node['tag']}{attrs}>{''.join(_render(k) for k in node['kids'])}</{node['tag']}>"


def _top_blocks(root):
    """Headings and outermost tables in document order (text outside them is dropped)."""
    out = []

    def walk(n):
        for k in n["kids"]:
            if isinstance(k, str):
                continue
            if k["tag"] in ("table", "h1", "h2", "h3"):
                out.append(k)
            else:
                walk(k)
    walk(root)
    return out


def clean_html(raw):
    blocks = [b for b in _top_blocks(_parse(raw)) if _clean(_text(b))]
    return "\n".join(_render(b) for b in blocks)


def _rows(table):
    rows = []

    def walk(n):
        for k in n["kids"]:
            if isinstance(k, str):
                continue
            if k["tag"] == "tr":
                cells = [c for c in k["kids"] if not isinstance(c, str) and c["tag"] in ("th", "td")]
                rows.append([(_clean(_text(c)), c["tag"]) for c in cells])
            elif k["tag"] != "table":      # nested tables stay with their own block
                walk(k)
    walk(table)
    return rows


def tables(raw):
    out, title = [], ""
    for b in _top_blocks(_parse(raw)):
        if b["tag"] != "table":
            title = _clean(_text(b)) or title
            continue
        rows = [r for r in _rows(b) if any(t for t, _ in r)]
        if not rows:
            continue
        header = [t for t, _ in rows[0]] if all(tag == "th" for _, tag in rows[0]) and len(rows[0]) > 1 else None
        body = rows[1:] if header else rows
        out.append({"title": title, "header": header, "rows": [[t for t, _ in r] for r in body]})
    return out


def _time_col(header):
    if not header:
        return None
    for i, h in enumerate(header):
        if h.lower() in TIME_HEADERS:
            return i
    return None


def merge_logs(pages):
    """pages: [(capture name, raw html)] for one page type. Returns (header, rows) or None if not a log page."""
    header, seen, rows = None, set(), []
    for _, raw in pages:
        for t in tables(raw):
            col = _time_col(t["header"])
            if col is None:
                continue
            header = header or t["header"]
            skip_no = t["header"][0].lower().rstrip(".") in ("no", "#", "index")
            for r in t["rows"]:
                key = tuple(r[1:] if skip_no else r)
                if key in seen or len(r) <= col:
                    continue
                seen.add(key)
                rows.append(r)
    if header is None:
        return None
    col = _time_col(header)
    if header[0].lower().rstrip(".") in ("no", "#", "index"):
        header = header[1:]
        rows = [r[1:] for r in rows]
        col -= 1
    rows.sort(key=lambda r: r[col], reverse=True)
    return header, rows


def history(pages):
    """Status pages over time: [{"title", "labels": [...], "rows": {label: [value per capture]}}], columns."""
    columns = [name for name, _ in pages]
    sections = {}
    order = []
    for i, (_, raw) in enumerate(pages):
        for t in tables(raw):
            if _time_col(t["header"]) is not None:
                continue
            title = t["title"] or "Status"
            sub = [h for h in (t["header"] or [])[1:]]
            sec = sections.get(title)
            if sec is None:
                sec = sections[title] = {"title": title, "labels": [], "rows": {}}
                order.append(title)
            for r in t["rows"]:
                if len(r) < 2 or not r[0]:
                    continue
                values = r[1:]
                for j, v in enumerate(values):
                    label = r[0] + (f" - {sub[j]}" if len(values) > 1 and j < len(sub) and sub[j] else
                                    (f" ({j + 1})" if len(values) > 1 else ""))
                    if label not in sec["rows"]:
                        sec["rows"][label] = [""] * len(pages)
                        sec["labels"].append(label)
                    sec["rows"][label][i] = v
    return [sections[t] for t in order], columns
