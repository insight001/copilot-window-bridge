"""Exercise discovery and dispatch against local HTTP mocks, never Copilot."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import connections
import send


class MockBridge:
    def __init__(self, workspace="project", instance_id=None):
        self.instance_id = instance_id or str(uuid.uuid4())
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self.metadata = {
            "instanceId": self.instance_id, "workspace": workspace,
            "folders": ["file:///work/" + workspace], "vscodeVersion": "1.118.0",
            "bridgeVersion": "0.2.0", "appName": "Visual Studio Code",
            "pid": os.getpid(), "startedAt": "2026-10-22T00:00:00.000Z",
        }
        self.gets = 0
        self.posts = []
        self.health_ids = []
        self.redirect = None
        self.post_status = 202
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data, location=None):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if self.headers.get("Authorization") == "Bearer " + bridge.token:
                    return True
                self.reply(401, {"error": self.headers.get("Authorization", "missing")})
                return False

            def do_GET(self):
                bridge.gets += 1
                if not self.authorized():
                    return
                if bridge.redirect:
                    self.reply(302, {"error": "moved"}, bridge.redirect)
                    return
                health = {"status": "ready", **bridge.metadata}
                if bridge.health_ids:
                    health["instanceId"] = bridge.health_ids.pop(0)
                self.reply(200, health)

            def do_POST(self):
                if not self.authorized():
                    return
                length = int(self.headers.get("Content-Length", "0"))
                bridge.posts.append(json.loads(self.rfile.read(length).decode("utf-8")))
                if bridge.post_status != 202:
                    self.reply(bridge.post_status, {"error": "echo " + bridge.token, "token": bridge.token})
                else:
                    self.reply(202, {"status": "dispatched", "instanceId": bridge.instance_id})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def descriptor(self):
        return {"url": self.url, "token": self.token, **self.metadata}


class ClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = patch.dict(os.environ, {"COPILOT_WINDOW_BRIDGE_DIR": str(self.directory)})
        environment.start()
        self.addCleanup(environment.stop)

    def bridge(self, workspace="project", instance_id=None):
        bridge = MockBridge(workspace, instance_id)
        self.addCleanup(bridge.close)
        return bridge

    def descriptor(self, bridge, **overrides):
        data = {**bridge.descriptor(), **overrides}
        path = self.directory / ("connection-" + data["instanceId"] + ".json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def invoke(self, arguments, stdin=""):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), patch("sys.stdin", io.StringIO(stdin)):
            try:
                result = send.main(arguments)
            except SystemExit as error:
                result = error.code
        return result, output.getvalue(), errors.getvalue()

    def test_loopback_url_validation(self):
        self.assertEqual(connections.validate_url("http://127.0.0.1:1234/"), "http://127.0.0.1:1234")
        self.assertEqual(connections.validate_url("http://[::1]:1234"), "http://[::1]:1234")
        for url in ("https://127.0.0.1:1", "http://localhost:1", "http://example.com:1",
                    "http://127.1:1", "http://127.0.0.1", "http://127.0.0.1:0",
                    "http://127.0.0.1:65536", "http://u@127.0.0.1:1", "http://127.0.0.1:1/x",
                    "http://127.0.0.1:1?", "http://127.0.0.1:1#", " http://127.0.0.1:1"):
            with self.subTest(url=url), self.assertRaises(connections.ClientError):
                connections.validate_url(url)

    def test_list_reports_multiple_windows_files_and_metadata_without_secrets(self):
        one, two = self.bridge("alpha"), self.bridge("beta")
        paths = [self.descriptor(one), self.descriptor(two)]
        code, output, errors = self.invoke(["list", "--json"])
        self.assertEqual((code, errors), (0, ""))
        rows = json.loads(output)
        self.assertEqual({row["instanceId"] for row in rows}, {one.instance_id, two.instance_id})
        self.assertEqual({row["connectionFile"] for row in rows}, {str(path) for path in paths})
        self.assertTrue(all(row["status"] == "live" and row["folders"] for row in rows))
        self.assertNotIn(one.token, output)
        self.assertNotIn(two.token, output)
        self.assertTrue(all("token" not in row for row in rows))
        self.assertEqual(one.posts + two.posts, [])

    def test_ambiguous_workspace_and_prefix_never_dispatch(self):
        one = self.bridge("same", "a1111111-1111-4111-8111-111111111111")
        two = self.bridge("same", "a2222222-2222-4222-8222-222222222222")
        self.descriptor(one)
        self.descriptor(two)
        for selector in (["--workspace", "same"], ["--window", "a"]):
            code, _, errors = self.invoke(["send", *selector, "hello"])
            self.assertEqual(code, 2)
            self.assertIn("ambiguous", errors)
        self.assertEqual(one.posts + two.posts, [])

    def test_unique_prefix_routes_only_selected_window_and_flags(self):
        one = self.bridge("alpha", "a1111111-1111-4111-8111-111111111111")
        two = self.bridge("beta", "b2222222-2222-4222-8222-222222222222")
        self.descriptor(one)
        self.descriptor(two)
        code, output, errors = self.invoke([
            "send", "--window", "B222", "--new", "--behavior", "steer", "--composer", "hello \u2603",
        ])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["instanceId"], two.instance_id)
        self.assertEqual(one.posts, [])
        self.assertEqual(two.posts, [{"message": "hello \u2603", "target": "new", "behavior": "steer", "inputMode": "composer"}])
        self.assertEqual(two.gets, 2)  # Discovery plus a fresh check before POST.

    def test_identity_mismatch_prevents_explicit_and_discovered_dispatch(self):
        bridge = self.bridge()
        wrong_id = str(uuid.uuid4())
        path = self.descriptor(bridge, instanceId=wrong_id)
        code, _, errors = self.invoke(["send", "--connection", str(path), "hello"])
        self.assertEqual(code, 2)
        self.assertIn("Instance ID mismatch", errors)
        path.unlink()
        self.descriptor(bridge)
        bridge.health_ids = [bridge.instance_id, wrong_id]
        code, _, errors = self.invoke(["send", "--window", bridge.instance_id, "hello"])
        self.assertEqual(code, 2)
        self.assertIn("Instance ID mismatch", errors)
        self.assertEqual(bridge.posts, [])

    def test_stale_workspace_duplicate_does_not_block_unique_live_window(self):
        bridge = self.bridge("same")
        self.descriptor(bridge)
        self.descriptor(bridge, instanceId=str(uuid.uuid4()))
        code, _, errors = self.invoke(["send", "--workspace", "same", "hello"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(len(bridge.posts), 1)

    def test_all_reports_stale_and_invalid_records_without_echoing_secrets(self):
        bridge = self.bridge()
        self.descriptor(bridge)
        stale_secret = "c" * 64
        self.descriptor(bridge, instanceId=str(uuid.uuid4()), token=stale_secret)
        invalid = self.directory / "connection-invalid.json"
        invalid.write_text('{"token":"PRIVATE_PARSE_ERROR"', encoding="utf-8")
        code, output, _ = self.invoke(["list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 1)
        code, output, errors = self.invoke(["list", "--json", "--all"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual({row["status"] for row in json.loads(output)}, {"live", "stale", "invalid"})
        for secret in (bridge.token, stale_secret, "PRIVATE_PARSE_ERROR"):
            self.assertNotIn(secret, output + errors)

    def test_regular_bounded_descriptors_and_nonrecursive_discovery(self):
        bridge = self.bridge()
        path = self.descriptor(bridge)
        link = self.directory / "connection-link.json"
        link.symlink_to(path)
        oversized = self.directory / "connection-oversized.json"
        oversized.write_bytes(b" " * (connections.DESCRIPTOR_LIMIT + 1))
        not_file = self.directory / "connection-directory.json"
        not_file.mkdir()
        for bad in (link, oversized, not_file):
            with self.subTest(path=bad), self.assertRaises(connections.ClientError):
                connections.read_connection(bad)
        nested = self.directory / "nested"
        nested.mkdir()
        (nested / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        (self.directory / "unrelated.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        records = connections.discover()
        self.assertEqual(len(records), 4)
        self.assertEqual(bridge.gets, 1)
        self.assertEqual([record.status for record in records].count("invalid"), 3)

    def test_human_fields_escape_controls_and_redact_known_token(self):
        bridge = self.bridge()
        name = "project\n\x1b[31m\u202e" + bridge.token
        bridge.metadata["workspace"] = name
        self.descriptor(bridge)
        code, output, errors = self.invoke(["list"])
        self.assertEqual((code, errors), (0, ""))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)
        self.assertIn("project\\n\\u001b[31m\\u202e[redacted]", output)
        self.assertNotIn(bridge.token, output)
        # Exact selection still uses the real name, not its redacted display.
        code, _, errors = self.invoke(["health", "--workspace", name])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(bridge.posts, [])

    def test_legacy_health_and_file_or_stdin_preserve_message_text(self):
        bridge = self.bridge()
        path = self.descriptor(bridge)
        code, _, errors = self.invoke(["--connection", str(path), "--health"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(bridge.posts, [])
        message_file = self.directory / "message.txt"
        message_file.write_text(" first\nsecond \u2603\n", encoding="utf-8")
        code, _, errors = self.invoke(["--connection", str(path), "--file", str(message_file)])
        self.assertEqual((code, errors), (0, ""))
        code, _, errors = self.invoke(["send", "--connection", str(path), "--stdin"], stdin="from stdin\n")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual([item["message"] for item in bridge.posts], [" first\nsecond \u2603\n", "from stdin\n"])
        self.assertTrue(all(item["inputMode"] == "preserve" for item in bridge.posts))

    def test_missing_selection_or_conflicting_message_sources_do_not_probe(self):
        bridge = self.bridge()
        path = self.descriptor(bridge)
        for args in (["send", "hello"], ["send", "--connection", str(path), "hello", "--stdin"]):
            code, _, _ = self.invoke(args)
            self.assertEqual(code, 2)
        self.assertEqual(bridge.gets, 0)
        self.assertEqual(bridge.posts, [])

    def test_post_failure_is_redacted_and_never_retried(self):
        bridge = self.bridge()
        bridge.post_status = 500
        path = self.descriptor(bridge)
        code, output, errors = self.invoke(["send", "--connection", str(path), "hello"])
        self.assertEqual(code, 1)
        self.assertEqual(len(bridge.posts), 1)
        self.assertNotIn(bridge.token, output + errors)
        self.assertIn("[redacted]", output)
        self.assertIn("No retry", errors)

    def test_redirects_are_not_followed_and_proxy_environment_is_ignored(self):
        bridge, other = self.bridge(), self.bridge()
        path = self.descriptor(bridge)
        bridge.redirect = other.url + "/health"
        code, _, errors = self.invoke(["health", "--connection", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("Redirect refused", errors)
        self.assertEqual(other.gets, 0)
        bridge.redirect = None
        with patch.dict(os.environ, {"http_proxy": other.url, "HTTP_PROXY": other.url, "no_proxy": "", "NO_PROXY": ""}):
            code, _, errors = self.invoke(["health", "--connection", str(path)])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(other.gets, 0)

    def test_registry_override_is_explicit_and_absolute(self):
        for value in ("", "relative/path"):
            with patch.dict(os.environ, {"COPILOT_WINDOW_BRIDGE_DIR": value}):
                with self.assertRaises(connections.ClientError):
                    connections.registry_directory()
                self.assertEqual(connections.registry_directory(self.directory), self.directory)
        alternate = self.directory / "alternate"
        alternate.mkdir()
        bridge = self.bridge()
        path = alternate / ("connection-" + bridge.instance_id + ".json")
        path.write_text(json.dumps(bridge.descriptor()), encoding="utf-8")
        code, output, errors = self.invoke(["list", "--directory", str(alternate), "--json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)[0]["connectionFile"], str(path))


if __name__ == "__main__":
    unittest.main()
