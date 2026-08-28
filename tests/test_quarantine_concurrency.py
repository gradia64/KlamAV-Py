"""
Concorrenza e guard strutturali della quarantena: flock sull'indice e
rifiuto di ri-quarantenare. Tutto su filesystem temporaneo.
"""

import fcntl
import threading
import time
from pathlib import Path

import pytest

from klamav_py.quarantine import Quarantine, QuarantineError


def test_reject_requarantine_inside_quarantine_dir(tmp_path):
    q = Quarantine(tmp_path / "q")
    # Un file DENTRO la directory di quarantena (es. un vecchio EICAR
    # ri-rilevato da una scansione home-wide che la copre) non deve
    # essere ri-spostato: nuova entry + vecchia entry orfana = la
    # quarantena si moltiplicherebbe a ogni ciclo.
    victim = q.dir / "already_inside.bin"
    victim.write_bytes(b"x")
    with pytest.raises(QuarantineError):
        q.quarantine_file(victim, "Eicar-Test-Signature")
    # E l'indice non deve essere toccato dal rifiuto.
    assert q.list_entries() == []


def test_index_lock_serializes_writers(tmp_path):
    """
    Prova diretta del flock: un "altro processo" (nel test, il thread
    principale) tiene il lock; quarantine_file chiamato da un thread
    non deve completare finché il lock non viene rilasciato.
    """
    q = Quarantine(tmp_path / "q")
    lock_path = tmp_path / "q" / "index.json.lock"

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        done = threading.Event()

        def writer():
            f = tmp_path / "victim.txt"
            f.write_bytes(b"eicar")
            q.quarantine_file(f, "Eicar-Test-Signature")
            done.set()

        t = threading.Thread(target=writer)
        t.start()

        time.sleep(0.5)  # margine: se il lock non fosse rispettato, qui avrebbe finito
        assert not done.is_set(), "quarantine_file non ha rispettato il lock sull'indice"

        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        t.join(timeout=5)
        assert done.is_set(), "quarantine_file non si è sbloccato dopo il rilascio"

    assert len(q.list_entries()) == 1


def test_concurrent_quarantine_calls_do_not_lose_entries(tmp_path):
    """
    Prova end-to-end della race read-modify-write: più writer concorrenti
    (il caso GUI + CLI + worker) senza lock esterno — ogni entry deve
    sopravvivere. Senza flock, l'ultima scrittura cancellava le entry
    delle altre.
    """
    q = Quarantine(tmp_path / "q")
    n_threads, per_thread = 5, 4

    def worker(base: int):
        for i in range(per_thread):
            f = tmp_path / f"victim_{base}_{i}.txt"
            f.write_bytes(b"eicar")
            q.quarantine_file(f, "Eicar-Test-Signature")

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    entries = q.list_entries()
    assert len(entries) == n_threads * per_thread
    # E tutti i file sono fisicamente in quarantena (nessun move perso).
    assert (
        len(
            [
                p
                for p in q.dir.iterdir()
                if p.name != "index.json" and p.name != "index.json.lock"
            ]
        )
        == n_threads * per_thread
    )
