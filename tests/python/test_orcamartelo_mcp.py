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

    def test_every_tool_has_a_backend_method_taking_its_arguments(self):
        import inspect
        tools = mcp.handle_message(rpc("tools/list"), None)["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(mcp._TOOL_METHODS))
        for tool in tools:
            method = getattr(daemon.mcp_backend, mcp._TOOL_METHODS[tool["name"]])
            params = set(inspect.signature(method).parameters)
            self.assertEqual(params, set(tool["inputSchema"]["properties"]), tool["name"])

    def test_tools_that_can_spoil_a_print_say_they_are_confirmed(self):
        for tool in mcp.TOOLS:
            if tool["annotations"]["destructiveHint"]:
                self.assertIn("confirms it in OrcaMartelo", tool["description"], tool["name"])

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
            request = daemon.wait_slicer_request(timeout=5)
            self.assertEqual(request["kind"], "camera_snapshot")
            daemon.deliver_slicer_result(request["id"], {"ok": True, "result": {"jpeg_base64": base64.b64encode(JPEG).decode()}})

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
        self.assertEqual(daemon.slicer_state["pending"], [])

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


class Slicer:
    """Answers slicer requests on a thread, as OrcaMartelo would."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop:
            request = daemon.wait_slicer_request(timeout=0.2)
            if request:
                self.requests.append(request)
                daemon.deliver_slicer_result(request["id"], self.answer(request))

    def close(self):
        self.stop = True
        self.thread.join()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        patches = [
            mock.patch.object(daemon, "mqtt_client", self.mqtt),
            mock.patch.object(daemon, "query_all", lambda: None),
            mock.patch.dict(daemon.telemetry, {"connected": True, "state": "free", "job_name": "Ready", "progress": 0}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, name, args=None):
        return mcp.handle_message(rpc("tools/call", {"name": name, "arguments": args or {}}), daemon.mcp_backend)["result"]

    def with_user(self, allowed):
        slicer = Slicer(lambda r: {"ok": True, "result": {"allowed": allowed}})
        self.addCleanup(slicer.close)
        return slicer

    def test_an_allowed_temperature_change_is_sent(self):
        slicer = self.with_user(True)
        with mock.patch.object(daemon.urllib.request, "urlopen", side_effect=OSError):
            result = self.call("set_temperature", {"nozzle": 220, "bed": 70})
        self.assertFalse(result["isError"])
        self.assertEqual(slicer.requests[0]["args"], {"action": "set_temperature", "nozzle": 220, "bed": 70})
        settings = [m["data"]["settings"] for _, m in self.mqtt.published if m.get("action") == "update"]
        self.assertEqual(settings[0], {"target_nozzle_temp": 220, "target_hotbed_temp": 70})

    def test_a_declined_action_sends_nothing(self):
        self.with_user(False)
        daemon.telemetry.update({"state": "printing", "job_name": "cube.gcode"})
        result = self.call("stop_print")
        self.assertTrue(result["isError"])
        self.assertIn("declined", result["content"][0]["text"])
        self.assertEqual(self.mqtt.published, [])

    def test_an_unanswered_confirmation_sends_nothing(self):
        with mock.patch.object(daemon, "CONFIRM_TIMEOUT", 0.1):
            result = self.call("home_axes")
        self.assertTrue(result["isError"])
        self.assertEqual(self.mqtt.published, [])

    def test_moving_is_refused_during_a_print_without_asking(self):
        slicer = self.with_user(True)
        daemon.telemetry.update({"state": "printing"})
        for name, args in (("move_axis", {"axis": "X", "distance_mm": 10}), ("run_gcode", {"gcode": "G28"}),
                           ("extrude", {"length_mm": 5}), ("home_axes", {})):
            with self.subTest(name=name):
                self.assertTrue(self.call(name, args)["isError"])
        self.assertEqual(slicer.requests, [])
        self.assertEqual(self.mqtt.published, [])

    def test_out_of_range_values_are_refused(self):
        self.assertTrue(self.call("set_temperature", {"nozzle": 400})["isError"])
        self.assertTrue(self.call("move_axis", {"axis": "X", "distance_mm": 0})["isError"])
        self.assertEqual(mcp.handle_message(rpc("tools/call", {"name": "set_speed_mode", "arguments": {"mode": "ludicrous"}}),
                                            daemon.mcp_backend)["error"]["code"], mcp.INVALID_PARAMS)

    def test_pause_and_lights_need_no_confirmation(self):
        daemon.telemetry.update({"state": "printing"})
        self.assertFalse(self.call("pause_print")["isError"])
        self.assertFalse(self.call("set_light", {"light": "camera", "on": False})["isError"])
        actions = [m["action"] for _, m in self.mqtt.published]
        self.assertIn("pause", actions)
        self.assertIn("control", actions)

    def test_the_ace_status_shows_the_dryer_from_the_box_report(self):
        daemon.telemetry.update({"material_boxes": [], "ace_humidity": None})
        daemon.apply_multi_color_box_report({"multi_color_box": [{
            "id": 0, "temp": 48, "humidity": 21, "auto_feed": 1, "loaded_slot": -1,
            "drying_status": {"status": 1, "target_temp": 55, "duration": 360, "remain_time": 90},
            "slots": [{"index": 0, "type": "PETG", "color": [255, 0, 0], "status": 5}]}]})
        status = self.call("get_ace_status")["structuredContent"]
        box, = status["boxes"]
        self.assertTrue(status["has_multi_color_box"])
        self.assertEqual((box["temperature_c"], box["humidity_percent"], box["drying"]), (48, 21, True))
        self.assertEqual((box["drying_target_c"], box["drying_remaining_minutes"], box["backup_enabled"]), (55, 90, True))
        self.assertIsNotNone(box["drying_ends"])

    def test_an_idle_dryer_reports_no_target(self):
        daemon.telemetry.update({"material_boxes": [{"id": 0, "temp": 25, "auto_feed": 0,
                                                     "drying_status": {"status": 0, "target_temp": 0}, "slots": []}]})
        box, = self.call("get_ace_status")["structuredContent"]["boxes"]
        self.assertEqual((box["drying"], box["drying_target_c"], box["drying_ends"]), (False, None, None))

    def test_the_printer_file_list_comes_from_its_report(self):
        def report():
            while not any(m.get("action") == "listLocal" for _, m in self.mqtt.published):
                pass
            daemon._on_file_report({"action": "listLocal", "data": {"records": [
                {"filename": "cube.gcode", "size": 1234, "timestamp": 1763300000000},
                {"filename": "_orcacubic_command_1_ab.gcode", "size": 5},
                {"filename": "models", "is_dir": True}]}})

        thread = threading.Thread(target=report)
        thread.start()
        result = self.call("list_printer_files")
        thread.join()
        files = result["structuredContent"]["files"]
        self.assertEqual([f["filename"] for f in files], ["cube.gcode"])
        self.assertEqual(files[0]["size_bytes"], 1234)

    def test_a_local_file_is_uploaded_after_confirmation(self):
        self.with_user(True)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "part.gcode")
            with open(path, "wb") as fh:
                fh.write(GCODE)
            with mock.patch.object(daemon, "upload_and_start_print_file", return_value={"status": "ok", "filename": "part.gcode"}) as upload:
                result = self.call("print_file", {"path": path})
        self.assertFalse(result["isError"])
        self.assertEqual(upload.call_args.args[:2], (GCODE, "part.gcode"))

    def test_print_plate_exports_confirms_uploads_and_removes_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Plate_1.gcode")
            with open(path, "wb") as fh:
                fh.write(GCODE)

            def answer(request):
                if request["kind"] == "export_plate":
                    return {"ok": True, "result": {"path": path, "filename": "Box_plate1.gcode", "plate": 1,
                                                   "estimated_minutes": 42, "estimated_cost": 0.45}}
                return {"ok": True, "result": {"allowed": True}}

            slicer = Slicer(answer)
            self.addCleanup(slicer.close)
            with mock.patch.object(daemon, "upload_and_start_print_file", return_value={"status": "ok", "filename": "Box_plate1.gcode"}) as upload:
                result = self.call("print_plate", {"plate": 1})
            self.assertFalse(result["isError"])
            self.assertEqual([r["kind"] for r in slicer.requests], ["export_plate", "confirm"])
            self.assertEqual(slicer.requests[1]["args"]["action"], "print_plate")
            self.assertEqual(upload.call_args.args[1], "Box_plate1.gcode")
            self.assertFalse(os.path.exists(path))

    def test_slicer_errors_reach_the_model(self):
        slicer = Slicer(lambda r: {"ok": False, "error": "No preset named Foo"})
        self.addCleanup(slicer.close)
        result = self.call("slicer_select_preset", {"type": "print", "name": "Foo"})
        self.assertTrue(result["isError"])
        self.assertIn("No preset named Foo", result["content"][0]["text"])


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
