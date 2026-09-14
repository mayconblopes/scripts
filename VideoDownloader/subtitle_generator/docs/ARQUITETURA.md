# Arquitetura proposta

```text
entrada (URL ou arquivo local)
        |
        v
media.py / downloader atual
        |
        v
language.py + LocalTranscriber (faster-whisper)
        |
        v
confirmação do idioma no menu
        |
        v
translation.py (backend local configurável)
        |
        v
srt.py -> arquivo .pt-BR.srt
```

O downloader atual deve continuar responsável por URLs e downloads. A
pipeline de legendas deve receber um arquivo local depois do download, o que
permite reutilizar a mesma etapa para arquivos já existentes no computador.

A confirmação do idioma deve ocorrer antes da transcrição completa. Em caso de
baixa confiança, o menu deve permitir escolher o idioma manualmente ou cancelar.

Nenhum modelo, arquivo baixado ou resultado temporário deve entrar no Git.
