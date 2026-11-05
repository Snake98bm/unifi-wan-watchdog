"""Tests for modem page cleaning/merging and the capture schedule. Run: python tests/test_pages.py
Set REAL_PAGES_DIR to a folder of real saved *.ha pages to also smoke-test them."""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import pages  # noqa: E402

LOG_PAGE = """<html><head><script>alert(1)</script><title>Logs</title></head><body>
<div id="secondary-navigation"><a href="/cgi-bin/home.ha">Device</a></div>
<div id="content"><h2>Logs</h2>
<table class="grid table100"><tr><th>No.</th><th>Date/Time</th><th>SourceIP</th><th>Reason</th></tr>
<tr class="a"><td>1</td><td>2026-11-03T07:30:43</td><td>198.51.100.20</td><td>Invalid IP Packet</td></tr>
<tr class="b"><td>2</td><td>2026-11-03T07:30:44</td><td>198.51.100.20</td><td onclick="evil()">Invalid IP Packet</td></tr>
</table></div><div id="footer">footer</div></body></html>"""

LOG_PAGE_LATER = LOG_PAGE.replace("<td>1</td><td>2026-11-03T07:30:43", "<td>1</td><td>2026-11-03T08:00:00")

STATUS_PAGE = """<div id="content"><h2>DSL Status</h2>
<table class="grid table100"><tr><th></th><th scope="col">Line 1</th><th scope="col">Line 2</th></tr>
<tr><th scope="row">Line State</th><td class="col2">Up</td><td class="col2">Up</td></tr>
<tr><th scope="row">Downstream Sync Rate (kbps)</th><td>30014</td><td>30004</td></tr></table>
<h2>Primary Broadband</h2><table class="table75"><tr><th scope="row">Broadband Connection</th><td>Up</td></tr></table></div>"""


class PageTests(unittest.TestCase):
    def test_clean_keeps_tables_and_drops_scripts_handlers_nav(self):
        out = pages.clean_html(LOG_PAGE)
        self.assertIn('<table class="grid table100">', out)
        self.assertIn('<tr class="a">', out)
        for bad in ("<script", "alert", "onclick", "evil", "footer", "Device"):
            self.assertNotIn(bad, out)

    def test_tables_titles_and_rows(self):
        t = pages.tables(STATUS_PAGE)
        self.assertEqual([x["title"] for x in t], ["DSL Status", "Primary Broadband"])
        self.assertEqual(t[0]["header"], ["", "Line 1", "Line 2"])
        self.assertEqual(t[0]["rows"][0], ["Line State", "Up", "Up"])

    def test_merge_logs_dedupes_and_sorts_newest_first(self):
        header, rows = pages.merge_logs([("c1", LOG_PAGE), ("c2", LOG_PAGE_LATER), ("c3", LOG_PAGE)])
        self.assertEqual(header, ["Date/Time", "SourceIP", "Reason"])       # "No." column dropped
        self.assertEqual([r[0] for r in rows], ["2026-11-03T08:00:00", "2026-11-03T07:30:44", "2026-11-03T07:30:43"])

    def test_status_page_is_not_a_log(self):
        self.assertIsNone(pages.merge_logs([("c1", STATUS_PAGE)]))

    def test_history_one_column_per_capture(self):
        later = STATUS_PAGE.replace("30014", "29000")
        sections, cols = pages.history([("20261102-235500-wan2", STATUS_PAGE), ("20261103-235500-wan2", later)])
        dsl = sections[0]
        self.assertEqual(cols, ["20261102-235500-wan2", "20261103-235500-wan2"])
        self.assertEqual(dsl["rows"]["Downstream Sync Rate (kbps) - Line 1"], ["30014", "29000"])
        self.assertEqual(sections[1]["rows"]["Broadband Connection"], ["Up", "Up"])

    @unittest.skipUnless(os.environ.get("REAL_PAGES_DIR"), "REAL_PAGES_DIR not set")
    def test_real_pages(self):
        d = os.environ["REAL_PAGES_DIR"]
        for f in sorted(os.listdir(d)):
            with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            out = pages.clean_html(raw)
            self.assertNotIn("<script", out)
            n = len(pages.tables(raw))
            log = pages.merge_logs([("x", raw)])
            print(f"\n  {f}: {len(out)} chars clean, {n} tables, log rows: {len(log[1]) if log else '-'}", end="")
            self.assertGreater(n, 0, f)


