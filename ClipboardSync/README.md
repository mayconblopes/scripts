# Clipboard Sync

Pequeno sincronizador local de clipboard de texto entre computadores Windows
e um celular Android. Não usa nuvem: o PC servidor oferece uma API HTTP somente
na rede local, descoberta por UDP.

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

## Outro computador como cliente

No segundo computador, com Python 3 instalado, execute:

```powershell
python ClipboardSync/clipboard_sync.py
```

O cliente procura o servidor automaticamente e sincroniza o texto
continuamente nos dois sentidos. Ao iniciar, o clipboard do servidor é copiado
para o cliente. Para iniciar usando o conteúdo local do cliente, acrescente
`--initial-sync client`. Encerre com `Ctrl+C`.

Se a descoberta automática estiver bloqueada, informe o endereço IP do
servidor. O cliente pedirá o token, a menos que você passe uma cópia local do
arquivo `clipboard_sync.token`:

```powershell
python ClipboardSync/clipboard_sync.py --server 192.168.1.20
python ClipboardSync/clipboard_sync.py --server 192.168.1.20 --token-file .\clipboard_sync.token
```

Os dois computadores precisam estar na mesma rede local. No PC servidor,
permita as portas TCP `8765` e UDP `8766` no Firewall para redes privadas,
usando as regras da seção anterior. O cliente também precisa poder acessar
essas portas na rede.

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
usa HTTP, protegida por um token aleatório, mas não há criptografia TLS. A
descoberta UDP compartilha o token com dispositivos na mesma rede; não exponha
as portas à Internet nem use em uma rede pública. A sincronização inclui
somente texto; imagens e arquivos não são transferidos. Fora do Windows, o
cliente desktop usa Tkinter e precisa de um ambiente gráfico disponível.
