"""Fachada da balança de checkout: o que as rotas, a janela e o heartbeat usam.

Regras que valem para todas as operações:

- **construir não tem efeito nenhum**: sem thread, sem porta, sem disco. A
  porta abre na primeira leitura (``leitor.py``);
- **a configuração é relida a cada uso** e saneada (o config.json é editado
  à mão: valor inválido vira o padrão), então ligar, trocar a porta ou o
  protocolo vale na leitura seguinte, sem reiniciar;
- **só vale com ``balanca_habilitada`` e o agente em loopback** (como a
  biometria: com ``host`` 0.0.0.0 qualquer máquina da loja forjaria
  ``Host``/``Origin``). O ``teste`` (valores sem salvar) e o DETECTAR
  dispensam a balança ligada, não o loopback;
- **``status`` não toca na porta**: lê o que as leituras deixaram.

``estado`` das respostas é o frame cru (``peso``, ``zero``, ``instavel``,
``negativo``, ``sobrecarga``, ``sem_resposta``, ``nao_reconhecido``): quem
pede aplica a regra (só ``peso`` com kg > 0 vale; ``zero``/``negativo`` =
prato vazio).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..biometria.origem import agente_em_loopback
from ..config import AgentConfig, load_config, save_config
from . import erros
from . import porta as porta_mod
from .leitor import Deteccao, LeitorBalanca
from .protocolos import (
    EstadoFrame,
    FrameBalanca,
    LeituraBalanca,
    ROTULOS_PROTOCOLO,
    descrever_bytes,
)

log = logging.getLogger("eldensys.agent.balanca")

PROTOCOLOS: tuple[str, ...] = tuple(ROTULOS_PROTOCOLO)
BAUDS: tuple[int, ...] = (2400, 4800, 9600, 19200, 38400)
_PORTA_RE = re.compile(r"COM\d{1,3}\Z")

PADRAO_BAUD = 9600
PADRAO_PROTOCOLO = "auto"
PADRAO_TIMEOUT_MS = 1500
PADRAO_OCIOSO_S = 5.0
TIMEOUT_CONFIG_MIN_MS, TIMEOUT_CONFIG_MAX_MS = 200, 10000
TIMEOUT_LEITURA_MIN_MS, TIMEOUT_LEITURA_MAX_MS = 100, 2000
OCIOSO_MIN_S, OCIOSO_MAX_S = 0.05, 600.0

MSG_SO_LOOPBACK = (
    "A balança de checkout só funciona com o EldenSys Agent atendendo apenas este "
    "computador (host 127.0.0.1 no config.json)."
)


def _inteiro(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass(frozen=True)
class ConfigBalanca:
    habilitada: bool
    porta: str
    baud: int
    protocolo: str
    timeout_ms: int
    ocioso_s: float

    def como_dict(self) -> dict:
        return {
            "habilitada": self.habilitada,
            "porta": self.porta,
            "baud": self.baud,
            "protocolo": self.protocolo,
            "timeout_ms": self.timeout_ms,
        }


def config_balanca(cfg: object) -> ConfigBalanca:
    """As chaves ``balanca_*`` do config.json, saneadas: inválido → padrão.

    ``is True`` de propósito: ``"balanca_habilitada": "false"`` (texto)
    escrito à mão não liga a balança.
    """
    porta = getattr(cfg, "balanca_porta", "")
    porta = porta.strip().upper() if isinstance(porta, str) else ""
    if not _PORTA_RE.match(porta):
        porta = ""
    baud = getattr(cfg, "balanca_baud", PADRAO_BAUD)
    if not _inteiro(baud) or baud not in BAUDS:
        baud = PADRAO_BAUD
    protocolo = getattr(cfg, "balanca_protocolo", PADRAO_PROTOCOLO)
    protocolo = protocolo.strip().lower() if isinstance(protocolo, str) else ""
    if protocolo not in PROTOCOLOS:
        protocolo = PADRAO_PROTOCOLO
    timeout = getattr(cfg, "balanca_timeout_ms", PADRAO_TIMEOUT_MS)
    if not _inteiro(timeout) or not TIMEOUT_CONFIG_MIN_MS <= timeout <= TIMEOUT_CONFIG_MAX_MS:
        timeout = PADRAO_TIMEOUT_MS
    ocioso = getattr(cfg, "balanca_ocioso_s", PADRAO_OCIOSO_S)
    if (
        isinstance(ocioso, bool)
        or not isinstance(ocioso, (int, float))
        or not math.isfinite(ocioso)
        or not OCIOSO_MIN_S <= ocioso <= OCIOSO_MAX_S
    ):
        ocioso = PADRAO_OCIOSO_S
    return ConfigBalanca(
        habilitada=getattr(cfg, "balanca_habilitada", False) is True,
        porta=porta,
        baud=int(baud),
        protocolo=protocolo,
        timeout_ms=int(timeout),
        ocioso_s=float(ocioso),
    )


# ── validação dos pedidos (contrato: 422 requisicao_invalida) ────────────
def validar_porta(v: object, *, vazia: bool) -> str:
    if isinstance(v, str) and ((vazia and v == "") or _PORTA_RE.match(v)):
        return v
    raise erros.requisicao_invalida(
        "Porta da balança inválida: use COM1 a COM999"
        + (" (ou vazia para nenhuma)." if vazia else ".")
    )


def validar_baud(v: object) -> int:
    if _inteiro(v) and v in BAUDS:
        return int(v)  # type: ignore[arg-type]
    raise erros.requisicao_invalida(
        "Velocidade da balança inválida: use " + ", ".join(str(b) for b in BAUDS) + "."
    )


def validar_protocolo(v: object) -> str:
    if isinstance(v, str) and v in PROTOCOLOS:
        return v
    raise erros.requisicao_invalida(
        "Protocolo da balança inválido: use " + ", ".join(PROTOCOLOS) + "."
    )


def validar_timeout_config(v: object) -> int:
    if _inteiro(v) and TIMEOUT_CONFIG_MIN_MS <= v <= TIMEOUT_CONFIG_MAX_MS:  # type: ignore[operator]
        return int(v)  # type: ignore[arg-type]
    raise erros.requisicao_invalida(
        f"Tempo de leitura da balança inválido: de {TIMEOUT_CONFIG_MIN_MS} a "
        f"{TIMEOUT_CONFIG_MAX_MS} ms."
    )


def validar_timeout_leitura(v: object) -> int:
    if _inteiro(v) and TIMEOUT_LEITURA_MIN_MS <= v <= TIMEOUT_LEITURA_MAX_MS:  # type: ignore[operator]
        return int(v)  # type: ignore[arg-type]
    raise erros.requisicao_invalida(
        f"Tempo da leitura inválido: de {TIMEOUT_LEITURA_MIN_MS} a {TIMEOUT_LEITURA_MAX_MS} ms."
    )


# ── respostas ────────────────────────────────────────────────────────────
def estado_e_kg(frame: FrameBalanca | None, dados: bytes) -> tuple[str, float | None]:
    """``estado`` e ``kg`` de uma resposta. ``kg``: o peso (``peso``), 0
    (``zero``), negativo (``negativo``, quando a balança informa o valor) e
    ``null`` no resto."""
    if frame is None:
        return ("nao_reconhecido" if dados else "sem_resposta"), None
    if frame.estado is EstadoFrame.peso:
        return "peso", frame.kg
    if frame.estado is EstadoFrame.zero:
        return "zero", 0.0
    if frame.estado is EstadoFrame.negativo:
        return "negativo", (-frame.kg if frame.kg else None)
    return frame.estado.value, None


def frame_descrito(dados: bytes) -> dict:
    d = descrever_bytes(dados)
    return {"texto": d.texto, "hex": d.hex}


def resposta_leitura(leitura: LeituraBalanca, *, diagnostico: bool = False) -> dict:
    estado, kg = estado_e_kg(leitura.frame, leitura.dados)
    r: dict[str, Any] = {
        "estado": estado,
        "kg": kg,
        "protocolo": leitura.protocolo,
        "lido_como": leitura.lido_como,
        "bytes": len(leitura.dados),
    }
    if diagnostico:
        r["frame"] = frame_descrito(leitura.dados)
    return r


def resposta_deteccao(d: Deteccao) -> dict:
    estado, kg = estado_e_kg(d.frame, d.dados)
    return {
        "protocolo": d.protocolo,
        "estado": estado,
        "kg": kg,
        "bytes": len(d.dados),
        "frame": frame_descrito(d.dados),
    }


class ServicoBalanca:
    def __init__(
        self,
        *,
        load_config_fn: Callable[[], AgentConfig] = load_config,
        save_config_fn: Callable[[AgentConfig], None] = save_config,
        abrir_porta_fn: Callable[[str, int], Any] | None = None,
        listar_portas_fn: Callable[[], list[dict]] | None = None,
        relogio_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load_config = load_config_fn
        self._save_config = save_config_fn
        self._listar_portas = listar_portas_fn or porta_mod.listar_portas
        self._lock_config = threading.Lock()
        self.leitor = LeitorBalanca(
            abrir_porta_fn or porta_mod.abrir_porta, ocioso_s=self._ocioso_s, relogio=relogio_fn
        )

    # ── configuração ──────────────────────────────────
    def _ocioso_s(self) -> float:
        try:
            return config_balanca(self._load_config()).ocioso_s
        except Exception:  # noqa: BLE001 - config ilegível: o padrão serve
            return PADRAO_OCIOSO_S

    def _cfg_loopback(self) -> AgentConfig:
        cfg = self._load_config()
        if not agente_em_loopback(cfg):
            raise erros.balanca_desligada(MSG_SO_LOOPBACK)
        return cfg

    # ── consultas (sem tocar na porta) ────────────────
    def status(self) -> dict:
        """``GET /balanca/status``. Desligada não é erro: ``habilitada`` diz."""
        cfg = self._load_config()
        c = config_balanca(cfg)
        ultima = self.leitor.ultima_leitura()
        if ultima is None:
            ultima_dict = None
        else:
            estado, kg = estado_e_kg(ultima.frame, ultima.dados)
            ultima_dict = {
                "estado": estado,
                "kg": kg,
                "protocolo": ultima.protocolo,
                "lido_como": ultima.lido_como,
            }
        return {
            "versao_agente": __version__,
            "habilitada": c.habilitada and agente_em_loopback(cfg),
            "config": c.como_dict(),
            "porta": self.leitor.estado_porta(),
            "ultima_leitura": ultima_dict,
            "protocolos": [{"id": k, "rotulo": v} for k, v in ROTULOS_PROTOCOLO.items()],
            "bauds": list(BAUDS),
        }

    def portas(self) -> dict:
        try:
            portas = self._listar_portas()
        except Exception:  # noqa: BLE001 - sem lista, a tela mostra vazio
            log.exception("Falha ao listar as portas COM")
            portas = []
        return {"portas": portas}

    def capacidades(self) -> dict:
        """``capabilities.balanca_checkout`` do heartbeat (não é o ``scale``,
        que é a pasta da carga). ``ok`` = nada de errado conhecido: porta sem
        erro e a última leitura (se houve) reconheceu um frame."""
        cfg = self._load_config()
        c = config_balanca(cfg)
        configurada = c.habilitada and agente_em_loopback(cfg) and bool(c.porta)
        ultima = self.leitor.ultima_leitura()
        ok = (
            configurada
            and self.leitor.estado_porta()["erro"] is None
            and (ultima is None or ultima.frame is not None)
        )
        return {"configured": configurada, "ok": ok, "porta": c.porta, "protocolo": c.protocolo}

    # ── operações ─────────────────────────────────────
    def salvar_config(self, dados: dict) -> dict:
        """``POST /balanca/config``: só as chaves enviadas mudam. Tudo é
        validado antes de gravar (nada é gravado pela metade)."""
        self._cfg_loopback()
        mudancas: dict[str, Any] = {}
        if dados.get("habilitada") is not None:
            if not isinstance(dados["habilitada"], bool):
                raise erros.requisicao_invalida("“habilitada” precisa ser verdadeiro ou falso.")
            mudancas["balanca_habilitada"] = dados["habilitada"]
        if dados.get("porta") is not None:
            mudancas["balanca_porta"] = validar_porta(dados["porta"], vazia=True)
        if dados.get("baud") is not None:
            mudancas["balanca_baud"] = validar_baud(dados["baud"])
        if dados.get("protocolo") is not None:
            mudancas["balanca_protocolo"] = validar_protocolo(dados["protocolo"])
        if dados.get("timeout_ms") is not None:
            mudancas["balanca_timeout_ms"] = validar_timeout_config(dados["timeout_ms"])
        with self._lock_config:
            cfg = self._load_config()
            antes = config_balanca(cfg)
            for chave, valor in mudancas.items():
                setattr(cfg, chave, valor)
            if mudancas:
                self._save_config(cfg)
            depois = config_balanca(cfg)
        if (antes.porta, antes.baud) != (depois.porta, depois.baud) or not depois.habilitada:
            # A porta antiga fica livre já (o PDV offline pode usá-la).
            try:
                self.leitor.liberar()
            except erros.ErroBalanca:
                pass
        return {"config": depois.como_dict()}

    def ler(
        self,
        timeout_ms: int | None = None,
        teste: dict | None = None,
        diagnostico: bool = False,
    ) -> dict:
        """``POST /balanca/leitura``: uma leitura curta. ``teste`` usa os
        valores sem salvar e dispensa a balança ligada."""
        cfg = self._cfg_loopback()
        c = config_balanca(cfg)
        t_ms = c.timeout_ms if timeout_ms is None else validar_timeout_leitura(timeout_ms)
        if teste is not None:
            porta = validar_porta(teste.get("porta"), vazia=True)
            baud = validar_baud(teste.get("baud"))
            protocolo = validar_protocolo(teste.get("protocolo"))
        else:
            if not c.habilitada:
                raise erros.balanca_desligada()
            porta, baud, protocolo = c.porta, c.baud, c.protocolo
        if not porta:
            raise erros.sem_porta()
        leitura = self.leitor.ler(porta=porta, baud=baud, protocolo=protocolo, timeout_s=t_ms / 1000)
        return resposta_leitura(leitura, diagnostico=diagnostico is True)

    def detectar(
        self,
        porta: str | None = None,
        baud: int | None = None,
        cancelamento: threading.Event | None = None,
    ) -> dict:
        """``POST /balanca/detectar``: sem ``porta``/``baud`` usa os salvos."""
        cfg = self._cfg_loopback()
        c = config_balanca(cfg)
        p = c.porta if porta is None else validar_porta(porta, vazia=True)
        b = c.baud if baud is None else validar_baud(baud)
        if not p:
            raise erros.sem_porta()
        return resposta_deteccao(self.leitor.detectar(porta=p, baud=b, cancelamento=cancelamento))

    def liberar(self) -> dict:
        """Fecha a porta agora (antes dos 5 s de ociosidade)."""
        self.leitor.liberar()
        return {"liberada": True}

    # ── fim ───────────────────────────────────────────
    def parar(self) -> None:
        """Sair do agente: solta a COM (o PDV offline pode precisar dela)."""
        self.leitor.parar()


# ── instância do processo (a janela de configuração consulta) ────────────
_servico: ServicoBalanca | None = None


def set_servico(servico: ServicoBalanca | None) -> None:
    global _servico
    _servico = servico


def get_servico() -> ServicoBalanca | None:
    return _servico
