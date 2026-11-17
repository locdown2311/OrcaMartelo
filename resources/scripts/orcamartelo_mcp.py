"""Model Context Protocol (MCP) server core for the OrcaMartelo printer bridge.

It answers MCP JSON-RPC messages with read-only views of the printer the bridge
watches: print progress, the estimated cost of the job, the loaded filaments and
the notification history. The LAN bridge serves it over HTTP at /mcp
(Streamable HTTP, JSON responses only) and orcamartelo_mcp_stdio.py serves it
over stdio for clients that launch a command. Both use handle_message(); the
stdio bridge passes no backend when the LAN bridge is not running, so a client
still connects and each tool call says why there is no data.
"""

import json

SERVER_INFO = {"name": "orcamartelo", "title": "OrcaMartelo", "version": "1.0.0"}
# Newest first. A client asking for another version gets the newest one, as the spec allows.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Control of the Anycubic printer OrcaMartelo is connected to over the LAN, and of the OrcaMartelo "
    "slicer itself. Read tools (get_*, list_*, slicer_get_*) are free to call. Tools that heat, move, "
    "start or stop a print, or clear the plate ask the user to confirm in an OrcaMartelo window and "
    "fail if they decline; do not retry a declined action. Moving, homing, extruding, G-code and "
    "loading filament are refused while a print runs. To print a model: slicer_load_models, then "
    "slicer_slice to see the time and cost, then print_plate, which opens OrcaMartelo's send dialog where "
    "the user picks the ACE slot of each filament. Costs use the currency of the filament profiles. set_filament_slot only changes what "
    "the printer records for a slot; load_filament moves filament."
)

_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_CONFIRMED = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


def _tool(name, title, description, properties=None, required=(), annotations=_READ):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False},
        "annotations": annotations,
    }


_SLOT = {"type": ["integer", "string"], "description": "Slot number as get_filaments lists it (1 for the first ACE slot)."}
_PRESET_TYPE = {"type": "string", "enum": ["printer", "print", "filament"], "description": "Kind of preset."}
_PLATE = {"type": "integer", "description": "Plate number, 1 for the first; the current plate when left out."}
_CONFIRM_NOTE = " The user confirms it in OrcaMartelo."
_MAPPING = {"type": "array",
            "items": {"type": "object", "properties": {"filament": {"type": "integer"}, "slot": {"type": ["integer", "string"]}},
                      "required": ["filament", "slot"]},
            "description": "Optional: the ACE slot to preselect for each filament, such as [{\"filament\": 1, \"slot\": 3}]. "
                           "The user sees and changes the mapping in OrcaMartelo's send dialog anyway; leave it out to "
                           "let the dialog match materials and colors."}

