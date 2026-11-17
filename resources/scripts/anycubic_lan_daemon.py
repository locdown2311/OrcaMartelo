import sys
import os
import time
import http.client
import io
import json
import re
import base64
import random
import string
import hashlib
import urllib.request
import urllib.parse
import ssl
import socket
import tempfile
try:
    from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
except ImportError:
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
import threading

# The public build vendors these two small runtime dependencies beside this script.
# Keep the bridge self-contained when it is launched by OrcaCubic's bundled Python.
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

import pyaes
import paho.mqtt.client as mqtt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import orcamartelo_mcp

PRINTER_IP = os.environ.get("ORCACUBIC_PRINTER_IP", "").strip()
if len(sys.argv) > 1 and sys.argv[1].strip():
    PRINTER_IP = sys.argv[1].strip()
if not PRINTER_IP:
    raise SystemExit("Usage: anycubic_lan_daemon.py <printer-ip> (or set ORCACUBIC_PRINTER_IP)")
BRIDGE_TOKEN = os.environ.get("ORCACUBIC_BRIDGE_TOKEN", "").strip()
if len(sys.argv) > 2 and sys.argv[2].strip():
    BRIDGE_TOKEN = sys.argv[2].strip()
if not BRIDGE_TOKEN:
    raise SystemExit("Missing OrcaCubic bridge authentication token")
PRINTER_HTTP_PORT = 18910
PRINTER_MQTT_PORT = 9883
HTTP_PORT = 18988

# A user-picked print file is streamed to the bridge as base64 JSON. Cap it so a
# large file cannot exhaust memory; the request cap leaves room for base64 growth.
MAX_UPLOAD_FILE_BYTES = 512 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_FILE_BYTES * 2

telemetry = {
    "connected": False,
    "ip": PRINTER_IP,
    "model": "Anycubic Printer",
    "model_id": "",
    "firmware": "",
    "cn": "",
    "state": "idle",
    "progress": 0,
    "job_name": "Ready",
    "curr_layer": 0,
    "total_layers": 0,
    "print_time": 0,
    "remain_time": 0,
    "total_print_time": 0,
    "nozzle_temp": 0,
    "target_nozzle_temp": 0,
    "bed_temp": 0,
    "target_bed_temp": 0,
    "camera_url": f"http://127.0.0.1:{HTTP_PORT}/camera",
    "raw_camera_url": f"http://{PRINTER_IP}:18088/flv",
    "custom_camera_url": "",
    "upload_url": f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload",
    "token": "",
    "light": 1,
    "nozzle_light": 0,
    "camera_light": 1,
    "fan": 0,
    "speed_mode": 2,
    "has_multi_color_box": False,
    "head_tools_model": None,
    "max_box_num": 0,
    "material_boxes": [],
    "external_spool": None,
    "filaments": [],
    "feed_status": {"box_id": None, "slot_index": -1, "type": 0, "current_status": 0, "code": 0},
    "channel_index": -1,
    "channel_status": 0,
    "has_filaments": 0,
    "channel_report_seq": 0,
    "active_slot": -1,
    "loaded_slot": -1
}

def _parse_total_layers_from_bytes(content_bytes):
    try:
        sample = content_bytes[:1048576]
        if len(content_bytes) > 2097152:
            sample += content_bytes[-1048576:]
        sample_str = sample.decode("utf-8", errors="ignore")
        import re
        m = re.search(r";\s*total layer number:\s*(\d+)", sample_str, re.IGNORECASE)
        if not m:
            m = re.search(r";\s*total_layers\s*=\s*(\d+)", sample_str, re.IGNORECASE)
        if not m:
            m = re.search(r";\s*total_layer_count\s*=\s*(\d+)", sample_str, re.IGNORECASE)
        if not m:
            m = re.search(r"TOTAL_LAYER=(\d+)", sample_str)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0

def _parse_time_str_to_minutes(val):
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(round(val))
    val_str = str(val).strip()
    import re
    d = re.search(r'(\d+)\s*d', val_str, re.IGNORECASE)
    h = re.search(r'(\d+)\s*h', val_str, re.IGNORECASE)
    m = re.search(r'(\d+)\s*m', val_str, re.IGNORECASE)
    s = re.search(r'(\d+)\s*s', val_str, re.IGNORECASE)
    if d or h or m or s:
        days = int(d.group(1)) if d else 0
        hours = int(h.group(1)) if h else 0
        mins = int(m.group(1)) if m else 0
        secs = int(s.group(1)) if s else 0
        return days * 1440 + hours * 60 + mins + (1 if secs >= 30 else 0)
    parts = val_str.split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 60 + int(parts[1]) + (1 if int(parts[2]) >= 30 else 0)
        except Exception:
            pass
    elif len(parts) == 2:
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass
    try:
        num = float(val_str)
        if num > 14400:
            return int(round(num / 60))
        return int(round(num))
    except Exception:
        pass
    return 0

def _parse_print_time_from_bytes(content_bytes):
    try:
        sample = content_bytes[:1048576]
        if len(content_bytes) > 2097152:
            sample += content_bytes[-1048576:]
        sample_str = sample.decode("utf-8", errors="ignore")
        import re
        patterns = [
            r";\s*print_time\s*=\s*([^\r\n;]+)",
            r";\s*estimated printing time[^=\r\n]*=\s*([^\r\n;]+)",
            r";\s*total estimated time:\s*([^\r\n;]+)",
            r";\s*model printing time:\s*([^\r\n;]+)",
        ]
        for pat in patterns:
            m = re.search(pat, sample_str, re.IGNORECASE)
            if m:
                mins = _parse_time_str_to_minutes(m.group(1).strip())
                if mins > 0:
                    return mins
    except Exception:
        pass
    return 0

mqtt_client = None
creds = {}
model_id = ""
device_id = ""
pending_cleanup = []
shutdown_event = threading.Event()
server_instance = None
slot_brand_overrides = {}
job_estimated_times = {}

def is_command_filename(filename):
    if not filename or not isinstance(filename, str):
        return False
    base = os.path.basename(filename).strip()
    return (
        base.startswith(("_orcacubic_command_", "_command_"))
        or "_orcacubic_command_" in base
        or "_command_" in base
    )

STATE_CODE_MAP = {
    0: "free",
    1: "busy",
    2: "printing",
    3: "paused",
    4: "completed",
    5: "stopped",
}


def normalize_printer_state(state_value):
    if isinstance(state_value, int):
        return STATE_CODE_MAP.get(state_value, "free" if state_value == 0 else str(state_value))
    if isinstance(state_value, str):
        val = state_value.strip().lower()
        if val in ("0", "free", "ready", "idle", "standby"):
            return "free"
        if val in ("1", "busy", "preparing", "heating"):
            return "busy"
        if val in ("2", "printing"):
            return "printing"
        if val in ("3", "paused"):
            return "paused"
        if val in ("4", "completed", "finish", "finished"):
            return "completed"
        if val in ("5", "stopped", "stop", "cancel", "cancelled"):
            return "stopped"
        return val
    return "free"


def public_telemetry():
    """Return dashboard telemetry without exposing token-bearing printer URLs."""
    res = {key: value for key, value in telemetry.items() if key != "upload_url"}
    res["state"] = normalize_printer_state(res.get("state", "free"))
    if is_command_filename(res.get("job_name")):
        res["job_name"] = "Ready"
        st = str(res.get("state", "")).lower()
        if st in ("free", "ready", "idle", "0", "completed", "finish", "finished", "stop", "stopped"):
            res["curr_layer"] = 0
            res["total_layers"] = 0
            res["progress"] = 0
            res["print_time"] = 0
            res["remain_time"] = 0
    rem = _as_int(res.get("remain_time", 0))
    st = str(res.get("state", "")).lower()
    if rem > 0 and st in ("printing", "busy", "paused"):
        end_ts = int(time.time() + rem * 60)
        res["estimated_end_time"] = time.strftime("%H:%M", time.localtime(end_ts))
        res["estimated_end_timestamp"] = end_ts
    else:
        res["estimated_end_time"] = "--"
        res["estimated_end_timestamp"] = 0
    return res

