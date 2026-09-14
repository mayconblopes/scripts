"""Transcrição e detecção de idioma com faster-whisper."""

from pathlib import Path
import sys

from .models import DetectedLanguage, TranscriptSegment


LANGUAGE_NAMES = {
    "de": "alemão",
    "en": "inglês",
    "es": "espanhol",
    "fr": "francês",
    "it": "italiano",
    "ja": "japonês",
    "ko": "coreano",
    "pt": "português",
    "ru": "russo",
    "zh": "chinês",
}


class LocalTranscriber:
    """Executa o Whisper local com carregamento tardio das dependências."""

    def __init__(self, model_name: str = "base", model_dir: Path | None = None) -> None:
        self.model_name = model_name
        self.model_dir = model_dir
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise RuntimeError(
                    "faster-whisper não está instalado; consulte "
                    "subtitle_generator/requirements.txt. "
                    f"Python em uso: {sys.executable}. Detalhe: {error}"
                ) from error
            kwargs = {"device": "cpu", "compute_type": "int8"}
            if self.model_dir is not None:
                kwargs["download_root"] = str(self.model_dir)
            self._model = WhisperModel(self.model_name, **kwargs)
        return self._model

    def detect_language(self, media_path: Path) -> DetectedLanguage:
        """Detecta o idioma usando o início do áudio, sem transcrever tudo."""

        _segments, info = self._get_model().transcribe(
            str(media_path),
            language=None,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            clip_timestamps="0,30",
        )
        code = info.language
        return DetectedLanguage(code, info.language_probability, LANGUAGE_NAMES.get(code, code))

    def transcribe(
        self, media_path: Path, language: str | None = None
    ) -> tuple[DetectedLanguage, list[TranscriptSegment]]:
        """Detecta o idioma e transcreve o arquivo com timestamps."""

        model = self._get_model()
        segments, info = model.transcribe(
            str(media_path),
            language=language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        rows = [
            TranscriptSegment(segment.start, segment.end, segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]
        detected = DetectedLanguage(
            info.language,
            info.language_probability,
            LANGUAGE_NAMES.get(info.language, info.language),
        )
        return detected, rows
