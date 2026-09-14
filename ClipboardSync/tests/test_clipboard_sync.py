import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))
from clipboard_sync import ClipboardState, choose_auto_role, content_hash  # noqa: E402


class FakeBackend:
    def __init__(self, text=""):
        self.text = text
        self.writes = []

    def read(self):
        return self.text

    def write(self, text):
        self.writes.append(text)
        self.text = text


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

    def test_same_remote_content_is_ignored(self):
        backend = FakeBackend("igual")
        state = ClipboardState.from_backend(backend)

        self.assertFalse(state.apply_remote("igual"))
        self.assertEqual(backend.writes, [])
        self.assertEqual(state.observed_hash, content_hash("igual"))

    def test_auto_election_is_symmetric(self):
        self.assertEqual(choose_auto_role("0000000000000001", "0000000000000002"), "server")
        self.assertEqual(choose_auto_role("0000000000000002", "0000000000000001"), "client")
        self.assertIsNone(choose_auto_role("igual", "igual"))


if __name__ == "__main__":
    unittest.main()
