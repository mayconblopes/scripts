import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))
from clipboard_sync import (
    ClientClipboardState as ClipboardState,
    Endpoint,
    FileTransferStore,
    TransferError,
    UnifiedClipboardSyncServer,
    _api_json,
    _download_folder,
    _prepare_send_path,
    _safe_extract_zip,
    _stream_download,
    _stream_transfer,
    content_hash,
    parse_discovery_response,
    server_request,
)  # noqa: E402


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
        server = UnifiedClipboardSyncServer(("127.0.0.1", 0), "teste-token", state, FileTransferStore())
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

    def test_first_transfer_acceptance_wins(self):
        store = FileTransferStore()
        store.register_device("receiver_00000001", "PC destino")
        store.register_device("receiver_00000002", "Android")
        offer = store.create_offer({
            "device_id": "sender_00000001", "device_name": "PC origem", "name": "arquivo.txt",
            "kind": "file", "size": 4, "expanded_size": 4, "file_count": 1,
            "sha256": content_hash("data"),
        })

        store.accept(offer["id"], "receiver_00000001", "PC destino")
        with self.assertRaises(TransferError) as error:
            store.accept(offer["id"], "receiver_00000002", "Android")
        self.assertEqual(error.exception.status_code, 409)

    def test_sender_is_not_an_offer_recipient(self):
        store = FileTransferStore()
        offer = store.create_offer({
            "device_id": "sender_00000001", "device_name": "PC origem", "name": "arquivo.txt",
            "kind": "file", "size": 4, "expanded_size": 4, "file_count": 1,
            "sha256": content_hash("data"),
        })
        with self.assertRaises(TransferError) as error:
            store.accept(offer["id"], "sender_00000001", "PC origem")
        self.assertEqual(error.exception.status_code, 403)

    def test_device_starting_during_offer_can_receive_it(self):
        store = FileTransferStore()
        offer = store.create_offer({
            "device_id": "sender_00000001", "device_name": "PC origem", "name": "arquivo.txt",
            "kind": "file", "size": 4, "expanded_size": 4, "file_count": 1,
            "sha256": content_hash("data"),
        })
        available = store.list_offers("receiver_00000001", "PC destino")
        self.assertEqual([item["id"] for item in available], [offer["id"]])

    def test_file_offer_upload_download_and_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileTransferStore(root / "spool")
            state = FakeServerState()
            server = UnifiedClipboardSyncServer(("127.0.0.1", 0), "teste-token", state, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = Endpoint("127.0.0.1", server.server_port, "teste-token")
            source = root / "source.txt"
            source.write_bytes(b"arquivo de teste")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            try:
                _api_json(endpoint, "GET", "/v2/offers", device_id="receiver_00000001", device_name="PC destino")
                offer = _api_json(endpoint, "POST", "/v2/offers", {
                    "device_id": "sender_00000001", "device_name": "PC origem", "name": source.name,
                    "kind": "file", "size": source.stat().st_size,
                    "expanded_size": source.stat().st_size, "file_count": 1, "sha256": digest,
                })
                transfer_id = offer["id"]
                _api_json(endpoint, "POST", f"/v2/offers/{transfer_id}/accept", {
                    "device_id": "receiver_00000001", "device_name": "PC destino",
                })
                body, response = _stream_transfer(endpoint, "PUT", transfer_id, source, device_id="sender_00000001")
                self.assertEqual(response[0], 200, body.decode("utf-8", errors="replace"))
                ready = _api_json(endpoint, "GET", f"/v2/offers/{transfer_id}/status", device_id="receiver_00000001")
                self.assertEqual(ready["status"], "ready")
                destination = root / "download.txt"
                _stream_download(endpoint, transfer_id, "receiver_00000001", destination, source.stat().st_size, digest)
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                _api_json(endpoint, "POST", f"/v2/transfers/{transfer_id}/complete", {"device_id": "receiver_00000001"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_folder_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "projeto"
            (source / "subpasta").mkdir(parents=True)
            (source / "arquivo.txt").write_text("conteúdo", encoding="utf-8")
            (source / "subpasta" / "outro.txt").write_text("mais", encoding="utf-8")
            archive, name, _size, expanded, count, _digest, temporary_archive = _prepare_send_path(source)
            self.assertTrue(temporary_archive)
            destination = root / "extraido"
            try:
                _safe_extract_zip(archive, destination, count, expanded)
                self.assertEqual(name, "projeto")
                self.assertEqual((destination / "arquivo.txt").read_text(encoding="utf-8"), "conteúdo")
                self.assertEqual((destination / "subpasta" / "outro.txt").read_text(encoding="utf-8"), "mais")
            finally:
                archive.unlink(missing_ok=True)

    def test_folder_transfer_download_uses_new_archive_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_directory = root / "LMay Ocarina Tab Maker"
            source_directory.mkdir()
            (source_directory / "README.txt").write_text("projeto", encoding="utf-8")
            archive, name, size, expanded, count, digest, temporary_archive = _prepare_send_path(source_directory)
            store = FileTransferStore(root / "spool")
            state = FakeServerState()
            server = UnifiedClipboardSyncServer(("127.0.0.1", 0), "teste-token", state, store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = Endpoint("127.0.0.1", server.server_port, "teste-token")
            try:
                _api_json(endpoint, "GET", "/v2/offers", device_id="receiver_00000001", device_name="PC destino")
                offer = _api_json(endpoint, "POST", "/v2/offers", {
                    "device_id": "sender_00000001", "device_name": "PC origem", "name": name,
                    "kind": "folder", "size": size, "expanded_size": expanded,
                    "file_count": count, "sha256": digest,
                })
                transfer_id = offer["id"]
                _api_json(endpoint, "POST", f"/v2/offers/{transfer_id}/accept", {
                    "device_id": "receiver_00000001", "device_name": "PC destino",
                })
                _body, response = _stream_transfer(endpoint, "PUT", transfer_id, archive, device_id="sender_00000001")
                self.assertEqual(response[0], 200)
                saved = _download_folder(
                    endpoint, transfer_id, "receiver_00000001", offer, root / "Downloads"
                )
                self.assertEqual((saved / "README.txt").read_text(encoding="utf-8"), "projeto")
            finally:
                if temporary_archive:
                    archive.unlink(missing_ok=True)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_offer_expires_after_thirty_second_deadline(self):
        store = FileTransferStore()
        store.register_device("receiver_00000001", "PC destino")
        offer = store.create_offer({
            "device_id": "sender_00000001", "device_name": "PC origem", "name": "arquivo.txt",
            "kind": "file", "size": 4, "expanded_size": 4, "file_count": 1,
            "sha256": content_hash("data"),
        })
        store._offers[offer["id"]].deadline = time.monotonic() - 1
        with self.assertRaises(TransferError) as error:
            store.accept(offer["id"], "receiver_00000001", "PC destino")
        self.assertEqual(error.exception.status_code, 410)


if __name__ == "__main__":
    unittest.main()
