"""
Worker eseguito in un QThread separato per aggiornare il database di ClamAV.
Usa pkexec per ottenere i permessi di root tramite Polkit/KDE.
Gestisce automaticamente il blocco del file di log fermando e riavviando 
il demone di sistema clamav-freshclam.
"""

from __future__ import annotations

import importlib.resources
import subprocess
from PySide6.QtCore import QThread, Signal


def _freshclam_update_script() -> str:
    """
    Percorso dello script di aggiornamento spedito col pacchetto
    (klamav_py/gui/resources/freshclam-update.sh).

    Passare a `pkexec sh <percorso>` un FILE fisso, invece di costruire la
    sequenza di comandi come stringa Python passata a `sh -c`, elimina
    strutturalmente la possibilità di shell injection lato Python: non
    esiste più, da nessuna parte in questo modulo, una stringa di comandi
    costruita a runtime che un futuro refactor potrebbe interpolare con
    input esterno. Il contenuto dello script (vedi il file) resta
    comunque testo statico, con lo stesso vincolo.

    importlib.resources risolve il file sia da un'installazione .deb/pip
    (dove tipicamente non è scrivibile dall'utente che lancia l'app,
    essendo root a possedere i pacchetti installati) sia da un checkout
    di sviluppo con venv attivo — stesso meccanismo già usato altrove nel
    progetto per le risorse (icona SVG).
    """
    resource = importlib.resources.files("klamav_py.gui") / "resources" / "freshclam-update.sh"
    return str(resource)


class UpdateWorker(QThread):
    output_line = Signal(str)
    finished_update = Signal(bool, str)  # (successo, messaggio)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        try:
            # pkexec esegue `sh <script>` con privilegi di root: sh legge
            # ed esegue il contenuto del file (vedi _freshclam_update_script
            # e il file stesso per i dettagli e le garanzie di sicurezza).
            cmd = ["pkexec", "sh", _freshclam_update_script()]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Unisce stdout e stderr
                text=True,
                bufsize=1, # Line buffering per output in tempo reale
            )

            for line in process.stdout:
                self.output_line.emit(line.strip())

            process.wait()
            success = (process.returncode == 0)
            
            if success:
                msg = "Database aggiornato con successo."
            else:
                msg = f"Errore durante l'aggiornamento (codice {process.returncode})."
                
            self.finished_update.emit(success, msg)

        except FileNotFoundError:
            self.finished_update.emit(False, "Comando 'pkexec' o 'sh' non trovato. Assicurati che il sistema sia configurato correttamente.")
        except Exception as exc:
            self.finished_update.emit(False, f"Errore imprevisto: {exc}")