def rgb_to_hex(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"


def _as_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_rgb(value, fallback=(175, 175, 175)):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        value = fallback
    return [max(0, min(255, _as_int(value[i], fallback[i]))) for i in range(3)]


def _slot_has_material(slot):
    if not isinstance(slot, dict):
        return False
    if "available" in slot:
        return bool(slot.get("available"))
    if slot.get("_empty") is True:
        return False
    if _as_int(slot.get("status"), -1) in (0, 4) or _as_int(slot.get("edit_status"), -1) == 2:
        return False
    material = str(slot.get("type") or "").strip()
    return material not in ("", "?")


def _normalize_slot(slot, box_id, source, loaded_slot=-1, slot_offset=0, active_slot=-1):
    local_index = max(0, _as_int(slot.get("index", slot.get("box_slot")), 0))
    raw_color = slot.get("color", [175, 175, 175])
    if isinstance(raw_color, str) and raw_color.startswith("#") and len(raw_color) >= 7:
        raw_color = [int(raw_color[1:3], 16), int(raw_color[3:5], 16), int(raw_color[5:7], 16)]
    color = _normalize_rgb(raw_color, (175, 175, 175))
    material = str(slot.get("type") or "?").strip() or "?"
    color_group = []
    for group_color in slot.get("color_group", []) or []:
        if isinstance(group_color, (list, tuple)) and len(group_color) >= 3:
            color_group.append(rgb_to_hex(*_normalize_rgb(group_color)))
    icon_type = _as_int(slot.get("icon_type"), 0)
    global_slot = (slot_offset + local_index) if source not in ("external", "external_mcb") else -1
    override_brand = slot_brand_overrides.get((box_id, local_index)) or slot_brand_overrides.get(local_index) or slot_brand_overrides.get(global_slot)
    raw_brand = str(override_brand or slot.get("brand") or slot.get("brand_name") or "Anycubic").strip()
    if raw_brand.lower() == "custom":
        brand = "Custom"
    elif raw_brand.lower() == "generic":
        brand = "Generic"
    elif raw_brand.lower() == "anycubic":
        brand = "Anycubic"
    else:
        brand = raw_brand or "Anycubic"
    is_external = source in ("external", "external_mcb")
    is_active = (local_index == active_slot and active_slot >= 0) if not is_external else False
    is_loaded = bool(slot.get("loaded")) or (loaded_slot >= 0 and local_index == loaded_slot)
    return {
        "slot": global_slot,
        "box_id": box_id,
        "box_slot": local_index,
        "source": source,
        "color": rgb_to_hex(*color),
        "type": material,
        "temp": 235 if "PETG" in material.upper() or "+" in material else 210,
        "loaded": is_loaded,
        "is_active": is_active,
        "available": _slot_has_material(slot),
        "brand": brand,
        "icon_type": icon_type,
        "finish_type": "luminous" if icon_type == 3 else ("gradient" if icon_type in (1, 2) else "solid"),
        "color_group_hex": color_group or [rgb_to_hex(*color)],
    }


def _merge_reported_slots(existing_slots, reported_slots):
    by_index = {int(slot.get("index", slot.get("box_slot", 0))): dict(slot) for slot in existing_slots or []}
    for reported in reported_slots or []:
        index = _as_int(reported.get("index"), 0)
        by_index[index] = {**by_index.get(index, {}), **reported}
    return [by_index[index] for index in sorted(by_index)]


def apply_multi_color_box_report(data, code=200, replace=True):
    box_list = data.get("multi_color_box", []) if isinstance(data, dict) else []
    if not isinstance(box_list, list):
        return
    if replace:
        telemetry["has_multi_color_box"] = len(box_list) > 0
    elif box_list:
        telemetry["has_multi_color_box"] = True
    telemetry["head_tools_model"] = data.get("head_tools_model", telemetry.get("head_tools_model"))
    telemetry["max_box_num"] = _as_int(data.get("max_box_num"), telemetry.get("max_box_num") or len(box_list))
    external_boxes = [dict(box) for box in telemetry.get("material_boxes", []) if box.get("source") == "external"]
    boxes = [] if replace else [dict(box) for box in telemetry.get("material_boxes", []) if box.get("source") != "external"]
    for reported in box_list:
        if not isinstance(reported, dict):
            continue
        box_id = _as_int(reported.get("id"), -1)
        slots = reported.get("slots") or []
        if not isinstance(slots, list):
            slots = []

        loaded_slot = _as_int(reported.get("loaded_slot"), -1)
        active_channel = -1

        # Priority 1: channel_index from extrudeControl (authoritative active extruder channel)
        if telemetry.get("channel_index", -1) >= 0:
            active_channel = telemetry["channel_index"]
        # Priority 2: box reported loaded_slot
        elif loaded_slot >= 0:
            active_channel = loaded_slot
        # Priority 3: current active_slot
        elif telemetry.get("active_slot", -1) >= 0:
            active_channel = telemetry["active_slot"]
        # Priority 4: active feed_status for this box
        else:
            feed_status = telemetry.get("feed_status")
            if isinstance(feed_status, dict):
                feed_box = _as_int(feed_status.get("box_id"), -99)
                feed_type = _as_int(feed_status.get("type"), 0)
                feed_slot = _as_int(feed_status.get("slot_index"), -1)
                if feed_box == box_id and feed_type in (1, 2) and feed_slot >= 0:
                    active_channel = feed_slot

        if box_id >= 0:
            if active_channel >= 0:
                telemetry["active_slot"] = active_channel
                telemetry["loaded_slot"] = active_channel
            else:
                telemetry["loaded_slot"] = loaded_slot
        all_boxes = list(boxes) + [b for b in box_list if isinstance(b, dict)]
        has_ace_box = any(_as_int(box.get("id"), -1) >= 0 for box in all_boxes if isinstance(box, dict))
        has_builtin_rack = any(_as_int(box.get("id"), -1) < 0 and len(box.get("slots") or []) > 1 for box in all_boxes if isinstance(box, dict))
        if box_id < 0:
            # An entry with negative id is only an external spool placeholder if an ACE unit is also connected
            # AND it has at most 1 slot. Otherwise, it is the printer's built-in multi-color rack.
            source = "external_mcb" if (has_ace_box and len(slots) <= 1) else "rack"
            slot_offset = 0
        else:
            source = "ace"
            slot_offset = (4 if has_builtin_rack else 0) + box_id * 4
        previous = next((box for box in boxes if _as_int(box.get("id"), -99) == box_id), None)
        if previous is not None and not replace:
            if previous.get("source") in ("rack", "external_mcb"):
                source = previous["source"]
            slots = _merge_reported_slots(previous.get("slots", []), slots)
            boxes.remove(previous)
        normalized_slots = [_normalize_slot(slot, box_id, source, loaded_slot, slot_offset, active_channel) for slot in sorted(slots, key=lambda slot: _as_int(slot.get("index", slot.get("box_slot", 0))))]
        drying_status_raw = reported.get("drying_status") or {}
        box_temp = _as_int(reported.get("temp", reported.get("cur_temp", 0)), 0)
        auto_feed = _as_int(reported.get("auto_feed", 0), 0)
        drying_info = {
            "status": _as_int(drying_status_raw.get("status", 0), 0),
            "target_temp": _as_int(drying_status_raw.get("target_temp", 0), 0),
            "duration": _as_int(drying_status_raw.get("duration", 0), 0),
            "remain_time": _as_int(drying_status_raw.get("remain_time", 0), 0),
        }
        boxes.append({
            "id": box_id,
            "source": source,
            "model_id": reported.get("model_id"),
            "loaded_slot": loaded_slot,
            "temp": box_temp,
            "auto_feed": auto_feed,
            "drying_status": drying_info,
            "slots": normalized_slots,
        })
        if box_id >= 0:
            telemetry["ace_drying"] = drying_info
            telemetry["ace_auto_feed"] = auto_feed
            telemetry["ace_temp"] = box_temp
            if reported.get("humidity") is not None:
                telemetry["ace_humidity"] = _as_int(reported.get("humidity"), 0)
        reported_feed = reported.get("feed_status")
        if reported_feed:
            telemetry["feed_status"] = {
                "box_id": box_id,
                "slot_index": _as_int(reported_feed.get("slot_index"), -1),
                "type": _as_int(reported_feed.get("type"), 0),
                "current_status": _as_int(reported_feed.get("current_status"), 0),
                "code": _as_int(reported_feed.get("code"), code),
            }
    if telemetry.get("external_spool") is not None and not any(box.get("source") == "external" for box in boxes):
        boxes = boxes + external_boxes
    boxes.sort(key=lambda box: (box.get("source") in ("external", "external_mcb"), _as_int(box.get("id"), -1)))
    telemetry["material_boxes"] = boxes
    telemetry["filaments"] = [slot for box in boxes for slot in box.get("slots", [])]
    if replace and not box_list and telemetry.get("external_spool") is not None:
        external = telemetry["external_spool"]
        telemetry["material_boxes"] = [{"id": -1, "source": "external", "loaded_slot": external.get("box_slot", 0), "slots": [external]}]
        telemetry["filaments"] = [external]


def apply_external_spool_report(data, code=200):
    if not isinstance(data, dict):
        return
    previous = telemetry.get("external_spool") or {}
    slot_index = max(0, _as_int(data.get("loaded_slot"), previous.get("box_slot", 0)))
    status_type = _as_int(data.get("status_type"), 0)
    current_status = _as_int(data.get("current_status"), 0)
    is_loaded = bool(data.get("loaded", previous.get("loaded", False)))
    slot = _normalize_slot({
        "index": slot_index,
        "type": data.get("type", previous.get("type", "?")),
        "color": data.get("color", previous.get("color", [175, 175, 175])),
        "loaded": is_loaded,
        "status": 5 if is_loaded else 4,
        "edit_status": data.get("edit_status", 1 if previous.get("available") else None),
        "brand_name": data.get("brand_name", previous.get("brand")),
        "color_group": data.get("color_group", previous.get("color_group_hex", [])),
        "icon_type": data.get("icon_type", previous.get("icon_type", 0)),
    }, -1, "external", slot_index if is_loaded else -1)
    # External is not an AMS channel. Keep a distinct sentinel so it cannot
    # collide with ACE 1 slot 1 (global AMS index 0).
    slot["slot"] = -1
    telemetry["external_spool"] = slot
    existing = [box for box in telemetry.get("material_boxes", []) if box.get("source") not in ("external", "external_mcb")]
    existing.append({"id": -1, "source": "external", "loaded_slot": slot_index, "slots": [slot]})
    telemetry["material_boxes"] = existing
    telemetry["filaments"] = [item for box in existing for item in box.get("slots", [])]
    if status_type in (1, 2) or current_status in (1, 2):
        telemetry["feed_status"] = {
            "box_id": -1,
            "slot_index": slot_index,
            "type": status_type,
            "current_status": current_status,
            "code": _as_int(data.get("code"), code),
        }
    elif (telemetry.get("feed_status") or {}).get("box_id") == -1:
        telemetry["feed_status"] = None


def fetch_credentials():
    global creds, model_id, device_id
    try:
        req = urllib.request.Request(f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/info")
        with urllib.request.urlopen(req, timeout=3) as r:
            info = json.loads(r.read().decode())
        token = info.get("token", "")
        if not token:
            return False
        telemetry["token"] = token
        if "?s=" not in telemetry.get("upload_url", ""):
            telemetry["upload_url"] = f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload?s={token}"

        telemetry["model"] = info.get("deviceName") or info.get("modelName") or "Anycubic Printer"
        telemetry["model_id"] = str(info.get("modelId", ""))
        telemetry["firmware"] = info.get("firmwareVersion") or info.get("firmware") or info.get("version") or ""
        telemetry["cn"] = info.get("cn", "")

        for cam_key in ("rtspUrl", "flvUrl", "cameraUrl", "videoUrl"):
            val = info.get(cam_key)
            if val and isinstance(val, str) and val.startswith("http"):
                telemetry["raw_camera_url"] = val
                break

        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
        k1 = token[:16]
        k2 = token[16:32]
        sign = hashlib.md5((hashlib.md5(k1.encode()).hexdigest() + str(ts) + nonce).encode()).hexdigest()
        url = f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/ctrl?ts={ts}&nonce={nonce}&sign={sign}&did=orca-bridge"

        req = urllib.request.Request(url, data=b"", headers={"Content-Length": "0", "User-Agent": "AnycubicSlicerNext/2.0.0.2"})
        with urllib.request.urlopen(req, timeout=3) as r:
            res = json.loads(r.read().decode())

        iv = res["data"]["token"].encode()
        raw = base64.b64decode(res["data"]["info"])
        decrypter = pyaes.Decrypter(pyaes.AESModeOfOperationCBC(k2.encode(), iv=iv))
        plain = decrypter.feed(raw) + decrypter.feed()
        # pyaes.Decrypter already validates and removes PKCS#7 padding.
        creds = json.loads(plain.decode())

        model_id = str(creds.get("modelId") or info.get("modelId") or "")
        device_id = creds.get("deviceId", "")
        if not model_id or not device_id:
            return False
        return True
    except Exception as e:
        print(f"[Bridge] Handshake error: {e}")
        return False

def on_mqtt_connect(c, userdata, flags, rc, properties=None):
    global telemetry
    print(f"[Bridge] MQTT connected, rc={rc}")
    telemetry["connected"] = True
    c.subscribe(f"anycubic/anycubicCloud/v1/printer/public/{model_id}/{device_id}/#")
    query_all()

# Notification center: print lifecycle events and printer errors, newest last.
# Kept across daemon restarts in a small JSON file per printer.
NOTIFICATION_LIMIT = 100
NOTIFICATIONS_FILE = os.path.join(
    tempfile.gettempdir(),
    "orcacubic_notifications_" + "".join(ch if ch.isalnum() else "_" for ch in PRINTER_IP) + ".json")
notifications = []
notifications_lock = threading.Lock()
notification_tracker = {"job": "", "next_id": 1}
ACTIVE_STATES = ("printing", "paused", "busy")


def _load_notifications():
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        items = [n for n in saved if isinstance(n, dict) and isinstance(n.get("id"), int)]
        notifications[:] = items[-NOTIFICATION_LIMIT:]
        if notifications:
            notification_tracker["next_id"] = notifications[-1]["id"] + 1
    except Exception:
        pass


def _save_notifications():
    try:
        tmp = NOTIFICATIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(notifications, fh)
        os.replace(tmp, NOTIFICATIONS_FILE)
    except Exception:
        pass


def add_notification(kind, event, title, message="", job="", code=0, dedupe_seconds=0):
    """Record an event; kind is error, warning, success or info."""
    now = int(time.time())
    with notifications_lock:
        if dedupe_seconds:
            for n in reversed(notifications):
                if now - n.get("time", 0) > dedupe_seconds:
                    break
                if n.get("event") == event and n.get("message") == message and n.get("code") == code:
                    return None
        entry = {
            "id": notification_tracker["next_id"], "time": now, "kind": kind, "event": event,
            "title": title, "message": message, "job": os.path.basename(job or ""), "code": code, "read": False,
        }
        notification_tracker["next_id"] += 1
        notifications.append(entry)
        del notifications[:-NOTIFICATION_LIMIT]
        _save_notifications()
    print(f"[Bridge] Notification: {title} {message}")
    return entry


def _is_real_job(name):
    return bool(name) and name != "Ready" and not is_command_filename(name)


def observe_notifications(prev_raw_state, prev_progress, prev_error):
    """Turn a telemetry change into notifications. prev_raw_state is "idle"
    until the printer reported a state, so a job already running when the
    bridge starts is tracked without announcing its start."""
    prev = normalize_printer_state(prev_raw_state)
    state = normalize_printer_state(telemetry.get("state"))
    job = telemetry.get("job_name", "")

    err = (telemetry.get("last_error", ""), telemetry.get("last_error_time", 0))
    if err[0] and err != prev_error:
        code = _as_int(telemetry.get("last_error_code", 0))
        homing = code in (10901, 10111) or "home" in err[0].lower()
        add_notification("warning" if homing else "error", "printer_error",
                         "Printer notice" if homing else "Printer error", err[0],
                         notification_tracker["job"], code, dedupe_seconds=60)

    if state in ("printing", "paused") and _is_real_job(job) and job != notification_tracker["job"]:
        notification_tracker["job"] = job
        if prev_raw_state != "idle":
            add_notification("info", "started", "Print started", "", job)
    current = notification_tracker["job"]
    if state == prev or not current:
        return
    progress = _as_int(prev_progress, 0)
    if state == "paused":
        add_notification("warning", "paused", "Print paused", "", current)
    elif state == "printing" and prev == "paused":
        add_notification("info", "resumed", "Print resumed", "", current)
    elif state == "completed" or (state == "free" and prev in ACTIVE_STATES and progress >= 99):
        add_notification("success", "finished", "Print finished", "", current)
        notification_tracker["job"] = ""
    elif state in ("failed", "abnormal"):
        add_notification("error", "failed", "Print failed", telemetry.get("last_error", ""), current)
        notification_tracker["job"] = ""
    elif state == "stopped" or (state == "free" and prev in ACTIVE_STATES):
        add_notification("warning", "stopped", "Print stopped", f"Stopped at {progress}%", current)
        notification_tracker["job"] = ""


def notifications_since(since_id):
    with notifications_lock:
        items = [dict(n) for n in notifications if n["id"] > since_id]
        unread = sum(1 for n in notifications if not n.get("read"))
        last_id = notifications[-1]["id"] if notifications else 0
    return {"notifications": items, "unread": unread, "last_id": last_id}


def mark_notifications_read(ids=None):
    with notifications_lock:
        for n in notifications:
            if ids is None or n["id"] in ids:
                n["read"] = True
        _save_notifications()


def clear_notifications():
    with notifications_lock:
        notifications.clear()
        _save_notifications()


# Estimated filament cost of the prints sent from OrcaMartelo, for the MCP server.
# Keyed by job_cost_key(), the newest last; kept across restarts like the notifications.
JOB_COST_LIMIT = 50
JOB_COSTS_FILE = os.path.join(
    tempfile.gettempdir(),
    "orcacubic_job_costs_" + "".join(ch if ch.isalnum() else "_" for ch in PRINTER_IP) + ".json")
job_costs = {}
job_costs_lock = threading.Lock()


def job_cost_key(name):
    """The file name without folder and print-file extensions, so the name the
    printer reports matches the name the print was sent under."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip().lower()
    for ext in (".3mf", ".gcode", ".bgcode"):
        if base.endswith(ext):
            base = base[:-len(ext)]
    return base


def _load_job_costs():
    try:
        with open(JOB_COSTS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            job_costs.update({k: v for k, v in saved.items() if isinstance(v, dict)})
    except Exception:
        pass


def _save_job_costs():
    try:
        tmp = JOB_COSTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job_costs, fh)
        os.replace(tmp, JOB_COSTS_FILE)
    except Exception:
        pass


def _as_float(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def record_job_cost(name, cost, weight_g, filament_mm=0.0, filaments=None):
    """Remember the sliced cost of a print sent under name. Nothing is kept without a weight."""
    key = job_cost_key(name)
    weight_g = _as_float(weight_g)
    if not key or weight_g <= 0:
        return None
    entry = {
        "job": os.path.basename(str(name)),
        "cost": round(_as_float(cost), 2),
        "weight_g": round(weight_g, 2),
        "filament_m": round(_as_float(filament_mm) / 1000.0, 2),
        "filaments": filaments or [],
        "sent_at": int(time.time()),
    }
    with job_costs_lock:
        job_costs.pop(key, None)
        job_costs[key] = entry
        for old in list(job_costs)[:-JOB_COST_LIMIT]:
            del job_costs[old]
        _save_job_costs()
    return entry


def find_job_cost(name):
    key = job_cost_key(name)
    if not key:
        return None
    with job_costs_lock:
        if key in job_costs:
            return dict(job_costs[key])
        # The printer may report a shortened or decorated name.
        for k in reversed(list(job_costs)):
            if k and (k in key or key in k):
                return dict(job_costs[k])
    return None


def _csv_floats(value):
    return [_as_float(v.strip()) for v in value.split(",") if v.strip()]


def parse_cost_from_bytes(content_bytes):
    """Read the cost lines OrcaSlicer writes at the end of a G-code file.
    Returns (cost, weight_g, filament_mm, filaments), or None without them."""
    import re
    head = content_bytes[:1048576].decode("utf-8", errors="ignore")
    tail = content_bytes[-1048576:].decode("utf-8", errors="ignore") if len(content_bytes) > 1048576 else ""
    text = head + "\n" + tail

    def value(pattern):
        m = re.search(r"^;\s*" + pattern + r"\s*=\s*([^\r\n]*)", text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    weights = _csv_floats(value(r"filament used \[g\]"))
    costs = _csv_floats(value(r"filament cost"))
    lengths = _csv_floats(value(r"filament used \[mm\]"))
    weight = _as_float(value(r"total filament used \[g\]")) or sum(weights)
    if weight <= 0:
        return None
    cost = _as_float(value(r"total filament cost")) or sum(costs)
    types = [t.strip() for t in value("filament_type").split(";")]
    colors = [c.strip() for c in value("filament_colou?r").split(";")]
    filaments = []
    for i, w in enumerate(weights):
        if w <= 0:
            continue
        filaments.append({
            "filament": i + 1,
            "type": types[i] if i < len(types) else "",
            "color": colors[i] if i < len(colors) else "",
            "weight_g": round(w, 2),
            "cost": round(costs[i], 2) if i < len(costs) else 0.0,
        })
    return cost, weight, sum(lengths), filaments


# Requests the MCP server hands to the slicer: a camera frame the bundled Python cannot decode,
# a confirmation the user answers in OrcaMartelo, and the slicer tools. The slicer holds a long
# poll on /slicer/wait, runs each request and posts its answer to /slicer/result.
SNAPSHOT_TIMEOUT = 32  # The camera may need FLV_START_TIMEOUT to start, then a few frames.
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
SLICER_TIMEOUT = 30
CONFIRM_TIMEOUT = 120
SEND_DIALOG_TIMEOUT = 600  # The user maps the filaments in the send dialog: give them time.
slicer_cond = threading.Condition()
slicer_state = {"next_id": 1, "pending": [], "results": {}}


def _is_mjpeg_url(url):
    u = (url or "").lower()
    return any(tag in u for tag in ("action=stream", "action=snapshot", ".mjpg", ".mjpeg", ".jpg", ":8080", "/webcam"))


def _jpeg_from_stream(url, timeout=6):
    """The first JPEG of an MJPEG stream, or a still image."""
    req = urllib.request.Request(url, headers={"User-Agent": "OrcaMartelo"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.headers.get("Content-Type", "").startswith("image/jpeg"):
            return resp.read(SNAPSHOT_MAX_BYTES)
        buf = b""
        while len(buf) < SNAPSHOT_MAX_BYTES:
            chunk = resp.read(16384)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if end >= 0:
                return buf[start:end + 2]
    return None


def slicer_call(kind, args, timeout):
    """Have the slicer run a request. Returns its result, or raises ValueError with the reason."""
    with slicer_cond:
        rid = slicer_state["next_id"]
        slicer_state["next_id"] += 1
        slicer_state["pending"].append({"id": rid, "kind": kind, "args": args})
        slicer_cond.notify_all()
        slicer_cond.wait_for(lambda: rid in slicer_state["results"], timeout)
        slicer_state["pending"] = [r for r in slicer_state["pending"] if r["id"] != rid]
        answer = slicer_state["results"].pop(rid, None)
    if answer is None:
        raise ValueError("OrcaMartelo did not answer in time. Check that it is open and not showing a dialog.")
    if not answer.get("ok"):
        raise ValueError(answer.get("error") or "OrcaMartelo could not do it")
    return answer.get("result") or {}


def wait_slicer_request(timeout=20):
    """Called by the slicer: the next request to run, or None after timeout seconds."""
    with slicer_cond:
        slicer_cond.wait_for(lambda: slicer_state["pending"] or shutdown_event.is_set(), timeout)
        return slicer_state["pending"].pop(0) if slicer_state["pending"] else None


def deliver_slicer_result(rid, answer):
    with slicer_cond:
        slicer_state["results"][rid] = answer if isinstance(answer, dict) else {"ok": False, "error": "Invalid answer"}
        # Answers nobody waits for any more (a timed-out request) are dropped.
        for old in sorted(slicer_state["results"])[:-16]:
            del slicer_state["results"][old]
        slicer_cond.notify_all()


def confirm_action(action, **details):
    """Ask the user in OrcaMartelo to allow an action. Raises ValueError unless allowed."""
    try:
        answer = slicer_call("confirm", dict(details, action=action), CONFIRM_TIMEOUT)
    except ValueError as e:
        raise ValueError(f"Not done: the confirmation in OrcaMartelo was not answered ({e})")
    if not answer.get("allowed"):
        raise ValueError("Not done: the user declined it in OrcaMartelo.")


# Where the camera snapshots are kept: the slicer passes its data folder; a temp folder
# stands in when the bridge runs alone.
DATA_DIR = os.environ.get("ORCAMARTELO_DATA_DIR") or os.path.join(tempfile.gettempdir(), "orcamartelo")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "camera_snapshots")
SNAPSHOT_KEEP = 30


def save_snapshot(jpeg):
    """Keep a snapshot as a file, for MCP clients that cannot show an image. Returns its path."""
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        stem = os.path.join(SNAPSHOT_DIR, time.strftime("snapshot-%Y%m%d-%H%M%S"))
        n = 1
        while os.path.exists(f"{stem}-{n:02d}.jpg"):  # Several pictures in one second; names sort by age.
            n += 1
        path = f"{stem}-{n:02d}.jpg"
        with open(path, "wb") as fh:
            fh.write(jpeg)
        old = sorted(f for f in os.listdir(SNAPSHOT_DIR) if f.startswith("snapshot-") and f.endswith(".jpg"))
        for name in old[:-SNAPSHOT_KEEP]:
            os.remove(os.path.join(SNAPSHOT_DIR, name))
        return path
    except OSError:
        return None


def capture_camera_jpeg():
    custom = telemetry.get("custom_camera_url") or ""
    if custom and _is_mjpeg_url(custom):
        urls = [custom.replace("action=stream", "action=snapshot"), custom] if "action=stream" in custom else [custom]
        for url in urls:
            try:
                data = _jpeg_from_stream(url)
                if data:
                    return data
            except Exception:
                pass
        raise ValueError(f"Could not read a picture from the camera at {custom}")
    if not telemetry.get("connected"):
        raise ValueError("The printer is not connected")
    try:
        answer = slicer_call("camera_snapshot", {"url": f"http://127.0.0.1:{HTTP_PORT}/camera"}, SNAPSHOT_TIMEOUT)
    except ValueError:
        # The native camera failed: an MJPEG camera on the printer may still answer.
        mjpeg = find_mjpeg_camera()
        data = _jpeg_from_stream(mjpeg.replace("action=stream", "action=snapshot")) if mjpeg else None
        if data:
            return data
        raise
    try:
        data = base64.b64decode(answer.get("jpeg_base64", ""), validate=True)
    except ValueError:
        data = b""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("The camera picture failed: no frame from the camera")
    return data


def _find_slot(slot):
    """The telemetry entry of a 1-based ACE slot number, or of "external". Raises ValueError."""
    external = str(slot).strip().lower() == "external"
    for box in telemetry.get("material_boxes") or []:
        for item in box.get("slots") or []:
            is_external = item.get("source") in ("external", "external_mcb")
            if is_external and external:
                return item
            if not is_external and not external and _as_int(item.get("slot", -1), -1) == _as_int(slot, 0) - 1:
                return item
    if external and isinstance(telemetry.get("external_spool"), dict):
        return telemetry["external_spool"]
    raise ValueError(f"There is no slot {slot}; get_filaments lists the slots")


def _require_connected():
    if not mqtt_client or not telemetry.get("connected"):
        raise ValueError("The printer is not connected")


def _require_idle(what):
    state = normalize_printer_state(telemetry.get("state"))
    if state in ACTIVE_STATES:
        raise ValueError(f"Not done: the printer is {state}. {what} is only allowed while it is idle.")


def _control(data):
    """Run a /control action for an MCP tool. Raises ValueError when the bridge reports an error."""
    _require_connected()
    result = perform_control(data) or {}
    if result.get("status") == "error":
        raise ValueError(result.get("message") or "The printer refused it")
    return result


# The printer answers a file listing on its report topic; list_printer_files waits for it.
file_list_cond = threading.Condition()
file_list_state = {"seq": 0, "records": None}


def _on_file_report(payload):
    data = payload.get("data")
    records = data.get("records", data.get("files")) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return
    with file_list_cond:
        file_list_state["records"] = records
        file_list_state["seq"] += 1
        file_list_cond.notify_all()


SPEED_MODES = {"silent": 1, "standard": 2, "sport": 3}


class McpBackend:
    """The printer data the MCP tools return (see orcamartelo_mcp.py)."""

    def print_status(self):
        t = public_telemetry()
        state = t.get("state", "free")
        active = state in ACTIVE_STATES
        job = t.get("job_name", "")
        status = {
            "connected": bool(t.get("connected")),
            "state": state,
            "job": job if _is_real_job(job) else None,
            "progress_percent": _as_int(t.get("progress", 0)),
            "layer": _as_int(t.get("curr_layer", 0)),
            "total_layers": _as_int(t.get("total_layers", 0)),
            "elapsed_minutes": _as_int(t.get("print_time", 0)),
            "remaining_minutes": _as_int(t.get("remain_time", 0)),
            "estimated_total_minutes": _as_int(t.get("total_print_time", 0)),
            "estimated_end": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t["estimated_end_timestamp"]))
                             if t.get("estimated_end_timestamp") else None,
            "nozzle_temp_c": t.get("nozzle_temp", 0),
            "nozzle_target_c": t.get("target_nozzle_temp", 0),
            "bed_temp_c": t.get("bed_temp", 0),
            "bed_target_c": t.get("target_bed_temp", 0),
            "fan_percent": t.get("fan", 0),
            "speed_mode": {1: "silent", 2: "standard", 3: "sport"}.get(_as_int(t.get("speed_mode", 2)), t.get("speed_mode")),
            "error": t.get("last_error") or None,
        }
        if not active and state != "completed":
            for key in ("layer", "total_layers", "elapsed_minutes", "remaining_minutes"):
                status[key] = 0
        return status

    def print_cost(self):
        t = public_telemetry()
        state = t.get("state", "free")
        job = t.get("job_name", "")
        entry = find_job_cost(job) if _is_real_job(job) else None
        current = entry is not None and state in ACTIVE_STATES
        if entry is None:
            with job_costs_lock:
                entry = dict(next(reversed(job_costs.values()))) if job_costs else None
        if entry is None:
            return {"available": False,
                    "reason": "No print sent from OrcaMartelo has a recorded cost yet. "
                              "Prints started on the printer or from other software have none."}
        progress = _as_int(t.get("progress", 0)) if current else (100 if state == "completed" else None)
        result = {
            "available": True,
            "job": entry.get("job"),
            "is_current_print": current,
            "estimated_cost": entry.get("cost"),
            "currency_note": "In the currency of the filament profiles' cost setting.",
            "filament_weight_g": entry.get("weight_g"),
            "filament_length_m": entry.get("filament_m"),
            "per_filament": entry.get("filaments") or [],
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(entry.get("sent_at", 0))),
        }
        if progress is not None:
            share = max(0, min(100, progress)) / 100.0
            result["progress_percent"] = progress
            result["spent_so_far_estimate"] = round(_as_float(entry.get("cost")) * share, 2)
            result["weight_so_far_estimate_g"] = round(_as_float(entry.get("weight_g")) * share, 1)
        return result

    def filaments(self):
        t = public_telemetry()
        slots = []
        for box in t.get("material_boxes") or []:
            for slot in box.get("slots") or []:
                if slot.get("source") in ("external", "external_mcb"):
                    continue  # Reported as external_spool.
                index = _as_int(slot.get("slot", -1), -1)
                slots.append({
                    "slot": index + 1 if index >= 0 else None,
                    "box": slot.get("box_id"),
                    "type": slot.get("type"),
                    "color": slot.get("color"),
                    "brand": slot.get("brand"),
                    "has_material": bool(slot.get("available")),
                    "loaded": bool(slot.get("loaded")),
                    "feeding": bool(slot.get("is_active")),
                })
        ext = t.get("external_spool")
        external = None
        if isinstance(ext, dict):
            external = {"type": ext.get("type"), "color": ext.get("color"), "brand": ext.get("brand"),
                        "has_material": bool(ext.get("available")), "loaded": bool(ext.get("loaded"))}
        return {"has_multi_color_box": bool(t.get("has_multi_color_box")), "slots": slots, "external_spool": external}

    def ace_status(self):
        t = public_telemetry()
        boxes = []
        for box in t.get("material_boxes") or []:
            if _as_int(box.get("id"), -1) < 0:
                continue  # The external spool holder has no heater.
            drying = box.get("drying_status") or {}
            on = bool(drying.get("status"))
            remaining = _as_int(drying.get("remain_time", 0)) if on else 0
            boxes.append({
                "box": box.get("id"),
                "temperature_c": box.get("temp"),
                "drying": on,
                "drying_target_c": drying.get("target_temp") if on else None,
                "drying_duration_minutes": drying.get("duration") if on else None,
                "drying_remaining_minutes": remaining if on else None,
                "drying_ends": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + remaining * 60)) if on and remaining else None,
                "backup_enabled": bool(box.get("auto_feed")),
            })
        if not boxes:
            return {"has_multi_color_box": False, "boxes": []}
        if t.get("ace_humidity") is not None:
            boxes[0]["humidity_percent"] = t.get("ace_humidity")
        return {"has_multi_color_box": True, "boxes": boxes}

    def notifications(self, limit=20, unread_only=False):
        with notifications_lock:
            items = [dict(n) for n in notifications if not unread_only or not n.get("read")]
            unread = sum(1 for n in notifications if not n.get("read"))
        items = items[-limit:][::-1]
        for n in items:
            n["time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(n.get("time", 0)))
        return {"unread": unread, "notifications": items}

    def temperatures(self):
        t = public_telemetry()
        return {
            "connected": bool(t.get("connected")),
            "nozzle_c": t.get("nozzle_temp", 0),
            "nozzle_target_c": t.get("target_nozzle_temp", 0),
            "bed_c": t.get("bed_temp", 0),
            "bed_target_c": t.get("target_bed_temp", 0),
        }

    def set_filament_slot(self, slot, color=None, material=None, brand=None):
        """Change what the printer records for a slot. slot is the 1-based slot number or "external"."""
        if not mqtt_client or not telemetry.get("connected"):
            raise ValueError("The printer is not connected")
        if color is None and material is None and brand is None:
            raise ValueError("Give a color, a material or a brand to set")
        if color is not None:
            hexdigits = str(color).strip().lstrip("#")
            if len(hexdigits) != 6 or any(ch not in string.hexdigits for ch in hexdigits):
                raise ValueError("color must be #RRGGBB")
            color = "#" + hexdigits.upper()
        if material is not None:
            material = str(material).strip().upper()
            if not material or len(material) > 16 or not all(ch.isalnum() or ch in "+-_ " for ch in material):
                raise ValueError("material must be a filament type such as PLA, PETG or ABS")
        if brand is not None:
            brand = str(brand).strip()[:32]

        external = str(slot).strip().lower() == "external"
        target = _find_slot(slot)

        new_color = color or target.get("color") or "#AFAFAF"
        update = {
            "index": _as_int(target.get("box_slot", 0)),
            "box_id": _as_int(target.get("box_id", -1), -1),
            "source": "external" if external else "ace",
            "type": material or target.get("type") or "PLA",
            "color": new_color,
            "brand": brand or target.get("brand") or "Anycubic",
        }
        if color is None:
            # Keep a gradient or luminous finish when only the material or brand changes.
            update["finish_type"] = target.get("finish_type", "solid")
            update["color_group"] = target.get("color_group_hex") or [new_color]
        sync_slots_to_printer([update])
        return {"status": "sent", "slot": "external" if external else _as_int(slot),
                "color": update["color"], "material": update["type"], "brand": update["brand"]}

    def camera_snapshot(self):
        data = capture_camera_jpeg()
        path = save_snapshot(data)
        t = public_telemetry()
        job = t.get("job_name", "")
        caption = (f"Printer camera at {time.strftime('%Y-%m-%d %H:%M:%S')}. State: {t.get('state')}"
                   + (f", printing {job} at {_as_int(t.get('progress', 0))}%" if _is_real_job(job) and t.get("state") in ACTIVE_STATES else "")
                   + "." + (f" Saved to {path}" if path else ""))
        return orcamartelo_mcp.ImageResult(base64.b64encode(data).decode("ascii"), "image/jpeg", caption)

    # Printer control. Anything that heats, moves or can spoil a print is confirmed in OrcaMartelo.

    def pause_print(self):
        _require_connected()
        if normalize_printer_state(telemetry.get("state")) != "printing":
            raise ValueError("There is no print running to pause")
        _control({"action": "pause"})
        return {"status": "sent", "action": "pause"}

    def resume_print(self):
        _require_connected()
        if normalize_printer_state(telemetry.get("state")) != "paused":
            raise ValueError("The print is not paused")
        _control({"action": "resume"})
        return {"status": "sent", "action": "resume"}

    def stop_print(self):
        _require_connected()
        state = normalize_printer_state(telemetry.get("state"))
        if state not in ACTIVE_STATES:
            raise ValueError("There is no print to stop")
        job = telemetry.get("job_name", "")
        confirm_action("stop_print", job=job, progress=_as_int(telemetry.get("progress", 0)))
        _control({"action": "stop"})
        return {"status": "sent", "action": "stop", "job": job}

    def set_temperature(self, nozzle=None, bed=None):
        _require_connected()
        if nozzle is None and bed is None:
            raise ValueError("Give a nozzle or bed temperature")
        for name, value, top in (("nozzle", nozzle, 300), ("bed", bed, 120)):
            if value is not None and not 0 <= value <= top:
                raise ValueError(f"The {name} temperature must be between 0 and {top} °C")
        confirm_action("set_temperature", nozzle=nozzle, bed=bed)
        if nozzle is not None and bed is not None:
            _control({"action": "temp", "heater": "both", "nozzle": nozzle, "bed": bed})
        elif nozzle is not None:
            _control({"action": "temp", "heater": "nozzle", "value": nozzle})
        else:
            _control({"action": "temp", "heater": "bed", "value": bed})
        return {"status": "sent", "nozzle_target_c": nozzle, "bed_target_c": bed}

    def set_fan(self, percent):
        if not 0 <= percent <= 100:
            raise ValueError("percent must be between 0 and 100")
        _control({"action": "fan", "value": percent})
        return {"status": "sent", "fan_percent": percent}

    def set_light(self, light, on):
        if light not in ("camera", "nozzle"):
            raise ValueError('light must be "camera" or "nozzle"')
        _control({"action": f"{light}_light", "status": 1 if on else 0})
        return {"status": "sent", "light": light, "on": on}

    def set_speed_mode(self, mode):
        if mode not in SPEED_MODES:
            raise ValueError("mode must be silent, standard or sport")
        _control({"action": "speed", "mode": SPEED_MODES[mode]})
        return {"status": "sent", "speed_mode": mode}

    def home_axes(self, axes="XYZ"):
        axes = axes.upper()
        if axes not in ("XYZ", "XY", "Z"):
            raise ValueError("axes must be XYZ, XY or Z")
        _require_connected()
        _require_idle("Homing")
        confirm_action("home_axes", axes=axes)
        _control({"action": "home", "axis": axes})
        return {"status": "sent", "homed": axes}

    def move_axis(self, axis, distance_mm):
        axis = axis.upper()
        if axis not in ("X", "Y", "Z"):
            raise ValueError("axis must be X, Y or Z")
        if not 0 < abs(distance_mm) <= 100:
            raise ValueError("distance_mm must be between -100 and 100, and not 0")
        _require_connected()
        _require_idle("Moving the head")
        confirm_action("move_axis", axis=axis, distance=distance_mm)
        _control({"action": "jog", "axis": axis, "dir": 1 if distance_mm > 0 else -1, "dist": abs(distance_mm)})
        return {"status": "sent", "axis": axis, "distance_mm": distance_mm}

    def disable_motors(self):
        _require_connected()
        _require_idle("Turning the motors off")
        _control({"action": "motor_off"})
        return {"status": "sent"}

    def extrude(self, length_mm):
        if not 0 < abs(length_mm) <= 100:
            raise ValueError("length_mm must be between -100 and 100, and not 0; negative retracts")
        _require_connected()
        _require_idle("Extruding")
        confirm_action("extrude", length=length_mm)
        result = _control({"action": "extrude", "dir": "extrude" if length_mm > 0 else "retract", "dist": abs(length_mm)})
        return {"status": result.get("status", "sent"), "length_mm": length_mm}

    def run_gcode(self, gcode):
        gcode = gcode.strip()
        if not gcode or len(gcode) > 20000:
            raise ValueError("gcode must be 1 to 20000 characters")
        _require_connected()
        _require_idle("Running G-code")
        confirm_action("run_gcode", gcode=gcode)
        result = upload_and_run_gcode(gcode, delete_after=True)
        if isinstance(result, dict) and result.get("status") == "error":
            raise ValueError(result.get("message") or "The printer refused the G-code")
        return {"status": "sent", "lines": len(gcode.splitlines())}

    # The multi-color box (ACE).

    def load_filament(self, slot):
        target = _find_slot(slot)
        if target.get("source") in ("external", "external_mcb"):
            raise ValueError("Only ACE slots can be loaded from here")
        _require_connected()
        _require_idle("Loading filament")
        confirm_action("load_filament", slot=_as_int(slot), material=target.get("type"), color=target.get("color"))
        _control({"action": "feed_filament", "slot": _as_int(target.get("box_slot", 0)),
                  "box_id": _as_int(target.get("box_id", 0)), "type": target.get("type") or "PLA"})
        return {"status": "sent", "slot": _as_int(slot)}

    def unload_filament(self, slot):
        target = _find_slot(slot)
        if target.get("source") in ("external", "external_mcb"):
            raise ValueError("Only ACE slots can be unloaded from here")
        _require_connected()
        _require_idle("Unloading filament")
        confirm_action("unload_filament", slot=_as_int(slot))
        _control({"action": "unfeed_filament", "slot": _as_int(target.get("box_slot", 0)), "box_id": _as_int(target.get("box_id", 0))})
        return {"status": "sent", "slot": _as_int(slot)}

    def set_ace_dryer(self, on, temperature=50, hours=4):
        if on and not 30 <= temperature <= 65:
            raise ValueError("temperature must be between 30 and 65 °C")
        if on and not 1 <= hours <= 24:
            raise ValueError("hours must be between 1 and 24")
        result = _control({"action": "set_ace_drying", "box_id": 0, "status": 1 if on else 0,
                           "target_temp": temperature, "duration": hours * 60})
        return {"status": "sent", "drying": result.get("drying_status")}

    def set_ace_backup(self, enabled):
        _control({"action": "set_ace_auto_feed", "box_id": 0, "auto_feed": 1 if enabled else 0})
        return {"status": "sent", "backup_enabled": enabled}

    # Print files.

    def list_printer_files(self):
        _require_connected()
        msg = {"type": "file", "action": "listLocal", "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
               "timestamp": int(time.time() * 1000), "data": {"path": "/"}}
        with file_list_cond:
            seq = file_list_state["seq"]
            for role in ("slicer", "web"):
                mqtt_client.publish(f"anycubic/anycubicCloud/v1/{role}/printer/{model_id}/{device_id}/file", json.dumps(msg))
            file_list_cond.wait_for(lambda: file_list_state["seq"] != seq, 8)
            records = file_list_state["records"] if file_list_state["seq"] != seq else None
        if records is None:
            raise ValueError("The printer did not list its files")
        files = []
        for r in records:
            if not isinstance(r, dict) or r.get("is_dir") or r.get("type") == "dir":
                continue
            name = r.get("filename") or r.get("name") or ""
            if not name or is_command_filename(name):
                continue
            stamp = _as_int(r.get("timestamp", r.get("time", 0)))
            stamp = stamp // 1000 if stamp > 10 ** 11 else stamp
            files.append({"filename": name, "size_bytes": _as_int(r.get("size", 0)),
                          "modified": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stamp)) if stamp else None})
        return {"files": files}

    def print_printer_file(self, filename):
        _require_connected()
        _require_idle("Starting a print")
        filename = sanitize_print_filename(filename)
        confirm_action("start_print", file=filename)
        _control({"action": "start_print_job", "filename": filename, "use_ams": bool(telemetry.get("has_multi_color_box"))})
        return {"status": "sent", "filename": filename}

    def _read_print_file(self, path):
        path = os.path.expanduser(path)
        if not path.lower().endswith((".gcode", ".3mf")) or not os.path.isfile(path):
            raise ValueError("path must be an existing .gcode or .gcode.3mf file on this computer")
        if os.path.getsize(path) > MAX_UPLOAD_FILE_BYTES:
            raise ValueError("The file is too large")
        with open(path, "rb") as fh:
            return path, fh.read()

    def _print_filaments(self, plate, path):
        if path:
            path, content = self._read_print_file(path)
            return file_filaments(content, path), {"file": os.path.basename(path)}
        answer = slicer_call("plate_filaments", {"plate": plate}, SLICER_TIMEOUT)
        return answer.get("filaments") or [], {"plate": answer.get("plate")}

    def get_print_filaments(self, plate=None, path=None):
        filaments, source = self._print_filaments(plate, path)
        slots = ace_slots()
        suggestion = suggest_mapping(filaments, slots)
        unmatched = [f["filament"] for f in filaments if f["filament"] not in suggestion]
        return dict(source, filaments=filaments,
                    loaded_slots=[{k: s[k] for k in ("slot", "type", "color", "brand")} for s in slots],
                    suggested_mapping=[{"filament": n, "slot": s["slot"]} for n, s in sorted(suggestion.items())],
                    unmatched_filaments=unmatched,
                    note="A filament prints from a loaded slot of the same material. Pass a mapping to "
                         "print_plate or print_file to choose others; filaments left out keep the suggestion.")

    def _preselect(self, filaments, mapping):
        """The slot_id of each filament, in order, for the send dialog to preselect; empty when the
        assistant proposed nothing and the dialog picks by itself."""
        if not mapping:
            return []
        chosen = resolve_mapping(filaments, ace_slots(), mapping)
        return [chosen[f["filament"]]["slot_id"] for f in filaments]

    def print_file(self, path, mapping=None):
        _require_connected()
        _require_idle("Starting a print")
        path, content = self._read_print_file(path)
        filaments = file_filaments(content, path)
        # The user maps the filaments and picks the options in OrcaMartelo's send dialog.
        info = slicer_call("choose_mapping", {"file": os.path.basename(path), "filaments": filaments,
                                              "preselect": self._preselect(filaments, mapping)}, SEND_DIALOG_TIMEOUT)
        try:
            box_mapping = json.loads(info.get("ams_mapping") or "[]")
        except ValueError:
            box_mapping = []
        result = upload_and_start_print_file(content, os.path.basename(path), use_ams=info.get("use_ams") == "1",
                                             task_settings={k: info.get(k) == "1" for k in
                                                            ("auto_leveling", "vibration_compensation", "flow_calibration", "timelapse")},
                                             ams_mapping=box_mapping)
        if result.get("status") == "error":
            raise ValueError(result.get("message") or "The upload failed")
        return {"status": "sent", "filename": result.get("filename"),
                "mapping": [{"filament": m["paint_index"] + 1, "slot": m["ams_index"] + 1}
                            for m in box_mapping if m.get("paint_index", 99) + 1 in {f["filament"] for f in filaments}]}

    # The slicer: these run in OrcaMartelo itself.

    def slicer_get_state(self):
        return slicer_call("get_state", {}, SLICER_TIMEOUT)

    def slicer_list_presets(self, type):
        return slicer_call("list_presets", {"type": type}, SLICER_TIMEOUT)

    def slicer_select_preset(self, type, name, filament=None):
        return slicer_call("select_preset", {"type": type, "name": name, "filament": filament}, SLICER_TIMEOUT)

    def slicer_get_settings(self, keys):
        return slicer_call("get_settings", {"keys": keys}, SLICER_TIMEOUT)

    def slicer_set_settings(self, settings):
        return slicer_call("set_settings", {"settings": settings}, SLICER_TIMEOUT)

    def slicer_load_models(self, paths):
        return slicer_call("load_models", {"paths": paths}, 120)

    def slicer_arrange(self):
        return slicer_call("arrange", {}, 120)

    def slicer_clear_plate(self):
        confirm_action("clear_plate")
        return slicer_call("clear_plate", {}, SLICER_TIMEOUT)

    def slicer_slice(self, plate=None):
        return slicer_call("slice", {"plate": plate}, 900)

    def print_plate(self, plate=None, mapping=None):
        """Slice a plate if needed and open OrcaMartelo's send dialog for it, as the Print button does:
        the user maps the filaments to ACE slots (a proposed mapping preselected) and starts it."""
        _require_connected()
        _require_idle("Starting a print")
        filaments, source = self._print_filaments(plate, None)
        slicer_call("send_plate", {"plate": source.get("plate"), "slot_ids": self._preselect(filaments, mapping)},
                    SEND_DIALOG_TIMEOUT + 900)
        return {"status": "sent", "plate": source.get("plate"),
                "note": "The user started the print in OrcaMartelo's send dialog; its upload queue shows the progress."}

    def printer_info(self):
        t = public_telemetry()
        return {
            "connected": bool(t.get("connected")),
            "model": t.get("model"),
            "firmware": t.get("firmware") or None,
            "ip": PRINTER_IP,
            "camera_stream_url": t.get("camera_url"),
            "has_multi_color_box": bool(t.get("has_multi_color_box")),
        }


mcp_backend = McpBackend()


def mcp_client_config():
    """How MCP clients reach this server, shown in the Device tab."""
    stdio_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orcamartelo_mcp_stdio.py")
    url = f"http://127.0.0.1:{HTTP_PORT}/mcp"
    return {
        "url": url,
        "command": sys.executable,
        "args": ["-I", stdio_script],
        "claude_desktop_config": {"mcpServers": {"orcamartelo": {"command": sys.executable, "args": ["-I", stdio_script]}}},
        "claude_code_command": f"claude mcp add --transport http orcamartelo {url}",
    }


def on_mqtt_message(c, userdata, msg):
    prev = (telemetry.get("state"), telemetry.get("progress", 0),
            (telemetry.get("last_error", ""), telemetry.get("last_error_time", 0)))
    _handle_mqtt_message(c, userdata, msg)
    try:
        observe_notifications(*prev)
    except Exception as e:
        print(f"[Bridge] Notification error: {e}")


def _handle_mqtt_message(c, userdata, msg):
    global telemetry, pending_cleanup
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
        t = msg.topic.split("/")[-2]

        code = payload.get("code", 200)
        msg_str = payload.get("msg", "")
        state_str = payload.get("state", "")

        # A camera command's answer, and the camera service's codes (114xx), are the camera's
        # state, not printer errors: a slow camera service (11402) during a print must not
        # raise a "printer error" notification.
        if t == "video" or 11400 <= _as_int(code) < 11500:
            camera_relay.on_camera_reply(_as_int(code), msg_str, state_str)
            if t == "video":
                return
            code = 200  # The rest of this report (a print status, say) is still read.

        # Always capture printer LCD warnings and error reports
        if (code != 200 and code != 0) or state_str in ["failed", "abnormal"]:
            if msg_str:
                display_msg = msg_str
                if code == 10901 or "home" in msg_str.lower():
                    display_msg = "Home the axis before moving (Click 🏠 Home to calibrate zero position)."
                elif code == 10111:
                    display_msg = f"LCD Notice (10111): {msg_str}. Please click 🏠 Home before starting motion or print jobs."
                telemetry["last_error"] = display_msg
                telemetry["last_error_code"] = code
                telemetry["last_error_time"] = int(time.time())
                print(f"[Bridge] PRINTER LCD NOTICE ({code}): {display_msg}")

        data = payload.get("data")

        if t == "tempature" or t == "temp":
            if data:
                telemetry["nozzle_temp"] = data.get("curr_nozzle_temp", telemetry["nozzle_temp"])
                telemetry["target_nozzle_temp"] = data.get("target_nozzle_temp", telemetry["target_nozzle_temp"])
                telemetry["bed_temp"] = data.get("curr_hotbed_temp", telemetry["bed_temp"])
                telemetry["target_bed_temp"] = data.get("target_hotbed_temp", telemetry["target_bed_temp"])

        elif t == "file" and payload.get("action") in ("listLocal", "listUdisk"):
            _on_file_report(payload)

        elif t == "info":
            if data:
                temp = data.get("temp", {})
                if temp:
                    telemetry["nozzle_temp"] = temp.get("curr_nozzle_temp", telemetry["nozzle_temp"])
                    telemetry["target_nozzle_temp"] = temp.get("target_nozzle_temp", telemetry["target_nozzle_temp"])
                    telemetry["bed_temp"] = temp.get("curr_hotbed_temp", telemetry["bed_temp"])
                    telemetry["target_bed_temp"] = temp.get("target_hotbed_temp", telemetry["target_bed_temp"])

                project = data.get("project", {}) if isinstance(data, dict) else {}
                if project:
                    proj_fname = project.get("filename", "")
                    proj_state = normalize_printer_state(project.get("state", telemetry["state"]))
                    is_cmd = is_command_filename(proj_fname)

                    if is_cmd:
                        # Internal command execution: never overwrite user print job name or stats with command files
                        if is_command_filename(telemetry.get("job_name")):
                            telemetry["job_name"] = "Ready"
                        state_str = str(proj_state).strip().lower()
                        if state_str in ("0", "free", "ready", "idle", "completed", "finish", "finished", "stop", "stopped", "cancel", "cancelled"):
                            telemetry["state"] = "free"
                            telemetry["curr_layer"] = 0
                            telemetry["total_layers"] = 0
                            telemetry["progress"] = 0
                            telemetry["remain_time"] = 0
                            telemetry["print_time"] = 0
                            telemetry["total_print_time"] = 0
                            telemetry["job_name"] = "Ready"
                    else:
                        telemetry["state"] = proj_state
                        telemetry["progress"] = project.get("progress", telemetry["progress"])
                        if proj_fname:
                            telemetry["job_name"] = proj_fname
                            b_proj = os.path.basename(proj_fname)
                            est = job_estimated_times.get(b_proj, 0)
                            if not est:
                                if b_proj.lower().endswith(".gcode"):
                                    est = job_estimated_times.get(b_proj[:-6], 0)
                                elif b_proj.lower().endswith(".3mf"):
                                    est = job_estimated_times.get(b_proj[:-4], 0)
                            if not est:
                                for k, v in job_estimated_times.items():
                                    if k and (k in b_proj or b_proj in k):
                                        est = v
                                        break
                            telemetry["total_print_time"] = est
                        rep_curr = project.get("curr_layer")
                        if rep_curr is None:
                            rep_curr = project.get("cur_layer", project.get("current_layer", data.get("curr_layer") if isinstance(data, dict) else None))
                        if rep_curr is not None:
                            telemetry["curr_layer"] = _as_int(rep_curr, telemetry["curr_layer"])
                        rep_total = (
                            project.get("total_layers")
                            or project.get("total_layer")
                            or project.get("total_layer_num")
                            or project.get("totalLayer")
                            or project.get("totalLayers")
                            or project.get("layer_num")
                            or (data.get("total_layers") if isinstance(data, dict) else None)
                            or (data.get("total_layer") if isinstance(data, dict) else None)
                            or (data.get("total_layer_num") if isinstance(data, dict) else None)
                        )
                        if rep_total is not None and _as_int(rep_total, 0) > 0:
                            telemetry["total_layers"] = _as_int(rep_total)

                        rep_remain = None
                        for k in ("remain_time", "remainTime", "left_time", "leftTime", "remaining_time", "remainingTime", "remain_min", "remainMin", "left_min", "leftMin"):
                            if isinstance(project, dict) and k in project and project[k] is not None:
                                rep_remain = project[k]
                                break
                        if rep_remain is None and isinstance(data, dict):
                            for k in ("remain_time", "left_time"):
                                if k in data and data[k] is not None:
                                    rep_remain = data[k]
                                    break
                        if rep_remain is not None:
                            val = _as_int(rep_remain, 0)
                            if val > 14400:
                                val = int(round(val / 60))
                            telemetry["remain_time"] = val

                        rep_print_time = (
                            project.get("print_time")
                            or project.get("printTime")
                            or project.get("elapsed_time")
                            or project.get("elapsedTime")
                            or (data.get("print_time") if isinstance(data, dict) else None)
                        )
                        if rep_print_time is not None:
                            pval = _as_int(rep_print_time, telemetry["print_time"])
                            if pval > 14400:
                                pval = int(round(pval / 60))
                            telemetry["print_time"] = pval

                        telemetry["speed_mode"] = project.get("print_speed_mode", telemetry["speed_mode"])

                        # If remain_time is omitted or reported as 0 by printer during active print, estimate it
                        if telemetry["remain_time"] <= 0 and telemetry["state"] in ("printing", "busy", "paused"):
                            pt = _as_int(telemetry.get("print_time", 0))
                            prog = float(telemetry.get("progress", 0))
                            curr_l = _as_int(telemetry.get("curr_layer", 0))
                            tot_l = _as_int(telemetry.get("total_layers", 0))
                            total_est = _as_int(telemetry.get("total_print_time", 0))
                            if total_est > 0:
                                if 0 <= prog <= 100:
                                    telemetry["remain_time"] = max(0, int(round(total_est * (100.0 - prog) / 100.0)))
                                elif tot_l > 0 and curr_l >= 0:
                                    telemetry["remain_time"] = max(0, int(round(total_est * (tot_l - curr_l) / tot_l)))
                            elif pt > 0 and 0 < prog < 100:
                                telemetry["remain_time"] = int(round(pt * (100.0 - prog) / prog))
                            elif pt > 0 and tot_l > curr_l > 0:
                                telemetry["remain_time"] = int(round(pt * (tot_l - curr_l) / curr_l))

                urls = data.get("urls", {})
                for cam_key in ("rtspUrl", "flvUrl", "cameraUrl", "videoUrl"):
                    val = urls.get(cam_key)
                    if val and isinstance(val, str) and val.startswith("http"):
                        telemetry["raw_camera_url"] = val
                        break
                up_url = urls.get("fileUploadurl") or urls.get("fileUploadUrl") or urls.get("uploadUrl") or urls.get("file_upload_url")
                if up_url:
                    telemetry["upload_url"] = up_url
                    up_token = urllib.parse.parse_qs(urllib.parse.urlparse(up_url).query).get("s", [""])[0]
                    if up_token:
                        telemetry["token"] = up_token

        elif t == "status":
            st = payload.get("state", "")
            if st != "":
                telemetry["state"] = normalize_printer_state(st)
                if is_command_filename(telemetry.get("job_name")):
                    telemetry["job_name"] = "Ready"

        elif t == "axis":
            if code != 200 and msg_str:
                telemetry["last_error"] = msg_str
                telemetry["last_error_code"] = code
                telemetry["last_error_time"] = int(time.time())
            elif code == 200 and telemetry.get("last_error") == "Home the axis before moving":
                telemetry["last_error"] = ""

        elif t == "fan":
            if data:
                telemetry["fan"] = data.get("fan_speed_pct", data.get("speed", telemetry["fan"]))

        elif t == "light":
            if data:
                lights = data.get("lights", [])
                for l_item in lights:
                    l_type = _as_int(l_item.get("type"), 0)
                    l_status = _as_int(l_item.get("status"), 0)
                    if l_type == 1:
                        telemetry["nozzle_light"] = l_status
                    elif l_type in (2, 3):
                        telemetry["camera_light"] = l_status
                        telemetry["light"] = l_status
                if lights and not any(_as_int(x.get("type"), 0) in (1, 2, 3) for x in lights):
                    st = _as_int(lights[0].get("status"), telemetry.get("light", 1))
                    telemetry["light"] = st
                    telemetry["camera_light"] = st

        elif t == "multiColorBox":
            # getInfo is a complete snapshot; command acknowledgements may only
            # contain the box or slot that changed and must not clear others.
            if isinstance(data, dict) and payload.get("action") == "getInfo":
                apply_multi_color_box_report(data, code)
            elif isinstance(data, dict) and payload.get("action") in ("refresh", "setInfo", "feedFilament"):
                apply_multi_color_box_report(data, code, replace=False)

        elif t == "extfilbox" and payload.get("action") in ("reportInfo", "setInfo", "getInfo", None):
            if isinstance(data, dict):
                apply_external_spool_report(data, code)

        elif t == "extrudeControl":
            if isinstance(data, dict):
                # Preserve previous values when a report omits fields: defaulting
                # current_status to 0 would make an in-progress switch look idle.
                idx = _as_int(data.get("index"), -1)
                if idx >= 0:
                    telemetry["channel_index"] = idx
                    telemetry["active_slot"] = idx
                telemetry["channel_status"] = data.get("current_status", telemetry.get("channel_status", 0))
                telemetry["has_filaments"] = data.get("has_filaments", telemetry.get("has_filaments", 0))
                telemetry["channel_report_seq"] = telemetry.get("channel_report_seq", 0) + 1
                if telemetry.get("active_slot", -1) >= 0:
                    for b in telemetry.get("material_boxes", []):
                        if isinstance(b, dict):
                            for s in b.get("slots", []):
                                if isinstance(s, dict):
                                    slot_idx = _as_int(s.get("index", s.get("box_slot", s.get("slot", -1))), -1)
                                    s["is_active"] = (slot_idx == telemetry["active_slot"] and s.get("source") not in ("external", "external_mcb"))
                    for s in telemetry.get("filaments", []):
                        if isinstance(s, dict):
                            slot_idx = _as_int(s.get("index", s.get("box_slot", s.get("slot", -1))), -1)
                            s["is_active"] = (slot_idx == telemetry["active_slot"] and s.get("source") not in ("external", "external_mcb"))
            elif payload.get("action") == "switchChannel" and code != 200:
                # Switch explicitly rejected by firmware; surface via last_error.
                telemetry["channel_switch_failed"] = int(time.time())

        elif t == "peripherie":
            # The original page treats peripherie=0 as a hint and asks for a
            # fresh multiColorBox snapshot. Only an empty getInfo report is
            # authoritative enough to clear live ACE data.
            if isinstance(data, dict) and data.get("multiColorBox"):
                telemetry["has_multi_color_box"] = True

    except Exception as e:
        pass

def query_extrude_control():
    if not mqtt_client:
        return
    topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/extrudeControl"
    msg = {
        "type": "extrudeControl",
        "action": "getInfo",
        "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
        "timestamp": int(time.time() * 1000),
        "data": {}
    }
    try:
        mqtt_client.publish(topic, json.dumps(msg))
    except Exception:
        pass


def query_all():
    global mqtt_client
    if not mqtt_client:
        return
    for q_type in ["tempature", "fan", "status", "peripherie", "multiColorBox", "extfilbox", "info", "light", "extrudeControl"]:
        topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/{q_type}"
        msg = {
            "type": q_type,
            "action": "getInfo" if q_type in ["multiColorBox", "extfilbox", "extrudeControl"] else "query",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {}
        }
        try:
            mqtt_client.publish(topic, json.dumps(msg))
        except:
            pass


def build_material_update_messages(slots_data):
    messages = []
    for s in slots_data or []:
        idx = int(s.get("index", 0))
        box_id = int(s.get("box_id", -1))
        source = s.get("source", "ace")
        m_type = s.get("type", "PLA")
        col = s.get("color")
        if isinstance(col, str) and col.startswith("#"):
            c_hex = col.lstrip("#")
            rgb = [
                int(c_hex[0:2], 16) if len(c_hex) >= 2 else 0,
                int(c_hex[2:4], 16) if len(c_hex) >= 4 else 210,
                int(c_hex[4:6], 16) if len(c_hex) >= 6 else 255,
            ]
        elif isinstance(col, list) and len(col) >= 3:
            rgb = [col[0], col[1], col[2]]
        else:
            rgb = [35, 163, 199]
        if source == "external":
            messages.append(("extfilbox", {
                "type": "extfilbox", "action": "setInfo", "data": {"type": m_type, "color": rgb}
            }))
            continue
        finish_type = s.get("finish_type", "solid")
        icon_type = int(s.get("icon_type", 3 if finish_type == "luminous" else (1 if finish_type == "gradient" else 0)))
        color_group = []
        for group_color in s.get("color_group", []) or []:
            if isinstance(group_color, str) and group_color.startswith("#") and len(group_color) >= 7:
                h = group_color.lstrip("#")
                color_group.append([int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255])
            elif isinstance(group_color, list) and len(group_color) >= 3:
                color_group.append([int(group_color[0]), int(group_color[1]), int(group_color[2]), 255])
        if not color_group:
            color_group = [rgb + [255]]
        messages.append(("multiColorBox", {
            "type": "multiColorBox",
            "action": "setInfo",
            "data": {"multi_color_box": [{"id": box_id, "slots": [{
                "index": idx,
                "type": m_type,
                "brand": s.get("brand", "Anycubic"),
                "color": color_group[0][:3],
                "color_group": color_group,
                "icon_type": icon_type,
            }]}]},
        }))
    return messages

def sync_slots_to_printer(slots_data):
    """Set the material, color and brand of slots on the printer, then read them back."""
    for s in slots_data or []:
        idx = int(s.get("index", 0))
        box_id = int(s.get("box_id", -1))
        b = str(s.get("brand") or "").strip()
        if b:
            slot_brand_overrides[(box_id, idx)] = b
            slot_brand_overrides[idx] = b
    if slots_data and mqtt_client:
        # Kobra X firmware accepts one material update per message.
        for suffix, material_msg in build_material_update_messages(slots_data):
            topic = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/{suffix}"
            material_msg["msgid"] = "".join(random.choices(string.hexdigits.lower(), k=32))
            material_msg["timestamp"] = int(time.time() * 1000)
            mqtt_client.publish(topic, json.dumps(material_msg))
            print(f"[Bridge] Published {suffix} material update")
            time.sleep(0.15)
        time.sleep(0.4)
        query_all()


def mqtt_worker():
    global mqtt_client
    while not shutdown_event.is_set():
        if not fetch_credentials():
            shutdown_event.wait(3)
            continue

        try:
            nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="orca-daemon-" + nonce)
            c.username_pw_set(creds.get("username"), creds.get("password"))
            c.tls_set(cert_reqs=ssl.CERT_NONE)
            c.tls_insecure_set(True)
            c.on_connect = on_mqtt_connect
            c.on_message = on_mqtt_message
            mqtt_client = c

            c.connect(PRINTER_IP, PRINTER_MQTT_PORT, 60)
            c.loop_start()

            while not shutdown_event.wait(2):
                query_all()

        except Exception as e:
            print(f"[Bridge] MQTT exception: {e}")
            shutdown_event.wait(3)

def delete_file_on_printer(filename):
    if not mqtt_client:
        return
    try:
        del_msg = {
            "type": "file",
            "action": "deleteBatch",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "root": "local",
                "files": [{"path": "/", "filename": filename}]
            }
        }
        for role in ("slicer", "web"):
            t_del = f"anycubic/anycubicCloud/v1/{role}/printer/{model_id}/{device_id}/file"
            mqtt_client.publish(t_del, json.dumps(del_msg))
        print(f"[Bridge] Sent deleteBatch for {filename}")
    except Exception as e:
        print(f"[Bridge] Error sending deleteBatch for {filename}: {e}")

def upload_and_run_gcode(gcode_text, delete_after=True):
    ts = int(time.time())
    rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    fname = f"_orcacubic_command_{ts}_{rand_id}.gcode"
    
    # Construct complete, compliant G-code with headers and statistics
    content = (
        "; HEADER_BLOCK_START\n"
        "; generated by AnycubicSlicerNext 2.0.0.2\n"
        "; total layer number: 1\n"
        "; filament_density: 1.24\n"
        "; filament_diameter: 1.75\n"
        "; max_z_height: 10.0\n"
        "; HEADER_BLOCK_END\n\n"
        f"{gcode_text.strip()}\n"
        "M400\n\n"
        "; statistics = begin\n"
        "; used_filament = 0.00\n"
        "; print_time = 0m 10s\n"
        "; model_size = 10.00,10.00,10.00\n"
        "; total_layers = 1\n"
        "; statistics = end\n"
    )
    content_bytes = content.encode("utf-8")
    fmd5 = hashlib.md5(content_bytes).hexdigest().lower()
    fsize = len(content_bytes)

    upload_url = telemetry.get("upload_url", f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload")
    tok = telemetry.get("token") or urllib.parse.parse_qs(urllib.parse.urlparse(upload_url).query).get("s", [""])[0]
    if tok and "?s=" not in upload_url:
        upload_url = f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload?s={tok}"
    boundary = "----WebKitFormBoundary" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
    
    part1 = f'--{boundary}\r\nContent-Disposition: form-data; name="filename"\r\n\r\n{fname}\r\n'
    part2 = f'--{boundary}\r\nContent-Disposition: form-data; name="gcode"; filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    part3 = f'\r\n--{boundary}--\r\n'
    
    body = part1.encode('utf-8') + part2.encode('utf-8') + content_bytes + part3.encode('utf-8')
    
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "X-File-Length": str(fsize),
        "X-BBL-Client-Name": "AnycubicSlicerNext",
        "X-BBL-Client-Type": "slicer",
        "X-BBL-Client-Version": "01.03.09.04",
        "User-Agent": "AnycubicSlicerNext/2.0.0.2"
    }
    
    req = urllib.request.Request(upload_url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = r.read().decode('utf-8', errors='ignore')
    print(f"[Bridge] Uploaded {fname}: {resp}")
    
    time.sleep(0.5)
    
    topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/print"
    msg = {
        "type": "print",
        "action": "start",
        "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
        "timestamp": int(time.time() * 1000),
        "data": {
            "taskid": "-1",
            "filename": fname,
            "url": "",
            "md5": fmd5,
            "filepath": None,
            "filetype": 1,
            "project_type": 1,
            "filesize": fsize,
            "ams_settings": {
                "use_ams": True,
                "ams_box_mapping": [
                    {
                        "ams_color": [253, 219, 39],
                        "ams_index": 2,
                        "material_type": "PLA+",
                        "paint_color": [253, 219, 39],
                        "paint_index": 0
                    }
                ]
            },
            "task_settings": {
                "auto_leveling": 0,
                "vibration_compensation": 0,
                "flow_calibration": 0,
                "dry_mode": 0,
                "ai_settings": {"status": 0, "count": 0, "type": 0},
                "timelapse": {"status": 0, "count": 0, "type": 0},
                "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                "model_objects_skip_parts": []
            }
        }
    }
    if mqtt_client:
        mqtt_client.publish(topic, json.dumps(msg))
        print(f"[Bridge] Triggered run for {fname}")

    if delete_after:
        pending_cleanup.append(fname)
        def _deferred_delete():
            time.sleep(6)
            delete_file_on_printer(fname)
            try:
                if fname in pending_cleanup:
                    pending_cleanup.remove(fname)
            except Exception:
                pass
        threading.Thread(target=_deferred_delete, daemon=True).start()
        
    return {"status": "ok", "filename": fname}


def sanitize_print_filename(raw_name):
    """Reduce a user-supplied name to a bare, printer-safe filename."""
    base = (raw_name or "").replace("\\", "/").split("/")[-1].strip()
    return base or "print.gcode"


def start_filename_for_print(filename):
    """The print starts under the uploaded name, `.gcode.3mf` included, as Anycubic Slicer Next does:
    the printer then prepares the job from the 3MF metadata (e.g. heats the bed for the auto leveling)."""
    return filename


# Filament mapping for prints the MCP server starts: which ACE slot prints each filament. The
# user settles it in OrcaMartelo's send dialog; these give the assistant the filaments, the
# loaded slots and a suggestion, and check a mapping it proposes against the dialog's rules
# (build_anycubic_ams_mapping): a filament prints from a loaded slot of the same material, and
# the external spool only serves a single-filament print.
_MATERIAL_PREFIXES = ("GENERIC", "ANYCUBIC", "ESUN", "SUNLU", "BAMBU", "POLYMAKER", "OVERTURE", "ELEGOO")


def normalize_material(material):
    m = str(material or "").upper().strip()
    for prefix in _MATERIAL_PREFIXES:
        if m.startswith(prefix):
            m = m[len(prefix):].strip()
            break
    return "".join(ch for ch in m if ch.isalnum())


def _rgb(color):
    h = str(color or "").strip().lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) >= 6 else None
    except ValueError:
        return None


def ace_slots():
    """The slots a print can use: [{slot (1-based, or "external"), slot_id, type, color, brand}]."""
    slots = []
    for f in telemetry.get("filaments") or []:
        external = f.get("source") in ("external", "external_mcb")
        slot_id = _as_int(f.get("slot", -1), -1)
        if not f.get("available", True) or not f.get("type") or f.get("type") == "?" or (slot_id < 0 and not external):
            continue
        slots.append({"slot": "external" if external else slot_id + 1, "slot_id": -1 if external else slot_id,
                      "type": f.get("type"), "color": f.get("color"), "brand": f.get("brand")})
    return slots


def file_filaments(content, name):
    """The filaments a .gcode or .gcode.3mf print uses: [{filament (1-based), type, color}]."""
    text = ""
    if name.lower().endswith(".3mf"):
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                inner = sorted(n for n in z.namelist() if n.lower().startswith("metadata/plate_") and n.lower().endswith(".gcode"))
                if inner:
                    raw = z.read(inner[0])
                    text = raw[:2097152].decode("utf-8", "ignore") + "\n" + raw[-2097152:].decode("utf-8", "ignore")
        except zipfile.BadZipFile:
            raise ValueError("The 3MF file is damaged")
    else:
        text = content[:2097152].decode("utf-8", "ignore") + "\n" + content[-2097152:].decode("utf-8", "ignore")

    def value(key):
        found = re.findall(r"^;\s*" + key + r"\s*=\s*([^\r\n]*)", text, re.MULTILINE | re.IGNORECASE)
        return found[-1].strip() if found else ""

    types = [t.strip() for t in value("filament_type").split(";") if t.strip()]
    colors = [c.strip() for c in value("filament_colou?r").split(";")]
    weights = [_as_float(w) for w in value(r"filament used \[g\]").split(",") if w.strip()]
    used = [i for i, w in enumerate(weights) if w > 0] or list(range(len(types)))
    return [{"filament": i + 1, "type": types[i] if i < len(types) else "",
             "color": colors[i] if i < len(colors) else ""} for i in used]


def suggest_mapping(filaments, slots):
    """For each filament, the loaded slot of its material with the nearest color, preferring unused slots."""
    chosen, used = {}, set()
    ace = [s for s in slots if s["slot"] != "external"]
    for f in filaments:
        same = [s for s in (ace or slots) if normalize_material(s["type"]) == normalize_material(f["type"])]
        if not same and len(filaments) == 1:
            same = [s for s in slots if normalize_material(s["type"]) == normalize_material(f["type"])]
        if not same:
            continue
        frgb = _rgb(f["color"])

        def distance(s):
            srgb = _rgb(s["color"])
            return sum((a - b) ** 2 for a, b in zip(frgb, srgb)) if frgb and srgb else 10 ** 6

        best = min(same, key=lambda s: (s["slot"] in used, distance(s)))
        chosen[f["filament"]] = best
        used.add(best["slot"])
    return chosen


def resolve_mapping(filaments, slots, mapping):
    """The slot of each filament: those the user gave, the rest suggested. Raises ValueError with the problem."""
    by_number = {s["slot"]: s for s in slots}
    numbers = {f["filament"] for f in filaments}
    chosen = {}
    for entry in mapping or []:
        if not isinstance(entry, dict) or "filament" not in entry or "slot" not in entry:
            raise ValueError('Each mapping entry needs "filament" and "slot", such as {"filament": 1, "slot": 3}')
        number = _as_int(entry["filament"], 0)
        slot_key = "external" if str(entry["slot"]).strip().lower() == "external" else _as_int(entry["slot"], 0)
        if number not in numbers:
            raise ValueError(f"The print has no filament {entry['filament']}; it uses {sorted(numbers)}")
        if slot_key not in by_number:
            raise ValueError(f"Slot {entry['slot']} is empty or does not exist; loaded: "
                             + ", ".join(str(s["slot"]) for s in slots))
        chosen[number] = by_number[slot_key]
    suggested = suggest_mapping([f for f in filaments if f["filament"] not in chosen], slots)
    for number, slot in suggested.items():
        chosen.setdefault(number, slot)
    problems = []
    for f in filaments:
        slot = chosen.get(f["filament"])
        if slot is None:
            problems.append(f"no loaded slot holds {f['type']} for filament {f['filament']}")
        elif normalize_material(slot["type"]) != normalize_material(f["type"]):
            problems.append(f"filament {f['filament']} is {f['type']} but slot {slot['slot']} holds {slot['type']}")
        elif slot["slot"] == "external" and len(filaments) > 1:
            problems.append("the external spool can only print a single-filament print")
    if problems:
        raise ValueError("Cannot map the filaments: " + "; ".join(problems) + ".")
    return chosen


def _default_ams_mapping(use_ams=True):
    """1-to-1 ACE/rack slot mapping built from current telemetry (mirrors start_print_job)."""
    ams_mapping = []
    fils = [f for f in telemetry.get("filaments", [])
            if f.get("source") not in ("external", "external_mcb") and f.get("available", True)]
    for i in range(len(fils) if fils else (4 if use_ams else 0)):
        f = fils[i] if fils and i < len(fils) else {}
        col = f.get("color", "#23a3c7")
        if isinstance(col, str) and col.startswith("#"):
            c_hex = col.lstrip("#")
            r = int(c_hex[0:2], 16) if len(c_hex) >= 2 else 0
            g = int(c_hex[2:4], 16) if len(c_hex) >= 4 else 210
            b = int(c_hex[4:6], 16) if len(c_hex) >= 6 else 255
            rgb = [r, g, b]
        else:
            rgb = [35, 163, 199]
        ams_mapping.append({
            "ams_index": int(f.get("slot", i)),
            "paint_index": i,
            "material_type": f.get("type", "PLA"),
            "ams_color": rgb,
            "paint_color": rgb
        })
    return ams_mapping


def upload_and_start_print_file(content_bytes, raw_filename, use_ams=True, task_settings=None, ams_mapping=None):
    """Upload a user-picked .gcode/.3mf over HTTP, then start it via MQTT as a local task.

    Transport mirrors the proven Remote Print path: the file bytes travel over HTTP
    to the printer's gcode_upload endpoint; only the start trigger uses MQTT, with
    filetype 1 (local) and no cloud URL.
    """
    if len(content_bytes) > MAX_UPLOAD_FILE_BYTES:
        return {"status": "error", "message": "File is too large to upload"}
    if not mqtt_client or not telemetry.get("connected"):
        return {"status": "error", "message": "Printer bridge is not connected"}

    fname = sanitize_print_filename(raw_filename)
    fmd5 = hashlib.md5(content_bytes).hexdigest().lower()
    fsize = len(content_bytes)

    upload_url = telemetry.get("upload_url", f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload")
    tok = telemetry.get("token") or urllib.parse.parse_qs(urllib.parse.urlparse(upload_url).query).get("s", [""])[0]
    if tok and "?s=" not in upload_url:
        upload_url = f"http://{PRINTER_IP}:{PRINTER_HTTP_PORT}/gcode_upload?s={tok}"
    boundary = "----WebKitFormBoundary" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
    part1 = f'--{boundary}\r\nContent-Disposition: form-data; name="filename"\r\n\r\n{fname}\r\n'
    part2 = f'--{boundary}\r\nContent-Disposition: form-data; name="gcode"; filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'
    part3 = f'\r\n--{boundary}--\r\n'
    body = part1.encode("utf-8") + part2.encode("utf-8") + content_bytes + part3.encode("utf-8")
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "X-File-Length": str(fsize),
        "X-BBL-Client-Name": "AnycubicSlicerNext",
        "X-BBL-Client-Type": "slicer",
        "X-BBL-Client-Version": "01.03.09.04",
        "User-Agent": "AnycubicSlicerNext/2.0.0.2"
    }
    req = urllib.request.Request(upload_url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        resp_text = r.read().decode("utf-8", errors="ignore")
    print(f"[Bridge] Uploaded print file {fname} ({fsize} bytes): {resp_text}")
    try:
        resp_json = json.loads(resp_text)
        if int(resp_json.get("code", 0)) != 200:
            return {"status": "error", "message": resp_json.get("message", "printer rejected upload")}
    except Exception:
        pass

    time.sleep(0.5)

    parsed_layers = _parse_total_layers_from_bytes(content_bytes)
    if parsed_layers > 0:
        telemetry["total_layers"] = parsed_layers
        telemetry["curr_layer"] = 0
    if fname and not is_command_filename(fname):
        telemetry["job_name"] = fname

    start_name = start_filename_for_print(fname)
    parsed_time = _parse_print_time_from_bytes(content_bytes)
    if parsed_time > 0:
        telemetry["total_print_time"] = parsed_time
        telemetry["remain_time"] = parsed_time
        b_name = os.path.basename(fname)
        job_estimated_times[b_name] = parsed_time
        job_estimated_times[os.path.basename(start_name)] = parsed_time
        if b_name.lower().endswith(".gcode"):
            job_estimated_times[b_name[:-6]] = parsed_time
        elif b_name.lower().endswith(".3mf"):
            job_estimated_times[b_name[:-4]] = parsed_time

    parsed_cost = parse_cost_from_bytes(content_bytes)
    if parsed_cost:
        record_job_cost(start_name, *parsed_cost)

    if ams_mapping is None:
        ams_mapping = _default_ams_mapping(use_ams)
    ts = task_settings or {}
    auto_leveling = 1 if ts.get("auto_leveling", 1) else 0
    vibration_compensation = 1 if ts.get("vibration_compensation", 0) else 0
    flow_calibration = 1 if ts.get("flow_calibration", 0) else 0
    timelapse_status = 1 if ts.get("timelapse", 0) else 0

    topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/print"
    msg = {
        "type": "print",
        "action": "start",
        "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
        "timestamp": int(time.time() * 1000),
        "data": {
            "taskid": "-1",
            "filename": start_name,
            "url": "",
            "md5": fmd5,
            "filepath": None,
            "filetype": 1,
            "project_type": 1,
            "filesize": fsize,
            "ams_settings": {"use_ams": use_ams, "ams_box_mapping": ams_mapping},
            "task_settings": {
                "auto_leveling": auto_leveling,
                "vibration_compensation": vibration_compensation,
                "flow_calibration": flow_calibration,
                "dry_mode": 0,
                "ai_settings": {"status": 0, "count": 0, "type": 0},
                "timelapse": {"status": timelapse_status, "count": 0, "type": 0},
                "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                "model_objects_skip_parts": []
            }
        }
    }
    mqtt_client.publish(topic, json.dumps(msg))
    print(f"[Bridge] Triggered print:start for {fname} (start name {start_name}, use_ams={use_ams})")
    return {"status": "ok", "filename": fname, "start_filename": start_name, "filesize": fsize}


def send_video_command(action_name):
    """Publish video command (startCapture/stopCapture) to both web and slicer topics."""
    if not mqtt_client:
        return
    msg = {
        "type": "video",
        "action": action_name,
        "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
        "timestamp": int(time.time() * 1000),
        "data": None
    }
    payload = json.dumps(msg)
    topics = [
        f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/video",
        f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/video",
    ]
    for top in topics:
        try:
            mqtt_client.publish(top, payload)
        except Exception as e:
            print(f"[Bridge] Error publishing {action_name} to {top}: {e}")
    print(f"[Bridge] Published {action_name} to video topics")


# The printer's own camera streams H.264 in FLV from port 18088 once it gets startCapture.
# All viewers (the Device tab, a reconnecting player, MCP snapshots) share one connection
# to it: the camera serves few clients and is slow to start, so a second connection or a
# quick reconnect used to fail while the first one was still open. A new viewer gets the
# stream header and the codec headers, then the stream from its next keyframe. When the
# printer drops the stream, the relay reconnects and keeps the timestamps going forward,
# so players see one continuous stream.
FLV_CONNECT_TIMEOUT = 3
FLV_READ_TIMEOUT = 10        # No data for this long: the stream stalled; reconnect.
FLV_START_TIMEOUT = 20       # The camera can take this long to start after startCapture.
FLV_IDLE_CLOSE = 15          # Keep the stream this long after the last viewer, for a quick reconnect.
FLV_KEEPALIVE = 20           # The printer stops the camera without a periodic startCapture.
FLV_KEEPALIVE_MAX = 120      # The longest it grows to while the camera service answers slowly.
FLV_CLIENT_MAX_BYTES = 8 * 1024 * 1024  # A viewer this far behind is dropped; it reconnects.
FLV_GOP_MAX_BYTES = 4 * 1024 * 1024     # The frames since the last keyframe kept for new viewers.


def _flv_tag(tag_type, body, timestamp):
    ts = max(0, int(timestamp)) & 0xFFFFFFFF
    header = bytes([tag_type]) + len(body).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big") + bytes([ts >> 24]) + b"\x00\x00\x00"
    return header + body + (11 + len(body)).to_bytes(4, "big")


class _RelayClient:
    def __init__(self):
        self.chunks = []
        self.size = 0
        self.closed = False
        self.waiting_keyframe = True
        self.video_seq = None
        self.audio_seq = None

    def push(self, data):
        self.chunks.append(data)
        self.size += len(data)
        if self.size > FLV_CLIENT_MAX_BYTES:
            self.closed = True


class FlvRelay:
    def __init__(self, url_fn, open_fn=None):
        self.url_fn = url_fn
        self.open_fn = open_fn or self._open
        self.cond = threading.Condition()
        self.clients = set()
        self.header = None
        self.meta = None
        self.video_seq = None
        self.audio_seq = None
        self.thread = None
        self.error = ""
        self.idle_since = time.time()
        self.starting = 0
        self.new_segment = True
        self.last_ts = -1
        self.frame_gap = 40
        # The tags since the last keyframe: a new viewer starts on them at once instead of
        # waiting for the next keyframe, which the printer's camera sends seldom.
        self.gop = []
        self.gop_bytes = 0
        self.connected = False
        self.keepalive = FLV_KEEPALIVE
        self.camera_reply = None
        self.last_tag_time = 0.0
        self.keyframe_times = []
        self.offset = None

    # Viewers.

    def attach(self, timeout=FLV_START_TIMEOUT):
        """A new viewer, once the stream has started. Raises ValueError when the camera does not start."""
        client = _RelayClient()
        with self.cond:
            # Counted while it waits, so the stream does not close as unwatched meanwhile.
            self.starting += 1
            try:
                if self.thread is None or not self.thread.is_alive():
                    self.error = ""
                    self.thread = threading.Thread(target=self._run, daemon=True)
                    self.thread.start()
                started = self.cond.wait_for(lambda: self.header is not None, timeout)
            finally:
                self.starting -= 1
                self.idle_since = time.time()
            if not started:
                raise ValueError(self.error or "the camera did not start")
            # The stream header goes first; tags reach the viewer only from here on.
            client.push(self.header)
            if self.meta:
                client.push(self.meta)
            self._start_on_gop(client)
            self.clients.add(client)
        return client

    def _start_on_gop(self, client):
        if self.video_seq is None or not self.gop:
            return
        for seq, attr in ((self.video_seq, "video_seq"), (self.audio_seq, "audio_seq")):
            if seq is not None:
                client.push(seq)
                setattr(client, attr, seq)
        for tag in self.gop:
            client.push(tag)
        client.waiting_keyframe = False

    def on_camera_reply(self, code, message, state):
        """The printer's answer to startCapture or stopCapture."""
        failed = (code not in (200, 0)) or state in ("failed", "abnormal")
        with self.cond:
            if failed:
                self.camera_reply = f"the printer's camera service answered {code}: {message or state}".strip()
                # The service is busy (11402 is its timeout): ask it less often.
                self.keepalive = min(FLV_KEEPALIVE_MAX, self.keepalive * 2)
            else:
                self.camera_reply = None
                self.keepalive = FLV_KEEPALIVE
        if failed:
            print(f"[Bridge] Camera command failed ({code}): {message or state}")

    def status(self):
        with self.cond:
            now = time.time()
            gaps = [b - a for a, b in zip(self.keyframe_times, self.keyframe_times[1:])]
            return {
                "running": self.thread is not None,
                "connected": self.connected,
                "viewers": len(self.clients),
                "error": self.error or self.camera_reply or None,
                "seconds_since_last_frame": round(now - self.last_tag_time, 1) if self.last_tag_time else None,
                "keyframe_interval_s": round(sum(gaps) / len(gaps), 1) if gaps else None,
                "url": self.url_fn(),
                # Where an MJPEG camera answers on the printer, for a player the native one fails.
                "mjpeg_url": mjpeg_camera["url"] if (self.error or self.camera_reply) and not self.connected else None,
            }

    def detach(self, client):
        with self.cond:
            self.clients.discard(client)
            if not self.clients:
                self.idle_since = time.time()

    def read(self, client, timeout=FLV_READ_TIMEOUT * 2):
        """The next bytes for a viewer, or None when it is dropped or nothing came in timeout seconds."""
        with self.cond:
            self.cond.wait_for(lambda: client.chunks or client.closed, timeout)
            if not client.chunks:
                return None
            data = b"".join(client.chunks)
            client.chunks = []
            client.size = 0
            return data

    # The printer side.

    def _open(self, url):
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=FLV_CONNECT_TIMEOUT)
        conn.connect()
        conn.sock.settimeout(FLV_READ_TIMEOUT)  # Then reads wait this long for data before the stream counts as stalled.
        conn.request("GET", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""),
                     headers={"User-Agent": "AnycubicSlicerNext/2.0.0.2"})
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise OSError(f"the camera answered HTTP {resp.status}")
        return resp

    def _idle(self):
        with self.cond:
            return not self.clients and not self.starting and time.time() - self.idle_since > FLV_IDLE_CLOSE

    def _run(self):
        failures = 0
        while not shutdown_event.is_set() and not self._idle():
            try:
                self._stream_once()
                failures = 0
            except Exception as e:
                failures += 1
                with self.cond:
                    self.error = str(e) or e.__class__.__name__
                    for c in self.clients:
                        c.waiting_keyframe = True
                    self.cond.notify_all()
                print(f"[Bridge] Camera stream interrupted: {self.error}")
            time.sleep(min(5.0, 0.5 * failures))
        with self.cond:
            self.header = None
            self.meta = None
            self.thread = None
            self.cond.notify_all()
        send_video_command("stopCapture")
        print("[Bridge] Camera stream closed: no viewers")

    def _connect(self):
        """Ask the printer to start the camera and connect as soon as it serves the stream."""
        deadline = time.time() + FLV_START_TIMEOUT
        last_error = None
        send_video_command("startCapture")
        while time.time() < deadline and not shutdown_event.is_set():
            try:
                return self.open_fn(self.url_fn())
            except Exception as e:
                last_error = e
                time.sleep(0.7)
                if self._idle():
                    break
        raise OSError(self.camera_reply or f"no camera stream at {self.url_fn()} ({last_error})")

    @staticmethod
    def _read_exact(resp, n):
        data = b""
        while len(data) < n:
            chunk = resp.read(n - len(data))
            if not chunk:
                raise EOFError("the camera closed the stream")
            data += chunk
        return data

    def _stream_once(self):
        resp = self._connect()
        try:
            head = self._read_exact(resp, 9)
            if head[:3] != b"FLV":
                raise ValueError("the camera stream is not FLV")
            self._read_exact(resp, int.from_bytes(head[5:9], "big") - 9 + 4)
            with self.cond:
                if self.header is None:
                    self.header = head[:5] + (9).to_bytes(4, "big") + b"\x00\x00\x00\x00"
                self.error = ""
                self.connected = True
                self.cond.notify_all()
            with self.cond:
                self.new_segment = True
            last_keepalive = time.time()
            while not self._idle():
                h = self._read_exact(resp, 11)
                tag_type = h[0] & 0x1F
                size = int.from_bytes(h[1:4], "big")
                ts = int.from_bytes(h[4:7], "big") | (h[7] << 24)
                body = self._read_exact(resp, size)
                self._read_exact(resp, 4)
                if time.time() - last_keepalive > self.keepalive:
                    send_video_command("startCapture")
                    last_keepalive = time.time()
                self._on_tag(tag_type, body, ts)
        finally:
            with self.cond:
                self.connected = False
                # Frames of the old connection cannot start a viewer on the new one.
                self.gop = []
                self.gop_bytes = 0
            resp.close()

    def _on_tag(self, tag_type, body, ts):
        with self.cond:
            if tag_type == 18:
                if self.meta is None:
                    self.meta = _flv_tag(18, body, 0)
                return
            is_video = tag_type == 9 and len(body) >= 2
            if is_video and (body[0] & 0x0F) == 7 and body[1] == 0:
                self.video_seq = _flv_tag(9, body, 0)
                for c in self.clients:
                    if not c.waiting_keyframe and c.video_seq != self.video_seq:
                        c.push(self.video_seq)
                        c.video_seq = self.video_seq
                self.cond.notify_all()
                return
            if tag_type == 8 and len(body) >= 2 and (body[0] >> 4) == 10 and body[1] == 0:
                self.audio_seq = _flv_tag(8, body, 0)
                return
            if tag_type not in (8, 9):
                return
            # Timestamps keep rising across reconnects and camera restarts.
            if self.new_segment or self.offset is None or ts + self.offset < self.last_ts - 1000:
                # One frame after the last one sent.
                self.offset = (self.last_ts + self.frame_gap if self.last_ts >= 0 else 0) - ts
                self.new_segment = False
            out_ts = max(0, ts + self.offset)
            if is_video and 0 < out_ts - self.last_ts <= 1000:
                self.frame_gap = out_ts - self.last_ts
            self.last_ts = max(self.last_ts, out_ts)
            tag = _flv_tag(tag_type, body, out_ts)
            keyframe = is_video and (body[0] >> 4) == 1
            now = time.time()
            self.last_tag_time = now
            if keyframe:
                self.gop = [tag]
                self.gop_bytes = len(tag)
                self.keyframe_times = (self.keyframe_times + [now])[-6:]
            elif self.gop:
                self.gop.append(tag)
                self.gop_bytes += len(tag)
                if self.gop_bytes > FLV_GOP_MAX_BYTES:
                    self.gop = []
                    self.gop_bytes = 0
            for c in self.clients:
                if c.waiting_keyframe:
                    if not keyframe or self.video_seq is None:
                        continue
                    c.waiting_keyframe = False
                    for seq, attr in ((self.video_seq, "video_seq"), (self.audio_seq, "audio_seq")):
                        if seq is not None and getattr(c, attr) != seq:
                            c.push(seq)
                            setattr(c, attr, seq)
                c.push(tag)
            self.cond.notify_all()


