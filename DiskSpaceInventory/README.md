# Disk Space Inventory

Ferramenta de linha de comando para descobrir o que ocupa espaço em uma
unidade ou diretório no Windows.

## O que ela faz

- Varre recursivamente arquivos e diretórios, incluindo itens ocultos.
- Evita seguir links simbólicos e junctions para não duplicar dados ou entrar
  em loops.
- Lista os maiores arquivos.
- Lista diretórios grandes e diretórios com muitos arquivos.
- Identifica possíveis temporários pelo local e por nomes/extensões comuns.
- Analisa `%TEMP%`, `%TMP%`, `C:\Windows\Temp`, relatórios do Windows Error
  Reporting e o cache de downloads do Windows Update quando esses locais estão
  disponíveis.
- Gera texto legível e, opcionalmente, JSON.

O script é somente leitura. Ele não exclui arquivos. A indicação de um item
temporário não significa que ele possa ser apagado sem conferência: arquivos
em uso, relatórios necessários para diagnóstico e caches de atualização exigem
cuidados adicionais.

## Uso

No PowerShell:

```powershell
python .\disk_space_inventory.py C:\ --json .\inventario-c.txt.json
python .\disk_space_inventory.py D:\Dados --min-file-size 1GB --top 50
python .\disk_space_inventory.py C:\Users\meu_usuario\Downloads --no-windows-temp
```

Por padrão, o relatório de texto é criado no diretório atual com nome
`disk_inventory_AAAA-MM-DD_HH-MM-SS.txt`.

Para consultar todas as opções:

```powershell
python .\disk_space_inventory.py --help
```

É recomendável executar o PowerShell como administrador ao analisar pastas de
sistema; sem isso, o relatório continua útil, mas registrará avisos para itens
sem permissão de leitura.