class ScheduleTests(unittest.TestCase):
    def test_rules(self):
        from capture import schedule_slot
        t = datetime(2026, 11, 3, 14, 7)
        self.assertEqual(schedule_slot("hourly", t, None), "2026110314")
        self.assertIsNone(schedule_slot("hourly", t, "2026110314"))                 # once per hour
        self.assertEqual(schedule_slot("hourly", datetime(2026, 11, 3, 15, 0), "2026110314"), "2026110315")
        self.assertIsNone(schedule_slot("daily", t, None))                          # not before 23:55
        self.assertEqual(schedule_slot("daily", datetime(2026, 11, 3, 23, 56), None), "20261103")
        self.assertIsNone(schedule_slot("daily", datetime(2026, 11, 3, 23, 58), "20261103"))
        self.assertIsNone(schedule_slot("off", t, None))


def dsl_page(s1, s2, ds=(0, 60029), us=(0, 11138), dmax=(0, 64944), umax=(0, 12043), fec=(0, 0, 5400, 1),
             crc=(0, 0, 3, 0)):
    """Trimmed AT&T/Arris Broadband Status page: the bonded "DSL Status" table, as the box draws it (the error
    rows have a Downstream and an Upstream column per line)."""
    def row(label, v):
        return f'<tr><th scope="row">{label}</th>' + "".join(f'<td colspan="2">{x}</td>' for x in v) + "</tr>"

    def row4(label, v):
        return f'<tr><th scope="row">{label}</th>' + "".join(f"<td>{x}</td>" for x in v) + "</tr>"
    return f'''<div class="section-header"><h2>DSL Status</h2></div>
    <table class="grid table100" summary="Table of Bonded DSL">
    <tr><th scope="col" id="spacer">&nbsp;</th>
    <th colspan="2" scope="col" id="Line1">Line 1</th><th colspan="2" scope="col" id="Line2">Line 2</th></tr>
    <tr class="a"><th scope="row" id="LineState">Line State</th>
      <td class="col2" colspan="2" headers="Line1 LineState">{s1}</td><td class="col2" colspan="2" headers="Line2 LineState">{s2}</td></tr>
    {row("Downstream Sync Rate (kbps)", ds)}{row("Upstream Sync Rate (kbps)", us)}
    {row("Downstream Max Attainable Rate (kbps)", dmax)}{row("Upstream Max Attainable Rate (kbps)", umax)}
    {row("Modulation", ("Unknown", "VDSL2"))}
    <tr><th>&nbsp;</th><th>Downstream</th><th>Upstream</th><th>Downstream</th><th>Upstream</th></tr>
    {row4("SN Margin (dB)", (0, 0, 9.3, 6.0))}{row4("FEC Errors", fec)}{row4("CRC Errors", crc)}
    </table>'''


SYSINFO = '''<table class="table100"><tr><th scope="row">Time Since Last Reboot</th>
<td class="col2">01:19:49:53</td></tr></table><div id="help"><strong>Time Since Last Reboot:</strong>
Days:Hours:Minutes:Seconds since the device was restarted.</div>'''


class DslTests(unittest.TestCase):
    def test_reads_both_lines(self):
        lines = pages.dsl_lines(dsl_page("Down", "Up"))
        self.assertEqual(lines, [
            {"line": "Line 1", "state": "Down", "down": 0, "up": 0, "ds_max": 0, "us_max": 0, "fec": [0, 0],
             "crc": [0, 0]},
            {"line": "Line 2", "state": "Up", "down": 60029, "up": 11138, "ds_max": 64944, "us_max": 12043,
             "fec": [5400, 1], "crc": [3, 0]}])

    def test_degraded_only_when_another_line_is_up(self):
        self.assertEqual(pages.dsl_degraded(pages.dsl_lines(dsl_page("Down", "Up"))), ["Line 1"])
        self.assertEqual(pages.dsl_degraded(pages.dsl_lines(dsl_page("Up", "TRAINING"))), ["Line 2"])
        self.assertEqual(pages.dsl_degraded(pages.dsl_lines(dsl_page("Up", "Up"))), [])
        self.assertEqual(pages.dsl_degraded(pages.dsl_lines(dsl_page("Down", "TRAINING"))), [])   # WAN down/reboot

    def test_other_pages_have_no_lines(self):
        self.assertEqual(pages.dsl_lines("<table><tr><td>Line State</td><td>Up</td></tr></table>"), [])
        self.assertEqual(pages.dsl_lines(""), [])

    def test_box_uptime(self):
        self.assertEqual(pages.box_uptime(SYSINFO), ((1 * 24 + 19) * 60 + 49) * 60 + 53)
        self.assertIsNone(pages.box_uptime("<h1>Access Code Required</h1>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
