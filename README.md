# EldenSys Agent

Agente local para Windows. Permite que o EldenSys ERP (rodando em produção
no Railway) alcance o que só existe no computador da loja:

- **Impressão local** — cupons térmicos (ESC/POS) e documentos PDF (NFC-e,
  DANFE, recibos, etiquetas) nas impressoras instaladas no Windows do caixa.
- **Canal remoto (v0.5)** — trabalhos disparados de *outro aparelho*
  (celular, outro PC): gravar a **carga da balança** na pasta que o programa
  da balança lê e **imprimir etiquetas** na impressora deste computador.
  Ver [Canal remoto](#canal-remoto-carga-de-balança-e-etiquetas-de-outro-aparelho).

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

## Canal remoto: carga de balança e etiquetas de outro aparelho

A impressão local só funciona quando o navegador está **na mesma máquina**
do agente (`localhost`). Para um celular ou outro PC mandar algo para o
computador da loja, o caminho é outro — passa pelo servidor:

```
Celular / outro PC ──cria trabalho──▶ Backend EldenSys (fila da loja)
                                           ▲
                                           │ poll / ack  (HTTPS de SAÍDA)
                              EldenSys Agent no PC da loja
                                   │                    │
                          pasta da balança     impressora de etiquetas
```

- **Só conexão de saída.** O agente pergunta ao EldenSys se há trabalho
  (`POST {api}/agent/poll/`) e confirma o resultado
  (`POST {api}/agent/jobs/<id>/ack/`). Nenhuma porta é aberta e **nenhuma
  rota nova existe no loopback** — a rota local não tem senha e aceita
  qualquer site, então gravar arquivo por ela seria um risco.
- **Pareamento.** No EldenSys, em *Configurações → Impressão → Agentes da
  loja*, gere um código (`ELD1-…`) e cole na janela do agente, aba **Loja**.
  O código traz o endereço da API, a loja e um segredo. O servidor amarra o
  código à primeira instalação que o usa: o mesmo código em outro PC é
  recusado.
- **Os destinos são desta máquina.** A pasta da balança e a impressora de
  etiquetas são escolhidas **na janela do agente**. O servidor manda só nome
  de arquivo e bytes — nunca caminho de disco nem nome de impressora — e o
  nome do arquivo é validado (sem `..`, sem separador, só `.txt`).
- **Agente não pareado não faz rede nenhuma.** PCs que usam o agente só para
  imprimir continuam exatamente como antes.

| Trabalho (`kind`) | O que o agente faz |
|---|---|
| `scale_file` | Grava o(s) TXT de carga (Toledo `txitens.txt`, Filizola `CADTXT.TXT` + `SETORTXT.TXT`, Urano) na pasta da balança, byte a byte, com troca atômica do arquivo. |
| `label_pdf`  | Imprime o PDF na impressora de etiquetas configurada, pelo mesmo caminho do `/print/pdf` com `render_mode: "label"`. |

O ritmo do polling é ditado pelo servidor (`poll_interval`: lento quando
ocioso, rápido quando alguém está na tela de Etiquetas). Um trabalho já
concluído nunca é refeito — se o servidor reentregar porque a confirmação se
perdeu, o agente só repete a confirmação (um lote de etiquetas não sai em
dobro por queda de rede).

Módulos: `agent/remote_client.py` (pareamento + HTTP), `agent/remote_jobs.py`
(ciclo poll → executa → ack), `agent/scale_files.py` (gravação segura).

> O agente roda na sessão do usuário: se o Windows do PC da loja estiver sem
> ninguém logado, os trabalhos remotos ficam aguardando.

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
| POST   | /config          | Atualiza CORS / log / sumatra / fonte / escala / margens |
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
  "allowed_origins": ["*"],
  "allowed_origin_regex": "...",
  "log_level": "INFO",
  "sumatra_path": "",
  "sumatra_label_disable_antialias": true,

  "scale_dir": "C:\\Toledo\\Carga",
  "remote_label_printer": "Argox OS-214",
  "server_url": "https://api.../api",
  "server_tenant": "loja_x",
  "server_token": "esa_...",
  "instance_id": "..."
}
```

| Chave (canal remoto) | O que é |
|---|---|
| `scale_dir` | Pasta onde a carga da balança é gravada. Vazio = este PC não recebe carga. |
| `remote_label_printer` | Impressora usada pelas etiquetas enviadas de outro aparelho. Vazio = desligado. |
| `server_url`, `server_tenant`, `server_token` | Pareamento com a loja (preenchidos pelo botão **Parear**). `server_token` é segredo: nunca é devolvido pelo `GET /config`. |
| `instance_id` | Identidade fixa desta instalação, gerada na primeira execução. |

**Forma recomendada**: clique duplo no ícone da bandeja (ou clique
direito → **"Configurações..."**) — abre uma janela com as abas
**Geral** (log, SumatraPDF) e **Loja** (pareamento, pasta da balança,
impressora de etiquetas remotas), sem editar JSON.

A pasta da balança, a impressora de etiquetas e o pareamento valem
imediatamente, sem reiniciar o agente.

Para edição manual: clique direito → **"Editar config.json (avançado)"**.

As mudanças nas seções `escpos_*` e `pdf_*` entram em vigor na
próxima impressão (sem reiniciar o agente). Já mudanças em
`allowed_origins`, `log_level` ou `sumatra_path` exigem reiniciar.

### Tamanho da fonte e qualidade

**Cupom térmico ESC/POS** (rota `/print/escpos`):

| Campo                      | O que faz                                                              |
|----------------------------|------------------------------------------------------------------------|
| `escpos_default_width`     | Largura padrão do texto (1-8). Vale quando o comando não especifica.   |
| `escpos_default_height`    | Altura padrão do texto (1-8). Idem.                                    |
| `escpos_default_font`      | `"a"` (12x24, padrão) ou `"b"` (9x17, menor). Idem.                    |
| `escpos_size_multiplier`   | Multiplicador global (0.5-4.0). Aplica POR CIMA do width/height vindo  |
|                            | do comando. Útil pra deixar TODAS as impressões maiores numa máquina   |
|                            | específica sem mexer no frontend. Ex.: `2.0` dobra tudo.               |

**PDF** (rota `/print/pdf` — NFC-e, DANFE, recibos A4, comandas HTML, etiquetas):

O agente imprime PDFs sempre **1:1** (`noscale`) e **sem auto-rotação**
(`disable-auto-rotation` do SumatraPDF): tamanho, margens, escala e orientação
são responsabilidade do EldenSys, que gera o PDF já no formato final. Sem o
`disable-auto-rotation`, o Sumatra girava a página 90° quando a orientação
dela diferia da orientação do papel do driver (etiquetas saíam deitadas).
Os campos antigos de ajuste (`pdf_fit_mode`, `pdf_scale`, `pdf_margin_*_mm`)
foram removidos na v0.2 — se existirem no `config.json`, são ignorados.

> **Dica pra térmica**: prefira `/print/escpos` quando possível — texto
> nativo sai mais nítido que PDF rasterizado e respeita
> `escpos_size_multiplier`.

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
