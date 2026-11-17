import http.client
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "resources" / "scripts"
os.environ["ORCACUBIC_PRINTER_IP"] = "192.0.2.31"
os.environ["ORCACUBIC_BRIDGE_TOKEN"] = "test-token"

spec = importlib.util.spec_from_file_location("anycubic_lan_daemon_camera", SCRIPTS / "anycubic_lan_daemon.py")
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg")


def flv_tag(tag_type, body, ts):
    return daemon._flv_tag(tag_type, body, ts)


def synthetic_flv(seconds=3, fps=10, gop=10):
    """An FLV the parser accepts: AVC sequence header, then keyframes every gop frames."""
    data = b"FLV\x01\x01" + (9).to_bytes(4, "big") + b"\x00\x00\x00\x00"
    data += flv_tag(18, b"\x02\x00\x0aonMetaData\x08\x00\x00\x00\x00\x00\x00\x09", 0)
    data += flv_tag(9, b"\x17\x00\x00\x00\x00\x01\x42\x00\x1e", 0)
    for i in range(seconds * fps):
        frame = (0x17 if i % gop == 0 else 0x27).to_bytes(1, "big") + b"\x01\x00\x00\x00" + bytes(40)
        data += flv_tag(9, frame, i * 1000 // fps)
    return data


def parse_flv(data):
    """(header ok, list of (type, timestamp, keyframe, sequence header)) of a viewer's bytes."""
    if data[:3] != b"FLV":
        return False, []
    pos = int.from_bytes(data[5:9], "big") + 4
    tags = []
    while pos + 11 <= len(data):
        tag_type = data[pos] & 0x1F
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        ts = int.from_bytes(data[pos + 4:pos + 7], "big") | (data[pos + 7] << 24)
        body = data[pos + 11:pos + 11 + size]
        if len(body) < size:
            break
        is_video = tag_type == 9 and len(body) >= 2
        tags.append((tag_type, ts, is_video and body[0] >> 4 == 1 and body[1] == 1, is_video and body[1] == 0))
        pos += 11 + size + 4
    return True, tags


class FakeCamera:
    """The printer's FLV endpoint: streams a file paced by its timestamps, serving one viewer at a time."""

    def __init__(self, flv, speed=4.0, cut_after_tags=None, refuse=False):
        self.flv = flv
        self.speed = speed
        self.cut_after_tags = cut_after_tags
        self.refuse = refuse
        self.connections = 0
        self.active = 0
        self.lock = threading.Lock()
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with camera.lock:
                    busy = camera.active > 0
                    if not busy and not camera.refuse:
                        camera.active += 1
                        camera.connections += 1
                if busy or camera.refuse:
                    self.send_response(503)
                    self.end_headers()
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/x-flv")
                    self.end_headers()
                    camera.stream(self.wfile)
                except OSError:
                    pass
                finally:
                    with camera.lock:
                        camera.active -= 1

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/flv"

    def stream(self, out):
        data = self.flv
        pos = int.from_bytes(data[5:9], "big") + 4
        out.write(data[:pos])
        start = time.time()
        sent = 0
        while pos + 11 <= len(data):
            size = int.from_bytes(data[pos + 1:pos + 4], "big")
            ts = int.from_bytes(data[pos + 4:pos + 7], "big")
            delay = ts / 1000 / self.speed - (time.time() - start)
            if delay > 0:
                time.sleep(delay)
            out.write(data[pos:pos + 15 + size])
            out.flush()
            pos += 15 + size
            sent += 1
            if self.cut_after_tags and sent >= self.cut_after_tags:
                return

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        for name, value in (("FLV_START_TIMEOUT", 3), ("FLV_IDLE_CLOSE", 0.5), ("FLV_READ_TIMEOUT", 2)):
            patch = mock.patch.object(daemon, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(daemon, "send_video_command", self.commands.append)
        patch.start()
        self.addCleanup(patch.stop)

    def camera(self, flv=None, **kwargs):
        cam = FakeCamera(flv or synthetic_flv(), **kwargs)
        self.addCleanup(cam.close)
        relay = daemon.FlvRelay(lambda: cam.url)
        self.addCleanup(self.wait_closed, relay)
        return cam, relay

    def wait_closed(self, relay):
        for _ in range(100):
            if relay.thread is None:
                return
            time.sleep(0.05)

    def watch(self, relay, seconds):
        client = relay.attach()
        data = b""
        end = time.time() + seconds
        try:
            while time.time() < end:
                chunk = relay.read(client, timeout=0.2)
                if chunk:
                    data += chunk
        finally:
            relay.detach(client)
        return data

    def assert_playable(self, data):
        ok, tags = parse_flv(data)
        self.assertTrue(ok)
        video = [t for t in tags if t[0] == 9]
        self.assertTrue(video[0][3], "the codec header comes first")
        frames = [t for t in video if not t[3]]
        self.assertTrue(frames[0][2], "the first frame is a keyframe")
        stamps = [t[1] for t in frames]
        self.assertEqual(stamps, sorted(stamps), "timestamps never go back")
        return frames

    def test_viewers_share_one_connection_and_each_starts_on_a_keyframe(self):
        cam, relay = self.camera()
        results = {}

        def viewer(name, delay):
            time.sleep(delay)
            results[name] = self.watch(relay, 0.6)

        threads = [threading.Thread(target=viewer, args=(n, d)) for n, d in (("first", 0), ("late", 0.25))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(cam.connections, 1)
        for name in ("first", "late"):
            with self.subTest(viewer=name):
                self.assertGreater(len(self.assert_playable(results[name])), 1)
        self.assertEqual(self.commands[0], "startCapture")

    def test_a_dropped_stream_reconnects_with_rising_timestamps(self):
        cam, relay = self.camera(cut_after_tags=12)
        frames = self.assert_playable(self.watch(relay, 2.5))
        self.assertGreaterEqual(cam.connections, 2)
        self.assertGreater(len(frames), 12)

    def test_a_viewer_reconnecting_at_once_gets_the_stream(self):
        cam, relay = self.camera()
        self.watch(relay, 0.3)
        # The old viewer has just gone and the printer serves one viewer: the shared stream keeps serving.
        self.assert_playable(self.watch(relay, 0.6))
        self.assertEqual(cam.connections, 1)

    def test_without_viewers_the_stream_closes_and_the_camera_stops(self):
        cam, relay = self.camera()
        self.watch(relay, 0.3)
        self.wait_closed(relay)
        self.assertIsNone(relay.thread)
        self.assertEqual(self.commands[-1], "stopCapture")

    def test_a_camera_that_never_starts_is_reported(self):
        cam, relay = self.camera(refuse=True)
        start = time.time()
        with self.assertRaises(ValueError):
            relay.attach(timeout=1)
        self.assertLess(time.time() - start, 3)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class RealVideoTests(RelayTests):
    """The relayed bytes of a real H.264 stream decode, also across a reconnect."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "camera.flv")
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-t", "8",
                        "-c:v", "libx264", "-profile:v", "baseline", "-g", "15", "-pix_fmt", "yuv420p", "-f", "flv", path],
                       check=True)
        cls.flv = Path(path).read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def decode(self, data):
        path = os.path.join(self.tmp.name, "relayed.flv")
        Path(path).write_bytes(data)
        run = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
        return run.returncode, run.stderr

    def test_the_relayed_stream_decodes(self):
        cam, relay = self.camera(self.flv)
        data = self.watch(relay, 1.0)
        self.assert_playable(data)
        code, errors = self.decode(data)
        self.assertEqual((code, errors), (0, ""))

    def test_a_stream_rejoined_after_a_drop_decodes(self):
        cam, relay = self.camera(self.flv, cut_after_tags=40)
        data = self.watch(relay, 3.0)
        self.assertGreaterEqual(cam.connections, 2)
        self.assert_playable(data)
        code, errors = self.decode(data)
        self.assertEqual((code, errors), (0, ""))

    # The synthetic-stream tests above also run here on the real video.
    def camera(self, flv=None, **kwargs):
        return super().camera(flv or self.flv, **kwargs)


class HttpCameraTests(unittest.TestCase):
    def test_the_camera_endpoint_serves_the_shared_stream(self):
        cam = FakeCamera(synthetic_flv())
        self.addCleanup(cam.close)
        relay = daemon.FlvRelay(lambda: cam.url)
        server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), daemon.BridgeServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(daemon, "camera_relay", relay), mock.patch.object(daemon, "send_video_command", lambda a: None), \
             mock.patch.dict(daemon.telemetry, {"custom_camera_url": ""}):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", "/camera")
            response = connection.getresponse()
            data = response.read1(65536)
            while len(data) < 200:
                data += response.read1(65536)
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "video/x-flv")
        self.assertEqual(data[:3], b"FLV")


if __name__ == "__main__":
    unittest.main()
