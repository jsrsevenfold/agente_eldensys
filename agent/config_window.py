"""Janela tkinter de configuração do agente.

Três abas:

- **Geral** — infraestrutura do agente: nível de log e caminho do
  SumatraPDF. A partir da v0.2 o agente é um intermediário puro; as opções
  de formatação de impressão (largura, margens, escala, fonte) ficam no
  EldenSys, que gera o PDF pronto.
- **Loja** (v0.5) — o canal remoto: pareamento com a loja e os destinos
  LOCAIS dos trabalhos que chegam de outro aparelho (pasta da balança e
  impressora de etiquetas). Ficam aqui, e não no EldenSys, de propósito: o
  servidor nunca dita caminho de disco nem nome de impressora desta máquina.
- **Leitor biométrico** (v0.6) — estado do leitor e do espelho das digitais,
  "Testar leitor", "Sincronizar digitais agora" e a prévia no cadastro. O
  teste roda numa thread; o resultado volta por fila e só a thread da janela
  mexe no Tk.
- **Balança de checkout** (v0.7) — a balança que pesa no caixa, pela porta
  serial, para o PDV online: ligar, porta, protocolo, velocidade e tempo,
  mais "Detectar" e "Testar (10 s)" com o peso ao vivo. As duas ações usam os
  valores da tela SEM salvar e rodam numa thread; o resultado volta por fila,
  lida a cada 200 ms. Não é a carga da balança etiquetadora (aba Loja).
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .balanca import erros as erros_balanca
from .balanca.erros import ErroBalanca
from .balanca.protocolos import ROTULOS_PROTOCOLO, rotulo_protocolo
from .balanca.servico import BAUDS
from .balanca.servico import get_servico as get_servico_balanca
from .biometria.erros import ErroBiometria
from .biometria.servico import get_servico
from .config import AgentConfig, is_paired, load_config, save_config
from .printers import list_printers
from .remote_client import PairingCodeError, parse_pairing_code
from .remote_jobs import RemoteStatus, get_worker

log = logging.getLogger("eldensys.agent.config_ui")


def _find_icon() -> Path | None:
    """Localiza assets/icon.ico em dev e em build PyInstaller."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets" / "icon.ico")
        candidates.append(Path(sys.executable).parent / "assets" / "icon.ico")
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "icon.ico")
    for c in candidates:
        if c.exists():
            return c
    return None


# Define AppUserModelID pra que o Windows agrupe a janela tkinter sob o ícone
# do agente (e não o do python.exe) na barra de tarefas.
def _set_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "com.eldensys.agent"
        )
    except Exception:
        pass

_window_lock = threading.Lock()
_window_open = False


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

#: Opção "desligado" do seletor de impressora de etiquetas remotas.
NO_PRINTER = "(nenhuma — não imprimir etiquetas de outros aparelhos)"

STATUS_REFRESH_MS = 1500
_WRAP = 500  # largura de quebra dos textos explicativos (px)

#: Opção "nenhuma porta" do seletor da balança de checkout.
SEM_PORTA_BALANCA = "(nenhuma)"
#: Peso ao vivo do "Testar": a fila da balança é lida a cada tanto.
BALANCA_FILA_MS = 200
BALANCA_TESTE_S = 10.0
#: Cada leitura do "Testar" e a pausa entre elas (as do PDV offline).
BALANCA_TESTE_LEITURA_MS = 800
BALANCA_TESTE_PAUSA_S = 0.25
#: Erros que não melhoram tentando de novo: o "Testar" para no primeiro.
_ERROS_BALANCA_FIM = frozenset(
    {
        erros_balanca.PORTA_EM_USO,
        erros_balanca.PORTA_INEXISTENTE,
        erros_balanca.BALANCA_DESLIGADA,
        erros_balanca.SEM_PORTA,
        erros_balanca.REQUISICAO_INVALIDA,
        erros_balanca.AGENTE_ENCERRANDO,
    }
)
_ID_DO_ROTULO = {rotulo: id_ for id_, rotulo in ROTULOS_PROTOCOLO.items()}


def describe_status(status: RemoteStatus | None, paired: bool) -> str:
    """Texto da linha de status do pareamento. Função pura (testável)."""
    if status is None:
        # Janela aberta sem o canal rodando (não acontece no agente normal).
        return "Pareado com uma loja." if paired else "Não pareado."
    if status.state == "online":
        linhas = [f"Conectado à loja {status.store_name or '(sem nome)'}"]
        if status.agent_name:
            linhas[0] += f" como “{status.agent_name}”"
        if status.last_ok:
            hora = time.strftime("%H:%M:%S", time.localtime(status.last_ok))
            linhas.append(f"Último contato: {hora}")
        if status.last_job:
            linhas.append(f"Último trabalho: {status.last_job}")
        return "\n".join(linhas)
    if status.state == "connecting":
        return "Conectando ao EldenSys..."
    if status.state == "error":
        return f"Sem conexão com o EldenSys (tentando de novo): {status.message}"
    if status.state in ("rejected", "blocked"):
        return status.message
    return (
        "Não pareado. Gere um código no EldenSys, em Configurações → Impressão "
        "→ Agentes da loja, e cole abaixo."
    )


