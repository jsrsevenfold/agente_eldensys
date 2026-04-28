# EldenSys Print Agent

Agente local de impressão para Windows. Permite que o EldenSys ERP
(rodando em produção no Railway) imprima cupons térmicos (ESC/POS) e
documentos PDF (NFC-e, DANFE, recibos) diretamente nas impressoras
instaladas no Windows do caixa.

## Arquitetura

```
┌──────────────────────────┐         HTTPS         ┌──────────────────────┐
│  Frontend EldenSys       │  ────────────────────▶│  Backend (Railway)   │
│  (React, no browser)     │                        └──────────────────────┘
│                          │
│   fetch('localhost:17777')│  HTTP loopback
│           │              │
└───────────┼──────────────┘
            ▼
   ┌──────────────────────────────┐
   │  EldenSys Agent (este app)   │
   │  - FastAPI 127.0.0.1:17777   │
   │  - Tray icon (pystray)       │
   │  - python-escpos (Win32Raw)  │
   │  - SumatraPDF (-print-to)    │
   └──────────────┬───────────────┘
                  ▼
          Impressoras Windows
```

## Por que HTTP em loopback?

- Latência zero (sem ida/volta ao Railway).
- Padrão da indústria: QZ Tray, Bematech e Elgin usam o mesmo modelo.
- Browsers modernos permitem `https://` chamar `http://localhost` sem
  bloqueio de mixed-content.

## Por que tray app e não Serviço Windows?

Serviços rodam em **Session 0** com a conta `LocalSystem`. Nesse contexto:
- Impressoras instaladas pelo usuário podem **não estar visíveis**.
- Drivers de impressora térmica USB frequentemente **falham** ao operar
  fora da sessão interativa.
- Sem feedback visual (o cliente não sabe se está rodando).

O tray app resolve tudo isso, é auto-iniciado via `HKCU\...\Run` (não
exige admin no boot), e o cliente vê o ícone na bandeja.

## Desenvolvimento

Pré-requisitos: Python 3.11+ no Windows.

```powershell
cd C:\DEV\agente_eldensys
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# Rodar em modo dev
python -m agent
```

O ícone aparece na bandeja. Teste:

```powershell
curl http://localhost:17777/health
curl http://localhost:17777/printers
```

### Endpoints

| Método | Path             | Descrição                                |
|--------|------------------|------------------------------------------|
| GET    | /health          | Status + versão                           |
| GET    | /printers        | Lista impressoras Windows + default       |
| GET    | /config          | Lê configuração atual                     |
| POST   | /config          | Atualiza CORS / log level / sumatra path  |
| POST   | /print/escpos    | Imprime via DSL ESC/POS                   |
| POST   | /print/raw       | Envia bytes brutos para a fila            |
| POST   | /print/pdf       | Imprime PDF (base64) via SumatraPDF       |
| POST   | /print/test      | Página de teste (escpos ou pdf)           |

### Exemplo de payload ESC/POS

```json
POST /print/escpos
{
  "printer": "EPSON TM-T20",
  "profile": "80mm",
  "commands": [
    { "type": "text", "text": "MINHA LOJA", "align": "center", "bold": true, "width": 2, "height": 2 },
    { "type": "text", "text": "Rua Exemplo, 123", "align": "center" },
    { "type": "line" },
    { "type": "text", "text": "Cupom #12345" },
    { "type": "qrcode", "text": "https://eldensys.com.br/v/12345", "size": 6 },
    { "type": "newline", "count": 3 },
    { "type": "cut" }
  ]
}
```

### Exemplo de payload PDF

```json
POST /print/pdf
{
  "printer": "Brother HL-L2350DW",
  "pdf_base64": "JVBERi0xLjQK...",
  "copies": 1,
  "paper": "A4",
  "duplex": false
}
```

## Configuração

Arquivo: `%APPDATA%\EldenSysAgent\config.json`

```json
{
  "host": "127.0.0.1",
  "port": 17777,
  "allowed_origins": [
    "https://app.eldensys.com.br",
    "http://localhost:5173"
  ],
  "log_level": "INFO",
  "sumatra_path": ""
}
```

> **Importante**: ajuste `allowed_origins` com o domínio de produção real
> do seu EldenSys antes de empacotar o instalador. Origens não listadas
> serão bloqueadas pelo CORS.

