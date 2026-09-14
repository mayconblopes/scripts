#!/usr/bin/env python3
"""
Site Scanner 2.5.1
Crawler público com requests + fallback Chromium/CDP.

Dependências:
    pip install requests beautifulsoup4 websocket-client lxml

Termux:
    pkg install x11-repo chromium

Uso:
    python scanner2.py
    python scanner2.py https://example.com/
"""

import gzip
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import argparse
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse, urlunparse

import requests
import websocket
from bs4 import BeautifulSoup

VERSION = "2.5.1"
TIMEOUT = 20
REQUEST_DELAY = 0.15
MAX_PAGES = 1500
MAX_DEPTH = 20
MAX_HTML_BYTES = 15 * 1024 * 1024
MAX_RESOURCE_INSPECTIONS = None
DOWNLOAD_DIR = Path("downloads")

CHROMIUM_PORT = 9222
CHROMIUM_START_TIMEOUT = 45
BROWSER_PAGE_TIMEOUT = 30
BROWSER_POLL_INTERVAL = 0.5
CHALLENGE_TIMEOUT = 30
CHROMIUM_PROFILE = Path.home() / ".config" / "scanner-chromium"

# Dynamic Page Engine.  The browser remains ordinary Chromium: no stealth
# flags, token replay, credential storage, or anti-bot bypass is attempted.
DYNAMIC_SCROLL_PRESETS = {
    "rapido": 10,
    "rápido": 10,
    "quick": 10,
    "medio": 30,
    "médio": 30,
    "medium": 30,
    "profundo": 100,
    "deep": 100,
}
DYNAMIC_DEFAULT_LEVEL = "medio"
DYNAMIC_IDLE_CYCLES = 3
DYNAMIC_SCROLL_PAUSE = 1.0
DYNAMIC_MIN_DOM_GROWTH = 256
FACEBOOK_ROOT = "facebook.com"
NETWORK_RESOURCE_TYPES = {
    "Image", "Media", "Font", "Stylesheet", "Script", "XHR", "Fetch",
    "WebSocket", "Other", "SignedExchange", "Ping", "Preflight",
}
NETWORK_TELEMETRY_MARKERS = (
    "analytics", "telemetry", "tracking", "track", "beacon", "collect",
    "pixel", "doubleclick", "googletagmanager", "google-analytics",
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
RESOURCE_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": BROWSER_HEADERS["Accept-Language"],
}

EXTENSION_CATEGORIES = {
    ".pdf": "documentos", ".doc": "documentos", ".docx": "documentos",
    ".odt": "documentos", ".rtf": "documentos", ".txt": "documentos",
    ".md": "documentos", ".epub": "documentos",
    ".xls": "planilhas", ".xlsx": "planilhas", ".ods": "planilhas", ".csv": "planilhas",
    ".ppt": "apresentacoes", ".pptx": "apresentacoes", ".odp": "apresentacoes",
    ".zip": "compactados", ".rar": "compactados", ".7z": "compactados",
    ".tar": "compactados", ".gz": "compactados", ".bz2": "compactados",
    ".xz": "compactados",
    ".jpg": "imagens", ".jpeg": "imagens", ".png": "imagens", ".gif": "imagens",
    ".webp": "imagens", ".svg": "imagens", ".bmp": "imagens", ".ico": "imagens",
    ".tif": "imagens", ".tiff": "imagens", ".avif": "imagens",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio",
    ".aac": "audio", ".m4a": "audio", ".opus": "audio",
    ".mp4": "video", ".mkv": "video", ".avi": "video", ".mov": "video",
    ".webm": "video", ".m4v": "video",
    ".json": "dados", ".xml": "dados", ".yaml": "dados", ".yml": "dados",
    ".css": "web", ".js": "web", ".mjs": "web", ".map": "web",
    ".ttf": "fontes", ".otf": "fontes", ".woff": "fontes", ".woff2": "fontes",
    ".apk": "programas", ".exe": "programas", ".msi": "programas",
    ".bin": "programas", ".iso": "programas",
}
KNOWN_FILE_EXTENSIONS = set(EXTENSION_CATEGORIES)
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def human_size(size):
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def normalize_url(url):
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    url, _ = urldefrag(url)
    try:
        p = urlparse(url)
    except Exception:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return None
    p = p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), fragment="")
    return urlunparse(p)


def clean_input_url(value):
    """Accept plain URLs and URLs copied from Markdown/HTML text."""
    value = html.unescape(str(value or "")).strip()
    markdown = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)]+)\)", value)
    if markdown:
        value = markdown.group(2)
    elif value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    elif value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value.strip(" `\"'")


