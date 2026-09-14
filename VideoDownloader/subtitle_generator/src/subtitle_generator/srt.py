"""Utilitários independentes para escrever arquivos SubRip (SRT)."""

from __future__ import annotations

from pathlib import Path
import re

from .models import TranscriptSegment


TIMESTAMP_RE = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)


def format_timestamp(seconds: float) -> str:
    """Converte segundos em ``HH:MM:SS,mmm``."""

    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def parse_timestamp(value: str) -> float:
    """Converte um timestamp SubRip em segundos."""

    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def read_srt(source: Path) -> list[TranscriptSegment]:
    """Lê um SRT UTF-8 e devolve os blocos com seus timestamps."""

    content = source.read_text(encoding="utf-8-sig")
    segments: list[TranscriptSegment] = []
    for block in re.split(r"\r?\n\s*\r?\n", content.strip()):
        lines = [line.rstrip() for line in block.splitlines()]
        if len(lines) < 3:
            continue
        match = TIMESTAMP_RE.match(lines[1].strip())
        text = "\n".join(lines[2:]).strip()
        if match is None or not text:
            continue
        segments.append(
            TranscriptSegment(
                parse_timestamp(match.group("start")),
                parse_timestamp(match.group("end")),
                text,
            )
        )
    if not segments:
        raise ValueError(f"o arquivo SRT não contém blocos válidos: {source}")
    return segments


def write_srt(segments: list[TranscriptSegment], destination: Path) -> Path:
    """Grava legendas UTF-8, recusando um caminho que já exista."""

    if destination.exists():
        raise FileExistsError(f"a legenda já existe: {destination}")
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        blocks.extend(
            [
                str(index),
                f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}",
                segment.text,
                "",
            ]
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    return destination
