# La sola presenza di questo file fa sì che pytest metta la radice del
# progetto in testa a sys.path (rootdir come prima entry). Senza, con il
# pacchetto klamav-py installato nel sistema e il venv non attivo, i test
# importerebbero /usr/lib/python3/dist-packages/klamav_py invece del
# sorgente: risultati falsi in entrambe le direzioni (moduli nuovi
# "mancanti", oppure test verdi su codice vecchio).