def describe_teste_leitor(resultado: dict) -> str:
    """Texto do "Testar leitor" (janela e bandeja). Função pura (testável)."""
    modelo = resultado.get("modelo") or "Leitor"
    partes = [p for p in (
        f"série {resultado['serie']}" if resultado.get("serie") else "",
        f"firmware {resultado['firmware']}" if resultado.get("firmware") else "",
    ) if p]
    linha = f"{modelo} respondeu" + (f" ({', '.join(partes)})." if partes else ".")
    if resultado.get("pro") is False:
        return linha + "\nEste leitor não é o iDBio Pro: ele não identifica digitais."
    digitais = resultado.get("digitais")
    if digitais is None:
        return linha
    return linha + f"\n{digitais} digitais na memória do leitor."


def describe_bio_status(
    status: dict | None, erro: str | None = None, poll: dict | None = None
) -> str:
    """Linha de estado da aba "Leitor biométrico". Função pura (testável)."""
    if erro:
        return erro
    if status is None:
        return "Biometria indisponível neste agente."
    sdk = status.get("sdk") or {}
    leitor = status.get("leitor") or {}
    espelho = status.get("espelho") or {}
    pareamento = status.get("pareamento") or {}
    poll = poll or {}
    linhas: list[str] = []
    if not sdk.get("presente"):
        linhas.append("Biblioteca do leitor ausente nesta instalação: reinstale o agente.")
    elif sdk.get("erro"):
        linhas.append(str(sdk["erro"]))
    if leitor.get("travado"):
        linhas.append("O leitor parou de responder: desconecte e reconecte o cabo USB.")
    elif leitor.get("conectado"):
        linha = f"{leitor.get('modelo') or 'Leitor'} conectado"
        if leitor.get("serie"):
            linha += f" (série {leitor['serie']})"
        if leitor.get("pro") is False:
            linha += " — não é o iDBio Pro"
        elif leitor.get("digitais") is not None:
            linha += f" — {leitor['digitais']} digitais na memória"
        linhas.append(linha + ".")
    elif leitor.get("erro"):
        linhas.append(str(leitor["erro"]))
    else:
        linhas.append("Leitor ainda não usado desde que o agente abriu. Use “Testar leitor”.")
    if leitor.get("ocupado"):
        linhas.append(f"Em uso agora ({leitor.get('operacao') or 'leitura'}).")
    if not pareamento.get("pareado"):
        linhas.append("Agente não pareado: a retaguarda ainda não usa este leitor.")
    elif poll.get("modulo") is False:
        linhas.append("O módulo de biometria não está ligado nesta loja.")
    elif poll.get("liberado") is False:
        linhas.append(
            "Este computador ainda não foi liberado para a biometria pelo administrador "
            "(EldenSys → Configurações → Impressão)."
        )
    elif espelho.get("em_andamento"):
        linhas.append(f"Recebendo as digitais da loja... {espelho.get('progresso') or 0}%")
    elif espelho.get("sincronizado_em"):
        if espelho.get("completo"):
            linhas.append("Digitais da loja em dia na memória do leitor.")
        else:
            linhas.append(f"Digitais da loja: {espelho.get('faltando') or 0} ficaram de fora.")
    if espelho.get("erro"):
        linhas.append(f"Sincronização: {espelho['erro']}")
    return "\n".join(linhas)


def _kg_br(kg: object) -> str:
    try:
        return f"{float(kg):.3f}".replace(".", ",")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"


def describe_leitura_balanca(leitura: dict) -> str:
    """Uma leitura da balança (``POST /balanca/leitura`` ou a última do
    status) em português. Função pura (testável)."""
    estado = leitura.get("estado")
    kg = leitura.get("kg")
    if estado == "peso":
        texto = f"{_kg_br(kg)} kg"
    elif estado == "zero":
        texto = "prato vazio"
    elif estado == "instavel":
        texto = "peso instável (aguardando estabilizar)"
    elif estado == "negativo":
        texto = "peso negativo (prato vazio, abaixo da tara)"
    elif estado == "sobrecarga":
        texto = "sobrecarga (peso acima da capacidade da balança)"
    elif estado == "sem_resposta":
        return "A balança não respondeu. Confira o cabo, a porta, a velocidade e se ela está ligada."
    elif estado == "nao_reconhecido":
        texto = (
            "A balança respondeu num formato desconhecido. Use “Detectar” ou troque o "
            "protocolo e a velocidade."
        )
        frame = leitura.get("frame") or {}
        if frame.get("hex"):
            texto += f"\nRecebido: {frame['hex']}"
        return texto
    else:
        return "Leitura sem resultado."
    lido = leitura.get("lido_como")
    pedido = leitura.get("protocolo")
    if lido:
        texto += f" — {rotulo_protocolo(lido)}"
        if pedido and lido != pedido and pedido in ROTULOS_PROTOCOLO and pedido != "auto":
            texto += (
                f"\nA balança fala {rotulo_protocolo(lido)}, mas o protocolo escolhido é "
                f"{rotulo_protocolo(pedido)}: use “Detectar” e salve."
            )
    return texto


