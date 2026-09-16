# Clipboard Sync

Sincroniza o clipboard de texto e transfere arquivos entre computadores e o
aplicativo Android na mesma rede local. Todos os computadores executam o mesmo
programa. Eles elegem automaticamente um servidor; os demais se conectam como
clientes. O servidor mantém a compatibilidade com a descoberta UDP e as rotas
`/v1/clipboard` já usadas pelo APK.

## Computadores

Com Python 3, execute em cada computador:

```powershell
python ClipboardSync/clipboard_sync.py
```

Antes da atualização, encerre as instâncias antigas do servidor e do cliente
com `Ctrl+C`; depois, inicie esta versão em cada computador. Execute apenas uma
instância por computador.

O programa determina o papel de cada computador sozinho. Ao iniciar, o
clipboard do servidor é aplicado aos clientes. Encerre com `Ctrl+C`.

Para iniciar uma transferência, digite no console de qualquer computador:

```text
send "C:\Users\usuario\Downloads\relatorio.pdf"
send "C:\Users\usuario\Documents\projeto"
```

O caminho pode ser de um arquivo ou de uma pasta. As pastas são compactadas
para a transferência e extraídas na pasta `Downloads` do dispositivo que
aceitar. O destino não é sobrescrito; se já existir um item com o mesmo nome,
o programa acrescenta um número ao nome. O remetente envia os dados somente
depois do primeiro aceite. Os demais dispositivos têm 30 segundos para aceitar
ou recusar; `Enter` aceita e `n` recusa. Se ninguém aceitar, o remetente informa
que não houve aceite.

Por padrão, arquivos recebidos vão para `Downloads`. É possível escolher outra
pasta com `--downloads "D:\\Recebidos"`. O limite é 10 GiB por transferência e
20 GiB de conteúdo descompactado por pasta.

Se o Firewall solicitar, permita nas redes privadas as portas TCP `8765` e UDP
`8766` em cada computador que executará o programa:

```powershell
New-NetFirewallRule -DisplayName "Clipboard Sync HTTP" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private
New-NetFirewallRule -DisplayName "Clipboard Sync Discovery" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 8766 -Profile Private
```

## Android

Abra `ClipboardSync/android` no Android Studio e execute `assembleDebug`, ou
use o wrapper Gradle incluído:

```powershell
cd ClipboardSync/android
$env:ANDROID_HOME = "C:\Users\lopes\.bubblewrap\android_sdk"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat assembleDebug
```

O APK será gerado em
`ClipboardSync/android/app/build/outputs/apk/debug/app-debug.apk`.
O telefone e os computadores precisam estar na mesma rede Wi-Fi. O APK mantém
as ações de enviar e capturar clipboard e também pode receber ofertas e enviar
arquivos ou pastas. As ofertas são consultadas enquanto o aplicativo está
aberto. No Android 8 e 9, o seletor do sistema pede que o usuário confirme
Downloads como destino; no Android 10 ou posterior, o aplicativo salva nessa
pasta diretamente.

## Token e rede

Cada computador mantém seu token local em
`ClipboardSync/pc/clipboard_sync.token`; o líder anuncia o próprio token ao APK
pelo protocolo legado de descoberta. A comunicação usa HTTP sem TLS e a
descoberta transmite o token na rede local. Execute somente em uma rede
confiável e não exponha as portas à Internet. A sincronização do desktop em
sistemas sem API nativa usa Tkinter e requer uma sessão gráfica.

Se dois computadores ficarem isolados por uma falha de rede, cada lado pode
eleger temporariamente um líder. Ao restabelecer a comunicação, a eleição
determinística converge para um único líder. A versão atual mantém ofertas e
arquivos temporários no servidor eleito; uma queda do servidor durante a
transferência exige iniciar uma nova oferta.