# An MJPEG camera some printers serve beside or instead of the native one (a Rinkhals
# camera app, mjpg-streamer, acproxycam). Probed when the native camera fails, at most
# once a minute.
MJPEG_CAMERA_PORTS = (8080, 8000)
mjpeg_camera = {"url": None, "checked": 0.0}
mjpeg_camera_lock = threading.Lock()


def find_mjpeg_camera():
    with mjpeg_camera_lock:
        if time.time() - mjpeg_camera["checked"] < 60:
            return mjpeg_camera["url"]
        mjpeg_camera["checked"] = time.time()
        found = None
        for port in MJPEG_CAMERA_PORTS:
            try:
                conn = http.client.HTTPConnection(PRINTER_IP, port, timeout=2)
                conn.request("GET", "/?action=snapshot", headers={"User-Agent": "OrcaMartelo"})
                resp = conn.getresponse()
                ok = resp.status == 200 and resp.getheader("Content-Type", "").startswith("image/")
                conn.close()
                if ok:
                    found = f"http://{PRINTER_IP}:{port}/?action=stream"
                    break
            except Exception:
                pass
        mjpeg_camera["url"] = found
        return found


def native_camera_url():
    url = telemetry.get("raw_camera_url") or ""
    return url if url.startswith("http") else f"http://{PRINTER_IP}:18088/flv"


