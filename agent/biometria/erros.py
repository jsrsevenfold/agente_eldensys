"""Erros da biometria: códigos do SDK traduzidos e o formato das rotas locais.

Os códigos (``int32_t``) vêm de ``cidbioconf.h`` (SDK 1.7.0): 0 = ok, > 0 =
aviso, < 0 = erro. O identificador curto e a mensagem são do contrato da
biometria (seção 0) — iguais na web, no agente e no PDV. Os três códigos que
o contrato não lista (-12 firmware inválido, -15 número de erro inválido,
-17 firmware anterior) saem como ``desconhecido``, com o código no detalhe.

Toda rota local devolve erro como ``{"erro", "mensagem", "detail"}`` (+ campos
extras como ``pode_cancelar``). ``ErroBiometria.corpo()`` monta isso; a rota
só escolhe o status HTTP que vem junto.
"""

from __future__ import annotations

#: código → (identificador curto, mensagem em português). Contrato, seção 0.
TABELA: dict[int, tuple[str, str]] = {
    3: ("sobrescrito", "Digital substituída na memória do leitor"),
    2: ("memoria_vazia", "Leitor sem digitais gravadas"),
    1: ("ja_iniciado", "Leitor já estava aberto"),
    0: ("ok", "Operação concluída"),
    -1: ("desconhecido", "Erro desconhecido no leitor"),
    # -2 também é o que o Init devolve com o leitor aberto por OUTRO programa
    # (fase 0, 24/11/2026): não dá para distinguir de cabo solto.
    -2: (
        "sem_leitor",
        "Leitor biométrico não encontrado ou em uso por outro programa. "
        "Confira o cabo USB e tente de novo.",
    ),
    -3: ("argumento", "Erro interno ao chamar o leitor"),
    -4: ("argumento", "Erro interno ao chamar o leitor"),
    -5: ("falha_captura", "Não foi possível ler o dedo. Tente de novo."),
    -6: ("tempo_esgotado", "Nenhum dedo colocado a tempo"),
    -7: ("usb", "Falha de comunicação USB com o leitor"),
    -8: ("io_host", "Falha de comunicação com o leitor neste computador"),
    -9: ("ja_cadastrada", "Esta digital já está gravada no leitor"),
    -10: ("merge", "As três leituras não parecem do mesmo dedo. Recomece."),
    -11: ("match", "Falha ao comparar digitais"),
    -13: ("memoria_cheia", "Memória do leitor cheia"),
    -14: ("id_inexistente", "Digital não encontrada no leitor"),
    -16: ("nao_pro", "Este leitor não é o iDBio Pro (sem identificação)"),
    -18: ("nao_identificado", "Digital não reconhecida"),
    -19: ("ocupado", "Leitor ocupado"),
    -20: ("cancelado", "Leitura cancelada"),
    -21: ("sem_dedo", "Nenhum dedo no leitor"),
    -22: ("template_invalido", "Digital em formato inválido"),
}

SEM_LEITOR = -2
TEMPO_ESGOTADO = -6
USB = -7
IO_HOST = -8
JA_CADASTRADA = -9
MERGE = -10
MEMORIA_CHEIA = -13
ID_INEXISTENTE = -14
NAO_PRO = -16
NAO_IDENTIFICADO = -18
OCUPADO = -19
CANCELADO = -20
SEM_DEDO = -21
TEMPLATE_INVALIDO = -22

#: Fim de captura sem leitura útil: a pessoa pode simplesmente tentar de novo.
CAPTURA_SEM_DEDO = frozenset({TEMPO_ESGOTADO, CANCELADO, SEM_DEDO})


def traduzir(codigo: int) -> tuple[str, str]:
    """(identificador curto, mensagem) de um código do SDK."""
    return TABELA.get(codigo, TABELA[-1])


class ErroBiometria(Exception):
    """Erro pronto para virar a resposta de uma rota local ``/biometria``."""

    def __init__(
        self,
        erro: str,
        mensagem: str,
        *,
        status: int = 500,
        detail: str | None = None,
        **extra: object,
    ) -> None:
        super().__init__(mensagem)
        self.erro = erro
        self.mensagem = mensagem
        self.status = status
        self.detail = detail or mensagem
        self.extra = extra

    def corpo(self) -> dict:
        return {"erro": self.erro, "mensagem": self.mensagem, "detail": self.detail, **self.extra}


