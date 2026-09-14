# Gerador local de legendas

Subprojeto para integrar o download de mídia e a geração de legendas
sincronizadas em PT-BR, sem enviar o arquivo para serviços externos.

## Fluxo implementado

1. Aceitar uma URL ou um arquivo de vídeo/áudio local.
2. Detectar automaticamente o idioma com `faster-whisper`.
3. Mostrar o idioma detectado e pedir confirmação ao usuário.
4. Transcrever o áudio mantendo os intervalos de cada fala.
5. Traduzir para PT-BR usando Ollama local (NLLB permanece como alternativa).
6. Gravar um `.srt` sem sobrescrever um arquivo existente.

## Organização

- `src/subtitle_generator/`: código da pipeline, separado do downloader atual.
- `tests/`: testes unitários e testes com arquivos curtos de amostra.
- `models/`: cache local do tradutor; não deve ser versionado. O cache do
  Whisper existente fica em `.whisper_models/` na raiz por compatibilidade.
- `output/`: resultados gerados durante o desenvolvimento; não contém arquivos
  de entrada.

O Whisper/faster-whisper faz a detecção e a transcrição, mas não traduz
diretamente para PT-BR. O backend padrão é o Ollama, que permite usar um
modelo conversacional local com contexto. O NLLB-200 continua disponível como
fallback por meio de `SUBTITLE_TRANSLATOR=nllb`.

## Instalação local

Com o Python utilizado pelo projeto, instale as dependências em
`.transcribe_deps`:

```text
python -m pip install --no-cache-dir -r subtitle_generator/requirements.txt --target .transcribe_deps
```

Na primeira tradução o NLLB será baixado automaticamente para
`subtitle_generator/models`. O arquivo tem tamanho grande e deve ser baixado
uma única vez; depois fica disponível offline. O modelo é gratuito para uso
pessoal, mas sua licença é CC BY-NC 4.0 e exige atenção à atribuição caso o
projeto seja redistribuído.

O downloader integrado é `video-downloader 1.3.py`. Ele aceita uma URL do
YouTube ou um caminho local, detecta o idioma, pede confirmação e salva o SRT
PT-BR ao lado da mídia sem substituir um arquivo já existente.

## Ollama

Instale o Ollama pelo instalador oficial do Windows e baixe um modelo local,
por exemplo:

```text
ollama pull qwen3:4b
```

O downloader usa `qwen3:4b` por padrão, por ser um equilíbrio melhor para uso
em CPU. Para escolher outro modelo, defina
`OLLAMA_MODEL`; para voltar ao NLLB, defina `SUBTITLE_TRANSLATOR=nllb` antes de
executar o programa.

O downloader prioriza runtimes locais opcionais (`.faster_runtime`,
`.torch_runtime` e `.hf_runtime`) quando presentes. Isso permite usar o
programa mesmo que o Python padrão do Windows seja diferente daquele usado na
instalação das dependências. Para versões diferentes do Python, o runtime
compatível é mantido em uma pasta `.subtitle_runtime_pyXY` correspondente.
