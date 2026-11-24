"""Fachada da biometria: o que as rotas, a bandeja e o heartbeat usam.

Junta o ``Leitor`` (thread dona do aparelho), o ``Espelho`` (memória do
leitor = digitais vivas da loja), os ``Cadastros`` em passos e o cliente das
rotas do servidor. Regras que valem para todas as operações:

- **nada toca o SDK antes das conferências baratas**: biometria ligada neste
  PC (``biometria_habilitada`` e agente só em loopback), pareado, mesma loja
  que a página pediu (``tenant``), pareamento não recusado, módulo ligado;
- **o veredito é do servidor**: o agente informa ``device_id``/score direto
  ao EldenSys com o token dele e só repassa a resposta ao navegador;
- **o template nunca sai daqui para o navegador** (nem em erro, nem em log).

Construir o serviço não tem efeito nenhum: sem thread, sem DLL, sem disco.
Tudo começa preguiçosamente no primeiro uso.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import APP_DIR, AgentConfig, is_paired, load_config
from ..remote_client import RemoteError
from . import sdk as sdk_mod
from .cadastro import Cadastros
from .cliente import ClienteBiometria, digitais_validas, uuid_canonico
from .erros import (
    CAPTURA_SEM_DEDO,
    NAO_IDENTIFICADO,
    NAO_PRO,
    BiometriaDesligada,
    ErroBiometria,
    ErroLeitor,
    erro_remoto,
    passo_invalido,
    requisicao_invalida,
)
from .espelho import Espelho
from .leitor import OCIOSO_PADRAO_S, Cancelamento, Leitor
from .origem import agente_em_loopback

log = logging.getLogger("eldensys.agent.biometria")

ARQUIVO_ESTADO = "biometria.json"
#: PKCE S256: base64url sem padding de 32 bytes.
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
#: Chaves da resposta do servidor que NUNCA voltam ao navegador.
_CHAVES_PROIBIDAS = frozenset({"template", "templates", "alvo_digitais", "digitais"})
#: Capacidade documentada do iDBio Pro (o SDK não informa).
CAPACIDADE_PADRAO = 3000


def biometria_ligada(cfg: AgentConfig) -> bool:
    return getattr(cfg, "biometria_habilitada", True) is True and agente_em_loopback(cfg)


def _status_remoto_padrao() -> Any:
    from ..remote_jobs import get_worker

    worker = get_worker()
    return worker.get_status() if worker else None


def _veredito(resp: dict) -> dict:
    """Resposta do servidor para o navegador, sem nada que pareça template."""
    return {k: v for k, v in resp.items() if k not in _CHAVES_PROIBIDAS}


class ServicoBiometria:
    def __init__(
        self,
        *,
        load_config_fn: Callable[[], AgentConfig] = load_config,
        carregar_sdk_fn: Callable[[], Any] = sdk_mod.carregar,
        dll_presente_fn: Callable[[], bool] = sdk_mod.dll_presente,
        cliente_fn: Callable[[AgentConfig], Any] = ClienteBiometria.da_config,
        status_remoto_fn: Callable[[], Any] = _status_remoto_padrao,
        caminho_estado: Path | None = None,
    ) -> None:
        self._load_config = load_config_fn
        self._dll_presente = dll_presente_fn
        self._cliente_fn = cliente_fn
        self._status_remoto = status_remoto_fn
        self.leitor = Leitor(carregar_sdk_fn, ocioso_s=self._ocioso_s)
        self.espelho = Espelho(
            leitor=self.leitor,
            cliente_fn=cliente_fn,
            load_config_fn=load_config_fn,
            caminho_estado=caminho_estado or (APP_DIR / ARQUIVO_ESTADO),
        )
        self.cadastros = Cadastros(
            leitor=self.leitor, espelho=self.espelho, ao_abrir=self._info_se_preciso
        )
        self._lock = threading.Lock()
        #: O que o último poll disse: módulo ligado? este PC liberado? versão?
        self._modulo: bool | None = None
        self._liberado: bool | None = None
        self._versao_servidor: str | None = None
        self._agent_id: str | None = None
        self._loja = ""
        self._sondado = False

    # ── configuração ──────────────────────────────────
    def _ocioso_s(self) -> float:
        try:
            return float(getattr(self._load_config(), "biometria_ocioso_s", OCIOSO_PADRAO_S))
        except (TypeError, ValueError):
            return OCIOSO_PADRAO_S

    def _cfg_ligada(self) -> AgentConfig:
        cfg = self._load_config()
        if getattr(cfg, "biometria_habilitada", True) is not True:
            raise BiometriaDesligada(
                "A biometria está desligada neste computador (configuração do EldenSys Agent)."
            )
        if not agente_em_loopback(cfg):
            raise BiometriaDesligada(
                "A biometria só funciona com o EldenSys Agent atendendo apenas este "
                "computador (host 127.0.0.1 no config.json)."
            )
        return cfg

    def _cfg_da_loja(self, tenant: str | None) -> AgentConfig:
        """Conferências que não tocam o leitor nem a rede (409/403 baratos)."""
        cfg = self._cfg_ligada()
        if not is_paired(cfg):
            raise ErroBiometria(
                "nao_pareado",
                "O EldenSys Agent deste computador não está pareado com a loja. Pareie em "
                "Configurações → Impressão → Agentes da loja.",
                status=409,
            )
        if tenant is not None:
            if not isinstance(tenant, str) or not _TENANT_RE.fullmatch(tenant):
                raise requisicao_invalida("Loja inválida no pedido.")
            if tenant != cfg.server_tenant:
                raise ErroBiometria(
                    "outra_loja",
                    "O EldenSys Agent deste computador está pareado com outra loja.",
                    status=409,
                )
        status = self._status_remoto_seguro()
        if status is not None and getattr(status, "state", "") == "rejected":
            raise ErroBiometria(
                "pareamento_recusado",
                "O EldenSys recusou o pareamento deste agente. Gere um novo código.",
                status=409,
            )
        with self._lock:
            modulo = self._modulo
        if modulo is False:
            raise ErroBiometria(
                "modulo_inativo", "O módulo de biometria não está ligado nesta loja.", status=403
            )
        return cfg

    def _status_remoto_seguro(self) -> Any:
        try:
            return self._status_remoto()
        except Exception:  # noqa: BLE001
            return None

    def _cliente(self, cfg: AgentConfig) -> Any:
        try:
            return self._cliente_fn(cfg)
        except Exception as e:  # PairingCodeError: URL do pareamento inválida
            raise erro_remoto(e) from None

    def _leitor_payload(self) -> dict:
        f = self.leitor.fatos()
        return {"modelo": f.modelo or "", "serie": f.serie or "", "firmware": f.firmware or ""}

    def _info_se_preciso(self, sdk: Any) -> None:
        """Lê modelo/série/firmware (sem dedo) se ainda não se sabe: o servidor
        registra o leitor de cada resultado na auditoria."""
        if self.leitor.fatos().serie:
            return
        try:
            info = sdk.info()
        except ErroLeitor as e:
            if e.status == 503:  # leitor sumiu: a operação não tem como seguir
                raise
            return
        self.leitor.anotar(modelo=info.modelo, serie=info.serie, firmware=info.firmware)

    def _exigir_espelho_pronto(self) -> None:
        """1:N com a memória ainda vazia acharia ninguém: avisa e mostra o progresso."""
        if self.espelho.primeira_carga_em_andamento():
            raise ErroBiometria(
                "sincronizando",
                "O leitor ainda está recebendo as digitais da loja. Aguarde um instante.",
                status=503,
                progresso=self.espelho.estado().get("progresso"),
            )

    # ── consultas (sem tocar no leitor) ───────────────
    def status(self) -> dict:
        """``GET /biometria/status``. Desligada neste PC = 503 ``biometria_desligada``
        (a web distingue "desligada" de "leitor desconectado")."""
        cfg = self._cfg_ligada()
        estado_leitor = self.leitor.estado()
        estado_leitor.pop("aberto", None)
        espelho = self.espelho.estado()
        sdk = self.leitor.sdk_estado()
        remoto = self._status_remoto_seguro()
        with self._lock:
            agent_id, loja = self._agent_id, self._loja
        pareado = is_paired(cfg)
        return {
            "versao_agente": __version__,
            "sdk": {
                "presente": bool(self._dll_presente()),
                "carregado": sdk["carregado"],
                "erro": sdk["erro"],
            },
            "leitor": {**estado_leitor, "capacidade": CAPACIDADE_PADRAO},
            "pareamento": {
                "pareado": pareado,
                "estado": getattr(remoto, "state", None) if remoto else None,
                "agent_id": agent_id if pareado else None,
                "loja": (loja or getattr(remoto, "store_name", "") or None) if pareado else None,
                "tenant": cfg.server_tenant if pareado else None,
                "api": cfg.server_url if pareado else None,
            },
            "espelho": espelho,
        }

    def capacidades(self) -> dict:
        """``capabilities.biometria`` do heartbeat (contrato, seção 2.2)."""
        cfg = self._load_config()
        f = self.leitor.fatos()
        travado = self.leitor.estado()["travado"]
        espelho = self.espelho.estado()
        presente = bool(self._dll_presente())
        sdk = self.leitor.sdk_estado()
        return {
            "configured": biometria_ligada(cfg) and presente,
            "ok": bool(f.conectado) and f.pro is True and not travado,
            "sdk": presente and sdk["erro"] is None,
            "pro": f.pro,
            "modelo": f.modelo,
            "serie": f.serie,
            "firmware": f.firmware,
            "digitais": f.digitais,
            "espelho_versao": espelho["versao"],
            "espelho_completo": espelho["completo"],
        }

    def descricao_poll(self) -> dict:
        """Módulo/liberação vistos no último poll (janela de configuração)."""
        with self._lock:
            return {"modulo": self._modulo, "liberado": self._liberado}

    # ── poll ──────────────────────────────────────────
    def ao_poll(self, resp: dict) -> None:
        """Lê ``biometria{habilitado, versao}`` da resposta do poll."""
        agent = resp.get("agent") if isinstance(resp.get("agent"), dict) else {}
        bio = resp.get("biometria")
        with self._lock:
            self._agent_id = str(agent.get("id") or "") or None
            self._loja = str(resp.get("store_name") or "")
            if not isinstance(bio, dict):
                # Chave ausente = módulo desligado na loja (contrato).
                self._modulo, self._liberado, self._versao_servidor = False, None, None
                return
            self._modulo = True
            self._liberado = bio.get("habilitado") is True
            versao = bio.get("versao")
            self._versao_servidor = versao if isinstance(versao, str) else None
            liberado, versao, sondar = self._liberado, self._versao_servidor, not self._sondado
        cfg = self._load_config()
        if not biometria_ligada(cfg) or not self._dll_presente():
            return
        if liberado:
            self.espelho.ao_poll(versao)
        elif sondar:
            self.sondar()

    def sondar(self) -> None:
        """Uma leitura de fundo (info + ids) para o heartbeat mostrar o leitor."""
        with self._lock:
            if self._sondado:
                return
            self._sondado = True

        def tarefa(sdk: Any, ctx: Any) -> None:
            self._anotar_info(sdk)

        try:
            self.leitor.enviar(tarefa, usuario=False, nome="sondar")
        except ErroBiometria:
            pass

    def _anotar_info(self, sdk: Any) -> dict:
        info = sdk.info()
        self.leitor.anotar(conectado=True, modelo=info.modelo, serie=info.serie, firmware=info.firmware)
        try:
            ids = sdk.ids()
        except ErroLeitor as e:
            if e.codigo != NAO_PRO:
                raise
            self.leitor.anotar(pro=False, digitais=None)
            return {**info.como_dict(), "pro": False, "digitais": None}
        self.leitor.anotar(pro=True, digitais=len(ids))
        return {**info.como_dict(), "pro": True, "digitais": len(ids)}

    # ── operações ─────────────────────────────────────
    def testar(self) -> dict:
        """Abre o leitor, lê modelo/série/firmware e conta as digitais."""
        self._cfg_ligada()
        dados = self.leitor.executar(
            lambda sdk, ctx: self._anotar_info(sdk), usuario=True, nome="testar"
        )
        return {"ok": True, **dados}

    def cancelar(self) -> dict:
        return {"cancelado": self.leitor.cancelar()}

    def sincronizar(self) -> dict:
        self._cfg_da_loja(None)
        return self.espelho.sincronizar(forcar=True)

    def identificar(
        self, desafio_id: object, tenant: str | None = None, cancelamento: Cancelamento | None = None
    ) -> dict:
        cfg = self._cfg_da_loja(tenant)
        try:
            did = uuid_canonico(desafio_id)  # type: ignore[arg-type]
        except ValueError:
            raise requisicao_invalida("Pedido de digital inválido.") from None
        cliente = self._cliente(cfg)
        try:
            desafio = cliente.desafio(did)
        except RemoteError as e:
            raise erro_remoto(e) from None
        finalidade, status = desafio.get("finalidade"), desafio.get("status")
        if finalidade == "cadastro":
            raise passo_invalido("Este pedido é de cadastro de digital; use o cadastro.")
        if status != "pendente":
            return {
                "status": str(status or "invalido"),
                "motivo": str(desafio.get("motivo") or "Este pedido já foi usado ou expirou."),
            }
        alvo = digitais_validas(desafio.get("alvo_digitais"))
        if not alvo:
            # Sem pessoa-alvo é 1:N na memória inteira (supervisor, achar cliente).
            self._exigir_espelho_pronto()
        if cancelamento is not None:
            cancelamento.exigir_ativo("identificar")

        def tarefa(sdk: Any, ctx: Any) -> Any:
            self._info_se_preciso(sdk)
            if alvo:
                self.espelho.garantir(sdk, alvo)
            return ctx.capturar(sdk.identificar)

        try:
            ident = self.leitor.executar(
                tarefa, usuario=True, nome="identificar", captura=True, cancelamento=cancelamento
            )
        except ErroLeitor as e:
            if e.codigo in CAPTURA_SEM_DEDO:
                return {"status": e.ident, "mensagem": e.mensagem}
            if e.codigo != NAO_IDENTIFICADO:
                raise
            ident = None
        try:
            resp = cliente.resultado(
                did,
                device_id=ident.device_id if ident else None,
                erro=None if ident else "nao_identificado",
                score=ident.score if ident else 0,
                qualidade=ident.qualidade if ident else 0,
                leitor=self._leitor_payload(),
            )
        except RemoteError as e:
            raise erro_remoto(e) from None
        return _veredito(resp)

    def login(
        self, challenge: object, tenant: str | None = None, cancelamento: Cancelamento | None = None
    ) -> dict:
        cfg = self._cfg_da_loja(tenant)
        if not isinstance(challenge, str) or not _CHALLENGE_RE.fullmatch(challenge):
            raise requisicao_invalida("Pedido de entrada inválido.")
        self._exigir_espelho_pronto()
        cliente = self._cliente(cfg)

        def tarefa(sdk: Any, ctx: Any) -> Any:
            self._info_se_preciso(sdk)
            return ctx.capturar(sdk.identificar)

        ident = self.leitor.executar(
            tarefa, usuario=True, nome="login", captura=True, cancelamento=cancelamento
        )
        try:
            resp = cliente.login(
                device_id=ident.device_id,
                score=ident.score,
                qualidade=ident.qualidade,
                challenge=challenge,
                leitor=self._leitor_payload(),
            )
        except RemoteError as e:
            raise erro_remoto(e) from None
        return {k: resp.get(k) for k in ("ticket", "expira_em", "nome", "email", "tenant", "loja")}

    # ── cadastro ──────────────────────────────────────
    def cadastro_iniciar(self, desafio_id: object, tenant: str | None = None) -> dict:
        cfg = self._cfg_da_loja(tenant)
        try:
            did = uuid_canonico(desafio_id)  # type: ignore[arg-type]
        except ValueError:
            raise requisicao_invalida("Pedido de cadastro inválido.") from None
        cliente = self._cliente(cfg)
        try:
            desafio = cliente.desafio(did)
        except RemoteError as e:
            raise erro_remoto(e) from None
        return self.cadastros.iniciar(desafio_id=did, desafio=desafio)

    def cadastro_verificar(self, sessao_id: str, cancelamento: Cancelamento | None = None) -> dict:
        cfg = self._cfg_da_loja(None)
        return self.cadastros.verificar(
            sessao_id,
            cliente=self._cliente(cfg),
            leitor_info=self._leitor_payload,
            cancelamento=cancelamento,
        )

    def cadastro_capturar(self, sessao_id: str, cancelamento: Cancelamento | None = None) -> dict:
        cfg = self._cfg_da_loja(None)
        return self.cadastros.capturar(
            sessao_id,
            mostrar_previa=getattr(cfg, "biometria_previa", True) is True,
            cancelamento=cancelamento,
        )

    def cadastro_concluir(self, sessao_id: str) -> dict:
        cfg = self._cfg_da_loja(None)
        return self.cadastros.concluir(
            sessao_id, cliente=self._cliente(cfg), leitor_info=self._leitor_payload
        )

    def cadastro_descartar(self, sessao_id: str) -> dict:
        return self.cadastros.descartar(sessao_id)

    # ── fim ───────────────────────────────────────────
    def parar(self) -> None:
        """Sair do agente: solta o leitor (o PDV pode precisar dele)."""
        self.cadastros.encerrar_todas()
        self.espelho.parar()
        self.leitor.parar()


# ── instância do processo (a janela de configuração consulta) ────────────
_servico: ServicoBiometria | None = None


def set_servico(servico: ServicoBiometria | None) -> None:
    global _servico
    _servico = servico


def get_servico() -> ServicoBiometria | None:
    return _servico
