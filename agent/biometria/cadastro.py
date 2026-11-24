"""Cadastro de digital pela retaguarda, em passos (contrato, seção 3).

    iniciar → verificar → captura ×3 → concluir     (ou descartar)

- **verificar**: uma identificação 1:N no leitor; o SERVIDOR decide se o dedo
  já é de outra pessoa (``recusado``) ou se está livre (``verificado``).
- **captura**: três leituras com qualidade ≥ 50; a prévia é uma PNG de até
  96 px, levemente desfocada (serve para a pessoa ver que leu, não para
  reconstruir a digital).
- **concluir**: ``MergeTemplates`` → ``SaveTemplate`` NO LEITOR PRIMEIRO →
  ``POST …/digital/``. O id vem do desafio (``digital_id``/``device_id``), então
  o leitor pode gravar antes do servidor confirmar. Servidor recusou → apaga
  do leitor. Rede caiu → a sessão guarda o template mesclado e um novo
  ``concluir`` só repete o envio, sem pedir o dedo de novo.

Os templates moram SÓ na memória desta sessão (nunca em disco ou log) e são
apagados ao concluir, descartar ou expirar.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from ..remote_client import RemoteError, ServidorRecusou
from .cliente import uuid_canonico
from .erros import (
    CAPTURA_SEM_DEDO,
    ID_INEXISTENTE,
    JA_CADASTRADA,
    MEMORIA_CHEIA,
    MERGE,
    NAO_IDENTIFICADO,
    TEMPLATE_INVALIDO,
    ErroBiometria,
    ErroLeitor,
    LeitorOcupado,
    erro_remoto,
    passo_invalido,
)
from .leitor import Cancelamento, Leitor
from .sdk import Captura, device_id_de, id_da_loja_valido

log = logging.getLogger("eldensys.agent.biometria.cadastro")

PASSOS = 4
CAPTURAS = 3
QUALIDADE_MINIMA = 50
PREVIA_MAX_PX = 96
PREVIA_DESFOQUE = 0.8
TTL_PADRAO_S = 120.0
TTL_MIN_S, TTL_MAX_S = 30.0, 600.0
MAX_SESSOES = 4
#: Depois de concluir, a sessão fica um pouco para responder a um "concluir"
#: repetido (a resposta pode ter se perdido no navegador).
GUARDA_CONCLUIDA_S = 120.0
#: Leituras ruins que só pedem outra tentativa (não são erro de leitor).
_LEITURA_FALHOU = CAPTURA_SEM_DEDO | {-5, TEMPLATE_INVALIDO}


def previa_png(imagem: bytes, largura: int, altura: int, *, max_px: int = PREVIA_MAX_PX) -> str | None:
    """PNG pequena (≤ ``max_px``), levemente desfocada, em base64 (sem ``data:``)."""
    if not imagem or largura <= 0 or altura <= 0 or len(imagem) < largura * altura:
        return None
    try:
        from PIL import Image, ImageFilter

        img = Image.frombytes("L", (largura, altura), bytes(imagem[: largura * altura]))
        img.thumbnail((max_px, max_px))
        img = img.filter(ImageFilter.GaussianBlur(radius=PREVIA_DESFOQUE))
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 - sem prévia não impede o cadastro
        log.warning("Falha ao montar a prévia da digital", exc_info=True)
        return None


def _ttl(expira_em: object) -> float:
    if isinstance(expira_em, str) and expira_em.strip():
        try:
            fim = datetime.fromisoformat(expira_em.strip().replace("Z", "+00:00"))
            if fim.tzinfo is None:
                fim = fim.replace(tzinfo=timezone.utc)
            restante = (fim - datetime.now(timezone.utc)).total_seconds()
            return min(max(restante, TTL_MIN_S), TTL_MAX_S)
        except ValueError:
            pass
    return TTL_PADRAO_S


@dataclass(eq=False)
class Sessao:
    id: str
    desafio_id: str
    digital_id: str
    device_id: int
    nome: str
    expira_em: float
    verificado: bool = False
    #: Recadastro do mesmo dedo: id antigo que a verificação achou (o servidor
    #: disse que é a mesma pessoa) — sai do leitor se o novo esbarrar nele.
    mesma_pessoa_id: int | None = None
    templates: list[str] = field(default_factory=list, repr=False)
    qualidades: list[int] = field(default_factory=list)
    mesclado: str | None = field(default=None, repr=False)
    gravada_no_leitor: bool = False
    resposta: dict | None = None
    descartada: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def limpar_templates(self) -> None:
        self.templates.clear()
        self.mesclado = None


class Cadastros:
    def __init__(
        self,
        *,
        leitor: Leitor,
        espelho: Any,
        relogio: Callable[[], float] = time.monotonic,
        ao_abrir: Callable[[Any], None] | None = None,
    ) -> None:
        self._leitor = leitor
        self._espelho = espelho
        self._relogio = relogio
        #: Chamado com o SDK antes da primeira leitura (o serviço lê a série).
        self._ao_abrir = ao_abrir or (lambda sdk: None)
        self._lock = threading.Lock()
        self._sessoes: dict[str, Sessao] = {}

    # ── sessões ───────────────────────────────────────
    def iniciar(self, *, desafio_id: str, desafio: dict) -> dict:
        finalidade, status = desafio.get("finalidade"), desafio.get("status")
        if finalidade != "cadastro":
            raise passo_invalido("Este pedido não é de cadastro de digital.")
        if status != "pendente":
            raise ErroBiometria(
                "servidor_recusou",
                "Este pedido de cadastro já foi usado ou expirou. Comece de novo.",
                status=409,
                codigo=f"biometria_desafio_{status or 'invalido'}",
            )
        try:
            digital_id = uuid_canonico(desafio.get("digital_id"))
        except ValueError:
            digital_id = ""
        device_id = desafio.get("device_id")
        if not digital_id or not id_da_loja_valido(device_id) or device_id != device_id_de(digital_id):
            raise ErroBiometria(
                "servidor_recusou",
                "O pedido de cadastro veio sem a identificação da digital. Comece de novo.",
                status=409,
                codigo="biometria_desafio_invalido",
            )
        alvo = desafio.get("alvo") if isinstance(desafio.get("alvo"), dict) else {}
        ttl = _ttl(desafio.get("expira_em"))
        sessao = Sessao(
            id=secrets.token_urlsafe(16),
            desafio_id=uuid_canonico(desafio_id),
            digital_id=digital_id,
            device_id=device_id,
            nome=str(alvo.get("nome") or ""),
            expira_em=self._relogio() + ttl,
        )
        with self._lock:
            self._purgar()
            while len(self._sessoes) >= MAX_SESSOES:
                mais_velha = min(self._sessoes.values(), key=lambda s: s.expira_em)
                self._tirar(mais_velha)
            self._sessoes[sessao.id] = sessao
        return {"sessao_id": sessao.id, "passos": PASSOS, "expira_em_s": int(ttl)}

    def descartar(self, sessao_id: str) -> dict:
        with self._lock:
            sessao = self._sessoes.get(sessao_id)
            if sessao is not None:
                self._tirar(sessao)
        return {"ok": True}

    def encerrar_todas(self) -> None:
        with self._lock:
            for sessao in list(self._sessoes.values()):
                self._tirar(sessao)

    def _purgar(self) -> None:
        agora = self._relogio()
        for sessao in [s for s in self._sessoes.values() if s.expira_em <= agora]:
            self._tirar(sessao)

    def _tirar(self, sessao: Sessao) -> None:
        """Tira a sessão da lista; limpa agora ou quando o passo em curso acabar."""
        self._sessoes.pop(sessao.id, None)
        sessao.descartada = True
        if sessao.lock.acquire(blocking=False):
            try:
                self._encerrar(sessao)
            finally:
                sessao.lock.release()
        else:
            self._leitor.cancelar()  # passo em curso: solta o dedo; o passo limpa

    def _encerrar(self, sessao: Sessao) -> None:
        if sessao.gravada_no_leitor and sessao.resposta is None:
            # Gravou no leitor e o servidor não confirmou: sem dono conhecido,
            # não pode ficar identificável (o espelho devolve se o servidor
            # tiver gravado e só a resposta se perdeu).
            self._apagar_em_segundo_plano(sessao.device_id)
            sessao.gravada_no_leitor = False
        sessao.limpar_templates()

    def _apagar_em_segundo_plano(self, device_id: int) -> None:
        def tarefa(sdk: Any, ctx: Any) -> None:
            try:
                sdk.apagar(device_id)
            except ErroLeitor as e:
                if e.codigo != ID_INEXISTENTE:
                    raise

        try:
            self._leitor.enviar(tarefa, usuario=False, nome="cadastro-desfazer")
        except ErroBiometria:
            log.warning("Não foi possível desfazer a gravação no leitor", exc_info=True)

    @contextmanager
    def _passo(self, sessao_id: str) -> Iterator[Sessao]:
        with self._lock:
            self._purgar()
            sessao = self._sessoes.get(sessao_id)
        if sessao is None:
            raise ErroBiometria(
                "sessao_inexistente",
                "Este cadastro expirou ou foi descartado. Comece de novo.",
                status=404,
            )
        if not sessao.lock.acquire(blocking=False):
            raise LeitorOcupado(pode_cancelar=True, operacao="cadastro")
        try:
            yield sessao
        finally:
            try:
                if sessao.descartada:
                    self._encerrar(sessao)
            finally:
                sessao.lock.release()

    # ── passos ────────────────────────────────────────
    def verificar(
        self,
        sessao_id: str,
        *,
        cliente: Any,
        leitor_info: Callable[[], dict],
        cancelamento: Cancelamento | None = None,
    ) -> dict:
        with self._passo(sessao_id) as s:
            if s.resposta is not None:
                raise passo_invalido("Este cadastro já foi concluído.")

            def tarefa(sdk: Any, ctx: Any) -> Any:
                self._ao_abrir(sdk)
                return ctx.capturar(sdk.identificar)

            try:
                ident = self._leitor.executar(
                    tarefa,
                    usuario=True,
                    nome="cadastro",
                    captura=True,
                    cancelamento=cancelamento,
                )
            except ErroLeitor as e:
                if e.codigo in CAPTURA_SEM_DEDO:
                    return {"status": e.ident, "mensagem": e.mensagem}
                if e.codigo != NAO_IDENTIFICADO:
                    raise
                ident = None
            try:
                resp = cliente.resultado(
                    s.desafio_id,
                    device_id=ident.device_id if ident else None,
                    erro=None if ident else "nao_identificado",
                    score=ident.score if ident else 0,
                    qualidade=ident.qualidade if ident else 0,
                    leitor=leitor_info(),
                )
            except RemoteError as e:
                raise erro_remoto(e) from None
            status = resp.get("status")
            if status == "recusado":
                s.verificado = False
                return {
                    "status": "duplicada",
                    "nome": resp.get("nome") or None,
                    "mensagem": resp.get("motivo") or None,
                }
            if status == "pendente" and resp.get("verificado") is True:
                s.verificado = True
                s.mesma_pessoa_id = ident.device_id if ident else None
                return {"status": "livre"}
            raise ErroBiometria(
                "servidor_recusou",
                str(resp.get("motivo") or "O EldenSys não confirmou a verificação. Comece de novo."),
                status=409,
                codigo=f"biometria_desafio_{status or 'invalido'}",
            )

    def capturar(
        self,
        sessao_id: str,
        *,
        mostrar_previa: bool,
        cancelamento: Cancelamento | None = None,
    ) -> dict:
        with self._passo(sessao_id) as s:
            if s.resposta is not None:
                raise passo_invalido("Este cadastro já foi concluído.")
            if not s.verificado:
                raise passo_invalido("Confira o dedo no leitor (primeiro passo) antes das leituras.")
            if s.mesclado is not None or len(s.templates) >= CAPTURAS:
                raise passo_invalido("As três leituras já foram feitas. Conclua o cadastro.")
            feitas = len(s.templates)
            try:
                cap: Captura = self._leitor.executar(
                    lambda sdk, ctx: ctx.capturar(sdk.capturar),
                    usuario=True,
                    nome="cadastro",
                    captura=True,
                    cancelamento=cancelamento,
                )
            except ErroLeitor as e:
                if e.codigo not in _LEITURA_FALHOU:
                    raise
                return {
                    "aceita": False,
                    "passo": feitas,
                    "de": CAPTURAS,
                    "qualidade": 0,
                    "previa_png": None,
                    "mensagem": e.mensagem,
                }
            previa = previa_png(cap.imagem, cap.largura, cap.altura) if mostrar_previa else None
            if cap.qualidade < QUALIDADE_MINIMA:
                return {
                    "aceita": False,
                    "passo": feitas,
                    "de": CAPTURAS,
                    "qualidade": cap.qualidade,
                    "previa_png": previa,
                    "mensagem": (
                        f"Leitura fraca (qualidade {cap.qualidade}; o mínimo é "
                        f"{QUALIDADE_MINIMA}). Limpe o dedo e o leitor e tente de novo."
                    ),
                }
            s.templates.append(cap.template)
            s.qualidades.append(cap.qualidade)
            return {
                "aceita": True,
                "passo": feitas + 1,
                "de": CAPTURAS,
                "qualidade": cap.qualidade,
                "previa_png": previa,
                "mensagem": None,
            }

    def concluir(self, sessao_id: str, *, cliente: Any, leitor_info: Callable[[], dict]) -> dict:
        with self._passo(sessao_id) as s:
            if s.resposta is not None:
                return dict(s.resposta)
            if not s.verificado:
                raise passo_invalido("Confira o dedo no leitor (primeiro passo) antes de concluir.")
            if s.mesclado is None and len(s.templates) < CAPTURAS:
                raise passo_invalido("Faltam leituras: são três do mesmo dedo.")
            if s.mesclado is None or not s.gravada_no_leitor:
                s.gravada_no_leitor = self._leitor.executar(
                    lambda sdk, ctx: self._mesclar_e_gravar(sdk, s), usuario=True, nome="cadastro"
                )
            qualidade = round(sum(s.qualidades) / len(s.qualidades)) if s.qualidades else 0
            try:
                resp = cliente.enviar_digital(
                    s.desafio_id, template=s.mesclado, qualidade=qualidade, leitor=leitor_info()
                )
            except RemoteError as e:
                if isinstance(e, ServidorRecusou) or not _transitorio(e):
                    self._desistir(s)
                    raise erro_remoto(e) from None
                # Rede: guarda tudo; "concluir" de novo só repete o envio.
                raise erro_remoto(e, pode_reenviar=True) from None
            if resp.get("status") != "aprovado":
                self._desistir(s)
                raise ErroBiometria(
                    "servidor_recusou",
                    str(resp.get("motivo") or "O EldenSys não aprovou o cadastro."),
                    status=409,
                    codigo=f"biometria_desafio_{resp.get('status') or 'invalido'}",
                )
            s.resposta = {
                "status": "aprovado",
                "digital_id": str(resp.get("digital_id") or s.digital_id),
                "nome": str(resp.get("nome") or s.nome),
                "gravada_no_leitor": s.gravada_no_leitor,
            }
            s.limpar_templates()
            s.expira_em = max(s.expira_em, self._relogio() + GUARDA_CONCLUIDA_S)
            log.info("Digital cadastrada (gravada no leitor: %s)", s.gravada_no_leitor)
            return dict(s.resposta)

    def _mesclar_e_gravar(self, sdk: Any, s: Sessao) -> bool:
        """Roda NA thread do leitor: merge (se preciso) e gravação na memória."""
        if s.mesclado is None:
            try:
                s.mesclado = sdk.mesclar(*s.templates)
            except ErroLeitor as e:
                if e.codigo == MERGE:
                    # Leituras de dedos diferentes: recomeça as três.
                    s.templates.clear()
                    s.qualidades.clear()
                raise
            s.templates.clear()
        apagou_antiga = liberou = False
        while True:
            try:
                sdk.salvar(s.device_id, s.mesclado)
                return True
            except ErroLeitor as e:
                antiga = s.mesma_pessoa_id
                if e.codigo == JA_CADASTRADA and antiga and antiga != s.device_id and not apagou_antiga:
                    # Recadastro do mesmo dedo: a digital antiga vira lápide no
                    # servidor ao concluir; aqui ela sai antes da nova entrar.
                    apagou_antiga = True
                    try:
                        sdk.apagar(antiga)
                    except ErroLeitor as e2:
                        if e2.codigo != ID_INEXISTENTE:
                            raise
                    continue
                if e.codigo == MEMORIA_CHEIA:
                    if not liberou and self._espelho.liberar_espaco(sdk, {s.device_id}):
                        liberou = True
                        continue
                    # Segue para o servidor: a digital vale e entra na memória
                    # quando houver espaço (o espelho tenta de novo).
                    log.warning("Cadastro: memória do leitor cheia; digital só no servidor")
                    return False
                raise

    def _desistir(self, s: Sessao) -> None:
        """Servidor disse não: o leitor não pode ficar com a digital."""
        if s.gravada_no_leitor:
            self._apagar_em_segundo_plano(s.device_id)
            s.gravada_no_leitor = False
        s.limpar_templates()
        s.qualidades.clear()
        s.verificado = False
        with self._lock:
            self._sessoes.pop(s.id, None)


def _transitorio(e: RemoteError) -> bool:
    from ..remote_client import RateLimited, TransportError

    return isinstance(e, (TransportError, RateLimited))
