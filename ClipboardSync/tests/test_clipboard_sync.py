import sys
import threading
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))
from clipboard_sync import (
    ClipboardState,
    Endpoint,
    content_hash,
    parse_discovery_response,
    server_request,
)  # noqa: E402
from pc.clipboard_sync_server import ClipboardSyncServer  # noqa: E402


class FakeBackend:
    def __init__(self, text=""):
        self.text = text
        self.writes = []

    def read(self):
        return self.text

    def write(self, text):
        self.writes.append(text)
        self.text = text


class FakeServerState:
    def __init__(self):
        self.text = "texto do servidor"
        self.source = "pc"

    def read_current(self):
        return self.text, 1.0, self.source, False

    def write_from_client(self, text, source):
        self.text = text
        self.source = source
        return 2.0, source


class ClipboardStateTests(unittest.TestCase):
    def test_local_change_is_reported_once(self):
        backend = FakeBackend("antes")
        state = ClipboardState.from_backend(backend)
        backend.text = "depois"

        self.assertEqual(state.poll_local_change(), "depois")
        self.assertIsNone(state.poll_local_change())

    def test_remote_change_is_not_echoed_back(self):
        backend = FakeBackend("antes")
        state = ClipboardState.from_backend(backend)

        self.assertTrue(state.apply_remote("remoto"))
        self.assertEqual(backend.writes, ["remoto"])
        self.assertIsNone(state.poll_local_change())

    def test_local_change_back_to_previous_remote_text_is_not_suppressed(self):
        backend = FakeBackend("antes")
        state = ClipboardState.from_backend(backend)

        state.apply_remote("remoto")
        self.assertIsNone(state.poll_local_change())
        backend.text = "antes"

        self.assertEqual(state.poll_local_change(), "antes")

    def test_same_remote_content_is_ignored(self):
        backend = FakeBackend("igual")
        state = ClipboardState.from_backend(backend)

        self.assertFalse(state.apply_remote("igual"))
        self.assertEqual(backend.writes, [])
        self.assertEqual(state.observed_hash, content_hash("igual"))

    def test_discovery_response_returns_server_endpoint(self):
        endpoint = parse_discovery_response("CLIPSYNC/1|8765|token-secreto|PC principal", "192.168.1.20")

        self.assertEqual(endpoint.host, "192.168.1.20")
        self.assertEqual(endpoint.port, 8765)
        self.assertEqual(endpoint.token, "token-secreto")
        self.assertEqual(endpoint.name, "PC principal")

    def test_invalid_discovery_response_is_ignored(self):
        self.assertIsNone(parse_discovery_response("CLIPSYNC/2|8765|token|PC", "192.168.1.20"))
        self.assertIsNone(parse_discovery_response("CLIPSYNC/1|70000|token|PC", "192.168.1.20"))

    def test_desktop_client_uses_server_http_api(self):
        state = FakeServerState()
        server = ClipboardSyncServer(("127.0.0.1", 0), "teste-token", state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = Endpoint("127.0.0.1", server.server_port, "teste-token")
        try:
            self.assertEqual(server_request(endpoint, "GET")["text"], "texto do servidor")
            server_request(endpoint, "POST", "texto do desktop")
            self.assertEqual(server_request(endpoint, "GET")["text"], "texto do desktop")
            self.assertEqual(state.source, "desktop")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
