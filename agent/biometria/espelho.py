"""Espelho: a memória do leitor acompanha as digitais vivas da loja.

O leitor Pro compara o dedo com as ~3.000 digitais da própria memória
(``CaptureAndIdentify``); o servidor só guarda. Este módulo mantém a memória
em dia, com regras pensadas para o PDV offline que usa o MESMO leitor no
mesmo computador:

- **versão.** O poll traz ``biometria.versao`` (texto opaco). Mudou → o
  agente pede ``GET …/digitais/?versao=<a última aplicada>``; ``igual`` =
  nada a baixar nem a gravar.
- **só as lápides saem.** ``removidos`` (digitais excluídas) são apagadas do
  leitor. Um id que o agente não conhece fica: pode ser uma digital que o PDV
  acabou de cadastrar sem rede. Id desconhecido só sai quando a memória
  enche (-13) ou quando a loja ou a série do leitor mudaram (o leitor veio de
  outro lugar).
- **sem rede não apaga.** Nada é apagado sem uma lista recebida agora.
- **carga em lotes, em segundo plano.** Cada lote é uma tarefa de FUNDO do
  ``Leitor``: cede a vez a quem está esperando o dedo e devolve o leitor no
  fim (pausa curta entre lotes = janela para o PDV).

``biometria.json`` (em ``%APPDATA%\\EldenSysAgent``) guarda só versão, loja,
série e contagens — NENHUM template e nenhum id de digital. Guarda também o
último agente visto no poll (``agent_id`` e nome da loja, amarrados ao
pareamento): o status usa enquanto o processo ainda não teve poll.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AgentConfig, is_paired
from ..remote_client import (
    ModuloInativo,
    PairingCodeError,
    PairingRejected,
    RemoteError,
    ServidorRecusou,
    StoreBlocked,
)
from .cliente import LOTE_BUSCA
from .erros import (
    ID_INEXISTENTE,
    MEMORIA_CHEIA,
    SEM_LEITOR,
    USB,
    ErroBiometria,
    ErroLeitor,
)
from .leitor import Leitor
from .sdk import id_da_loja_valido

log = logging.getLogger("eldensys.agent.biometria.espelho")

LIMITE_PADRAO = 3000
#: Tempo máximo de uma tarefa de fundo (depois ela devolve o leitor).
LOTE_GRAVACAO_S = 4.0
#: Pausa entre lotes: janela para o PDV do mesmo PC abrir o leitor.
PAUSA_ENTRE_LOTES_S = 0.5
NOVA_TENTATIVA_S = 60.0
NOVA_TENTATIVA_MAX_S = 600.0
#: Despejos seguidos por gravação antes de desistir (memória cheia).
DESPEJOS_POR_GRAVACAO = 3


@dataclass
class EstadoEspelho:
    versao: str | None = None
    tenant: str = ""
    api: str = ""
    serie: str = ""
    completo: bool = False
    faltando: int = 0
    sincronizado_em: str | None = None
    #: Último agente visto no poll e o pareamento a que ele pertence
    #: (``servico.impressao_do_pareamento``). Só o poll grava estes três; a
    #: carga do espelho os preserva (``Espelho._salvar``).
    agent_id: str = ""
    loja: str = ""
    pareamento: str = ""


#: Campos que só o poll grava (``Espelho.guardar_agente``).
_CAMPOS_AGENTE = ("agent_id", "loja", "pareamento")


def ler_estado(caminho: Path) -> EstadoEspelho:
    """Lê o ``biometria.json``; qualquer problema = estado vazio (sincroniza tudo)."""
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EstadoEspelho()
    if not isinstance(dados, dict):
        return EstadoEspelho()
    estado = EstadoEspelho()
    tipos = {
        "versao": (str, type(None)),
        "tenant": str,
        "api": str,
        "serie": str,
        "completo": bool,
        "faltando": int,
        "sincronizado_em": (str, type(None)),
        "agent_id": str,
        "loja": str,
        "pareamento": str,
    }
    for f in fields(EstadoEspelho):
        valor = dados.get(f.name, getattr(estado, f.name))
        if isinstance(valor, tipos[f.name]) and not (f.name == "faltando" and isinstance(valor, bool)):
            setattr(estado, f.name, valor)
    return estado


def gravar_estado(caminho: Path, estado: EstadoEspelho) -> None:
    payload = json.dumps(asdict(estado), indent=2, ensure_ascii=False)
    tmp = caminho.with_name(caminho.name + ".tmp")
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, caminho)
    except OSError:
        log.warning("Não foi possível gravar %s", caminho, exc_info=True)


def _agora_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ids(valor: object) -> list[int]:
    if not isinstance(valor, list):
        return []
    return [v for v in valor if id_da_loja_valido(v)]


def _sem_repetir(ids: list[int]) -> list[int]:
    vistos: set[int] = set()
    saida = []
    for i in ids:
        if i not in vistos:
            vistos.add(i)
            saida.append(i)
    return saida


def _limite(valor: object) -> int:
    if type(valor) is int and valor > 0:
        return min(valor, 100_000)
    return LIMITE_PADRAO


def mensagem_remota(e: Exception) -> str:
    """Frase curta (português) para um erro do servidor, sem detalhes técnicos."""
    if isinstance(e, PairingRejected):
        return "Pareamento recusado pelo EldenSys. Gere um novo código."
    if isinstance(e, ModuloInativo):
        return "O módulo de biometria não está ligado nesta loja."
    if isinstance(e, StoreBlocked):
        return "Loja bloqueada ou inativa no EldenSys."
    if isinstance(e, ServidorRecusou):
        return e.detail or "O EldenSys recusou o pedido."
    if isinstance(e, PairingCodeError):
        return "Pareamento inválido. Gere um novo código."
    return "Sem conexão com o EldenSys."


@dataclass
class _Lote:
    processados: int
    falhas: int
    cheia: bool


class Espelho:
    def __init__(
        self,
        *,
        leitor: Leitor,
        cliente_fn: Callable[[AgentConfig], Any],
        load_config_fn: Callable[[], AgentConfig],
        caminho_estado: Path,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._leitor = leitor
        self._cliente_fn = cliente_fn
        self._load_config = load_config_fn
        self._caminho = caminho_estado
        self._relogio = relogio

        self._lock = threading.Lock()
        #: Duas threads gravam o arquivo (carga do espelho e poll): uma por vez.
        self._lock_disco = threading.Lock()
        self._estado: EstadoEspelho | None = None
        self._thread: threading.Thread | None = None
        self._em_andamento = False
        self._progresso: int | None = None
        self._erro: str | None = None
        self._proxima_tentativa = 0.0
        self._espera = NOVA_TENTATIVA_S
        self._parar = threading.Event()
        #: Última lista da loja (só ids), para escolher quem sai com memória cheia.
        self._prioridade: list[int] = []
        self._clientes: frozenset[int] = frozenset()

    # ── estado ────────────────────────────────────────
    def _estado_atual(self) -> EstadoEspelho:
        with self._lock:
            if self._estado is None:
                self._estado = ler_estado(self._caminho)
            return self._estado

    def _salvar(self, estado: EstadoEspelho) -> None:
        """Grava o estado do espelho. Os campos do agente ficam como estão: a
        carga começou antes e não pode desfazer um poll que chegou no meio."""
        self._estado_atual()
        with self._lock:
            atual = self._estado
            self._estado = replace(estado, **{c: getattr(atual, c) for c in _CAMPOS_AGENTE})
        self._gravar()

    def _gravar(self) -> None:
        # Cada gravação leva o estado MAIS NOVO (lido já com a vez no disco):
        # a última a terminar nunca é uma versão velha.
        with self._lock_disco:
            with self._lock:
                estado = self._estado
            gravar_estado(self._caminho, estado)

    # ── último agente visto no poll ───────────────────
    def agente_salvo(self, pareamento: str) -> tuple[str, str] | None:
        """``(agent_id, loja)`` do último poll, se for deste pareamento."""
        e = self._estado_atual()
        if e.agent_id and pareamento and e.pareamento == pareamento:
            return e.agent_id, e.loja
        return None

    def guardar_agente(self, agent_id: str, loja: str, pareamento: str) -> None:
        """Anota o agente do poll; o disco só é tocado quando algo muda (o
        poll vem a cada poucos segundos)."""
        self._estado_atual()
        with self._lock:
            atual = self._estado
            if (atual.agent_id, atual.loja, atual.pareamento) == (agent_id, loja, pareamento):
                return
            self._estado = replace(atual, agent_id=agent_id, loja=loja, pareamento=pareamento)
        self._gravar()

    def estado(self) -> dict:
        e = self._estado_atual()
        with self._lock:
            return {
                "versao": e.versao,
                "completo": e.completo,
                "faltando": e.faltando,
                "sincronizado_em": e.sincronizado_em,
                "em_andamento": self._em_andamento,
                "progresso": self._progresso,
                "erro": self._erro,
            }

    def primeira_carga_em_andamento(self) -> bool:
        """Nunca completou uma carga e está carregando agora (1:N ainda cego)."""
        e = self._estado_atual()
        with self._lock:
            return self._em_andamento and e.versao is None

    # ── disparo ───────────────────────────────────────
    def sincronizar(self, *, forcar: bool = False) -> dict:
        """Começa uma sincronização em segundo plano (se não houver uma)."""
        with self._lock:
            if not self._em_andamento:
                self._em_andamento = True
                self._progresso = 0
                self._erro = None
                self._parar.clear()
                self._thread = threading.Thread(
                    target=self._rodar, args=(forcar,), daemon=True, name="biometria-espelho"
                )
                self._thread.start()
            return {"em_andamento": True, "progresso": self._progresso}

    def ao_poll(self, versao: str | None) -> None:
        """Chamado a cada poll com a versão do conjunto de digitais da loja."""
        cfg = self._load_config()
        e = self._estado_atual()
        outra_loja = e.tenant != cfg.server_tenant or e.api != cfg.server_url
        mudou = e.versao is None or outra_loja or (versao is not None and versao != e.versao)
        with self._lock:
            pode = not self._em_andamento and self._relogio() >= self._proxima_tentativa
        if mudou and pode:
            self.sincronizar()

    def parar(self, timeout: float = 2.0) -> None:
        self._parar.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _rodar(self, forcar: bool) -> None:
        erro: str | None = None
        try:
            self.sincronizar_agora(forcar=forcar)
        except ErroBiometria as e:
            erro = e.mensagem
        except (RemoteError, PairingCodeError) as e:
            erro = mensagem_remota(e)
        except Exception:
            log.exception("Falha inesperada ao sincronizar as digitais")
            erro = "Falha inesperada ao sincronizar as digitais."
        with self._lock:
            if erro is not None:
                self._proxima_tentativa = self._relogio() + self._espera
                self._espera = min(self._espera * 2, NOVA_TENTATIVA_MAX_S)
                self._erro = erro
                log.warning("Espelho do leitor: %s", erro)
            else:
                self._espera = NOVA_TENTATIVA_S
                self._proxima_tentativa = 0.0
            self._em_andamento = False
            self._progresso = None

    def _marcar_progresso(self, feitos: int, total: int) -> None:
        with self._lock:
            self._progresso = 100 if total <= 0 else min(100, int(100 * feitos / total))

    def _pausa(self) -> None:
        if self._parar.wait(PAUSA_ENTRE_LOTES_S):
            raise ErroBiometria("biometria_desligada", "Sincronização interrompida.", status=503)

    # ── sincronização (thread do espelho; o leitor só por tarefas de fundo) ──
    def sincronizar_agora(self, *, forcar: bool = False) -> None:
        cfg = self._load_config()
        if not is_paired(cfg):
            raise ErroBiometria(
                "nao_pareado", "O EldenSys Agent não está pareado com uma loja.", status=409
            )
        cliente = self._cliente_fn(cfg)
        anterior = self._estado_atual()
        mesma_loja = anterior.tenant == cfg.server_tenant and anterior.api == cfg.server_url
        versao_envio = anterior.versao if (mesma_loja and not forcar) else None

        resp = cliente.digitais(versao_envio)  # sem rede: levanta e nada é apagado
        if resp.get("igual") is True and versao_envio is not None:
            self._salvar(
                EstadoEspelho(**{**asdict(anterior), "sincronizado_em": _agora_iso()})
            )
            return

        versao_nova = resp.get("versao")
        versao_nova = str(versao_nova) if versao_nova not in (None, "") else None
        clientes = _ids(resp.get("clientes"))
        desejados = _sem_repetir(_ids(resp.get("usuarios")) + clientes)[: _limite(resp.get("limite"))]
        desejados_set = set(desejados)
        removidos = set(_ids(resp.get("removidos"))) - desejados_set
        with self._lock:
            self._prioridade = desejados
            self._clientes = frozenset(clientes)

        info, lidos = self._leitor.executar(
            lambda sdk, ctx: (sdk.info(), sdk.ids()), usuario=False, nome="espelho"
        )
        ids_leitor = set(lidos)
        # GetTemplateIDs respondeu: só o iDBio Pro responde (o comum dá -16).
        self._leitor.anotar(
            conectado=True,
            pro=True,
            modelo=info.modelo,
            serie=info.serie,
            firmware=info.firmware,
            digitais=len(ids_leitor),
        )
        trocou = bool(
            (anterior.tenant and anterior.tenant != cfg.server_tenant)
            or (anterior.api and anterior.api != cfg.server_url)
            or (anterior.serie and info.serie and anterior.serie != info.serie)
        )
        apagar = sorted(i for i in ids_leitor if i in removidos)
        if trocou:
            # Leitor de outra loja ou outro aparelho: o que não é desta loja sai.
            log.info("Espelho: loja ou leitor trocou; ids desconhecidos serão apagados")
            apagar += sorted(i for i in ids_leitor if i not in desejados_set and i not in removidos)
        faltam = [i for i in desejados if i not in ids_leitor]
        total = len(apagar) + len(faltam)
        feitos = 0
        self._marcar_progresso(0, total)

        pendentes = list(apagar)
        while pendentes:
            n = self._leitor.executar(
                lambda sdk, ctx, lote=pendentes: self._apagar_lote(sdk, ctx, lote, ids_leitor),
                usuario=False,
                nome="espelho",
            )
            pendentes = pendentes[n:]
            feitos += n
            self._marcar_progresso(feitos, total)
            if pendentes:
                self._pausa()

        desconhecidos = [i for i in sorted(ids_leitor) if i not in desejados_set]
        cheia = False
        for k in range(0, len(faltam), LOTE_BUSCA):
            if self._parar.is_set():
                raise ErroBiometria("biometria_desligada", "Sincronização interrompida.", status=503)
            lote_ids = faltam[k : k + LOTE_BUSCA]
            fila = [(d, t) for d, t in cliente.buscar(lote_ids) if d in desejados_set]
            feitos += len(lote_ids) - len(fila)  # sumiram no servidor entre a lista e a busca
            while fila:
                r: _Lote = self._leitor.executar(
                    lambda sdk, ctx, f=fila: self._gravar_lote(sdk, ctx, f, desconhecidos, ids_leitor),
                    usuario=False,
                    nome="espelho",
                )
                fila = fila[r.processados :]
                feitos += r.processados
                self._marcar_progresso(feitos, total)
                if r.cheia:
                    cheia = True
                    break
                self._pausa()
            if cheia:
                break

        faltando = sum(1 for i in desejados if i not in ids_leitor)
        self._leitor.anotar(digitais=len(ids_leitor))
        self._salvar(
            EstadoEspelho(
                versao=versao_nova,
                tenant=cfg.server_tenant,
                api=cfg.server_url,
                serie=info.serie or anterior.serie,
                completo=faltando == 0,
                faltando=faltando,
                sincronizado_em=_agora_iso(),
            )
        )
        if cheia:
            with self._lock:
                self._erro = f"Memória do leitor cheia: {faltando} digitais ficaram de fora."
            log.warning("Espelho: memória do leitor cheia (%d de fora)", faltando)

    # ── tarefas de fundo (rodam na thread do leitor) ──
    @staticmethod
    def _apagar_lote(sdk: Any, ctx: Any, lote: list[int], ids_leitor: set[int]) -> int:
        inicio = time.monotonic()
        feitos = 0
        for device_id in lote:
            if feitos and (ctx.ceder() or time.monotonic() - inicio > LOTE_GRAVACAO_S):
                break
            try:
                sdk.apagar(device_id)
            except ErroLeitor as e:
                if e.codigo != ID_INEXISTENTE:
                    raise
            ids_leitor.discard(device_id)
            feitos += 1
        return feitos

    def _gravar_lote(
        self,
        sdk: Any,
        ctx: Any,
        fila: list[tuple[int, str]],
        desconhecidos: list[int],
        ids_leitor: set[int],
    ) -> _Lote:
        inicio = time.monotonic()
        processados = falhas = 0
        for device_id, template in fila:
            if processados and (ctx.ceder() or time.monotonic() - inicio > LOTE_GRAVACAO_S):
                break
            try:
                self._salvar_com_despejo(sdk, device_id, template, desconhecidos, ids_leitor)
            except ErroLeitor as e:
                if e.codigo == MEMORIA_CHEIA:
                    return _Lote(processados, falhas, True)
                if e.codigo in (SEM_LEITOR, USB):
                    raise
                # -9 (já gravada com outro id), -22…: segue com as demais.
                falhas += 1
                log.warning("Espelho: digital %s não gravada (código %s)", device_id, e.codigo)
            processados += 1
        return _Lote(processados, falhas, False)

    @staticmethod
    def _salvar_com_despejo(
        sdk: Any, device_id: int, template: str, candidatos: list[int], ids_leitor: set[int]
    ) -> None:
        """``SaveTemplate``; com memória cheia tira um id desconhecido e tenta de novo."""
        while True:
            try:
                sdk.salvar(device_id, template)
                ids_leitor.add(device_id)
                return
            except ErroLeitor as e:
                if e.codigo != MEMORIA_CHEIA or not candidatos:
                    raise
                vitima = candidatos.pop(0)
                log.info("Espelho: memória cheia, apagando id desconhecido do leitor")
                try:
                    sdk.apagar(vitima)
                except ErroLeitor as e2:
                    if e2.codigo != ID_INEXISTENTE:
                        raise
                ids_leitor.discard(vitima)

    # ── dentro de uma operação do usuário ─────────────
    def candidatos_despejo(self, sdk: Any, protegidos: set[int]) -> list[int]:
        """Quem pode sair da memória: desconhecidos, depois os clientes menos prioritários."""
        with self._lock:
            prioridade = list(self._prioridade)
            clientes = self._clientes
        presentes = set(sdk.ids())
        desejados = set(prioridade)
        desconhecidos = sorted(i for i in presentes if i not in desejados and i not in protegidos)
        menos_usados = [
            i
            for i in reversed(prioridade)
            if i in clientes and i in presentes and i not in protegidos
        ]
        return desconhecidos + menos_usados

    def garantir(self, sdk: Any, digitais: list[tuple[int, str]]) -> None:
        """Grava na memória as digitais da pessoa-alvo antes de identificar.

        Sobrescrever o mesmo id é inofensivo (aviso 3). Memória cheia tira
        alguém (``candidatos_despejo``) e tenta de novo.
        """
        protegidos = {d for d, _ in digitais}
        candidatos: list[int] | None = None
        for device_id, template in digitais:
            for _ in range(DESPEJOS_POR_GRAVACAO + 1):
                try:
                    sdk.salvar(device_id, template)
                    break
                except ErroLeitor as e:
                    if e.codigo != MEMORIA_CHEIA:
                        # -9: o mesmo dedo já está com outro id; identifica igual.
                        log.info("Garantir digital no leitor: código %s", e.codigo)
                        break
                    if candidatos is None:
                        candidatos = self.candidatos_despejo(sdk, protegidos)
                    if not candidatos:
                        raise
                    self._despejar(sdk, candidatos.pop(0))
            else:
                raise ErroLeitor(MEMORIA_CHEIA, "garantir")

    def liberar_espaco(self, sdk: Any, protegidos: set[int]) -> bool:
        """Tira UMA digital da memória para caber outra (cadastro com memória cheia)."""
        candidatos = self.candidatos_despejo(sdk, protegidos)
        if not candidatos:
            return False
        self._despejar(sdk, candidatos[0])
        return True

    def _despejar(self, sdk: Any, device_id: int) -> None:
        try:
            sdk.apagar(device_id)
        except ErroLeitor as e:
            if e.codigo != ID_INEXISTENTE:
                raise
        with self._lock:
            desejada = device_id in self._prioridade
        if desejada:
            e = self._estado_atual()
            self._salvar(
                EstadoEspelho(**{**asdict(e), "completo": False, "faltando": e.faltando + 1})
            )
