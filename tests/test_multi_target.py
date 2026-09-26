"""
Selezione multipla da Dolphin (%F): codifica IPC multi-percorso,
lettura completa lato server, scansione unica di più destinazioni.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from klamav_py.clamd_client import ClamdEndpoint, ScanResult  # noqa: E402
from klamav_py.gui import main_window as mw  # noqa: E402
from klamav_py.gui.single_instance import (  # noqa: E402
    IPC_MAX_PAYLOAD_BYTES, encode_targets, notify_running_instance,
)

app = QApplication.instance() or QApplication([])

RADICE = Path(__file__).resolve().parent.parent


# --- codifica / decodifica ----------------------------------------------------------

def test_andata_e_ritorno():
    paths = ["/home/u/a.pdf", "/home/u/cartella con spazi", "/home/u/è.txt"]
    payload, esclusi = encode_targets(paths)
    assert esclusi == 0
    assert mw._decode_ipc_targets(payload) == paths


def test_formato_precedente_un_solo_percorso():
    assert mw._decode_ipc_targets(b"/home/u/file.txt") == ["/home/u/file.txt"]


def test_payload_sempre_sotto_il_limite():
    paths = [f"/home/u/cartella/file-{i:05d}.bin" for i in range(20_000)]
    payload, esclusi = encode_targets(paths)
    assert len(payload) < IPC_MAX_PAYLOAD_BYTES
    assert esclusi > 0
    decodificati = mw._decode_ipc_targets(payload)
    assert decodificati == paths[: len(decodificati)]
    assert len(decodificati) + esclusi == len(paths)


def test_troncato_scarta_l_ultimo_percorso():
    # "/b/lun" è l'inizio tagliato di "/b/lungo": non va scansionato.
    assert mw._decode_ipc_targets(b"/a\0/b/lun", truncated=True) == ["/a"]


def test_parti_malformate_scartate_singolarmente():
    assert mw._decode_ipc_targets(b"/a\0\xff\xfe\0\0/c") == ["/a", "/c"]


def test_servicemenu_usa_la_selezione_multipla():
    src = (RADICE / "klamav_py/gui/main_window.py").read_text(encoding="utf-8")
    assert "--scan-target %F" in src and "--scan-target %f" not in src


# --- IPC reale: molti percorsi in più letture -----------------------------------

def test_lettura_completa_di_un_payload_grande(tmp_path):
    """
    Client in un PROCESSO separato, come nell'uso reale (Dolphin lancia
    una seconda istanza). Non un thread: in PySide waitForReadyRead non
    rilascia il GIL, e un client Python nello stesso processo resterebbe
    fermo fino alla scadenza dell'attesa.
    """
    sock_path = str(tmp_path / "ipc")
    server = QLocalServer()
    assert server.listen(sock_path)

    n = 5000   # ~150 KiB: più letture lato server
    codice = (
        "import sys\n"
        "from klamav_py.gui.single_instance import encode_targets, notify_running_instance\n"
        f"paths = [f'/home/u/foto/IMG_{{i:05d}}.jpg' for i in range({n})]\n"
        "sys.exit(0 if notify_running_instance(sys.argv[1], encode_targets(paths)[0]) else 1)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(RADICE), QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen([sys.executable, "-c", codice, sock_path], env=env)

    client = None
    for _ in range(1000):
        server.waitForNewConnection(10)   # in PySide restituisce una tupla
        if server.hasPendingConnections():
            client = server.nextPendingConnection()
            break
    assert client is not None, "il client non si è connesso"
    raw, troncato = mw.MainWindow._read_ipc_payload(client)
    assert proc.wait(timeout=10) == 0
    server.close()

    assert not troncato
    paths = [f"/home/u/foto/IMG_{i:05d}.jpg" for i in range(n)]
    assert mw._decode_ipc_targets(raw) == paths


# --- ScanPage e ScanWorker ------------------------------------------------------

class _ScanPage:
    start_external_scan = mw.ScanPage.start_external_scan
    _on_path_edited = mw.ScanPage._on_path_edited

    def __init__(self):
        self.path_edit = QLineEdit()
        self._external_targets = None

    def _start_scan(self):
        pass


def test_selezione_multipla_in_scan_page(tmp_path):
    files = []
    for n in ("a", "b", "c"):
        f = tmp_path / n
        f.write_text("x")
        files.append(f)
    p = _ScanPage()
    p.start_external_scan(files + [tmp_path / "sparito"])
    assert p._external_targets == files
    assert p.path_edit.text() == f"{files[0]} (+2 altri)"
    p._on_path_edited("modificato a mano")
    assert p._external_targets is None


def test_un_solo_elemento_resta_percorso_semplice(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")
    p = _ScanPage()
    p.start_external_scan([f])
    assert p._external_targets is None and p.path_edit.text() == str(f)


def test_worker_scansiona_tutte_le_destinazioni(tmp_path):
    from klamav_py.gui.scan_worker import ScanWorker

    visti = []

    class Client:
        def __init__(self, **kw):
            self.skipped = {}

        def scan_stream(self, target, **kw):
            visti.append(Path(target).name)
            yield ScanResult(str(target), "OK")
            yield ScanResult(str(target) + "/x", "ERROR", "Permission denied")

    w = ScanWorker(endpoint=ClamdEndpoint(), target=[tmp_path / "a", tmp_path / "b"], client_factory=Client)
    fine = []
    w.finished_scan.connect(lambda *a: fine.append(a))
    w.run()
    assert visti == ["a", "b"]
    assert fine == [(4, 0, 2, 0)]


def test_parte_oltre_path_max_scartata():
    lunghissimo = b"/" + b"a" * 5000
    assert mw._decode_ipc_targets(b"/ok\0" + lunghissimo + b"\0/ok2") == ["/ok", "/ok2"]
