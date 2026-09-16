#!/usr/bin/env python3
"""Servidor local para sincronizar o clipboard de texto com um Android."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


APP_VERSION = "1"
HTTP_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"CLIPSYNC_DISCOVER_V1"
TOKEN_FILE = Path(__file__).resolve().with_name("clipboard_sync.token")
MAX_TEXT_BYTES = 2 * 1024 * 1024


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
        text = read_clipboard()
        with self._lock:
            changed = text != self._text
            if changed:
                self._text = text
                self._updated_at = time.time()
                self._source = "pc"
            return self._text, self._updated_at, self._source, changed

    def write_from_client(self, text: str, source: str) -> tuple[float, str]:
        write_clipboard(text)
        with self._lock:
            self._text = text
            self._updated_at = time.time()
            self._source = source
            return self._updated_at, self._source

    def write_from_phone(self, text: str) -> tuple[float, str]:
        """Mantém compatibilidade com chamadas antigas do servidor."""
        return self.write_from_client(text, "android")


class ClipboardSyncServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, state: ClipboardState) -> None:
        super().__init__(address, ClipboardRequestHandler)
        self.token = token
        self.state = state


class ClipboardRequestHandler(BaseHTTPRequestHandler):
    server: ClipboardSyncServer

    def log_message(self, format: str, *args: Any) -> None:
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


def load_or_create_token(path: Path) -> str:
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(24)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def discovery_loop(server: ClipboardSyncServer, stop: threading.Event) -> None:
    response = (
        f"CLIPSYNC/{APP_VERSION}|{server.server_port}|{server.token}|"
        f"{socket.gethostname().replace('|', '-') }"
    ).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        sock.settimeout(1.0)
        while not stop.is_set():
            try:
                data, address = sock.recvfrom(1024)
            except socket.timeout:
                continue
            if data == DISCOVERY_REQUEST:
                sock.sendto(response, address)


def clipboard_monitor(state: ClipboardState, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        try:
            _text, _updated_at, _source, changed = state.read_current()
            if changed:
                digest = hashlib.sha256(state._text.encode("utf-8")).hexdigest()[:8]
                print(f"[PC] Novo clipboard detectado (sha256 {digest}).")
        except ClipboardError as error:
            print(f"[aviso] Não foi possível ler o clipboard: {error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servidor local de sincronização de clipboard.")
    parser.add_argument("--host", default="0.0.0.0", help="endereço de escuta (padrão: todas as interfaces)")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help=f"porta HTTP (padrão: {HTTP_PORT})")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE, help="arquivo do token compartilhado")
    parser.add_argument("--token", help="define o token manualmente, sem gravá-lo no arquivo padrão")
    parser.add_argument("--poll-interval", type=float, default=0.5, help="intervalo para detectar mudanças no PC")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.port < 1 or args.port > 65535:
        print("[ERRO] A porta deve estar entre 1 e 65535.")
        return 1
    token = args.token or load_or_create_token(args.token_file)
    state = ClipboardState()
    stop = threading.Event()
    try:
        server = ClipboardSyncServer((args.host, args.port), token, state)
    except OSError as error:
        print(f"[ERRO] Não foi possível abrir a porta {args.port}: {error}")
        return 1

    discovery = threading.Thread(target=discovery_loop, args=(server, stop), daemon=True)
    monitor = threading.Thread(
        target=clipboard_monitor,
        args=(state, stop, max(0.1, args.poll_interval)),
        daemon=True,
    )
    discovery.start()
    monitor.start()
    print("CLIPBOARD SYNC PC")
    print(f"[+] Servidor HTTP: http://127.0.0.1:{args.port}")
    print(f"[+] Descoberta UDP: porta {DISCOVERY_PORT}")
    print(f"[+] Token salvo em: {args.token_file}")
    print("[i] O servidor está ouvindo. Pressione Ctrl+C para sair.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] Encerrando...")
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
