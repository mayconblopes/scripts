
Scripts de linha de comando em Python, organizados por ferramenta.

## Ferramentas

- `VideoDownloader/`: baixa vídeos usando `yt-dlp`.
- `SiteScanner/`: rastreia sites e inspeciona recursos.
- `VideoCompressor/`: compacta qualquer formato aceito pelo `ffmpeg`, sem
  redimensionar o vídeo original.

## Compressor de vídeo

Requer `ffmpeg` no `PATH` (`ffprobe` é opcional, mas melhora a leitura das
informações do arquivo):

Executado sem parâmetros, o compressor abre um menu interativo. Também é
possível usar a CLI diretamente:

```text
python VideoCompressor/video-compressor.py
python VideoCompressor/video-compressor.py video.mkv
python VideoCompressor/video-compressor.py video.mov --profile alta -o video.mp4
python VideoCompressor/video-compressor.py video.avi --profile pequeno --force
python VideoCompressor/video-compressor.py video.mp4 --target-size 80
python VideoCompressor/video-compressor.py video.mp4 --estimate-only --profile equilibrada
```

Os perfis disponíveis são `alta`, `equilibrada`, `pequeno` e `minimo`. Perfis
baseados em CRF oferecem qualidade mais consistente, mas só permitem estimar o
tamanho final. `--target-size MB` faz duas passagens e controla o tamanho por
bitrate, com pequena variação esperada. A saída padrão é H.264/AAC em MP4;
largura, altura, proporção e metadados do vídeo são preservados.