camera_relay = FlvRelay(native_camera_url)


def perform_control(data):
    """Run one /control action. Returns the JSON answer."""
    result = {"status": "ok", "ip": PRINTER_IP}
    action = data.get("action", "")
    if action and action not in ("clear_alert", "set_camera_url") and (not mqtt_client or not telemetry.get("connected")):
        result = {"status": "error", "message": "Printer bridge is not connected", "ip": PRINTER_IP}
        action = ""
    if action in ["pause", "resume", "stop"]:
        topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/print"
        msg = {
            "type": "print",
            "action": action,
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": None
        }
        if mqtt_client:
            mqtt_client.publish(topic, json.dumps(msg))

    elif action in ("light", "nozzle_light", "camera_light"):
        target = data.get("target", "")
        if action == "nozzle_light" or target == "nozzle":
            light_type = 1
        elif action == "camera_light" or target == "camera":
            light_type = 2
        else:
            light_type = int(data.get("type", 2))

        status = int(data.get("status", 1))
        brightness = 100 if status == 1 else 0

        msg = {
            "type": "light",
            "action": "control",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "type": light_type,
                "status": status,
                "brightness": brightness
            }
        }
        payload_str = json.dumps(msg)
        if mqtt_client:
            for top in (
                f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/light",
                f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/light"
            ):
                try:
                    mqtt_client.publish(top, payload_str)
                except Exception:
                    pass
            # If camera light (type 2), also publish type 3 for Kobra X compatibility
            if light_type == 2:
                try:
                    msg3 = dict(msg)
                    msg3["data"] = {"type": 3, "status": status, "brightness": brightness}
                    mqtt_client.publish(f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/light", json.dumps(msg3))
                except Exception:
                    pass
            print(f"[Bridge] Published light (type={light_type}) status={status} to light topics")

        if light_type == 1:
            telemetry["nozzle_light"] = status
            try:
                upload_and_run_gcode(f"M355 S{status}\nSET_PIN PIN=head_light VALUE={status}")
            except Exception:
                pass
        else:
            telemetry["camera_light"] = status
            telemetry["light"] = status

    elif action == "set_camera_url":
        custom_url = str(data.get("url", data.get("camera_url", "")) or "").strip()
        telemetry["custom_camera_url"] = custom_url
        return {"status": "ok", "custom_camera_url": custom_url}

    elif action == "start_capture" or action == "video_start":
        send_video_command("startCapture")

    elif action == "stop_capture" or action == "video_stop":
        # While the shared stream runs, it stops the camera itself once its last viewer is gone.
        if camera_relay.thread is None:
            send_video_command("stopCapture")

    elif action == "speed":
        mode = int(data.get("mode", 2))
        topic = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/print"
        msg = {
            "type": "print",
            "action": "setSpeedMode",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "print_speed_mode": mode
            }
        }
        if mqtt_client:
            mqtt_client.publish(topic, json.dumps(msg))
        telemetry["speed_mode"] = mode

    elif action == "jog":
        axis_name = data.get("axis", "X").upper()
        axis_map = {"X": 1, "Y": 2, "Z": 3, "XY": 4, "XYZ": 5}
        ax = axis_map.get(axis_name, 1)
        direction = int(data.get("dir", 1))
        move_type = 1 if direction > 0 else 0
        dist = abs(float(data.get("dist", 10)))
        topic = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/axis"
        msg = {
            "type": "axis",
            "action": "move",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "axis": ax,
                "move_type": move_type,
                "distance": dist
            }
        }
        if mqtt_client:
            mqtt_client.publish(topic, json.dumps(msg))
            print(f"[Bridge] Published jog {axis_name} {direction} {dist}mm")

    elif action == "home":
        axis_name = data.get("axis", "XYZ").upper()
        ax = 5 if axis_name == "XYZ" else (4 if axis_name == "XY" else (3 if axis_name == "Z" else 1))
        topic = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/axis"
        msg = {
            "type": "axis",
            "action": "move",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "axis": ax,
                "move_type": 2,
                "distance": 0
            }
        }
        if mqtt_client:
            mqtt_client.publish(topic, json.dumps(msg))
            print(f"[Bridge] Published Home {axis_name}")

    elif action == "clear_alert":
        telemetry["last_error"] = ""
        telemetry["last_error_code"] = 0
        telemetry["axis_error"] = ""

    elif action == "motor_off" or action == "turnOff":
        topic = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/axis"
        msg = {
            "type": "axis",
            "action": "turnOff",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": None
        }
        if mqtt_client:
            mqtt_client.publish(topic, json.dumps(msg))
            print("[Bridge] Published Motor Turn Off")

    elif action == "switch_channel":
        channel_idx = max(0, min(3, _as_int(data.get("index", data.get("slot", 0)), 0)))
        msg = {
            "type": "extrudeControl",
            "action": "switchChannel",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {"index": channel_idx}
        }
        if mqtt_client:
            topic_ext = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/extrudeControl"
            mqtt_client.publish(topic_ext, json.dumps(msg))
            print(f"[Bridge] Published switchChannel to slot {channel_idx + 1} (index {channel_idx})")
            threading.Timer(1.5, query_extrude_control).start()

    elif action == "start_print_job":
        filename = data.get("filename", "")
        if filename and mqtt_client:
            topic_print = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/print"
            # Construct 1-to-1 AMS box mapping for all 4 slots matching physical feeder
            ams_mapping = data.get("ams_box_mapping")
            use_ams = bool(data.get("use_ams", True))
            if not ams_mapping:
                ams_mapping = []
                fils = [f for f in telemetry.get("filaments", []) if f.get("source") not in ("external", "external_mcb") and f.get("available", True)]
                for i in range(len(fils) if fils else (4 if use_ams else 0)):
                    f = fils[i] if fils and i < len(fils) else {}
                    col = f.get("color", "#23a3c7")
                    if isinstance(col, str) and col.startswith("#"):
                        c_hex = col.lstrip("#")
                        r = int(c_hex[0:2], 16) if len(c_hex) >= 2 else 0
                        g = int(c_hex[2:4], 16) if len(c_hex) >= 4 else 210
                        b = int(c_hex[4:6], 16) if len(c_hex) >= 6 else 255
                        rgb = [r, g, b]
                    else:
                        rgb = [35, 163, 199]
                    ams_mapping.append({
                        "ams_index": int(f.get("slot", i)),
                        "paint_index": i,
                        "material_type": f.get("type", "PLA"),
                        "ams_color": rgb,
                        "paint_color": rgb
                    })

            requested_tasks = data.get("task_settings", {})
            auto_leveling = 1 if requested_tasks.get("auto_leveling", 1) else 0
            vibration_compensation = 1 if requested_tasks.get("vibration_compensation", 0) else 0
            flow_calibration = 1 if requested_tasks.get("flow_calibration", 0) else 0
            timelapse_status = 1 if requested_tasks.get("timelapse", 0) else 0

            target_filename = start_filename_for_print(filename)

            msg = {
                "type": "print",
                "action": "start",
                "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
                "timestamp": int(time.time() * 1000),
                "data": {
                    "taskid": "-1",
                    "filename": target_filename,
                    "url": "",
                    "md5": "",
                    "filepath": None,
                    "filetype": 1,
                    "project_type": 1,
                    "filesize": 0,
                    "ams_settings": {
                        "use_ams": use_ams,
                        "ams_box_mapping": ams_mapping
                    },
                    "task_settings": {
                        "auto_leveling": auto_leveling,
                        "vibration_compensation": vibration_compensation,
                        "flow_calibration": flow_calibration,
                        "dry_mode": 0,
                        "ai_settings": {"status": 0, "count": 0, "type": 0},
                        "timelapse": {"status": timelapse_status, "count": 0, "type": 0},
                        "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                        "model_objects_skip_parts": []
                    }
                }
            }
            total_layers = _as_int(data.get("total_layers", 0), 0)
            if total_layers > 0:
                telemetry["total_layers"] = total_layers
                telemetry["curr_layer"] = 0
            if filename and not is_command_filename(filename):
                telemetry["job_name"] = filename

            total_min = _as_int(data.get("estimated_print_time_min", 0), 0)
            if total_min <= 0:
                dhms = data.get("estimated_print_time_dhms")
                if dhms:
                    total_min = _parse_time_str_to_minutes(dhms)
            if total_min > 0:
                telemetry["total_print_time"] = total_min
                telemetry["remain_time"] = total_min
                b_name = os.path.basename(filename)
                job_estimated_times[b_name] = total_min
                job_estimated_times[os.path.basename(target_filename)] = total_min
                if b_name.lower().endswith(".gcode"):
                    job_estimated_times[b_name[:-6]] = total_min
                elif b_name.lower().endswith(".3mf"):
                    job_estimated_times[b_name[:-4]] = total_min

            record_job_cost(target_filename, data.get("estimated_cost"), data.get("estimated_weight_g"),
                            data.get("estimated_filament_mm"))
            mqtt_client.publish(topic_print, json.dumps(msg))
            print(f"[Bridge] Published print:start for {filename} with use_ams={use_ams} ams_mapping={json.dumps(ams_mapping)}")
        else:
            result = {"status": "error", "message": "Print filename or printer connection is unavailable", "ip": PRINTER_IP}

    elif action == "feed_filament":
        slot_idx = int(data.get("slot", 0))
        box_id = int(data.get("box_id", -1))
        m_type = data.get("type", "PLA")
        topic_feed = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/multiColorBox"
        msg = {
            "type": "multiColorBox",
            "action": "feedFilament",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "multi_color_box": [{
                    "id": box_id,
                    "feed_status": {
                        "slot_index": slot_idx,
                        "type": 1,
                        "material_type": m_type
                    }
                }]
            }
        }
        topic_ext = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/extrudeControl"
        msg_ext = {
            "type": "extrudeControl",
            "action": "switchChannel",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {"index": slot_idx}
        }
        if mqtt_client:
            mqtt_client.publish(topic_feed, json.dumps(msg))
            mqtt_client.publish(topic_ext, json.dumps(msg_ext))
            telemetry["feed_status"] = {"box_id": box_id, "slot_index": slot_idx, "type": 1, "current_status": 1, "code": 0}
            print(f"[Bridge] Published feedFilament box {box_id} slot {slot_idx} ({m_type}) and switchChannel {slot_idx}")

    elif action == "unfeed_filament":
        slot_idx = int(data.get("slot", 0))
        box_id = int(data.get("box_id", -1))
        topic_feed = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/multiColorBox"
        msg = {
            "type": "multiColorBox",
            "action": "feedFilament",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "multi_color_box": [{
                    "id": box_id,
                    "feed_status": {"slot_index": slot_idx, "type": 2}
                }]
            }
        }
        if mqtt_client:
            mqtt_client.publish(topic_feed, json.dumps(msg))
            telemetry["feed_status"] = {"box_id": box_id, "slot_index": slot_idx, "type": 2, "current_status": 1, "code": 0}
            print(f"[Bridge] Published unfeed box {box_id} slot {slot_idx}")

    elif action == "fan":
        fan_value = max(0, min(100, int(data.get("value", 0))))
        topic_fan = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/fan"
        msg = {
            "type": "fan",
            "action": "setSpeed",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {"fan_speed_pct": fan_value}
        }
        if mqtt_client:
            mqtt_client.publish(topic_fan, json.dumps(msg))
            print(f"[Bridge] Published model fan {fan_value}%")
        telemetry["fan"] = fan_value

    elif action == "temp":
        heater = data.get("heater", "nozzle")
        val = int(data.get("value", 0))
        settings = {}
        if heater == "nozzle":
            settings["target_nozzle_temp"] = val
            telemetry["target_nozzle_temp"] = val
        elif heater == "bed":
            settings["target_hotbed_temp"] = val
            telemetry["target_bed_temp"] = val
        elif heater in ("both", "all"):
            nozzle_val = int(data.get("nozzle", val))
            bed_val = int(data.get("bed", val))
            settings["target_nozzle_temp"] = nozzle_val
            settings["target_hotbed_temp"] = bed_val
            telemetry["target_nozzle_temp"] = nozzle_val
            telemetry["target_bed_temp"] = bed_val

        # 1. Direct Klipper / Moonraker dispatch (instant native control if Klipper/Rinkhals is running)
        if PRINTER_IP:
            try:
                gcode_cmds = []
                if heater == "nozzle":
                    gcode_cmds.append(f"M104 S{val}")
                elif heater == "bed":
                    gcode_cmds.append(f"M140 S{val}")
                elif heater in ("both", "all"):
                    gcode_cmds.append(f"M104 S{nozzle_val}")
                    gcode_cmds.append(f"M140 S{bed_val}")
                for cmd in gcode_cmds:
                    req = urllib.request.Request(
                        f"http://{PRINTER_IP}/api/printer/command",
                        data=json.dumps({"command": cmd}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(req, timeout=1)
                    print(f"[Bridge] Sent direct Klipper G-code via Moonraker: {cmd}")
            except Exception as e:
                # Non-Klipper firmware or Moonraker endpoint unavailable; fallback to MQTT seamlessly
                pass

        # 2. Primary Kobra 3 protocol: type print, action update, settings target_hotbed_temp / target_nozzle_temp
        msg = {
            "type": "print",
            "action": "update",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "taskid": "-1",
                "settings": settings
            }
        }
        payload_str = json.dumps(msg)
        for topic_role in ("slicer", "web"):
            top = f"anycubic/anycubicCloud/v1/{topic_role}/printer/{model_id}/{device_id}/print"
            if mqtt_client:
                mqtt_client.publish(top, payload_str)

        # Legacy fallback for older firmware models
        legacy_msg = {
            "type": "tempature",
            "action": "set",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "type": 0 if heater == "nozzle" else 1,
                "target_hotbed_temp": settings.get("target_hotbed_temp", 0),
                "target_nozzle_temp": settings.get("target_nozzle_temp", 0)
            }
        }
        legacy_str = json.dumps(legacy_msg)
        for topic_role in ("slicer", "web"):
            top_legacy = f"anycubic/anycubicCloud/v1/{topic_role}/printer/{model_id}/{device_id}/tempature"
            if mqtt_client:
                mqtt_client.publish(top_legacy, legacy_str)

        print(f"[Bridge] Published set temperature {heater}={val} (settings={settings})")

    elif action == "extrude":
        direction = data.get("dir", "extrude")
        dist = abs(float(data.get("dist", 10)))
        sign = 1 if direction == "extrude" else -1
        e_dist = sign * dist
        gcode = f"; Extrude filament\nM83\nG1 E{e_dist:.1f} F150\nM400\n"
        try:
            result = upload_and_run_gcode(gcode, delete_after=True)
            print(f"[Bridge] Extrude executed: {direction} {dist}mm")
        except Exception as e:
            result = {"status": "error", "message": str(e)}

    elif action == "set_ace_drying":
        box_id = int(data.get("box_id", 0))
        status = 1 if data.get("status", 1) else 0
        target_temp = int(data.get("target_temp", 50))
        raw_duration = int(data.get("duration", 240))
        duration = raw_duration * 60 if raw_duration <= 24 else raw_duration
        topic_dry = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/multiColorBox"
        topic_dry_slicer = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/multiColorBox"
        msg = {
            "type": "multiColorBox",
            "action": "setDry",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "multi_color_box": [{
                    "id": box_id,
                    "drying_status": {
                        "status": status,
                        "target_temp": target_temp,
                        "duration": duration,
                        "remain_time": duration if status else 0
                    }
                }]
            }
        }
        if mqtt_client:
            payload_str = json.dumps(msg)
            mqtt_client.publish(topic_dry, payload_str)
            mqtt_client.publish(topic_dry_slicer, payload_str)
            print(f"[Bridge] Published setDry for box {box_id}: status={status}, temp={target_temp}C, duration={duration}m")
        telemetry["ace_drying"] = {
            "box_id": box_id,
            "status": status,
            "target_temp": target_temp,
            "duration": duration,
            "remain_time": duration if status else 0
        }
        result = {"status": "ok", "box_id": box_id, "drying_status": telemetry["ace_drying"], "ip": PRINTER_IP}

    elif action == "set_ace_auto_feed":
        box_id = int(data.get("box_id", 0))
        auto_feed = 1 if data.get("auto_feed", 1) else 0
        topic_auto = f"anycubic/anycubicCloud/v1/web/printer/{model_id}/{device_id}/multiColorBox"
        topic_auto_slicer = f"anycubic/anycubicCloud/v1/slicer/printer/{model_id}/{device_id}/multiColorBox"
        msg = {
            "type": "multiColorBox",
            "action": "setAutoFeed",
            "msgid": "".join(random.choices(string.hexdigits.lower(), k=32)),
            "timestamp": int(time.time() * 1000),
            "data": {
                "multi_color_box": [{
                    "id": box_id,
                    "auto_feed": auto_feed
                }]
            }
        }
        if mqtt_client:
            payload_str = json.dumps(msg)
            mqtt_client.publish(topic_auto, payload_str)
            mqtt_client.publish(topic_auto_slicer, payload_str)
            print(f"[Bridge] Published setAutoFeed for box {box_id}: auto_feed={auto_feed}")
        telemetry["ace_auto_feed"] = auto_feed
        result = {"status": "ok", "box_id": box_id, "auto_feed": auto_feed, "ip": PRINTER_IP}

    return result


