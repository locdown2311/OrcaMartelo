# Anycubic printer camera

The Device tab, the MCP camera snapshot and anything else that shows the printer's camera read it through the LAN bridge (`resources/scripts/anycubic_lan_daemon.py`) at `http://127.0.0.1:18988/camera`.

## The printer's own camera

The printer serves its camera as H.264 in FLV at `http://<printer>:18088/flv`, but only after an MQTT `startCapture`. Two things about it shape the design:

- It can take many seconds to start.
- It serves few viewers at a time. A second connection, or a reconnect while the previous connection is still open, is refused.

`FlvRelay` therefore keeps one connection to the printer and shares it with every viewer.

- **Starting.** The first viewer starts the stream. The relay sends `startCapture` and retries the connection for up to `FLV_START_TIMEOUT` before the viewer gets an error. It repeats `startCapture` every `FLV_KEEPALIVE` seconds while streaming, since the printer otherwise stops the camera.
- **New viewers.** The relay parses the FLV tags. A new viewer receives the stream header and the metadata tag first, then the cached AVC (and AAC) sequence headers and the frames from the last keyframe on (see Rare keyframes). It never starts on a frame that depends on one it did not see.
- **Drops and stalls.** When the printer closes the stream, or sends nothing for `FLV_READ_TIMEOUT`, the relay reconnects, and viewers wait for the first keyframe of the new connection. The relay rewrites timestamps so they keep rising by one frame interval across the gap, and players see one continuous stream rather than a restart they would stall on.
- **Closing.** After the last viewer leaves, the relay keeps the connection for `FLV_IDLE_CLOSE` seconds, so a reconnecting player or a snapshot finds it warm. Then it closes and sends `stopCapture`. The Device tab's own stop request does nothing while the relay runs.
- **A busy camera service.** The printer answers `startCapture` on its video report topic. A failure, such as 11402 when its camera service times out during a print, is camera state rather than a printer error. The relay shows it in its status, raises no printer-error notification, and doubles the keepalive interval up to `FLV_KEEPALIVE_MAX` until the service answers normally again. Codes 11400–11499 on other reports are handled the same way.
- **Rare keyframes.** The printer's camera sends keyframes seldom, so the relay keeps the frames since the last keyframe (up to `FLV_GOP_MAX_BYTES`). A new viewer starts on them at once rather than waiting for the next keyframe, which could take longer than the player's stall limit.
- **Slow viewers.** A viewer more than `FLV_CLIENT_MAX_BYTES` behind is dropped and reconnects. It does not hold the others back.

Connections use a short connect timeout and a separate read timeout. One timeout for both used to end healthy streams at the first pause in the data.

`GET /status/camera` reports whether the stream runs and is connected, its last error, the seconds since the last frame and the keyframe interval. The player shows this reason while it reconnects.

The camera settings also offer **Direct** (`/camera?direct=1`). In this mode each view gets its own connection to the printer, as before the relay existed. It is a fallback for a camera the shared stream does not suit, and then only one view can watch at a time.

When the printer's camera does not start, the bridge tries the MJPEG ports a camera add-on may serve (8080, 8000) for that viewer.

## Custom cameras

A custom URL from the Device tab's camera settings (typically MJPEG) is relayed to each viewer separately, with the same timeouts. MJPEG servers handle many clients.

## The Device tab player

The player in `resources/web/anycubic/workbench.html` uses flv.js for FLV and an `<img>` for MJPEG. Once the user starts it, it reconnects by itself until the user stops it:

- after a player error or the end of the stream;
- when no new frame has been decoded for `CAM_STALL_MS`.

Retries back off from 1 to 10 seconds. The player shows Live only once frames are decoded. It skips ahead when it falls more than `CAM_MAX_LATENCY_S` behind the newest buffered frame.

## The bridge process

The camera, the notifications and MCP all depend on the bridge process. The slicer starts it when an Anycubic printer is selected, and a watchdog in `AnycubicLink.cpp` checks every five seconds that it still runs. If the process has ended and no other OrcaMartelo window serves the same printer, the watchdog starts it again.

The bundled Python is started with `-I`, so `PYTHONPATH`, `PYTHONHOME` and the user packages of other Python installations on the machine do not reach it. The MCP stdio command uses `-I` for the same reason. The bridge's output goes to `log/anycubic_bridge.log` in the data folder, and the previous run's log is kept as `anycubic_bridge.prev.log`.
