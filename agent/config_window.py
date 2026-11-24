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
        self.root.geometry("580x560")
        self.root.minsize(540, 520)
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
        self._build()
        self._refresh_status()

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
        notebook.add(geral, text="Geral")
        notebook.add(loja, text="Loja (balança e etiquetas)")
        notebook.add(bio, text="Leitor biométrico")
        self._build_geral(geral)
        self._build_loja(loja)
        self._build_biometria(bio)

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
            "A pasta da balança e a impressora de etiquetas já estão valendo.\n"
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

    def _on_close(self) -> None:
        try:
            if self._status_job is not None:
                self.root.after_cancel(self._status_job)
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
