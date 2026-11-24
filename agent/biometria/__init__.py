"""Leitor biométrico Control iD iDBio Pro (v0.6).

O navegador não alcança USB: a retaguarda do EldenSys pede ao agente que
capture e identifique a digital, e o agente informa o resultado DIRETO ao
servidor, com o token do pareamento. O template nunca passa pelo navegador.

Este ``__init__`` fica vazio de propósito: ``server.py`` importa a política
de origem (``origem``) sem carregar o SDK, as threads nem o espelho.
"""
