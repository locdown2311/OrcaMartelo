# Anycubic printer MCP server

AI assistants that speak the Model Context Protocol (MCP) can read and adjust the Anycubic LAN printer that OrcaMartelo is connected to. They can read the print progress, temperatures, estimated cost, ACE filaments, notification log and a camera picture, and they can set the material, color and brand of an ACE slot.

## Where it runs

The server lives in the Anycubic LAN bridge (`resources/scripts/anycubic_lan_daemon.py`). The slicer starts the bridge with its bundled Python when an Anycubic printer is selected, so the server is available whenever OrcaMartelo is open with that printer. The bridge already holds the printer's live state from MQTT, so the MCP tools are thin views over that state.

- `orcamartelo_mcp.py` holds the protocol: the tool list, argument checks and the JSON-RPC answers. It has no printer code. It gets a backend object whose methods return the data, which keeps it testable and lets the stdio transport use it without a printer.
- The bridge serves it at `POST http://127.0.0.1:18988/mcp`. This is Streamable HTTP with JSON answers only, since the server never sends messages of its own. The bridge's usual check refuses requests carrying another site's `Origin`, which protects against DNS rebinding.
- `orcamartelo_mcp_stdio.py` is the stdio transport for clients that launch a command, such as Claude Desktop. It forwards each line to `/mcp`. While the bridge is down it answers by itself with no backend, so the client keeps the server and each tool call explains how to bring the bridge up.

The MCP button in the Device tab shows the client configuration that `GET /mcp/info` returns, with the paths of the bundled Python and the stdio script on this install.

## Cost

The printer does not report cost. The bridge records the slicer's estimate for each print it sends, keyed by the file name without its extensions, and keeps it across restarts in a small JSON file in the temp folder.

- An upload from the Plater passes `PrintStatistics` (cost, weight and length) through the print host's extended info.
- A file sent from the Device tab is read from the G-code's own `; total filament cost` and `; filament used [g]` lines.

The cost spent so far is the estimate scaled by progress. Prints started from the printer or other software have no cost.

## Camera picture

An MJPEG camera (a custom URL) is read by the bridge directly. The printer's own camera streams H.264 in FLV, which the bundled Python cannot decode, so the slicer takes that picture with the FFmpeg it already links for the camera view:

1. `AnycubicCameraSnapshots` in the GUI holds a long poll on `GET /camera/snapshot/wait`.
2. A `get_camera_snapshot` call queues a request and wakes the poll. The answer carries the bridge's own `/camera` proxy URL, which also starts the printer's camera.
3. The slicer decodes a frame on a worker thread, scales it to at most 1280 px wide and posts the JPEG to `POST /camera/snapshot?id=`.
4. The tool returns the JPEG as MCP image content. If no answer arrives within 15 seconds, the tool reports an error.

## Changing the printer

`set_filament_slot` is the only tool that changes anything. It sends the same material update as the Device tab's slot editor and leaves fields that were not given unchanged. When only the material or brand changes, a gradient or luminous finish is kept. It does not load or move filament. Printer controls such as pause or stop and temperature changes are not exposed.