TOOLS = [
    # Printer state.
    _tool("get_print_status", "Print status",
          "State of the printer and the current print: state, file name, progress in percent, current and total layer, "
          "elapsed and remaining minutes, estimated end time, nozzle and bed temperatures with their targets, fan and speed mode."),
    _tool("get_temperatures", "Temperatures", "Current and target temperatures of the nozzle and the bed, in degrees Celsius."),
    _tool("get_camera_snapshot", "Camera snapshot",
          "Take a picture with the printer's camera now and return it as a JPEG image, to check the print "
          "(spaghetti, detached parts, first layer) or the bed. The picture is also saved as a file, whose path "
          "the result gives, to pass to a model that reads images."),
    _tool("get_print_cost", "Print cost",
          "Estimated cost, filament weight and length of the current print, or of the last one when the printer is idle, "
          "as sliced by OrcaMartelo, and the part spent so far in proportion to the progress. Only prints sent from "
          "OrcaMartelo have a cost."),
    _tool("get_filaments", "Loaded filaments",
          "Spools in the multi-color box (ACE) slots and the external spool: material, color, brand, and which slot is loaded or feeding."),
    _tool("get_ace_status", "ACE status",
          "State of the multi-color box (ACE): its temperature and humidity, whether the dryer is on with its target "
          "temperature and the minutes left, and whether the backup spool switch is on."),
    _tool("get_notifications", "Notifications",
          "Recent print events (started, paused, resumed, finished, stopped, failed) and printer errors, newest first.",
          {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Most events to return (default 20)."},
           "unread_only": {"type": "boolean", "description": "Only events not yet read in OrcaMartelo."}}),
    _tool("get_printer_info", "Printer information", "Printer model, firmware, address, connection state and camera stream URL."),

    # Printer control.
    _tool("pause_print", "Pause print", "Pause the running print.", annotations=_WRITE),
    _tool("resume_print", "Resume print", "Resume a paused print.", annotations=_WRITE),
    _tool("stop_print", "Stop print", "Cancel the current print. It cannot be resumed." + _CONFIRM_NOTE, annotations=_CONFIRMED),
    _tool("set_temperature", "Set temperatures",
          "Set the target temperature of the nozzle, the bed or both; 0 turns a heater off." + _CONFIRM_NOTE,
          {"nozzle": {"type": "integer", "minimum": 0, "maximum": 300, "description": "Nozzle target in °C."},
           "bed": {"type": "integer", "minimum": 0, "maximum": 120, "description": "Bed target in °C."}},
          annotations=_CONFIRMED),
    _tool("set_fan", "Set part fan", "Set the part cooling fan speed.",
          {"percent": {"type": "integer", "minimum": 0, "maximum": 100}}, ["percent"], _WRITE),
    _tool("set_light", "Set light", "Turn the camera (chamber) light or the nozzle light on or off.",
          {"light": {"type": "string", "enum": ["camera", "nozzle"]}, "on": {"type": "boolean"}}, ["light", "on"], _WRITE),
    _tool("set_speed_mode", "Set speed mode", "Set the print speed mode.",
          {"mode": {"type": "string", "enum": ["silent", "standard", "sport"]}}, ["mode"], _WRITE),
    _tool("home_axes", "Home axes", "Home the axes. Only while idle." + _CONFIRM_NOTE,
          {"axes": {"type": "string", "enum": ["XYZ", "XY", "Z"], "description": "Default XYZ."}}, annotations=_CONFIRMED),
    _tool("move_axis", "Move axis", "Move the head (X, Y) or the bed (Z) by a distance. Only while idle." + _CONFIRM_NOTE,
          {"axis": {"type": "string", "enum": ["X", "Y", "Z"]},
           "distance_mm": {"type": "number", "minimum": -100, "maximum": 100, "description": "Negative moves the other way."}},
          ["axis", "distance_mm"], _CONFIRMED),
    _tool("disable_motors", "Disable motors", "Turn the stepper motors off. Only while idle.", annotations=_WRITE),
    _tool("extrude", "Extrude or retract", "Push filament through the hot nozzle, or pull it back with a negative length. "
          "Only while idle, with the nozzle hot." + _CONFIRM_NOTE,
          {"length_mm": {"type": "number", "minimum": -100, "maximum": 100}}, ["length_mm"], _CONFIRMED),
    _tool("run_gcode", "Run G-code", "Run G-code commands on the printer, as a short file it prints. Only while idle." + _CONFIRM_NOTE,
          {"gcode": {"type": "string", "description": "One command per line."}}, ["gcode"], _CONFIRMED),

    # Multi-color box (ACE).
    _tool("set_filament_slot", "Set filament slot",
          "Set the material, color or brand the printer records for an ACE slot or the external spool, as when editing the "
          "slot on the printer. Give the slot and at least one of color, material and brand; the others keep their value. "
          "Nothing is loaded or moved.",
          {"slot": {"type": ["integer", "string"], "description": "Slot number as get_filaments lists it, or \"external\"."},
           "color": {"type": "string", "description": "Color as #RRGGBB."},
           "material": {"type": "string", "description": "Filament type, such as PLA, PETG, ABS or TPU."},
           "brand": {"type": "string", "description": "Filament brand."}},
          ["slot"], _WRITE),
    _tool("load_filament", "Load filament", "Feed the filament of an ACE slot into the toolhead. Only while idle." + _CONFIRM_NOTE,
          {"slot": _SLOT}, ["slot"], _CONFIRMED),
    _tool("unload_filament", "Unload filament", "Pull the filament of an ACE slot back into the ACE. Only while idle." + _CONFIRM_NOTE,
          {"slot": _SLOT}, ["slot"], _CONFIRMED),
    _tool("set_ace_dryer", "ACE dryer", "Start or stop drying the filament in the ACE; get_ace_status shows the dryer.",
          {"on": {"type": "boolean"},
           "temperature": {"type": "integer", "minimum": 30, "maximum": 65, "description": "°C, default 50."},
           "hours": {"type": "integer", "minimum": 1, "maximum": 24, "description": "Default 4."}},
          ["on"], _WRITE),
    _tool("set_ace_backup", "ACE backup spool", "Turn the ACE's automatic switch to a matching spool when one runs out on or off.",
          {"enabled": {"type": "boolean"}}, ["enabled"], _WRITE),

    # Print files.
    _tool("list_printer_files", "Printer files", "Print files stored on the printer."),
    _tool("print_printer_file", "Print a printer file", "Start printing a file stored on the printer. Only while idle." + _CONFIRM_NOTE,
          {"filename": {"type": "string", "description": "As list_printer_files names it."}}, ["filename"], _CONFIRMED),
    _tool("get_print_filaments", "Filaments of a print",
          "The filaments a plate (or a .gcode/.gcode.3mf file on this computer) prints with, their material and color, "
          "the loaded ACE slots, and a suggested slot for each filament: same material, nearest color. The user picks "
          "the slots visually in the send dialog that print_plate and print_file open.",
          {"plate": _PLATE, "path": {"type": "string", "description": "A print file instead of a plate."}}),
    _tool("print_file", "Print a file from this computer",
          "Open OrcaMartelo's send dialog for a .gcode or .gcode.3mf file from this computer: the user maps its "
          "filaments to ACE slots there, picks the options and starts it, or cancels. Only while idle.",
          {"path": {"type": "string", "description": "Full path of the file."}, "mapping": _MAPPING}, ["path"], _CONFIRMED),

    # The slicer.
    _tool("slicer_get_state", "Slicer state",
          "What OrcaMartelo has open: the selected printer, process and filament presets, the plates with their objects, "
          "and for a sliced plate its estimated time, filament and cost."),
    _tool("slicer_list_presets", "List presets", "Names of the presets of a kind that suit the selected printer.",
          {"type": _PRESET_TYPE}, ["type"]),
    _tool("slicer_select_preset", "Select preset", "Select a printer, process or filament preset by name.",
          {"type": _PRESET_TYPE, "name": {"type": "string"},
           "filament": {"type": "integer", "minimum": 1, "description": "For a filament preset: which filament, 1 for the first."}},
          ["type", "name"], _WRITE),
    _tool("slicer_get_settings", "Read settings",
          "Values of settings of the selected presets by key, such as layer_height, sparse_infill_density, wall_loops, "
          "enable_support, nozzle_temperature.",
          {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50}}, ["keys"]),
    _tool("slicer_set_settings", "Change settings",
          "Change settings of the selected process, filament or printer preset by key, as when editing them in the settings "
          "tabs; the preset shows as modified and is not saved. Values are given as OrcaMartelo writes them, such as \"0.2\" "
          "or \"15%\".",
          {"settings": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]},
                        "description": "Setting key to new value."}}, ["settings"], _WRITE),
    _tool("slicer_load_models", "Load models", "Add model files (STL, 3MF, STEP, OBJ) from this computer to the current plate.",
          {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20, "description": "Full paths."}},
          ["paths"], _WRITE),
    _tool("slicer_arrange", "Arrange", "Arrange the objects on the plates.", annotations=_WRITE),
    _tool("slicer_clear_plate", "Delete all objects", "Delete every object of the project." + _CONFIRM_NOTE, annotations=_CONFIRMED),
    _tool("slicer_slice", "Slice", "Slice a plate and return its estimated time, filament weight, cost and filament changes.",
          {"plate": _PLATE}, annotations=_WRITE),
    _tool("print_plate", "Print plate",
          "Slice a plate if needed and open OrcaMartelo's send dialog for it, as its Print button does: the user maps "
          "the filaments to ACE slots there, picks the options and starts it, or cancels. Only while idle.",
          {"plate": _PLATE, "mapping": _MAPPING}, annotations=_CONFIRMED),
]

