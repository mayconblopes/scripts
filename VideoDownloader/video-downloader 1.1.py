#!/usr/bin/env python3
"""
VIDEO DOWNLOADER 1.2

Downloader interativo baseado em yt-dlp, compatível com Windows, Linux e
Termux/Android.

Recursos preservados da versão 1.0:
- vídeo na melhor qualidade ou com resolução máxima;
- áudio original e conversão para MP3 (128/192/256/320 kbps);
- legendas, vídeo com legendas e incorporação opcional;
- vídeo individual ou playlist completa;
- organização por site/uploader e, em playlists, por playlist/índice;
- histórico para evitar downloads duplicados;
- cookies por arquivo cookies.txt ou navegador;
- consulta das informações do vídeo;
- retomada e novas tentativas de downloads/fragmentos.

Correções da versão 1.2:
- se a resolução solicitada não estiver disponível, tenta sequencialmente:
  resolução solicitada, melhor combinação compatível, melhor formato único e seleção padrão do
  yt-dlp; o programa informa quando um fallback é utilizado.
- históricos separados por tipo de download, evitando que vídeo bloqueie áudio;
- MP4 + M4A priorizados e MKV usado como contêiner seguro para fallback;
- falhas de mesclagem exibidas corretamente e encaminhadas ao fallback.

Uso:
    python downloader.py
    python downloader.py "https://..."       # abre o menu interativo
    python downloader.py "URL" --mode video --resolution 1080 --single
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import yt_dlp
except ImportError:
    yt_dlp = None  # type: ignore[assignment]


APP_NAME = "VIDEO DOWNLOADER"
VERSION = "1.2"
SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = SCRIPT_DIR / "downloads"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"

DOMAIN_ALIASES = {
    "youtu.be": "youtube.com",
    "m.youtube.com": "youtube.com",
    "music.youtube.com": "youtube.com",
    "x.com": "twitter.com",
    "mobile.x.com": "twitter.com",
    "mobile.twitter.com": "twitter.com",
    "m.instagram.com": "instagram.com",
    "vm.tiktok.com": "tiktok.com",
}


class DownloadLogger:
    """Captura erros inclusive quando yt-dlp continua uma playlist."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        # O progresso normal continua sendo exibido pelo yt-dlp.
        return

    def warning(self, message: str) -> None:
        print(f"[aviso yt-dlp] {message}")

    def error(self, message: str) -> None:
        text = str(message).strip()
        if text and text not in self.errors:
            self.errors.append(text)
            print(f"[erro yt-dlp] {text}")


def require_yt_dlp() -> bool:
    if yt_dlp is not None:
        return True

    print("\n[ERRO] O pacote yt-dlp não está instalado.")
    print("Termux/Android: pip install -U yt-dlp")
    print("Windows/Linux:  python -m pip install -U yt-dlp")
    return False


def header() -> None:
    print()
    print("=" * 72)
    print(f"{APP_NAME} {VERSION}")
    print("=" * 72)


def pause() -> None:
    try:
        input("\nPressione ENTER para continuar...")
    except (EOFError, KeyboardInterrupt):
        pass


def get_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return DOMAIN_ALIASES.get(domain, domain or "outros")


def valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def find_ffmpeg(explicit_location: Optional[str] = None) -> Optional[str]:
    if explicit_location:
        location = Path(explicit_location).expanduser()
        if location.is_file():
            return str(location)
        for name in ("ffmpeg", "ffmpeg.exe"):
            candidate = location / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffmpeg")


def ffmpeg_available(explicit_location: Optional[str] = None) -> bool:
    return find_ffmpeg(explicit_location) is not None


def choose(prompt: str, options: dict[str, str], default: Optional[str] = None) -> str:
    while True:
        print()
        for key, label in options.items():
            suffix = " [padrão]" if default == key else ""
            print(f"  {key}) {label}{suffix}")

        value = input(f"\n{prompt}: ").strip().lower()
        if not value and default is not None:
            return default
        if value in options:
            return value
        print("[!] Opção inválida.")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [S/n]" if default else " [s/N]"
    while True:
        answer = input(prompt + suffix + ": ").strip().lower()
        if not answer:
            return default
        if answer in ("s", "sim", "y", "yes"):
            return True
        if answer in ("n", "nao", "não", "no"):
            return False
        print("[!] Responda com S ou N.")


