# EldenSys Agent

Agente local para Windows. Permite que o EldenSys ERP (rodando em produção
no Railway) alcance o que só existe no computador da loja:

- **Impressão local** — cupons térmicos (ESC/POS) e documentos PDF (NFC-e,
  DANFE, recibos, etiquetas) nas impressoras instaladas no Windows do caixa.
- **Canal remoto (v0.5)** — trabalhos disparados de *outro aparelho*
  (celular, outro PC): gravar a **carga da balança** na pasta que o programa
  da balança lê e **imprimir etiquetas** na impressora deste computador.
  Ver [Canal remoto](#canal-remoto-carga-de-balança-e-etiquetas-de-outro-aparelho).
- **Leitor biométrico (v0.6)** — o leitor de digital **Control iD iDBio Pro**
  ligado neste computador, usado pela retaguarda: liberação do supervisor,
  entrar pela digital, confirmar o cliente na venda a prazo e cadastrar
  digitais. Ver [Leitor biométrico](#leitor-biométrico-v06).

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
  rota de balança ou etiqueta existe no loopback** — as rotas de impressão
  não têm senha e aceitam qualquer site (CORS `*`), então gravar arquivo por
  elas seria um risco. As rotas locais sensíveis (`POST /config` e
  `/biometria/*`) têm política de origem própria: ver
  [Segurança das rotas locais](#segurança-das-rotas-locais).
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

## Leitor biométrico (v0.6)

O navegador não alcança USB. Para usar o leitor **Control iD iDBio Pro** na
retaguarda, a página fala com o agente deste computador; o agente fala com o
leitor (DLL `libcidbio.dll` do SDK 1.7.0) e com o servidor:

```
Retaguarda (navegador) ──1. cria o desafio──▶ Backend EldenSys
        │                                          ▲
        │ 2. POST /biometria/identificar           │ 4. resultado, com o
        │    {desafio_id}                          │    token do agente
        ▼                                          │
   EldenSys Agent ── 3. captura e identifica no leitor (1:N dentro do Pro)
```

- **O servidor decide; o navegador só carrega o id do desafio.** O agente
  informa o `device_id` identificado direto ao EldenSys, com o token do
  pareamento. A digital (template) nunca passa pelo navegador, não vai para o
  log nem para o disco deste computador; a imagem só aparece como prévia
  pequena e desfocada no cadastro.
- **Só PC liberado.** Além de pareado, o agente precisa ser liberado para a
  biometria pelo administrador no EldenSys; o módulo `biometria` precisa
  estar ligado na loja.
- **Espelho da memória.** O leitor Pro compara o dedo com as ~3.000 digitais
  da própria memória. O poll traz a versão das digitais da loja; quando muda,
  o agente baixa a lista e grava o que falta, em lotes e em segundo plano
  (usuários primeiro, depois os clientes mais recentes). Só as digitais
  excluídas (lápides) são apagadas: um id desconhecido — por exemplo, uma
  digital que o PDV cadastrou sem rede — só sai com a memória cheia ou quando
  o leitor veio de outra loja ou é outro aparelho. O estado fica em
  `%APPDATA%\EldenSysAgent\biometria.json`, sem nenhuma digital (guarda também
  o último agente e a loja vistos no poll, para o status valer antes do
  primeiro poll quando o agente liga sem internet).
- **Leitor dividido com o PDV offline.** O PDV no mesmo computador usa o
  mesmo leitor: o agente só o abre quando precisa e o devolve
  `biometria_ocioso_s` (20 s) depois de uma operação do usuário — o cadastro
  são 4 leituras seguidas — ou na hora depois de uma tarefa de fundo.
- **Uma leitura por vez.** Uma segunda operação recebe `409 ocupado` (com
  `pode_cancelar`); fechar a aba ou abortar o `fetch` cancela a captura
  (`CancelCapture`) em vez de prender o leitor por 30 s.

| Método | Rota | O que faz |
|---|---|---|
| GET  | `/biometria/status` | Estado do SDK, do leitor, do pareamento e do espelho. Não abre o leitor nem chama o servidor. |
| POST | `/biometria/testar` | Abre o leitor: modelo, série, firmware, se é o Pro e quantas digitais tem. |
| POST | `/biometria/identificar` | `{desafio_id, tenant?}` → captura, identifica e devolve o veredito do servidor (ou `{status: "tempo_esgotado"\|"cancelado"\|"sem_dedo"}`). |
| POST | `/biometria/login` | `{challenge, tenant?}` → identifica e devolve o ticket de entrada (PKCE). |
| POST | `/biometria/cadastro/iniciar` | `{desafio_id, tenant?}` → `{sessao_id, passos: 4, expira_em_s}`. |
| POST | `/biometria/cadastro/{sessao_id}/verificar` | Dedo já é de outra pessoa? `{status: "livre"\|"duplicada"}`. |
| POST | `/biometria/cadastro/{sessao_id}/captura` | Uma das 3 leituras (qualidade ≥ 50), com prévia opcional. |
| POST | `/biometria/cadastro/{sessao_id}/concluir` | Junta as 3 leituras, grava no leitor e depois no servidor (servidor recusou = apaga do leitor; sem rede = repetir só reenvia). |
| POST | `/biometria/cadastro/{sessao_id}/descartar` | Abandona o cadastro. |
| POST | `/biometria/cancelar` | Cancela a leitura em andamento. |
| POST | `/biometria/sincronizar` | `202`: recarrega as digitais da loja na memória do leitor. |

Erros saem como `{"erro", "mensagem", "detail"}` (+ campos extras):
`403 origem_recusada|modulo_inativo`, `404 sessao_inexistente`,
`409 nao_pareado|pareamento_recusado|outra_loja|ocupado|passo_invalido|memoria_cheia|servidor_recusou`,
`422 leitor_nao_pro|requisicao_invalida` (e o identificador do código do
leitor, como `merge` ou `nao_identificado`),
`503 sdk_ausente|leitor_desconectado|leitor_em_uso|leitor_travado|sincronizando|sem_conexao|biometria_desligada`,
`500 interno` (sem detalhes técnicos; eles vão só para o log).

Na bandeja, **Testar leitor biométrico** mostra o resultado numa notificação.
Na janela de configuração, a aba **Leitor biométrico** mostra o estado, testa
o leitor, sincroniza as digitais e liga/desliga a prévia.

Módulos: `agent/biometria/` — `sdk.py` (binding ctypes), `leitor.py` (thread
dona do leitor), `espelho.py`, `cadastro.py`, `cliente.py` (rotas do
servidor), `servico.py` (fachada), `origem.py` (política de origem e CORS),
`rotas.py`, `erros.py`. Testes sem o leitor usam `tests/fake_idbio.py`; com o
leitor ligado, `tools/sonda_idbio.py` mede o aparelho.

### Segurança das rotas locais

Qualquer página aberta no navegador da loja alcança `http://localhost:17777`.

- **Impressão** (`/print/*`, `/printers`, `/health`, `GET /config`): CORS `*`,
  como sempre — qualquer página da loja pode imprimir. O `allowed_origins` do
  config.json é mantido em `["*"]` pelo próprio agente.
- **`POST /config`**: pedido com `Origin` fora da política abaixo recebe 403
  (sem `Origin`, como o `curl`, continua aceito). `sumatra_path` só aceita um
  `SumatraPDF*.exe` que exista neste computador — antes da v0.6 um site
  qualquer podia apontar o agente para outro programa.
- **`/biometria/*`**: só as origens da política; `Host` precisa ser
  `localhost`/`127.0.0.1`/`[::1]` (contra DNS rebinding); `Origin: null` e
  POST sem `Origin` são recusados; o CORS ecoa a própria origem (nunca `*`) e
  o preflight responde `Access-Control-Allow-Private-Network: true`. Com
  `host` diferente de `127.0.0.1`/`localhost` no config.json, a biometria
  fica desligada.

Origens aceitas: `https://eldensys.hubdesktop.com.br` e os subdomínios dele.
`http://localhost:5173` e `http://127.0.0.1:5173` (Vite) só com
`"biometria_permitir_dev": true`; outras, só listadas em
`biometria_origens_extras`. Essas duas chaves existem só no config.json:
nenhuma rota as grava.

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
| GET    | /health          | Status, versão e `recursos` (`impressao`, `canal_remoto`, `biometria`) |
| GET    | /printers        | Lista impressoras Windows + default       |
| GET    | /config          | Lê a configuração (sem pareamento nem chaves da biometria) |
| POST   | /config          | Log, SumatraPDF e anti-aliasing de etiqueta — só origens da [política](#segurança-das-rotas-locais) |
| POST   | /print/escpos    | Imprime via DSL ESC/POS                   |
| POST   | /print/raw       | Envia bytes brutos para a fila            |
| POST   | /print/pdf       | Imprime PDF (base64) via SumatraPDF       |
| POST   | /print/test      | Página de teste (escpos ou pdf)           |
| *      | /biometria/*     | Leitor biométrico — ver [Leitor biométrico](#leitor-biométrico-v06) |

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
  "instance_id": "...",

  "biometria_permitir_dev": false,
  "biometria_origens_extras": [],
  "biometria_habilitada": true,
  "biometria_previa": true,
  "biometria_ocioso_s": 20.0
}
```

| Chave (canal remoto) | O que é |
|---|---|
| `scale_dir` | Pasta onde a carga da balança é gravada. Vazio = este PC não recebe carga. |
| `remote_label_printer` | Impressora usada pelas etiquetas enviadas de outro aparelho. Vazio = desligado. |
| `server_url`, `server_tenant`, `server_token` | Pareamento com a loja (preenchidos pelo botão **Parear**). `server_token` é segredo: nunca é devolvido pelo `GET /config`. |
| `instance_id` | Identidade fixa desta instalação, gerada na primeira execução. |

| Chave (biometria, v0.6) | O que é |
|---|---|
| `biometria_habilitada` | `false` desliga a biometria neste PC: o leitor nunca é aberto e as rotas `/biometria` respondem `503 biometria_desligada`. |
| `biometria_previa` | Mostra a prévia (PNG ≤ 96 px, desfocada) de cada leitura do cadastro. Também na aba **Leitor biométrico**. |
| `biometria_ocioso_s` | Segundos com o leitor aberto depois de uma operação do usuário (depois ele é devolvido ao PDV do mesmo PC). |
| `biometria_permitir_dev` | Aceita o Vite local (`localhost:5173`/`127.0.0.1:5173`) nas rotas sensíveis. Só desenvolvimento. |
| `biometria_origens_extras` | Origens a mais aceitas nas rotas sensíveis (sem curinga). |

Nenhuma chave da biometria aparece no `GET /config` nem é aceita pelo
`POST /config`: só a janela (prévia) e o config.json mudam esses valores.

**Forma recomendada**: clique duplo no ícone da bandeja (ou clique
direito → **"Configurações..."**) — abre uma janela com as abas
**Geral** (log, SumatraPDF), **Loja** (pareamento, pasta da balança,
impressora de etiquetas remotas) e **Leitor biométrico** (estado, testar,
sincronizar, prévia), sem editar JSON.

A pasta da balança, a impressora de etiquetas e o pareamento valem
imediatamente, sem reiniciar o agente.

Para edição manual: clique direito → **"Editar config.json (avançado)"**.

As mudanças nas seções `escpos_*` e `pdf_*` entram em vigor na
próxima impressão (sem reiniciar o agente). Já mudanças em
`log_level` ou `sumatra_path` exigem reiniciar. As chaves `biometria_*`
valem no próximo pedido, sem reiniciar.

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

> **CORS**: as rotas de impressão aceitam qualquer origem (`allowed_origins`
> é mantido em `["*"]` pelo agente; não precisa ajustar por loja). As rotas
> sensíveis (`POST /config`, `/biometria/*`) seguem a
> [política de origem](#segurança-das-rotas-locais), que já cobre o
> domínio de produção e os subdomínios.

Logs: `%APPDATA%\EldenSysAgent\logs\agent.log` (rotativo, 5 arquivos × 2MB).

## Build do executável

1. Baixe **SumatraPDF** portable em <https://www.sumatrapdfreader.org/download-free-pdf-viewer>
   e salve como `vendor\SumatraPDF.exe`.
2. Copie a DLL do leitor biométrico (kit IDBIO_SDK 1.7.0,
   `lib\C_C++\win64\libcidbio.dll`) para `vendor\idbio\libcidbio.dll` — ver
   `vendor\idbio\README.txt`. O `build.ps1` falha sem ela e confere o SHA-256
   e se é x64; `-SkipBiometria` gera o agente sem o leitor.
3. (Opcional) Coloque um ícone em `assets\icon.ico` (256×256) e `assets\icon.png`.
4. Rode:

```powershell
.\installer\build.ps1 -Clean
```

Saída: `dist\EldenSysAgent\EldenSysAgent.exe` (standalone, ~40MB), com a DLL
do leitor em `dist\EldenSysAgent\_internal\vendor\idbio\`.

## Build do instalador (.exe)

Pré-requisito: [Inno Setup 6+](https://jrsoftware.org/isinfo.php).

```powershell
# Após o build acima:
& 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' .\installer\eldensys-agent.iss
```

Saída: `dist\installer\EldenSysAgent-Setup-<versão>.exe` (o `build.ps1` também
deixa a cópia `EldenSysAgent-Setup.exe`, nome que o EldenSys baixa).

O instalador:
- Só instala em Windows 64 bits (`ArchitecturesAllowed=x64compatible`: a DLL
  do leitor é x64; exige Inno Setup 6.3+).
- Instala em `%LOCALAPPDATA%\Programs\EldenSysAgent\` (sem pedir administrador).
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
| CORS blocked no DevTools (impressão)         | `allowed_origins` sem `"*"` (editado à mão)     | O agente regrava `["*"]` ao abrir: reinicie o agente                                   |
| `403 origem_recusada` na biometria           | Página fora de `*.eldensys.hubdesktop.com.br`   | Use o domínio oficial; outro domínio só em `biometria_origens_extras` (config.json)    |
| `503 sdk_ausente` na biometria               | `libcidbio.dll` fora da instalação              | Reinstalar o agente (gerado sem `-SkipBiometria`)                                      |
| `503 leitor_em_uso`                          | O PDV do mesmo PC está com o leitor             | Aguardar ou fechar a tela do PDV que espera o dedo                                     |
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
- **SDK do iDBio (Control iD, `libcidbio.dll` 1.7.0)**: licença de
  redistribuição **a confirmar** com a Control iD; por isso a DLL fica fora do
  git (`vendor\idbio\README.txt`).
