#!/usr/bin/env python3
"""Compacta vídeos com ffmpeg sem alterar suas proporções.

Exemplos:
    python video-compressor.py video.mkv
    python video-compressor.py video.mov --profile pequeno -o video.mp4
    python video-compressor.py video.avi --target-size 80 --force
    python video-compressor.py video.mp4 --estimate-only --profile equilibrada

Os perfis usam CRF: a qualidade visual tende a ser mais consistente, mas o
tamanho final não é conhecido antes da codificação. Para controlar diretamente
o tamanho, use --target-size, que executa uma codificação em duas passagens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


APP_NAME = "VIDEO COMPRESSOR"
VERSION = "1.0"


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    crf: int
    preset: str
    audio_bitrate: str
    expected_reduction: tuple[int, int]


PROFILES = {
    "alta": Profile(
        "alta", "melhor qualidade; arquivo maior", 22, "medium", "160k", (25, 55)
    ),
    "equilibrada": Profile(
        "equilibrada", "recomendado para a maioria dos vídeos", 27, "medium", "128k", (40, 70)
    ),
    "pequeno": Profile(
        "pequeno", "prioriza tamanho reduzido", 31, "slow", "96k", (55, 85)
    ),
    "minimo": Profile(
        "minimo", "menor arquivo; perda visual mais perceptível", 35, "slow", "64k", (65, 92)
    ),
}

PROFILE_ALIASES = {
    "alta qualidade": "alta",
    "quality": "alta",
    "high": "alta",
    "balanceada": "equilibrada",
    "balanced": "equilibrada",
    "medium": "equilibrada",
    "small": "pequeno",
    "tiny": "minimo",
}

VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ts", ".vob", ".webm", ".wmv",
}
VALID_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
    "slow", "slower", "veryslow", "placebo",
}


class CompressorError(RuntimeError):
    """Erro esperado apresentado sem traceback ao usuário."""


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


def choose(prompt: str, options: dict[str, str], default: Optional[str] = None) -> str:
    while True:
        print()
        for key, label in options.items():
            suffix = " [padrão]" if default == key else ""
            print(f"  {key}) {label}{suffix}")
        try:
            value = input(f"\n{prompt}: ").strip().lower()
        except EOFError as exc:
            raise CompressorError("Entrada interativa encerrada.") from exc
        if not value and default is not None:
            return default
        if value in options:
            return value
        print("[!] Opção inválida.")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [S/n]" if default else " [s/N]"
    while True:
        try:
            answer = input(prompt + suffix + ": ").strip().lower()
        except EOFError as exc:
            raise CompressorError("Entrada interativa encerrada.") from exc
        if not answer:
            return default
        if answer in ("s", "sim", "y", "yes"):
            return True
        if answer in ("n", "nao", "não", "no"):
            return False
        print("[!] Responda com S ou N.")


def ask_number(prompt: str, *, minimum: float = 0) -> float:
    while True:
        try:
            value = float(input(f"{prompt}: ").strip().replace(",", "."))
        except EOFError as exc:
            raise CompressorError("Entrada interativa encerrada.") from exc
        except ValueError:
            print("[!] Digite um número válido.")
            continue
        if value > minimum:
            return value
        print(f"[!] O valor deve ser maior que {minimum}.")


def select_input_file() -> Path:
    candidates = sorted(
        (item for item in Path.cwd().iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: item.name.lower(),
    )
    if candidates:
        print("\nVídeos encontrados na pasta atual:")
        for index, candidate in enumerate(candidates, start=1):
            print(f"  {index}) {candidate.name}")
        print("  0) Digitar outro caminho")
        selection = input("\nSelecione o vídeo ou informe o caminho: ").strip().strip('"')
        if selection.isdigit() and 1 <= int(selection) <= len(candidates):
            return candidates[int(selection) - 1].resolve()
    else:
        selection = input("\nCaminho do vídeo: ").strip().strip('"')
    if not selection or selection == "0":
        raise CompressorError("Nenhum arquivo de entrada foi informado.")
    return Path(selection).expanduser().resolve()


def print_profiles() -> None:
    print("\nPerfis de compactação:")
    for profile in PROFILES.values():
        low, high = profile.expected_reduction
        print(
            f"  {profile.name:12} CRF {profile.crf:2} | {profile.description}; "
            f"redução estimada de {low}% a {high}%"
        )
    print("\nCRF menor preserva mais qualidade e normalmente gera arquivos maiores.")


def interactive_arguments(action: str, input_path: Path) -> argparse.Namespace:
    profile_name = choose(
        "Perfil",
        {key: profile.description for key, profile in PROFILES.items()},
        default="equilibrada",
    )
    args = argparse.Namespace(
        input=input_path,
        output=None,
        profile=profile_name,
        crf=None,
        preset=None,
        audio_bitrate=None,
        no_audio=False,
        target_size=None,
        estimate_only=action == "3",
        force=False,
    )
    if action == "2":
        args.target_size = ask_number("Tamanho alvo aproximado em MB", minimum=0)
    if action == "3":
        return args

    default_output = output_path(input_path, None)
    output_value = input(
        f"\nArquivo de saída [ENTER para {default_output.name}]: "
    ).strip().strip('"')
    if output_value:
        args.output = Path(output_value).expanduser()
    args.no_audio = ask_yes_no("Remover o áudio?", default=False)
    if ask_yes_no("Alterar opções avançadas?", default=False):
        while True:
            crf_value = input("CRF (ENTER para usar o perfil): ").strip()
            if not crf_value:
                break
            try:
                args.crf = int(crf_value)
                break
            except ValueError:
                print("[!] Digite um número inteiro para o CRF.")
        args.preset = choose(
            "Preset do encoder",
            {preset: preset for preset in sorted(VALID_PRESETS)},
            default=PROFILES[profile_name].preset,
        )
        audio_options = {bitrate: bitrate for bitrate in ("64k", "96k", "128k", "160k", "192k", "256k")}
        args.audio_bitrate = choose(
            "Bitrate do áudio",
            audio_options,
            default=PROFILES[profile_name].audio_bitrate,
        )
    selected_output = output_path(input_path, args.output)
    if selected_output.exists():
        args.force = ask_yes_no(f"Substituir {selected_output}?", default=False)
    return args


def human_size(size: Optional[float]) -> str:
    if size is None:
        return "desconhecido"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def find_executable(name: str) -> Optional[str]:
    return shutil.which(name) or shutil.which(f"{name}.exe")


def run_command(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except OSError as exc:
        raise CompressorError(f"Não foi possível executar {command[0]}: {exc}") from exc


def probe_video(path: Path, ffprobe: Optional[str], ffmpeg: str) -> dict:
    """Obtém duração, dimensões, bitrate e proporção sem depender de pacote Python."""
    if ffprobe:
        result = run_command(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_entries", "format=duration,size,bit_rate:stream=codec_type,width,height,bit_rate,sample_aspect_ratio",
                str(path),
            ],
            capture=True,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout or "{}")
                streams = data.get("streams", [])
                video = next((item for item in streams if item.get("codec_type") == "video"), {})
                duration = float(data.get("format", {}).get("duration") or 0)
                size = float(data.get("format", {}).get("size") or path.stat().st_size)
                bitrate = float(data.get("format", {}).get("bit_rate") or 0)
                return {
                    "duration": duration,
                    "size": size,
                    "bitrate": bitrate,
                    "width": video.get("width"),
                    "height": video.get("height"),
                    "sar": video.get("sample_aspect_ratio") or "1:1",
                }
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

    # Fallback para instalações que só têm ffmpeg no PATH.
    result = run_command([ffmpeg, "-i", str(path)], capture=True)
    diagnostic = (result.stderr or "") + (result.stdout or "")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", diagnostic)
    dimension_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})(?:\s|,)", diagnostic)
    bitrate_match = re.search(r"bitrate:\s*(\d+)\s*kb/s", diagnostic)
    if not duration_match:
        raise CompressorError(
            "Não foi possível ler a duração do vídeo. Verifique se o arquivo é válido "
            "e se o ffmpeg consegue abri-lo."
        )
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "duration": duration,
        "size": float(path.stat().st_size),
        "bitrate": float(bitrate_match.group(1)) * 1000 if bitrate_match else 0,
        "width": int(dimension_match.group(1)) if dimension_match else None,
        "height": int(dimension_match.group(2)) if dimension_match else None,
        "sar": "1:1",
    }


def normalize_profile(value: str) -> str:
    normalized = value.strip().lower()
    normalized = PROFILE_ALIASES.get(normalized, normalized)
    if normalized not in PROFILES:
        valid = ", ".join(PROFILES)
        raise CompressorError(f"Perfil inválido: {value}. Escolha entre: {valid}.")
    return normalized


def output_path(input_path: Path, requested: Optional[Path]) -> Path:
    if requested:
        return requested if requested.suffix else requested.with_suffix(".mp4")
    return input_path.with_name(f"{input_path.stem}_compressed.mp4")


def validate_output(input_path: Path, output: Path, force: bool) -> None:
    if input_path.resolve() == output.resolve():
        raise CompressorError("A saída deve ser diferente do arquivo de entrada.")
    if output.exists() and not force:
        raise CompressorError(
            f"O arquivo de saída já existe: {output}\nUse --force para substituí-lo."
        )
    output.parent.mkdir(parents=True, exist_ok=True)


def estimate_size(info: dict, profile: Profile) -> tuple[float, float]:
    """Retorna faixa heurística; CRF não permite prever tamanho com precisão."""
    source_size = info["size"]
    low_reduction, high_reduction = profile.expected_reduction
    return (
        source_size * (1 - high_reduction / 100),
        source_size * (1 - low_reduction / 100),
    )


def print_video_info(info: dict) -> None:
    dimensions = "desconhecidas"
    if info.get("width") and info.get("height"):
        dimensions = f"{info['width']}x{info['height']}"
    print(f"[i] Origem: {human_size(info['size'])} | {info['duration'] / 60:.1f} min | {dimensions}")
    if info.get("width") and info.get("height"):
        print("[i] Proporção: preservada (nenhum redimensionamento será aplicado)")


def codec_arguments(profile: Profile, args: argparse.Namespace) -> list[str]:
    crf = args.crf if args.crf is not None else profile.crf
    preset = args.preset or profile.preset
    audio_bitrate = args.audio_bitrate or profile.audio_bitrate
    arguments = [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]
    if not args.no_audio:
        arguments += ["-c:a", "aac", "-b:a", audio_bitrate]
    return arguments


def common_arguments(
    input_path: Path,
    output: Path,
    args: argparse.Namespace,
    *,
    include_faststart: bool = True,
) -> list[str]:
    arguments = ["-hide_banner", "-nostdin", "-i", str(input_path), "-map", "0:v:0"]
    if args.no_audio:
        arguments += ["-an"]
    else:
        arguments += ["-map", "0:a:0?"]
    arguments += ["-map_metadata", "0", "-map_chapters", "0"]
    if include_faststart and output.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        arguments += ["-movflags", "+faststart"]
    arguments += ["-y" if args.force else "-n", str(output)]
    return arguments


def build_crf_command(ffmpeg: str, input_path: Path, output: Path, profile: Profile, args: argparse.Namespace) -> list[str]:
    return [ffmpeg] + common_arguments(input_path, output, args)[:-2] + codec_arguments(profile, args) + common_arguments(input_path, output, args)[-2:]


def run_crf(ffmpeg: str, input_path: Path, output: Path, profile: Profile, args: argparse.Namespace) -> None:
    command = build_crf_command(ffmpeg, input_path, output, profile, args)
    print(f"[i] Codificação CRF {args.crf if args.crf is not None else profile.crf} / preset {args.preset or profile.preset}...")
    result = run_command(command)
    if result.returncode != 0:
        raise CompressorError("O ffmpeg não conseguiu compactar o vídeo. Consulte a mensagem acima.")


def target_video_bitrate(
    info: dict,
    target_mb: float,
    audio_bitrate: str,
    *,
    no_audio: bool = False,
) -> int:
    if info["duration"] <= 0:
        raise CompressorError("A duração do vídeo é inválida para calcular --target-size.")
    audio_kbps = 0 if no_audio else float(re.sub(r"[^0-9.]", "", audio_bitrate) or 0)
    total_kbps = target_mb * 1024 * 8 / info["duration"]
    video_kbps = int(total_kbps * 0.96 - audio_kbps)
    if video_kbps < 100:
        raise CompressorError(
            "O tamanho alvo é pequeno demais para a duração do vídeo. "
            "Aumente --target-size ou reduza --audio-bitrate."
        )
    return video_kbps


def run_target_size(ffmpeg: str, input_path: Path, output: Path, profile: Profile, args: argparse.Namespace, info: dict) -> None:
    audio_bitrate = args.audio_bitrate or profile.audio_bitrate
    video_kbps = target_video_bitrate(
        info, args.target_size, audio_bitrate, no_audio=args.no_audio
    )
    pass_dir = Path(tempfile.mkdtemp(prefix="video-compressor-"))
    passlog = pass_dir / "ffmpeg-pass"
    null_output = "NUL" if os.name == "nt" else "/dev/null"
    base = common_arguments(input_path, output, args)
    first_base = common_arguments(input_path, output, args, include_faststart=False)
    # A primeira passagem não grava o arquivo final; a segunda usa o bitrate calculado.
    first = [ffmpeg] + first_base[:-2] + [
        "-c:v", "libx264", "-preset", args.preset or profile.preset,
        "-b:v", f"{video_kbps}k", "-pass", "1", "-passlogfile", str(passlog),
        "-an", "-f", "mp4", "-y", null_output,
    ]
    audio_arguments = [] if args.no_audio else ["-c:a", "aac", "-b:a", audio_bitrate]
    second = [ffmpeg] + base[:-2] + [
        "-c:v", "libx264", "-preset", args.preset or profile.preset,
        "-b:v", f"{video_kbps}k", "-pass", "2", "-passlogfile", str(passlog),
        *audio_arguments, "-pix_fmt", "yuv420p",
    ] + base[-2:]
    try:
        print(f"[i] Codificação em duas passagens para aproximadamente {human_size(args.target_size * 1024 * 1024)}...")
        first_result = run_command(first)
        if first_result.returncode != 0:
            raise CompressorError("A primeira passagem do ffmpeg falhou.")
        second_result = run_command(second)
        if second_result.returncode != 0:
            raise CompressorError("A segunda passagem do ffmpeg falhou.")
    finally:
        shutil.rmtree(pass_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    profile_help = " | ".join(f"{key}: {value.description}" for key, value in PROFILES.items())
    parser = argparse.ArgumentParser(
        description="Compacta vídeos com ffmpeg preservando largura, altura e proporção original.",
        epilog=(
            "Perfis: " + profile_help + ". Estimativas são aproximadas; conteúdo, codec e duração influenciam o resultado."
        ),
    )
    parser.add_argument("input", type=Path, help="arquivo de vídeo de entrada")
    parser.add_argument("-o", "--output", type=Path, help="arquivo de saída; padrão: <nome>_compressed.mp4")
    parser.add_argument("--profile", default="equilibrada", help="perfil de compactação (padrão: equilibrada)")
    parser.add_argument("--crf", type=int, help="CRF personalizado, de 18 (melhor) a 40 (menor)")
    parser.add_argument("--preset", choices=sorted(VALID_PRESETS), help="velocidade/eficiência do encoder")
    parser.add_argument("--audio-bitrate", help="bitrate do áudio, por exemplo 96k, 128k ou 192k")
    parser.add_argument("--no-audio", action="store_true", help="remove todas as faixas de áudio")
    parser.add_argument("--target-size", type=float, metavar="MB", help="tamanho alvo aproximado; usa duas passagens")
    parser.add_argument("--estimate-only", action="store_true", help="apenas mostra a estimativa e não codifica")
    parser.add_argument("--force", action="store_true", help="substitui o arquivo de saída se ele existir")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def process_arguments(args: argparse.Namespace) -> int:
    try:
        input_path = args.input.expanduser().resolve()
        if not input_path.is_file():
            raise CompressorError(f"Arquivo de entrada não encontrado: {input_path}")
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
            print(f"[aviso] Extensão não reconhecida ({input_path.suffix or 'sem extensão'}); tentando abrir mesmo assim.")
        if args.crf is not None and not 18 <= args.crf <= 40:
            raise CompressorError("--crf deve estar entre 18 e 40.")
        if args.target_size is not None and args.target_size <= 0:
            raise CompressorError("--target-size deve ser maior que zero.")
        profile_name = normalize_profile(args.profile)
        profile = PROFILES[profile_name]
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
        if not ffmpeg:
            raise CompressorError("ffmpeg não encontrado no PATH. Instale-o e tente novamente.")
        info = probe_video(input_path, ffprobe, ffmpeg)
        print_video_info(info)
        output = output_path(input_path, args.output)
        if not args.estimate_only:
            validate_output(input_path, output, args.force)

        if args.target_size is not None:
            estimate_low = estimate_high = args.target_size * 1024 * 1024
            print(f"[i] Estimativa: aproximadamente {human_size(estimate_low)} (controle por bitrate)")
        else:
            estimate_low, estimate_high = estimate_size(info, profile)
            print(
                f"[i] Estimativa ({profile.name}): {human_size(estimate_low)} a "
                f"{human_size(estimate_high)}; é uma faixa aproximada."
            )
        if args.estimate_only:
            return 0

        if args.target_size is not None:
            run_target_size(ffmpeg, input_path, output, profile, args, info)
        else:
            run_crf(ffmpeg, input_path, output, profile, args)
        actual_size = output.stat().st_size if output.exists() else None
        print(f"[+] Concluído: {output}")
        print(f"[+] Tamanho final: {human_size(actual_size)}")
        if actual_size and info["size"]:
            reduction = (1 - actual_size / info["size"]) * 100
            print(f"[+] Redução: {reduction:.1f}%")
        return 0
    except CompressorError as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[interrompido] Operação cancelada.", file=sys.stderr)
        return 130


def interactive_mode() -> int:
    """Executa o fluxo de menus usado quando nenhum parâmetro foi informado."""
    while True:
        try:
            header()
            action = choose(
                "Comando",
                {
                    "1": "Compactar usando um perfil de qualidade",
                    "2": "Compactar para um tamanho alvo",
                    "3": "Estimar o tamanho final",
                    "4": "Consultar perfis",
                    "0": "Sair",
                },
            )
            if action == "0":
                print("Até logo!")
                return 0
            if action == "4":
                print_profiles()
                pause()
                continue
            input_path = select_input_file()
            args = interactive_arguments(action, input_path)
            result = process_arguments(args)
            pause()
        except (EOFError, KeyboardInterrupt):
            print("\nAté logo!")
            return 0
        except CompressorError as exc:
            print(f"[ERRO] {exc}", file=sys.stderr)
            pause()


def main(argv: Optional[list[str]] = None) -> int:
    raw_arguments = sys.argv[1:] if argv is None else argv
    if not raw_arguments:
        return interactive_mode()
    args = build_parser().parse_args(raw_arguments)
    return process_arguments(args)


if __name__ == "__main__":
    raise SystemExit(main())