def progress_hook(data: dict[str, Any]) -> None:
    if data.get("status") == "finished":
        filename = data.get("filename")
        if filename:
            print(f"\n[+] Download recebido: {filename}")
        print("[+] Finalizando/processando arquivo...")


def postprocessor_hook(data: dict[str, Any]) -> None:
    if data.get("status") == "finished":
        processor = data.get("postprocessor")
        if processor:
            print(f"[+] Processamento concluído: {processor}")


def archive_path_for_mode(mode: str) -> Path:
    """Retorna um histórico independente para cada tipo de download.

    O histórico do yt-dlp é indexado pelo ID do vídeo, não pelo formato.
    Portanto, um histórico único faria um vídeo já baixado impedir um
    download posterior somente de áudio ou em MP3.
    """

    return DOWNLOAD_ROOT / f".download_archive_{mode}.txt"


def cookie_options() -> dict[str, Any]:
    """Escolhe cookies de arquivo, navegador ou nenhum."""

    if COOKIES_FILE.is_file():
        print(f"\n[i] Arquivo de cookies encontrado: {COOKIES_FILE.name}")
        if ask_yes_no("Usar esse arquivo de cookies?", default=True):
            return {"cookiefile": str(COOKIES_FILE)}

    if not ask_yes_no("Tentar importar cookies de um navegador instalado?", default=False):
        return {}

    browsers = {
        "1": "chrome",
        "2": "edge",
        "3": "firefox",
        "4": "brave",
        "5": "chromium",
        "6": "opera",
        "0": "cancelar",
    }
    selected = choose("Navegador", browsers, default="0")
    if selected == "0":
        return {}
    return {"cookiesfrombrowser": (browsers[selected],)}


def base_options(
    url: str,
    playlist: bool,
    cookies: Optional[dict[str, Any]] = None,
    use_archive: bool = True,
    ffmpeg_location: Optional[str] = None,
    mode: str = "video",
) -> dict[str, Any]:
    domain = get_domain(url)
    destination = DOWNLOAD_ROOT / domain
    destination.mkdir(parents=True, exist_ok=True)

    if playlist:
        template = (
            destination
            / "%(uploader,uploader_id,channel,channel_id|Sem autor)s"
            / "%(playlist_title|Playlist)s"
            / "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
        )
    else:
        template = (
            destination
            / "%(uploader,uploader_id,channel,channel_id|Sem autor)s"
            / "%(title)s [%(id)s].%(ext)s"
        )

    options: dict[str, Any] = {
        "outtmpl": str(template),
        "windowsfilenames": True,
        # Em download individual, falhas de pós-processamento precisam
        # chegar ao fallback. Em playlists, continue para os próximos itens.
        "ignoreerrors": playlist,
        "overwrites": False,
        "continuedl": True,
        "noplaylist": not playlist,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "retries": 10,
        "fragment_retries": 10,
    }

    if use_archive:
        DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        options["download_archive"] = str(archive_path_for_mode(mode))
    if cookies:
        options.update(cookies)
    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location
    return options


def require_ffmpeg(reason: str, explicit_location: Optional[str] = None) -> bool:
    path = find_ffmpeg(explicit_location)
    if path:
        return True

    print()
    print("[ERRO] FFmpeg não foi encontrado no PATH.")
    print(f"Ele é necessário para {reason}.")
    print()
    print("Termux/Android:")
    print("  pkg install ffmpeg")
    print("Windows/Linux: instale o FFmpeg e adicione-o ao PATH.")
    return False


def select_resolution() -> Optional[int]:
    resolutions = {
        "1": "Melhor disponível",
        "2": "Até 2160p (4K)",
        "3": "Até 1440p",
        "4": "Até 1080p",
        "5": "Até 720p",
        "6": "Até 480p",
        "7": "Até 360p",
    }
    selected = choose("Qualidade", resolutions, default="1")
    return {
        "2": 2160,
        "3": 1440,
        "4": 1080,
        "5": 720,
        "6": 480,
        "7": 360,
    }.get(selected)


def select_mp3_quality() -> str:
    qualities = {
        "1": "320 kbps",
        "2": "256 kbps",
        "3": "192 kbps",
        "4": "128 kbps",
    }
    selected = choose("Qualidade do MP3", qualities, default="1")
    return {"1": "320", "2": "256", "3": "192", "4": "128"}[selected]