class BridgeServer(BaseHTTPRequestHandler):
    def _send_stream_headers(self, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_camera_error(self, message):
        self.send_response(502)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode("utf-8", errors="replace"))

    def serve_camera(self):
        query_params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        param_url = query_params.get("url", [""])[0]
        custom_url = telemetry.get("custom_camera_url") or ""
        upstream_url = param_url or custom_url
        if upstream_url and not upstream_url.startswith("http"):
            upstream_url = ""

        if not upstream_url and query_params.get("direct", [""])[0] == "1":
            # The printer's own camera on a connection of this viewer alone, as before the
            # shared stream: a fallback for a camera the shared stream does not suit.
            send_video_command("startCapture")
            if not self._serve_direct_camera(native_camera_url(), attempts=int(FLV_START_TIMEOUT / 0.7)):
                self._send_camera_error("Failed to connect to the printer camera at " + native_camera_url())
            return

        if not upstream_url:
            # The printer's own camera, shared by every viewer.
            try:
                client = camera_relay.attach()
            except ValueError as e:
                # An MJPEG camera on the printer is offered through /status/camera: this
                # answer goes to an FLV player, which cannot play MJPEG.
                find_mjpeg_camera()
                self._send_camera_error(f"Failed to start the printer camera: {e}")
                return
            try:
                self._send_stream_headers("video/x-flv")
                while True:
                    data = camera_relay.read(client)
                    if data is None:
                        break
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, socket.error):
                pass
            except Exception as e:
                print(f"[Bridge] Camera proxy exception: {e}")
            finally:
                camera_relay.detach(client)
            return

        if not self._serve_direct_camera(upstream_url):
            self._send_camera_error("Failed to connect to the camera stream at " + upstream_url)

    def _serve_direct_camera(self, url, attempts=3):
        """Relay a camera (a custom MJPEG one, or the native one in Direct mode) to this viewer
        alone. Returns False when it does not answer."""
        resp = None
        for candidate in [url]:
            for _ in range(attempts):
                try:
                    parsed = urllib.parse.urlparse(candidate)
                    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                    conn = conn_cls(parsed.hostname, parsed.port, timeout=FLV_CONNECT_TIMEOUT)
                    conn.connect()
                    conn.sock.settimeout(FLV_READ_TIMEOUT)
                    conn.request("GET", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""),
                                 headers={"User-Agent": "AnycubicSlicerNext/2.0.0.2"})
                    r = conn.getresponse()
                    if r.status == 200:
                        resp = r
                        break
                    conn.close()
                except Exception:
                    pass
                time.sleep(0.7)
            if resp:
                break
        if resp is None:
            return False
        try:
            self._send_stream_headers(resp.headers.get("Content-Type", "video/x-flv"))
            with resp:
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass
        except Exception as e:
            print(f"[Bridge] Camera proxy exception: {e}")
        return True

    def authorized(self):
        token = self.headers.get("X-OrcaCubic-Token", "")
        if not token:
            parsed_query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = parsed_query.get("token", [""])[0] or parsed_query.get("X-OrcaCubic-Token", [""])[0]
        if token and token == BRIDGE_TOKEN:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1", "localhost"):
            origin = self.headers.get("Origin", "")
            if not origin or origin == "null" or "127.0.0.1" in origin or "localhost" in origin:
                return True
        return False

    def require_authorized(self):
        if self.authorized():
            return True
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"forbidden"}')
        return False

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OrcaCubic-Token, X-OrcaCubic-Printer, Range")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/slicer/wait"):
            if not self.require_authorized():
                return
            request = wait_slicer_request()
            if request is None:
                self.send_response(204)
                self.end_headers()
            else:
                self.send_json(200, request)
        elif self.path.startswith("/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "ip": PRINTER_IP}).encode("utf-8"))
        elif self.path.startswith("/camera"):
            self.serve_camera()
        elif not self.require_authorized():
            return
        elif self.path.startswith("/status/camera"):
            self.send_json(200, camera_relay.status())
        elif self.path.startswith("/mcp/info"):
            self.send_json(200, mcp_client_config())
        elif urllib.parse.urlparse(self.path).path == "/mcp":
            # Streamable HTTP without server-initiated messages: no event stream to open.
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()
        elif self.path.startswith("/notifications"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(notifications_since(_as_int(query.get("since", ["0"])[0], 0))).encode("utf-8"))
        elif self.path.startswith("/upload-token"):
            upload_url = telemetry.get("upload_url", "")
            token = urllib.parse.parse_qs(urllib.parse.urlparse(upload_url).query).get("s", [""])[0] or telemetry.get("token", "")
            self.send_response(200 if token else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"upload_token": token, "ip": PRINTER_IP} if token else {"error": "upload token unavailable", "ip": PRINTER_IP}).encode("utf-8"))
        elif self.path.startswith("/status") or self.path == "/" or self.path.startswith("/api/v1"):
            if self.path.startswith("/status"):
                query_extrude_control()
            # Auto-expire historical alerts after 20 seconds or when printing
            if telemetry.get("last_error"):
                err_age = time.time() - telemetry.get("last_error_time", 0)
                st = telemetry.get("state", "").lower()
                if err_age > 20 or st in ["printing", "auto_leveling", "heating", "busy"]:
                    telemetry["last_error"] = ""
                    telemetry["last_error_code"] = 0

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(public_telemetry()).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_mcp(self):
        """MCP over Streamable HTTP: one JSON-RPC message (or batch) per POST, answered as JSON."""
        content_length = _as_int(self.headers.get("Content-Length", 0))
        if content_length <= 0 or content_length > 1048576:
            self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
            return
        text = self.rfile.read(content_length).decode("utf-8", errors="replace")
        answer = orcamartelo_mcp.handle_text(text, mcp_backend)
        if answer is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = answer.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.require_authorized():
            return
        if urllib.parse.urlparse(self.path).path == "/mcp":
            self.handle_mcp()
            return
        if urllib.parse.urlparse(self.path).path == "/slicer/result":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            length = _as_int(self.headers.get("Content-Length", 0))
            try:
                answer = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 2 * SNAPSHOT_MAX_BYTES else None
            except ValueError:
                answer = None
            deliver_slicer_result(_as_int(query.get("id", ["0"])[0]), answer)
            self.send_json(200, {"status": "ok"})
            return
        expected_printer = self.headers.get("X-OrcaCubic-Printer", "")
        if expected_printer and expected_printer != PRINTER_IP:
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Device selection changed"}).encode("utf-8"))
            return
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_REQUEST_BODY_BYTES:
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Request body too large", "ip": PRINTER_IP}).encode("utf-8"))
            return
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            data = json.loads(body)
        except:
            data = {}

        result = {"status": "ok", "ip": PRINTER_IP}

        if self.path.startswith("/notifications/read"):
            ids = data.get("ids")
            mark_notifications_read(set(_as_int(i, -1) for i in ids) if isinstance(ids, list) else None)
            result = notifications_since(0)
            result["notifications"] = []
        elif self.path.startswith("/notifications/clear"):
            clear_notifications()
            result = notifications_since(0)
        elif self.path.startswith("/shutdown"):
            result = {"status": "ok", "ip": PRINTER_IP}
            shutdown_event.set()
            if mqtt_client:
                try:
                    mqtt_client.disconnect()
                    mqtt_client.loop_stop()
                except Exception:
                    pass
            if server_instance:
                threading.Thread(target=server_instance.shutdown, daemon=True).start()
        elif self.path.startswith("/control"):
            result = perform_control(data)
        elif self.path.endswith("/mach_mqtt/publish"):
            topic = data.get("topic", "")
            payload_raw = data.get("payload", "")
            if topic and mqtt_client:
                mqtt_client.publish(topic, payload_raw)
                print(f"[Bridge] Proxy-published to {topic}")
                result = {"code": 200, "data": None, "msg": "ok", "ip": PRINTER_IP}
            else:
                result = {"code": 503, "data": None, "msg": "Printer bridge is not connected", "ip": PRINTER_IP}

        elif self.path.startswith("/run_gcode"):
            gcode = data.get("gcode", "")
            delete_after = data.get("delete_after", True)
            if gcode.strip():
                try:
                    result = upload_and_run_gcode(gcode, delete_after=delete_after)
                except Exception as e:
                    result = {"status": "error", "message": str(e)}

        elif self.path.startswith("/upload_print_file"):
            # A print file picked in the workbench UI. The bytes arrive base64-encoded
            # in JSON so the existing fetch hook keeps authorizing the request.
            encoded = data.get("content_base64", "")
            fname = data.get("filename", "")
            if not encoded or not fname:
                result = {"status": "error", "message": "Missing filename or file content", "ip": PRINTER_IP}
            else:
                try:
                    content_bytes = base64.b64decode(encoded, validate=True)
                except Exception:
                    content_bytes = b""
                if not content_bytes:
                    result = {"status": "error", "message": "File content could not be decoded", "ip": PRINTER_IP}
                else:
                    try:
                        result = upload_and_start_print_file(
                            content_bytes,
                            fname,
                            use_ams=bool(data.get("use_ams", True)),
                            task_settings=data.get("task_settings") or {}
                        )
                        result["ip"] = PRINTER_IP
                    except Exception as e:
                        result = {"status": "error", "message": f"Upload failed: {e}", "ip": PRINTER_IP}

        elif self.path.startswith("/sync_to_printer"):
            sync_slots_to_printer(data.get("slots", []))
            result = {"status": "ok", "ip": PRINTER_IP}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode("utf-8"))

    def log_message(self, format, *args):
        pass

def run_server():
    global server_instance
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), BridgeServer)
    server_instance = server
    print(f"[Bridge] Local API Server running on http://127.0.0.1:{HTTP_PORT}")
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == "__main__":
    _load_notifications()
    _load_job_costs()
    t1 = threading.Thread(target=mqtt_worker, daemon=True)
    t1.start()
    run_server()
