"""HTTP-level tests: start the real server on a free port and call it with urllib."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from server import create_server


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, _ = create_server("127.0.0.1", 0, ":memory:")
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_health_and_catalogue(self):
        self.assertEqual(self.call("GET", "/api/health")[1]["ok"], True)
        status, cat = self.call("GET", "/api/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(len(cat["adapters"]), 3)
        self.assertIn("chaos", [s["id"] for s in cat["scenarios"]])

    def test_full_workflow_over_http(self):
        status, snap = self.call("POST", "/api/experiments", {"scenario": "chaos"})
        self.assertEqual(status, 200)
        cid = snap["conversation"]["conversation_id"]
        self.assertEqual(len(snap["in_flight"]), 1)
        status, r = self.call("POST", f"/api/conversations/{cid}/recover")
        self.assertEqual((status, r["status"]), (200, "incomplete"))
        status, err = self.call("POST", f"/api/conversations/{cid}/resume")
        self.assertEqual((status, err["error"]["code"]), (409, "not_resumable"))
        self.assertEqual(self.call("POST", f"/api/conversations/{cid}/deliver")[1]["delivered"], 1)
        self.assertTrue(self.call("POST", f"/api/conversations/{cid}/recover")[1]["resume"]["resumable"])
        self.assertEqual(self.call("POST", f"/api/conversations/{cid}/resume")[1]["appended"], 2)
        r = self.call("POST", f"/api/conversations/{cid}/recover")[1]
        self.assertEqual(r["dialog_state"], "complete")
        status, hist = self.call("GET", f"/api/conversations/{cid}/recoveries")
        self.assertEqual(len(hist["recoveries"]), 3)
        listing = self.call("GET", "/api/conversations")[1]["conversations"]
        self.assertIn(cid, [c["conversation_id"] for c in listing])

    def test_prototype_compatible_interruption_flag(self):
        snap = self.call("POST", "/api/experiments", {"inject_interruption": True})[1]
        self.assertEqual(snap["conversation"]["faults"], ["adapter_timeout"])
        self.assertEqual(snap["conversation"]["status"], "interrupted")

    def test_ingest_endpoint(self):
        env = {"message_id": "u1", "sequence": 1, "adapter": "web-chat", "role": "user", "content": "hi"}
        self.assertEqual(self.call("POST", "/api/conversations/conv_http/events", env)[1]["outcome"], "accepted")
        self.assertEqual(self.call("POST", "/api/conversations/conv_http/events", env)[1]["outcome"], "duplicate")
        status, err = self.call("POST", "/api/conversations/conv_http/events", {"adapter": "web-chat"})
        self.assertEqual(status, 400)
        self.assertTrue(err["error"]["details"])

    def test_errors_are_json(self):
        cases = [
            ("POST", "/api/experiments", None, b"{not json", 400, "invalid_json"),
            ("GET", "/api/conversations/conv_nope", None, None, 404, "conversation_not_found"),
            ("GET", "/api/nothing", None, None, 404, "not_found"),
            ("GET", "/api/experiments", None, None, 405, "method_not_allowed"),
            ("POST", "/api/experiments", {"scenario": "bogus"}, None, 400, "unknown_scenario"),
            ("POST", "/api/experiments", [1, 2], None, 400, "invalid_body"),
            ("GET", "/../server.py", None, None, 404, "not_found"),
            ("PUT", "/api/experiments", {}, None, 405, "method_not_allowed"),
            ("PATCH", "/api/health", {}, None, 405, "method_not_allowed"),
            ("DELETE", "/api/nothing", None, None, 404, "not_found"),
            ("POST", "/", {}, None, 405, "method_not_allowed"),
        ]
        for method, path, body, raw, status, code in cases:
            with self.subTest(path=path, code=code):
                got_status, payload = self.call(method, path, body, raw)
                self.assertEqual((got_status, payload["error"]["code"]), (status, code))

    def test_head_is_rejected_without_body(self):
        req = urllib.request.Request(self.base + "/api/health", method="HEAD")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 405)

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as res:
            self.assertIn(b"Threadline", res.read())
        with urllib.request.urlopen(self.base + "/app.js", timeout=5) as res:
            self.assertIn("javascript", res.headers["Content-Type"])


if __name__ == "__main__":
    unittest.main()
