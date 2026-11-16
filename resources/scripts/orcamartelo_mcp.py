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
    "Access to the 3D printer OrcaMartelo is connected to over the LAN. "
    "Use get_print_status for progress and time left, get_temperatures for the nozzle and bed, "
    "get_print_cost for the estimated cost of the current or last print, get_filaments for the "
    "spools in the multi-color box (ACE), get_camera_snapshot to look at the print, and "
    "get_notifications for finished, stopped and failed prints and printer errors. "
    "set_filament_slot changes the material, color or brand the printer records for a slot; "
    "it does not move filament. Costs use the currency of the filament profiles in OrcaMartelo."
)

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_SETS_SLOT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "get_print_status",
        "title": "Print status",
        "description": "State of the printer and the current print: state, file name, progress in percent, "
                       "current and total layer, elapsed and remaining minutes, estimated end time, "
                       "nozzle and bed temperatures with their targets, and fan and speed mode.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_temperatures",
        "title": "Temperatures",
        "description": "Current and target temperatures of the nozzle and the bed, in degrees Celsius.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_camera_snapshot",
        "title": "Camera snapshot",
        "description": "Take a picture with the printer's camera now and return it as a JPEG image, to check "
                       "the print (spaghetti, detached parts, first layer) or the bed.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_print_cost",
        "title": "Print cost",
        "description": "Estimated cost, filament weight and length of the current print, or of the last one "
                       "when the printer is idle, as sliced by OrcaMartelo, and the part spent so far in "
                       "proportion to the progress. Only prints sent from OrcaMartelo have a cost.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_filaments",
        "title": "Loaded filaments",
        "description": "Spools in the multi-color box (ACE) slots and the external spool: material, color, "
                       "brand, and which slot is loaded or feeding.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "set_filament_slot",
        "title": "Set filament slot",
        "description": "Set the material, color or brand the printer records for a multi-color box (ACE) slot "
                       "or the external spool, as when editing the slot on the printer. Give the slot and at "
                       "least one of color, material and brand; the others keep their value. Nothing is loaded "
                       "or moved.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slot": {"type": ["integer", "string"],
                         "description": "Slot number as get_filaments lists it (1 for the first ACE slot), or \"external\"."},
                "color": {"type": "string", "description": "Color as #RRGGBB."},
                "material": {"type": "string", "description": "Filament type, such as PLA, PETG, ABS or TPU."},
                "brand": {"type": "string", "description": "Filament brand."},
            },
            "required": ["slot"],
            "additionalProperties": False,
        },
        "annotations": _SETS_SLOT,
    },
    {
        "name": "get_notifications",
        "title": "Notifications",
        "description": "Recent print events (started, paused, resumed, finished, stopped, failed) and printer "
                       "errors, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Most events to return (default 20)."},
                "unread_only": {"type": "boolean", "description": "Only events not yet read in OrcaMartelo."},
            },
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_printer_info",
        "title": "Printer information",
        "description": "Printer model, firmware, address, connection state and camera stream URL.",
        "inputSchema": _NO_ARGS,
        "annotations": _READ_ONLY,
    },
]

_TOOL_METHODS = {
    "get_print_status": "print_status",
    "get_temperatures": "temperatures",
    "get_camera_snapshot": "camera_snapshot",
    "set_filament_slot": "set_filament_slot",
    "get_print_cost": "print_cost",
    "get_filaments": "filaments",
    "get_notifications": "notifications",
    "get_printer_info": "printer_info",
}

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


_JSON_TYPES = {"integer": int, "boolean": bool, "string": str}


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
        # bool is an int in Python, but not an integer in JSON.
        if not any(isinstance(value, _JSON_TYPES[t]) and not (t == "integer" and isinstance(value, bool)) for t in types):
            return f"{key} must be {' or '.join(types)}"
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