def hostname(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_facebook_hostname(value):
    host = (value or "").lower().rstrip(".")
    return host == FACEBOOK_ROOT or host.endswith("." + FACEBOOK_ROOT)


def logical_hostname(value):
    host = (value or "").lower().rstrip(".")
    return FACEBOOK_ROOT if is_facebook_hostname(host) else host


def same_host(url, host):
    # Facebook uses several first-party subdomains.  Treat them as one
    # logical target, while keeping every non-Facebook domain external.
    return logical_hostname(hostname(url)) == logical_hostname(host)


def dynamic_level_to_cycles(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in DYNAMIC_SCROLL_PRESETS:
        return DYNAMIC_SCROLL_PRESETS[text]
    try:
        cycles = int(text)
    except (TypeError, ValueError):
        return None
    return max(0, min(cycles, 1000))


def is_obvious_telemetry(url):
    """Reject only generic, clearly telemetry-like network URLs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    haystack = f"{host}{path}"
    if any(marker in haystack for marker in NETWORK_TELEMETRY_MARKERS):
        return not Path(path).suffix and any(
            marker in path or marker in query
            for marker in ("collect", "beacon", "pixel", "track", "event", "telemetry")
        )
    return any(marker in path for marker in ("/collect", "/beacon", "/pixel"))


def looks_like_dynamic_document(url, html_text):
    if is_facebook_hostname(hostname(url)):
        return True
    low = (html_text or "").lower()
    markers = (
        "__next_data__", "data-reactroot", "ng-version", "webpackjsonp",
        "infinite-scroll", "lazyload", "intersectionobserver", "vue.",
    )
    return sum(marker in low for marker in markers) >= 2


def get_extension(url):
    try:
        return Path(unquote(urlparse(url).path)).suffix.lower()
    except Exception:
        return ""


def clean_content_type(value):
    return value.split(";", 1)[0].strip().lower() if value else ""


def is_probably_html(content_type):
    return clean_content_type(content_type) in HTML_CONTENT_TYPES


def looks_like_html(data):
    if not data:
        return False
    sample = data[:4096].lstrip().lower()
    return (
        sample.startswith(b"<!doctype html")
        or sample.startswith(b"<html")
        or b"<html" in sample[:1000]
        or b"<head" in sample[:1000]
        or b"<body" in sample[:1000]
    )


def content_length(response):
    try:
        value = response.headers.get("Content-Length")
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def sanitize_filename(filename):
    if not filename:
        return "arquivo"
    filename = unquote(filename).strip()
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ")
    return filename or "arquivo"


def filename_from_url(url):
    try:
        name = Path(unquote(urlparse(url).path)).name
    except Exception:
        name = ""
    return sanitize_filename(name or "arquivo")


def filename_from_content_disposition(value):
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", value, re.I)
    if m:
        return sanitize_filename(unquote(m.group(1).strip().strip('"')))
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if m:
        return sanitize_filename(m.group(1))
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        return sanitize_filename(m.group(1).strip().strip('"'))
    return None


def unique_path(path):
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def detect_cloudflare_challenge(status, body):
    if not body:
        return False
    text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
    low = text.lower()

    strong = (
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge-platform/",
        "window._cf_chl_opt",
        "_cf_chl_opt",
        "__cf_chl_",
        "cf_chl_",
        "cf-turnstile",
        "challenge-platform",
    )
    if any(x in low for x in strong):
        return True

    titles = (
        "<title>just a moment",
        "<title>attention required",
        "enable javascript and cookies to continue",
        "checking your browser",
        "verify you are human",
        "performing security verification",
    )
    if any(x in low for x in titles):
        return True

    return "cloudflare" in low and ("challenge" in low or "turnstile" in low)


def should_use_browser_fallback(status, content_type, body):
    if detect_cloudflare_challenge(status, body):
        return True, "cloudflare"
    html = clean_content_type(content_type) in HTML_CONTENT_TYPES or looks_like_html(body)
    if status == 202 and html:
        return True, "html-202"
    if html and not body:
        return True, "html-vazio"
    return False, None


def classify_resource(url, content_type="", filename=None):
    ext = Path(filename).suffix.lower() if filename else ""
    ext = ext or get_extension(url)
    if ext in EXTENSION_CATEGORIES:
        return EXTENSION_CATEGORIES[ext]
    ct = clean_content_type(content_type)
    if ct in HTML_CONTENT_TYPES:
        return "paginas"
    if ct == "application/pdf":
        return "documentos"
    if ct.startswith("image/"):
        return "imagens"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("font/"):
        return "fontes"
    if "spreadsheet" in ct or "excel" in ct or ct == "text/csv":
        return "planilhas"
    if "wordprocessingml" in ct or "msword" in ct or "opendocument.text" in ct:
        return "documentos"
    if "presentation" in ct or "powerpoint" in ct:
        return "apresentacoes"
    if any(x in ct for x in ("zip", "rar", "7z", "compressed", "archive", "gzip")):
        return "compactados"
    if "json" in ct or "xml" in ct:
        return "dados"
    if ct == "text/css" or "javascript" in ct:
        return "web"
    if ct == "application/octet-stream" or ct.startswith("application/x-"):
        return "outros"
    if ct.startswith("text/"):
        return "documentos"
    return "outros"


class ChromiumEngine:
    def __init__(self, port=CHROMIUM_PORT):
        self.port = port
        self.process = None
        self.started_by_us = False
        self.browser_path = None
        self.ws = None
        self.message_id = 0
        self.log_handle = None
        self.log_path = CHROMIUM_PROFILE / "chromium-cdp.log"
        self.network_requests = {}
        self.network_responses = {}
        self.network_urls = set()

    def reset_network_events(self):
        self.network_requests.clear()
        self.network_responses.clear()
        self.network_urls.clear()
        self._last_dynamic_network_count = 0

    def _record_event(self, msg):
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            url = normalize_url(request.get("url", ""))
            if url:
                request_id = params.get("requestId", url)
                self.network_requests[request_id] = {
                    "url": url,
                    "resource_type": params.get("type", ""),
                    "method": request.get("method", "GET"),
                }
                self.network_urls.add(url)
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            url = normalize_url(response.get("url", ""))
            if url:
                request_id = params.get("requestId", url)
                self.network_responses[request_id] = {
                    "url": url,
                    "resource_type": params.get("type", ""),
                    "status": response.get("status"),
                    "content_type": response.get("mimeType", ""),
                }
                self.network_urls.add(url)

    def find_browser(self):
        candidates = ["chromium-browser", "chromium", "google-chrome", "google-chrome-stable", "chrome"]
        if os.name == "nt":
            candidates += [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        for candidate in candidates:
            if os.path.isabs(candidate):
                if os.path.exists(candidate):
                    return candidate
            else:
                path = shutil.which(candidate)
                if path:
                    return path
        return None

    def is_running(self):
        return port_open("127.0.0.1", self.port)

    def get_json(self, endpoint):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{endpoint}", timeout=5
        ) as response:
            return json.load(response)

    def start(self):
        if self.is_running():
            print("       [>] Chromium/CDP já está disponível.")
            try:
                self.connect()
                return True
            except Exception as exc:
                print(f"       [!] A porta {self.port} não respondeu como CDP: {exc}")
                return False

        self.browser_path = self.find_browser()
        if not self.browser_path:
            print("       [!] Chromium não encontrado.")
            return False

        CHROMIUM_PROFILE.mkdir(parents=True, exist_ok=True)
        args = [
            self.browser_path,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.port}",
            f"--remote-allow-origins=http://127.0.0.1:{self.port}",
            f"--user-data-dir={CHROMIUM_PROFILE}",
            "about:blank",
        ]
        print("       [>] Iniciando Chromium...")
        try:
            self.log_handle = open(self.log_path, "ab")
            self.process = subprocess.Popen(
                args, stdout=self.log_handle, stderr=subprocess.STDOUT
            )
        except Exception as exc:
            print(f"       [!] Falha ao iniciar Chromium: {exc}")
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            return False

        self.started_by_us = True
        deadline = time.time() + CHROMIUM_START_TIMEOUT
        last_connect_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                print(f"       [!] Chromium encerrou durante a inicialização (código {self.process.returncode}).")
                self.print_start_diagnostics()
                return False
            if self.is_running():
                try:
                    self.connect()
                    print("       [✓] Chromium iniciado.")
                    return True
                except Exception as exc:
                    last_connect_error = exc
            time.sleep(0.25)
        print("       [!] Chromium não abriu o CDP a tempo.")
        if last_connect_error:
            print(f"       [i] Último erro de conexão CDP: {last_connect_error}")
        self.print_start_diagnostics()
        return False

    def print_start_diagnostics(self):
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            text = ""
        if text.strip():
            print(f"       [i] Log do Chromium: {self.log_path}")
            print("       [i] Últimas mensagens do Chromium:")
            for line in text.splitlines()[-12:]:
                print(f"           {line}")
        else:
            print(f"       [i] Nenhum diagnóstico foi gravado em: {self.log_path}")

    def connect(self):
        try:
            pages = self.get_json("/json")
        except Exception:
            pages = self.get_json("/json/list")
        page = next((x for x in pages if x.get("type") == "page"), None)
        if not page:
            raise RuntimeError("Nenhuma página CDP encontrada.")
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        try:
            # Chrome pode rejeitar o Origin padrão do websocket-client mesmo
            # com o DevTools HTTP endpoint disponível. O CDP local não precisa
            # desse cabeçalho de origem.
            self.ws = websocket.create_connection(
                page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
            )
        except TypeError:
            self.ws = websocket.create_connection(
                page["webSocketDebuggerUrl"],
                timeout=10,
                origin=f"http://127.0.0.1:{self.port}",
            )
        self.command("Page.enable")
        self.command("Runtime.enable")
        self.command("Network.enable")

    def command(self, method, params=None):
        if not self.ws:
            raise RuntimeError("CDP não conectado.")
        self.message_id += 1
        mid = self.message_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method"):
                self._record_event(msg)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg

    def evaluate(self, expression):
        result = self.command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("result", {}).get("value")

    def page_info(self):
        value = self.evaluate(r"""
        JSON.stringify({
          url: location.href,
          title: document.title,
          readyState: document.readyState,
          htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0,
          elementCount: document.querySelectorAll ? document.querySelectorAll('*').length : 0,
          scrollHeight: document.documentElement ? document.documentElement.scrollHeight : 0,
          html: document.documentElement ? document.documentElement.outerHTML : ""
        })
        """)
        return json.loads(value) if value else {
            "url": "", "title": "", "readyState": "", "htmlLength": 0,
            "elementCount": 0, "scrollHeight": 0, "html": ""
        }

    def browser_has_challenge(self, info):
        title = info.get("title", "").lower()
        html = info.get("html", "").lower()
        return (
            "just a moment" in title
            or "challenges.cloudflare.com" in html
            or "/cdn-cgi/challenge-platform/" in html
            or "_cf_chl_opt" in html
        )

    def navigate(self, url):
        if not self.is_running() and not self.start():
            return None
        if not self.ws:
            self.connect()

        self.reset_network_events()
        print(f"       [>] Chromium: {url}")
        try:
            self.command("Page.navigate", {"url": url})
        except Exception as exc:
            print(f"       [!] Erro CDP: {exc}")
            return None

        deadline = time.time() + BROWSER_PAGE_TIMEOUT
        last = None
        challenge_seen = False

        while time.time() < deadline:
            time.sleep(BROWSER_POLL_INTERVAL)
            try:
                info = self.page_info()
            except Exception:
                continue
            last = info
            if self.browser_has_challenge(info):
                if not challenge_seen:
                    print("       [>] Challenge em execução...")
                    challenge_seen = True
                continue
            if info.get("readyState") == "complete" and info.get("htmlLength", 0) > 0:
                print("       [✓] DOM renderizado.")
                return info

        if challenge_seen:
            deadline = time.time() + CHALLENGE_TIMEOUT
            while time.time() < deadline:
                time.sleep(BROWSER_POLL_INTERVAL)
                try:
                    info = self.page_info()
                except Exception:
                    continue
                last = info
                if (
                    not self.browser_has_challenge(info)
                    and info.get("readyState") == "complete"
                    and info.get("htmlLength", 0) > 0
                ):
                    print("       [✓] Challenge concluído.")
                    return info

        return last

    def dynamic_scroll(self, cycles, idle_cycles=DYNAMIC_IDLE_CYCLES):
        """Scroll progressively and stop after repeated cycles without growth."""
        if cycles <= 0:
            return {"cycles": 0, "idle_cycles": 0, "stopped_early": False}

        previous = self.page_info()
        idle = 0
        executed = 0
        stopped_early = False
        for cycle in range(1, cycles + 1):
            try:
                self.evaluate(r"""
                (() => {
                  const height = Math.max(
                    document.documentElement ? document.documentElement.scrollHeight : 0,
                    document.body ? document.body.scrollHeight : 0
                  );
                  const step = Math.max(window.innerHeight * 0.85, 400);
                  const target = Math.min(height, window.scrollY + step);
                  window.scrollTo({top: target, behavior: 'auto'});
                  return target;
                })()
                """)
            except Exception:
                break

            time.sleep(DYNAMIC_SCROLL_PAUSE)
            try:
                current = self.page_info()
            except Exception:
                break
            executed = cycle
            html_growth = current.get("htmlLength", 0) - previous.get("htmlLength", 0)
            element_growth = current.get("elementCount", 0) - previous.get("elementCount", 0)
            new_urls = len(self.network_urls)
            old_urls = getattr(self, "_last_dynamic_network_count", 0)
            network_growth = new_urls - old_urls
            self._last_dynamic_network_count = new_urls
            relevant_growth = (
                html_growth >= DYNAMIC_MIN_DOM_GROWTH
                or element_growth > 0
                or network_growth > 0
            )
            idle = 0 if relevant_growth else idle + 1
            previous = current
            if idle >= idle_cycles:
                stopped_early = True
                break

        return {
            "cycles": executed,
            "idle_cycles": idle,
            "stopped_early": stopped_early,
        }

    def get_cookies(self):
        for method in ("Network.getAllCookies", "Storage.getCookies"):
            try:
                result = self.command(method)
                return result.get("result", {}).get("cookies", [])
            except Exception:
                pass
        return []

    def sync_cookies_to_requests(self, session):
        imported = 0
        for cookie in self.get_cookies():
            name, value = cookie.get("name"), cookie.get("value")
            if not name:
                continue
            try:
                session.cookies.set(
                    name, value,
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
                imported += 1
            except Exception:
                pass
        return imported

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.process and self.started_by_us:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None


class SiteScanner:
    def __init__(self, start_url, dynamic_cycles=None, dynamic_disabled=False):
        start_url = normalize_url(clean_input_url(start_url))
        if not start_url:
            raise ValueError("URL inválida.")
        self.start_url = start_url
        self.host = hostname(start_url)
        self.logical_host = logical_hostname(self.host)
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.browser = ChromiumEngine()

        self.queue = deque()
        self.queued_pages = set()
        self.visited_pages = set()
        self.failed_pages = set()

        self.resources = {}
        self.resource_queue = deque()
        self.queued_resources = set()
        self.inspected_resources = set()

        self.external_links = set()
        self.external_resources = set()
        self.errors = []
        self.anomalies = []
        self.sitemaps_seen = set()

        self.requests_count = 0
        self.browser_pages = 0
        self.cloudflare_challenges = 0
        self.dynamic_disabled = dynamic_disabled
        self.dynamic_cycles = dynamic_level_to_cycles(dynamic_cycles)
        self.dynamic_level = self.dynamic_cycles
        self.dynamic_pages = 0
        self.dynamic_page_urls = set()
        self.dynamic_scroll_cycles = 0
        self.dynamic_network_urls = set()
        self.dynamic_network_requests = 0
        self.dynamic_network_responses = 0
        self.login_required_pages = set()

    @property
    def is_known_dynamic_domain(self):
        return is_facebook_hostname(self.host)

    def configure_dynamic_mode(self, value=None, prompt=False):
        if self.dynamic_disabled:
            self.dynamic_cycles = 0
            self.dynamic_level = 0
            return 0
        cycles = dynamic_level_to_cycles(value)
        if cycles is None and prompt:
            print(
                "\n[+] Página/rede social dinâmica detectada: Facebook.\n"
                "    O Chromium usará apenas a sessão já existente do perfil persistente.\n"
                "    Escolha a profundidade do scroll:"
            )
            print("    [1] Rápido   (10 ciclos)")
            print("    [2] Médio    (30 ciclos)")
            print("    [3] Profundo (100 ciclos)")
            print("    [4] Personalizado")
            try:
                choice = input("    Opção [2]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = ""
            mapping = {"1": "rapido", "2": "medio", "3": "profundo"}
            if choice == "4":
                try:
                    choice = input("    Número de ciclos [30]: ").strip() or "30"
                except (EOFError, KeyboardInterrupt):
                    choice = "30"
            cycles = dynamic_level_to_cycles(mapping.get(choice, "medio"))
        if cycles is None:
            cycles = 0 if not self.is_known_dynamic_domain else DYNAMIC_SCROLL_PRESETS[DYNAMIC_DEFAULT_LEVEL]
        self.dynamic_cycles = cycles
        self.dynamic_level = cycles
        if cycles:
            print(f"    [+] Scroll dinâmico configurado: {cycles} ciclo(s).")
        return cycles

    def dynamic_enabled_for(self, url):
        return bool(self.dynamic_cycles and (self.is_known_dynamic_domain or self.dynamic_cycles > 0))

    def request(self, method, url, **kwargs):
        try:
            self.requests_count += 1
            r = self.session.request(
                method, url, timeout=TIMEOUT, allow_redirects=True, **kwargs
            )
            if REQUEST_DELAY:
                time.sleep(REQUEST_DELAY)
            return r
        except requests.RequestException as exc:
            self.errors.append({"url": url, "method": method, "error": str(exc)})
            return None

    def record_anomaly(self, url, status, message, content_type="", size=None):
        self.anomalies.append({
            "url": url, "status": status, "message": message,
            "content_type": clean_content_type(content_type), "size": size,
        })

    def enqueue_page(self, url, depth):
        url = normalize_url(url)
        if not url:
            return
        if not same_host(url, self.host):
            self.external_links.add(url)
            return
        if depth > MAX_DEPTH or url in self.visited_pages or url in self.queued_pages:
            return
        self.queued_pages.add(url)
        self.queue.append((url, depth))

    def queue_resource(self, url, source, external=False):
        url = normalize_url(url)
        if not url or url in self.queued_resources or url in self.inspected_resources:
            return
        self.queued_resources.add(url)
        self.resource_queue.append({"url": url, "source": source, "external": external})

    def add_resource(
        self, url, source, content_type="", size=None, filename=None,
        status=None, external=False, downloadable=True
    ):
        url = normalize_url(url)
        if not url:
            return
        filename = filename or filename_from_url(url)
        category = classify_resource(url, content_type, filename)
        if category == "paginas":
            return
        data = {
            "url": url, "filename": filename, "category": category,
            "content_type": clean_content_type(content_type), "size": size,
            "source": source, "status": status, "external": external,
            "downloadable": downloadable,
        }
        old = self.resources.get(url)
        if old:
            if not old.get("size") and size:
                old["size"] = size
            if old.get("category") == "outros" and category != "outros":
                old["category"] = category
            return
        self.resources[url] = data
        if external:
            self.external_resources.add(url)

    def inspect_resource(self, item):
        url = item["url"]
        if url in self.inspected_resources:
            return
        self.inspected_resources.add(url)

        r = self.request("HEAD", url, headers=RESOURCE_HEADERS)
        use_get = r is None or (r is not None and r.status_code in (400, 405, 500, 501))
        if r is not None and use_get:
            r.close()
        if use_get:
            r = self.request(
                "GET", url,
                headers={**RESOURCE_HEADERS, "Range": "bytes=0-8191"},
                stream=True,
            )
        if r is None:
            return

        final = normalize_url(r.url) or url
        status = r.status_code
        ct = r.headers.get("Content-Type", "")
        disposition = r.headers.get("Content-Disposition", "")
        filename = filename_from_content_disposition(disposition) or filename_from_url(final)
        size = content_length(r)
        category = classify_resource(final, ct, filename)

        if category == "paginas":
            r.close()
            if same_host(final, self.host):
                self.enqueue_page(final, 0)
            return

        self.add_resource(
            final, item["source"], ct, size, filename, status,
            not same_host(final, self.host), 200 <= status < 400
        )
        r.close()

    def inspect_queued_resources(self):
        if not self.resource_queue:
            return
        print("\n[+] Inspecionando recursos descobertos...")
        count = 0
        while self.resource_queue:
            if MAX_RESOURCE_INSPECTIONS is not None and count >= MAX_RESOURCE_INSPECTIONS:
                break
            item = self.resource_queue.popleft()
            count += 1
            if count == 1 or count % 25 == 0:
                print(f"    {count} recurso(s) processado(s)...")
            self.inspect_resource(item)
        print(f"    [+] {count} recurso(s) inspecionado(s).")

    def process_candidate(self, base_url, raw_url, depth, source, resource_hint=False):
        if not raw_url:
            return
        raw_url = raw_url.strip()
        if not raw_url or raw_url.lower().startswith(
            ("mailto:", "tel:", "javascript:", "data:", "blob:", "about:")
        ):
            return
        absolute = normalize_url(urljoin(base_url, raw_url))
        if not absolute:
            return
        internal = same_host(absolute, self.host)
        if resource_hint or get_extension(absolute) in KNOWN_FILE_EXTENSIONS:
            self.queue_resource(absolute, source, external=not internal)
        elif not internal:
            self.external_links.add(absolute)
        else:
            self.enqueue_page(absolute, depth + 1)

    def process_srcset(self, base_url, srcset, depth, source):
        if not srcset:
            return
        for part in srcset.split(","):
            pieces = part.strip().split()
            if pieces:
                self.process_candidate(base_url, pieces[0], depth, source, True)

    def extract_css_urls(self, base_url, css_text, depth):
        if not css_text:
            return
        for value in re.findall(r"url\(\s*['\"]?([^'\"\)]+)", css_text, re.I):
            self.process_candidate(base_url, value, depth, "recurso CSS", True)

    def parse_html(self, url, html, depth):
        soup = BeautifulSoup(html, "html.parser")
        base_tag = soup.find("base", href=True)
        base = urljoin(url, base_tag["href"]) if base_tag else url

        for tag in soup.find_all("a", href=True):
            self.process_candidate(base, tag.get("href"), depth, "link HTML", tag.has_attr("download"))

        for tag in soup.find_all("img"):
            if tag.get("src"):
                self.process_candidate(base, tag["src"], depth, "imagem HTML", True)
            if tag.get("srcset"):
                self.process_srcset(base, tag["srcset"], depth, "imagem srcset")
            for attr in ("data-src", "data-original", "data-lazy-src"):
                if tag.get(attr):
                    self.process_candidate(base, tag[attr], depth, f"imagem {attr}", True)

        for tag_name in ("video", "audio", "source", "track"):
            for tag in soup.find_all(tag_name):
                if tag.get("src"):
                    self.process_candidate(base, tag["src"], depth, f"{tag_name} HTML", True)
                if tag.get("srcset"):
                    self.process_srcset(base, tag["srcset"], depth, f"{tag_name} srcset")

        for tag in soup.find_all("video", poster=True):
            self.process_candidate(base, tag["poster"], depth, "poster de vídeo", True)

        for tag in soup.find_all("script", src=True):
            self.process_candidate(base, tag["src"], depth, "script HTML", True)

        resource_rels = {
            "stylesheet", "icon", "shortcut", "preload", "prefetch",
            "manifest", "apple-touch-icon",
        }
        for tag in soup.find_all("link", href=True):
            rel = {str(x).lower() for x in tag.get("rel", [])}
            self.process_candidate(
                base, tag["href"], depth,
                "recurso <link>" if rel.intersection(resource_rels) else "link HTML",
                bool(rel.intersection(resource_rels)),
            )

        for tag in soup.find_all("iframe", src=True):
            iframe = normalize_url(urljoin(base, tag["src"]))
            if iframe:
                if same_host(iframe, self.host):
                    self.enqueue_page(iframe, depth + 1)
                else:
                    self.external_links.add(iframe)

        for tag in soup.find_all("object", data=True):
            self.process_candidate(base, tag["data"], depth, "object HTML", True)
        for tag in soup.find_all("embed", src=True):
            self.process_candidate(base, tag["src"], depth, "embed HTML", True)
        for style in soup.find_all("style"):
            self.extract_css_urls(base, style.get_text(), depth)
        for tag in soup.find_all(style=True):
            self.extract_css_urls(base, tag.get("style"), depth)

    def process_network_observations(self, base_url, depth):
        """Merge CDP network URLs into the normal URL/classification pipeline."""
        requests_by_id = self.browser.network_requests
        responses_by_id = self.browser.network_responses
        observed = {}
        for request_id, item in requests_by_id.items():
            observed[item["url"]] = {
                "resource_type": item.get("resource_type", ""),
                "content_type": "",
                "status": None,
            }
        for request_id, item in responses_by_id.items():
            current = observed.setdefault(item["url"], {})
            current.update({
                "resource_type": item.get("resource_type", "") or current.get("resource_type", ""),
                "content_type": item.get("content_type", ""),
                "status": item.get("status"),
            })

        for url, meta in observed.items():
            url = normalize_url(url)
            if not url or is_obvious_telemetry(url):
                continue
            self.dynamic_network_urls.add(url)
            resource_type = meta.get("resource_type", "")
            content_type = meta.get("content_type", "")
            response_is_html = is_probably_html(content_type)
            is_document = resource_type in ("Document", "Iframe")
            resource_hint = (
                resource_type in NETWORK_RESOURCE_TYPES
                and not (resource_type in ("XHR", "Fetch") and response_is_html)
            )
            if is_document or response_is_html:
                self.process_candidate(
                    base_url, url, depth, "URL observada via CDP Network", False
                )
            else:
                self.process_candidate(
                    base_url, url, depth, "recurso observado via CDP Network", resource_hint
                )

        self.dynamic_network_requests += len(requests_by_id)
        self.dynamic_network_responses += len(responses_by_id)

    def looks_like_login_page(self, info):
        title = (info.get("title") or "").lower()
        text = BeautifulSoup(info.get("html") or "", "html.parser").get_text(" ", strip=True).lower()
        markers = (
            "log in", "login", "entrar", "sign in", "crie uma conta",
            "create new account", "senha", "password",
        )
        return self.is_known_dynamic_domain and any(marker in f"{title} {text}" for marker in markers)

    def browser_fetch(self, url, depth, dynamic=False):
        print("       [>] Ativando motor Chromium...")
        if not self.browser.start():
            self.record_anomaly(url, None, "Chromium não disponível.")
            return False

        info = self.browser.navigate(url)
        if not info:
            self.record_anomaly(url, None, "Chromium não obteve a página.")
            return False

        final = normalize_url(info.get("url", "")) or url
        html = info.get("html", "")
        title = info.get("title", "")
        if (
            not dynamic
            and self.dynamic_cycles is None
            and not self.dynamic_disabled
            and looks_like_dynamic_document(final, html)
        ):
            self.dynamic_cycles = DYNAMIC_SCROLL_PRESETS["rapido"]
            self.dynamic_level = self.dynamic_cycles
            dynamic = True
            print("       [>] Página dinâmica detectada; aplicando scroll rápido automático (10 ciclos).")
        dynamic = bool(dynamic or (self.dynamic_cycles and looks_like_dynamic_document(final, html)))

        if not html or len(html) < 50:
            self.record_anomaly(final, None, "DOM vazio após navegação Chromium.")
            return False
        if self.browser.browser_has_challenge(info):
            self.record_anomaly(final, 403, "Challenge ainda presente no navegador.", "text/html", len(html))
            return False

        if self.looks_like_login_page(info):
            self.login_required_pages.add(final)
            print("       [!] A página aparenta exigir login; usando somente a sessão do perfil Chromium.")

        scroll_stats = None
        if dynamic and self.dynamic_cycles:
            print(f"       [>] Dynamic Page Engine: scroll de até {self.dynamic_cycles} ciclo(s)...")
            scroll_stats = self.browser.dynamic_scroll(self.dynamic_cycles)
            self.dynamic_scroll_cycles += scroll_stats["cycles"]
            try:
                info = self.browser.page_info()
                final = normalize_url(info.get("url", "")) or final
                html = info.get("html", "")
                title = info.get("title", "")
            except Exception:
                pass
            self.process_network_observations(final, depth)
            print(
                "       [+] Scroll executado: "
                f"{scroll_stats['cycles']} ciclo(s)"
                + ("; parada antecipada por falta de crescimento relevante." if scroll_stats["stopped_early"] else ".")
            )

        self.browser_pages += 1
        self.visited_pages.add(final)
        if dynamic:
            self.dynamic_pages += 1
            self.dynamic_page_urls.add(final)
        print(f"       [✓] Browser DOM: {len(html)} caracteres")
        if title:
            print(f"       [✓] Título: {title[:100]}")

        imported = self.browser.sync_cookies_to_requests(self.session)
        if imported:
            print(f"       [✓] Cookies sincronizados: {imported}")

        before_pages = len(self.queued_pages)
        before_resources = len(self.queued_resources)
        self.parse_html(final, html, depth)
        print(
            f"       [+] Novas páginas: {max(0, len(self.queued_pages)-before_pages)} | "
            f"Recursos descobertos: {max(0, len(self.queued_resources)-before_resources)}"
        )
        return True

    def crawl_page(self, url, depth):
        if self.dynamic_enabled_for(url):
            if self.is_known_dynamic_domain:
                print("       [>] Domínio Facebook: usando Dynamic Page Engine.")
            else:
                print("       [>] Scroll dinâmico solicitado; usando Chromium.")
            dynamic_result = self.browser_fetch(url, depth, dynamic=True)
            if dynamic_result:
                return True
            print("       [!] Dynamic Page Engine indisponível; tentando crawler HTTP.")

        r = self.request("GET", url, headers=BROWSER_HEADERS)
        if r is None:
            print("       [!] Falha HTTP.")
            return False

        final = normalize_url(r.url) or url
        status = r.status_code
        ct = r.headers.get("Content-Type", "")
        body = r.content or b""
        body_size = len(body)

        print(f"       HTTP {status} | {clean_content_type(ct) or '?'} | {human_size(body_size)}")

        use_browser, reason = should_use_browser_fallback(status, ct, body)
        if use_browser:
            if reason == "cloudflare":
                print("       [!] Cloudflare challenge detectado.")
                self.cloudflare_challenges += 1
            elif reason == "html-202":
                print("       [!] HTTP 202 com HTML intermediário/suspeito.")
                print("       [>] Encaminhando página ao Chromium.")
            elif reason == "html-vazio":
                print("       [!] Resposta HTML vazia.")
                print("       [>] Encaminhando página ao Chromium.")
            r.close()
            return self.browser_fetch(url, depth)

        if status in (401, 403):
            print(f"       [!] Acesso recusado: HTTP {status}")
            self.record_anomaly(final, status, "Acesso recusado.", ct, body_size)
            r.close()
            return False

        if status >= 400:
            print(f"       [!] Resposta HTTP {status}")
            self.record_anomaly(final, status, f"Resposta HTTP {status}.", ct, body_size)
            r.close()
            return False

        if body_size == 0:
            print("       [!] Resposta vazia.")
            self.record_anomaly(final, status, "Servidor retornou corpo vazio.", ct, 0)
            r.close()
            return False

        html_response = is_probably_html(ct) or looks_like_html(body)
        if html_response:
            if body_size > MAX_HTML_BYTES:
                self.record_anomaly(final, status, "HTML excede limite.", ct, body_size)
                r.close()
                return False
            if not r.encoding:
                r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
            self.visited_pages.add(final)
            print(f"       [+] HTML recebido: {len(html)} caracteres")
            bp, br = len(self.queued_pages), len(self.queued_resources)
            self.parse_html(final, html, depth)
            print(
                f"       [+] Novas páginas: {max(0,len(self.queued_pages)-bp)} | "
                f"Recursos descobertos: {max(0,len(self.queued_resources)-br)}"
            )
            r.close()
            return True

        disposition = r.headers.get("Content-Disposition", "")
        filename = filename_from_content_disposition(disposition) or filename_from_url(final)
        size = content_length(r) or body_size
        self.add_resource(
            final, "resposta HTTP de link interno", ct, size, filename, status,
            not same_host(final, self.host), True
        )
        print(f"       [+] Recurso: {filename}")
        r.close()
        return True

    def scan_robots(self):
        p = urlparse(self.start_url)
        url = f"{p.scheme}://{p.netloc}/robots.txt"
        r = self.request("GET", url, headers=BROWSER_HEADERS)
        if r is None:
            print("    [-] robots.txt: erro")
            return
        body = r.content or b""
        if detect_cloudflare_challenge(r.status_code, body):
            print("    [!] robots.txt: Cloudflare challenge")
            r.close()
            return
        if r.status_code != 200:
            print(f"    [-] robots.txt: HTTP {r.status_code}")
            r.close()
            return
        print(f"    [+] robots.txt encontrado ({len(r.text)} caracteres)")
        for line in r.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm:
                    self.scan_sitemap(sm)
        r.close()

    def scan_sitemap(self, sitemap_url):
        sitemap_url = normalize_url(sitemap_url)
        if not sitemap_url or sitemap_url in self.sitemaps_seen:
            return
        self.sitemaps_seen.add(sitemap_url)
        r = self.request("GET", sitemap_url, headers=BROWSER_HEADERS)
        if r is None:
            return
        body = r.content or b""
        if detect_cloudflare_challenge(r.status_code, body) or r.status_code != 200:
            r.close()
            return
        data = body
        if sitemap_url.lower().endswith(".gz") or "gzip" in r.headers.get("Content-Type", "").lower():
            try:
                data = gzip.decompress(data)
            except Exception:
                r.close()
                return
        try:
            soup = BeautifulSoup(data, "xml")
        except Exception:
            r.close()
            return
        first = soup.find()
        root = first.name.lower() if first else ""
        locations = [x.get_text(strip=True) for x in soup.find_all("loc")]
        if root == "sitemapindex":
            for loc in locations:
                self.scan_sitemap(loc)
        else:
            for loc in locations:
                candidate = normalize_url(loc)
                if not candidate or not same_host(candidate, self.host):
                    continue
                if get_extension(candidate) in KNOWN_FILE_EXTENSIONS:
                    self.queue_resource(candidate, "sitemap", False)
                else:
                    self.enqueue_page(candidate, 0)
        r.close()

    def scan_default_sitemaps(self):
        p = urlparse(self.start_url)
        base = f"{p.scheme}://{p.netloc}"
        for url in (f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"):
            self.scan_sitemap(url)

    def run(self):
        print(f"\nSITE SCANNER {VERSION}\n" + "=" * 70)
        print(f"Alvo:    {self.start_url}\nDomínio: {self.host}\n" + "=" * 70)
        print("\n[+] Verificando robots.txt...")
        self.scan_robots()
        print("\n[+] Verificando sitemaps conhecidos...")
        self.scan_default_sitemaps()
        print("\n[+] Adicionando página inicial...")
        self.enqueue_page(self.start_url, 0)
        print("\n[+] Iniciando crawling...\n")

        try:
            while self.queue:
                if len(self.visited_pages) >= MAX_PAGES:
                    print(f"[!] Limite de {MAX_PAGES} páginas atingido.")
                    break
                url, depth = self.queue.popleft()
                if url in self.visited_pages:
                    continue
                print(f"[{len(self.visited_pages)+1:04}] depth={depth:<2} {url}")
                if not self.crawl_page(url, depth):
                    self.failed_pages.add(url)
            self.inspect_queued_resources()
        finally:
            self.browser.close()

        print("\n" + "=" * 70 + "\n[+] Varredura concluída.\n" + "=" * 70)


def group_resources(scanner):
    groups = defaultdict(list)
    for item in scanner.resources.values():
        groups[item["category"]].append(item)
    return groups


def build_numbered_resources(scanner):
    items = list(scanner.resources.values())
    items.sort(key=lambda x: (x["category"], x["filename"].lower(), x["url"]))
    for i, item in enumerate(items, 1):
        item["_id"] = i
    return items


def show_summary(scanner):
    groups = group_resources(scanner)
    print("\n" + "=" * 70 + "\nRESULTADO\n" + "=" * 70)
    print(f"Requisições HTTP:       {scanner.requests_count}")
    print(f"Páginas visitadas:      {len(scanner.visited_pages)}")
    print(f"Páginas via Chromium:   {scanner.browser_pages}")
    print(f"Páginas via motor dinâmico: {scanner.dynamic_pages}")
    print(f"Ciclos de scroll:        {scanner.dynamic_scroll_cycles}")
    print(f"URLs únicas via Network: {len(scanner.dynamic_network_urls)}")
    print(f"Challenges Cloudflare:  {scanner.cloudflare_challenges}")
    print(f"Páginas com falha:      {len(scanner.failed_pages)}")
    print(f"Recursos encontrados:   {len(scanner.resources)}")
    print(f"Recursos externos/CDN:  {len(scanner.external_resources)}")
    print(f"Links externos:          {len(scanner.external_links)}")
    print(f"Anomalias:               {len(scanner.anomalies)}")
    print(f"Erros de rede:           {len(scanner.errors)}")
    if groups:
        print("\nCATEGORIAS\n" + "-" * 70)
        for category in sorted(groups):
            print(f"{category.upper():22}{len(groups[category]):6}")


def list_resources(resources, category=None, search=None):
    selected = resources
    if category:
        selected = [x for x in selected if x["category"] == category]
    if search:
        s = search.lower()
        selected = [x for x in selected if s in x["filename"].lower() or s in x["url"].lower()]
    print(f"\n{'ID':>5}  {'TIPO':<15} {'TAMANHO':>10} {'EXT':>3} NOME\n" + "-" * 100)
    for x in selected:
        ext = "EXT" if x.get("external") else ""
        print(f"{x['_id']:>5}  {x['category']:<15} {human_size(x['size']):>10} {ext:>3} {x['filename']}")
    print(f"\n{len(selected)} recurso(s).")
    return selected


def show_resource(item):
    print("\n" + "=" * 70)
    print(f"ID:           {item['_id']}")
    print(f"Nome:         {item['filename']}")
    print(f"Categoria:    {item['category']}")
    print(f"Tamanho:      {human_size(item['size'])}")
    print(f"Status HTTP:  {item['status']}")
    print(f"Content-Type: {item['content_type']}")
    print(f"Origem:       {item['source']}")
    print(f"Externo/CDN:  {'sim' if item['external'] else 'não'}")
    print(f"Download:     {'sim' if item['downloadable'] else 'não'}")
    print(f"URL:          {item['url']}")
    print("=" * 70)


def show_anomalies(scanner):
    print("\n" + "=" * 70 + "\nANOMALIAS\n" + "=" * 70)
    if not scanner.anomalies:
        print("Nenhuma anomalia registrada.")
        return
    for i, x in enumerate(scanner.anomalies, 1):
        print(f"\n[{i}] HTTP {x['status']} - {x['message']}")
        print(f"    Tipo: {x['content_type'] or '?'}")
        print(f"    Tamanho: {human_size(x['size'])}")
        print(f"    URL: {x['url']}")


def download_resource(session, item, site_host):
    if not item.get("downloadable", True):
        print(f"[!] Recurso não disponível: {item['filename']}")
        return False
    folder = DOWNLOAD_DIR / sanitize_filename(site_host) / item["category"]
    folder.mkdir(parents=True, exist_ok=True)
    print(f"\nBaixando: {item['filename']}\nURL: {item['url']}")
    try:
        with session.get(
            item["url"], headers=RESOURCE_HEADERS, stream=True,
            timeout=TIMEOUT, allow_redirects=True
        ) as r:
            if r.status_code in (401, 403):
                print(f"[!] Servidor recusou acesso: HTTP {r.status_code}")
                return False
            r.raise_for_status()
            filename = (
                filename_from_content_disposition(r.headers.get("Content-Disposition", ""))
                or item["filename"] or filename_from_url(r.url)
            )
            path = unique_path(folder / sanitize_filename(filename))
            total = content_length(r)
            downloaded = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        print(
                            f"\r{downloaded/total*100:6.1f}% "
                            f"{human_size(downloaded)} / {human_size(total)}",
                            end="", flush=True
                        )
                    else:
                        print(f"\r{human_size(downloaded)}", end="", flush=True)
            print(f"\n[+] Salvo em: {path}")
            return True
    except requests.RequestException as exc:
        print(f"\n[!] Erro no download: {exc}")
        return False



def export_external_bookmarks(scanner):
    """Gera um bookmarks.html com os links externos descobertos no site."""
    site_dir = DOWNLOAD_DIR / sanitize_filename(scanner.host)
    site_dir.mkdir(parents=True, exist_ok=True)
    path = site_dir / "bookmarks.html"

    links = sorted(scanner.external_links)
    origin = html.escape(scanner.start_url, quote=True)
    host = html.escape(scanner.host, quote=True)

    items = []
    for url in links:
        safe_url = html.escape(url, quote=True)
        safe_text = html.escape(url)
        items.append(
            f'      <li><a href="{safe_url}" target="_blank" '
            f'rel="noopener noreferrer">{safe_text}</a></li>'
        )

    list_html = "\n".join(items) if items else (
        '      <li class="empty">Nenhum link externo foi encontrado nesta varredura.</li>'
    )

    document = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Links externos — {host}</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body {{
      max-width: 1000px;
      margin: 0 auto;
      padding: 32px 20px 64px;
      line-height: 1.55;
    }}
    h1 {{
      margin-bottom: 8px;
    }}
    .meta {{
      margin: 0 0 28px;
      opacity: .75;
    }}
    ol {{
      padding-left: 28px;
    }}
    li {{
      margin: 10px 0;
      overflow-wrap: anywhere;
    }}
    a {{
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }}
    .empty {{
      opacity: .7;
    }}
  </style>
</head>
<body>
  <h1>Links externos</h1>
  <p class="meta">
    Site analisado:
    <a href="{origin}" target="_blank" rel="noopener noreferrer">{origin}</a><br>
    Domínio: {host}<br>
    Links encontrados: {len(links)}
  </p>
  <ol>
{list_html}
  </ol>
</body>
</html>
"""

    path.write_text(document, encoding="utf-8")
    print(f"\n[+] Bookmarks salvos em: {path.resolve()}")
    print(f"[+] Links externos incluídos: {len(links)}")
    return path

def export_scan(scanner, resources):
    data = {
        "scanner": {"name": "Site Scanner", "version": VERSION},
        "site": scanner.start_url,
        "domain": scanner.host,
        "statistics": {
            "requests": scanner.requests_count,
            "pages": len(scanner.visited_pages),
            "browser_pages": scanner.browser_pages,
            "dynamic_pages": scanner.dynamic_pages,
            "dynamic_scroll_cycles": scanner.dynamic_scroll_cycles,
            "dynamic_network_urls": len(scanner.dynamic_network_urls),
            "dynamic_network_requests": scanner.dynamic_network_requests,
            "dynamic_network_responses": scanner.dynamic_network_responses,
            "login_required_pages": len(scanner.login_required_pages),
            "cloudflare_challenges": scanner.cloudflare_challenges,
            "failed_pages": len(scanner.failed_pages),
            "resources": len(scanner.resources),
            "external_resources": len(scanner.external_resources),
            "external_links": len(scanner.external_links),
            "anomalies": len(scanner.anomalies),
            "errors": len(scanner.errors),
        },
        "pages_visited": sorted(scanner.visited_pages),
        "dynamic": {
            "enabled": bool(scanner.dynamic_cycles),
            "configured_cycles": scanner.dynamic_cycles or 0,
            "pages": sorted(scanner.dynamic_page_urls),
            "network_urls": sorted(scanner.dynamic_network_urls),
            "login_required_pages": sorted(scanner.login_required_pages),
        },
        "failed_pages": sorted(scanner.failed_pages),
        "resources": [
            {k: v for k, v in item.items() if k != "_id"} for item in resources
        ],
        "external_links": sorted(scanner.external_links),
        "anomalies": scanner.anomalies,
        "errors": scanner.errors,
    }
    site_dir = DOWNLOAD_DIR / sanitize_filename(scanner.host)
    site_dir.mkdir(parents=True, exist_ok=True)
    path = site_dir / "scan.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[+] Catálogo salvo em: {path.resolve()}")


CATEGORY_ALIASES = {
    "pdf": "documentos", "pdfs": "documentos", "doc": "documentos",
    "docs": "documentos", "documento": "documentos", "documentos": "documentos",
    "imagem": "imagens", "imagens": "imagens", "foto": "imagens", "fotos": "imagens",
    "video": "video", "videos": "video", "audio": "audio",
    "planilha": "planilhas", "planilhas": "planilhas",
    "zip": "compactados", "rar": "compactados", "compactados": "compactados",
    "apresentacao": "apresentacoes", "apresentacoes": "apresentacoes",
    "dados": "dados", "fontes": "fontes", "programas": "programas",
    "web": "web", "outros": "outros",
}


def resolve_category(value):
    return CATEGORY_ALIASES.get(value.lower(), value.lower())


def select_resources(resources, value):
    value = value.strip().lower()
    if value == "all":
        return resources[:]
    category = resolve_category(value)
    matches = [x for x in resources if x["category"] == category]
    if matches:
        return matches
    if "," in value:
        try:
            ids = {int(x.strip()) for x in value.split(",")}
            return [x for x in resources if x["_id"] in ids]
        except ValueError:
            return []
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", value)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return [x for x in resources if a <= x["_id"] <= b]
    try:
        rid = int(value)
        return [x for x in resources if x["_id"] == rid]
    except ValueError:
        return []


def print_help():
    print("""
COMANDOS
----------------------------------------------------------------------
list
filter pdf
filter imagens
filter compactados
search ocarina
show 10
download 10
download 1,2,5
download 10-20
download pdf
download imagens
download all
download external
bookmarks
anomalies
stats
export
help
quit
""")


def interactive_mode(scanner):
    resources = build_numbered_resources(scanner)
    show_summary(scanner)
    print_help()

    while True:
        try:
            command = input("\nscanner2> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not command:
            continue
        lower = command.lower()

        if lower in ("quit", "exit", "q"):
            break
        if lower in ("help", "?"):
            print_help()
            continue
        if lower == "list":
            list_resources(resources)
            continue
        if lower == "stats":
            show_summary(scanner)
            continue
        if lower in ("anomalies", "anomalias"):
            show_anomalies(scanner)
            continue
        if lower == "export":
            export_scan(scanner, resources)
            continue
        if lower in ("bookmarks", "external", "download external", "download links", "download links externos"):
            export_external_bookmarks(scanner)
            continue
        if lower.startswith("show "):
            try:
                rid = int(lower[5:].strip())
            except ValueError:
                print("ID inválido.")
                continue
            item = next((x for x in resources if x["_id"] == rid), None)
            print("Recurso não encontrado.") if item is None else show_resource(item)
            continue
        if lower.startswith("filter "):
            list_resources(resources, category=resolve_category(lower[7:].strip()))
            continue
        if lower.startswith("search "):
            list_resources(resources, search=command[7:].strip())
            continue
        if lower.startswith("download "):
            selected = select_resources(resources, command[9:].strip())
            if not selected:
                print("Nenhum recurso correspondente.")
                continue
            downloadable = [x for x in selected if x.get("downloadable", True)]
            print(f"\n{len(downloadable)} arquivo(s) disponível(is) para download.")
            if len(downloadable) > 1:
                try:
                    confirmation = input("Continuar? [s/N]: ").strip().lower()
                except KeyboardInterrupt:
                    print()
                    continue
                if confirmation not in ("s", "sim", "y", "yes"):
                    print("Download cancelado.")
                    continue
            success = sum(download_resource(scanner.session, x, scanner.host) for x in downloadable)
            print(f"\n[+] Downloads concluídos: {success}/{len(downloadable)}")
            continue
        print("Comando desconhecido. Digite 'help'.")


def main():
    parser = argparse.ArgumentParser(
        description="Site Scanner 2.5 com Dynamic Page Engine e suporte a Facebook."
    )
    parser.add_argument("url", nargs="?", help="URL inicial do site")
    parser.add_argument(
        "--dynamic", "--scroll", dest="dynamic", metavar="NIVEL",
        help="scroll dinâmico: rapido, medio, profundo ou número de ciclos",
    )
    parser.add_argument(
        "--no-dynamic", action="store_true",
        help="desativa o Dynamic Page Engine, inclusive para Facebook",
    )
    args = parser.parse_args()

    clear_screen()
    print("=" * 70)
    print(f" SITE SCANNER {VERSION}")
    print("=" * 70 + "\n")

    raw_url = args.url or input("URL do site: ").strip()
    url = clean_input_url(raw_url)
    if url != raw_url.strip():
        print(f"URL normalizada: {url}")
    if not url:
        print("Nenhuma URL informada.")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        scanner = SiteScanner(url, args.dynamic, args.no_dynamic)
    except ValueError as exc:
        print(f"Erro: {exc}")
        return

    if scanner.is_known_dynamic_domain:
        scanner.configure_dynamic_mode(args.dynamic, prompt=not args.no_dynamic)
    elif args.dynamic:
        scanner.configure_dynamic_mode(args.dynamic, prompt=False)

    try:
        scanner.run()
    except KeyboardInterrupt:
        print("\n[!] Varredura interrompida.")
        scanner.browser.close()

    interactive_mode(scanner)


if __name__ == "__main__":
    main()
