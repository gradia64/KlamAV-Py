"""Avvio di KlamAV-Py dall'AppImage (packaging/appimage/AppRun).

Installato in <AppDir>/usr/share/klamav-py/launcher.py ed eseguito con il
Python dell'AppImage; il primo argomento sceglie l'entry point («gui» o
«cli»), il resto passa invariato.

NON `python3 -m klamav_py…`: con -m Python mette la cartella corrente in
sys.path[0], davanti a tutto, e un json.py (o un checkout di klamav_py/)
nella cartella di avvio verrebbe importato al posto dei moduli veri. Con
uno script sys.path[0] è la cartella dello script.
"""
import sys

entry = sys.argv[1] if len(sys.argv) > 1 else "gui"
args = sys.argv[2:]

if entry == "cli":
    # La CLI non importa Qt: funziona anche senza sessione grafica.
    from klamav_py.cli import main

    sys.argv = ["klamav-py", *args]
    sys.exit(main(args))

from klamav_py.gui.app import main  # noqa: E402

sys.argv = ["klamav-py-gui", *args]
sys.exit(main())
