"""Orquestração planejada do fluxo URL/arquivo -> SRT."""

from pathlib import Path

from .models import MediaInput


def default_srt_path(media_path: Path, target_language: str = "pt-BR") -> Path:
    """Retorna o nome padrão da legenda sem substituir extensão do arquivo."""

    return media_path.with_name(f"{media_path.stem}.{target_language}.srt")


def available_srt_path(media_path: Path, target_language: str = "pt-BR") -> Path:
    """Escolhe um nome livre, preservando legendas já existentes."""

    candidate = default_srt_path(media_path, target_language)
    suffix = 2
    while candidate.exists():
        candidate = media_path.with_name(
            f"{media_path.stem}.{target_language}.{suffix}.srt"
        )
        suffix += 1
    return candidate


def describe_source(source: MediaInput) -> str:
    """Descreve a origem para mensagens do CLI."""

    if source.local_path is not None:
        return str(source.local_path)
    return source.source
