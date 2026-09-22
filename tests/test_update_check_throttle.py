"""
Limite al controllo aggiornamenti automatico (0.1.8).

Il controllo all'avvio interroga l'API GitHub non autenticata (60
richieste l'ora per IP). Con riavvii frequenti dell'applicazione ogni
avvio era una richiesta; ora ne parte al massimo una ogni sei ore. Il
pulsante in Impostazioni resta invece sempre immediato.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from klamav_py.gui.main_window import (
    _UPDATE_CHECK_MIN_INTERVAL_SECONDS as INTERVALLO,
    _controllo_aggiornamenti_dovuto,
)

MAIN_WINDOW = Path(__file__).resolve().parent.parent / "klamav_py" / "gui" / "main_window.py"
ADESSO = 1_800_000_000.0


@pytest.mark.parametrize(
    "ultimo, atteso, caso",
    [
        (0.0, True, "nessun controllo registrato"),
        (ADESSO - INTERVALLO - 1, True, "oltre l'intervallo"),
        (ADESSO - INTERVALLO, True, "esattamente sull'intervallo"),
        (ADESSO - 60, False, "un minuto fa"),
        (ADESSO - INTERVALLO + 1, False, "poco prima dell'intervallo"),
        (ADESSO + 86400, True, "timestamp nel futuro: orologio spostato o conf di un'altra macchina"),
    ],
)
def test_decisione(ultimo, atteso, caso):
    assert _controllo_aggiornamenti_dovuto(ultimo, ADESSO) is atteso, caso


def test_intervallo_di_sei_ore():
    assert INTERVALLO == 6 * 3600


def test_avvio_usa_il_punto_di_ingresso_con_limite():
    """
    Il timer di avvio deve chiamare _check_updates_automatico: chiamare
    direttamente _check_updates aggirerebbe il limite senza che nessun
    test se ne accorga.
    """
    tree = ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))
    attributi = [
        n.attr
        for chiamata in ast.walk(tree)
        if isinstance(chiamata, ast.Call)
        and isinstance(chiamata.func, ast.Attribute)
        and chiamata.func.attr == "singleShot"
        for n in ast.walk(chiamata)
        if isinstance(n, ast.Attribute) and n.attr.startswith("_check_updates")
    ]
    assert attributi == ["_check_updates_automatico"], attributi