Logs: `%APPDATA%\EldenSysAgent\logs\agent.log` (rotativo, 5 arquivos × 2MB).

## Build do executável

1. Baixe **SumatraPDF** portable em <https://www.sumatrapdfreader.org/download-free-pdf-viewer>
   e salve como `vendor\SumatraPDF.exe`.
2. (Opcional) Coloque um ícone em `assets\icon.ico` (256×256) e `assets\icon.png`.
3. Rode:

```powershell
.\installer\build.ps1 -Clean
```

Saída: `dist\EldenSysAgent\EldenSysAgent.exe` (standalone, ~40MB).

## Build do instalador (.exe)

Pré-requisito: [Inno Setup 6+](https://jrsoftware.org/isinfo.php).

```powershell
# Após o build acima:
& 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' .\installer\eldensys-agent.iss
```

Saída: `dist\installer\EldenSysAgent-Setup-0.1.0.exe`.

O instalador:
- Instala em `C:\Program Files\EldenSysAgent\`.
- Registra auto-start em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- Cria atalho no Menu Iniciar.
- Inicia o agente ao final.
- Uninstaller remove o registro de auto-start e mata o processo.

> **Aviso SmartScreen**: como o `.exe` não está assinado por uma CA,
> o Windows vai mostrar aviso "Editor desconhecido" na primeira execução.
> O cliente precisa clicar em **Mais informações → Executar assim mesmo**.
> Para remover esse aviso, é preciso comprar um certificado de
> assinatura de código (Sectigo/DigiCert ~US$ 200/ano).

## Integração no frontend EldenSys

Já adicionado:
- `frontend/src/api/printAgent.ts` — cliente HTTP
- `frontend/src/hooks/usePrintAgent.ts` — hook React
- `frontend/src/components/settings/PrintAgentSettings.tsx` — UI de configuração
- Aba **Settings → Impressão** mostra status do agente, lista impressoras
  e permite imprimir página de teste.

A configuração escolhida (impressora térmica + impressora PDF) é salva
em `localStorage` sob a chave `eldensys.printAgent.config`. Cada caixa
tem sua própria configuração no próprio navegador.

### Usando nos seus componentes

```ts
import { loadAgentConfig, printEscpos, printPdf, blobToBase64 } from '@/api/printAgent'

const cfg = loadAgentConfig()
if (cfg.thermalPrinter) {
  await printEscpos({
    printer: cfg.thermalPrinter,
    profile: cfg.thermalProfile,
    commands: [
      { type: 'text', text: 'Venda #1234', align: 'center', bold: true },
      { type: 'cut' },
    ],
  })
}

// PDF a partir de Blob
const pdfBlob = await fetch('/api/sales/123/danfe.pdf').then(r => r.blob())
await printPdf({
  printer: cfg.pdfPrinter!,
  pdfBase64: await blobToBase64(pdfBlob),
})
```

## Troubleshooting

| Sintoma                                      | Causa provável                                  | Solução                                                                                |
|----------------------------------------------|-------------------------------------------------|----------------------------------------------------------------------------------------|
| Frontend mostra "agente offline"             | Agente não rodando ou porta 17777 ocupada       | Verificar tray; matar processo na 17777 (`netstat -ano`)                              |
| CORS blocked no DevTools                     | Origem do EldenSys não está em `allowed_origins`| Editar `config.json`, reiniciar agente                                                 |
| Mixed-content blocked                        | Browser muito antigo                            | Atualizar Chrome/Edge; localhost é exceção desde 2020                                  |
| Impressora não aparece no `/printers`        | Driver instalado em conta diferente             | Reinstalar driver no usuário que roda o agente                                         |
| ESC/POS imprime caracteres estranhos         | Encoding incorreto                              | Adicionar comando `raw` com bytes do code page (ex.: `ESC t n`)                        |
| PDF não imprime, erro "SumatraPDF não encontrado" | Binário ausente em `vendor/`               | Baixar e colocar em `vendor\SumatraPDF.exe`, ou setar `sumatra_path` em `config.json`  |

## Licenças de terceiros

- **SumatraPDF**: GPLv3 (binário portable redistribuível). Ver site oficial.
- **python-escpos**: MIT.
- **FastAPI / Uvicorn / Pydantic**: MIT / BSD.
- **pywin32**: PSF.
- **pystray / Pillow**: LGPL / HPND.
