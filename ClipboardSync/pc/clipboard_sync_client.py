#!/usr/bin/env python3
"""Cliente desktop para sincronizar o clipboard com o servidor Clipboard Sync."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import hashlib
import json
import socket
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pc.clipboard_sync_server import (
    ClipboardError as SystemClipboardError,
    DISCOVERY_PORT,
    DISCOVERY_REQUEST,
    MAX_TEXT_BYTES,
    read_clipboard,
    write_clipboard,
)


DEFAULT_HTTP_PORT = 8765
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_RECONNECT_DELAY = 3.0


class ClipboardError(RuntimeError):
    """Indica que o cliente não conseguiu acessar o clipboard ou o servidor."""


class AuthenticationError(ClipboardError):
    """Indica que o servidor recusou o token compartilhado."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    token: str
    name: str = ""


class SystemClipboardBackend:
    """Adapta as funções de clipboard compartilhadas com o servidor do PC."""

    def read(self) -> str:
        try:
            return read_clipboard()
        except SystemClipboardError as error:
            raise ClipboardError(str(error)) from error

    def write(self, text: str) -> None:
        try:
            write_clipboard(text)
        except SystemClipboardError as error:
            raise ClipboardError(str(error)) from error


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ClipboardState:
    """Evita reenviar ao servidor as alterações recebidas dele."""

    def __init__(self, backend: Any, observed_hash: str) -> None:
        self.backend = backend
        self.observed_hash = observed_hash

    @classmethod
    def from_backend(cls, backend: Any) -> "ClipboardState":
        return cls(backend, content_hash(backend.read()))

    def poll_local_change(self) -> str | None:
        text = self.backend.read()
        current_hash = content_hash(text)
        if current_hash == self.observed_hash:
            return None

        self.observed_hash = current_hash
        return text

    def apply_remote(self, text: str) -> bool:
        remote_hash = content_hash(text)
        if remote_hash == self.observed_hash:
            return False
        self.backend.write(text)
        self.observed_hash = remote_hash
        return True


def parse_discovery_response(payload: str, host: str) -> Endpoint | None:
    parts = payload.split("|", 3)
    if len(parts) != 4 or parts[0] != "CLIPSYNC/1":
        return None
    try:
        port = int(parts[1])
    except ValueError:
        return None
    token, name = parts[2], parts[3]
    if not host or not token or not 1 <= port <= 65535:
        return None
    return Endpoint(host=host, port=port, token=token, name=name)


def discover_server(timeout: float = 4.0) -> Endpoint:
    """Procura o servidor por broadcast UDP, como o aplicativo Android."""

    deadline = time.monotonic() + timeout
    request = DISCOVERY_REQUEST
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while time.monotonic() < deadline:
            try:
                sock.sendto(request, ("255.255.255.255", DISCOVERY_PORT))
            except OSError as error:
                raise ClipboardError(f"Não foi possível procurar o servidor na rede: {error}") from error

            receive_until = min(deadline, time.monotonic() + 0.8)
            while time.monotonic() < receive_until:
                sock.settimeout(max(0.05, receive_until - time.monotonic()))
                try:
                    payload, address = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError as error:
                    raise ClipboardError(f"Falha na descoberta do servidor: {error}") from error
                try:
                    decoded = payload.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                endpoint = parse_discovery_response(decoded, address[0])
                if endpoint is not None:
                    return endpoint

    raise ClipboardError(
        "Nenhum servidor Clipboard Sync respondeu. Confirme que os dois computadores "
        "estão na mesma rede e que o Firewall permite as portas TCP 8765 e UDP 8766. "
        "Também é possível informar o servidor com --server."
    )


