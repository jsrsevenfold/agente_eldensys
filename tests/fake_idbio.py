"""SDK falso do iDBio Pro: a mesma interface de ``agent.biometria.sdk.Sdk``.

Sem hardware e sem DLL. Imita o que importa para as regras do agente:

- memória com capacidade (``-13`` quando cheia, aviso 3 ao sobrescrever);
- ``Init``/``Terminate`` contados; operação sem ``Init`` = ``-2``;
- o "dedo no leitor" (``dedo``): ``identificar`` acha o id cujo template é
  igual; ``capturar`` devolve esse template;
- roteiros de captura/identificação/mescla (resultado ou código de erro);
- captura PRESA (``"bloquear"``) até ``cancelar_captura`` chegar de outra
  thread — como a captura real esperando o dedo.
"""

from __future__ import annotations

import base64
import threading

from agent.biometria.erros import ErroLeitor
from agent.biometria.sdk import (
    Captura,
    Identificacao,
    InfoLeitor,
    id_da_loja_valido,
    template_valido,
)

BLOQUEAR = "bloquear"


def tpl(n: int) -> str:
    """Template válido (base64, 128 caracteres) e diferente para cada ``n``."""
    return base64.b64encode(int(n).to_bytes(8, "big", signed=False) * 12).decode()


class SdkFalso:
    def __init__(
        self,
        *,
        capacidade: int = 3000,
        conectado: bool = True,
        pro: bool = True,
        modelo: str = "iDBio Pro",
        serie: str = "SN-0001",
        firmware: str = "1.2.0",
    ) -> None:
        self.memoria: dict[int, str] = {}
        self.capacidade = capacidade
        self.conectado = conectado
        self.pro = pro
        self.info_ = InfoLeitor(firmware=firmware, serie=serie, modelo=modelo)
        self.init_erro: int | None = None
        self.iniciado = False
        self.inits = 0
        self.terminates = 0
        self.chamadas: list[str] = []
        self.dedo: str | None = None
        self.capturas: list = []
        self.identificacoes: list = []
        self.mesclas: list = []
        self.rejeitar_duplicado = False
        self.salvar_erro: dict[int, int] = {}
        self.cancelamentos = 0
        self.espera_max = 10.0
        self.bloqueado = threading.Event()
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ── utilidades dos testes ──
    def desconectar(self) -> None:
        self.conectado = False

    def _abrir_ok(self, op: str) -> None:
        self.chamadas.append(op)
        if not self.conectado:
            raise ErroLeitor(-2, op)
        if not self.iniciado:
            raise ErroLeitor(-2, f"{op} sem Init")

    def _esperar_cancelamento(self) -> None:
        with self._lock:
            self._cancel.clear()
            self.bloqueado.set()
        cancelou = self._cancel.wait(self.espera_max)
        with self._lock:
            self.bloqueado.clear()
            self._cancel.clear()
        raise ErroLeitor(-20 if cancelou else -6, "captura")

    @staticmethod
    def _roteiro(item):
        if isinstance(item, int) and not isinstance(item, bool) and item < 0:
            raise ErroLeitor(item, "roteiro")
        if callable(item):
            return item()
        return item

    # ── interface do Sdk ──
    def iniciar(self) -> int:
        self.chamadas.append("Init")
        if not self.conectado:
            raise ErroLeitor(-2, "Init")
        if self.init_erro is not None:
            raise ErroLeitor(self.init_erro, "Init")
        self.inits += 1
        ja = self.iniciado
        self.iniciado = True
        return 1 if ja else 0

    def terminar(self) -> int:
        self.chamadas.append("Terminate")
        self.terminates += 1
        self.iniciado = False
        return 0

    def info(self) -> InfoLeitor:
        self._abrir_ok("GetDeviceInfo")
        return self.info_

    def ids(self) -> list[int]:
        self._abrir_ok("GetTemplateIDs")
        if not self.pro:
            raise ErroLeitor(-16, "GetTemplateIDs")
        return list(self.memoria)

    def capturar(self) -> Captura:
        self._abrir_ok("CaptureImageAndTemplate")
        if self.capturas:
            item = self.capturas.pop(0)
            if item == BLOQUEAR:
                self._esperar_cancelamento()
            resultado = self._roteiro(item)
            if isinstance(resultado, Captura):
                return resultado
        template = self.dedo or tpl(1)
        return Captura(template, bytes(range(256)) * 3, 24, 32, 80)

    def identificar(self) -> Identificacao:
        self._abrir_ok("CaptureAndIdentify")
        if not self.pro:
            raise ErroLeitor(-16, "CaptureAndIdentify")
        if self.identificacoes:
            item = self.identificacoes.pop(0)
            if item == BLOQUEAR:
                self._esperar_cancelamento()
            resultado = self._roteiro(item)
            if isinstance(resultado, Identificacao):
                return resultado
        if self.dedo is not None:
            for device_id, template in self.memoria.items():
                if template == self.dedo:
                    return Identificacao(device_id, 9000, 85)
        raise ErroLeitor(-18, "CaptureAndIdentify")

    def cancelar_captura(self) -> int:
        with self._lock:
            self.cancelamentos += 1
            if self.bloqueado.is_set():
                self._cancel.set()
        return 0

    def mesclar(self, t1: str, t2: str, t3: str) -> str:
        self._abrir_ok("MergeTemplates")
        for t in (t1, t2, t3):
            if not template_valido(t):
                raise ErroLeitor(-22, "MergeTemplates")
        if self.mesclas:
            resultado = self._roteiro(self.mesclas.pop(0))
            if isinstance(resultado, str):
                return resultado
        if not (t1 == t2 == t3):
            raise ErroLeitor(-10, "MergeTemplates")
        return t1

    def salvar(self, device_id: int, template: str) -> int:
        self._abrir_ok("SaveTemplate")
        if not id_da_loja_valido(device_id):
            raise ErroLeitor(-4, "SaveTemplate")
        if not template_valido(template):
            raise ErroLeitor(-22, "SaveTemplate")
        if not self.pro:
            raise ErroLeitor(-16, "SaveTemplate")
        if device_id in self.salvar_erro:
            raise ErroLeitor(self.salvar_erro[device_id], "SaveTemplate")
        if device_id in self.memoria:
            self.memoria[device_id] = template
            return 3
        if self.rejeitar_duplicado and template in self.memoria.values():
            raise ErroLeitor(-9, "SaveTemplate")
        if len(self.memoria) >= self.capacidade:
            raise ErroLeitor(-13, "SaveTemplate")
        self.memoria[device_id] = template
        return 0

    def apagar(self, device_id: int) -> int:
        self._abrir_ok("DeleteTemplate")
        if device_id not in self.memoria:
            raise ErroLeitor(-14, "DeleteTemplate")
        del self.memoria[device_id]
        return 0

    def mensagem(self, codigo: int) -> str | None:
        return f"codigo {codigo}"


