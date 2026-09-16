#!/usr/bin/env python3
"""Sincroniza o clipboard e transfere arquivos entre PCs e Android na rede local."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


APP_VERSION = "1"
HTTP_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"CLIPSYNC_DISCOVER_V1"
TOKEN_FILE = Path(__file__).resolve().parent / "pc" / "clipboard_sync.token"
MAX_TEXT_BYTES = 2 * 1024 * 1024
PEER_DISCOVERY_PREFIX = b"CLIPSYNC_PEER_V2|"
PEER_DISCOVERY_PORT = DISCOVERY_PORT
DEVICE_HEARTBEAT_TTL = 15.0
PEER_TTL = 4.0
STARTUP_ELECTION_DELAY = 3.0
MAX_TRANSFER_BYTES = 10 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 50_000
TRANSFER_DIRECTORY = Path(tempfile.gettempdir()) / "ClipboardSyncTransfers"
DEVICE_ID_FILE = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / ".config")
) / "ClipboardSync" / "device_id"


class ClipboardError(RuntimeError):
    """Indica que o sistema operacional não permitiu acessar o clipboard."""


if os.name == "nt":
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _windows_open_clipboard() -> None:
    for _ in range(10):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise ClipboardError(f"não foi possível abrir o clipboard: {ctypes.WinError()}")


def read_clipboard() -> str:
    """Lê texto do clipboard, usando a API nativa do Windows quando disponível."""

    if os.name == "nt":
        _windows_open_clipboard()
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError(f"não foi possível bloquear o clipboard: {ctypes.WinError()}")
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception as error:
        raise ClipboardError(
            "a leitura de clipboard fora do Windows requer um ambiente gráfico com tkinter"
        ) from error


def write_clipboard(text: str) -> None:
    """Substitui o texto do clipboard."""

    if os.name == "nt":
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        _windows_open_clipboard()
        handle = None
        try:
            if not user32.EmptyClipboard():
                raise ClipboardError(f"não foi possível limpar o clipboard: {ctypes.WinError()}")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not handle:
                raise ClipboardError(f"não foi possível reservar memória: {ctypes.WinError()}")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError(f"não foi possível bloquear a memória: {ctypes.WinError()}")
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ClipboardError(f"não foi possível gravar o clipboard: {ctypes.WinError()}")
            handle = None  # a propriedade foi transferida para o Windows
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()
        return

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
        finally:
            root.destroy()
    except Exception as error:
        raise ClipboardError(
            "a gravação de clipboard fora do Windows requer um ambiente gráfico com tkinter"
        ) from error


class ClipboardState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._text = ""
        self._updated_at = time.time()
        self._source = "pc"

    def read_current(self) -> tuple[str, float, str, bool]:
        with self._lock:
            text = read_clipboard()
            changed = text != self._text
            if changed:
                self._text = text
                self._updated_at = time.time()
                self._source = "pc"
            return self._text, self._updated_at, self._source, changed

    def write_from_client(self, text: str, source: str) -> tuple[float, str]:
        with self._lock:
            write_clipboard(text)
            self._text = text
            self._updated_at = time.time()
            self._source = source
            return self._updated_at, self._source

    def write_from_phone(self, text: str) -> tuple[float, str]:
        """Mantém compatibilidade com chamadas antigas do servidor."""
        return self.write_from_client(text, "android")


class TransferError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FileOffer:
    transfer_id: str
    sender_id: str
    sender_name: str
    name: str
    kind: str
    size: int
    sha256: str
    expanded_size: int
    file_count: int
    recipients: set[str]
    deadline: float
    expires_at: float
    status: str = "offered"
    declined: set[str] = field(default_factory=set)
    accepted_by: str = ""
    accepted_name: str = ""
    upload_deadline: float = 0.0
    file_path: Path | None = None
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0


def _valid_device_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value or ""))


def _clean_display_name(value: str, *, filename: bool = False) -> str:
    value = "".join(char for char in value if char.isprintable()).strip()
    if not value or len(value) > 255:
        raise TransferError(400, "nome vazio ou grande demais")
    if filename and (
        value in {".", ".."}
        or value.endswith((".", " "))
        or any(char in value for char in ("/", "\\", ":"))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)
    ):
        raise TransferError(400, "o nome enviado precisa ser apenas um nome de arquivo ou pasta")
    return value


class FileTransferStore:
    """Mantém ofertas e arquivos temporários com aceite exclusivo."""

    def __init__(self, directory: Path = TRANSFER_DIRECTORY) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._devices: dict[str, tuple[str, float]] = {}
        self._offers: dict[str, FileOffer] = {}

    def register_device(self, device_id: str, device_name: str) -> str:
        if not _valid_device_id(device_id):
            raise TransferError(400, "identificador de dispositivo inválido")
        name = _clean_display_name(device_name or "Dispositivo")
        with self._lock:
            self._devices[device_id] = (name, time.monotonic())
        return name

    def create_offer(self, metadata: dict[str, Any]) -> dict[str, Any]:
        sender_id = metadata.get("device_id")
        if not isinstance(sender_id, str) or not _valid_device_id(sender_id):
            raise TransferError(400, "identificador de remetente inválido")
        sender_name = self.register_device(sender_id, str(metadata.get("device_name", "PC")))
        name = _clean_display_name(str(metadata.get("name", "")), filename=True)
        kind = metadata.get("kind")
        size = metadata.get("size")
        expanded_size = metadata.get("expanded_size")
        file_count = metadata.get("file_count")
        digest = metadata.get("sha256")
        if kind not in {"file", "folder"}:
            raise TransferError(400, "tipo de transferência inválido")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_TRANSFER_BYTES:
            raise TransferError(413, "tamanho do arquivo fora do limite aceito")
        if (
            not isinstance(expanded_size, int)
            or isinstance(expanded_size, bool)
            or not 0 <= expanded_size <= MAX_EXPANDED_BYTES
        ):
            raise TransferError(413, "tamanho descompactado fora do limite aceito")
        if (
            not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or not 0 <= file_count <= MAX_ARCHIVE_FILES
            or (kind == "file" and file_count != 1)
        ):
            raise TransferError(400, "quantidade de arquivos inválida")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise TransferError(400, "SHA-256 inválido")

        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            recipients = {
                device_id
                for device_id, (_name, last_seen) in self._devices.items()
                if device_id != sender_id and now - last_seen <= DEVICE_HEARTBEAT_TTL
            }
            offer = FileOffer(
                transfer_id=uuid.uuid4().hex,
                sender_id=sender_id,
                sender_name=sender_name,
                name=name,
                kind=kind,
                size=size,
                sha256=digest.lower(),
                expanded_size=expanded_size,
                file_count=file_count,
                recipients=recipients,
                deadline=now + 30.0,
                expires_at=time.time() + 30.0,
            )
            self._offers[offer.transfer_id] = offer
            return self._offer_payload(offer)

    def list_offers(self, device_id: str, device_name: str) -> list[dict[str, Any]]:
        self.register_device(device_id, device_name)
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            # Inclui aparelhos que começaram a consultar ofertas depois da criação.
            for offer in self._offers.values():
                if offer.status == "offered" and device_id != offer.sender_id and now < offer.deadline:
                    offer.recipients.add(device_id)
            return [
                self._offer_payload(offer)
                for offer in self._offers.values()
                if offer.status == "offered"
                and device_id in offer.recipients
                and device_id not in offer.declined
                and now < offer.deadline
            ]

    def accept(self, transfer_id: str, device_id: str, device_name: str) -> dict[str, Any]:
        name = self.register_device(device_id, device_name)
        with self._lock:
            self._cleanup_locked()
            offer = self._find_locked(transfer_id)
            if device_id == offer.sender_id or device_id not in offer.recipients:
                raise TransferError(403, "este dispositivo não pode aceitar a oferta")
            if offer.status == "expired":
                raise TransferError(410, "a oferta expirou")
            if offer.status == "accepted" and offer.accepted_by == device_id:
                return self._offer_payload(offer)
            if offer.status != "offered":
                raise TransferError(409, "outro dispositivo já aceitou ou a oferta não está mais disponível")
            if time.monotonic() >= offer.deadline:
                offer.status = "expired"
                offer.finished_at = time.monotonic()
                raise TransferError(410, "a oferta expirou")
            offer.status = "accepted"
            offer.accepted_by = device_id
            offer.accepted_name = name
            offer.upload_deadline = time.monotonic() + 300.0
            return self._offer_payload(offer)

    def decline(self, transfer_id: str, device_id: str) -> None:
        with self._lock:
            self._cleanup_locked()
            offer = self._find_locked(transfer_id)
            if device_id not in offer.recipients:
                raise TransferError(403, "este dispositivo não recebeu a oferta")
            if offer.status == "offered":
                offer.declined.add(device_id)

    def status(self, transfer_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            offer = self._find_locked(transfer_id)
            if device_id not in {offer.sender_id, offer.accepted_by}:
                raise TransferError(403, "este dispositivo não pode consultar a transferência")
            return self._offer_payload(offer)

    def begin_upload(self, transfer_id: str, device_id: str, length: int) -> tuple[FileOffer, Path]:
        with self._lock:
            self._cleanup_locked()
            offer = self._find_locked(transfer_id)
            if device_id != offer.sender_id:
                raise TransferError(403, "somente o remetente pode enviar o conteúdo")
            if offer.status != "accepted":
                raise TransferError(409, "o conteúdo só pode ser enviado depois do aceite")
            if time.monotonic() >= offer.upload_deadline:
                offer.status = "failed"
                offer.finished_at = time.monotonic()
                raise TransferError(410, "o prazo para iniciar a transferência expirou")
            if length != offer.size:
                raise TransferError(400, "o tamanho enviado não corresponde à oferta")
            if not 0 <= length <= MAX_TRANSFER_BYTES:
                raise TransferError(413, "tamanho do arquivo fora do limite aceito")
            offer.status = "uploading"
            path = self.directory / f"{offer.transfer_id}.part"
            return offer, path

    def finish_upload(self, transfer_id: str, path: Path, digest: str, success: bool) -> dict[str, Any]:
        with self._lock:
            offer = self._find_locked(transfer_id)
            if not success or digest.lower() != offer.sha256:
                path.unlink(missing_ok=True)
                offer.status = "failed"
                offer.finished_at = time.monotonic()
                if success:
                    raise TransferError(400, "o SHA-256 do conteúdo não corresponde à oferta")
                raise TransferError(400, "o upload foi interrompido")
            final_path = self.directory / f"{offer.transfer_id}.bin"
            os.replace(path, final_path)
            offer.file_path = final_path
            offer.status = "ready"
            return self._offer_payload(offer)

    def download(self, transfer_id: str, device_id: str) -> tuple[FileOffer, Path]:
        with self._lock:
            self._cleanup_locked()
            offer = self._find_locked(transfer_id)
            if device_id != offer.accepted_by:
                raise TransferError(403, "somente o dispositivo que aceitou pode baixar o arquivo")
            if offer.status != "ready" or offer.file_path is None:
                raise TransferError(409, "o conteúdo ainda não está pronto")
            return offer, offer.file_path

    def complete(self, transfer_id: str, device_id: str) -> None:
        with self._lock:
            offer = self._find_locked(transfer_id)
            if device_id != offer.accepted_by:
                raise TransferError(403, "somente o dispositivo que aceitou pode concluir")
            offer.status = "completed"
            offer.finished_at = time.monotonic()
            self._delete_file_locked(offer)

    def cancel(self, transfer_id: str, device_id: str) -> None:
        with self._lock:
            offer = self._find_locked(transfer_id)
            if device_id != offer.sender_id:
                raise TransferError(403, "somente o remetente pode cancelar")
            if offer.status in {"offered", "accepted", "uploading", "ready"}:
                offer.status = "cancelled"
                offer.finished_at = time.monotonic()
                self._delete_file_locked(offer)

    def _find_locked(self, transfer_id: str) -> FileOffer:
        if not re.fullmatch(r"[a-f0-9]{32}", transfer_id or ""):
            raise TransferError(404, "oferta não encontrada")
        offer = self._offers.get(transfer_id)
        if offer is None:
            raise TransferError(404, "oferta não encontrada")
        return offer

    def _offer_payload(self, offer: FileOffer) -> dict[str, Any]:
        return {
            "id": offer.transfer_id,
            "name": offer.name,
            "kind": offer.kind,
            "size": offer.size,
            "expanded_size": offer.expanded_size,
            "file_count": offer.file_count,
            "sha256": offer.sha256,
            "sender_id": offer.sender_id,
            "sender_name": offer.sender_name,
            "status": offer.status,
            "accepted_by": offer.accepted_by,
            "accepted_name": offer.accepted_name,
            "expires_at": offer.expires_at,
            "expires_in": max(0.0, offer.deadline - time.monotonic()),
        }

    def _cleanup_locked(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for transfer_id, offer in list(self._offers.items()):
            if offer.status == "offered" and now >= offer.deadline:
                offer.status = "expired"
                offer.finished_at = now
            elif offer.status in {"accepted", "uploading"} and now >= offer.upload_deadline:
                offer.status = "failed"
                offer.finished_at = now
                self._delete_file_locked(offer)
            if offer.status in {"expired", "failed", "cancelled", "completed"}:
                self._delete_file_locked(offer)
                if offer.finished_at and now - offer.finished_at > 3600:
                    self._offers.pop(transfer_id, None)

    @staticmethod
    def _delete_file_locked(offer: FileOffer) -> None:
        if offer.file_path is not None:
            offer.file_path.unlink(missing_ok=True)
            offer.file_path = None


class ClipboardSyncServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, state: ClipboardState) -> None:
        super().__init__(address, ClipboardRequestHandler)
        self.token = token
        self.state = state


class ClipboardRequestHandler(BaseHTTPRequestHandler):
    server: ClipboardSyncServer

    def log_message(self, format: str, *args: Any) -> None:
        if len(args) >= 2:
            try:
                if 200 <= int(args[1]) < 400:
                    return
            except (TypeError, ValueError):
                pass
        print(f"[HTTP] {self.address_string()} - {format % args}")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Clipboard-Token", "")
        if secrets.compare_digest(supplied, self.server.token):
            return True
        self._send_json(401, {"error": "token inválido"})
        return False

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"service": "clipboard-sync", "version": APP_VERSION})
            return
        if self.path != "/v1/clipboard" or not self._authorized():
            if self.path != "/v1/clipboard":
                self._send_json(404, {"error": "rota não encontrada"})
            return
        try:
            text, updated_at, source, _changed = self.server.state.read_current()
            self._send_json(
                200,
                {"text": text, "updated_at": updated_at, "source": source},
            )
        except ClipboardError as error:
            self._send_json(500, {"error": str(error)})

    def do_POST(self) -> None:
        if self.path != "/v1/clipboard":
            self._send_json(404, {"error": "rota não encontrada"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Content-Length inválido"})
            return
        if length <= 0 or length > MAX_TEXT_BYTES:
            self._send_json(413, {"error": "clipboard vazio ou grande demais"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = payload["text"]
            if not isinstance(text, str):
                raise TypeError
            source = self.headers.get("X-Clipboard-Source", "android")
            if source not in {"android", "desktop"}:
                source = "cliente"
            updated_at, source = self.server.state.write_from_client(text, source)
            if source == "android":
                label = "Android"
            elif source == "desktop":
                label = "PC cliente"
            else:
                label = "cliente"
            print(f"[+] Clipboard recebido de {label} ({len(text)} caracteres).")
            self._send_json(200, {"ok": True, "updated_at": updated_at, "source": source})
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            self._send_json(400, {"error": "JSON inválido; esperado {\"text\": \"...\"}"})
        except ClipboardError as error:
            self._send_json(500, {"error": str(error)})


class UnifiedClipboardRequestHandler(ClipboardRequestHandler):
    """Mantém as rotas v1 do APK e atende as transferências v2."""

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/v2/offers":
            if not self._authorized():
                return
            try:
                offers = self.server.transfers.list_offers(
                    self.headers.get("X-Clipboard-Device-Id", ""),
                    _decode_header_value(self.headers["X-Clipboard-Device-Name"])
                    if self.headers.get("X-Clipboard-Device-Name")
                    else "Dispositivo",
                )
                self._send_json(200, {"offers": offers})
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            return

        match = re.fullmatch(r"/v2/offers/([a-f0-9]{32})/status", parsed.path)
        if match:
            if not self._authorized():
                return
            try:
                result = self.server.transfers.status(
                    match.group(1), self.headers.get("X-Clipboard-Device-Id", "")
                )
                self._send_json(200, result)
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            return

        match = re.fullmatch(r"/v2/transfers/([a-f0-9]{32})/content", parsed.path)
        if match:
            if not self._authorized():
                return
            try:
                offer, path = self.server.transfers.download(
                    match.group(1), self.headers.get("X-Clipboard-Device-Id", "")
                )
                try:
                    source = path.open("rb")
                except OSError as error:
                    self._send_json(500, {"error": f"não foi possível ler o arquivo temporário: {error}"})
                    return
                headers_sent = False
                with source:
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(offer.size))
                        self.send_header("X-Clipboard-File-Name", _encode_header_value(offer.name))
                        self.send_header("X-Clipboard-File-Kind", offer.kind)
                        self.send_header("X-Clipboard-File-SHA256", offer.sha256)
                        self.end_headers()
                        headers_sent = True
                        shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
                    except (ConnectionError, TimeoutError):
                        # Clientes podem cancelar a transferência ao fechar a janela ou falhar ao salvar.
                        return
                    except OSError as error:
                        if headers_sent:
                            print(f"[HTTP] falha durante envio do arquivo {offer.transfer_id}: {error}")
                        else:
                            with contextlib.suppress(ConnectionError, OSError):
                                self._send_json(500, {"error": f"não foi possível enviar o arquivo: {error}"})
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/v2/offers":
            if not self._authorized():
                return
            try:
                payload = self._read_json_body()
                offer = self.server.transfers.create_offer(payload)
                self._send_json(201, offer)
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                self._send_json(400, {"error": "JSON inválido para criar a oferta"})
            return

        match = re.fullmatch(r"/v2/offers/([a-f0-9]{32})/(accept|decline|cancel)", parsed.path)
        if match:
            if not self._authorized():
                return
            try:
                payload = self._read_json_body()
                transfer_id, action = match.groups()
                device_id = payload.get("device_id", "")
                if action == "accept":
                    result = self.server.transfers.accept(
                        transfer_id, device_id, str(payload.get("device_name", "Dispositivo"))
                    )
                    self._send_json(200, result)
                elif action == "decline":
                    self.server.transfers.decline(transfer_id, device_id)
                    self._send_json(200, {"ok": True})
                else:
                    self.server.transfers.cancel(transfer_id, device_id)
                    self._send_json(200, {"ok": True})
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
                self._send_json(400, {"error": "JSON inválido para esta ação"})
            return

        match = re.fullmatch(r"/v2/transfers/([a-f0-9]{32})/complete", parsed.path)
        if match:
            if not self._authorized():
                return
            try:
                payload = self._read_json_body()
                self.server.transfers.complete(match.group(1), payload.get("device_id", ""))
                self._send_json(200, {"ok": True})
            except TransferError as error:
                self._send_json(error.status_code, {"error": str(error)})
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
                self._send_json(400, {"error": "JSON inválido para concluir a transferência"})
            return

        super().do_POST()

    def do_PUT(self) -> None:
        parsed = urlsplit(self.path)
        match = re.fullmatch(r"/v2/transfers/([a-f0-9]{32})/content", parsed.path)
        if not match:
            self._send_json(404, {"error": "rota não encontrada"})
            return
        if not self._authorized():
            return
        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise TransferError(411, "Content-Length obrigatório")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise TransferError(400, "Content-Length inválido") from error
            offer, part_path = self.server.transfers.begin_upload(
                match.group(1), self.headers.get("X-Clipboard-Device-Id", ""), length
            )
            digest = hashlib.sha256()
            remaining = length
            try:
                self.connection.settimeout(30.0)
                with part_path.open("wb") as destination:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise TransferError(400, "upload interrompido antes do fim do arquivo")
                        destination.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
            except (OSError, TimeoutError, TransferError) as error:
                with contextlib.suppress(TransferError):
                    self.server.transfers.finish_upload(offer.transfer_id, part_path, "", False)
                if isinstance(error, TransferError):
                    raise
                raise TransferError(408, f"upload interrompido: {error}") from error
            result = self.server.transfers.finish_upload(
                offer.transfer_id, part_path, digest.hexdigest(), True
            )
            self._send_json(200, result)
        except TransferError as error:
            self._send_json(error.status_code, {"error": str(error)})
        except OSError as error:
            self._send_json(500, {"error": f"não foi possível salvar o arquivo: {error}"})

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise TransferError(400, "Content-Length inválido") from error
        if length <= 0 or length > 64 * 1024:
            raise TransferError(413, "requisição JSON vazia ou grande demais")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("esperado um objeto JSON")
        return value


def _encode_header_value(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_header_value(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise TransferError(400, "nome codificado inválido") from error


class UnifiedClipboardSyncServer(ClipboardSyncServer):
    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        state: ClipboardState,
        transfers: FileTransferStore,
    ) -> None:
        self.transfers = transfers
        super().__init__(address, token, state)
        self.RequestHandlerClass = UnifiedClipboardRequestHandler


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    token: str
    name: str = ""


class ClipboardClientError(RuntimeError):
    pass


class AuthenticationError(ClipboardClientError):
    pass


class SystemClipboardBackend:
    def read(self) -> str:
        return read_clipboard()

    def write(self, text: str) -> None:
        write_clipboard(text)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ClientClipboardState:
    def __init__(self, backend: Any, observed_hash: str) -> None:
        self.backend = backend
        self.observed_hash = observed_hash

    @classmethod
    def from_backend(cls, backend: Any) -> "ClientClipboardState":
        return cls(backend, content_hash(backend.read()))

    def poll_local_change(self) -> str | None:
        text = self.backend.read()
        digest = content_hash(text)
        if digest == self.observed_hash:
            return None
        self.observed_hash = digest
        return text

    def apply_remote(self, text: str) -> bool:
        digest = content_hash(text)
        if digest == self.observed_hash:
            return False
        self.backend.write(text)
        self.observed_hash = digest
        return True


def parse_discovery_response(payload: str, host: str) -> Endpoint | None:
    parts = payload.split("|", 3)
    if len(parts) != 4 or parts[0] != "CLIPSYNC/1":
        return None
    try:
        port = int(parts[1])
    except ValueError:
        return None
    if not host or not parts[2] or not 1 <= port <= 65535:
        return None
    return Endpoint(host, port, parts[2], parts[3])


def discover_endpoint(host: str, timeout: float = 1.0) -> Endpoint | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(DISCOVERY_REQUEST, (host, DISCOVERY_PORT))
            payload, address = sock.recvfrom(1024)
        except (OSError, socket.timeout):
            return None
    try:
        return parse_discovery_response(payload.decode("utf-8"), address[0])
    except UnicodeDecodeError:
        return None


def server_request(
    endpoint: Endpoint,
    method: str,
    text: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "X-Clipboard-Token": endpoint.token}
    data = None
    if text is not None:
        data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise ClipboardClientError("O clipboard excede o limite de 2 MiB aceito pelo servidor.")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["X-Clipboard-Source"] = "desktop"
    request = Request(
        f"http://{endpoint.host}:{endpoint.port}/v1/clipboard",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code in {401, 403}:
            raise AuthenticationError("O servidor recusou o token de clipboard.") from error
        raise ClipboardClientError(f"Servidor respondeu HTTP {error.code}: {detail}") from error
    except (URLError, OSError, TimeoutError) as error:
        raise ClipboardClientError(f"Não foi possível conectar ao servidor: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClipboardClientError("O servidor retornou uma resposta inválida.") from error
    if not isinstance(value, dict) or (method == "GET" and not isinstance(value.get("text"), str)):
        raise ClipboardClientError("O servidor retornou um clipboard inválido.")
    return value


def _api_json(
    endpoint: Endpoint,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    device_id: str = "",
    device_name: str = "",
    timeout: float = 5.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "X-Clipboard-Token": endpoint.token}
    if device_id:
        headers["X-Clipboard-Device-Id"] = device_id
    if device_name:
        headers["X-Clipboard-Device-Name"] = _encode_header_value(device_name)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(f"http://{endpoint.host}:{endpoint.port}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ClipboardClientError("Resposta inválida do servidor.")
            return value
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ClipboardClientError(f"HTTP {error.code}: {detail}") from error
    except (URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClipboardClientError(f"Falha na comunicação com o servidor: {error}") from error


def load_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_hex(24)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write(token + "\n")
    return token


def _load_or_create_device_id(path: Path = DEVICE_ID_FILE) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="ascii").strip()
        if _valid_device_id(value):
            return value
    except FileNotFoundError:
        pass
    value = uuid.uuid4().hex
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="ascii").strip()
    with os.fdopen(descriptor, "w", encoding="ascii") as destination:
        destination.write(value + "\n")
    return value


class PeerDiscovery:
    """Descobre os PCs e escolhe o nó de menor identificador como líder."""

    def __init__(self, port: int, startup_delay: float = STARTUP_ELECTION_DELAY) -> None:
        self.node_id = _load_or_create_device_id()
        self.boot_id = uuid.uuid4().hex
        self.hostname = socket.gethostname().replace("|", "-")
        self.port = port
        self.startup_delay = startup_delay
        self.started = time.monotonic()
        self.peers: dict[tuple[str, str], tuple[str, str, int, float]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self.is_leader = False
        self.leader: tuple[str, str, str, int] | None = None
        self.ready = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clipboard-peer-discovery", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self) -> tuple[bool, tuple[str, str, str, int] | None]:
        with self._lock:
            return self.is_leader, self.leader

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        ) as sender:
            try:
                listener.bind(("0.0.0.0", DISCOVERY_PORT))
                listener.settimeout(0.25)
                sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError as error:
                self.error = f"Não foi possível abrir a descoberta UDP {DISCOVERY_PORT}: {error}"
                print(f"[ERRO] {self.error}")
                return
            last_beacon = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_beacon >= 0.8:
                    record = {
                        "node": self.node_id,
                        "boot": self.boot_id,
                        "name": self.hostname,
                        "port": self.port,
                    }
                    payload = PEER_DISCOVERY_PREFIX + json.dumps(record, separators=(",", ":")).encode("utf-8")
                    with contextlib.suppress(OSError):
                        sender.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                    last_beacon = now
                try:
                    payload, address = listener.recvfrom(2048)
                except socket.timeout:
                    payload = b""
                    address = ("", 0)
                except OSError:
                    continue
                if payload == DISCOVERY_REQUEST:
                    with self._lock:
                        leader = self.is_leader
                    if leader:
                        response = f"CLIPSYNC/{APP_VERSION}|{self.port}|{self.token}|{self.hostname}".encode("utf-8")
                        with contextlib.suppress(OSError):
                            listener.sendto(response, address)
                elif payload.startswith(PEER_DISCOVERY_PREFIX):
                    try:
                        record = json.loads(payload[len(PEER_DISCOVERY_PREFIX):].decode("utf-8"))
                        node_id, boot_id = record["node"], record["boot"]
                        name, port = record["name"], int(record["port"])
                        if not _valid_device_id(node_id) or not _valid_device_id(boot_id) or not 1 <= port <= 65535:
                            raise ValueError("registro de peer inválido")
                        with self._lock:
                            self.peers[(node_id, boot_id)] = (address[0], str(name)[:100], port, time.monotonic())
                    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                        pass
                now = time.monotonic()
                with self._lock:
                    for key, peer in list(self.peers.items()):
                        if now - peer[3] > PEER_TTL:
                            self.peers.pop(key, None)
                    nodes = [(self.node_id, self.boot_id, "127.0.0.1", self.hostname, self.port)]
                    nodes.extend((key[0], key[1], *value[:3]) for key, value in self.peers.items())
                    winner = min(nodes, key=lambda row: (row[0], row[1]))
                    self.ready = now - self.started >= self.startup_delay
                    self.is_leader = self.ready and winner[0] == self.node_id and winner[1] == self.boot_id
                    self.leader = (winner[0], winner[1], winner[2], winner[4])

    token: str = ""


class InputReader:
    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True, name="clipboard-console-input").start()

    def _read(self) -> None:
        if os.name == "nt":
            import msvcrt
            buffer = ""
            while not self.stop.is_set():
                if not msvcrt.kbhit():
                    time.sleep(0.05)
                    continue
                char = msvcrt.getwch()
                if char in {"\r", "\n"}:
                    print()
                    self.lines.put(buffer)
                    buffer = ""
                elif char == "\x03":
                    self.lines.put("\x03")
                elif char == "\b":
                    buffer = buffer[:-1]
                elif char in {"\x00", "\xe0"}:
                    with contextlib.suppress(Exception):
                        msvcrt.getwch()
                else:
                    buffer += char
                    print(char, end="", flush=True)
            return
        while not self.stop.is_set():
            try:
                line = sys.stdin.readline()
            except (EOFError, OSError):
                return
            if not line:
                time.sleep(0.1)
                continue
            self.lines.put(line.rstrip("\r\n"))


def _unique_download_path(downloads: Path, name: str) -> Path:
    downloads.mkdir(parents=True, exist_ok=True)
    candidate = downloads / name
    if not candidate.exists():
        return candidate
    path = Path(name)
    for index in range(1, 100_000):
        candidate = downloads / f"{path.stem} ({index}){path.suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("não foi possível encontrar um nome livre na pasta Downloads")


def _safe_extract_zip(archive_path: Path, destination: Path, expected_files: int, expected_size: int) -> None:
    seen: dict[str, tuple[str, bool]] = {}
    total_size = 0
    file_count = 0
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_FILES * 2:
                raise TransferError(400, "a pasta contém entradas demais")
            for entry in entries:
                raw = entry.filename.replace("\\", "/")
                if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
                    raise TransferError(400, "caminho inválido dentro do arquivo compactado")
                parts = [part for part in raw.split("/") if part]
                if not parts or any(part in {".", ".."} or ":" in part or part.endswith((".", " ")) for part in parts):
                    raise TransferError(400, "caminho inseguro dentro do arquivo compactado")
                if len(parts) > 64:
                    raise TransferError(400, "a pasta possui níveis demais")
                for part in parts:
                    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I):
                        raise TransferError(400, "nome reservado do Windows dentro do arquivo compactado")
                parts = [unicodedata.normalize("NFC", part) for part in parts]
                is_directory = entry.is_dir()
                for index in range(1, len(parts) + 1):
                    relative = "/".join(parts[:index])
                    key = relative.casefold()
                    is_final = index == len(parts)
                    item_is_directory = is_directory if is_final else True
                    old = seen.get(key)
                    if old is not None:
                        if old[0] != relative or (not is_final and not old[1]):
                            raise TransferError(400, "há nomes duplicados ou conflitos de caminho")
                        if is_final and old[1] != item_is_directory:
                            raise TransferError(400, "há conflito entre arquivo e pasta")
                        if is_final and not item_is_directory:
                            raise TransferError(400, "há nomes duplicados no arquivo compactado")
                    else:
                        seen[key] = (relative, item_is_directory)
                mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}):
                    raise TransferError(400, "atalhos e entradas especiais não são permitidos")
                target = destination.joinpath(*parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                file_count += 1
                total_size += entry.file_size
                if file_count > MAX_ARCHIVE_FILES or total_size > MAX_EXPANDED_BYTES:
                    raise TransferError(413, "a pasta excede os limites de extração")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        if file_count != expected_files or total_size != expected_size:
            raise TransferError(400, "o conteúdo da pasta não corresponde à oferta")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _prepare_send_path(source: Path) -> tuple[Path, str, int, int, int, str, bool]:
    source = source.expanduser()
    if source.is_symlink():
        raise OSError("links simbólicos não podem ser enviados")
    source = source.resolve(strict=True)
    temporary = False
    if source.is_file():
        size = source.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise OSError("o arquivo excede o limite de 10 GiB")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return source, source.name, size, size, 1, digest.hexdigest(), temporary
    if not source.is_dir():
        raise OSError("o caminho precisa apontar para um arquivo ou uma pasta")
    descriptor, archive_name = tempfile.mkstemp(prefix="clipboardsync-", suffix=".zip")
    os.close(descriptor)
    archive_path = Path(archive_name)
    temporary = True
    expanded_size = 0
    file_count = 0
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for entry in sorted(source.rglob("*")):
                if entry.is_symlink():
                    raise OSError(f"a pasta contém um link simbólico: {entry}")
                relative = entry.relative_to(source).as_posix()
                if entry.is_dir():
                    archive.writestr(relative.rstrip("/") + "/", b"")
                    continue
                if not entry.is_file():
                    raise OSError(f"a pasta contém um item especial: {entry}")
                file_count += 1
                expanded_size += entry.stat().st_size
                if file_count > MAX_ARCHIVE_FILES or expanded_size > MAX_EXPANDED_BYTES:
                    raise OSError("a pasta excede os limites de transferência")
                archive.write(entry, relative)
        size = archive_path.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise OSError("a pasta compactada excede o limite de 10 GiB")
        digest_state = hashlib.sha256()
        with archive_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest_state.update(chunk)
        digest = digest_state.hexdigest()
        return archive_path, source.name, size, expanded_size, file_count, digest, temporary
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _stream_transfer(endpoint: Endpoint, method: str, transfer_id: str, path: Path | None, *, device_id: str) -> tuple[bytes, Any]:
    connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=60)
    route = f"/v2/transfers/{transfer_id}/content"
    headers = {"X-Clipboard-Token": endpoint.token, "X-Clipboard-Device-Id": device_id}
    try:
        if method == "PUT":
            assert path is not None
            headers["Content-Length"] = str(path.stat().st_size)
            connection.putrequest("PUT", route)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    connection.send(chunk)
        else:
            connection.request("GET", route, headers=headers)
        response = connection.getresponse()
        return response.read(), (response.status, response.reason, dict(response.getheaders()))
    finally:
        connection.close()


def _stream_download(endpoint: Endpoint, transfer_id: str, device_id: str, destination: Path, expected_size: int, expected_hash: str) -> None:
    connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=60)
    try:
        connection.request(
            "GET", f"/v2/transfers/{transfer_id}/content",
            headers={"X-Clipboard-Token": endpoint.token, "X-Clipboard-Device-Id": device_id},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ClipboardClientError(f"HTTP {response.status}: {response.read().decode('utf-8', errors='replace')}")
        if int(response.getheader("Content-Length", "-1")) != expected_size:
            raise ClipboardClientError("o tamanho informado pelo servidor não corresponde à oferta")
        digest = hashlib.sha256()
        received = 0
        with destination.open("xb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
        if received != expected_size or digest.hexdigest() != expected_hash:
            destination.unlink(missing_ok=True)
            raise ClipboardClientError("a verificação do arquivo recebido falhou")
    finally:
        connection.close()


def _download_folder(
    endpoint: Endpoint,
    transfer_id: str,
    device_id: str,
    offer: dict[str, Any],
    downloads: Path,
) -> Path:
    destination = _unique_download_path(downloads, offer["name"])
    with tempfile.TemporaryDirectory(prefix="clipsync-download-") as temporary_directory:
        archive = Path(temporary_directory) / "folder.zip"
        _stream_download(endpoint, transfer_id, device_id, archive, offer["size"], offer["sha256"])
        _safe_extract_zip(archive, destination, offer["file_count"], offer["expanded_size"])
    return destination


class UnifiedClipboardApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.device_id = _load_or_create_device_id()
        self.device_name = socket.gethostname()
        self.discovery = PeerDiscovery(args.port, args.election_delay)
        self.discovery.token = args.token or load_or_create_token(args.token_file)
        self.state = ClipboardState()
        self.backend = SystemClipboardBackend()
        self.client_state: ClientClipboardState | None = None
        self.pending_clipboard: str | None = None
        self.stop = threading.Event()
        self.server: UnifiedClipboardSyncServer | None = None
        self.server_thread: threading.Thread | None = None
        self.server_is_local = False
        self.endpoint: Endpoint | None = None
        self.last_endpoint: tuple[str, int, str] | None = None
        self.last_error = ""
        self.input = InputReader()
        self.pending: dict[str, dict[str, Any]] = {}
        self.send_lock = threading.Lock()
        self.last_offer_check = 0.0
        self.last_clipboard_check = 0.0
        self.last_endpoint_check = 0.0
        self.last_server_clipboard_check = 0.0
        self.clipboard_warning = False

    def _start_server(self) -> None:
        if self.server is not None:
            return
        try:
            with contextlib.suppress(ClipboardError):
                self.state.read_current()
            server = UnifiedClipboardSyncServer((self.args.host, self.args.port), self.discovery.token, self.state, FileTransferStore())
        except OSError as error:
            print(f"[ERRO] Não foi possível assumir o papel de servidor na porta {self.args.port}: {error}")
            return
        self.server = server
        self.server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="clipboard-http-server")
        self.server_thread.start()
        self.server_is_local = True
        print(f"[i] Este computador é o servidor ({self.device_name}).")

    def _stop_server(self) -> None:
        server = self.server
        if server is None:
            return
        self.server = None
        self.server_is_local = False
        with contextlib.suppress(Exception):
            server.shutdown()
        server.server_close()
        print("[i] Papel de servidor transferido para outro computador.")

    def _resolve_endpoint(self) -> Endpoint | None:
        is_leader, leader = self.discovery.snapshot()
        if not self.discovery.ready or leader is None:
            return None
        if is_leader:
            if not self.server_is_local:
                self._start_server()
            if self.server is None:
                return None
            return Endpoint("127.0.0.1", self.server.server_port, self.discovery.token, self.device_name)
        if time.monotonic() - self.last_endpoint_check < 1.5:
            return self.endpoint
        self.last_endpoint_check = time.monotonic()
        self._stop_server()
        host = leader[2]
        found = discover_endpoint(host, timeout=0.5)
        return found

    def _send_file(self, raw_path: str) -> None:
        if not self.send_lock.acquire(blocking=False):
            print("[aviso] Já existe um envio aguardando aceite.")
            return
        try:
            argument = raw_path.strip()
            if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in {'"', "'"}:
                argument = argument[1:-1]
            elif any(char.isspace() for char in argument):
                raise ValueError('Use: send "caminho-arquivo-ou-pasta"')
            source = Path(argument)
            upload_path, name, size, expanded, count, digest, temporary = _prepare_send_path(source)
            endpoint = self.endpoint
            if endpoint is None:
                raise ClipboardClientError("nenhum servidor está disponível neste momento")
            kind = "folder" if source.is_dir() else "file"
            created = _api_json(endpoint, "POST", "/v2/offers", {
                "device_id": self.device_id, "device_name": self.device_name, "name": name,
                "kind": kind, "size": size, "sha256": digest, "expanded_size": expanded, "file_count": count,
            })
            transfer_id = created["id"]
            print(f"[i] Oferta de {name} enviada. Aguardando aceite por até 30 segundos.")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not self.stop.is_set():
                status = _api_json(endpoint, "GET", f"/v2/offers/{transfer_id}/status", device_id=self.device_id)
                if status.get("status") == "accepted":
                    print(f"[i] {status.get('accepted_name', 'Um dispositivo')} aceitou. Enviando...")
                    _body, response = _stream_transfer(endpoint, "PUT", transfer_id, upload_path, device_id=self.device_id)
                    if response[0] != 200:
                        raise ClipboardClientError(f"Falha ao enviar arquivo (HTTP {response[0]}): {_body.decode('utf-8', errors='replace')}")
                    print(f"[+] Transferência de {name} concluída.")
                    return
                if status.get("status") in {"expired", "cancelled", "failed"}:
                    print("[i] Nenhum dispositivo aceitou o arquivo.")
                    return
                time.sleep(0.5)
            _api_json(endpoint, "POST", f"/v2/offers/{transfer_id}/cancel", {"device_id": self.device_id})
            print("[i] Nenhum dispositivo aceitou o arquivo.")
        except (OSError, ValueError, KeyError, ClipboardClientError, TransferError) as error:
            print(f"[ERRO] Não foi possível enviar o arquivo: {error}")
        finally:
            if "temporary" in locals() and temporary:
                upload_path.unlink(missing_ok=True)
            self.send_lock.release()

    def _receive(self, offer: dict[str, Any]) -> None:
        transfer_id = offer["id"]
        endpoint = self.endpoint
        if endpoint is None:
            return
        try:
            _api_json(endpoint, "POST", f"/v2/offers/{transfer_id}/accept", {
                "device_id": self.device_id, "device_name": self.device_name,
            })
            print(f"[i] Aceite registrado. Aguardando o envio de {offer['name']}...")
            while not self.stop.is_set():
                status = _api_json(endpoint, "GET", f"/v2/offers/{transfer_id}/status", device_id=self.device_id)
                if status.get("status") == "ready":
                    break
                if status.get("status") in {"failed", "cancelled", "expired"}:
                    raise ClipboardClientError("a transferência foi cancelada ou falhou")
                time.sleep(0.5)
            downloads = self.args.downloads.expanduser()
            if offer["kind"] == "folder":
                saved = _download_folder(endpoint, transfer_id, self.device_id, offer, downloads)
            else:
                saved = _unique_download_path(downloads, offer["name"])
                _stream_download(endpoint, transfer_id, self.device_id, saved, offer["size"], offer["sha256"])
            _api_json(endpoint, "POST", f"/v2/transfers/{transfer_id}/complete", {"device_id": self.device_id})
            print(f"[+] Arquivo recebido em: {saved}")
        except (OSError, KeyError, ClipboardClientError, TransferError) as error:
            print(f"[ERRO] Não foi possível receber {offer.get('name', 'o arquivo')}: {error}")

    def _check_offers(self) -> None:
        now = time.time()
        for transfer_id, offer in list(self.pending.items()):
            if offer.get("expires_at", now + 1) <= now:
                self.pending.pop(transfer_id, None)
                print(f"[i] A oferta de {offer.get('name')} expirou sem resposta.")
        if not self.endpoint or time.monotonic() - self.last_offer_check < 1.0:
            return
        self.last_offer_check = time.monotonic()
        try:
            result = _api_json(
                self.endpoint, "GET", "/v2/offers", device_id=self.device_id, device_name=self.device_name
            )
            for offer in result.get("offers", []):
                if offer.get("id") in self.pending:
                    continue
                self.pending[offer["id"]] = offer
                print(f"\nO arquivo {offer.get('name')} está pronto para ser transferido. Aceitar ? (S/n)")
        except ClipboardClientError as error:
            if "404" not in str(error) and str(error) != self.last_error:
                print(f"[aviso] Não foi possível consultar arquivos disponíveis: {error}")

    def _handle_console_line(self, line: str) -> None:
        stripped = line.strip()
        if stripped.lower().startswith("send "):
            threading.Thread(target=self._send_file, args=(stripped[5:].strip(),), daemon=True).start()
            return
        if self.pending:
            transfer_id = next(iter(self.pending))
            offer = self.pending.pop(transfer_id)
            if line.strip().lower() == "n":
                if self.endpoint:
                    with contextlib.suppress(ClipboardClientError):
                        _api_json(self.endpoint, "POST", f"/v2/offers/{transfer_id}/decline", {"device_id": self.device_id})
                print(f"[i] Transferência de {offer.get('name')} recusada.")
                return
            threading.Thread(target=self._receive, args=(offer,), daemon=True).start()
            return
        if stripped:
            print('[i] Comando não reconhecido. Use: send "caminho-arquivo-ou-pasta"')

    def _sync_clipboard(self) -> None:
        endpoint = self.endpoint
        if endpoint is None or (self.server_is_local and endpoint.host == "127.0.0.1"):
            return
        if time.monotonic() - self.last_clipboard_check < self.args.poll_interval:
            return
        self.last_clipboard_check = time.monotonic()
        try:
            if self.client_state is None:
                self.client_state = ClientClipboardState.from_backend(self.backend)
                if self.pending_clipboard is not None:
                    self.pending_clipboard = self.backend.read()
                    server_request(endpoint, "POST", self.pending_clipboard)
                    print(f"[PC cliente] Clipboard enviado ao servidor ({len(self.pending_clipboard)} caracteres).")
                    self.pending_clipboard = None
                else:
                    remote = server_request(endpoint, "GET")["text"]
                    if self.args.initial_sync == "client":
                        self.pending_clipboard = self.backend.read()
                        server_request(endpoint, "POST", self.pending_clipboard)
                        self.pending_clipboard = None
                    elif self.client_state.apply_remote(remote):
                        print("[i] Clipboard copiado do servidor para este computador.")
                self.last_endpoint = (endpoint.host, endpoint.port, endpoint.token)
                return
            current = (endpoint.host, endpoint.port, endpoint.token)
            if self.last_endpoint != current:
                self.last_endpoint = current
                self.client_state = ClientClipboardState.from_backend(self.backend)
            local = self.client_state.poll_local_change()
            if local is not None:
                self.pending_clipboard = local
            remote = server_request(endpoint, "GET")["text"]
            if self.pending_clipboard is not None:
                server_request(endpoint, "POST", self.pending_clipboard)
                print(f"[PC cliente] Clipboard enviado ao servidor ({len(self.pending_clipboard)} caracteres).")
                self.pending_clipboard = None
            elif self.client_state.apply_remote(remote):
                print(f"[PC cliente] Clipboard atualizado pelo servidor ({len(remote)} caracteres).")
            self.last_error = ""
        except (ClipboardClientError, ClipboardError) as error:
            message = str(error)
            if message != self.last_error:
                print(f"[aviso] {message}")
                self.last_error = message
            self.client_state = None

    def _monitor_server_clipboard(self) -> None:
        if not self.server_is_local or time.monotonic() - self.last_server_clipboard_check < self.args.poll_interval:
            return
        self.last_server_clipboard_check = time.monotonic()
        try:
            text, _updated_at, _source, changed = self.state.read_current()
            if changed:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
                print(f"[PC] Novo clipboard detectado (sha256 {digest}).")
            self.clipboard_warning = False
        except ClipboardError as error:
            if not self.clipboard_warning:
                print(f"[aviso] Não foi possível ler o clipboard: {error}")
                self.clipboard_warning = True

    def run(self) -> int:
        self.discovery.start()
        self.input.start()
        print("CLIPBOARD SYNC PC — inicializando descoberta automática")
        print(f"[i] ID do dispositivo: {self.device_id}")
        print('[i] Digite send "caminho-arquivo-ou-pasta" para compartilhar arquivos. Ctrl+C encerra.')
        try:
            while not self.stop.is_set():
                if self.discovery.error:
                    return 1
                was_leader = self.server_is_local
                is_leader, _leader = self.discovery.snapshot()
                if is_leader and not was_leader:
                    self._start_server()
                elif not is_leader and was_leader:
                    self._stop_server()
                old_endpoint = self.endpoint
                self.endpoint = self._resolve_endpoint()
                if (
                    self.endpoint is not None
                    and not self.server_is_local
                    and (
                        old_endpoint is None
                        or (old_endpoint.host, old_endpoint.port, old_endpoint.token)
                        != (self.endpoint.host, self.endpoint.port, self.endpoint.token)
                    )
                ):
                    print(f"[i] Cliente conectado ao servidor {self.endpoint.name or self.endpoint.host}.")
                self._monitor_server_clipboard()
                self._sync_clipboard()
                self._check_offers()
                while True:
                    try:
                        line = self.input.lines.get_nowait()
                    except queue.Empty:
                        break
                    if line == "\x03":
                        raise KeyboardInterrupt
                    self._handle_console_line(line)
                time.sleep(min(self.args.poll_interval, 0.2))
        except KeyboardInterrupt:
            print("\n[i] Encerrando Clipboard Sync...")
        finally:
            self.stop.set()
            self.input.stop.set()
            self.discovery.close()
            self._stop_server()
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sincroniza clipboard e compartilha arquivos entre PCs e Android na rede local."
    )
    parser.add_argument("--host", default="0.0.0.0", help="interface HTTP (padrão: todas)")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help=f"porta HTTP (padrão: {HTTP_PORT})")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE, help="arquivo do token legado usado pelo APK")
    parser.add_argument("--token", help="token legado manual")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="intervalo do clipboard (padrão: 0,5 s)")
    parser.add_argument("--election-delay", type=float, default=STARTUP_ELECTION_DELAY, help="espera antes da eleição (padrão: 3 s)")
    parser.add_argument("--initial-sync", choices=("server", "client"), default="server", help="clipboard inicial que prevalece")
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads", help="pasta para arquivos recebidos")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.port <= 65535 or args.poll_interval <= 0 or args.election_delay < 0:
        print("[ERRO] Porta ou intervalo inválido.")
        return 2
    if args.token:
        # Token manual não é gravado, mantendo o arquivo legado existente para os demais PCs.
        args.token = str(args.token)
    try:
        return UnifiedClipboardApp(args).run()
    except KeyboardInterrupt:
        print("\n[i] Clipboard Sync encerrado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
