import base64
import http.client
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "resources" / "scripts"
os.environ["ORCACUBIC_PRINTER_IP"] = "192.0.2.21"
os.environ["ORCACUBIC_BRIDGE_TOKEN"] = "test-token"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


daemon = load("anycubic_lan_daemon_mcp", "anycubic_lan_daemon.py")
mcp = daemon.orcamartelo_mcp
stdio = load("orcamartelo_mcp_stdio", "orcamartelo_mcp_stdio.py")

GCODE = b"""; HEADER_BLOCK_START
; total layer number: 10
G1 X1 Y1
; filament used [mm] = 1000.00, 0, 500.00
; filament used [g] = 3.00, 0, 1.50
; filament cost = 0.30, 0, 0.15
; total filament used [g] = 4.50
; total filament cost = 0.45
; filament_type = PETG;PLA;ABS
; filament_colour = #FF0000;#00FF00;#0000FF
"""


def rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class ProtocolTests(unittest.TestCase):
    def test_initialize_agrees_on_a_supported_version(self):
        old = mcp.handle_message(rpc("initialize", {"protocolVersion": "2024-11-05"}), None)["result"]
        self.assertEqual(old["protocolVersion"], "2024-11-05")
        self.assertIn("tools", old["capabilities"])
        unknown = mcp.handle_message(rpc("initialize", {"protocolVersion": "1999-01-01"}), None)["result"]
        self.assertEqual(unknown["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_answer(self):
        self.assertIsNone(mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, None))

    def test_only_set_filament_slot_changes_the_printer(self):
        tools = mcp.handle_message(rpc("tools/list"), None)["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(mcp._TOOL_METHODS))
        writers = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
        self.assertEqual(writers, {"set_filament_slot"})
        for tool in tools:
            self.assertTrue(hasattr(daemon.mcp_backend, mcp._TOOL_METHODS[tool["name"]]))

    def test_without_the_bridge_a_tool_call_explains_what_to_do(self):
        result = mcp.handle_message(rpc("tools/call", {"name": "get_print_status"}), None)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("OrcaMartelo", result["content"][0]["text"])

    def test_bad_requests_get_json_rpc_errors(self):
        self.assertEqual(mcp.handle_message(rpc("nope"), None)["error"]["code"], mcp.METHOD_NOT_FOUND)
        self.assertEqual(mcp.handle_message(rpc("tools/call", {"name": "nope"}), None)["error"]["code"], mcp.INVALID_PARAMS)
        self.assertEqual(json.loads(mcp.handle_text("{", None))["error"]["code"], mcp.PARSE_ERROR)

    def test_a_batch_is_answered_without_its_notifications(self):
        answers = mcp.handle_message([rpc("ping", msg_id=1), {"jsonrpc": "2.0", "method": "notifications/x"}, rpc("ping", msg_id=2)], None)
        self.assertEqual([a["id"] for a in answers], [1, 2])


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        daemon.JOB_COSTS_FILE = os.path.join(self.tmp.name, "costs.json")
        daemon.job_costs.clear()
        daemon.telemetry.update({"state": "free", "job_name": "Ready", "progress": 0})

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_lines_are_read_from_the_end_of_a_large_file(self):
        content = GCODE[:40] + b";" + b"x" * 3_000_000 + b"\n" + GCODE[40:]
        cost, weight, length, filaments = daemon.parse_cost_from_bytes(content)
        self.assertAlmostEqual(cost, 0.45)
        self.assertAlmostEqual(weight, 4.5)
        self.assertAlmostEqual(length, 1500.0)
        # The unused second filament is left out.
        self.assertEqual([(f["filament"], f["type"], f["color"]) for f in filaments], [(1, "PETG", "#FF0000"), (3, "ABS", "#0000FF")])

    def test_a_file_without_cost_lines_records_nothing(self):
        self.assertIsNone(daemon.parse_cost_from_bytes(b"G1 X1\n"))
        self.assertIsNone(daemon.record_job_cost("a.gcode", "", ""))

    def test_the_running_print_reports_its_cost_and_the_share_spent(self):
        daemon.record_job_cost("Box.gcode.3mf", 2.0, 80.0, 26000.0)
        daemon.telemetry.update({"state": "printing", "job_name": "box.gcode", "progress": 25})
        cost = daemon.mcp_backend.print_cost()
        self.assertTrue(cost["is_current_print"])
        self.assertEqual((cost["estimated_cost"], cost["filament_weight_g"], cost["filament_length_m"]), (2.0, 80.0, 26.0))
        self.assertAlmostEqual(cost["spent_so_far_estimate"], 0.5)

    def test_when_idle_the_last_print_is_reported(self):
        daemon.record_job_cost("first.gcode", 1.0, 10.0)
        daemon.record_job_cost("second.gcode", 3.0, 30.0)
        cost = daemon.mcp_backend.print_cost()
        self.assertFalse(cost["is_current_print"])
        self.assertEqual(cost["job"], "second.gcode")

    def test_without_any_recorded_print_there_is_no_cost(self):
        self.assertFalse(daemon.mcp_backend.print_cost()["available"])

    def test_costs_survive_a_restart(self):
        daemon.record_job_cost("kept.gcode", 1.5, 12.0)
        daemon.job_costs.clear()
        daemon._load_job_costs()
        self.assertEqual(daemon.find_job_cost("kept.gcode")["cost"], 1.5)


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        patches = [
            mock.patch.object(daemon, "mqtt_client", self.mqtt),
            mock.patch.object(daemon, "query_all", lambda: None),
            mock.patch.object(daemon.time, "sleep", lambda s: None),
            mock.patch.dict(daemon.telemetry, {"connected": True, "material_boxes": [
                {"id": 0, "slots": [
                    {"slot": 0, "box_id": 0, "box_slot": 0, "source": "ace", "type": "PLA", "color": "#FF0000",
                     "brand": "Anycubic", "finish_type": "gradient", "color_group_hex": ["#FF0000", "#0000FF"]},
                    {"slot": 1, "box_id": 0, "box_slot": 1, "source": "ace", "type": "PETG", "color": "#00FF00", "brand": "Sunlu"},
                ]},
                {"id": -1, "slots": [{"slot": -1, "box_id": -1, "box_slot": 0, "source": "external", "type": "ABS", "color": "#000000"}]},
            ]}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, args):
        return mcp.handle_message(rpc("tools/call", {"name": "set_filament_slot", "arguments": args}), daemon.mcp_backend)

    def sent_slot(self):
        (topic, msg), = self.mqtt.published
        return topic, msg

    def test_a_new_color_is_sent_and_the_rest_of_the_slot_kept(self):
        result = self.call({"slot": 2, "color": "#123abc"})["result"]
        self.assertFalse(result["isError"])
        topic, msg = self.sent_slot()
        self.assertTrue(topic.endswith("/multiColorBox"))
        slot = msg["data"]["multi_color_box"][0]["slots"][0]
        self.assertEqual((slot["index"], slot["type"], slot["color"], slot["brand"]), (1, "PETG", [0x12, 0x3A, 0xBC], "Sunlu"))

    def test_changing_only_the_material_keeps_a_gradient(self):
        self.call({"slot": 1, "material": "petg"})
        slot = self.sent_slot()[1]["data"]["multi_color_box"][0]["slots"][0]
        self.assertEqual(slot["type"], "PETG")
        self.assertEqual(slot["icon_type"], 1)
        self.assertEqual(len(slot["color_group"]), 2)

    def test_the_external_spool_is_set_too(self):
        self.call({"slot": "external", "color": "#FFFFFF"})
        topic, msg = self.sent_slot()
        self.assertTrue(topic.endswith("/extfilbox"))
        self.assertEqual(msg["data"]["color"], [255, 255, 255])

    def test_bad_values_are_refused_without_sending_anything(self):
        for args in ({"slot": 9, "color": "#FFFFFF"}, {"slot": 1, "color": "red"}, {"slot": 1}, {"slot": 1, "material": "PLA; rm"}):
            with self.subTest(args=args):
                self.assertTrue(self.call(args)["result"]["isError"])
        self.assertEqual(self.mqtt.published, [])
        self.assertEqual(self.call({"slot": True, "color": "#FFFFFF"})["error"]["code"], mcp.INVALID_PARAMS)
        self.assertEqual(self.call({"color": "#FFFFFF"})["error"]["code"], mcp.INVALID_PARAMS)


JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body\xff\xd9"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        for key in ("custom_camera_url", "connected"):
            patch = mock.patch.dict(daemon.telemetry, {key: daemon.telemetry.get(key)})
            patch.start()
            self.addCleanup(patch.stop)

    def snapshot(self):
        return mcp.handle_message(rpc("tools/call", {"name": "get_camera_snapshot"}), daemon.mcp_backend)["result"]

    def test_the_native_camera_frame_comes_from_the_slicer(self):
        daemon.telemetry.update({"custom_camera_url": "", "connected": True})

        def slicer():
            sid = daemon.wait_snapshot_request(timeout=5)
            daemon.deliver_snapshot(sid, JPEG)

        thread = threading.Thread(target=slicer)
        thread.start()
        result = self.snapshot()
        thread.join()
        image = result["content"][0]
        self.assertEqual((image["type"], image["mimeType"]), ("image", "image/jpeg"))
        self.assertEqual(base64.b64decode(image["data"]), JPEG)

    def test_a_slicer_that_never_answers_gives_a_tool_error(self):
        daemon.telemetry.update({"custom_camera_url": "", "connected": True})
        with mock.patch.object(daemon, "SNAPSHOT_TIMEOUT", 0.1):
            result = self.snapshot()
        self.assertTrue(result["isError"])
        self.assertEqual(daemon.snapshot_state["pending"], [])

    def test_an_mjpeg_camera_is_read_directly(self):
        class Mjpeg(daemon.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n--frame\r\n")

            def log_message(self, *args):
                pass

        server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), Mjpeg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            daemon.telemetry["custom_camera_url"] = f"http://127.0.0.1:{server.server_address[1]}/webcam/stream.mjpeg"
            result = self.snapshot()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(base64.b64decode(result["content"][0]["data"]), JPEG)


class HttpTests(unittest.TestCase):
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

    def post(self, body, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("POST", "/mcp", body=json.dumps(body), headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if data else None)

    def test_the_bridge_answers_mcp_over_http(self):
        daemon.telemetry.update({"state": "printing", "job_name": "cube.gcode", "progress": 40, "curr_layer": 4, "total_layers": 10})
        status, body = self.post(rpc("tools/call", {"name": "get_print_status"}))
        self.assertEqual(status, 200)
        data = body["result"]["structuredContent"]
        self.assertEqual((data["state"], data["job"], data["progress_percent"], data["layer"]), ("printing", "cube.gcode", 40, 4))

    def test_a_notification_is_accepted_without_a_body(self):
        status, body = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))

    def test_a_web_page_from_another_site_is_refused(self):
        status, _ = self.post(rpc("ping"), {"Origin": "https://example.com"})
        self.assertEqual(status, 403)

    def test_the_stdio_bridge_forwards_to_the_http_server(self):
        stdin = io.TextIOWrapper(io.BytesIO((json.dumps(rpc("tools/list")) + "\n").encode()))
        stdout = io.TextIOWrapper(io.BytesIO())
        with mock.patch.object(stdio, "BRIDGE_URL", f"http://127.0.0.1:{self.port}/mcp"), \
             mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            stdio.main()
            stdout.flush()
            out = stdout.buffer.getvalue()
        self.assertEqual(len(json.loads(out)["result"]["tools"]), len(mcp.TOOLS))


class StdioOfflineTests(unittest.TestCase):
    def test_the_stdio_bridge_answers_alone_when_the_bridge_is_down(self):
        stdin = io.TextIOWrapper(io.BytesIO((json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"})) + "\n"
                                             + json.dumps(rpc("tools/call", {"name": "get_print_cost"}, 2)) + "\n").encode()))
        stdout = io.TextIOWrapper(io.BytesIO())
        with mock.patch.object(stdio, "BRIDGE_URL", "http://127.0.0.1:1/mcp"), \
             mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            stdio.main()
            stdout.flush()
            lines = stdout.buffer.getvalue().decode().splitlines()
        self.assertEqual(json.loads(lines[0])["result"]["serverInfo"]["name"], "orcamartelo")
        self.assertTrue(json.loads(lines[1])["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
