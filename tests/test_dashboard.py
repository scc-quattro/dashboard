import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from collector import DEFAULT_CONFIG, collect, load_config
from server import create_server, standalone


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "work"
        self.project.mkdir()
        self.evidence = self.project / "evidence.json"
        self.evidence.write_text('{"synthetic": true}\n')
        self.report = {"stage": "verified", "baseline_verified": True,
                       "sources": [{"path": "evidence.json", "sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest()}],
                       "primary": {"value": 12.5, "unit": "s", "label": "time"},
                       "chart": {"labels": ["base", "new"], "series": [{"name": "time", "values": [20, 12.5]}]}}
        self.config = {"schema_version": 1, "projects": [{"id": "custom", "name": "Custom Project", "kind": "APP", "path": "work"}]}
        self.path = self.root / "config.json"
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.config))
        (self.project / "dashboard.json").write_text(json.dumps(self.report))

    def test_default_demo_has_no_private_paths_and_three_verified_charts(self):
        snapshot = collect(DEFAULT_CONFIG)
        self.assertTrue(snapshot["demo"])
        self.assertEqual(len(snapshot["projects"]), 4)
        self.assertEqual(snapshot["warnings"], [])
        self.assertEqual(sum(bool(p["chart"]) for p in snapshot["projects"]), 3)
        self.assertNotIn(str(Path.home()), json.dumps(snapshot))

    def test_custom_project_and_resource_type(self):
        snapshot = collect(self.path)
        project = snapshot["projects"][0]
        self.assertEqual(project["name"], "Custom Project")
        self.assertEqual(project["kind"], "APP")
        self.assertTrue(project["baseline_verified"])
        self.assertIsNotNone(project["chart"])
        self.assertNotIn(str(self.root), json.dumps(snapshot))

    def test_changed_evidence_hides_verified_metrics(self):
        self.evidence.write_text("changed")
        snapshot = collect(self.path)
        project = snapshot["projects"][0]
        self.assertFalse(project["baseline_verified"])
        self.assertEqual(project["primary"]["text"], "—")
        self.assertIsNone(project["chart"])
        self.assertEqual(len(snapshot["warnings"]), 1)

    def test_missing_pinned_hash_cannot_claim_verified(self):
        self.report["sources"][0].pop("sha256")
        self.save()
        self.assertFalse(collect(self.path)["projects"][0]["baseline_verified"])

    def test_unverified_report_does_not_show_chart(self):
        self.report["baseline_verified"] = False
        self.save()
        self.assertIsNone(collect(self.path)["projects"][0]["chart"])

    def test_missing_report_is_visible_as_warning(self):
        (self.project / "dashboard.json").unlink()
        self.assertEqual(len(collect(self.path)["warnings"]), 1)

    def test_chart_nan_or_mismatched_series_invalidates_report(self):
        for values in [[float("nan"), 1], [1], [-1, 2]]:
            with self.subTest(values=values):
                self.report["chart"]["series"][0]["values"] = values
                self.save()
                self.assertEqual(len(collect(self.path)["warnings"]), 1)

    def test_source_traversal_and_symlink_escape_are_rejected(self):
        (self.root / "private.txt").write_text("private")
        for source in ["../private.txt", str(self.root / "private.txt"), "link.txt"]:
            if source == "link.txt":
                (self.project / source).symlink_to(self.root / "private.txt")
            self.report["sources"] = [{"path": source}]
            self.save()
            self.assertEqual(len(collect(self.path)["warnings"]), 1)

    def test_duplicate_project_or_escaping_report_is_rejected(self):
        self.config["projects"].append(copy.deepcopy(self.config["projects"][0]))
        self.save()
        with self.assertRaises(ValueError):
            load_config(self.path)
        self.config["projects"].pop()
        self.config["projects"][0]["report"] = "../private.json"
        self.save()
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_malformed_optional_field_does_not_leave_partial_result(self):
        self.report["jobs"] = [{"id": "bad", "observed_at": "not a time"}]
        self.save()
        project = collect(self.path)["projects"][0]
        self.assertFalse(project["baseline_verified"])
        self.assertIsNone(project["chart"])
        self.assertEqual(project["sources"], [])

    def test_standalone_escapes_script_terminator_and_does_not_embed_sessions(self):
        data = collect()
        data["title"] = "</script><script>alert('unsafe')</script>"
        html = standalone(data)
        self.assertNotIn("window.DASHBOARD_SNAPSHOT={\"title\":\"</script>", html)
        self.assertIn("\\u003c/script>", html)
        self.assertNotIn('src="/app.js"', html)
        self.assertNotIn('src="/vendor/echarts.min.js"', html)
        self.assertNotIn('"sessions":', html)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def get(self, path, headers=None):
        return urlopen(Request(self.url + path, headers=headers or {}), timeout=10)

    def test_health_and_default_disabled_traces(self):
        self.assertTrue(json.load(self.get("/api/health"))["ok"])
        data = json.load(self.get("/api/traces"))
        self.assertFalse(data["enabled"])
        self.assertEqual(data["sessions"], [])
        self.assertIsNone(self.server.trace_reader)

    def test_trace_rejects_remote_host_origin_and_cross_site(self):
        for headers in [{"Host": "dashboard.example.com"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.get("/api/traces", headers)
            self.assertEqual(error.exception.code, 403)

    def test_no_arbitrary_file_routes(self):
        for path in ["/collector.py", "/dashboard.local.json", "/examples/dashboard.json", "/../LICENSE", "/api/traces/../../etc/passwd"]:
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.get(path)
            self.assertEqual(error.exception.code, 404)

    def test_export_headers_and_refresh(self):
        response = self.get("/download.html")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn(b"DASHBOARD_SNAPSHOT", response.read())

    def test_flag_alone_cannot_enable_traces(self):
        with patch("server.TraceReader") as reader:
            server = create_server(port=0, enable_traces=True)
            server.server_close()
            reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
