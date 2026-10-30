import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[2] / "resources" / "scripts" / "anycubic_lan_daemon.py"
os.environ["ORCACUBIC_PRINTER_IP"] = "192.0.2.20"
os.environ["ORCACUBIC_BRIDGE_TOKEN"] = "test-token"
spec = importlib.util.spec_from_file_location("anycubic_lan_daemon_notifications", MODULE_PATH)
daemon = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(daemon)

TOPIC = "anycubic/anycubicCloud/v1/printer/public/20025/dev/{}/report"


def send(topic, payload):
    msg = SimpleNamespace(topic=TOPIC.format(topic), payload=json.dumps(payload).encode("utf-8"))
    daemon.on_mqtt_message(None, None, msg)


def project(state, progress, filename="cube.gcode"):
    send("info", {"code": 200, "data": {"project": {"filename": filename, "state": state, "progress": progress}}})


def events():
    return [(n["event"], n["job"]) for n in daemon.notifications]


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        daemon.NOTIFICATIONS_FILE = os.path.join(self.tmp.name, "notifications.json")
        daemon.notifications.clear()
        daemon.notification_tracker.update({"job": "", "next_id": 1})
        daemon.telemetry.update({"state": "free", "progress": 0, "job_name": "Ready",
                                 "last_error": "", "last_error_code": 0, "last_error_time": 0})

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_print_reports_start_pause_resume_and_finish(self):
        project("printing", 1)
        project("printing", 40)
        project("paused", 50)
        project("printing", 50)
        project("printing", 100)
        project("finished", 100)
        project("free", 0)
        self.assertEqual(events(), [("started", "cube.gcode"), ("paused", "cube.gcode"),
                                    ("resumed", "cube.gcode"), ("finished", "cube.gcode")])
        self.assertEqual(daemon.notifications[-1]["kind"], "success")

    def test_a_print_that_goes_idle_early_is_reported_as_stopped(self):
        project("printing", 10)
        send("status", {"state": "free"})
        self.assertEqual(events()[-1], ("stopped", "cube.gcode"))
        self.assertIn("10%", daemon.notifications[-1]["message"])

    def test_a_print_running_when_the_bridge_starts_is_not_announced(self):
        daemon.telemetry["state"] = "idle"
        project("printing", 60)
        project("finished", 100)
        self.assertEqual(events(), [("finished", "cube.gcode")])

    def test_internal_command_files_make_no_notifications(self):
        project("printing", 0, "_orcacubic_command_home.gcode")
        project("finished", 100, "_orcacubic_command_home.gcode")
        self.assertEqual(events(), [])

    def test_a_repeated_printer_error_is_recorded_once(self):
        for _ in range(3):
            send("print", {"code": 10500, "msg": "Filament runout", "state": "failed"})
        errors = [n for n in daemon.notifications if n["event"] == "printer_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["kind"], errors[0]["code"]), ("error", 10500))

    def test_notifications_survive_a_restart(self):
        project("printing", 1)
        daemon.notifications.clear()
        daemon._load_notifications()
        self.assertEqual(events(), [("started", "cube.gcode")])
        self.assertEqual(daemon.notification_tracker["next_id"], 2)


class NotificationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), daemon.BridgeServer)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        daemon.NOTIFICATIONS_FILE = os.path.join(self.tmp.name, "notifications.json")
        daemon.notifications.clear()
        daemon.notification_tracker.update({"job": "", "next_id": 1})
        daemon.add_notification("info", "started", "Print started", job="a.gcode")
        daemon.add_notification("success", "finished", "Print finished", job="a.gcode")

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={"X-OrcaCubic-Token": "test-token", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return data

    def test_listing_since_an_id_returns_only_newer_entries(self):
        data = self.request("GET", "/notifications?since=1")
        self.assertEqual([n["id"] for n in data["notifications"]], [2])
        self.assertEqual((data["unread"], data["last_id"]), (2, 2))

    def test_marking_read_and_clearing(self):
        self.assertEqual(self.request("POST", "/notifications/read", {"ids": [1]})["unread"], 1)
        self.assertEqual(self.request("POST", "/notifications/read", {})["unread"], 0)
        data = self.request("POST", "/notifications/clear", {})
        self.assertEqual((data["notifications"], data["last_id"]), ([], 0))


if __name__ == "__main__":
    unittest.main()
