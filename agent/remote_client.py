"""Cliente HTTP do canal remoto: pareamento e as duas chamadas ao EldenSys.

Até a v0.4 o agente nunca falava com o servidor — só recebia do navegador
da própria máquina. O canal remoto inverte o sentido: o agente pergunta ao
EldenSys se há trabalho (`poll`) e avisa o resultado (`ack`). É sempre
conexão de SAÍDA, então funciona atrás de NAT/firewall sem abrir porta.

Contrato (o lado servidor mora no app `agents` do EldenSys):

    POST {url}/agent/poll/            heartbeat + busca trabalhos
    POST {url}/agent/jobs/<id>/ack/   resultado de um trabalho

    Authorization: Bearer <token>     segredo do pareamento
    X-Tenant: <schema>                loja
    X-Agent-Instance: <uuid>          esta instalação

Usa só a biblioteca padrão (`urllib`) de propósito: nenhuma dependência nova
no instalador, e no Windows o `ssl` já confia na loja de certificados do
sistema.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import __version__

PAIRING_PREFIX = "ELD1-"

#: Resposta do poll carrega arquivos em base64 (carga de balança: KBs;
#: PDF de etiqueta: até 5 MB → ~7 MB em base64). Teto contra resposta absurda.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


# ── Pareamento ───────────────────────────────────────────────────────────
class PairingCodeError(ValueError):
    """Código de pareamento malformado (mensagem pronta pra janela)."""


@dataclass(frozen=True)
class Pairing:
    url: str  # base da API, sem barra final (ex.: https://api.../api)
    tenant: str
    token: str


def normalize_server_url(url: object) -> str:
    """Aceita só https (http apenas em localhost, pra desenvolvimento).

    O token viaja em todo poll: mandá-lo por http seria entregá-lo a
    qualquer um na rede da loja.
    """
    if not isinstance(url, str) or not url.strip():
        raise PairingCodeError("Código de pareamento sem endereço do servidor.")
    cleaned = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise PairingCodeError("Endereço do servidor inválido no código de pareamento.")
    if parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL_HOSTS):
        return cleaned
    raise PairingCodeError("O endereço do servidor precisa começar com https://")


def parse_pairing_code(code: object) -> Pairing:
    """Decodifica `ELD1-<base64url de {"u","t","k"}>` gerado pelo EldenSys."""
    if not isinstance(code, str):
        raise PairingCodeError("Código de pareamento inválido.")
    compact = "".join(code.split())  # colar de e-mail/WhatsApp traz quebras
    if not compact.upper().startswith(PAIRING_PREFIX):
        raise PairingCodeError(
            "Código de pareamento inválido: ele começa com ELD1- e é gerado no "
            "EldenSys, em Configurações → Impressão → Agentes da loja."
        )
    body = compact[len(PAIRING_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise PairingCodeError("Código de pareamento incompleto ou corrompido.") from e
    if not isinstance(data, dict):
        raise PairingCodeError("Código de pareamento inválido.")

    url = normalize_server_url(data.get("u"))
    tenant, token = data.get("t"), data.get("k")
    # Vão em cabeçalho HTTP: recusar qualquer coisa fora do alfabeto esperado
    # (inclusive quebra de linha) aqui, com mensagem clara.
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise PairingCodeError("Código de pareamento inválido (loja).")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise PairingCodeError("Código de pareamento inválido (chave).")
    return Pairing(url=url, tenant=tenant, token=token)


# ── Erros de chamada ─────────────────────────────────────────────────────
class RemoteError(Exception):
    """Base: qualquer falha ao falar com o EldenSys."""


class PairingRejected(RemoteError):
    """401/404: token revogado, usado em outro PC, ou loja inexistente.

    Não adianta insistir — o usuário precisa gerar um código novo.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class StoreBlocked(RemoteError):
    """403: loja bloqueada/inativa. Pode voltar sozinha; tentar de vez em quando."""


class RateLimited(RemoteError):
    """429: o servidor pediu pra esperar `retry_after` segundos."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransportError(RemoteError):
    """Rede fora, timeout, 5xx, resposta ilegível — tentar de novo com backoff."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Não segue redirecionamento.

    O urllib reenviaria o `Authorization` para o destino do redirect, que
    pode ser outro host. Um 3xx aqui vira erro e pronto.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _error_payload(err: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        data = json.loads(err.read(64 * 1024).decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


class RemoteClient:
    """Uma instância por ciclo de poll (a config pode mudar entre ciclos)."""

    def __init__(
        self,
        url: str,
        tenant: str,
        token: str,
        instance_id: str,
        opener: Any = None,
        timeout: float = 20.0,
    ) -> None:
        self.url = normalize_server_url(url)
        self.tenant = tenant
        self.token = token
        self.instance_id = instance_id
        self._opener = opener or _default_opener()
        self._timeout = timeout

    # ── chamadas ──────────────────────────────────────
    def poll(self, agent_version: str, hostname: str, capabilities: dict) -> dict:
        return self._post(
            "/agent/poll/",
            {
                "agent_version": agent_version,
                "hostname": hostname,
                "capabilities": capabilities,
            },
        )

    def ack(self, job_id: str, status: str, detail: str = "", result: dict | None = None) -> dict:
        safe_id = urllib.parse.quote(str(job_id), safe="")
        return self._post(
            f"/agent/jobs/{safe_id}/ack/",
            {"status": status, "detail": detail, "result": result or {}},
        )

    # ── transporte ────────────────────────────────────
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Tenant": self.tenant,
                "X-Agent-Instance": self.instance_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"EldenSysAgent/{__version__}",
            },
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise self._from_http_error(e) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            # URLError: DNS/conexão/TLS. OSError: timeout e reset no meio da
            # leitura. ValueError: cabeçalho inválido montado a partir da config.
            raise TransportError(f"Sem conexão com o EldenSys: {e}") from e

        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError("Resposta do EldenSys grande demais.")
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError) as e:
            raise TransportError("Resposta do EldenSys não é JSON.") from e
        if not isinstance(data, dict):
            raise TransportError("Resposta inesperada do EldenSys.")
        return data

    @staticmethod
    def _from_http_error(e: urllib.error.HTTPError) -> RemoteError:
        payload = _error_payload(e)
        detail = str(payload.get("detail") or "").strip()
        code = str(payload.get("code") or "").strip()
        if e.code in (401, 404):
            # 404 = loja (X-Tenant) inexistente: o pareamento não serve mais.
            return PairingRejected(
                detail or "Pareamento recusado pelo EldenSys. Gere um novo código.", code
            )
        if e.code == 403:
            return StoreBlocked(detail or "Loja bloqueada ou inativa no EldenSys.")
        if e.code == 429:
            retry_after: float | None = None
            try:
                retry_after = float(e.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            return RateLimited("EldenSys pediu para aguardar.", retry_after)
        return TransportError(f"EldenSys respondeu {e.code}. {detail}".strip())
