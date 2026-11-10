"""Nomi di file e cartelle mostrati come testo, mai come markup.

Un QLabel lasciato al formato predefinito (AutoText) decide da solo se una
stringa è rich text, e basta qualcosa che somigli a un tag: il nome di un
file trovato da una scansione o di una cartella monitorata, scelto da chi ha
prodotto il file (un altro utente in una cartella condivisa, un archivio
scaricato, un supporto rimovibile), poteva ridurre a 1 pt o cancellare il
resto di un avviso di sicurezza («le sottocartelle oltre questo limite NON
sono protette», «Quarantena fallita»). Segnalato da un penetration test della
0.1.19.

Tre strumenti, per tre tipi di destinazione:

- plain_labels(): ogni etichetta lasciata a indovinare diventa testo
  semplice; chi vuole il rich text lo dichiara (setTextFormat(Qt.RichText)).
- PlainDialogs: le finestre di messaggio statiche (QMessageBox.warning() e
  simili) e i QInputDialog nascono fuori portata, ma come figli della pagina
  che li apre: l'evento ChildPolished arriva prima che vengano mostrati.
- plain_tooltip(): un tooltip non ha un formato da impostare, quindi il
  testo diventa rich text esplicito con il contenuto preso alla lettera.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QInputDialog, QLabel, QMessageBox, QWidget


def plain_labels(widget: QWidget) -> None:
    """Rende testo semplice ogni etichetta di widget (figli compresi) in AutoText."""
    for label in widget.findChildren(QLabel):
        if label.textFormat() == Qt.AutoText:
            label.setTextFormat(Qt.PlainText)


def plain_tooltip(text: str) -> str:
    """Un tooltip che mostra text così com'è, a capo compresi."""
    if not text:
        return ""
    return f'<p style="white-space:pre-wrap">{html.escape(text)}</p>'


class PlainDialogs:
    """Mixin per i widget che aprono finestre di messaggio con testi dal disco.

    Va prima della classe Qt nelle basi (class Pagina(PlainDialogs, QWidget)).
    """

    def childEvent(self, event) -> None:  # noqa: N802 (nome di Qt)
        super().childEvent(event)
        if event.type() == QEvent.ChildPolished:
            child = event.child()
            if isinstance(child, (QMessageBox, QInputDialog)):
                plain_labels(child)