def describe_deteccao_balanca(resultado: dict) -> str:
    """Resultado do "Detectar". Função pura (testável)."""
    protocolo = resultado.get("protocolo")
    if protocolo:
        leitura = describe_leitura_balanca({**resultado, "lido_como": None})
        return (
            f"Encontrada: {rotulo_protocolo(protocolo)} (lendo {leitura}). "
            "O protocolo foi escolhido acima; clique em Salvar para usar."
        )
    if resultado.get("bytes"):
        frame = resultado.get("frame") or {}
        return (
            "A balança respondeu, mas em nenhum protocolo conhecido. Confira a velocidade."
            + (f"\nRecebido: {frame['hex']}" if frame.get("hex") else "")
        )
    return "A balança não respondeu. Confira o cabo, a porta, a velocidade e se ela está ligada."


def describe_balanca_status(status: dict | None, erro: str | None = None) -> str:
    """Linha de estado da aba "Balança de checkout". Função pura (testável)."""
    if erro:
        return erro
    if status is None:
        return "Balança de checkout indisponível neste agente."
    cfg = status.get("config") or {}
    porta = status.get("porta") or {}
    linhas: list[str] = []
    if not cfg.get("habilitada"):
        linhas.append("Desligada: o PDV online não usa a balança deste computador.")
    elif not status.get("habilitada"):
        linhas.append(
            "Ligada, mas o agente não atende só este computador (host 127.0.0.1 no "
            "config.json): a balança fica desligada."
        )
    elif not cfg.get("porta"):
        linhas.append("Ligada, mas sem porta escolhida.")
    else:
        linhas.append(
            f"Ligada na {cfg['porta']} ({rotulo_protocolo(cfg.get('protocolo') or 'auto')}, "
            f"{cfg.get('baud')} bps)."
        )
    if porta.get("erro"):
        linhas.append(str(porta["erro"]))
    elif porta.get("aberta"):
        linhas.append("Porta aberta agora (fecha sozinha alguns segundos depois da última leitura).")
    ultima = status.get("ultima_leitura")
    if ultima:
        linhas.append("Última leitura: " + describe_leitura_balanca(ultima))
    return "\n".join(linhas)


def open_config_window_async() -> None:
    """Abre a janela em uma thread própria. Idempotente (uma janela por vez)."""
    global _window_open
    with _window_lock:
        if _window_open:
            return
        _window_open = True
    threading.Thread(target=_run_window, daemon=True, name="config-window").start()


def _run_window() -> None:
    global _window_open
    try:
        ConfigWindow().run()
    except Exception:
        log.exception("Falha na janela de configuração")
    finally:
        with _window_lock:
            _window_open = False


