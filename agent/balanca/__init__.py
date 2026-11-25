"""Balança de checkout pela porta serial (v0.7).

O navegador não alcança a porta COM: o PDV online do EldenSys pede ao agente
uma leitura curta (``POST /balanca/leitura``) e repete enquanto pesa — o peso
"ao vivo" é isso, sem WebSocket (que escaparia da política de origem).

- ``protocolos``: interpretação PURA dos frames, porte fiel de
  ``eldensys_mobile/lib/core/services/balanca_protocolos.dart`` (PDV offline);
- ``porta``: pyserial 8N1 e a tradução dos erros do Windows;
- ``leitor``: a thread dona da porta (porte do I/O de ``scale_service.dart``);
- ``servico``: a fachada das rotas, da bandeja e do heartbeat;
- ``rotas``: ``/balanca/*`` (mesma política de origem da biometria).

Este ``__init__`` fica vazio de propósito: importar o pacote não abre porta
nem cria thread.
"""
