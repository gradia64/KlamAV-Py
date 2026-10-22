"""
Esiti del recupero delle quarantene interrotte, visibili all'utente.

Quarantine.recovered registrava le operazioni riprese (completate,
annullate, intenti non validi messi da parte), ma nessuno lo leggeva:
l'utente non sapeva che un'operazione interrotta era stata completata o
annullata. Ora la pagina Quarantena li mostra (con una notifica della tray
se la pagina non è visibile) e la CLI li scrive su stderr prima della
scansione, con lo stesso testo e senza cambiare il codice di uscita.

Ogni situazione si prepara due volte, in due cartelle: il recupero la
consuma, e la pagina e la CLI devono vederla entrambe.
"""

from __future__ import annotations

import errno
import os
import re
import time
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import klamav_py.cli as cli  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import ClamdEndpoint  # noqa: E402
from klamav_py.quarantine import Quarantine  # noqa: E402
from klamav_py.quarantine_location import decide  # noqa: E402

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


class Crash(BaseException):
    pass


# Gli effetti dei mock sollevano la CLASSE Crash, mai un'istanza condivisa:
# un'istanza tenuta dal mock conserva il proprio traceback, quindi i frame
# e, con f_back, il frame del test con le sue pagine. Il ciclo lo libera
# il garbage collector, anche nel thread di un worker del test successivo,
# e distruggere widget fuori dal thread della GUI fa crashare Qt
# (segfault visto nella CI con Python 3.12).


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# -- le tre situazioni ----------------------------------------------------

def _vittima(base: Path) -> Path:
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    f = home / "virus.exe"
    f.write_bytes(b"X5O!P%@AP")
    return f


def _interrotta(q: Quarantine, vittima: Path, **patches) -> None:
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)):
        ctx = [patch(target, **kw) for target, kw in patches.items()]
        for c in ctx:
            c.start()
        try:
            with pytest.raises(Crash):
                q.quarantine_file(vittima, "Eicar-Test-Signature")
        finally:
            for c in reversed(ctx):
                c.stop()


def completata(base: Path) -> tuple[Path, Path, str]:
    q = Quarantine(base / "q")
    vittima = _vittima(base)
    _interrotta(q, vittima, **{"klamav_py.quarantine.Quarantine._write_index":
                               {"side_effect": Crash}})
    return q.dir, vittima, "completata"


def annullata(base: Path) -> tuple[Path, Path, str]:
    q = Quarantine(base / "q")
    vittima = _vittima(base)
    rename_reale = os.rename

    def rename(src, dst):
        if Path(dst).parent == q.dir:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        if Path(src) == vittima:
            raise Crash()
        return rename_reale(src, dst)

    _interrotta(q, vittima, **{"klamav_py.quarantine.os.rename": {"side_effect": rename}})
    return q.dir, vittima, "annullata"


def non_valida(base: Path) -> tuple[Path, Path, str]:
    q = Quarantine(base / "q")
    vittima = _vittima(base)
    (q.dir / ".123_abc.intent").write_text('{"original_path": 42}')
    return q.dir, vittima, "non recuperata"


SITUAZIONI = [completata, annullata, non_valida]


# -- pagina e CLI ----------------------------------------------------------

def _page(qdir: Path, parent=None, show=False):
    page = mw.QuarantinePage(qdir, parent)
    if show:
        # Prima di processEvents: l'esito del worker arriva con un segnale
        # accodato, quindi la pagina è già visibile quando lo riceve.
        page.show()
        parent.show()
    return _page_wait(page)


def _page_text(page) -> str:
    label = getattr(page, "recovery_label", None)
    assert label is not None, "la pagina Quarantena non mostra gli esiti del recupero"
    return label.text() if not label.isHidden() else ""


