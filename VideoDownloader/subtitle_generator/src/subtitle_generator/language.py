"""Detecção e confirmação de idioma (a integração com Whisper vem depois)."""

from .models import DetectedLanguage


def language_summary(language: DetectedLanguage) -> str:
    """Formata o idioma detectado para apresentação no menu."""

    label = language.name or language.code
    return f"{label} ({language.code}, confiança {language.probability:.0%})"
