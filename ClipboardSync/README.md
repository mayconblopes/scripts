# Clipboard Sync

Pequeno sincronizador local de clipboard de texto entre o Windows e um
celular Android. Não usa nuvem: o PC oferece uma API HTTP somente na rede
local e o APK localiza o servidor por descoberta UDP.

## PC

Execute no computador:

```powershell
python ClipboardSync/pc/clipboard_sync_server.py
```

Na primeira execução, o servidor cria `clipboard_sync.token`. Esse arquivo é
secreto e não deve ser versionado. Se o Firewall do Windows perguntar, permita
acesso somente em redes privadas.

Se o aplicativo não encontrar o PC, abra o PowerShell como Administrador e
crie as regras de entrada somente para redes privadas:

```powershell
New-NetFirewallRule -DisplayName "Clipboard Sync HTTP" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private
New-NetFirewallRule -DisplayName "Clipboard Sync Discovery" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 8766 -Profile Private
```

Depois de testar, as regras podem ser removidas com:

```powershell
Remove-NetFirewallRule -DisplayName "Clipboard Sync HTTP"
Remove-NetFirewallRule -DisplayName "Clipboard Sync Discovery"
```

O servidor aceita texto de até 2 MiB. Ele monitora mudanças no clipboard do PC,
recebe o clipboard do Android por `POST` e responde ao pedido de captura por
`GET`. O aplicativo não precisa de configuração manual de IP.

## Android

Abra `clipboard_sync/android` no Android Studio e execute `assembleDebug`, ou
use o wrapper Gradle já incluído, apontando `ANDROID_HOME` para o SDK portátil
do projeto Library:

```powershell
cd ClipboardSync/android
$env:ANDROID_HOME = "C:\Users\lopes\.bubblewrap\android_sdk"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat assembleDebug
```

O APK de depuração será criado em:

```text
ClipboardSync/android/app/build/outputs/apk/debug/app-debug.apk
```

O celular e o PC precisam estar na mesma rede Wi-Fi. O aplicativo possui dois
botões:

- `Capturar clipboard do PC`: solicita o texto ao PC e coloca-o no clipboard
  do Android.
- `Enviar clipboard para o PC`: envia o texto atual do Android e substitui o
  clipboard do PC.

## Segurança e limitações

O projeto é destinado a uso pessoal em uma rede confiável. A comunicação local
usa HTTP, protegida por um token aleatório, mas não há criptografia TLS. Não
exponha a porta 8765 à Internet. A primeira versão sincroniza somente texto;
imagens e arquivos não são incluídos.
