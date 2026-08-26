"""
Worker eseguito in un QThread separato per aggiornare il database di ClamAV.
Usa pkexec per ottenere i permessi di root tramite Polkit/KDE.
Gestisce automaticamente il blocco del file di log fermando e riavviando 
il demone di sistema clamav-freshclam.
"""

from __future__ import annotations

import subprocess
from PySide6.QtCore import QThread, Signal


class UpdateWorker(QThread):
    output_line = Signal(str)
    finished_update = Signal(bool, str)  # (successo, messaggio)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        try:
            # Spiegazione dei comandi:
            # 1. systemctl stop: Ferma il demone (su Debian/Ubuntu è clamav-freshclam, su Arch/Fedora è freshclam). 
            #    Usiamo 2>/dev/null per silenziare gli errori se il servizio ha un nome diverso o non esiste.
            # 2. freshclam --stdout: Lancia l'aggiornamento reale.
            # 3. res=$?: Salva il codice di uscita di freshclam.
            # 4. systemctl start: Riavvia il demone di sistema.
            # 5. exit $res: Esce restituendo il codice di freshclam (per dire alla GUI se è andato bene o no).
            
            command_string = (
                "systemctl stop clamav-freshclam 2>/dev/null; "
                "systemctl stop freshclam 2>/dev/null; "
                "freshclam --stdout; "
                "res=$?; "
                "systemctl start clamav-freshclam 2>/dev/null; "
                "systemctl start freshclam 2>/dev/null; "
                "exit $res"
            )
            
            # pkexec esegue sh, che esegue la nostra stringa di comandi in sequenza
            cmd = ["pkexec", "sh", "-c", command_string]
            
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
