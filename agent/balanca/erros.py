"""Erros da balança de checkout no formato das rotas locais.

Toda rota ``/balanca`` devolve erro como ``{"erro", "mensagem", "detail"}``
(``detail`` repete a mensagem para quem só lê ``detail``, como no
``printAgent.ts``). Os códigos e os status HTTP são do contrato do lote de
24/11/2026 (seção 3) — a web decide o que mostrar por eles.
"""

from __future__ import annotations

BALANCA_DESLIGADA = "balanca_desligada"  # 409
SEM_PORTA = "sem_porta"  # 409
PORTA_EM_USO = "porta_em_uso"  # 503
PORTA_INEXISTENTE = "porta_inexistente"  # 503
PORTA_FALHOU = "porta_falhou"  # 503
BALANCA_SEM_RESPOSTA = "balanca_sem_resposta"  # 503
OCUPADA = "ocupada"  # 409
REQUISICAO_INVALIDA = "requisicao_invalida"  # 422
AGENTE_ENCERRANDO = "agente_encerrando"  # 503
#: Só sai quando o navegador já foi embora (``POST /balanca/detectar``
#: cancelado pela desconexão): ninguém lê esta resposta.
CANCELADO = "cancelado"  # 409


class ErroBalanca(Exception):
    """Erro pronto para virar a resposta de uma rota local ``/balanca``."""

    def __init__(self, erro: str, mensagem: str, status: int, detail: str | None = None) -> None:
        super().__init__(mensagem)
        self.erro = erro
        self.mensagem = mensagem
        self.status = status
        self.detail = detail or mensagem

    def corpo(self) -> dict:
        return {"erro": self.erro, "mensagem": self.mensagem, "detail": self.detail}


def requisicao_invalida(mensagem: str) -> ErroBalanca:
    return ErroBalanca(REQUISICAO_INVALIDA, mensagem, 422)


def balanca_desligada(mensagem: str | None = None) -> ErroBalanca:
    return ErroBalanca(
        BALANCA_DESLIGADA,
        mensagem
        or "A balança de checkout está desligada neste computador. Ligue em "
        "Configurações › Impressão › Balança de checkout (ou na janela do EldenSys Agent).",
        409,
    )


def sem_porta() -> ErroBalanca:
    return ErroBalanca(
        SEM_PORTA,
        "Nenhuma porta da balança foi escolhida neste computador. Escolha a porta em "
        "Configurações › Impressão › Balança de checkout.",
        409,
    )


def porta_em_uso(porta: str, detail: str | None = None) -> ErroBalanca:
    return ErroBalanca(
        PORTA_EM_USO,
        f"A porta {porta} está em uso por outro programa (ex.: o PDV offline EldenSys, "
        "Acesso Toledo). Feche o outro programa ou desligue a balança nele.",
        503,
        detail,
    )


def porta_inexistente(porta: str, detail: str | None = None) -> ErroBalanca:
    return ErroBalanca(
        PORTA_INEXISTENTE,
        f"A porta {porta} não existe neste computador. Confira o cabo da balança e "
        "atualize a lista de portas.",
        503,
        detail,
    )


def porta_falhou(porta: str, detail: str | None = None) -> ErroBalanca:
    return ErroBalanca(
        PORTA_FALHOU,
        f"Não foi possível usar a porta {porta} da balança. Confira o cabo e tente de novo.",
        503,
        detail,
    )


def porta_caiu(porta: str, detail: str | None = None) -> ErroBalanca:
    """A porta abriu, mas parou no meio da leitura (adaptador USB removido)."""
    return ErroBalanca(
        PORTA_FALHOU,
        f"A comunicação com a balança na porta {porta} caiu durante a leitura. Confira o "
        "cabo; a próxima leitura tenta abrir a porta de novo.",
        503,
        detail,
    )


def balanca_sem_resposta() -> ErroBalanca:
    """Vigia: a thread da balança passou muito do prazo (driver preso)."""
    return ErroBalanca(
        BALANCA_SEM_RESPOSTA,
        "A leitura da balança travou neste computador. Desconecte e reconecte o cabo da "
        "balança; se continuar, feche e abra o EldenSys Agent.",
        503,
    )


def ocupada() -> ErroBalanca:
    return ErroBalanca(
        OCUPADA,
        "A balança está ocupada com outras leituras. Tente de novo em instantes.",
        409,
    )


def agente_encerrando() -> ErroBalanca:
    return ErroBalanca(AGENTE_ENCERRANDO, "O EldenSys Agent está encerrando.", 503)


def cancelado() -> ErroBalanca:
    return ErroBalanca(CANCELADO, "Detecção da balança cancelada.", 409)
