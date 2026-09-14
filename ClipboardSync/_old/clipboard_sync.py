#!/usr/bin/env python3
"""Sincronizador bidirecional de clipboard para computador e Android/Termux.

O processo no computador normalmente roda como servidor e o processo no
Android como cliente. O protocolo usa uma conexão TCP persistente com JSON
delimitado por quebras de linha; não há dependências externas.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional


PROTOCOL_VERSION = 1
DEFAULT_PORT = 8765
DISCOVERY_PORT = 8766
MAX_TEXT_CHARS = 1_000_000
MAX_MESSAGE_BYTES = 4_500_000
POLL_INTERVAL = 0.25
DISCOVERY_INTERVAL = 1.0


class ClipboardError(RuntimeError):
    """Erro ao ler ou escrever o clipboard local."""


class ClipboardBackend:
    """Interface mínima usada pela sincronização."""

    name = "desconhecido"

    def read(self) -> str:
        raise NotImplementedError

    def write(self, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class TermuxClipboardBackend(ClipboardBackend):
    name = "Termux:API"

    def __init__(self) -> None:
        self.get_command = shutil.which("termux-clipboard-get")
        self.set_command = shutil.which("termux-clipboard-set")
        if not self.get_command or not self.set_command:
            raise ClipboardError(
                "Não encontrei termux-clipboard-get/set. Instale o app Termux:API "
                "e execute: pkg install termux-api"
            )

    def _run(self, command: str, *args: str, input_text: Optional[str] = None) -> str:
        try:
            result = subprocess.run(
                [command, *args],
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            command_name = os.path.basename(command)
            raise ClipboardError(
                f"{command_name} não respondeu em 5 segundos. Teste diretamente com "
                f"'{command_name}' no Termux. Se também travar, atualize/reinstale "
                "Termux e Termux:API pela mesma fonte e abra o Termux:API uma vez."
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClipboardError(f"Falha ao acessar o clipboard pelo Termux: {exc}") from exc
        return result.stdout

    def read(self) -> str:
        return self._run(self.get_command).rstrip("\r\n")

    def write(self, text: str) -> None:
        self._run(self.set_command, input_text=text)


class TkClipboardBackend(ClipboardBackend):
    name = "Tkinter"

    def __init__(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            raise ClipboardError(
                "Tkinter não está disponível. Instale Python com Tkinter ou use "
                "--clipboard-backend termux no Android."
            ) from exc

        self._tk = tk
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.update()
        except tk.TclError as exc:
            raise ClipboardError(f"Não foi possível inicializar o clipboard gráfico: {exc}") from exc

    def read(self) -> str:
        try:
            self.root.update()
            return self.root.clipboard_get()
        except self._tk.TclError as exc:
            # Clipboard vazio ou contendo apenas um formato não textual.
            message = str(exc).lower()
            if "selection doesn't exist" in message or "clipboard" in message:
                return ""
            raise ClipboardError(f"Falha ao ler o clipboard: {exc}") from exc

    def write(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
        except self._tk.TclError as exc:
            raise ClipboardError(f"Falha ao escrever no clipboard: {exc}") from exc

    def close(self) -> None:
        try:
            self.root.destroy()
        except self._tk.TclError:
            pass


def create_backend(kind: str) -> ClipboardBackend:
    if kind == "termux":
        return TermuxClipboardBackend()
    if kind == "tkinter":
        return TkClipboardBackend()
    if shutil.which("termux-clipboard-get") and shutil.which("termux-clipboard-set"):
        return TermuxClipboardBackend()
    # No Termux, o clipboard não pode ser acessado pelo Tkinter. Detectamos o
    # ambiente antes do fallback para apresentar a correção real ao usuário.
    termux_prefix = os.environ.get("PREFIX", "")
    if os.environ.get("TERMUX_VERSION") or "com.termux" in termux_prefix:
        raise ClipboardError(
            "Android/Termux detectado, mas a Termux:API não está instalada. "
            "Instale o aplicativo Termux:API (mesma fonte do Termux), abra-o uma vez "
            "e execute no Termux: pkg install termux-api"
        )
    return TkClipboardBackend()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ClipboardError("Mensagem de clipboard excede o limite de 4,5 MB.")
    sock.sendall(encoded)


def receive_line(sock: socket.socket, timeout: float = 10.0) -> dict[str, Any]:
    """Recebe uma mensagem pequena de handshake sem deixar buffer pendente."""
    previous_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("A outra ponta encerrou a conexão.")
            data.extend(chunk)
            if len(data) > MAX_MESSAGE_BYTES:
                raise ClipboardError("Mensagem recebida é grande demais.")
    finally:
        sock.settimeout(previous_timeout)

    line = bytes(data).split(b"\n", 1)[0]
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClipboardError("A outra ponta enviou um handshake inválido.") from exc
    if not isinstance(value, dict):
        raise ClipboardError("A mensagem recebida não é um objeto JSON.")
    return value


def drain_messages(sock: socket.socket, buffer: bytearray) -> list[dict[str, Any]]:
    """Lê todas as mensagens disponíveis de um socket não bloqueante."""
    messages: list[dict[str, Any]] = []
    while True:
        try:
            chunk = sock.recv(65536)
        except BlockingIOError:
            break
        if not chunk:
            raise ConnectionError("A outra ponta encerrou a conexão.")
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise ClipboardError("Buffer de mensagens excedeu o limite permitido.")

        while b"\n" in buffer:
            raw, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ClipboardError("A outra ponta enviou uma mensagem inválida.") from exc
            if not isinstance(value, dict):
                raise ClipboardError("A mensagem recebida não é um objeto JSON.")
            messages.append(value)
    return messages


def validate_text_message(message: dict[str, Any]) -> str:
    if message.get("type") != "clipboard":
        raise ClipboardError("Tipo de mensagem desconhecido.")
    text = message.get("text")
    if not isinstance(text, str):
        raise ClipboardError("Mensagem de clipboard sem texto válido.")
    if len(text) > MAX_TEXT_CHARS:
        raise ClipboardError("O texto recebido excede o limite de 1.000.000 caracteres.")
    return text


@dataclass
class ClipboardState:
    backend: ClipboardBackend
    observed_hash: str
    suppressed_hash: Optional[str] = None

    @classmethod
    def from_backend(cls, backend: ClipboardBackend) -> "ClipboardState":
        return cls(backend=backend, observed_hash=content_hash(backend.read()))

    def poll_local_change(self) -> Optional[str]:
        text = self.backend.read()
        current_hash = content_hash(text)
        if current_hash == self.observed_hash:
            return None

        self.observed_hash = current_hash
        if current_hash == self.suppressed_hash:
            self.suppressed_hash = None
            return None
        return text

    def apply_remote(self, text: str) -> bool:
        remote_hash = content_hash(text)
        if remote_hash == self.observed_hash:
            return False
        self.backend.write(text)
        self.observed_hash = remote_hash
        self.suppressed_hash = remote_hash
        return True


def check_token(expected: str, message: dict[str, Any]) -> bool:
    received = message.get("token")
    return isinstance(received, str) and hmac.compare_digest(expected, received)


def choose_auto_role(local_id: str, peer_id: str) -> Optional[str]:
    """Escolhe o mesmo servidor nos dois dispositivos, sem configuração manual."""
    if not local_id or local_id == peer_id:
        return None
    return "server" if local_id < peer_id else "client"


def discovery_message(node_id: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "clipboard-discovery",
                "protocol": PROTOCOL_VERSION,
                "node_id": node_id,
                "port": DEFAULT_PORT,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def create_discovery_socket() -> socket.socket:
    discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    discovery.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        discovery.bind(("", DISCOVERY_PORT))
        discovery.setblocking(False)
    except OSError:
        discovery.close()
        raise ClipboardError(
            f"Não foi possível abrir a porta UDP {DISCOVERY_PORT} para descoberta automática. "
            "Feche outra instância do ClipboardSync."
        )
    return discovery


def find_auto_peer(discovery: socket.socket, node_id: str, timeout: float = 30.0) -> tuple[str, int, str]:
    """Anuncia o processo e aguarda um processo compatível na mesma rede."""
    beacon = discovery_message(node_id)
    deadline = time.monotonic() + timeout
    next_beacon = 0.0
    peers: dict[str, tuple[float, str, int]] = {}

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_beacon:
            try:
                discovery.sendto(beacon, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                # Algumas redes bloqueiam broadcast; ainda podemos ouvir um
                # anúncio enviado pelo outro dispositivo.
                pass
            next_beacon = now + DISCOVERY_INTERVAL

        try:
            while True:
                raw, address = discovery.recvfrom(4096)
                try:
                    message = json.loads(raw.decode("ascii"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                peer_id = message.get("node_id")
                peer_port = message.get("port")
                if (
                    message.get("type") == "clipboard-discovery"
                    and message.get("protocol") == PROTOCOL_VERSION
                    and isinstance(peer_id, str)
                    and len(peer_id) == 16
                    and peer_id != node_id
                    and isinstance(peer_port, int)
                    and 1 <= peer_port <= 65535
                ):
                    peers[peer_id] = (now, address[0], peer_port)
        except BlockingIOError:
            pass

        recent = [
            (peer_id, values)
            for peer_id, values in peers.items()
            if now - values[0] <= DISCOVERY_INTERVAL * 3
        ]
        if recent:
            peer_id, (_, peer_ip, peer_port) = min(recent, key=lambda item: item[0])
            return peer_ip, peer_port, peer_id
        time.sleep(0.1)

    raise ClipboardError(
        "Nenhum outro ClipboardSync foi encontrado na rede local. "
        "Verifique se os dois dispositivos estão no mesmo Wi-Fi e se o isolamento "
        "de clientes/AP está desativado."
    )


def create_auto_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("0.0.0.0", DEFAULT_PORT))
        listener.listen(1)
        listener.settimeout(0.5)
    except OSError as exc:
        listener.close()
        raise ClipboardError(
            f"Não foi possível abrir a porta TCP {DEFAULT_PORT}. "
            "Verifique o Firewall ou se outra instância já está usando a porta."
        ) from exc
    return listener


def auto_hello(node_id: str) -> dict[str, Any]:
    return {"type": "hello", "protocol": PROTOCOL_VERSION, "node_id": node_id}


def validate_auto_hello(message: dict[str, Any], expected_peer_id: str) -> None:
    if (
        message.get("type") != "hello"
        or message.get("protocol") != PROTOCOL_VERSION
        or message.get("node_id") != expected_peer_id
    ):
        raise ClipboardError("O outro dispositivo não pertence à sessão automática esperada.")


def run_auto(args: argparse.Namespace) -> int:
    """Modo padrão: descobre o par e elege automaticamente o servidor."""
    node_id = secrets.token_hex(8)
    backend = create_backend("auto")
    while True:
        try:
            state = ClipboardState.from_backend(backend)
            break
        except ClipboardError as exc:
            print_error(str(exc))
            print("O ClipboardSync continuará tentando acessar o clipboard local...")
            time.sleep(5.0)
    discovery = create_discovery_socket()
    listener = create_auto_listener()
    print(f"ClipboardSync automático ativo ({backend.name}).")
    print("Procurando o outro dispositivo na rede local...")

    try:
        while True:
            try:
                peer_ip, peer_port, peer_id = find_auto_peer(discovery, node_id)
            except ClipboardError as exc:
                print_error(str(exc))
                print("Continuando a procurar; inicie o ClipboardSync no outro dispositivo.")
                time.sleep(3.0)
                continue
            role = choose_auto_role(node_id, peer_id)
            if role is None:
                continue

            if role == "server":
                print(f"Par encontrado ({peer_ip}). Este dispositivo será o servidor.")
                _accept_auto_session(listener, peer_id, backend, state)
            else:
                print(f"Par encontrado ({peer_ip}). Este dispositivo será o cliente.")
                try:
                    _connect_auto_session(peer_ip, peer_port, node_id, peer_id, backend, state)
                except (ClipboardError, ConnectionError, OSError) as exc:
                    print_error(f"conexão automática: {exc}")
            print("Conexão encerrada; procurando novamente...")
    except KeyboardInterrupt:
        print("\nClipboardSync encerrado.")
        return 0
    finally:
        discovery.close()
        listener.close()
        backend.close()


def _accept_auto_session(
    listener: socket.socket,
    expected_peer_id: str,
    backend: ClipboardBackend,
    state: ClipboardState,
) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            conn, address = listener.accept()
        except socket.timeout:
            continue
        try:
            hello = receive_line(conn)
            validate_auto_hello(hello, expected_peer_id)
            send_json(conn, {"type": "hello_ack", "protocol": PROTOCOL_VERSION})
            conn.setblocking(False)
            send_json(conn, {"type": "clipboard", "seq": 0, "text": backend.read()})
            print(f"Conectado automaticamente a {address[0]}.")
            _host_session(conn, backend, state)
            return
        except (ClipboardError, ConnectionError, OSError) as exc:
            print_error(f"tentativa automática recusada: {exc}")
        finally:
            conn.close()


def _connect_auto_session(
    peer_ip: str,
    peer_port: int,
    node_id: str,
    expected_server_id: str,
    backend: ClipboardBackend,
    state: ClipboardState,
) -> None:
    if choose_auto_role(node_id, expected_server_id) != "client":
        raise ClipboardError("Eleição automática inconsistente.")
    with socket.create_connection((peer_ip, peer_port), timeout=5) as conn:
        send_json(conn, auto_hello(node_id))
        ack = receive_line(conn)
        if ack.get("type") != "hello_ack" or ack.get("protocol") != PROTOCOL_VERSION:
            raise ClipboardError("Resposta inválida do servidor automático.")
        conn.setblocking(False)
        buffer = bytearray()
        next_poll = 0.0
        print("Conectado automaticamente. Clipboard inicial do servidor prevalece.")

        while True:
            readable, _, _ = select.select([conn], [], [], 0.1)
            if readable:
                for message in drain_messages(conn, buffer):
                    text = validate_text_message(message)
                    if state.apply_remote(text):
                        print("Clipboard recebido do computador e aplicado no Android.")

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + POLL_INTERVAL
                local_text = state.poll_local_change()
                if local_text is not None:
                    send_json(conn, {"type": "clipboard", "text": local_text})
                    print("Clipboard local enviado ao servidor.")


def print_error(message: str) -> None:
    print(f"Erro: {message}", file=sys.stderr)


def run_host(args: argparse.Namespace) -> int:
    token = args.token or secrets.token_urlsafe(18)
    backend = create_backend(args.clipboard_backend)
    state = ClipboardState.from_backend(backend)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.port))
    listener.listen(1)

    print(f"Servidor ativo em {args.bind}:{args.port} usando {backend.name}.")
    print(f"Token para o Android: {token}")
    print("Aguardando conexão. Pressione Ctrl+C para sair.")

    try:
        while True:
            conn, address = listener.accept()
            try:
                conn.settimeout(10)
                hello = receive_line(conn)
                if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
                    raise ClipboardError("Protocolo incompatível.")
                if not check_token(token, hello):
                    raise ClipboardError("Token inválido.")
                send_json(conn, {"type": "hello_ack", "protocol": PROTOCOL_VERSION})
                conn.setblocking(False)
                current_text = backend.read()
                send_json(conn, {"type": "clipboard", "seq": 0, "text": current_text})
                print(f"Conectado: {address[0]}:{address[1]}. Sincronização iniciada.")
                _host_session(conn, backend, state)
            except (ClipboardError, ConnectionError, OSError) as exc:
                print_error(f"conexão encerrada: {exc}")
            finally:
                conn.close()
                print("Aguardando reconexão...")
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
        return 0
    finally:
        listener.close()
        backend.close()


def _host_session(conn: socket.socket, backend: ClipboardBackend, state: ClipboardState) -> None:
    buffer = bytearray()
    next_poll = 0.0
    sequence = 0
    while True:
        readable, _, _ = select.select([conn], [], [], 0.1)
        if readable:
            for message in drain_messages(conn, buffer):
                text = validate_text_message(message)
                if state.apply_remote(text):
                    sequence += 1
                    send_json(conn, {"type": "clipboard", "seq": sequence, "text": text})
                    print("Clipboard recebido do Android e enviado ao computador.")

        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL_INTERVAL
            local_text = state.poll_local_change()
            if local_text is not None:
                sequence += 1
                send_json(conn, {"type": "clipboard", "seq": sequence, "text": local_text})
                print("Clipboard do computador enviado ao Android.")


def run_client(args: argparse.Namespace) -> int:
    backend = create_backend(args.clipboard_backend)
    state = ClipboardState.from_backend(backend)
    print(f"Cliente usando {backend.name}; destino {args.server}:{args.port}.")
    print("Pressione Ctrl+C para sair.")

    try:
        while True:
            try:
                _client_session(args, backend, state)
            except (ClipboardError, ConnectionError, OSError) as exc:
                print_error(f"conexão: {exc}")
                print(f"Tentando novamente em {args.reconnect_delay:g} s...")
                time.sleep(args.reconnect_delay)
    except KeyboardInterrupt:
        print("\nCliente encerrado.")
        return 0
    finally:
        backend.close()


def _client_session(args: argparse.Namespace, backend: ClipboardBackend, state: ClipboardState) -> None:
    with socket.create_connection((args.server, args.port), timeout=10) as conn:
        send_json(conn, {"type": "hello", "protocol": PROTOCOL_VERSION, "token": args.token})
        ack = receive_line(conn)
        if ack.get("type") != "hello_ack" or ack.get("protocol") != PROTOCOL_VERSION:
            raise ClipboardError("Servidor recusou o protocolo ou enviou um ACK inválido.")
        conn.setblocking(False)
        buffer = bytearray()
        next_poll = 0.0
        print("Conectado. O clipboard inicial do computador prevalece nesta conexão.")

        while True:
            readable, _, _ = select.select([conn], [], [], 0.1)
            if readable:
                for message in drain_messages(conn, buffer):
                    text = validate_text_message(message)
                    if state.apply_remote(text):
                        print("Clipboard recebido do computador e aplicado no Android.")

            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + POLL_INTERVAL
                local_text = state.poll_local_change()
                if local_text is not None:
                    send_json(conn, {"type": "clipboard", "text": local_text})
                    print("Clipboard do Android enviado ao computador.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sincroniza clipboard de texto bidirecionalmente entre computador e Android."
    )
    subparsers = parser.add_subparsers(dest="mode")

    subparsers.add_parser("auto", help="descobre automaticamente o outro dispositivo (padrão)")

    host = subparsers.add_parser("host", help="inicia o servidor, normalmente no computador")
    host.add_argument("--bind", default="0.0.0.0", help="endereço local para escutar (padrão: 0.0.0.0)")
    host.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"porta TCP (padrão: {DEFAULT_PORT})")
    host.add_argument("--token", help="token compartilhado; se omitido, um token aleatório será gerado")
    host.add_argument(
        "--clipboard-backend",
        choices=("auto", "tkinter", "termux"),
        default="auto",
        help="backend local do clipboard (padrão: auto)",
    )

    client = subparsers.add_parser("client", help="conecta ao servidor, normalmente no Android/Termux")
    client.add_argument("--server", required=True, help="IP ou nome do computador servidor")
    client.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"porta TCP (padrão: {DEFAULT_PORT})")
    client.add_argument("--token", required=True, help="mesmo token exibido pelo servidor")
    client.add_argument(
        "--clipboard-backend",
        choices=("auto", "tkinter", "termux"),
        default="auto",
        help="backend local do clipboard (padrão: auto)",
    )
    client.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="segundos entre tentativas de reconexão (padrão: 3)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode in (None, "auto"):
            return run_auto(args)
        if args.mode == "host":
            return run_host(args)
        return run_client(args)
    except ClipboardError as exc:
        print_error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