def _http_do_codigo(codigo: int, ident: str) -> tuple[int, str]:
    """Status HTTP e ``erro`` da rota local para um código do SDK.

    Os nomes com código próprio na seção 3 do contrato saem com ele; os
    demais saem 422 com o identificador curto da tabela (``merge``,
    ``tempo_esgotado``…), que a web já conhece pela seção 0.
    """
    if codigo in (SEM_LEITOR, USB, IO_HOST):
        return 503, "leitor_desconectado"
    if codigo == OCUPADO:
        return 503, "leitor_em_uso"
    if codigo == NAO_PRO:
        return 422, "leitor_nao_pro"
    if codigo == MEMORIA_CHEIA:
        return 409, "memoria_cheia"
    if ident in ("desconhecido", "argumento"):
        return 500, "interno"
    return 422, ident


class ErroLeitor(ErroBiometria):
    """Código negativo devolvido pela DLL (ou recusado antes de chamá-la)."""

    def __init__(self, codigo: int, operacao: str = "") -> None:
        ident, mensagem = traduzir(codigo)
        status, erro = _http_do_codigo(codigo, ident)
        onde = f", {operacao}" if operacao else ""
        super().__init__(erro, mensagem, status=status, detail=f"{mensagem} (código {codigo}{onde})")
        self.codigo = codigo
        self.ident = ident
        self.operacao = operacao


class SdkIndisponivel(ErroBiometria):
    """DLL ausente, de arquitetura errada ou sem as funções esperadas."""

    def __init__(self, motivo: str) -> None:
        super().__init__("sdk_ausente", motivo, status=503)


class LeitorOcupado(ErroBiometria):
    """Outra operação do usuário já está usando o leitor neste agente."""

    def __init__(self, *, pode_cancelar: bool, operacao: str = "") -> None:
        super().__init__(
            "ocupado",
            "O leitor está ocupado com outra leitura. Termine ou cancele a anterior.",
            status=409,
            pode_cancelar=pode_cancelar,
            operacao=operacao,
        )


class LeitorTravado(ErroBiometria):
    def __init__(self) -> None:
        super().__init__(
            "leitor_travado",
            "O leitor biométrico parou de responder. Desconecte e reconecte o cabo USB; "
            "se continuar, feche e abra o EldenSys Agent.",
            status=503,
        )


class LeitorEmUso(ErroBiometria):
    """O leitor não abriu porque outro programa deste computador o segura."""

    def __init__(self, codigo: int | None = None) -> None:
        super().__init__(
            "leitor_em_uso",
            "O leitor biométrico está em uso por outro programa neste computador "
            "(por exemplo, o PDV). Aguarde ou feche o outro programa e tente de novo.",
            status=503,
            detail=None if codigo is None else f"Leitor não abriu (código {codigo}).",
        )


class BiometriaDesligada(ErroBiometria):
    def __init__(self, mensagem: str) -> None:
        super().__init__("biometria_desligada", mensagem, status=503)


def requisicao_invalida(mensagem: str) -> ErroBiometria:
    return ErroBiometria("requisicao_invalida", mensagem, status=422)


def passo_invalido(mensagem: str) -> ErroBiometria:
    return ErroBiometria("passo_invalido", mensagem, status=409)


def erro_remoto(e: Exception, *, pode_reenviar: bool = False) -> ErroBiometria:
    """Falha ao falar com o EldenSys → erro da rota local (contrato, seção 3).

    Recusa de negócio sai 409 ``servidor_recusou`` com o ``code`` do servidor
    em ``codigo`` e a frase dele em ``mensagem``; rede fora sai 503
    ``sem_conexao`` (``pode_reenviar`` diz se repetir o pedido basta).
    """
    from ..remote_client import (
        ModuloInativo,
        PairingCodeError,
        PairingRejected,
        ServidorRecusou,
        StoreBlocked,
    )

    if isinstance(e, PairingRejected):
        return ErroBiometria(
            "pareamento_recusado",
            "O EldenSys recusou o pareamento deste agente. Gere um novo código em "
            "Configurações → Impressão → Agentes da loja.",
            status=409,
        )
    if isinstance(e, ModuloInativo):
        return ErroBiometria(
            "modulo_inativo", "O módulo de biometria não está ligado nesta loja.", status=403
        )
    if isinstance(e, StoreBlocked):
        return ErroBiometria(
            "servidor_recusou", str(e) or "Loja bloqueada.", status=409, codigo="tenant_blocked"
        )
    if isinstance(e, ServidorRecusou):
        return ErroBiometria(
            "servidor_recusou",
            e.detail or "O EldenSys recusou o pedido.",
            status=409,
            codigo=e.code,
        )
    if isinstance(e, PairingCodeError):
        return ErroBiometria(
            "nao_pareado", "O EldenSys Agent não está pareado com uma loja.", status=409
        )
    return ErroBiometria(
        "sem_conexao",
        "Sem conexão com o EldenSys. Confira a internet e tente de novo.",
        status=503,
        pode_reenviar=pode_reenviar,
    )