class ConfigWindow:
    def __init__(self) -> None:
        self.cfg: AgentConfig = load_config()
        _set_app_id()
        self.root = tk.Tk()
        self.root.title("EldenSys Agent — Configurações")
        self.root.geometry("600x600")
        self.root.minsize(560, 560)
        self._apply_icon()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

        self.vars: dict[str, tk.Variable] = {}
        self._status_job: str | None = None
        #: Resultados das ações da biometria (threads) → thread da janela.
        self._fila_bio: queue.Queue[str] = queue.Queue()
        #: Balança: (tipo, dado) das threads do Detectar/Testar → janela.
        self._fila_balanca: queue.Queue[tuple[str, object]] = queue.Queue()
        #: Ligado ao fechar a janela ou ao clicar em "Parar": o teste e o
        #: Detectar param na próxima volta (a porta é devolvida logo).
        self._balanca_parar = threading.Event()
        self._balanca_job: str | None = None
        self._build()
        self._refresh_status()
        self._drenar_balanca()

    def _apply_icon(self) -> None:
        """Aplica o ícone da empresa na janela (título + barra de tarefas)."""
        ico = _find_icon()
        if not ico:
            return
        try:
            # iconbitmap aceita .ico nativo no Windows (título + taskbar).
            self.root.iconbitmap(default=str(ico))
        except tk.TclError:
            # Fallback: tenta como PhotoImage (.png/.gif). .ico não funciona aqui.
            png = ico.with_suffix(".png")
            if png.exists():
                try:
                    img = tk.PhotoImage(file=str(png))
                    self.root.iconphoto(True, img)
                    self._icon_ref = img  # mantém referência viva
                except tk.TclError:
                    pass

    # ── construção da UI ──────────────────────────────
    def _build(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 8))

        geral = ttk.Frame(notebook, padding=14)
        loja = ttk.Frame(notebook, padding=14)
        bio = ttk.Frame(notebook, padding=14)
        balanca = ttk.Frame(notebook, padding=14)
        notebook.add(geral, text="Geral")
        notebook.add(loja, text="Loja (balança e etiquetas)")
        notebook.add(bio, text="Leitor biométrico")
        notebook.add(balanca, text="Balança de checkout")
        self._build_geral(geral)
        self._build_loja(loja)
        self._build_biometria(bio)
        self._build_balanca(balanca)

        # ── Botões ──
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(btns, text="Restaurar padrões", command=self._reset_defaults).pack(side="left")
        ttk.Button(btns, text="Cancelar", command=self._on_close).pack(side="right", padx=4)
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_geral(self, container: ttk.Frame) -> None:
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=0)

        ttk.Label(
            container,
            text="O agente é um intermediário puro: todas as opções de\n"
            "formatação de impressão (largura, margens, fonte, escala)\n"
            "ficam no EldenSys, em Configurações → Impressão.\n\n"
            "Aqui você ajusta só a infraestrutura do agente.",
            justify="left",
            foreground="#555",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        # ── Nível de log ──
        ttk.Label(container, text="Nível de detalhe dos registros (logs)").grid(
            row=1, column=0, sticky="w", pady=4
        )
        log_var = tk.StringVar(value=self.cfg.log_level)
        self.vars["log_level"] = log_var
        ttk.Combobox(
            container,
            textvariable=log_var,
            values=LOG_LEVELS,
            state="readonly",
            width=14,
        ).grid(row=1, column=1, sticky="e")
        ttk.Label(
            container,
            text="DEBUG gera muitos logs (use só pra investigar problemas).",
            foreground="#888",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # ── Caminho do SumatraPDF ──
        ttk.Label(container, text="Caminho do programa SumatraPDF").grid(
            row=3, column=0, sticky="w", pady=(8, 4)
        )
        sumatra_var = tk.StringVar(value=self.cfg.sumatra_path)
        self.vars["sumatra_path"] = sumatra_var
        ttk.Entry(container, textvariable=sumatra_var).grid(
            row=4, column=0, columnspan=2, sticky="we", pady=(0, 4)
        )
        ttk.Label(
            container,
            text="Deixe em branco — o agente encontra automaticamente em\n"
            "vendor/SumatraPDF.exe ou nos diretórios padrão do Windows.",
            foreground="#888",
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 4))

    def _build_loja(self, container: ttk.Frame) -> None:
        container.columnconfigure(0, weight=1)

        ttk.Label(
            container,
            text="Com o agente pareado, o EldenSys consegue mandar para este "
            "computador trabalhos disparados de outro aparelho (celular, outro "
            "PC): a carga da balança e a impressão de etiquetas.",
            justify="left",
            foreground="#555",
            wraplength=_WRAP,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        # ── Pareamento ──
        par = ttk.LabelFrame(container, text="Pareamento com a loja", padding=10)
        par.grid(row=1, column=0, sticky="we", pady=(0, 10))
        par.columnconfigure(0, weight=1)

        self.pair_status = tk.StringVar(value="")
        ttk.Label(
            par, textvariable=self.pair_status, justify="left", wraplength=_WRAP - 30
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.code_var = tk.StringVar(value="")
        ttk.Entry(par, textvariable=self.code_var).grid(
            row=1, column=0, sticky="we", padx=(0, 6)
        )
        ttk.Button(par, text="Parear", command=self._pair).grid(row=1, column=1, sticky="e")
        self.unpair_btn = ttk.Button(par, text="Desparear", command=self._unpair)
        self.unpair_btn.grid(row=2, column=1, sticky="e", pady=(6, 0))
        ttk.Label(
            par,
            text="Cole o código que começa com ELD1-.",
            foreground="#888",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        # ── Balança ──
        bal = ttk.LabelFrame(container, text="Carga da balança", padding=10)
        bal.grid(row=2, column=0, sticky="we", pady=(0, 10))
        bal.columnconfigure(0, weight=1)

        scale_var = tk.StringVar(value=self.cfg.scale_dir)
        self.vars["scale_dir"] = scale_var
        ttk.Entry(bal, textvariable=scale_var).grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(bal, text="Procurar...", command=self._browse_scale_dir).grid(
            row=0, column=1, sticky="e"
        )
        ttk.Label(
            bal,
            text="Pasta onde o programa da balança busca o arquivo de carga. "
            "Deixe em branco se este computador não recebe carga de balança.",
            foreground="#888",
            justify="left",
            wraplength=_WRAP - 30,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── Etiquetas remotas ──
        etq = ttk.LabelFrame(container, text="Etiquetas enviadas de outro aparelho", padding=10)
        etq.grid(row=3, column=0, sticky="we")
        etq.columnconfigure(0, weight=1)

        nomes = self._printer_names()
        atual = (self.cfg.remote_label_printer or "").strip()
        opcoes = [NO_PRINTER, *nomes]
        if atual and atual not in nomes:
            opcoes.append(atual)  # impressora removida/desligada: mantém visível
        self.label_printer_var = tk.StringVar(value=atual or NO_PRINTER)
        ttk.Combobox(
            etq, textvariable=self.label_printer_var, values=opcoes, state="readonly"
        ).grid(row=0, column=0, sticky="we")
        ttk.Label(
            etq,
            text="Impressora deste computador usada quando alguém escolhe "
            "“Impressora da loja” na tela de Etiquetas em outro aparelho.",
            foreground="#888",
            justify="left",
            wraplength=_WRAP - 30,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

    def _build_biometria(self, container: ttk.Frame) -> None:
        container.columnconfigure(0, weight=1)

        ttk.Label(
            container,
            text="O leitor de digital (Control iD iDBio Pro) ligado neste computador é "
            "usado pela retaguarda do EldenSys: liberação do supervisor, entrar pela "
            "digital, confirmar o cliente e cadastrar digitais. As digitais ficam "
            "cifradas no EldenSys e na memória do leitor — nunca neste computador.",
            justify="left",
            foreground="#555",
            wraplength=_WRAP,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        est = ttk.LabelFrame(container, text="Estado", padding=10)
        est.grid(row=1, column=0, sticky="we", pady=(0, 10))
        est.columnconfigure(0, weight=1)
        self.bio_status = tk.StringVar(value="")
        ttk.Label(
            est, textvariable=self.bio_status, justify="left", wraplength=_WRAP - 30
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        botoes = ttk.Frame(est)
        botoes.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Button(botoes, text="Testar leitor", command=self._bio_testar).pack(side="left")
        ttk.Button(
            botoes, text="Sincronizar digitais agora", command=self._bio_sincronizar
        ).pack(side="left", padx=(6, 0))
        self.bio_resultado = tk.StringVar(value="")
        ttk.Label(
            est, textvariable=self.bio_resultado, justify="left", wraplength=_WRAP - 30,
            foreground="#1f5f99",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        previa_var = tk.BooleanVar(value=bool(getattr(self.cfg, "biometria_previa", True)))
        self.vars["biometria_previa"] = previa_var
        ttk.Checkbutton(
            container, text="Mostrar prévia da digital no cadastro", variable=previa_var
        ).grid(row=2, column=0, sticky="w")
        ttk.Label(
            container,
            text="A prévia é uma imagem pequena e desfocada de cada leitura, só para a "
            "pessoa ver que o dedo foi lido. Vale a partir do próximo cadastro.",
            foreground="#888",
            justify="left",
            wraplength=_WRAP - 30,
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))

    # ── ações da aba Leitor biométrico ────────────────
    def _bio_testar(self) -> None:
        servico = get_servico()
        if servico is None:
            self.bio_resultado.set("Biometria indisponível neste agente.")
            return
        self.bio_resultado.set("Testando o leitor... (não precisa pôr o dedo)")

        def rodar() -> None:
            try:
                texto = describe_teste_leitor(servico.testar())
            except ErroBiometria as e:
                texto = e.mensagem
            except Exception:
                log.exception("Falha ao testar o leitor biométrico")
                texto = "Falha ao testar o leitor. Veja o log do agente."
            self._fila_bio.put(texto)

        threading.Thread(target=rodar, daemon=True, name="biometria-testar").start()

    def _bio_sincronizar(self) -> None:
        servico = get_servico()
        if servico is None:
            self.bio_resultado.set("Biometria indisponível neste agente.")
            return
        try:
            servico.sincronizar()
            self.bio_resultado.set("Sincronização iniciada; o andamento aparece acima.")
        except ErroBiometria as e:
            self.bio_resultado.set(e.mensagem)

    def _refresh_bio(self) -> None:
        while True:
            try:
                self.bio_resultado.set(self._fila_bio.get_nowait())
            except queue.Empty:
                break
        servico = get_servico()
        if servico is None:
            self.bio_status.set(describe_bio_status(None))
            return
        try:
            texto = describe_bio_status(servico.status(), poll=servico.descricao_poll())
        except ErroBiometria as e:
            texto = describe_bio_status(None, erro=e.mensagem)
        except Exception:
            log.exception("Falha ao ler o estado da biometria")
            texto = "Não foi possível ler o estado do leitor."
        self.bio_status.set(texto)

    # ── aba Balança de checkout ───────────────────────
    def _build_balanca(self, container: ttk.Frame) -> None:
        container.columnconfigure(1, weight=1)

        ttk.Label(
            container,
            text="A balança que pesa no caixa, ligada na porta serial (COM) deste computador, "
            "lida pelo PDV online do EldenSys (produtos por quilo e refeição por quilo). "
            "Não é a carga da balança etiquetadora (aba Loja). Uma porta COM só atende um "
            "programa por vez: se o PDV offline já usa esta balança, deixe desligado aqui.",
            justify="left",
            foreground="#555",
            wraplength=_WRAP,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        hab_var = tk.BooleanVar(value=getattr(self.cfg, "balanca_habilitada", False) is True)
        self.vars["balanca_habilitada"] = hab_var
        ttk.Checkbutton(
            container, text="Usar a balança no PDV online (EldenSys web)", variable=hab_var
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # ── Porta ──
        ttk.Label(container, text="Porta").grid(row=2, column=0, sticky="w", pady=3)
        atual = (getattr(self.cfg, "balanca_porta", "") or "").strip()
        self.balanca_porta_var = tk.StringVar(value=atual or SEM_PORTA_BALANCA)
        self.balanca_porta_combo = ttk.Combobox(
            container, textvariable=self.balanca_porta_var, state="readonly", width=14
        )
        self.balanca_porta_combo.grid(row=2, column=1, sticky="w")
        ttk.Button(container, text="Atualizar", command=self._balanca_atualizar_portas).grid(
            row=2, column=2, sticky="e"
        )
        self.balanca_portas_info = tk.StringVar(value="")
        ttk.Label(
            container,
            textvariable=self.balanca_portas_info,
            foreground="#888",
            justify="left",
            wraplength=_WRAP - 30,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self._balanca_atualizar_portas()

        # ── Protocolo ──
        ttk.Label(container, text="Protocolo").grid(row=4, column=0, sticky="w", pady=3)
        protocolo = str(getattr(self.cfg, "balanca_protocolo", "auto") or "auto").lower()
        if protocolo not in ROTULOS_PROTOCOLO:
            protocolo = "auto"
        # Fora de self.vars: mostra o rótulo e o _collect converte para o id.
        self.balanca_protocolo_var = tk.StringVar(value=rotulo_protocolo(protocolo))
        ttk.Combobox(
            container,
            textvariable=self.balanca_protocolo_var,
            values=list(ROTULOS_PROTOCOLO.values()),
            state="readonly",
            width=30,
        ).grid(row=4, column=1, columnspan=2, sticky="w")

        # ── Velocidade ──
        ttk.Label(container, text="Velocidade (bps)").grid(row=5, column=0, sticky="w", pady=3)
        baud = getattr(self.cfg, "balanca_baud", 9600)
        baud_var = tk.IntVar(value=baud if baud in BAUDS else 9600)
        self.vars["balanca_baud"] = baud_var
        ttk.Combobox(
            container,
            textvariable=baud_var,
            values=[str(b) for b in BAUDS],
            state="readonly",
            width=10,
        ).grid(row=5, column=1, sticky="w")

        # ── Tempo ──
        ttk.Label(container, text="Tempo de cada leitura (ms)").grid(
            row=6, column=0, sticky="w", pady=3
        )
        tempo = getattr(self.cfg, "balanca_timeout_ms", 1500)
        tempo_var = tk.IntVar(value=tempo if isinstance(tempo, int) else 1500)
        self.vars["balanca_timeout_ms"] = tempo_var
        ttk.Spinbox(
            container, from_=200, to=10000, increment=100, textvariable=tempo_var, width=8
        ).grid(row=6, column=1, sticky="w")

        # ── Estado e testes ──
        est = ttk.LabelFrame(container, text="Estado e teste (sem salvar)", padding=10)
        est.grid(row=7, column=0, columnspan=3, sticky="we", pady=(10, 0))
        est.columnconfigure(0, weight=1)
        self.balanca_status = tk.StringVar(value="")
        ttk.Label(
            est, textvariable=self.balanca_status, justify="left", wraplength=_WRAP - 30
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        botoes = ttk.Frame(est)
        botoes.grid(row=1, column=0, sticky="w")
        self.balanca_btn_detectar = ttk.Button(
            botoes, text="Detectar", command=self._balanca_detectar
        )
        self.balanca_btn_detectar.pack(side="left")
        self.balanca_btn_testar = ttk.Button(
            botoes, text="Testar (10 s)", command=self._balanca_testar
        )
        self.balanca_btn_testar.pack(side="left", padx=(6, 0))
        self.balanca_btn_parar = ttk.Button(
            botoes, text="Parar", command=self._balanca_parar_teste
        )
        self.balanca_btn_parar.pack(side="left", padx=(6, 0))
        self.balanca_btn_parar.state(["disabled"])
        self.balanca_resultado = tk.StringVar(value="")
        ttk.Label(
            est,
            textvariable=self.balanca_resultado,
            justify="left",
            wraplength=_WRAP - 30,
            foreground="#1f5f99",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _balanca_atualizar_portas(self) -> None:
        servico = get_servico_balanca()
        try:
            if servico is not None:
                portas = servico.portas()["portas"]
            else:
                from .balanca.porta import listar_portas

                portas = listar_portas()
        except Exception:  # noqa: BLE001 - sem lista, a tela mostra vazio
            log.warning("Não foi possível listar as portas COM", exc_info=True)
            portas = []
        nomes = [str(p.get("porta") or "") for p in portas if p.get("porta")]
        atual = self.balanca_porta_var.get()
        opcoes = [SEM_PORTA_BALANCA, *nomes]
        if atual and atual not in opcoes:
            opcoes.append(atual)  # adaptador desconectado agora: mantém visível
        self.balanca_porta_combo.configure(values=opcoes)
        if portas:
            self.balanca_portas_info.set(
                "Encontradas: "
                + "; ".join(
                    f"{p['porta']} ({p['descricao']})" if p.get("descricao") else str(p["porta"])
                    for p in portas
                )
            )
        else:
            self.balanca_portas_info.set(
                "Nenhuma porta COM encontrada. Conecte o cabo da balança e clique em Atualizar."
            )

    def _balanca_valores(self) -> tuple[str, int, str] | None:
        """Porta, velocidade e protocolo da TELA (sem salvar), ou None."""
        porta = self.balanca_porta_var.get()
        if not porta or porta == SEM_PORTA_BALANCA:
            self.balanca_resultado.set("Escolha a porta da balança primeiro.")
            return None
        try:
            baud = int(self.vars["balanca_baud"].get())
        except (tk.TclError, ValueError):
            baud = 9600
        protocolo = _ID_DO_ROTULO.get(self.balanca_protocolo_var.get(), "auto")
        return porta, baud, protocolo

    def _balanca_ocupada(self, ocupada: bool) -> None:
        for btn in (self.balanca_btn_detectar, self.balanca_btn_testar):
            btn.state(["disabled"] if ocupada else ["!disabled"])
        self.balanca_btn_parar.state(["!disabled"] if ocupada else ["disabled"])

    def _balanca_detectar(self) -> None:
        servico = get_servico_balanca()
        if servico is None:
            self.balanca_resultado.set("Balança de checkout indisponível neste agente.")
            return
        valores = self._balanca_valores()
        if valores is None:
            return
        porta, baud, _ = valores
        self._balanca_parar.clear()
        self._balanca_ocupada(True)
        self.balanca_resultado.set(
            f"Procurando o protocolo da balança na {porta}... (até uns 8 segundos)"
        )
        fila, parar = self._fila_balanca, self._balanca_parar

        def rodar() -> None:
            try:
                fila.put(
                    ("deteccao", servico.detectar(porta=porta, baud=baud, cancelamento=parar))
                )
            except ErroBalanca as e:
                if e.erro != erros_balanca.CANCELADO:
                    fila.put(("texto", e.mensagem))
            except Exception:
                log.exception("Falha ao detectar a balança")
                fila.put(("texto", "Falha ao detectar a balança. Veja o log do agente."))
            finally:
                fila.put(("fim", None))

        threading.Thread(target=rodar, daemon=True, name="balanca-detectar").start()

    def _balanca_testar(self) -> None:
        servico = get_servico_balanca()
        if servico is None:
            self.balanca_resultado.set("Balança de checkout indisponível neste agente.")
            return
        valores = self._balanca_valores()
        if valores is None:
            return
        porta, baud, protocolo = valores
        self._balanca_parar.clear()
        self._balanca_ocupada(True)
        self.balanca_resultado.set("Lendo a balança... ponha um peso no prato.")
        fila, parar = self._fila_balanca, self._balanca_parar
        teste = {"porta": porta, "baud": baud, "protocolo": protocolo}

        def rodar() -> None:
            fim = time.monotonic() + BALANCA_TESTE_S
            try:
                while time.monotonic() < fim and not parar.is_set():
                    try:
                        r = servico.ler(
                            timeout_ms=BALANCA_TESTE_LEITURA_MS, teste=teste, diagnostico=True
                        )
                    except ErroBalanca as e:
                        fila.put(("texto", e.mensagem))
                        if e.erro in _ERROS_BALANCA_FIM:
                            break
                    else:
                        fila.put(("leitura", r))
                    parar.wait(BALANCA_TESTE_PAUSA_S)
            except Exception:
                log.exception("Falha ao testar a balança")
                fila.put(("texto", "Falha ao testar a balança. Veja o log do agente."))
            finally:
                fila.put(("fim", None))

        threading.Thread(target=rodar, daemon=True, name="balanca-testar").start()

    def _balanca_parar_teste(self) -> None:
        self._balanca_parar.set()

    def _drenar_balanca(self) -> None:
        """Resultados das threads da balança → tela (só esta thread mexe no Tk)."""
        try:
            while True:
                try:
                    tipo, dado = self._fila_balanca.get_nowait()
                except queue.Empty:
                    break
                if tipo == "leitura":
                    self.balanca_resultado.set(
                        "Ao vivo: " + describe_leitura_balanca(dado)  # type: ignore[arg-type]
                    )
                elif tipo == "deteccao":
                    resultado = dado  # type: ignore[assignment]
                    self.balanca_resultado.set(describe_deteccao_balanca(resultado))
                    protocolo = resultado.get("protocolo")  # type: ignore[union-attr]
                    if protocolo in ROTULOS_PROTOCOLO:
                        self.balanca_protocolo_var.set(rotulo_protocolo(protocolo))
                elif tipo == "texto":
                    self.balanca_resultado.set(str(dado))
                elif tipo == "fim":
                    self._balanca_ocupada(False)
            self._balanca_job = self.root.after(BALANCA_FILA_MS, self._drenar_balanca)
        except tk.TclError:
            pass  # janela fechada entre o agendamento e a execução

    def _refresh_balanca(self) -> None:
        servico = get_servico_balanca()
        if servico is None:
            self.balanca_status.set(describe_balanca_status(None))
            return
        try:
            texto = describe_balanca_status(servico.status())
        except Exception:
            log.exception("Falha ao ler o estado da balança de checkout")
            texto = "Não foi possível ler o estado da balança."
        self.balanca_status.set(texto)

    @staticmethod
    def _printer_names() -> list[str]:
        try:
            return [str(p.get("name") or "") for p in list_printers() if p.get("name")]
        except Exception:  # pywin32 ausente / spooler parado
            log.warning("Não foi possível listar as impressoras", exc_info=True)
            return []

    # ── status do pareamento ──────────────────────────
    def _refresh_status(self) -> None:
        try:
            worker = get_worker()
            status = worker.get_status() if worker else None
            paired = is_paired(load_config())
            self.pair_status.set(describe_status(status, paired))
            self.unpair_btn.state(["!disabled"] if paired else ["disabled"])
            self._refresh_bio()
            self._refresh_balanca()
            self._status_job = self.root.after(STATUS_REFRESH_MS, self._refresh_status)
        except tk.TclError:
            pass  # janela fechada entre o agendamento e a execução

    @staticmethod
    def _wake_worker() -> None:
        worker = get_worker()
        if worker:
            worker.wake()

    # ── ações da aba Loja ─────────────────────────────
    def _pair(self) -> None:
        try:
            pairing = parse_pairing_code(self.code_var.get())
        except PairingCodeError as e:
            messagebox.showerror("Código inválido", str(e), parent=self.root)
            return
        c = load_config()
        if is_paired(c) and not messagebox.askyesno(
            "Substituir pareamento",
            "Este agente já está pareado com uma loja.\n\n"
            "Substituir pelo código novo?",
            parent=self.root,
        ):
            return
        c.server_url, c.server_tenant, c.server_token = (
            pairing.url,
            pairing.tenant,
            pairing.token,
        )
        try:
            save_config(c)
        except OSError as e:
            messagebox.showerror("Erro ao salvar", str(e), parent=self.root)
            return
        self.code_var.set("")
        log.info("Agente pareado com a loja %s em %s", pairing.tenant, pairing.url)
        self._wake_worker()

    def _unpair(self) -> None:
        if not messagebox.askyesno(
            "Desparear",
            "O agente deixa de receber carga de balança e etiquetas desta loja.\n\n"
            "Para voltar, será preciso gerar um novo código no EldenSys. Continuar?",
            parent=self.root,
        ):
            return
        c = load_config()
        c.server_url = c.server_tenant = c.server_token = ""
        try:
            save_config(c)
        except OSError as e:
            messagebox.showerror("Erro ao salvar", str(e), parent=self.root)
            return
        log.info("Agente despareado pelo usuário")
        self._wake_worker()

    def _browse_scale_dir(self) -> None:
        atual = self.vars["scale_dir"].get().strip()
        escolhida = filedialog.askdirectory(
            parent=self.root,
            title="Pasta da balança",
            initialdir=atual if atual and Path(atual).is_dir() else None,
            mustexist=True,
        )
        if escolhida:
            # askdirectory devolve com "/"; o Windows aceita, mas o caminho
            # aparece no EldenSys — melhor no formato que o usuário reconhece.
            self.vars["scale_dir"].set(str(Path(escolhida)))

    # ── ações ─────────────────────────────────────────
    def _collect(self) -> AgentConfig:
        # Relê do disco: mantém campos não-expostos (host, port, origins...) e
        # o pareamento, que é salvo na hora pelo botão "Parear".
        c = load_config()
        for key, var in self.vars.items():
            try:
                value = var.get()
            except tk.TclError:
                raise ValueError(f"Valor inválido em '{key}'")
            setattr(c, key, value)
        c.scale_dir = c.scale_dir.strip()
        printer = self.label_printer_var.get()
        c.remote_label_printer = "" if printer == NO_PRINTER else printer
        porta = self.balanca_porta_var.get().strip()
        c.balanca_porta = "" if porta == SEM_PORTA_BALANCA else porta
        c.balanca_protocolo = _ID_DO_ROTULO.get(self.balanca_protocolo_var.get(), "auto")
        if not 200 <= c.balanca_timeout_ms <= 10000:
            raise ValueError("O tempo de cada leitura da balança deve ficar entre 200 e 10000 ms.")
        if c.balanca_habilitada and not c.balanca_porta:
            raise ValueError("Escolha a porta da balança de checkout (ou desligue a balança).")
        return c

    def _save(self) -> None:
        try:
            new_cfg = self._collect()
        except ValueError as e:
            messagebox.showerror("Valor inválido", str(e), parent=self.root)
            return
        if (
            new_cfg.scale_dir
            and not Path(new_cfg.scale_dir).is_dir()
            and not messagebox.askyesno(
                "Pasta não encontrada",
                f"A pasta da balança não existe ou está inacessível agora:\n\n"
                f"{new_cfg.scale_dir}\n\n"
                "Enquanto ela não existir, a carga da balança vai falhar. "
                "Salvar mesmo assim?",
                parent=self.root,
            )
        ):
            return
        try:
            save_config(new_cfg)
        except OSError as e:
            messagebox.showerror("Erro ao salvar", str(e), parent=self.root)
            return
        self._wake_worker()
        messagebox.showinfo(
            "Salvo",
            "Configurações salvas.\n\n"
            "A pasta da balança, a impressora de etiquetas e a balança de checkout "
            "já estão valendo.\n"
            "Mudanças no nível de log ou no caminho do SumatraPDF "
            "exigem fechar e abrir o agente novamente.",
            parent=self.root,
        )
        self._on_close()

    def _reset_defaults(self) -> None:
        if not messagebox.askyesno(
            "Restaurar padrões",
            "Tem certeza? Os campos voltam para os valores padrão "
            "(o pareamento com a loja é mantido).",
            parent=self.root,
        ):
            return
        defaults = AgentConfig()
        for key, var in self.vars.items():
            var.set(getattr(defaults, key))
        self.label_printer_var.set(NO_PRINTER)
        self.balanca_porta_var.set(SEM_PORTA_BALANCA)
        self.balanca_protocolo_var.set(rotulo_protocolo("auto"))  # "Auto-detectar"

    def _on_close(self) -> None:
        # Teste ou Detectar em andamento param na próxima volta e a porta
        # volta a fechar sozinha (o PDV offline pode precisar dela).
        self._balanca_parar.set()
        try:
            if self._status_job is not None:
                self.root.after_cancel(self._status_job)
            if self._balanca_job is not None:
                self.root.after_cancel(self._balanca_job)
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
