"""Janela tkinter de configuração do agente.

A partir da v0.2 o agente é um intermediário puro — todas as opções de
formatação de impressão (largura, margens, escala, fonte) ficam no
EldenSys, que gera o PDF pronto. Esta janela mostra só ajustes de
infraestrutura do agente: nível de log e caminho do SumatraPDF.
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
        self.root.title("EldenSys Agent — Configurações")
        self.root.geometry("520x360")
        self.root.minsize(460, 320)
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
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)
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

        # ── Botões ──
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(btns, text="Restaurar padrões", command=self._reset_defaults).pack(side="left")
        ttk.Button(btns, text="Cancelar", command=self._on_close).pack(side="right", padx=4)
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── ações ─────────────────────────────────────────
    def _collect(self) -> AgentConfig:
        c = load_config()  # mantém campos não-expostos (host, port, origins...)
        for key, var in self.vars.items():
            try:
                value = var.get()
            except tk.TclError:
                raise ValueError(f"Valor inválido em '{key}'")
            setattr(c, key, value)
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
            "Mudanças no nível de log ou no caminho do SumatraPDF "
            "exigem fechar e abrir o agente novamente.",
            parent=self.root,
        )
        self._on_close()

    def _reset_defaults(self) -> None:
        if not messagebox.askyesno(
            "Restaurar padrões",
            "Tem certeza? Os campos voltam para os valores padrão.",
            parent=self.root,
        ):
            return
        defaults = AgentConfig()
        for key, var in self.vars.items():
            var.set(getattr(defaults, key))

    def _on_close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()
