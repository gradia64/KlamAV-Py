# La sola presenza di questo file fa sì che pytest metta la radice del
# progetto in testa a sys.path (rootdir come prima entry). Senza, con il
# pacchetto klamav-py installato nel sistema e il venv non attivo, i test
# importerebbero /usr/lib/python3/dist-packages/klamav_py invece del
# sorgente: risultati falsi in entrambe le direzioni (moduli nuovi
# "mancanti", oppure test verdi su codice vecchio).

import pytest


@pytest.fixture(autouse=True)
def _registro_prese_visione_isolato(tmp_path_factory, monkeypatch):
    """Nessun test legge o scrive il registro delle prese visione reale
    (~/.local/share/klamav-py/acknowledged.json) di chi lancia la suite:
    worker e CLI lo consultano per ogni rilevamento «solo segnalazione»."""
    import klamav_py.acknowledged as acknowledged

    path = tmp_path_factory.mktemp("prese-visione") / acknowledged.REGISTRY_NAME
    monkeypatch.setattr(acknowledged, "default_registry_path", lambda: path)
