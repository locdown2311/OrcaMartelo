import http.client
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "resources" / "scripts" / "anycubic_lan_daemon.py"
os.environ["ORCACUBIC_PRINTER_IP"] = "192.0.2.20"
os.environ["ORCACUBIC_BRIDGE_TOKEN"] = "test-token"
spec = importlib.util.spec_from_file_location("anycubic_lan_daemon", MODULE_PATH)
daemon = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(daemon)


class TargetBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        daemon.PRINTER_IP = "192.0.2.20"
        daemon.BRIDGE_TOKEN = "test-token"
        cls.server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), daemon.BridgeServer)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post_control(self, expected_printer):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        body = json.dumps({"action": "clear_alert"})
        connection.request(
            "POST",
            "/control",
            body,
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": expected_printer,
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_matching_device_target_is_accepted(self):
        status, payload = self.post_control("192.0.2.20")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_stale_device_target_is_rejected_before_control(self):
        status, payload = self.post_control("192.0.2.21")
        self.assertEqual(status, 409)
        self.assertEqual(payload["message"], "Device selection changed")

    def test_start_print_job_does_not_repeat_pre_engage(self):
        published_messages = []
        class MockMqttClient:
            def publish(self, topic, payload):
                published_messages.append((topic, json.loads(payload)))

        old_client = daemon.mqtt_client
        daemon.mqtt_client = MockMqttClient()
        daemon.telemetry["connected"] = True
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            body = json.dumps({
                "action": "start_print_job",
                "filename": "test.gcode",
                "use_ams": True,
                "pre_engage_filament": True,
                "initial_slot": "2",
                "ams_box_mapping": [
                    {"ams_index": 2, "paint_index": 0, "material_type": "PLA"}
                ]
            })
            connection.request("POST", "/control", body, {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": "192.0.2.20"
            })
            resp = connection.getresponse()
            self.assertEqual(resp.status, 200)
            connection.close()

            # The upload path pre-engages the channel before this request, then waits
            # for the printer's extrudeControl report. start_print_job must only start.
            ext_msgs = [p for t, p in published_messages if p.get("type") == "extrudeControl" and p.get("action") == "switchChannel"]
            self.assertEqual(len(ext_msgs), 0)

            print_msgs = [p for t, p in published_messages if p.get("type") == "print" and p.get("action") == "start"]
            self.assertEqual(len(print_msgs), 1)
            self.assertEqual(print_msgs[0]["data"]["filetype"], 1)
            self.assertEqual(print_msgs[0]["data"]["filename"], "test.gcode")
        finally:
            daemon.mqtt_client = old_client

    def test_state_is_always_normalized_string_in_telemetry_and_status(self):
        self.assertEqual(daemon.normalize_printer_state(0), "free")
        self.assertEqual(daemon.normalize_printer_state(1), "busy")
        self.assertEqual(daemon.normalize_printer_state(2), "printing")
        self.assertEqual(daemon.normalize_printer_state("0"), "free")
        self.assertEqual(daemon.normalize_printer_state("READY"), "free")

        daemon.telemetry["state"] = 0
        pub = daemon.public_telemetry()
        self.assertIsInstance(pub["state"], str)
        self.assertEqual(pub["state"], "free")

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("GET", "/status", headers={"X-OrcaCubic-Token": "test-token"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        conn.close()
        self.assertIsInstance(data.get("state"), str)
        self.assertEqual(data.get("state"), "free")

    def test_estimated_remaining_time_and_eta_when_printer_reports_zero(self):
        msg = {
            "type": "report",
            "data": {
                "project": {
                    "filename": "presepe.gcode",
                    "state": "printing",
                    "curr_layer": 114,
                    "total_layers": 475,
                    "progress": 24,
                    "print_time": 85,
                    "remain_time": 0
                }
            }
        }
        daemon.on_mqtt_message(None, None, type("MqttMsg", (), {"topic": "anycubic/anycubicCloud/v1/printer/public/20030/test-device/info/report", "payload": json.dumps(msg).encode()})())
        self.assertEqual(daemon.telemetry["state"], "printing")
        self.assertEqual(daemon.telemetry["print_time"], 85)
        self.assertGreater(daemon.telemetry["remain_time"], 250)
        self.assertLess(daemon.telemetry["remain_time"], 280)

        pub = daemon.public_telemetry()
        self.assertIn("estimated_end_time", pub)
        self.assertNotEqual(pub["estimated_end_time"], "--")
        self.assertEqual(len(pub["estimated_end_time"]), 5)  # HH:MM

    def test_start_print_job_for_3mf_uses_local_task_and_internal_gcode_name(self):
        published_messages = []
        class MockMqttClient:
            def publish(self, topic, payload):
                published_messages.append((topic, json.loads(payload)))

        old_client = daemon.mqtt_client
        daemon.mqtt_client = MockMqttClient()
        daemon.telemetry["connected"] = True
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            body = json.dumps({
                "action": "start_print_job",
                "filename": "Plate_model.gcode.3mf",
                "use_ams": True,
                "pre_engage_filament": False,
                "ams_box_mapping": [
                    {"ams_index": 0, "paint_index": 0, "material_type": "PLA"}
                ]
            })
            connection.request("POST", "/control", body, {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": "192.0.2.20"
            })
            resp = connection.getresponse()
            self.assertEqual(resp.status, 200)
            connection.close()

            print_msgs = [p for t, p in published_messages if p.get("type") == "print" and p.get("action") == "start"]
            self.assertEqual(len(print_msgs), 1)
            # Local print task uses filetype 1
            self.assertEqual(print_msgs[0]["data"]["filetype"], 1)
            # Internal filename must be the extracted .gcode, not ending in .3mf
            self.assertEqual(print_msgs[0]["data"]["filename"], "Plate_model.gcode")
        finally:
            daemon.mqtt_client = old_client

    def test_switch_channel_control_action(self):
        published_messages = []
        class MockMqttClient:
            def publish(self, topic, payload):
                published_messages.append((topic, json.loads(payload)))

        old_client = daemon.mqtt_client
        daemon.mqtt_client = MockMqttClient()
        daemon.telemetry["connected"] = True
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            body = json.dumps({
                "action": "switch_channel",
                "index": 3
            })
            connection.request("POST", "/control", body, {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": "192.0.2.20"
            })
            resp = connection.getresponse()
            self.assertEqual(resp.status, 200)
            connection.close()

            ext_msgs = [p for t, p in published_messages if p.get("type") == "extrudeControl" and p.get("action") == "switchChannel"]
            self.assertEqual(len(ext_msgs), 1)
            self.assertEqual(ext_msgs[0]["data"]["index"], 3)
        finally:
            daemon.mqtt_client = old_client

    def test_extrude_control_mqtt_report_updates_channel_index(self):
        class MockMessage:
            def __init__(self, topic, payload):
                self.topic = topic
                self.payload = payload.encode("utf-8")

        mock_payload = json.dumps({
            "type": "extrudeControl",
            "action": "getInfo",
            "code": 200,
            "data": {
                "index": 2,
                "current_status": 0,
                "has_filaments": 1,
                "move_type": 0
            }
        })
        msg = MockMessage(f"anycubic/anycubicCloud/v1/printer/public/20030/test-device/extrudeControl/report", mock_payload)
        previous_seq = daemon.telemetry.get("channel_report_seq", 0)
        daemon.on_mqtt_message(None, None, msg)
        self.assertEqual(daemon.telemetry["channel_index"], 2)
        self.assertEqual(daemon.telemetry["channel_status"], 0)
        self.assertEqual(daemon.telemetry["channel_report_seq"], previous_seq + 1)

    def test_extrude_control_report_without_status_preserves_switching_state(self):
        # A report carrying only `index` must not reset an in-progress switch
        # (current_status 3) to idle, or the C++ confirmation check would see
        # "idle + matching index" and start the print mid-feed.
        class MockMessage:
            def __init__(self, topic, payload):
                self.topic = topic
                self.payload = payload.encode("utf-8")

        daemon.telemetry["channel_index"] = 0
        daemon.telemetry["channel_status"] = 3  # switching in progress

        msg = MockMessage(
            "anycubic/anycubicCloud/v1/printer/public/20030/test-device/extrudeControl/report",
            json.dumps({
                "type": "extrudeControl",
                "action": "switchChannel",
                "code": 200,
                "data": {"index": 2}
            })
        )
        daemon.on_mqtt_message(None, None, msg)
        self.assertEqual(daemon.telemetry["channel_index"], 2)
        self.assertEqual(daemon.telemetry["channel_status"], 3)

    def test_extrude_control_switch_failure_is_recorded(self):
        class MockMessage:
            def __init__(self, topic, payload):
                self.topic = topic
                self.payload = payload.encode("utf-8")

        daemon.telemetry.pop("channel_switch_failed", None)
        msg = MockMessage(
            "anycubic/anycubicCloud/v1/printer/public/20030/test-device/extrudeControl/report",
            json.dumps({
                "type": "extrudeControl",
                "action": "switchChannel",
                "code": 10502,
                "msg": "switch failed",
                "data": None
            })
        )
        daemon.on_mqtt_message(None, None, msg)
        self.assertIn("channel_switch_failed", daemon.telemetry)

    def test_upload_print_file_uploads_over_http_and_starts_local_task(self):
        published_messages = []
        upload_requests = []

        class MockMqttClient:
            def publish(self, topic, payload):
                published_messages.append((topic, json.loads(payload)))

        class MockResponse:
            def read(self):
                return b'{"code":200,"data":{"gcode":"cube.gcode"},"message":"success"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        content = b"; HEADER_BLOCK_START\nG28\nM400\n"
        expected_md5 = __import__("hashlib").md5(content).hexdigest().lower()

        def fake_urlopen(req, timeout=None):
            upload_requests.append(req)
            return MockResponse()

        old_client = daemon.mqtt_client
        old_urlopen = daemon.urllib.request.urlopen
        daemon.mqtt_client = MockMqttClient()
        daemon.telemetry["connected"] = True
        daemon.urllib.request.urlopen = fake_urlopen
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps({
                "filename": "cube.gcode",
                "content_base64": __import__("base64").b64encode(content).decode("ascii"),
                "use_ams": True
            })
            connection.request("POST", "/upload_print_file", body, {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": "192.0.2.20"
            })
            resp = connection.getresponse()
            payload = json.loads(resp.read())
            connection.close()

            self.assertEqual(resp.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["filename"], "cube.gcode")

            # File bytes travelled over HTTP multipart to the printer upload endpoint.
            self.assertEqual(len(upload_requests), 1)
            req = upload_requests[0]
            self.assertIn("gcode_upload", req.full_url)
            # Multipart body must open with the declared boundary delimiter.
            ctype = req.get_header("Content-type")
            boundary = ctype.split("boundary=", 1)[1].strip()
            self.assertTrue(req.data.startswith(("--" + boundary).encode("utf-8")))
            self.assertTrue(req.data.endswith(("--" + boundary + "--\r\n").encode("utf-8")))
            self.assertIn(b'name="gcode"; filename="cube.gcode"', req.data)
            self.assertIn(content, req.data)
            self.assertEqual(req.get_header("X-file-length"), str(len(content)))

            # The start trigger is MQTT-only, local filetype 1, no cloud URL.
            print_msgs = [p for t, p in published_messages if p.get("type") == "print" and p.get("action") == "start"]
            self.assertEqual(len(print_msgs), 1)
            self.assertEqual(print_msgs[0]["data"]["filetype"], 1)
            self.assertEqual(print_msgs[0]["data"]["filename"], "cube.gcode")
            self.assertEqual(print_msgs[0]["data"]["md5"], expected_md5)
            self.assertEqual(print_msgs[0]["data"]["filesize"], len(content))
            self.assertEqual(print_msgs[0]["data"]["url"], "")
        finally:
            daemon.mqtt_client = old_client
            daemon.urllib.request.urlopen = old_urlopen

    def test_upload_print_file_3mf_starts_under_inner_gcode_name(self):
        published_messages = []

        class MockMqttClient:
            def publish(self, topic, payload):
                published_messages.append((topic, json.loads(payload)))

        class MockResponse:
            def read(self):
                return b'{"code":200,"message":"success"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=None):
            return MockResponse()

        old_client = daemon.mqtt_client
        old_urlopen = daemon.urllib.request.urlopen
        daemon.mqtt_client = MockMqttClient()
        daemon.telemetry["connected"] = True
        daemon.urllib.request.urlopen = fake_urlopen
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps({
                "filename": "Plate_model.gcode.3mf",
                "content_base64": __import__("base64").b64encode(b"PK\x03\x04dummy").decode("ascii")
            })
            connection.request("POST", "/upload_print_file", body, {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-OrcaCubic-Token": "test-token",
                "X-OrcaCubic-Printer": "192.0.2.20"
            })
            resp = connection.getresponse()
            payload = json.loads(resp.read())
            connection.close()

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["start_filename"], "Plate_model.gcode")
            print_msgs = [p for t, p in published_messages if p.get("type") == "print" and p.get("action") == "start"]
            self.assertEqual(len(print_msgs), 1)
            self.assertEqual(print_msgs[0]["data"]["filename"], "Plate_model.gcode")
            self.assertEqual(print_msgs[0]["data"]["filetype"], 1)
        finally:
            daemon.mqtt_client = old_client
            daemon.urllib.request.urlopen = old_urlopen

    def test_upload_print_file_rejects_bad_payload(self):
        old_client = daemon.mqtt_client
        daemon.mqtt_client = None  # must be rejected before any upload is attempted
        try:
            for bad_body in (
                json.dumps({"filename": "", "content_base64": "AAAA"}),
                json.dumps({"filename": "x.gcode", "content_base64": ""}),
                json.dumps({"filename": "x.gcode", "content_base64": "!!!not-base64!!!"}),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                connection.request("POST", "/upload_print_file", bad_body, {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(bad_body)),
                    "X-OrcaCubic-Token": "test-token",
                    "X-OrcaCubic-Printer": "192.0.2.20"
                })
                resp = connection.getresponse()
                payload = json.loads(resp.read())
                connection.close()
                self.assertEqual(resp.status, 200)
                self.assertEqual(payload["status"], "error")
        finally:
            daemon.mqtt_client = old_client

    def test_camera_endpoint_handles_offline_upstream_without_crash(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        # Point to unreachable address to simulate camera temporarily offline
        daemon.telemetry["raw_camera_url"] = "http://127.0.0.1:59999/flv"
        connection.request("GET", "/camera")
        resp = connection.getresponse()
        self.assertEqual(resp.status, 502)
        connection.close()


class MaterialSystemTests(unittest.TestCase):
    def setUp(self):
        daemon.telemetry["has_multi_color_box"] = False
        daemon.telemetry["material_boxes"] = []
        daemon.telemetry["external_spool"] = None
        daemon.telemetry["filaments"] = []

    def test_external_ace_report_with_optional_fields_is_detected(self):
        daemon.apply_multi_color_box_report({
            "multi_color_box": [{
                "id": 0,
                "slots": [{"index": 0, "type": "PLA", "color": [1, 2, 3], "edit_status": 1}],
            }]
        })
        self.assertTrue(daemon.telemetry["has_multi_color_box"])
        self.assertEqual(daemon.telemetry["material_boxes"][0]["id"], 0)
        self.assertEqual(daemon.telemetry["filaments"][0]["slot"], 0)

    def test_multiple_ace_units_keep_box_and_global_slot_identity(self):
        daemon.apply_multi_color_box_report({
            "max_box_num": 4,
            "multi_color_box": [
                {"id": 0, "slots": [{"index": 3, "type": "PLA", "color": [10, 20, 30], "status": 5}]},
                {"id": 1, "slots": [{"index": 2, "type": "PETG", "color": [40, 50, 60], "edit_status": 1}]},
            ],
        })
        slots = daemon.telemetry["filaments"]
        self.assertEqual([(s["box_id"], s["box_slot"], s["slot"]) for s in slots], [(0, 3, 3), (1, 2, 6)])
        self.assertEqual(len(daemon.telemetry["material_boxes"]), 2)

    def test_builtin_negative_id_rack_keeps_unique_zero_based_slots(self):
        daemon.apply_multi_color_box_report({"multi_color_box": [{"id": -1, "slots": [
            {"index": 0, "type": "PLA", "edit_status": 1},
            {"index": 3, "type": "PETG", "edit_status": 1},
        ]}]})
        box = daemon.telemetry["material_boxes"][0]
        self.assertEqual(box["source"], "rack")
        self.assertEqual([slot["slot"] for slot in box["slots"]], [0, 3])

    def test_mixed_negative_id_entry_uses_multicolorbox_not_extfilbox(self):
        daemon.apply_multi_color_box_report({"head_tools_model": 1, "multi_color_box": [
            {"id": -1, "slots": [{"index": 0, "type": "TPU", "edit_status": 1}]},
            {"id": 0, "slots": [{"index": 0, "type": "PLA", "edit_status": 1}]},
        ]})
        rack = next(box for box in daemon.telemetry["material_boxes"] if box["id"] == -1)
        self.assertEqual(rack["source"], "external_mcb")
        suffix, payload = daemon.build_material_update_messages([rack["slots"][0]])[0]
        self.assertEqual(suffix, "multiColorBox")
        self.assertEqual(payload["data"]["multi_color_box"][0]["id"], -1)

    def test_status_four_slot_is_not_available(self):
        daemon.apply_multi_color_box_report({"multi_color_box": [{"id": 0, "slots": [
            {"index": 0, "type": "PLA", "status": 4},
        ]}]})
        self.assertFalse(daemon.telemetry["filaments"][0]["available"])

    def test_external_spool_is_used_when_no_ace_is_connected(self):
        daemon.apply_external_spool_report({"type": "TPU", "color": [7, 8, 9], "loaded": 1})
        slot = daemon.telemetry["filaments"][0]
        self.assertEqual(slot["source"], "external")
        self.assertEqual(slot["slot"], -1)
        self.assertEqual(slot["type"], "TPU")

    def test_external_spool_and_ace_units_are_retained_together(self):
        daemon.apply_external_spool_report({"type": "TPU", "color": [7, 8, 9], "loaded": 1})
        daemon.apply_multi_color_box_report({"multi_color_box": [
            {"id": 0, "slots": [{"index": 0, "type": "PLA", "edit_status": 1}]},
            {"id": 1, "slots": [{"index": 0, "type": "PETG", "edit_status": 1}]},
        ]})
        self.assertEqual([box["source"] for box in daemon.telemetry["material_boxes"]], ["ace", "ace", "external"])

    def test_live_empty_ace_report_clears_stale_box_data(self):
        daemon.apply_multi_color_box_report({"multi_color_box": [{"id": 0, "slots": [{"index": 0, "type": "PLA", "edit_status": 1}]}]})
        daemon.apply_multi_color_box_report({"multi_color_box": []})
        self.assertFalse(daemon.telemetry["has_multi_color_box"])
        self.assertEqual(daemon.telemetry["material_boxes"], [])
        self.assertEqual(daemon.telemetry["filaments"], [])

    def test_live_empty_ace_report_keeps_external_spool(self):
        daemon.apply_external_spool_report({"type": "TPU", "color": [7, 8, 9], "loaded": 1})
        daemon.apply_multi_color_box_report({"multi_color_box": []})
        self.assertFalse(daemon.telemetry["has_multi_color_box"])
        self.assertEqual([box["source"] for box in daemon.telemetry["material_boxes"]], ["external"])

    def test_material_updates_keep_reported_ace_box_id(self):
        messages = daemon.build_material_update_messages([
            {"source": "ace", "box_id": 2, "index": 3, "type": "PETG", "color": "#010203"}
        ])
        suffix, payload = messages[0]
        self.assertEqual(suffix, "multiColorBox")
        self.assertEqual(payload["data"]["multi_color_box"][0]["id"], 2)
        self.assertEqual(payload["data"]["multi_color_box"][0]["slots"][0]["index"], 3)

    def test_external_spool_updates_use_extfilbox_protocol(self):
        messages = daemon.build_material_update_messages([
            {"source": "external", "box_id": -1, "index": 0, "type": "TPU", "color": "#070809"}
        ])
        suffix, payload = messages[0]
        self.assertEqual(suffix, "extfilbox")
        self.assertEqual(payload["action"], "setInfo")
        self.assertEqual(payload["data"], {"type": "TPU", "color": [7, 8, 9]})

    def test_partial_external_ack_preserves_known_material(self):
        daemon.apply_external_spool_report({"type": "TPU", "color": [7, 8, 9], "loaded": 1, "edit_status": 1})
        daemon.apply_external_spool_report({"loaded": 1})
        slot = daemon.telemetry["external_spool"]
        self.assertEqual(slot["type"], "TPU")
        self.assertEqual(slot["color"], "#070809")
        self.assertTrue(slot["available"])

    def test_material_queries_request_peripheral_ace_and_external_spool_state(self):
        class FakeClient:
            def __init__(self):
                self.messages = []

            def publish(self, topic, body):
                self.messages.append((topic.rsplit("/", 1)[-1], json.loads(body)["action"]))

        previous = daemon.mqtt_client
        daemon.mqtt_client = FakeClient()
        try:
            daemon.query_all()
            self.assertIn(("peripherie", "query"), daemon.mqtt_client.messages)
            self.assertIn(("multiColorBox", "getInfo"), daemon.mqtt_client.messages)
            self.assertIn(("extfilbox", "getInfo"), daemon.mqtt_client.messages)
        finally:
            daemon.mqtt_client = previous

    def test_kobra_x_builtin_slots_classified_as_rack_with_zero_based_indices(self):
        # Kobra X reports head_tools_model: 1 and a box with id: -1 and 4 slots.
        # This must be treated as the built-in rack with slots 0, 1, 2, 3 (not external with -1).
        daemon.apply_multi_color_box_report({
            "head_tools_model": 1,
            "multi_color_box": [{
                "id": -1,
                "model_id": 40002,
                "loaded_slot": -1,
                "slots": [
                    {"index": 0, "type": "PLA", "color": [35, 163, 199]},
                    {"index": 1, "type": "PLA", "color": [117, 120, 123]},
                    {"index": 2, "type": "PLA", "color": [253, 219, 39]},
                    {"index": 3, "type": "PLA", "color": [215, 217, 210]},
                ]
            }]
        })
        self.assertEqual(len(daemon.telemetry["material_boxes"]), 1)
        box = daemon.telemetry["material_boxes"][0]
        self.assertEqual(box["source"], "rack")
        self.assertEqual([s["slot"] for s in box["slots"]], [0, 1, 2, 3])
        self.assertEqual([s["box_slot"] for s in box["slots"]], [0, 1, 2, 3])
        self.assertTrue(all(s["available"] for s in box["slots"]))

    def test_partial_setinfo_ack_does_not_replace_complete_ace_snapshot(self):
        daemon.apply_multi_color_box_report({
            "multi_color_box": [
                {"id": 0, "slots": [{"index": 0, "type": "PLA", "edit_status": 1}]},
                {"id": 1, "slots": [{"index": 0, "type": "PETG", "edit_status": 1}]},
            ]
        })
        class Message:
            topic = "anycubic/anycubicCloud/v1/printer/public/20030/device/multiColorBox/report"
            payload = json.dumps({
                "type": "multiColorBox", "action": "setInfo", "code": 200,
                "data": {"multi_color_box": [{"id": 0, "slots": [{"index": 0, "type": "ABS"}]}]},
            }).encode()

        daemon.on_mqtt_message(None, None, Message())
        boxes = daemon.telemetry["material_boxes"]
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0]["slots"][0]["type"], "ABS")
        self.assertEqual(boxes[1]["slots"][0]["type"], "PETG")

    def test_camera_telemetry_urls_configured(self):
        self.assertTrue(daemon.telemetry["camera_url"].endswith("/camera"))
        self.assertIn("18088/flv", daemon.telemetry["raw_camera_url"])

    def test_send_video_command_publishes_to_both_video_topics(self):
        class FakeClient:
            def __init__(self):
                self.messages = []

            def publish(self, topic, body):
                self.messages.append((topic, json.loads(body)["action"]))

        previous = daemon.mqtt_client
        daemon.mqtt_client = FakeClient()
        try:
            daemon.send_video_command("startCapture")
            self.assertEqual(len(daemon.mqtt_client.messages), 2)
            topics = [m[0] for m in daemon.mqtt_client.messages]
            self.assertTrue(any("/web/printer/" in t and t.endswith("/video") for t in topics))
            self.assertTrue(any("/slicer/printer/" in t and t.endswith("/video") for t in topics))
            self.assertTrue(all(m[1] == "startCapture" for m in daemon.mqtt_client.messages))
        finally:
            daemon.mqtt_client = previous

    def test_light_control_nozzle_and_camera(self):
        class FakeClient:
            def __init__(self):
                self.published = []

            def publish(self, topic, body):
                self.published.append((topic, json.loads(body)))

        previous = daemon.mqtt_client
        fake = FakeClient()
        daemon.mqtt_client = fake
        daemon.telemetry["connected"] = True
        try:
            server = daemon.BridgeServer
            # Test camera light control
            body_cam = json.dumps({"action": "camera_light", "status": 1}).encode("utf-8")
            class FakeReqCam:
                def __init__(self):
                    self.headers = {"Content-Length": str(len(body_cam)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                    self.rfile = io.BytesIO(body_cam)
                    self.wfile = io.BytesIO()
                    self.client_address = ("127.0.0.1", 12345)
                    self.path = "/control"
                def require_authorized(self): return True
                def send_response(self, code): self.code = code
                def send_header(self, k, v): pass
                def end_headers(self): pass

            req_cam = FakeReqCam()
            server.do_POST(req_cam)
            self.assertEqual(daemon.telemetry["camera_light"], 1)
            self.assertTrue(any(msg[1]["data"]["type"] == 2 for msg in fake.published))

            # Test nozzle light control
            fake.published.clear()
            body_nozzle = json.dumps({"action": "nozzle_light", "status": 1}).encode("utf-8")
            class FakeReqNozzle:
                def __init__(self):
                    self.headers = {"Content-Length": str(len(body_nozzle)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                    self.rfile = io.BytesIO(body_nozzle)
                    self.wfile = io.BytesIO()
                    self.client_address = ("127.0.0.1", 12345)
                    self.path = "/control"
                def require_authorized(self): return True
                def send_response(self, code): self.code = code
                def send_header(self, k, v): pass
                def end_headers(self): pass

            req_nozzle = FakeReqNozzle()
            server.do_POST(req_nozzle)
            self.assertEqual(daemon.telemetry["nozzle_light"], 1)
            self.assertTrue(any(msg[1]["data"]["type"] == 1 for msg in fake.published))
        finally:
            daemon.mqtt_client = previous

    def test_set_camera_url_action(self):
        body = json.dumps({"action": "set_camera_url", "url": "http://192.168.1.50:8080/stream"}).encode("utf-8")
        class FakeReq:
            def __init__(self):
                self.headers = {"Content-Length": str(len(body)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                self.rfile = io.BytesIO(body)
                self.wfile = io.BytesIO()
                self.client_address = ("127.0.0.1", 12345)
                self.path = "/control"
            def require_authorized(self): return True
            def send_response(self, code): self.code = code
            def send_header(self, k, v): pass
            def end_headers(self): pass

        req = FakeReq()
        daemon.BridgeServer.do_POST(req)
        self.assertEqual(daemon.telemetry["custom_camera_url"], "http://192.168.1.50:8080/stream")

    def test_ace_drying_and_backup_telemetry_reporting(self):
        daemon.apply_multi_color_box_report({
            "multi_color_box": [{
                "id": 0,
                "model_id": 40001,
                "loaded_slot": 1,
                "temp": 48,
                "auto_feed": 1,
                "drying_status": {
                    "status": 1,
                    "target_temp": 50,
                    "duration": 240,
                    "remain_time": 180,
                },
                "slots": [
                    {"index": 0, "type": "PLA", "color": [255, 255, 255]},
                ]
            }]
        })
        self.assertEqual(daemon.telemetry["ace_temp"], 48)
        self.assertEqual(daemon.telemetry["ace_auto_feed"], 1)
        self.assertEqual(daemon.telemetry["ace_drying"], {
            "status": 1,
            "target_temp": 50,
            "duration": 240,
            "remain_time": 180,
        })
        self.assertTrue(len(daemon.telemetry["material_boxes"]) > 0)
        box = daemon.telemetry["material_boxes"][0]
        self.assertEqual(box["temp"], 48)
        self.assertEqual(box["auto_feed"], 1)
        self.assertEqual(box["drying_status"]["status"], 1)
        self.assertEqual(box["drying_status"]["target_temp"], 50)

    def test_set_ace_drying_control_action(self):
        class FakeClient:
            def __init__(self):
                self.published = []

            def publish(self, topic, body):
                self.published.append((topic, json.loads(body)))

        previous = daemon.mqtt_client
        fake = FakeClient()
        daemon.mqtt_client = fake
        daemon.telemetry["connected"] = True
        try:
            body = json.dumps({
                "action": "set_ace_drying",
                "box_id": 0,
                "status": 1,
                "target_temp": 50,
                "duration": 4
            }).encode("utf-8")

            class FakeReq:
                def __init__(self):
                    self.headers = {"Content-Length": str(len(body)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                    self.rfile = io.BytesIO(body)
                    self.wfile = io.BytesIO()
                    self.client_address = ("127.0.0.1", 12345)
                    self.path = "/control"
                def require_authorized(self): return True
                def send_response(self, code): self.code = code
                def send_header(self, k, v): pass
                def end_headers(self): pass

            req = FakeReq()
            daemon.BridgeServer.do_POST(req)
            self.assertEqual(daemon.telemetry["ace_drying"]["status"], 1)
            self.assertEqual(daemon.telemetry["ace_drying"]["target_temp"], 50)
            self.assertEqual(daemon.telemetry["ace_drying"]["duration"], 240)
            self.assertTrue(any(
                msg[1].get("action") == "setDry" and
                msg[1].get("type") == "multiColorBox" and
                msg[1]["data"]["multi_color_box"][0]["drying_status"]["target_temp"] == 50
                for msg in fake.published
            ))
        finally:
            daemon.mqtt_client = previous

    def test_set_ace_auto_feed_control_action(self):
        class FakeClient:
            def __init__(self):
                self.published = []

            def publish(self, topic, body):
                self.published.append((topic, json.loads(body)))

        previous = daemon.mqtt_client
        fake = FakeClient()
        daemon.mqtt_client = fake
        daemon.telemetry["connected"] = True
        try:
            body = json.dumps({
                "action": "set_ace_auto_feed",
                "box_id": 0,
                "auto_feed": 1
            }).encode("utf-8")

            class FakeReq:
                def __init__(self):
                    self.headers = {"Content-Length": str(len(body)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                    self.rfile = io.BytesIO(body)
                    self.wfile = io.BytesIO()
                    self.client_address = ("127.0.0.1", 12345)
                    self.path = "/control"
                def require_authorized(self): return True
                def send_response(self, code): self.code = code
                def send_header(self, k, v): pass
                def end_headers(self): pass

            req = FakeReq()
            daemon.BridgeServer.do_POST(req)
            self.assertEqual(daemon.telemetry["ace_auto_feed"], 1)
            self.assertTrue(any(
                msg[1].get("action") == "setAutoFeed" and
                msg[1].get("type") == "multiColorBox" and
                msg[1]["data"]["multi_color_box"][0]["auto_feed"] == 1
                for msg in fake.published
            ))
        finally:
            daemon.mqtt_client = previous

    def test_active_filament_slot_stability_with_external_spool(self):
        # Slot 2 is currently loaded in extruder (status 5)
        daemon.telemetry["active_slot"] = -1
        daemon.telemetry["channel_index"] = -1
        daemon.telemetry["feed_status"] = None
        daemon.apply_multi_color_box_report({
            "multi_color_box": [{
                "id": 0,
                "loaded_slot": 2,
                "slots": [
                    {"index": 0, "type": "PLA", "status": 1, "color": [255, 0, 0]},
                    {"index": 1, "type": "PLA", "status": 1, "color": [0, 255, 0]},
                    {"index": 2, "type": "PETG", "status": 5, "color": [0, 0, 255]},
                    {"index": 3, "type": "PLA", "status": 1, "color": [255, 255, 0]},
                ]
            }]
        })
        self.assertEqual(daemon.telemetry["active_slot"], 2)
        slots = [s for s in daemon.telemetry["filaments"] if s.get("source") != "external"]
        self.assertFalse(slots[0]["is_active"])
        self.assertFalse(slots[1]["is_active"])
        self.assertTrue(slots[2]["is_active"])
        self.assertFalse(slots[3]["is_active"])

        # External spool query reports idle (status_type 0)
        daemon.apply_external_spool_report({
            "loaded_slot": 0,
            "status_type": 0,
            "current_status": 0,
            "type": "PLA",
            "color": [200, 200, 200],
            "loaded": False
        })
        # Feed status should NOT be set to slot 0 for an idle external spool
        self.assertIsNone(daemon.telemetry.get("feed_status"))
        self.assertEqual(daemon.telemetry["active_slot"], 2)

        # Subsequent multiColorBox query (which might omit loaded_slot or have loaded_slot: -1)
        daemon.apply_multi_color_box_report({
            "multi_color_box": [{
                "id": 0,
                "loaded_slot": -1,
                "slots": [
                    {"index": 0, "type": "PLA", "status": 1, "color": [255, 0, 0]},
                    {"index": 1, "type": "PLA", "status": 1, "color": [0, 255, 0]},
                    {"index": 2, "type": "PETG", "status": 5, "color": [0, 0, 255]},
                    {"index": 3, "type": "PLA", "status": 1, "color": [255, 255, 0]},
                ]
            }]
        })
        # Active slot must STILL be 2, without flipping to 0
        self.assertEqual(daemon.telemetry["active_slot"], 2)
        ace_slots = [s for s in daemon.telemetry["filaments"] if s.get("source") != "external"]
        self.assertFalse(ace_slots[0]["is_active"])
        self.assertTrue(ace_slots[2]["is_active"])

    def test_single_active_slot_when_all_slots_have_filament(self):
        # All 4 slots have filament loaded in ACE (status 2). Extruder channel is slot 3 (index 2).
        daemon.telemetry["active_slot"] = -1
        daemon.telemetry["channel_index"] = 2
        daemon.telemetry["feed_status"] = None
        daemon.apply_multi_color_box_report({
            "multi_color_box": [{
                "id": 0,
                "loaded_slot": -1,
                "slots": [
                    {"index": 0, "type": "PETG", "status": 2, "color": [0, 0, 0]},
                    {"index": 1, "type": "PETG", "status": 2, "color": [0, 100, 0]},
                    {"index": 2, "type": "ABS", "status": 2, "color": [255, 255, 0]},
                    {"index": 3, "type": "ABS", "status": 2, "color": [0, 255, 0]},
                ]
            }]
        })
        self.assertEqual(daemon.telemetry["active_slot"], 2)
        ace_slots = [s for s in daemon.telemetry["filaments"] if s.get("source") != "external"]
        self.assertEqual(len(ace_slots), 4)
        # Exactly one slot is active: slot 3 (index 2)
        self.assertFalse(ace_slots[0]["is_active"])
        self.assertFalse(ace_slots[1]["is_active"])
        self.assertTrue(ace_slots[2]["is_active"])
        self.assertFalse(ace_slots[3]["is_active"])

    def test_is_command_filename_detection(self):
        self.assertTrue(daemon.is_command_filename("_orcacubic_command_1792450398_4k7a.gcode"))
        self.assertTrue(daemon.is_command_filename("/local/_orcacubic_command_test.gcode"))
        self.assertTrue(daemon.is_command_filename("_command_extrude.gcode"))
        self.assertFalse(daemon.is_command_filename("benchy.gcode"))
        self.assertFalse(daemon.is_command_filename("Plate_1.gcode.3mf"))
        self.assertFalse(daemon.is_command_filename("Ready"))
        self.assertFalse(daemon.is_command_filename(""))
        self.assertFalse(daemon.is_command_filename(None))

    def test_internal_command_file_suppressed_from_telemetry_info(self):
        class MockMsg:
            def __init__(self, topic, data):
                self.topic = topic
                self.payload = json.dumps(data).encode("utf-8")

        daemon.model_id = "test_model"
        daemon.device_id = "test_device"
        daemon.telemetry["job_name"] = "Ready"
        daemon.telemetry["total_layers"] = 0
        daemon.telemetry["curr_layer"] = 0
        daemon.telemetry["progress"] = 0
        daemon.telemetry["state"] = "free"

        info_payload = {
            "code": 200,
            "data": {
                "project": {
                    "filename": "_orcacubic_command_1792450398_4k7a.gcode",
                    "state": 0,
                    "curr_layer": 0,
                    "total_layers": 1,
                    "progress": 0
                }
            }
        }
        msg = MockMsg("anycubic/anycubicCloud/v1/printer/public/test_model/test_device/info/report", info_payload)
        daemon.on_mqtt_message(None, None, msg)

        # Internal command file must NEVER pollute job_name or total_layers
        self.assertEqual(daemon.telemetry["job_name"], "Ready")
        self.assertEqual(daemon.telemetry["total_layers"], 0)
        self.assertEqual(daemon.telemetry["curr_layer"], 0)
        self.assertEqual(daemon.telemetry["progress"], 0)
        self.assertEqual(daemon.public_telemetry()["job_name"], "Ready")

    def test_real_print_job_updates_telemetry_info(self):
        class MockMsg:
            def __init__(self, topic, data):
                self.topic = topic
                self.payload = json.dumps(data).encode("utf-8")

        daemon.model_id = "test_model"
        daemon.device_id = "test_device"
        info_payload = {
            "code": 200,
            "data": {
                "project": {
                    "filename": "benchy.gcode",
                    "state": "printing",
                    "curr_layer": 15,
                    "total_layers": 200,
                    "progress": 7.5
                }
            }
        }
        msg = MockMsg("anycubic/anycubicCloud/v1/printer/public/test_model/test_device/info/report", info_payload)
        daemon.on_mqtt_message(None, None, msg)

        self.assertEqual(daemon.telemetry["job_name"], "benchy.gcode")
        self.assertEqual(daemon.telemetry["curr_layer"], 15)
        self.assertEqual(daemon.telemetry["total_layers"], 200)
        self.assertEqual(daemon.telemetry["progress"], 7.5)
        self.assertEqual(daemon.public_telemetry()["job_name"], "benchy.gcode")

    def test_public_telemetry_sanitizes_stale_command_filename(self):
        daemon.telemetry["job_name"] = "_orcacubic_command_stale.gcode"
        daemon.telemetry["state"] = "free"
        daemon.telemetry["total_layers"] = 1
        pub = daemon.public_telemetry()
        self.assertEqual(pub["job_name"], "Ready")
        self.assertEqual(pub["total_layers"], 0)

    def test_parse_print_time_from_bytes(self):
        sample = b"; HEADER_BLOCK_START\n; print_time = 1h 25m 30s\n; total layer number: 120\n; HEADER_BLOCK_END\n"
        mins = daemon._parse_print_time_from_bytes(sample)
        self.assertEqual(mins, 86)

        sample2 = b"; estimated printing time (normal mode) = 2h 10m\n"
        self.assertEqual(daemon._parse_print_time_from_bytes(sample2), 130)

        sample3 = b"; total estimated time: 45m 10s\n"
        self.assertEqual(daemon._parse_print_time_from_bytes(sample3), 45)

    def test_start_print_job_records_estimated_time_and_calculates_remain(self):
        class MockMqtt:
            def __init__(self):
                self.published = []
            def publish(self, topic, payload):
                self.published.append((topic, payload))

        old_client = daemon.mqtt_client
        daemon.mqtt_client = MockMqtt()
        daemon.model_id = "test_model"
        daemon.device_id = "test_device"
        daemon.telemetry["connected"] = True
        daemon.telemetry["state"] = "printing"
        daemon.telemetry["progress"] = 25.0
        daemon.telemetry["remain_time"] = 0

        try:
            body = json.dumps({
                "action": "start_print_job",
                "filename": "dragon.gcode",
                "estimated_print_time_min": 120,
                "total_layers": 300
            }).encode("utf-8")
            class FakeReq:
                def __init__(self):
                    self.headers = {"Content-Length": str(len(body)), "X-OrcaCubic-Token": daemon.BRIDGE_TOKEN}
                    self.rfile = io.BytesIO(body)
                    self.wfile = io.BytesIO()
                    self.client_address = ("127.0.0.1", 12345)
                    self.path = "/control"
                def require_authorized(self): return True
                def send_response(self, code): self.code = code
                def send_header(self, k, v): pass
                def end_headers(self): pass

            daemon.BridgeServer.do_POST(FakeReq())

            self.assertEqual(daemon.telemetry["total_print_time"], 120)
            self.assertEqual(daemon.job_estimated_times.get("dragon.gcode"), 120)

            # Simulate MQTT telemetry update reporting 50% progress but remain_time=0
            class MockMsg:
                def __init__(self, topic, data):
                    self.topic = topic
                    self.payload = json.dumps(data).encode("utf-8")

            info_payload = {
                "code": 200,
                "data": {
                    "project": {
                        "filename": "dragon.gcode",
                        "state": "printing",
                        "curr_layer": 150,
                        "total_layers": 300,
                        "progress": 50.0,
                        "remain_time": 0
                    }
                }
            }
            msg = MockMsg("anycubic/anycubicCloud/v1/printer/public/test_model/test_device/info/report", info_payload)
            daemon.on_mqtt_message(None, None, msg)

            # 120 min total * (100 - 50)% = 60 min remaining
            self.assertEqual(daemon.telemetry["remain_time"], 60)
            pub = daemon.public_telemetry()
            self.assertEqual(pub["remain_time"], 60)
            self.assertNotEqual(pub["estimated_end_time"], "--")
        finally:
            daemon.mqtt_client = old_client


if __name__ == "__main__":
    unittest.main()

