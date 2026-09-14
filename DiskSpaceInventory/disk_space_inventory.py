#!/usr/bin/env python3
"""Inventário de espaço em disco para Windows.

Varre uma unidade ou diretório, incluindo arquivos ocultos, e gera um
relatório dos maiores arquivos, diretórios mais pesados e diretórios com
muitos arquivos. Também inspeciona diretórios temporários conhecidos do
Windows quando executado nesse sistema.

O programa é deliberadamente somente leitura: ele nunca exclui arquivos.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from shutil import disk_usage
from typing import Iterable, Optional


DEFAULT_TOP = 30
DEFAULT_MIN_FILE_SIZE = 500 * 1024 * 1024
DEFAULT_MIN_DIR_SIZE = 1024 * 1024 * 1024
DEFAULT_MIN_FILE_COUNT = 10_000
DEFAULT_TEMP_AGE_DAYS = 7

TEMP_EXTENSIONS = {".tmp", ".temp", ".dmp", ".wer"}


@dataclass
class FileRecord:
    path: str
    size: int
    modified: Optional[str]
    hidden: bool
    temporary_by_location: bool = False
    temporary_by_name: bool = False


@dataclass
class DirectoryRecord:
    path: str
    size: int = 0
    file_count: int = 0
    directory_count: int = 0
    direct_size: int = 0
    direct_file_count: int = 0
    errors: int = 0
    hidden: bool = False


@dataclass
class ScanResult:
    root: str
    files: list[FileRecord] = field(default_factory=list)
    directories: list[DirectoryRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    files_scanned: int = 0
    directories_scanned: int = 0
    bytes_scanned: int = 0
    started_at: str = ""
    finished_at: str = ""


def parse_size(value: str) -> int:
    """Converte 500MB, 1.5GB ou um número de bytes para inteiro."""
    text = value.strip().upper().replace(",", ".")
    units = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }
    for unit in sorted(units, key=len, reverse=True):
        if text.endswith(unit):
            number = text[: -len(unit)].strip()
            try:
                return max(0, int(float(number) * units[unit]))
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    f"Tamanho inválido: {value!r}. Use, por exemplo, 500MB ou 1GB."
                ) from exc
    try:
        return max(0, int(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Tamanho inválido: {value!r}. Use, por exemplo, 500MB ou 1GB."
        ) from exc


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.1f} {unit}".replace(",", "X").replace(".", ",").replace("X", ".")
        value /= 1024
    return f"{size} B"


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def iso_datetime(timestamp: float) -> Optional[str]:
    try:
        return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def is_reparse_point(path: Path, entry_stat: Optional[os.stat_result] = None) -> bool:
    """Evita seguir junctions/symlinks e criar loops durante a varredura."""
    try:
        mode = entry_stat.st_mode if entry_stat is not None else path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if os.name == "nt" and hasattr(ctypes, "windll"):
            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attributes != -1 and attributes & reparse_flag:
                return True
    except (OSError, AttributeError):
        return True
    return False


def windows_hidden(path: Path, entry_stat: Optional[os.stat_result] = None) -> bool:
    """Retorna se o item tem o atributo Hidden do Windows ou começa com ponto."""
    if path.name.startswith("."):
        return True
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return False
    try:
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != -1 and bool(attributes & 0x2)
    except (OSError, AttributeError):
        return False


def normalized(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except OSError:
        return os.path.normcase(str(path.absolute()))


def iter_windows_temp_locations() -> list[tuple[Path, str, str]]:
    """Retorna diretórios conhecidos, com descrição e orientação de limpeza."""
    if os.name != "nt":
        return []

    locations: list[tuple[Path, str, str]] = []

    def add(raw: Optional[str], label: str, guidance: str) -> None:
        if not raw:
            return
        path = Path(os.path.expandvars(raw)).expanduser()
        if normalized(path) not in {normalized(item[0]) for item in locations}:
            locations.append((path, label, guidance))

    add(os.environ.get("TEMP"), "TEMP do usuário (%TEMP%)", "Normalmente pode ser limpo com os aplicativos fechados.")
    add(os.environ.get("TMP"), "TMP do usuário (%TMP%)", "Normalmente pode ser limpo com os aplicativos fechados.")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    add(str(Path(system_root) / "Temp"), "TEMP do Windows", "Pode exigir administrador; não remova itens em uso.")
    add(
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "WER", "ReportArchive"),
        "Relatórios antigos do Windows Error Reporting",
        "São diagnósticos; remova apenas se não precisar deles para investigação.",
    )
    add(
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "WER", "ReportQueue"),
        "Fila do Windows Error Reporting",
        "Cuidado: pode conter relatórios ainda pendentes; prefira a Limpeza de Disco.",
    )
    add(
        os.path.join(system_root, "SoftwareDistribution", "Download"),
        "Cache de downloads do Windows Update",
        "Cuidado: limpe preferencialmente pelas ferramentas do Windows e com o serviço parado.",
    )
    return locations


def is_under(path: Path, roots: Iterable[Path]) -> bool:
    current = normalized(path)
    return any(current == normalized(root) or current.startswith(normalized(root) + os.sep) for root in roots)


def likely_temp_by_name(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in TEMP_EXTENSIONS or name.startswith(("~$", "tmp", "temp"))


def scan_directory(root: Path, temp_roots: Iterable[Path] = ()) -> ScanResult:
    result = ScanResult(root=str(root), started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    records: dict[str, DirectoryRecord] = {}
    parent_by_key: dict[str, Optional[str]] = {}
    stack = [root]
    temp_roots = list(temp_roots)

    root_key = normalized(root)
    records[root_key] = DirectoryRecord(path=str(root), hidden=windows_hidden(root))
    parent_by_key[root_key] = None

    while stack:
        current = stack.pop()
        current_key = normalized(current)
        current_record = records[current_key]
        result.directories_scanned += 1
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            if is_reparse_point(entry_path, entry_stat):
                                current_record.errors += 1
                                result.errors.append(f"Ignorado junction/link: {entry_path}")
                                continue
                            key = normalized(entry_path)
                            if key in records:
                                current_record.errors += 1
                                result.errors.append(f"Diretório repetido ignorado: {entry_path}")
                                continue
                            records[key] = DirectoryRecord(
                                path=str(entry_path),
                                hidden=windows_hidden(entry_path, entry_stat),
                            )
                            parent_by_key[key] = current_key
                            current_record.directory_count += 1
                            stack.append(entry_path)
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue
                        size = max(0, int(entry_stat.st_size))
                        hidden = windows_hidden(entry_path, entry_stat)
                        result.files.append(
                            FileRecord(
                                path=str(entry_path),
                                size=size,
                                modified=iso_datetime(entry_stat.st_mtime),
                                hidden=hidden,
                                temporary_by_location=is_under(entry_path, temp_roots),
                                temporary_by_name=likely_temp_by_name(entry_path),
                            )
                        )
                        current_record.direct_size += size
                        current_record.direct_file_count += 1
                        result.files_scanned += 1
                        result.bytes_scanned += size
                    except OSError as exc:
                        current_record.errors += 1
                        result.errors.append(f"Sem acesso a {entry_path}: {exc}")
        except OSError as exc:
            current_record.errors += 1
            result.errors.append(f"Sem acesso a {current}: {exc}")

    # Agrega cada diretório no pai, do mais profundo para o mais raso.
    ordered_keys = sorted(records, key=lambda key: len(Path(records[key].path).parts), reverse=True)
    for key in ordered_keys:
        record = records[key]
        parent_key = parent_by_key[key]
        if parent_key is None:
            record.size += record.direct_size
            record.file_count += record.direct_file_count
            continue
        record.size += record.direct_size
        record.file_count += record.direct_file_count
        parent = records[parent_key]
        parent.size += record.size
        parent.file_count += record.file_count
        parent.errors += record.errors

    result.directories = list(records.values())
    result.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return result


def get_disk_usage(root: Path) -> Optional[dict[str, int]]:
    try:
        usage = disk_usage(root)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return None


def top_files(result: ScanResult, limit: int, min_size: int) -> list[FileRecord]:
    return sorted((item for item in result.files if item.size >= min_size), key=lambda item: item.size, reverse=True)[:limit]


def top_directories(result: ScanResult, limit: int, min_size: int, min_files: int) -> list[DirectoryRecord]:
    matching = (item for item in result.directories if item.size >= min_size or item.file_count >= min_files)
    return sorted(matching, key=lambda item: (item.size, item.file_count), reverse=True)[:limit]


def old_temp_files(result: ScanResult, age_days: int, limit: int) -> list[FileRecord]:
    cutoff = datetime.now().astimezone() - timedelta(days=age_days)
    found: list[FileRecord] = []
    for item in result.files:
        if not item.temporary_by_location or not item.modified:
            continue
        try:
            modified = datetime.fromisoformat(item.modified)
        except ValueError:
            continue
        if modified <= cutoff:
            found.append(item)
    return sorted(found, key=lambda item: item.size, reverse=True)[:limit]


def label_flags(item: FileRecord) -> str:
    flags = []
    if item.hidden:
        flags.append("oculto")
    if item.temporary_by_location:
        flags.append("local temporário")
    if item.temporary_by_name:
        flags.append("nome temporário")
    return f" [{', '.join(flags)}]" if flags else ""


def render_file_table(items: Iterable[FileRecord], empty: str = "Nenhum item encontrado.") -> list[str]:
    rows = list(items)
    if not rows:
        return [empty]
    return [f"  {format_size(item.size):>12}  {item.path}{label_flags(item)}" for item in rows]


def render_report(
    result: ScanResult,
    args: argparse.Namespace,
    usage: Optional[dict[str, int]],
    temp_reports: list[tuple[str, str, ScanResult]],
) -> str:
    large_files = top_files(result, args.top, args.min_file_size)
    large_dirs = top_directories(result, args.top, args.min_dir_size, args.min_file_count)
    suspicious_names = sorted(
        (item for item in result.files if item.temporary_by_name and not item.temporary_by_location),
        key=lambda item: item.size,
        reverse=True,
    )[: args.top]

    lines = [
        "RELATÓRIO DE INVENTÁRIO DE ESPAÇO EM DISCO",
        "=" * 64,
        f"Raiz analisada: {result.root}",
        f"Início: {result.started_at}",
        f"Fim: {result.finished_at}",
        f"Arquivos encontrados: {format_count(result.files_scanned)}",
        f"Diretórios encontrados: {format_count(result.directories_scanned)}",
        f"Espaço contabilizado: {format_size(result.bytes_scanned)}",
    ]
    if usage:
        lines.extend(
            [
                "",
                "USO DA UNIDADE",
                "-" * 64,
                f"Total: {format_size(usage['total'])}",
                f"Usado: {format_size(usage['used'])}",
                f"Livre: {format_size(usage['free'])}",
            ]
        )

    lines.extend(["", f"MAIORES ARQUIVOS (mínimo {format_size(args.min_file_size)})", "-" * 64])
    lines.extend(render_file_table(large_files))
    lines.extend(
        [
            "",
            f"DIRETÓRIOS MAIS PESADOS OU COM MUITOS ARQUIVOS (mínimo {format_size(args.min_dir_size)} ou {format_count(args.min_file_count)} arquivos)",
            "-" * 64,
        ]
    )
    if large_dirs:
        for item in large_dirs:
            lines.append(
                f"  {format_size(item.size):>12}  {format_count(item.file_count):>12} arquivos  {item.path}"
                f" ({format_size(item.direct_size)} diretos)"
            )
    else:
        lines.append("Nenhum diretório atende aos limites definidos.")

    lines.extend(["", "POSSÍVEIS ARQUIVOS TEMPORÁRIOS PELO NOME", "-" * 64])
    lines.append("Estes itens exigem conferência manual; a extensão sozinha não prova que são descartáveis.")
    lines.extend(render_file_table(suspicious_names))

    if temp_reports:
        lines.extend(["", "LOCAIS TEMPORÁRIOS CONHECIDOS DO WINDOWS", "-" * 64])
        lines.append(
            f"Itens antigos = modificados há pelo menos {args.temp_age_days} dias. O programa não apaga nada."
        )
        for label, guidance, temp_result in temp_reports:
            lines.extend(
                [
                    "",
                    f"{label}: {temp_result.root}",
                    f"  Espaço: {format_size(temp_result.bytes_scanned)} | Arquivos: {format_count(temp_result.files_scanned)}",
                    f"  Orientação: {guidance}",
                    "  Maiores itens antigos:",
                ]
            )
            lines.extend(render_file_table(old_temp_files(temp_result, args.temp_age_days, args.top), "  Nenhum item antigo encontrado."))
            if temp_result.errors:
                lines.append(f"  Avisos de acesso: {format_count(len(temp_result.errors))}")

    lines.extend(["", "AVISOS", "-" * 64])
    lines.append("Arquivos em uso, de sistema, de aplicativos ou de atualizações podem não ser seguros para remoção.")
    lines.append("Para liberar espaço, confirme o conteúdo e use a Lixeira, Configurações > Sistema > Armazenamento ou Limpeza de Disco.")
    lines.append("Atributos ocultos foram incluídos na varredura quando o sistema permitiu.")
    if result.errors:
        lines.append(f"Ocorreram {format_count(len(result.errors))} avisos de acesso; veja o JSON para a lista completa.")
    return "\n".join(lines) + "\n"


def json_payload(
    result: ScanResult,
    args: argparse.Namespace,
    usage: Optional[dict[str, int]],
    temp_reports: list[tuple[str, str, ScanResult]],
) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": result.root,
        "thresholds": {
            "min_file_size": args.min_file_size,
            "min_directory_size": args.min_dir_size,
            "min_directory_file_count": args.min_file_count,
            "temporary_age_days": args.temp_age_days,
        },
        "disk_usage": usage,
        "summary": {
            "files_scanned": result.files_scanned,
            "directories_scanned": result.directories_scanned,
            "bytes_scanned": result.bytes_scanned,
            "warnings": len(result.errors),
        },
        "large_files": [asdict(item) for item in top_files(result, args.top, args.min_file_size)],
        "large_directories": [
            asdict(item) for item in top_directories(result, args.top, args.min_dir_size, args.min_file_count)
        ],
        "temporary_by_name": [
            asdict(item)
            for item in sorted(
                (item for item in result.files if item.temporary_by_name and not item.temporary_by_location),
                key=lambda item: item.size,
                reverse=True,
            )[: args.top]
        ],
        "windows_temporary_locations": [
            {
                "label": label,
                "path": temp_result.root,
                "guidance": guidance,
                "summary": {
                    "files_scanned": temp_result.files_scanned,
                    "bytes_scanned": temp_result.bytes_scanned,
                    "warnings": len(temp_result.errors),
                },
                "old_files": [
                    asdict(item) for item in old_temp_files(temp_result, args.temp_age_days, args.top)
                ],
            }
            for label, guidance, temp_result in temp_reports
        ],
        "warnings": result.errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gera um inventário de espaço em uma unidade ou diretório. "
            "Inclui arquivos ocultos e, no Windows, locais temporários conhecidos."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("root", help=r"Unidade ou diretório a analisar, por exemplo C:\ ou D:\Dados")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Quantidade de itens exibidos em cada ranking")
    parser.add_argument(
        "--min-file-size", type=parse_size, default=DEFAULT_MIN_FILE_SIZE, metavar="TAMANHO",
        help="Tamanho mínimo para o ranking de arquivos (ex.: 500MB, 1GB)",
    )
    parser.add_argument(
        "--min-dir-size", type=parse_size, default=DEFAULT_MIN_DIR_SIZE, metavar="TAMANHO",
        help="Tamanho mínimo para o ranking de diretórios",
    )
    parser.add_argument(
        "--min-file-count", type=int, default=DEFAULT_MIN_FILE_COUNT,
        help="Quantidade mínima de arquivos para destacar um diretório",
    )
    parser.add_argument(
        "--temp-age-days", type=int, default=DEFAULT_TEMP_AGE_DAYS,
        help="Idade mínima, em dias, para listar arquivos dos locais temporários",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Arquivo de texto do relatório (por padrão, disk_inventory_AAAA-MM-DD_HH-MM-SS.txt)",
    )
    parser.add_argument("--json", type=Path, help="Também salva os dados detalhados em JSON")
    parser.add_argument(
        "--no-windows-temp", action="store_true",
        help="Não analisar os locais temporários conhecidos do Windows",
    )
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.top < 1:
        parser.error("--top deve ser maior que zero.")
    if args.min_file_count < 1:
        parser.error("--min-file-count deve ser maior que zero.")
    if args.temp_age_days < 0:
        parser.error("--temp-age-days não pode ser negativo.")
    if not args.root.strip():
        parser.error("Informe uma unidade ou diretório.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    root = Path(os.path.expandvars(args.root)).expanduser()
    if not root.exists():
        print(f"Erro: o caminho não existe: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Erro: informe uma unidade ou diretório, não um arquivo: {root}", file=sys.stderr)
        return 2
    root = root.resolve()

    temp_locations = iter_windows_temp_locations() if not args.no_windows_temp else []
    temp_roots = [path for path, _, _ in temp_locations]

    print(f"Analisando {root}...", flush=True)
    result = scan_directory(root, temp_roots)
    temp_reports: list[tuple[str, str, ScanResult]] = []
    seen_temp: set[str] = set()
    for path, label, guidance in temp_locations:
        key = normalized(path)
        if key in seen_temp or not path.exists() or not path.is_dir():
            continue
        seen_temp.add(key)
        if key == normalized(root) or is_under(path, [root]) or is_under(root, [path]):
            # Já foi coberto pela análise principal; os arquivos receberam as marcações de localização.
            continue
        print(f"Analisando local temporário: {path}...", flush=True)
        temp_reports.append((label, guidance, scan_directory(path, temp_roots)))

    usage = get_disk_usage(root)
    report = render_report(result, args, usage, temp_reports)
    output = args.output or Path.cwd() / f"disk_inventory_{datetime.now():%Y-%m-%d_%H-%M-%S}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"Relatório salvo em: {output}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(json_payload(result, args, usage, temp_reports), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON salvo em: {args.json}")

    print(
        f"Concluído: {format_count(result.files_scanned)} arquivos, "
        f"{format_size(result.bytes_scanned)} contabilizados, {len(result.errors)} avisos."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
