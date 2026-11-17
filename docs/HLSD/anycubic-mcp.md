# Anycubic printer MCP server

AI assistants that speak the Model Context Protocol (MCP) can watch and control two things:

- the Anycubic LAN printer that OrcaMartelo is connected to;
- OrcaMartelo itself: presets, settings, models, slicing and printing a plate.

## Where it runs

The server lives in the Anycubic LAN bridge (`resources/scripts/anycubic_lan_daemon.py`). The slicer starts the bridge with its bundled Python when an Anycubic printer is selected, so the server is available whenever OrcaMartelo is open with that printer. The bridge already holds the printer's live state from MQTT and sends its commands. The printer tools are thin views over that state and over `perform_control()`, the same actions the Device tab sends to `/control`.

- `orcamartelo_mcp.py` holds the protocol: the tool list, argument checks against each tool's schema, and the JSON-RPC answers. It has no printer code. Each tool calls the backend method of the same name, which keeps the protocol testable and lets the stdio transport use it without a printer.
- The bridge serves it at `POST http://127.0.0.1:18988/mcp`. This is Streamable HTTP with JSON answers only, since the server never sends messages of its own. The bridge's usual check refuses requests carrying another site's `Origin`, which protects against DNS rebinding.
- `orcamartelo_mcp_stdio.py` is the stdio transport for clients that launch a command, such as Claude Desktop. It forwards each line to `/mcp`. While the bridge is down it answers by itself with no backend, so the client keeps the server and each tool call explains how to bring the bridge up.

The MCP button in the Device tab shows the client configuration that `GET /mcp/info` returns, with the paths of the bundled Python and the stdio script on this install.

## Requests the slicer answers

Some work only the slicer process can do. The bridge hands these requests to the GUI over one channel:

1. `AnycubicMcpBridge` in the GUI holds a long poll on `GET /slicer/wait`, which returns `{id, kind, args}`.
2. The GUI re-arms the poll first, then runs the request on the GUI thread, so a dialog or a running slice does not hold up the next request.
3. It posts `{ok, result | error}` to `POST /slicer/result?id=`.
4. `slicer_call()` in the bridge waits for that answer, with a timeout per kind. A missing or failed answer becomes a tool error the model reads.

The kinds are:

- **`camera_snapshot`:** a JPEG frame of the printer's camera. `get_camera_snapshot` returns it as image content and also saves it under `camera_snapshots/` in the data folder (the newest 30), giving the path, so a client can hand the file to a model that reads images. The slicer passes its data folder to the bridge as `ORCAMARTELO_DATA_DIR`.
  - An MJPEG camera (a custom URL) is read by the bridge directly, without the slicer.
  - The printer's own camera streams H.264 in FLV, which the bundled Python cannot decode. The slicer takes this picture with the FFmpeg it already links for the camera view. It decodes a frame of the bridge's `/camera` proxy on a worker thread and scales it to at most 1280 px wide.
- **`confirm`:** a yes/no dialog in OrcaMartelo.
- **Slicer tools:** `get_state`, `list_presets`, `select_preset`, `get_settings`, `set_settings`, `load_models`, `arrange`, `clear_plate`, `plate_filaments`, `slice` and `send_plate`.
  - `select_preset` and `set_settings` act as the sidebar and the settings tabs do. Changed settings leave the preset modified and unsaved.
  - `slice` and `send_plate` post the plate's slice event when needed, then poll until the result is ready for printing. `send_plate` then sends the plate through `Plater::send_gcode_legacy()` without its dialog. That is the same upload the send dialog starts, with the same Anycubic header processing and the dialog's options as last chosen (`AnycubicSendOptions::load()`), so a plate sent from MCP prints like one sent by hand.

## Confirmation

An assistant must not spoil a print or move the machine on its own, so tools that do any of the following are confirmed:

- heat (`set_temperature`);
- move (`home_axes`, `move_axis`, `extrude`, `run_gcode`);
- feed filament (`load_filament`, `unload_filament`);
- start or stop a print (`stop_print`, `print_printer_file`; `print_file` and `print_plate` open the send dialog instead, where the user starts the print);
- delete objects (`slicer_clear_plate`).

The bridge asks first (`confirm_action()`). The slicer shows what will happen, in the user's language, and the action runs only on Yes. An unanswered dialog (two minutes) or an action the slicer does not recognise counts as No.

Moving, homing, extruding, G-code and filament loading are refused outright while a print runs or is paused. Pausing, resuming, fans, lights, speed mode, the ACE dryer and backup setting, and slot records are not confirmed. These tools carry `destructiveHint: false`; the confirmed ones carry `destructiveHint: true`.

## Cost

The printer does not report cost. The bridge records the slicer's estimate for each print it sends, keyed by the file name without its extensions, and keeps it across restarts in a small JSON file in the temp folder.

- An upload from the Plater passes `PrintStatistics` (cost, weight and length) through the print host's extended info.
- A file sent from the Device tab or by the MCP tools is read from the G-code's own `; total filament cost` and `; filament used [g]` lines.

The cost spent so far is the estimate scaled by progress. Prints started from the printer or other software have no cost.

## Filament mapping

A multi-color print needs to know which ACE slot prints each filament. Choosing that by text proved impractical, so the user picks it visually in the Anycubic send dialog, with its spool icons and per-filament slot lists:

- `print_plate` slices the plate when needed, then calls `Plater::send_gcode_legacy()` with an `McpSend`. That opens the same send dialog as the Print button and raises OrcaMartelo. The upload, the Anycubic header processing and the options are those of a print sent by hand. Cancelling the dialog is reported to the assistant.
- `print_file` reads the file's filaments from its config lines. The slicer opens the dialog for them (`choose_mapping`) and returns the dialog's extended info. The bridge then uploads the file with that `ams_mapping` and those options.

The dialog matches materials and nearest colors by itself. An assistant may still propose a `mapping`, which is checked against the dialog's rules (a loaded slot of the filament's material; the external spool only for a single filament) and preselected with `preselect_slots()`. The user can change it. `get_print_filaments` lists the filaments, the loaded slots and a suggestion, for the assistant to talk about.
