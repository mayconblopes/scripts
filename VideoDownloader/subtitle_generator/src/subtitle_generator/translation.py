"""Backends locais de tradução para PT-BR."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Protocol

from .models import TranscriptSegment


class LocalTranslator(Protocol):
    """Contrato comum para NLLB-200, Argos Translate ou outro backend local."""

    def translate(
        self,
        segments: list[TranscriptSegment],
        source_language: str,
        target_language: str = "pt",
    ) -> list[TranscriptSegment]:
        ...


NLLB_LANGUAGE_CODES = {
    "af": "afr_Latn",
    "am": "amh_Ethi",
    "ar": "arb_Arab",
    "as": "asm_Beng",
    "az": "azj_Latn",
    "be": "bel_Cyrl",
    "bg": "bul_Cyrl",
    "bn": "ben_Beng",
    "bs": "bos_Latn",
    "ca": "cat_Latn",
    "cs": "ces_Latn",
    "cy": "cym_Latn",
    "da": "dan_Latn",
    "de": "deu_Latn",
    "en": "eng_Latn",
    "es": "spa_Latn",
    "et": "est_Latn",
    "eu": "eus_Latn",
    "fa": "pes_Arab",
    "fi": "fin_Latn",
    "fr": "fra_Latn",
    "ga": "gle_Latn",
    "gl": "glg_Latn",
    "gu": "guj_Gujr",
    "he": "heb_Hebr",
    "hi": "hin_Deva",
    "hr": "hrv_Latn",
    "hu": "hun_Latn",
    "hy": "hye_Armn",
    "id": "ind_Latn",
    "is": "isl_Latn",
    "it": "ita_Latn",
    "ja": "jpn_Jpan",
    "ka": "kat_Geor",
    "kk": "kaz_Cyrl",
    "km": "khm_Khmr",
    "kn": "kan_Knda",
    "ko": "kor_Hang",
    "lo": "lao_Laoo",
    "lt": "lit_Latn",
    "lv": "lvs_Latn",
    "mk": "mkd_Cyrl",
    "ml": "mal_Mlym",
    "mn": "khk_Cyrl",
    "mr": "mar_Deva",
    "ms": "zsm_Latn",
    "my": "mya_Mymr",
    "ne": "npi_Deva",
    "nl": "nld_Latn",
    "no": "nob_Latn",
    "pa": "pan_Guru",
    "ps": "pbt_Arab",
    "pl": "pol_Latn",
    "pt": "por_Latn",
    "ro": "ron_Latn",
    "ru": "rus_Cyrl",
    "sk": "slk_Latn",
    "sl": "slv_Latn",
    "so": "som_Latn",
    "sq": "als_Latn",
    "sr": "srp_Cyrl",
    "sv": "swe_Latn",
    "sw": "swh_Latn",
    "ta": "tam_Taml",
    "te": "tel_Telu",
    "th": "tha_Thai",
    "tl": "tgl_Latn",
    "tr": "tur_Latn",
    "uk": "ukr_Cyrl",
    "ur": "urd_Arab",
    "uz": "uzn_Latn",
    "vi": "vie_Latn",
    "xh": "xho_Latn",
    "yi": "ydd_Hebr",
    "zh": "zho_Hans",
    "zu": "zul_Latn",
}


class NllbTranslator:
    """Traduz segmentos com NLLB-200, carregando o modelo apenas quando usado."""

    def __init__(
        self,
        model_name: str = "facebook/nllb-200-distilled-600M",
        model_dir: Path | None = None,
        batch_size: int = 4,
    ) -> None:
        self.model_name = model_name
        self.model_dir = model_dir
        self.batch_size = batch_size
        self._tokenizer = None
        self._model = None
        self._torch = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as error:
            raise RuntimeError(
                "NLLB requer transformers e torch; consulte "
                "subtitle_generator/requirements.txt"
            ) from error

        kwargs = {}
        if self.model_dir is not None:
            kwargs["cache_dir"] = str(self.model_dir)
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name, **kwargs)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(self.model_name, **kwargs)
        self._model.eval()
        self._torch = torch

    def translate(
        self,
        segments: list[TranscriptSegment],
        source_language: str,
        target_language: str = "pt",
    ) -> list[TranscriptSegment]:
        """Traduz cada segmento e preserva seus timestamps."""

        self._load()
        source_code = NLLB_LANGUAGE_CODES.get(source_language)
        target_code = NLLB_LANGUAGE_CODES.get(target_language, "por_Latn")
        if source_code is None:
            raise ValueError(f"idioma não suportado pelo mapeamento NLLB: {source_language}")

        self._tokenizer.src_lang = source_code
        target_id = self._tokenizer.convert_tokens_to_ids(target_code)
        translated: list[TranscriptSegment] = []
        for offset in range(0, len(segments), self.batch_size):
            batch = segments[offset : offset + self.batch_size]
            inputs = self._tokenizer(
                [segment.text for segment in batch],
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            with self._torch.no_grad():
                output = self._model.generate(
                    **inputs,
                    forced_bos_token_id=target_id,
                    num_beams=4,
                    early_stopping=True,
                    no_repeat_ngram_size=3,
                    repetition_penalty=1.05,
                    max_new_tokens=256,
                )
            texts = self._tokenizer.batch_decode(output, skip_special_tokens=True)
            translated.extend(
                TranscriptSegment(segment.start, segment.end, text.strip())
                for segment, text in zip(batch, texts)
            )
        return translated


class OllamaTranslator:
    """Traduz com um modelo conversacional servido pelo Ollama local."""

    def __init__(
        self,
        model_name: str = "qwen3:8b",
        base_url: str = "http://127.0.0.1:11434",
        batch_size: int = 1,
        timeout: int = 900,
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self.timeout = timeout

    @staticmethod
    def _group_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        """Agrupa fragmentos curtos para o modelo receber contexto semântico."""

        groups: list[TranscriptSegment] = []
        current: list[TranscriptSegment] = []

        def flush() -> None:
            if current:
                groups.append(
                    TranscriptSegment(
                        current[0].start,
                        current[-1].end,
                        " ".join(item.text.strip() for item in current).strip(),
                    )
                )
                current.clear()

        for segment in segments:
            if not segment.text.strip():
                continue
            if current:
                gap = segment.start - current[-1].end
                duration = segment.end - current[0].start
                if gap > 1.5 or len(current) >= 4 or duration >= 14:
                    flush()
            current.append(segment)
            if re.search(r"[.!?。！？]$", segment.text.strip()) and len(current) >= 2:
                flush()
        flush()
        return groups

    def _request(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model_name,
            "messages": messages,
            # A tradução estruturada não precisa do modo de raciocínio do
            # Qwen3; desativá-lo evita esperas longas antes do JSON final.
            "think": False,
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "translations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "text": {"type": "string"},
                            },
                            "required": ["id", "text"],
                        },
                    }
                },
                "required": ["translations"],
            },
            "options": {"temperature": 0.15},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 500 and "allocate" in detail.lower():
                raise RuntimeError(
                    f"Ollama não conseguiu carregar {self.model_name} por falta de "
                    "memória disponível. Use um modelo menor, por exemplo "
                    "qwen3:4b, com `ollama pull qwen3:4b`, ou defina "
                    "OLLAMA_MODEL=qwen3:1.7b."
                ) from error
            raise RuntimeError(f"Ollama respondeu HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise RuntimeError(
                "Ollama não está em execução em "
                f"{self.base_url}. Inicie o aplicativo Ollama e tente novamente."
            ) from error
        except TimeoutError as error:
            raise RuntimeError("Ollama demorou demais para responder à tradução.") from error

        if result.get("error"):
            raise RuntimeError(f"Ollama: {result['error']}")
        try:
            return str(result["message"]["content"])
        except (KeyError, TypeError) as error:
            raise RuntimeError("Resposta inesperada da API local do Ollama.") from error

    @staticmethod
    def _parse_translations(content: str, expected_ids: set[int]) -> dict[int, str]:
        cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Ollama não retornou o JSON esperado para as legendas."
            ) from error
        if isinstance(data, dict) and isinstance(data.get("translations"), list):
            items = data["translations"]
        elif isinstance(data, dict) and {"id", "text"}.issubset(data):
            # Alguns modelos retornam diretamente um item quando o lote tem
            # apenas uma entrada, apesar de o prompt pedir um contêiner.
            items = [data]
        else:
            items = data
        if not isinstance(items, list):
            raise RuntimeError("Ollama retornou uma estrutura de tradução inválida.")
        parsed: dict[int, str] = {}
        for item in items:
            if not isinstance(item, dict) or "id" not in item or "text" not in item:
                continue
            try:
                item_id = int(item["id"])
            except (TypeError, ValueError):
                continue
            parsed[item_id] = str(item["text"]).strip()
        missing = expected_ids - parsed.keys()
        if missing:
            raise RuntimeError(
                "Ollama não devolveu todas as traduções "
                f"(faltam: {', '.join(map(str, sorted(missing)))})"
            )
        return parsed

    def translate(
        self,
        segments: list[TranscriptSegment],
        source_language: str,
        target_language: str = "pt",
    ) -> list[TranscriptSegment]:
        """Traduz blocos com contexto e conserva seus intervalos de tempo."""

        grouped = self._group_segments(segments)
        translated: list[TranscriptSegment] = []
        for offset in range(0, len(grouped), self.batch_size):
            batch = grouped[offset : offset + self.batch_size]
            entries = [
                {"id": index, "text": segment.text}
                for index, segment in enumerate(batch)
            ]
            system = (
                "Você é um tradutor profissional de legendas. Traduza do idioma "
                f"{source_language} para português brasileiro ({target_language}). "
                "Use linguagem natural e preserve nomes próprios, marcas e modelos. "
                "Não invente informações, não explique a tradução e não repita frases. "
                "Retorne somente JSON válido no formato "
                '{"translations":[{"id":0,"text":"..."}]}.'
            )
            user = (
                "Traduza cada bloco abaixo. Mantenha exatamente um item traduzido "
                "para cada id, usando o contexto dos demais blocos quando necessário:\n"
                + json.dumps(entries, ensure_ascii=False)
            )
            content = self._request(
                [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
            parsed = self._parse_translations(content, set(range(len(batch))))
            translated.extend(
                TranscriptSegment(segment.start, segment.end, parsed[index])
                for index, segment in enumerate(batch)
            )
        return translated
