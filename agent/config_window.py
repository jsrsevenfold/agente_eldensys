"""Janela tkinter de configuração do agente.

Abre via menu da bandeja → "Configurações". Edita os mesmos campos
do config.json em um formulário organizado por abas, com nomes amigáveis.
"""

from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .config import AgentConfig, load_config, save_config

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


# ── Mapeamentos rótulo amigável ↔ valor técnico ──
FONT_LABELS = {"Normal (recomendado)": "a", "Compacta (menor)": "b"}
FONT_LABELS_REVERSE = {v: k for k, v in FONT_LABELS.items()}

FIT_LABELS = {
    "Ajustar ao papel (recomendado)": "fit",
    "Tamanho original (1:1)": "noscale",
    "Reduzir só se passar do papel": "shrink",
}
FIT_LABELS_REVERSE = {v: k for k, v in FIT_LABELS.items()}

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


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
        self.root.title("EldenSys Agent — Configurações de Impressão")
        self.root.geometry("600x720")
        self.root.minsize(560, 600)
        self._apply_icon()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

        self.vars: dict[str, tk.Variable] = {}
        self._build()

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
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # "Recibos (PDF)" agora é a aba principal — afeta todos os cupons
        # do sistema atual (venda, delivery, cozinha, NFC-e, comanda).
        # "ESC/POS (legado)" só serve pra integrações antigas que ainda
        # mandam comandos ESC/POS direto pra impressora.
        notebook.add(self._build_a4_tab(notebook), text="Recibos (PDF)")
        notebook.add(self._build_thermal_tab(notebook), text="ESC/POS (legado)")
        notebook.add(self._build_advanced_tab(notebook), text="Avançado")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Restaurar padrões", command=self._reset_defaults).pack(side="left")
        ttk.Button(btns, text="Cancelar", command=self._on_close).pack(side="right", padx=4)
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Aba: Cupom Térmico ────────────────────────────
    def _build_thermal_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=14)
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0)

        ttk.Label(
            f,
            text="⚠ Estas opções SÓ valem pra integrações que mandam\n"
            "comandos ESC/POS direto (endpoint /print/escpos).\n"
            "Os cupons do EldenSys atual usam PDF — ajuste na aba\n"
            '"Recibos (PDF)".',
            justify="left",
            foreground="#a00",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self._spin(
            f,
            "Largura da letra",
            "escpos_default_width",
            1,
            8,
            row=1,
            hint="1 = normal, 2 = dobro de largura, 3 = triplo... (máx. 8)",
        )
        self._spin(
            f,
            "Altura da letra",
            "escpos_default_height",
            1,
            8,
            row=3,
            hint="1 = normal, 2 = dobro de altura, 3 = triplo... (máx. 8)",
        )

        ttk.Label(f, text="Tipo de letra").grid(row=5, column=0, sticky="w", pady=4)
        font_label_var = tk.StringVar(value=FONT_LABELS_REVERSE.get(self.cfg.escpos_default_font, "Normal (recomendado)"))
        self.vars["__font_label__"] = font_label_var
        ttk.Combobox(
            f,
            textvariable=font_label_var,
            values=list(FONT_LABELS.keys()),
            state="readonly",
            width=24,
        ).grid(row=5, column=1, sticky="e")
        ttk.Label(
            f,
            text="A fonte compacta cabe mais texto na linha, mas fica menor.",
            foreground="#888",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._float_spin(
            f,
            "Aumentar tudo (zoom da impressão)",
            "escpos_size_multiplier",
            0.5,
            4.0,
            0.1,
            row=7,
            hint="1.0 = sem alteração; 2.0 = dobra TUDO no cupom desta impressora.",
        )

        # ── Texto mais escuro / negrito ──
        ttk.Separator(f, orient="horizontal").grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Label(
            f, text="Intensidade da impressão", font=("Segoe UI", 9, "bold")
        ).grid(row=10, column=0, columnspan=2, sticky="w")

        bold_var = tk.BooleanVar(value=bool(self.cfg.escpos_default_bold))
        self.vars["escpos_default_bold"] = bold_var
        ttk.Checkbutton(
            f,
            text="Imprimir tudo em negrito (letra mais escura/visível)",
            variable=bold_var,
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ttk.Label(
            f,
            text="Recomendado para impressoras térmicas com fita gasta ou\n"
            "papel de baixa qualidade — fica bem mais legível.",
            foreground="#888",
            justify="left",
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # ── Margens do cupom térmico ──
        ttk.Separator(f, orient="horizontal").grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Label(
            f, text="Margens do cupom térmico (mm)", font=("Segoe UI", 9, "bold")
        ).grid(row=14, column=0, columnspan=2, sticky="w")
        ttk.Label(
            f,
            text="Desloca o conteúdo do cupom pra dentro da bobina.",
            foreground="#888",
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(0, 4))

        self._float_spin(
            f,
            "Margem esquerda (mm)",
            "escpos_left_margin_mm",
            0,
            30,
            0.5,
            row=16,
        )
        self._float_spin(
            f,
            "Margem direita (mm)",
            "escpos_right_margin_mm",
            0,
            30,
            0.5,
            row=18,
        )
        self._float_spin(
            f,
            "Margem superior — espaço em branco no topo (mm)",
            "escpos_top_margin_mm",
            0,
            30,
            0.5,
            row=20,
        )
        self._float_spin(
            f,
            "Margem inferior — espaço antes do corte (mm)",
            "escpos_bottom_margin_mm",
            0,
            30,
            0.5,
            row=22,
        )

        ttk.Label(
            f,
            text="ℹ As margens da aba 'Folha A4 / Recibos' também são\n"
            "  aplicadas a cupons impressos em PDF (comanda HTML).",
            foreground="#555",
            justify="left",
        ).grid(row=24, column=0, columnspan=2, sticky="w", pady=(8, 0))

        return f

    # ── Aba: Recibos (PDF) ────────────────────────────
    def _build_a4_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=14)
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0)

        ttk.Label(
            f,
            text="Ajustes aplicados a TODOS os cupons impressos como PDF:\n"
            "venda, delivery, cozinha, NFC-e, comanda, recibos.",
            justify="left",
            foreground="#555",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # ── Correção de corte na direita (o problema mais comum) ──
        ttk.Label(
            f,
            text="Corrigir corte na borda direita",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self._float_spin(
            f,
            "Mover impressão pra esquerda (mm)",
            "pdf_thermal_shift_left_mm",
            0,
            20,
            0.5,
            row=2,
            hint="Se palavras estão sendo cortadas na borda direita,\n"
            "aumente este valor (tipicamente 3-8mm) até parar de cortar.\n"
            "Não muda o tamanho do papel — só desloca o conteúdo.",
        )

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=10
        )

        ttk.Label(f, text="Como ajustar ao papel").grid(row=5, column=0, sticky="w", pady=4)
        fit_label_var = tk.StringVar(
            value=FIT_LABELS_REVERSE.get(self.cfg.pdf_fit_mode, "Ajustar ao papel (recomendado)")
        )
        self.vars["__fit_label__"] = fit_label_var
        ttk.Combobox(
            f,
            textvariable=fit_label_var,
            values=list(FIT_LABELS.keys()),
            state="readonly",
            width=32,
        ).grid(row=5, column=1, sticky="e")

        self._float_spin(
            f,
            "Zoom (aumenta o tamanho da letra)",
            "pdf_scale",
            0.5,
            3.0,
            0.05,
            row=6,
            hint="1.0 = original; 1.2 = aumenta 20%; 1.5 = aumenta 50%.",
        )

        ttk.Separator(f, orient="horizontal").grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=12
        )
        ttk.Label(
            f, text="Margens em milímetros (mm)", font=("Segoe UI", 9, "bold")
        ).grid(row=9, column=0, columnspan=2, sticky="w")
        ttk.Label(
            f,
            text="Adiciona espaço em branco nas bordas (em ambos os lados\n"
            "do cupom). Não confunda com 'Mover impressão pra esquerda'\n"
            "acima — aquele corrige corte da térmica sem encolher o cupom.",
            foreground="#888",
            justify="left",
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self._float_spin(f, "Margem de cima (mm)", "pdf_margin_top_mm", 0, 50, 0.5, row=11)
        self._float_spin(f, "Margem da direita (mm)", "pdf_margin_right_mm", 0, 50, 0.5, row=12)
        self._float_spin(f, "Margem de baixo (mm)", "pdf_margin_bottom_mm", 0, 50, 0.5, row=13)
        self._float_spin(f, "Margem da esquerda (mm)", "pdf_margin_left_mm", 0, 50, 0.5, row=14)

        return f

    # ── Aba: Avançado ─────────────────────────────────
    def _build_advanced_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        f = ttk.Frame(parent, padding=14)
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0)

        ttk.Label(
            f,
            text="Estas opções só são necessárias em situações específicas.\n"
            "Alterações aqui exigem reiniciar o agente.",
            justify="left",
            foreground="#a00",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        ttk.Label(f, text="Nível de detalhe dos registros (logs)").grid(
            row=1, column=0, sticky="w", pady=4
        )
        log_var = tk.StringVar(value=self.cfg.log_level)
        self.vars["log_level"] = log_var
        ttk.Combobox(
            f, textvariable=log_var, values=LOG_LEVELS, state="readonly", width=14
        ).grid(row=1, column=1, sticky="e")

        ttk.Label(f, text="Caminho do programa SumatraPDF").grid(
            row=2, column=0, sticky="w", pady=(12, 4)
        )
        sumatra_var = tk.StringVar(value=self.cfg.sumatra_path)
        self.vars["sumatra_path"] = sumatra_var
        ttk.Entry(f, textvariable=sumatra_var).grid(
            row=3, column=0, columnspan=2, sticky="we", pady=(0, 6)
        )
        ttk.Label(
            f,
            text="Deixe em branco — o agente encontra automaticamente.",
            foreground="#888",
        ).grid(row=4, column=0, columnspan=2, sticky="w")

        return f

    # ── helpers de campos ────────────────────────────
    def _spin(
        self,
        parent: ttk.Frame,
        label: str,
        key: str,
        mn: int,
        mx: int,
        row: int,
        hint: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        var = tk.IntVar(value=int(getattr(self.cfg, key)))
        self.vars[key] = var
        ttk.Spinbox(parent, from_=mn, to=mx, textvariable=var, width=8).grid(
            row=row, column=1, sticky="e"
        )
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, 6)
            )

    def _float_spin(
        self,
        parent: ttk.Frame,
        label: str,
        key: str,
        mn: float,
        mx: float,
        step: float,
        row: int,
        hint: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        var = tk.DoubleVar(value=float(getattr(self.cfg, key)))
        self.vars[key] = var
        ttk.Spinbox(
            parent,
            from_=mn,
            to=mx,
            increment=step,
            textvariable=var,
            width=8,
            format="%.2f",
        ).grid(row=row, column=1, sticky="e")
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, 6)
            )

    # ── ações ─────────────────────────────────────────
    def _collect(self) -> AgentConfig:
        c = load_config()  # mantém campos não-expostos (host, port, origins...)
        for key, var in self.vars.items():
            if key.startswith("__"):  # campos de tradução, processados abaixo
                continue
            try:
                value = var.get()
            except tk.TclError:
                raise ValueError(f"Valor inválido em '{key}'")
            setattr(c, key, value)

        # Traduz rótulos amigáveis pros valores técnicos
        font_label = self.vars["__font_label__"].get()
        c.escpos_default_font = FONT_LABELS.get(font_label, "a")

        fit_label = self.vars["__fit_label__"].get()
        c.pdf_fit_mode = FIT_LABELS.get(fit_label, "fit")

        return c

    def _save(self) -> None:
        try:
            new_cfg = self._collect()
        except ValueError as e:
            messagebox.showerror("Valor inválido", str(e), parent=self.root)
            return
        try:
            save_config(new_cfg)
        except OSError as e:
            messagebox.showerror("Erro ao salvar", str(e), parent=self.root)
            return
        messagebox.showinfo(
            "Salvo",
            "Configurações salvas.\n\n"
            "As opções de impressão (letra, zoom, margens) entram em vigor "
            "na próxima impressão.\n\n"
            "Mudanças em 'Avançado' exigem fechar e abrir o agente novamente.",
            parent=self.root,
        )
        self._on_close()

    def _reset_defaults(self) -> None:
        if not messagebox.askyesno(
            "Restaurar padrões",
            "Tem certeza? Todos os campos voltam para os valores padrão.",
            parent=self.root,
        ):
            return
        defaults = AgentConfig()
        for key, var in self.vars.items():
            if key == "__font_label__":
                var.set(FONT_LABELS_REVERSE[defaults.escpos_default_font])
            elif key == "__fit_label__":
                var.set(FIT_LABELS_REVERSE[defaults.pdf_fit_mode])
            else:
                var.set(getattr(defaults, key))

    def _on_close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
