"""Quectel AT answer parsing. Run: python tests/test_atparse.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import pages  # noqa: E402
from atparse import _rows, at_html, at_response  # noqa: E402


def rows(cmd, text):
    return dict(_rows(cmd, at_response(text)))


class AtParseTests(unittest.TestCase):
    def test_qeng_sa(self):
        r = rows('AT+QENG="servingcell"', 'AT+QENG="servingcell"\r\n+QENG: "servingcell","NOCONN","NR5G-SA","TDD",'
                 '311,480,A1B2C3D4E,512,4A0B12,520110,41,12,-95,-11,14,1,-\r\nOK\r\n')
        self.assertEqual((r["tech"], r["band"], r["RSRP"], r["RSRQ"], r["SINR"]), ("NR5G-SA", "41", "-95", "-11", "14"))

    def test_qeng_nsa(self):
        text = ('+QENG: "servingcell","NOCONN"\n+QENG: "LTE","FDD",311,480,2A3B4C5,123,2300,66,5,5,5A01,-101,-12,'
                '-70,10,8,-,-\n+QENG:"NR5G-NSA",311,480,345,-98,16,-11,648672,77,12,1\nOK')
        r = rows('AT+QENG="servingcell"', text)
        self.assertEqual((r["state"], r["LTE band"], r["LTE RSRP"], r["NR band"], r["NR SINR"]),
                         ("NOCONN", "66", "-101", "77", "16"))

    def test_qcainfo_temps_qmap_csq(self):
        r = rows("AT+QCAINFO", '+QCAINFO: "PCC",2300,100,"LTE BAND 66",1,123,-101,-12,-70,10\n'
                               '+QCAINFO: "SCC",648672,12,"NR5G BAND 77",1,345,-98,-11,0,16\nOK')
        self.assertEqual((r["PCC band"], r["SCC 1 band"], r["SCC 1 RSRP"]), ("LTE BAND 66", "NR5G BAND 77", "-98"))
        r = rows("AT+QTEMP", '+QTEMP:"modem-lte-sub6-pa1","41"\n+QTEMP:"cpuss-0-usr","55"\nOK')
        self.assertEqual(r["cpuss-0-usr"], "55")
        r = rows('AT+QMAP="WWAN"', '+QMAP: "WWAN",1,1,"IPV4","203.0.113.10"\n+QMAP: "WWAN",1,1,"IPV6","::"\nOK')
        self.assertEqual(r["WWAN IPV4 address"], "203.0.113.10")
        self.assertEqual(rows("AT+CSQ", "+CSQ: 22,99\nOK")["RSSI (0-31)"], "22")
        self.assertEqual(rows("AT+QGMR", "RM520NGLAAR03A03M4G\nOK")["firmware"], "RM520NGLAAR03A03M4G")

    def test_json_and_html_answers(self):
        self.assertEqual(at_response('{"output": "+CPIN: READY\\nOK"}'), ["+CPIN: READY"])
        self.assertEqual(at_response("<pre>+CFUN: 1\nOK</pre>"), ["+CFUN: 1"])

    def test_html_works_with_history_view(self):
        h1 = at_html("AT+CSQ", at_response("+CSQ: 22,99\nOK"))
        h2 = at_html("AT+CSQ", at_response("+CSQ: 9,99\nOK"))
        self.assertNotIn("<script", pages.clean_html(h1 + "<script>x</script>"))
        sections, _ = pages.history([("20261102-235500-wan1", h1), ("20261103-235500-wan1", h2)])
        self.assertEqual(sections[0]["title"], "AT+CSQ")
        self.assertEqual(sections[0]["rows"]["RSSI (0-31)"], ["22", "9"])

    def test_repeated_labels_kept_apart(self):
        h = at_html("AT+QNWINFO", at_response('+QNWINFO: "FDD LTE","311480","LTE BAND 5",2560\n'
                                              '+QNWINFO: "TDD NR5G","311480","NR5G BAND 77",653952\nOK'))
        sections, _ = pages.history([("20261103-235500-wan1", h)])
        self.assertEqual(sections[0]["rows"]["band"], ["LTE BAND 5"])
        self.assertEqual(sections[0]["rows"]["band #2"], ["NR5G BAND 77"])

    def test_toolkit_text_and_json_pages(self):
        from atparse import text_html
        t = pages.tables(text_html("get_uptime", "17:21:14 up  2:11,  load average: 2.73, 2.60, 2.53\n"))[0]
        self.assertIn(["uptime", "2:11"], t["rows"])
        t = pages.tables(text_html("get_uptime", "03:01:02 up 3 min,  load average: 0.5, 0.2, 0.1"))[0]
        self.assertIn(["uptime", "3 min"], t["rows"])
        t = pages.tables(text_html("get_watchcat_status", "{}"))[0]
        self.assertEqual(t["rows"], [["status", "empty (not configured)"]])
        t = pages.tables(text_html("get_ttl_status", '{"isEnabled": false, "ttl": 0}'))[0]
        self.assertIn(["isEnabled", "False"], t["rows"])

    def test_qmanager_json_pages(self):
        from atparse import at_error, at_response, text_html
        self.assertEqual(at_response('{"success": true, "response": "AT+CSQ\\r\\r\\n+CSQ: 22,99\\r\\n\\r\\nOK\\r", '
                                     '"command": "AT+CSQ"}'), ["+CSQ: 22,99"])
        self.assertEqual(at_error('{"success": false, "error": "command_failed", "command": "AT+X"}'), "command_failed")
        self.assertIsNone(at_error('{"success": true, "response": "OK"}'))
        # status: nested fields flattened; a list of records (log entries) gets its own Time table
        ts = pages.tables(text_html("logs.sh", '{"total": 1, "stats": {"rotated_files": 0}, "entries": ['
                                               '{"timestamp": "2026-11-05 10:00:01", "level": "WARN", '
                                               '"component": "poller", "message": "modem slow"}]}'))
        self.assertIn(["stats.rotated_files", "0"], ts[0]["rows"])
        self.assertEqual(ts[1]["header"], ["Time", "level", "component", "message"])
        self.assertEqual(ts[1]["rows"], [["2026-11-05 10:00:01", "WARN", "poller", "modem slow"]])
        merged = pages.merge_logs([("a", text_html("logs.sh", '{"entries": [{"timestamp": "2026-11-05 10:00:01", '
                                                              '"message": "x"}]}'))])
        self.assertEqual(merged[1], [["2026-11-05 10:00:01", "x"]])

    def test_cause_codes(self):
        r = rows("AT+CEREG?", "+CEREG: 2,1,\"5A01\",\"1A2B3C\",7\nOK")
        self.assertEqual((r["LTE status"], r["LTE status meaning"], r["LTE TAC"]), ("1", "registered (home)", "5A01"))
        r = rows("AT+C5GREG?", "+C5GREG: 0,3,,,,1,11\nOK")
        self.assertEqual((r["5G status meaning"], r["5G reject cause"]), ("registration DENIED", "11"))
        r = rows("AT+CEER", '+CEER: "No cause information available"\nOK')
        self.assertEqual(r["last disconnect reason"], "No cause information available")
        r = rows("AT+CGACT?", "+CGACT: 1,1\n+CGACT: 2,0\nOK")
        self.assertEqual((r["context 1 active"], r["context 2 active"]), ("1", "0"))
        r = rows("AT+CGCONTRDP", '+CGCONTRDP: 1,5,"vzwinternet","203.0.113.10.255.255.255.252","203.0.113.9",'
                                 '"198.224.166.135","198.224.167.135"\nOK')
        self.assertEqual((r["context 1 APN"], r["context 1 gateway"]), ("vzwinternet", "203.0.113.9"))

    def test_gateway_log_merges_over_captures(self):
        from atparse import gateway_log_html
        a = ("2026-11-03T03:00:26-08:00 UDM ubios-udapi-server[2384]: dhcp-client: Suspending udhcpc-eth7\n"
             "2026-11-03T03:00:53-08:00 UDM ubios-udapi-server[2384]: wf-interface-eth7 (192.168.225.44) is up\n")
        b = a + "2026-11-03T09:00:00-08:00 UDM wan-watchdog[1]: eth7 back UP at 1000 Mb/s after 6s (link down 5 s)\n"
        header, merged = pages.merge_logs([("c1", gateway_log_html("log", a)), ("c2", gateway_log_html("log", b))])
        self.assertEqual(header, ["Time", "Process", "Message"])
        self.assertEqual([r[0] for r in merged], ["2026-11-03 09:00:00", "2026-11-03 03:00:53", "2026-11-03 03:00:26"])
        self.assertIn("no matching log lines", gateway_log_html("log", ""))

    def test_escaping(self):
        self.assertNotIn("<b>", at_html("AT+X", ["+X: <b>hi</b>"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