# Each tool runs the backend method of the same name, minus a get_ prefix for the readers that had one.
_TOOL_METHODS = {t["name"]: t["name"] for t in TOOLS}
_TOOL_METHODS.update({
    "get_print_status": "print_status",
    "get_temperatures": "temperatures",
    "get_camera_snapshot": "camera_snapshot",
    "get_print_cost": "print_cost",
    "get_filaments": "filaments",
    "get_ace_status": "ace_status",
    "get_notifications": "notifications",
    "get_printer_info": "printer_info",
})

OFFLINE_MESSAGE = ("The OrcaMartelo printer bridge is not running. Open OrcaMartelo with an Anycubic "
                   "printer selected in the Device tab, then try again.")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class ImageResult:
    """A tool result that is a picture, with a caption for the model."""

    def __init__(self, data_base64, mime_type, caption):
        self.data_base64 = data_base64
        self.mime_type = mime_type
        self.caption = caption


_JSON_TYPES = {"integer": int, "number": (int, float), "boolean": bool, "string": str, "array": list, "object": dict}


def _check_arguments(tool, args):
    """The error in args against the tool's input schema, or None."""
    schema = tool["inputSchema"]
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"Missing argument: {key}"
    for key, value in args.items():
        if key not in properties:
            return f"Unknown argument: {key}"
        types = properties[key]["type"]
        types = types if isinstance(types, list) else [types]
        # bool is an int in Python, but not a number in JSON.
        if not any(isinstance(value, _JSON_TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool)) for t in types):
            return f"{key} must be {' or '.join(types)}"
        if "enum" in properties[key] and value not in properties[key]["enum"]:
            return f"{key} must be one of {', '.join(map(str, properties[key]['enum']))}"
    return None


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_result(data, is_error=False):
    if isinstance(data, ImageResult):
        return {"content": [{"type": "image", "data": data.data_base64, "mimeType": data.mime_type},
                            {"type": "text", "text": data.caption}], "isError": False}
    if is_error:
        return {"content": [{"type": "text", "text": str(data)}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}],
        "structuredContent": data,
        "isError": False,
    }