def format_error(message: str) -> bool:
    text = message.lower()
    return any(
        pattern in text
        for pattern in (
            "requested format is not available",
            "requested format not available",
            "format is not available",
            "no video formats found",
            "no formats found",
            "error muxing",
            "could not write header",
            "invalid argument",
            "codec not currently supported in container",
            "stream #",
        )
    )


def video_format_strategies(
    resolution: Optional[int], has_ffmpeg: bool
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Retorna formatos em ordem, com contêiner seguro para cada tentativa."""

    strategies: list[tuple[str, Optional[str], Optional[str]]] = []
    if resolution is not None:
        if has_ffmpeg:
            strategies.append(
                (
                    f"resolução solicitada ({resolution}p, MP4 compatível)",
                    f"bv[ext=mp4][height<={resolution}]+ba[ext=m4a]/b[ext=mp4][height<={resolution}]",
                    "mp4",
                )
            )
            strategies.append(
                (
                    f"resolução solicitada ({resolution}p, MKV)",
                    f"bv[height<={resolution}]+ba/b[height<={resolution}]",
                    "mkv",
                )
            )
        else:
            strategies.append(
                (
                    f"formato único até {resolution}p (FFmpeg ausente)",
                    f"b[height<={resolution}]/b",
                    None,
                )
            )

    # Primeiro tenta MP4 + M4A. O fallback genérico usa MKV, que aceita
    # vídeo MP4 com áudio Opus/WebM mesmo em versões antigas do FFmpeg.
    if has_ffmpeg:
        strategies.append(
            (
                "melhor vídeo + áudio compatível com MP4",
                "bv[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
                "mp4",
            )
        )
        strategies.append(("melhor vídeo + áudio (MKV)", "bv+ba/b", "mkv"))
    strategies.append(("melhor formato único", "b", None))
    strategies.append(("seleção padrão do yt-dlp", None, None))
    return strategies


def audio_format_strategies() -> list[tuple[str, Optional[str], Optional[str]]]:
    return [
        ("melhor áudio (M4A quando disponível)", "bestaudio[ext=m4a]/bestaudio/best", None),
        ("melhor áudio disponível", "bestaudio/best", None),
        ("melhor formato único", "best", None),
        ("seleção padrão do yt-dlp", None, None),
    ]


def build_options(
    url: str,
    mode: str,
    playlist: bool,
    cookies: Optional[dict[str, Any]],
    resolution: Optional[int],
    subtitle_language: Optional[str],
    include_automatic_subtitles: bool,
    embed_subtitles: bool,
    mp3_quality: str,
    use_archive: bool,
    ffmpeg_path: Optional[str],
) -> dict[str, Any]:
    options = base_options(
        url=url,
        mode=mode,
        playlist=playlist,
        cookies=cookies,
        use_archive=use_archive,
        ffmpeg_location=ffmpeg_path,
    )
    has_ffmpeg = ffmpeg_path is not None

    if mode in {"video", "video-subs"} and has_ffmpeg:
        # A estratégia de formato escolhe MP4 apenas quando vídeo e áudio
        # são compatíveis. O padrão seguro para combinações genéricas é MKV.
        options["merge_output_format"] = "mkv"

    if mode in {"subtitles", "video-subs"}:
        options.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": include_automatic_subtitles,
                "subtitleslangs": [subtitle_language or "all"],
                "subtitlesformat": "best",
            }
        )

    if mode == "subtitles":
        options["skip_download"] = True

    if mode == "mp3":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": mp3_quality,
            }
        ]

    if mode == "video-subs" and embed_subtitles:
        options.setdefault("postprocessors", []).append(
            {"key": "FFmpegEmbedSubtitle"}
        )
    return options


def run_attempt(
    url: str,
    options: dict[str, Any],
    label: str,
    format_selector: Optional[str],
    merge_output_format: Optional[str] = None,
    use_archive: bool = True,
) -> tuple[bool, list[str]]:
    attempt_options = dict(options)
    logger = DownloadLogger()
    attempt_options["logger"] = logger

    if format_selector is None:
        attempt_options.pop("format", None)
    else:
        attempt_options["format"] = format_selector

    if merge_output_format:
        attempt_options["merge_output_format"] = merge_output_format
    if not use_archive:
        # O yt-dlp pode registrar o ID antes de uma falha na mesclagem. Isso
        # não pode impedir que o fallback tente novamente os arquivos.
        attempt_options.pop("download_archive", None)

    print(f"[*] Tentativa: {label}")
    try:
        with yt_dlp.YoutubeDL(attempt_options) as ydl:
            return_code = ydl.download([url])
    except yt_dlp.utils.DownloadError as error:
        logger.errors.append(str(error))
        return_code = 1
    except KeyboardInterrupt:
        raise
    except Exception as error:
        logger.errors.append(str(error))
        return_code = 1

    errors = list(logger.errors)
    return return_code == 0 and not errors, errors


def download_with_fallback(
    url: str,
    mode: str,
    playlist: bool,
    cookies: Optional[dict[str, Any]] = None,
    resolution: Optional[int] = None,
    subtitle_language: Optional[str] = None,
    include_automatic_subtitles: bool = True,
    embed_subtitles: bool = False,
    mp3_quality: str = "192",
    use_archive: bool = True,
    ffmpeg_location: Optional[str] = None,
) -> bool:
    ffmpeg_path = find_ffmpeg(ffmpeg_location)

    if mode == "mp3" and not ffmpeg_path:
        return require_ffmpeg("converter o áudio para MP3", ffmpeg_location)
    if mode == "video-subs" and embed_subtitles and not ffmpeg_path:
        return require_ffmpeg("incorporar legendas ao vídeo", ffmpeg_location)

    if ffmpeg_path:
        print(f"[+] FFmpeg encontrado: {ffmpeg_path}")
    elif mode == "video-subs":
        print("[aviso] FFmpeg ausente; vídeo e legenda serão salvos separadamente.")
    elif mode in {"video", "audio"}:
        print("[aviso] FFmpeg ausente; serão usados formatos de arquivo único.")

    destination = DOWNLOAD_ROOT / get_domain(url)
    print()
    print("=" * 72)
    print(f"{APP_NAME} {VERSION}")
    print("=" * 72)
    print(f"URL:      {url}")
    print(f"Site:     {get_domain(url)}")
    print(f"Destino:  {destination}")
    print("=" * 72)
    print()

    options = build_options(
        url=url,
        mode=mode,
        playlist=playlist,
        cookies=cookies,
        resolution=resolution,
        subtitle_language=subtitle_language,
        include_automatic_subtitles=include_automatic_subtitles,
        embed_subtitles=embed_subtitles,
        mp3_quality=mp3_quality,
        use_archive=use_archive,
        ffmpeg_path=ffmpeg_path,
    )

    if mode == "subtitles":
        strategies = [("somente legendas", None, None)]
    elif mode in {"video", "video-subs"}:
        strategies = video_format_strategies(resolution, ffmpeg_path is not None)
    else:
        strategies = audio_format_strategies()

    last_errors: list[str] = []
    for index, (label, selector, merge_output_format) in enumerate(strategies):
        try:
            success, errors = run_attempt(
                url,
                options,
                label,
                selector,
                merge_output_format=merge_output_format,
                # O primeiro intento usa o histórico normalmente. Se ele
                # baixar streams e falhar depois, os próximos precisam poder
                # tentar o fallback de verdade.
                use_archive=index == 0,
            )
        except KeyboardInterrupt:
            print("\n[!] Download interrompido pelo usuário.")
            return False

        if success:
            if index > 0:
                print(f"[aviso] Fallback usado com sucesso: {label}.")
                if resolution is not None and mode in {"video", "video-subs"}:
                    print("        A resolução/formato solicitado não estava disponível.")
            return True

        last_errors = errors
        message = "\n".join(errors)
        if index == len(strategies) - 1:
            break
        if not format_error(message):
            print("[ERRO] O download falhou por um motivo que não parece ser formato.")
            if message:
                print(message)
            return False
        print("[aviso] Formato indisponível; tentando o próximo fallback...")

    print("\n[ERRO] Não foi possível concluir o download.")
    if last_errors:
        print(last_errors[-1])
    return False


def download_video(
    url: str,
    playlist: bool,
    cookies: dict[str, Any],
    ffmpeg_location: Optional[str] = None,
    resolution: Optional[int] = None,
    ask_resolution: bool = False,
) -> bool:
    if ask_resolution:
        resolution = select_resolution()
    return download_with_fallback(
        url, "video", playlist, cookies, resolution=resolution,
        ffmpeg_location=ffmpeg_location,
    )


def download_audio(
    url: str,
    playlist: bool,
    cookies: dict[str, Any],
    ffmpeg_location: Optional[str] = None,
) -> bool:
    return download_with_fallback(
        url, "audio", playlist, cookies, ffmpeg_location=ffmpeg_location
    )


def download_mp3(
    url: str,
    playlist: bool,
    cookies: dict[str, Any],
    ffmpeg_location: Optional[str] = None,
    quality: Optional[str] = None,
) -> bool:
    if not require_ffmpeg("converter o áudio para MP3", ffmpeg_location):
        return False
    if quality is None:
        quality = select_mp3_quality()
    return download_with_fallback(
        url, "mp3", playlist, cookies, mp3_quality=quality,
        ffmpeg_location=ffmpeg_location,
    )


def download_subtitles(
    url: str,
    playlist: bool,
    cookies: dict[str, Any],
    ffmpeg_location: Optional[str] = None,
) -> bool:
    language = input(
        "\nIdioma da legenda (ex.: pt-BR, pt, en) [ENTER = todos disponíveis]: "
    ).strip()
    automatic = ask_yes_no(
        "Incluir também legendas automáticas, quando existirem?", default=True
    )
    return download_with_fallback(
        url, "subtitles", playlist, cookies,
        subtitle_language=language or "all",
        include_automatic_subtitles=automatic,
        use_archive=False,
        ffmpeg_location=ffmpeg_location,
    )


def download_video_with_subtitles(
    url: str,
    playlist: bool,
    cookies: dict[str, Any],
    ffmpeg_location: Optional[str] = None,
    resolution: Optional[int] = None,
    ask_resolution: bool = False,
) -> bool:
    if ask_resolution:
        resolution = select_resolution()

    language = input(
        "\nIdioma da legenda (ex.: pt-BR, pt, en) [ENTER = pt.*,en.*]: "
    ).strip()
    embed = ask_yes_no("Incorporar legenda no arquivo de vídeo?", default=False)
    if embed and not require_ffmpeg("incorporar legendas ao vídeo", ffmpeg_location):
        return False

    return download_with_fallback(
        url, "video-subs", playlist, cookies,
        resolution=resolution,
        subtitle_language=language or "pt.*,en.*",
        include_automatic_subtitles=True,
        embed_subtitles=embed,
        ffmpeg_location=ffmpeg_location,
    )


def show_info(url: str, cookies: dict[str, Any]) -> None:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    options.update(cookies)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            print("[!] Não foi possível obter informações.")
            return

        print()
        print("-" * 72)
        print(f"Título:      {info.get('title') or '-'}")
        print(f"Autor/canal: {info.get('uploader') or info.get('channel') or '-'}")
        print(f"Site:        {info.get('extractor_key') or get_domain(url)}")
        duration = info.get("duration")
        if duration is not None:
            hours, rem = divmod(int(duration), 3600)
            minutes, seconds = divmod(rem, 60)
            duration_text = (
                f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                if hours else f"{minutes:02d}:{seconds:02d}"
            )
            print(f"Duração:     {duration_text}")
        print(f"ID:          {info.get('id') or '-'}")
        print("-" * 72)
    except Exception as error:
        print(f"\n[ERRO] Não foi possível consultar a URL:\n{error}")


def main_menu(url: str, ffmpeg_location: Optional[str] = None) -> None:
    while True:
        header()
        print(f"URL atual: {url}")
        print(f"Site:      {get_domain(url)}")
        print(f"FFmpeg:    {'OK' if ffmpeg_available(ffmpeg_location) else 'não encontrado'}")

        mode_choice = choose(
            "Modo",
            {"1": "Vídeo individual", "2": "Playlist / sequência completa"},
            default="1",
        )
        playlist = mode_choice == "2"
        cookies = cookie_options()

        while True:
            header()
            print(f"URL:       {url}")
            print(f"Modo:      {'playlist' if playlist else 'vídeo individual'}")
            print()
            print("  1) Baixar vídeo")
            print("  2) Baixar somente áudio")
            print("  3) Baixar e converter para MP3")
            print("  4) Baixar somente legendas")
            print("  5) Baixar vídeo + legendas")
            print("  6) Mostrar informações do vídeo")
            print("  7) Alterar URL")
            print("  8) Alterar modo/cookies")
            print("  0) Sair")

            option = input("\nEscolha: ").strip()
            if option == "1":
                download_video(
                    url, playlist, cookies, ffmpeg_location, ask_resolution=True
                )
                pause()
            elif option == "2":
                download_audio(url, playlist, cookies, ffmpeg_location)
                pause()
            elif option == "3":
                download_mp3(url, playlist, cookies, ffmpeg_location)
                pause()
            elif option == "4":
                download_subtitles(url, playlist, cookies, ffmpeg_location)
                pause()
            elif option == "5":
                download_video_with_subtitles(
                    url, playlist, cookies, ffmpeg_location, ask_resolution=True
                )
                pause()
            elif option == "6":
                show_info(url, cookies)
                pause()
            elif option == "7":
                new_url = input("\nNova URL: ").strip()
                if valid_url(new_url):
                    url = new_url
                    break
                print("[!] URL inválida.")
                pause()
            elif option == "8":
                break
            elif option == "0":
                print("\nAté mais.")
                return
            else:
                print("[!] Opção inválida.")
                pause()


def parse_resolution(value: str) -> Optional[int]:
    normalized = value.strip().lower().replace("p", "")
    if normalized in {"", "best", "melhor", "0"}:
        return None
    try:
        result = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use uma resolução como 720 ou 1080") from error
    if result < 1:
        raise argparse.ArgumentTypeError("a resolução deve ser positiva")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Downloader baseado em yt-dlp.")
    parser.add_argument("url", nargs="?", help="URL do vídeo ou playlist")
    parser.add_argument(
        "--mode",
        choices=("video", "audio", "mp3", "subtitles", "video-subs", "info"),
        help="modo não interativo; sem esta opção, abre o menu interativo",
    )
    parser.add_argument("--resolution", type=parse_resolution, help="máxima, como 1080")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--playlist", action="store_true", help="baixar playlist inteira")
    group.add_argument("--single", action="store_true", help="baixar somente um item")
    parser.add_argument("--subtitle-lang", default="all", help="idiomas das legendas")
    parser.add_argument("--no-auto-subs", action="store_true", help="não baixar legendas automáticas")
    parser.add_argument("--embed-subs", action="store_true", help="incorporar legendas no vídeo")
    parser.add_argument("--mp3-quality", choices=("128", "192", "256", "320"), default="192")
    parser.add_argument("--cookies", type=Path, help="arquivo cookies.txt em formato Netscape")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER", help="ex.: chrome, firefox")
    parser.add_argument("--ffmpeg-location", help="pasta ou caminho do FFmpeg")
    parser.add_argument("--interactive", action="store_true", help="força o menu interativo")
    return parser


def cli_download(args: argparse.Namespace, url: str) -> int:
    if args.cookies:
        cookies: dict[str, Any] = {"cookiefile": str(args.cookies)}
    elif args.cookies_from_browser:
        cookies = {"cookiesfrombrowser": (args.cookies_from_browser,)}
    elif COOKIES_FILE.is_file():
        cookies = {"cookiefile": str(COOKIES_FILE)}
    else:
        cookies = {}

    playlist = bool(args.playlist)
    if args.single:
        playlist = False
    mode = args.mode or "video"
    if mode == "info":
        show_info(url, cookies)
        return 0

    success = download_with_fallback(
        url=url,
        mode=mode,
        playlist=playlist,
        cookies=cookies,
        resolution=args.resolution,
        subtitle_language=args.subtitle_lang,
        include_automatic_subtitles=not args.no_auto_subs,
        embed_subtitles=args.embed_subs,
        mp3_quality=args.mp3_quality,
        use_archive=mode != "subtitles",
        ffmpeg_location=args.ffmpeg_location,
    )
    return 0 if success else 1


def main() -> int:
    args = build_parser().parse_args()
    # Permite que --help funcione mesmo antes de instalar a dependência.
    if not require_yt_dlp():
        return 1
    header()
    print("YouTube, Instagram, X/Twitter, TikTok e outros sites compatíveis com yt-dlp.")
    print(f"\nDownloads serão armazenados em:\n{DOWNLOAD_ROOT}")

    url = args.url
    if not url:
        try:
            url = input("\nURL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Operação cancelada.")
            return 1

    if not valid_url(url):
        print("\n[ERRO] Informe uma URL válida iniciando com http:// ou https://")
        return 1

    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    has_cli_options = any(
        (
            args.mode,
            args.resolution is not None,
            args.playlist,
            args.single,
            args.cookies,
            args.cookies_from_browser,
            args.ffmpeg_location,
            args.interactive,
            args.embed_subs,
            args.no_auto_subs,
            args.subtitle_lang != "all",
            args.mp3_quality != "192",
        )
    )

    if has_cli_options and not args.interactive:
        return cli_download(args, url)
    main_menu(url, args.ffmpeg_location)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
