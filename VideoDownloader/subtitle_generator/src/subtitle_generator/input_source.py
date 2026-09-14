"""Abstração das fontes aceitas pelo gerador de legendas."""

from pathlib import Path
from urllib.parse import urlparse


def is_url(value: str) -> bool:
    """Retorna se o valor parece ser uma URL HTTP(S)."""

    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_local_file(value: str) -> Path:
    """Valida e retorna um arquivo local sem alterá-lo."""

    path = Path(value).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"arquivo de mídia não encontrado: {path}")
    return path