def _cli_stderr(tmp_path: Path, qdir: Path, monkeypatch, capsys) -> str:
    client = MagicMock()
    client.ping.return_value = True
    client.scan_stream.return_value = iter(())
    client.skipped = {}
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: client)
    monkeypatch.setattr(
        cli, "decide_quarantine_dir",
        partial(decide, unit_hidden=(), mountinfo=MOUNTS_EXT4,
                volatile_roots=[tmp_path / "volatile"]),
    )
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    target = tmp_path / "dati"
    target.mkdir(exist_ok=True)
    capsys.readouterr()
    code = cli.main(["scan", str(target), "--quarantine", str(qdir)])
    assert code == 0, "il recupero non è un errore della scansione"
    return capsys.readouterr().err


def _neutro(text: str, base: Path) -> str:
    # Il nome dell'intento messo da parte ha data e un suffisso casuale.
    text = re.sub(r"\.corrupt-[0-9-]+-[0-9a-f]{6}", ".corrupt-<X>", text)
    return text.replace(str(base), "<BASE>")


@pytest.mark.parametrize("situazione", SITUAZIONI, ids=lambda f: f.__name__)
def test_stesso_testo_nella_pagina_e_su_stderr(app, tmp_path, monkeypatch, capsys, situazione):
    qdir_gui, vittima_gui, parola = situazione(tmp_path / "gui")
    qdir_cli, _, _ = situazione(tmp_path / "cli")

    page = _page(qdir_gui)
    testo_pagina = _page_text(page)
    testo_cli = _cli_stderr(tmp_path, qdir_cli, monkeypatch, capsys)

    assert testo_pagina, "nessun esito nella pagina"
    assert parola in testo_pagina
    if parola != "non recuperata":
        assert str(vittima_gui) in testo_pagina
    assert _neutro(testo_cli.strip(), tmp_path / "cli") == \
        _neutro(testo_pagina, tmp_path / "gui")


def test_nessun_esito_nessun_messaggio(app, tmp_path, monkeypatch, capsys):
    q = Quarantine(tmp_path / "gui" / "q")
    page = _page(q.dir)
    assert _page_text(page) == ""
    qcli = Quarantine(tmp_path / "cli" / "q")
    assert _cli_stderr(tmp_path, qcli.dir, monkeypatch, capsys) == ""


def test_esito_dell_aggiorna_mostrato(app, tmp_path):
    q = Quarantine(tmp_path / "q")
    page = _page(q.dir)
    assert _page_text(page) == ""
    # Operazione interrotta durante la sessione, poi «Aggiorna».
    vittima = _vittima(tmp_path)
    _interrotta(q, vittima, **{"klamav_py.quarantine.Quarantine._write_index":
                               {"side_effect": Crash}})
    page.refresh()
    page = _page_wait(page)
    testo = _page_text(page)
    assert str(vittima) in testo and "completata" in testo


def _page_wait(page):
    app = QApplication.instance()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        if page.worker is None and page.loading_label.isHidden():
            return page
        time.sleep(0.01)
    pytest.fail("caricamento della quarantena non terminato")


def _finestra_con_tray():
    window = QWidget()
    window.tray_icon = MagicMock()
    window.tray_icon.isVisible.return_value = True
    return window


def test_notifica_della_tray_se_la_pagina_non_e_visibile(app, tmp_path):
    qdir, vittima, _ = completata(tmp_path)
    window = _finestra_con_tray()
    page = _page(qdir, window)  # finestra mai mostrata: pagina non visibile
    calls = window.tray_icon.showMessage.call_args_list
    assert len(calls) == 1, "una notifica per caricamento"
    assert str(vittima) in calls[0].args[1] and "completata" in calls[0].args[1]

    page.refresh()  # nessun esito nuovo: nessuna notifica
    _page_wait(page)
    assert len(window.tray_icon.showMessage.call_args_list) == 1


def test_nessuna_notifica_se_la_pagina_e_visibile(app, tmp_path):
    qdir, _, _ = completata(tmp_path)
    window = _finestra_con_tray()
    try:
        page = _page(qdir, window, show=True)
        assert page.isVisible()
        assert "completata" in _page_text(page)
        window.tray_icon.showMessage.assert_not_called()
    finally:
        window.close()
