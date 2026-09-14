"""Modelos de dados compartilhados pela pipeline de legendas."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class DetectedLanguage:
    code: str
    probability: float
    name: str = ""


@dataclass(frozen=True)
class MediaInput:
    source: str
    local_path: Path | None = None
    downloaded: bool = False
