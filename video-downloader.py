#!/usr/bin/env python3
"""
VIDEO DOWNLOADER 1.0

Downloader interativo baseado em yt-dlp.

Recursos:
- Vídeo na melhor qualidade
- Escolha de resolução máxima
- Áudio original
- Conversão para MP3
- Legendas
- Vídeo individual ou playlist
- Organização automática por site/uploader
- Arquivo de histórico para evitar downloads duplicados
- Cookies opcionais (arquivo cookies.txt ou navegador)
- FFmpeg usado pelo yt-dlp quando necessário

Uso:
    python downloader.py
    python downloader.py "https://..."
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import yt_dlp
except ImportError:
    print("\n[ERRO] O pacote yt-dlp não está instalado.")
    print("Instale com:")
    print("    python -m pip install -U yt-dlp")
    raise SystemExit(1)


APP_NAME = "VIDEO DOWNLOADER"
VERSION = "1.0"

SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = SCRIPT_DIR / "downloads"
ARCHIVE_FILE = DOWNLOAD_ROOT / ".download_archive.txt"
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


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def choose(prompt: str, options: dict[str, str], default: str | None = None) -> str:
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


def progress_hook(data: dict) -> None:
    if data.get("status") == "finished":
        filename = data.get("filename")
        if filename:
            print(f"\n[+] Download recebido: {filename}")
        print("[+] Finalizando/processando arquivo...")


def postprocessor_hook(data: dict) -> None:
    if data.get("status") == "finished":
        pp = data.get("postprocessor")
        if pp:
            print(f"[+] Processamento concluído: {pp}")


def cookie_options() -> dict:
    """
    Retorna configuração de cookies opcional.

    Prioridade:
    1. cookies.txt ao lado do script, se existir.
    2. navegador escolhido pelo usuário.
    3. sem cookies.
    """
    if COOKIES_FILE.is_file():
        print(f"\n[i] Arquivo de cookies encontrado: {COOKIES_FILE.name}")
        if ask_yes_no("Usar esse arquivo de cookies?", default=True):
            return {"cookiefile": str(COOKIES_FILE)}

    use_browser = ask_yes_no(
        "Tentar importar cookies de um navegador instalado?",
        default=False,
    )
    if not use_browser:
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
    use_archive: bool = True,
) -> dict:
    domain = get_domain(url)
    destination = DOWNLOAD_ROOT / domain
    destination.mkdir(parents=True, exist_ok=True)

    # Para playlists, adiciona uma pasta com o título da playlist.
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

    opts = {
        "outtmpl": str(template),
        "windowsfilenames": True,
        "ignoreerrors": True,
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
        opts["download_archive"] = str(ARCHIVE_FILE)

    return opts


def require_ffmpeg(reason: str) -> bool:
    if ffmpeg_available():
        return True

    print()
    print("[ERRO] FFmpeg não foi encontrado no PATH.")
    print(f"Ele é necessário para {reason}.")
    print()
    print("Windows:")
    print("  Instale o FFmpeg e adicione-o ao PATH.")
    print()
    print("Termux/Android:")
    print("  pkg install ffmpeg")
    return False


def select_resolution() -> str:
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

    heights = {
        "2": 2160,
        "3": 1440,
        "4": 1080,
        "5": 720,
        "6": 480,
        "7": 360,
    }

    if selected == "1":
        return "bv*+ba/b"

    height = heights[selected]
    return (
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}]/"
        "bv*+ba/b"
    )


def download_video(url: str, playlist: bool, cookies: dict) -> None:
    opts = base_options(url, playlist)
    opts.update(cookies)
    opts["format"] = select_resolution()

    if ffmpeg_available():
        opts["merge_output_format"] = "mp4"
    else:
        print()
        print("[AVISO] FFmpeg não foi encontrado.")
        print("O yt-dlp tentará baixar um formato único com áudio e vídeo.")
        opts["format"] = "b"

    run_download(url, opts)


def download_audio(url: str, playlist: bool, cookies: dict) -> None:
    opts = base_options(url, playlist)
    opts.update(cookies)
    opts["format"] = "ba/b"
    run_download(url, opts)


def download_mp3(url: str, playlist: bool, cookies: dict) -> None:
    if not require_ffmpeg("converter o áudio para MP3"):
        return

    qualities = {
        "1": "320 kbps",
        "2": "256 kbps",
        "3": "192 kbps",
        "4": "128 kbps",
    }
    selected = choose("Qualidade do MP3", qualities, default="1")
    quality_map = {
        "1": "320K",
        "2": "256K",
        "3": "192K",
        "4": "128K",
    }

    opts = base_options(url, playlist)
    opts.update(cookies)
    opts["format"] = "ba/b"
    opts["postprocessors"] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality_map[selected],
        }
    ]
    run_download(url, opts)


def download_subtitles(url: str, playlist: bool, cookies: dict) -> None:
    language = input(
        "\nIdioma da legenda (ex.: pt-BR, pt, en) "
        "[ENTER = todos disponíveis]: "
    ).strip()

    opts = base_options(url, playlist, use_archive=False)
    opts.update(cookies)
    opts["skip_download"] = True
    opts["writesubtitles"] = True

    automatic = ask_yes_no(
        "Incluir também legendas automáticas, quando existirem?",
        default=True,
    )
    opts["writeautomaticsub"] = automatic

    if language:
        opts["subtitleslangs"] = [language]
    else:
        opts["subtitleslangs"] = ["all"]

    opts["subtitlesformat"] = "best"
    run_download(url, opts)


def download_video_with_subtitles(
    url: str,
    playlist: bool,
    cookies: dict,
) -> None:
    opts = base_options(url, playlist)
    opts.update(cookies)
    opts["format"] = select_resolution()
    opts["writesubtitles"] = True
    opts["writeautomaticsub"] = True

    language = input(
        "\nIdioma da legenda (ex.: pt-BR, pt, en) "
        "[ENTER = pt.*,en.*]: "
    ).strip()

    opts["subtitleslangs"] = [language] if language else ["pt.*", "en.*"]

    embed = ask_yes_no("Incorporar legenda no arquivo de vídeo?", default=False)

    if embed:
        if not require_ffmpeg("incorporar legendas ao vídeo"):
            return
        opts["embedsubtitles"] = True

    if ffmpeg_available():
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "b"

    run_download(url, opts)


def show_info(url: str, cookies: dict) -> None:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    opts.update(cookies)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            print("[!] Não foi possível obter informações.")
            return

        print()
        print("-" * 72)
        print(f"Título:      {info.get('title') or '-'}")
        print(
            "Autor/canal: "
            f"{info.get('uploader') or info.get('channel') or '-'}"
        )
        print(f"Site:        {info.get('extractor_key') or get_domain(url)}")

        duration = info.get("duration")
        if duration is not None:
            duration = int(duration)
            hours, rem = divmod(duration, 3600)
            minutes, seconds = divmod(rem, 60)
            if hours:
                duration_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                duration_text = f"{minutes:02d}:{seconds:02d}"
            print(f"Duração:     {duration_text}")

        print(f"ID:          {info.get('id') or '-'}")
        print("-" * 72)

    except Exception as exc:
        print(f"\n[ERRO] Não foi possível consultar a URL:\n{exc}")


def run_download(url: str, opts: dict) -> None:
    domain = get_domain(url)

    print()
    print("=" * 72)
    print("INICIANDO")
    print("=" * 72)
    print(f"URL:      {url}")
    print(f"Site:     {domain}")
    print(f"Destino:  {DOWNLOAD_ROOT / domain}")
    print("=" * 72)
    print()

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            error_code = ydl.download([url])

        print()
        if error_code:
            print("[!] O yt-dlp informou que um ou mais itens falharam.")
        else:
            print("[+] Operação concluída.")

    except yt_dlp.utils.DownloadError as exc:
        print()
        print("[ERRO] O download não pôde ser concluído.")
        print(exc)

    except KeyboardInterrupt:
        print("\n[!] Operação interrompida pelo usuário.")

    except Exception as exc:
        print()
        print("[ERRO] Ocorreu um erro inesperado:")
        print(exc)


def main_menu(url: str) -> None:
    while True:
        header()
        print(f"URL atual: {url}")
        print(f"Site:      {get_domain(url)}")
        print(f"FFmpeg:    {'OK' if ffmpeg_available() else 'não encontrado'}")

        mode = choose(
            "Modo",
            {
                "1": "Vídeo individual",
                "2": "Playlist / sequência completa",
            },
            default="1",
        )
        playlist = mode == "2"

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
                download_video(url, playlist, cookies)
                pause()
            elif option == "2":
                download_audio(url, playlist, cookies)
                pause()
            elif option == "3":
                download_mp3(url, playlist, cookies)
                pause()
            elif option == "4":
                download_subtitles(url, playlist, cookies)
                pause()
            elif option == "5":
                download_video_with_subtitles(url, playlist, cookies)
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


def main() -> None:
    header()
    print("YouTube, Instagram, X/Twitter, TikTok e outros sites")
    print("compatíveis com o yt-dlp.")
    print()
    print(f"Downloads serão armazenados em:\n{DOWNLOAD_ROOT}")

    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        print()
        url = input("URL: ").strip()

    if not valid_url(url):
        print("\n[ERRO] Informe uma URL válida iniciando com http:// ou https://")
        raise SystemExit(1)

    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    main_menu(url)


if __name__ == "__main__":
    main()