def _call_tool(params, backend):
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return None, f"Unknown tool: {name}"
    if not isinstance(args, dict):
        return None, "Tool arguments must be an object"
    problem = _check_arguments(tool, args)
    if problem:
        return None, problem
    if backend is None:
        return _tool_result(OFFLINE_MESSAGE, is_error=True), None
    if name == "get_notifications":
        args = {"limit": max(1, min(100, args.get("limit", 20))), "unread_only": bool(args.get("unread_only", False))}
    try:
        data = getattr(backend, _TOOL_METHODS[name])(**args)
    except ValueError as e:  # The printer refused or the request cannot be done: tell the model why.
        return _tool_result(str(e), is_error=True), None
    except Exception as e:
        return _tool_result(f"Could not reach the printer: {e}", is_error=True), None
    return _tool_result(data), None


def _handle_one(msg, backend):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid request")
    method = msg["method"]
    params = msg.get("params") or {}
    if "id" not in msg:
        return None  # A notification (initialized, cancelled, ...) needs no answer.
    msg_id = msg["id"]
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        result, error = _call_tool(params, backend)
        if error:
            return _error(msg_id, INVALID_PARAMS, error)
        return _result(msg_id, result)
    return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def handle_message(message, backend):
    """Answer one JSON-RPC message, or a batch of them.

    message is the decoded JSON body. Returns the response object (a list for a
    batch), or None when nothing needs an answer.
    """
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "Empty batch")
        responses = [r for r in (_handle_one(m, backend) for m in message) if r is not None]
        return responses or None
    return _handle_one(message, backend)


def handle_text(text, backend):
    """Like handle_message, for a raw JSON text. Returns the response text or None."""
    try:
        message = json.loads(text)
    except (ValueError, TypeError):
        return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
    response = handle_message(message, backend)
    return None if response is None else json.dumps(response, ensure_ascii=False)