def _read_token_file(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ClipboardError(f"Não foi possível ler o arquivo de token {path}: {error}") from error
    if not token:
        raise ClipboardError(f"O arquivo de token {path} está vazio.")
    return token


def resolve_endpoint(args: argparse.Namespace) -> Endpoint:
    if not args.server:
        return discover_server(args.discovery_timeout)

    token = args.token
    if args.token_file:
        token = _read_token_file(args.token_file)
    if not token:
        token = getpass.getpass("Token do Clipboard Sync: ").strip()
    if not token:
        raise ClipboardError("O token não pode ficar vazio.")
    if not 1 <= args.port <= 65535:
        raise ClipboardError("A porta deve estar entre 1 e 65535.")
    return Endpoint(args.server, args.port, token)


def server_request(
    endpoint: Endpoint,
    method: str,
    text: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    url = f"http://{endpoint.host}:{endpoint.port}/v1/clipboard"
    headers = {
        "Accept": "application/json",
        "X-Clipboard-Token": endpoint.token,
    }
    data = None
    if text is not None:
        data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise ClipboardError("O clipboard excede o limite de 2 MiB aceito pelo servidor.")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["X-Clipboard-Source"] = "desktop"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code in {401, 403}:
            raise AuthenticationError(
                "Token recusado pelo servidor. Confira o arquivo de token ou a senha informada."
            ) from error
        raise ClipboardError(f"Servidor respondeu HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ClipboardError(f"Não foi possível conectar ao servidor: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClipboardError("O servidor retornou uma resposta inválida.") from error

    if not isinstance(result, dict):
        raise ClipboardError("O servidor retornou uma resposta inválida.")
    if method == "GET" and not isinstance(result.get("text"), str):
        raise ClipboardError("O servidor não retornou um clipboard de texto válido.")
    return result


class ClipboardSyncClient:
    def __init__(
        self,
        endpoint: Endpoint,
        backend: Any,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
        initial_sync: str = "server",
    ) -> None:
        self.endpoint = endpoint
        self.backend = backend
        self.state = ClipboardState.from_backend(backend)
        self.poll_interval = poll_interval
        self.reconnect_delay = reconnect_delay
        self.initial_sync = initial_sync

    def run(self) -> int:
        print(f"Cliente desktop usando {self.endpoint.host}:{self.endpoint.port}.")
        if self.endpoint.name:
            print(f"Servidor: {self.endpoint.name}")

        last_error: str | None = None
        try:
            while True:
                try:
                    remote_text = server_request(self.endpoint, "GET")["text"]
                    break
                except AuthenticationError as error:
                    print(f"[ERRO] {error}")
                    return 1
                except ClipboardError as error:
                    message = str(error)
                    if message != last_error:
                        print(f"[aviso] {message}")
                        last_error = message
                    time.sleep(self.reconnect_delay)

            local_text = self.backend.read()
            pending_text: str | None = None
            if self.initial_sync == "server":
                if self.state.apply_remote(remote_text):
                    print("[i] Clipboard copiado do servidor para este computador.")
            elif local_text != remote_text:
                pending_text = local_text

            last_remote_text = remote_text
            print("[i] Sincronização ativa. Pressione Ctrl+C para sair.")
            while True:
                try:
                    local_change = self.state.poll_local_change()
                    if local_change is not None:
                        pending_text = local_change

                    remote_text = server_request(self.endpoint, "GET")["text"]
                    if remote_text != last_remote_text and pending_text is None:
                        self.state.apply_remote(remote_text)
                        last_remote_text = remote_text
                        print(f"[PC cliente] Clipboard atualizado pelo servidor ({len(remote_text)} caracteres).")

                    if pending_text is not None:
                        server_request(self.endpoint, "POST", pending_text)
                        last_remote_text = pending_text
                        print(f"[PC cliente] Clipboard enviado ao servidor ({len(pending_text)} caracteres).")
                        pending_text = None

                    if last_error:
                        print("[i] Conexão com o servidor restabelecida.")
                        last_error = None
                except ClipboardError as error:
                    if isinstance(error, AuthenticationError):
                        print(f"[ERRO] {error}")
                        return 1
                    message = str(error)
                    if message != last_error:
                        print(f"[aviso] {message}")
                        last_error = message
                    time.sleep(self.reconnect_delay)
                    continue

                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("\n[i] Cliente encerrado.")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sincroniza continuamente o clipboard com o PC servidor do Clipboard Sync."
    )
    parser.add_argument(
        "--server",
        help="IP ou nome do computador servidor (padrão: descoberta automática)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"porta HTTP no modo manual (padrão: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--token",
        help="token do servidor; evite usá-lo no histórico do terminal",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="caminho para uma cópia local do arquivo de token do servidor",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=4.0,
        help="tempo para procurar o servidor (padrão: 4 s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="intervalo entre verificações do clipboard (padrão: 0,5 s)",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=DEFAULT_RECONNECT_DELAY,
        help="espera após falha de conexão (padrão: 3 s)",
    )
    parser.add_argument(
        "--initial-sync",
        choices=("server", "client"),
        default="server",
        help="qual clipboard prevalece ao iniciar (padrão: server)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_interval <= 0 or args.reconnect_delay <= 0 or args.discovery_timeout <= 0:
        print("[ERRO] Os intervalos devem ser maiores que zero.")
        return 2
    try:
        endpoint = resolve_endpoint(args)
        return ClipboardSyncClient(
            endpoint,
            SystemClipboardBackend(),
            poll_interval=args.poll_interval,
            reconnect_delay=args.reconnect_delay,
            initial_sync=args.initial_sync,
        ).run()
    except ClipboardError as error:
        print(f"[ERRO] {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