class ServidorFalso:
    """Dublê do ``ClienteBiometria``: as rotas ``/api/agent/biometria/*`` da loja."""

    def __init__(self) -> None:
        self.templates: dict[int, str] = {}
        self.usuarios: list[int] = []
        self.clientes: list[int] = []
        self.removidos: list[int] = []
        self.versao = "0:2026-11-24T10:00:00+00:00"
        self.limite = 3000
        self.chamadas: list[tuple] = []
        self.falhas: dict[str, Exception] = {}
        self.desafios: dict[str, dict] = {}
        self.resultados: list[dict] = []
        self.enviadas: list[dict] = []
        self.logins: list[dict] = []
        #: veredito do POST resultado: dict fixo ou função(device_id) -> dict
        self.veredito = None
        self.resposta_digital: dict | None = None
        self.resposta_login: dict | None = None

    def vivas(self, usuarios: dict[int, str] | None = None, clientes: dict[int, str] | None = None):
        for d, t in (usuarios or {}).items():
            self.usuarios.append(d)
            self.templates[d] = t
        for d, t in (clientes or {}).items():
            self.clientes.append(d)
            self.templates[d] = t

    def _talvez_falhar(self, nome: str) -> None:
        erro = self.falhas.get(nome)
        if erro is not None:
            raise erro

    def digitais(self, versao):
        self.chamadas.append(("digitais", versao))
        self._talvez_falhar("digitais")
        if versao is not None and versao == self.versao:
            return {"versao": self.versao, "igual": True}
        return {
            "versao": self.versao,
            "limite": self.limite,
            "usuarios": list(self.usuarios),
            "clientes": list(self.clientes),
            "removidos": list(self.removidos),
        }

    def buscar(self, ids):
        assert len(ids) <= 200
        self.chamadas.append(("buscar", list(ids)))
        self._talvez_falhar("buscar")
        return [(i, self.templates[i]) for i in ids if i in self.templates]

    def desafio(self, desafio_id):
        self.chamadas.append(("desafio", desafio_id))
        self._talvez_falhar("desafio")
        return dict(self.desafios[desafio_id])

    def resultado(self, desafio_id, *, device_id, erro, score, qualidade, leitor):
        corpo = {
            "desafio": desafio_id,
            "device_id": device_id,
            "erro": erro,
            "score": score,
            "qualidade": qualidade,
            "leitor": leitor,
        }
        self.chamadas.append(("resultado", desafio_id))
        self.resultados.append(corpo)
        self._talvez_falhar("resultado")
        v = self.veredito
        if callable(v):
            return v(device_id)
        if v is not None:
            return dict(v)
        if device_id is None:
            return {"status": "recusado", "motivo": "Digital não reconhecida."}
        return {"status": "aprovado", "motivo": "", "nome": "Fulano"}

    def enviar_digital(self, desafio_id, *, template, qualidade, leitor):
        self.chamadas.append(("digital", desafio_id))
        self.enviadas.append({"desafio": desafio_id, "template": template, "qualidade": qualidade})
        self._talvez_falhar("digital")
        return dict(self.resposta_digital or {})

    def login(self, *, device_id, score, qualidade, challenge, leitor):
        self.chamadas.append(("login", device_id))
        self.logins.append({"device_id": device_id, "challenge": challenge, "leitor": leitor})
        self._talvez_falhar("login")
        return dict(self.resposta_login or {})
