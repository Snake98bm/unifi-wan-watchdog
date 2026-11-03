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


if __name__ == "__main__":
    unittest.main(verbosity=2)
