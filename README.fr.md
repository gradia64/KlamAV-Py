<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-Italiano-green?style=for-the-badge" alt="Italiano"></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/Lang-English-blue?style=for-the-badge" alt="English"></a>
  <a href="README.fr.md"><img src="https://img.shields.io/badge/Lang-Français-blue?style=for-the-badge" alt="Français"></a>
  <a href="README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-blue?style=for-the-badge" alt="Deutsch"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

# KlamAV-Py

**Licence :** GNU General Public License v3.0

Frontend minimal pour ClamAV via clamd — CLI et GUI pour Linux.

Réécriture minimale, en Python, de l'idée à la base de KlamAV 0.22 (frontend à ClamAV), sans les parties devenues technologie morte (Dazuko, DCOP, Qt3) et sans les problèmes de sécurité de l'original (injection de shell via KShellProcess) : aucun exécutable externe n'est invoqué pour la numérisation, les chemins ne transitent jamais par un shell.

<p align="center">
  <img src="docs/screenshots/scan.png" alt="KlamAV-Py — scan" width="45%" />
  <img src="docs/screenshots/quarantine.png" alt="Quarantaine" width="45%" />
</p>
<p align="center">
  <img src="docs/screenshots/history.png" alt="Historique" width="45%" />
  <img src="docs/screenshots/settings.png" alt="Paramètres" width="45%" />
</p>

## Caractéristiques

- Numérisation via le protocole natif de clamd (INSTREAM/IDSESSION sur socket Unix), CLI et GUI.
- CLI sans dépendances externes (bibliothèque standard uniquement), GUI en PySide6.
- Quarantaine avec permissions neutralisées (0400), nom sur disque imprévisible, restauration des permissions d'origine, index partagé et protégé par verrou entre CLI/GUI/worker.
- Numérisations planifiées : timer interne à la GUI et/ou unit systemd utilisateur (via paquet .deb), avec journal persistant des résultats.
- Surveillance en temps réel RÉCURSIVE des dossiers configurés (QFileSystemWatcher, avec expansion des sous-dossiers, réconciliation périodique des watches perdus et plafond configurable pour ne pas épuiser fs.inotify.max_user_watches).
- Pause/Reprise des numérisations, garde « une numérisation à la fois ».
- Pré-vérification dimensionnelle : les fichiers au-delà de StreamMaxLength sont signalés « non vérifiés » sans gaspiller la session clamd.
- Prise de connaissance des signalements non déplacés (signatures heuristiques, archives de messagerie) : après les avoir vérifiés, ils ne sont plus comptés parmi les infectés, tant que le contenu ne change pas.
- Intégration menu contextuel Dolphin, autostart, system tray, single-instance avec IPC.
- Contrôle des mises à jour de l'application via GitHub Releases, manuel ou au démarrage.

## Prérequis

- clamav-daemon installé et actif (clamd), et non les seuls binaires clamscan/freshclam : ce projet parle avec le démon via socket, et n'invoque aucun exécutable externe pour la numérisation.
- Python 3.10+ comme baseline déclarée. Le minimum technique réel est 3.9 (Path.is_relative_to) ; le walrus operator utilisé dans clamd_client.py existe depuis 3.8. Testé localement sur 3.14 (Debian Sid) et en CI sur 3.10–3.14.
- CLI : aucune dépendance externe à l'exécution — tourne même avec le Python du système, sans venv.
- GUI : PySide6, à installer dans un venv dédié (voir ci-dessous), et non dans le Python du système.
- GUI, uniquement pour la mise à jour de la base de virus via le bouton « Mettre à jour la base » : freshclam dans le PATH et pkexec (PolicyKit) disponibles, l'opération nécessitant des privilèges root.
- GUI, uniquement pour l'intégration avec le menu contextuel de Dolphin : un environnement KDE Plasma avec kbuildsycoca5/kbuildsycoca6.
- Développement/test : pytest (voir requirements-dev.txt), non requis à l'exécution.

## Installation

### Depuis une release précompilée (recommandée pour l'usage quotidien)

Télécharge le paquet .deb depuis la page Releases et installe :

```bash
sudo dpkg -i klamav-py_*.deb
sudo apt-get install -f    # répare les dépendances manquantes, si nécessaire
```

Installe klamav-py (CLI) et klamav-py-gui dans /usr/bin/, active automatiquement le timer systemd utilisateur pour la numérisation planifiée et enregistre l'application dans les menus. Détails et avertissements dans la section « Paquetisation .deb » plus bas.

### Depuis AUR (Arch Linux et dérivées)

```sh
git clone https://aur.archlinux.org/klamav-py.git
cd klamav-py && makepkg -si
```

Ou avec un helper AUR : `yay -S klamav-py`, `paru -S klamav-py`.

Le paquet vaut aussi pour Manjaro, EndeavourOS, CachyOS et les autres dérivées qui utilisent AUR. `pyside6` et `clamav` sont des dépendances optionnelles au niveau pacman : la CLI fonctionne sans GUI, mais pour l'usage normal les deux sont nécessaires.

Contrairement au `.deb`, le timer de numérisation planifiée **n'est pas** activé pendant l'installation : sur Arch la politique est que l'état du système est décidé par l'utilisateur. Le paquet affiche l'instruction à exécuter :

```sh
systemctl --user enable --now klamav-scan.timer
```

### Depuis les sources (développement ou build du paquet)

**Build du .deb :**

```bash
sudo apt install devscripts debhelper dh-python pybuild-plugin-pyproject \
    python3-all python3-setuptools
cd klamav-py
dpkg-buildpackage -us -uc -b
sudo dpkg -i ../klamav-py_<versione>_all.deb
```

**Environnement de développement (GUI depuis le checkout) :**

```bash
cd klamav-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Avec Python 3.10 et 3.11, `requirements.txt` installe PySide6 6.11.x : les wheels à partir de 6.12 sont compilées pour Python 3.12 et sur ces versions font planter la GUI après quelques centaines de signaux (`Fatal Python error: bool_dealloc` ; [PYSIDE-3474](https://qt-project.atlassian.net/browse/PYSIDE-3474)). Dans un venv déjà existant, il suffit de relancer `pip install -r requirements.txt` ; pour vérifier : `python -c "import PySide6; print(PySide6.__version__)"` doit indiquer 6.11 ou antérieur.

Avec le venv actif (ou en appelant directement venv/bin/python) :

```bash
python3 -m klamav_py.gui.app
```

La CLI n'a pas besoin du venv et tourne avec le Python du système.

Note pour le développement : si le venv contient aussi une copie installée du paquet (`pip install .` non-editable), l'import de klamav_py peut se résoudre là plutôt que dans le checkout selon le répertoire courant. Pour éviter le piège, utilise `pip install -e .` ; et sur une machine avec checkout et .deb installés ensemble, vérifie toujours quelle instance GUI est réellement vivante (`ps aux | grep klamav`) : le single-instance fait que le second lancement est absorbé par la première instance existante, quelle qu'elle soit.

## Usage rapide

```bash
# CLI : vérifier que clamd répond (fonctionne aussi sans venv)
python3 -m klamav_py.cli ping

# CLI : numérisation avec quarantaine automatique
python3 -m klamav_py.cli scan /home/utente/Scaricati \
    --quarantine ~/.local/share/klamav-py/quarantine

# CLI : numérisation de la home avec exclusions et log d'erreurs sur fichier
python3 -m klamav_py.cli scan ~ \
    --exclude ~/.local/share/klamav-py \
    --exclude ~/.cache \
    --log-errors ~/klamav-errors.log

# GUI (nécessite le venv actif ou l'interpréteur du venv, voir ci-dessus)
python3 -m klamav_py.gui.app

# ou, avec socket/quarantaine non standard :
python3 -m klamav_py.gui.app --socket /run/clamav/clamd.ctl \
    --quarantine-dir ~/.local/share/klamav-py/quarantine

# ou pour démarrer directement la numérisation d'un chemin (utilisé
# aussi par l'intégration Dolphin, voir ci-dessous) :
python3 -m klamav_py.gui.app --scan-target /percorso/da/scansionare
```

Le répertoire passé à `--quarantine` est toujours exclu automatiquement du parcours, aussi bien en CLI qu'en GUI : les fichiers déjà gérés ne doivent pas être re-détectés (et re-quaranténés) à chaque numérisation couvrant leur emplacement.

## CLI : options de numérisation

- `--quarantine DIR` — déplace les fichiers infectés dans DIR (créé avec permissions 0700).
- `--exclude DIR` — répertoire à exclure du parcours récursif (répétable). Les répertoires exclus ne sont même pas lus (pruning) : utile pour les caches, la Trash, les montages réseau synchronisés.
- `--max-stream-size BYTES` — seuil du pré-check dimensionnel (défaut 25MB, aligné sur StreamMaxLength de clamd.conf ; 0 désactive le pré-check).
- `--log-errors FILE` — détail de chaque erreur (chemin et motif) sur fichier. Le fichier est créé avec permissions 0600 ; les liens symboliques, les FIFO et les fichiers déjà existants appartenant à un autre utilisateur sont refusés (exit 2). Évite néanmoins les répertoires partagés comme /tmp : le log énumère les chemins de tes fichiers.
- `--version` — affiche la version et quitte (option globale, pas du sous-commande scan).
- `--no-persistent` / `--session-batch-size N` — contrôle de la session persistante (voir section dédiée).

Codes de sortie de `scan` — utiles pour OnFailure= dans systemd ou pour des scripts de surveillance. Vaut le premier cas qui s'applique, dans cet ordre :

| Ordre | Sortie | Cas |
| --- | --- | --- |
| 1 | 2 | Erreur bloquante, la numérisation ne démarre pas ou s'interrompt : clamd injoignable ; chemin inexistant, lien cassé, ni répertoire ni fichier régulier, ou non lisible ; quarantaine ou `--log-errors` non utilisables |
| 2 | 1 | Au moins une infection trouvée, même avec des pannes d'E/S |
| 3 | 2 | Au moins une panne d'E/S sans infection (voir ci-dessous) |
| 4 | 0 | Sinon |

Les permissions refusées, les fichiers disparus pendant la numérisation, les fichiers trop grands et les erreurs de clamd sur un seul fichier ne changent pas le code. Un sous-dossier aux permissions refusées ne change pas le code : il apparaît sur stderr comme `CARTELLA NON LEGGIBILE` et a un compteur à part dans le résumé ; s'il est attendu (dossiers créés avec sudo, bind mount de container) il faut l'exclure avec `--exclude`. Une autre erreur de lecture, sur un fichier ou un sous-dossier (EIO d'un disque qui se dégrade, ESTALE ou ETIMEDOUT d'un montage NFS qui ne répond pas), est une panne : elle compte parmi les erreurs et, sans infection, la numérisation sort avec 2 parce qu'une partie de l'arbre n'a pas été contrôlée ; la dernière ligne du résumé le dit aussi avec sortie 1. Dans la GUI le résultat est « Complétée avec erreurs », et la même phrase apparaît dans l'état et le rapport de la numérisation manuelle, dans la notification et dans le journal de la planification interne ; l'Historique a une colonne « Pannes d'E/S ».

## Signalements non déplacés et prise de connaissance

Les signatures heuristiques (`Heuristics.*`) et les fichiers dans les archives de messagerie sont seulement signalés, jamais déplacés automatiquement : déplacer un message casse l'archive du client de messagerie. Un tel signalement se répétait pourtant à chaque numérisation, et avec le timer système chaque nuit arrivait la même notification, jusqu'à ce qu'on cesse de la regarder.

Après avoir vérifié le fichier, supprime-le ou enregistre la prise de connaissance :

```bash
journalctl --user -u klamav-scan.service | grep INFETTO
klamav-py --acknowledge ~/.local/share/local-mail/trash/new/1695.R42.host
klamav-py --list-acknowledged        # préfixe du hash, signature, chemin, dates
klamav-py --unacknowledge 3f2a9c01be47   # par préfixe du hash ou par chemin
```

`--acknowledge` rescane le fichier avec clamd et enregistre la prise de connaissance seulement s'il est encore infecté avec un détecteur « signalement uniquement ». La règle est celle de la numérisation, il faut donc l'appeler avec les mêmes `--report-only` (et `--quarantine-all`) utilisés en numérisation, indiqués avant : `klamav-py --report-only ~/Archivio --acknowledge ~/Archivio/vecchio.exe`. Les numérisations suivantes affichent `GIÀ VALUTATO` et ne le comptent pas parmi les infectés : un signalement **nouveau** ressort encore avec 1 et notifie. La clé est le SHA-256 du contenu plus la signature, pas le chemin : un message qui passe de `new/` à `cur/` reste évalué, un contenu changé redevient signalé, et une prise de connaissance ne masque jamais un détecteur qui irait en quarantaine. La prise de connaissance vaut donc pour le contenu et la signature, dans n'importe quel chemin : une copie identique ailleurs, avec la même signature, ressort déjà évaluée. Les entrées qu'aucune numérisation ne retrouve depuis 90 jours sont retirées. Registre dans `~/.local/share/klamav-py/acknowledged.json` (0600), partagé avec la GUI, où la page **Signalements** offre les mêmes actions (et la suppression, seulement si le fichier est encore celui détecté ; pour un maildir géré par Akonadi il peut falloir ensuite `akonadictl fsck`). La page montre les signalements de la session courante de la fenêtre (manuelle, planifiée interne, temps réel) ; ceux du timer système se trouvent dans le journal et se gèrent avec les commandes ci-dessus.

## Session persistante, pré-check dimensionnel, reconstruction

Par défaut `scan_stream` réutilise une seule connexion (session IDSESSION) pour plusieurs fichiers à la suite au lieu d'en ouvrir une nouvelle pour chacun — sur des arbres de dizaines de milliers de fichiers cela évite l'overhead de connect répété. La session est tout de même fermée et rouverte tous les 500 fichiers (`--session-batch-size`) pour limiter l'impact de limites non documentées sur des sessions très longues, et recréée de zéro chaque fois que quelque chose tourne mal : un fichier problématique ne compromet pas le reste de la numérisation.

Quand clamd ferme la connexion (cas typique : refus pour StreamMaxLength), la session est marquée morte et recréée avant le fichier suivant, ainsi les fichiers suivants ne meurent pas en cascade sur une connexion déjà fermée.

Pré-check dimensionnel : les fichiers au-delà du seuil (défaut 25MB, comme StreamMaxLength) ne sont pas envoyés du tout à clamd — ils sortent tout de suite avec le statut TOO_LARGE (« non vérifié »), distinct des vraies erreurs aussi bien en CLI qu'en GUI. Sans pré-check, clamd refuse le stream à mi-envoi en fermant la connexion : l'erreur que l'utilisateur verrait serait une « pipe interrompue » sur un fichier qui va en fait très bien (voir « Diagnostic des erreurs courantes »).

Le pré-check a une race connue et acceptée — un fichier qui grossit au-delà du seuil entre le contrôle et l'envoi est de toute façon géré par le fallback qui classe les erreurs de stream sur les fichiers au-dessus du seuil comme TOO_LARGE.

Entrées non régulières (depuis 0.1.5) : les liens symboliques, les sockets, les FIFO et les périphériques sont écartés pendant le parcours et ne sont pas envoyés à clamd.

`os.walk()` classe comme « fichier » tout ce qui n'est pas un répertoire, donc ils finissaient dans la liste à numériser avec deux conséquences : les liens symboliques pendants (très fréquents dans node_modules et dans les stores pnpm nettoyés) produisaient des centaines de faux ERREUR « [Errno 2] Fichier ou répertoire inexistant », et `open()` sur une FIFO sans écrivain BLOQUE indéfiniment dans le noyau — aucune exception, aucun timeout : une seule FIFO dans l'arbre plantait la numérisation entière.

Ce ne sont pas des trous de couverture : un lien symbolique n'a pas de contenu propre (la cible, si elle est dans l'arbre, est numérisée comme fichier réel), et les sockets, FIFO et périphériques n'ont pas de contenu persistant infectable. C'est pourquoi ils n'apparaissent pas dans les résultats et n'entrent pas dans l'invariant `scanned = propres + infectés + erreurs + trop_grands` ; ils sont comptés à part et affichés dans le résumé de la CLI. TOO_LARGE et les permissions refusées restent au contraire visibles parce que ce sont de vrais trous.

L'ouverture utilise `O_NOFOLLOW|O_NONBLOCK` avec vérification `fstat()` sur le descripteur déjà ouvert : le contrôle du type porte exactement sur l'objet qui sera lu, sans fenêtre TOCTOU par rapport au chemin.

Timeout (depuis 0.1.5) : l'attente du verdict de clamd a son propre timeout (120s), distinct de celui de connexion et d'envoi (30s). Ce sont des choses différentes : envoyer les octets est rapide, mais l'analyse d'une archive ou d'un PDF avec de nombreux objets imbriqués peut demander beaucoup plus. Avec une valeur unique basse ces fichiers sortaient comme « session clamd interrompue : timed out » bien qu'ils soient parfaitement lisibles.

Les timeouts donnent droit à un second essai sur session propre (seulement les timeouts : sur pipe interrompue cela signifierait refus pour StreamMaxLength et réessayer serait du gaspillage) ; un fichier non vérifié même au second essai a un message distinct, parce que celui-là est bien un trou de couverture. La valeur doit rester cohérente avec CommandReadTimeout/ReadTimeout dans clamd.conf : si clamd lâche avant, allonger le timeout côté client ne sert à rien.

Si tu soupçonnes qu'un problème soit lié à la session persistante, `--no-persistent` revient au comportement « une connexion par fichier » pour isoler le cas.

En fin de numérisation la CLI affiche un résumé des erreurs regroupées par type (ex. « 2 Permission denied », « 1 Broken pipe »), au lieu d'avoir à faire grep/sort/uniq -c à la main.

## GUI : présentation de l'interface

Application PySide6 à fenêtre unique avec barre latérale et style inspiré de KDE Plasma (utilise toujours les couleurs de la palette système, jamais de couleurs en dur, sauf pour les états sémantiques comme « infecté »). Huit sections :

- **Numérisation** — numérisation manuelle sur un chemin au choix, dans un QThread séparé (ScanWorker) : la fenêtre reste réactive même sur de gros répertoires, et la barre d'état affiche le fichier en cours de traitement en temps réel (avec élypsis du texte pour ne pas faire croître le layout sur de longs chemins). Les compteurs distinguent numérisés / infectés / erreurs / non vérifiés (trop grands), et seuls infectés/erreurs/non-vérifiés produisent des lignes dans la liste.
- **Suspendre / Reprendre.** La pause a la granularité du fichier entier : le fichier en streaming est complété, puis le worker s'arrête (le bouton suit les signaux du worker, pas le clic, donc l'état affiché est toujours l'état réel). « Interrompre » reste actif même en pause. Après une pause plus longue que ~25s (proche de l'IdleTimeout de clamd, 30s par défaut) la session est recréée proactivement à la reprise, pour ne pas produire une erreur factice sur le premier fichier. Avec la fenêtre minimisée dans le tray, le tooltip de l'icône affiche les compteurs de la numérisation en cours et l'état de pause.
- **Quarantaine manuelle par défaut.** La case « Mettre automatiquement en quarantaine les fichiers infectés » est désactivée par défaut : les fichiers signalés restent où ils sont et tu les déplaces avec « Mettre en quarantaine les sélectionnés » — pratique pour évaluer un faux positif avant de déplacer quoi que ce soit. « Copier le log » copie dans le presse-papiers toutes les lignes de la liste (infectés, erreurs, non vérifiés).
- **Historique** — registre persistant (~/.local/share/klamav-py/history.json, les 1000 dernières entrées ; le répertoire est 0700 et les fichiers 0600, parce qu'ils énumèrent les chemins et les fichiers infectés) avec date/heure, type, chemin, numérisés, infectés, erreurs, non vérifiés, dossiers non lisibles, déjà évalués et pannes d'E/S (un sous-ensemble des erreurs), plus le motif pour une numérisation non complétée ou reportée. Les numérisations planifiées indexent aussi le journal détaillé sur disque (tooltip sur la ligne) : le détail infectés/erreurs d'une numérisation en arrière-plan, qui ne passe par aucune liste UI, reste ainsi toujours inspectable.
- **Quarantaine** — lit le même index JSON utilisé par la CLI (index.json dans le répertoire de quarantaine) : les deux outils sont interchangeables sur les mêmes données. Restauration à l'emplacement d'origine (avec restauration des permissions d'origine, refus explicite si le chemin a été entre-temps réoccupé) ou suppression définitive.
- **Signalements** — les fichiers détectés mais laissés à leur place (signatures heuristiques, archives de messagerie) dans les numérisations de la session, avec « J'ai vérifié, ne plus signaler » (prise de connaissance, voir « Signalements non déplacés et prise de connaissance ») et « Supprimer… » avec confirmation ; en dessous, les prises de connaissance enregistrées, révocables.
- **Mises à jour** — télécharge les définitions de virus en lançant `freshclam --stdout` via pkexec (authentification PolicyKit), en arrêtant le démon clamav-freshclam/freshclam du système pour éviter les conflits de verrou sur le fichier de log et en le redémarrant à la fin. Depuis 0.1.7 le redémarrage a lieu aussi si la mise à jour est interrompue (déconnexion, signal), et ne concerne que les services qui étaient actifs avant : un démon arrêté volontairement reste arrêté. Sortie affichée en temps réel. En choisissant « Quitter » pendant la mise à jour, l'app se ferme dès qu'elle se termine au lieu de l'interrompre. Par défaut elle démarre automatiquement 1.5s après le lancement de l'app (désactivable dans Paramètres).
- **Temps réel** — journal des numérisations automatiques sur les dossiers surveillés (configurables dans Paramètres). Utilise QFileSystemWatcher : création/modification d'un fichier → numérisation après un debounce de 3s (pour ne pas numériser les fichiers encore en écriture), et les fichiers infectés sont toujours mis en quarantaine automatiquement. Les fichiers trop grands sont étiquetés « Non vérifié », et non « Sûr » (voir les choix de design).
- **Planification** — numérisation récurrente (toutes les N heures ou jours) sur un dossier au choix, via QTimer interne : nécessite l'app en exécution (même minimisée dans le tray). Pendant l'exécution la page affiche l'état et les compteurs en direct ; le tooltip du tray reflète l'avancement ; en fin de numérisation le journal détaillé est écrit dans ~/.local/share/klamav-py/logs/scheduled-<timestamp>.log (rotation : les 10 dernières exécutions) et référencé dans l'Historique. Ce n'est pas un timer système comme celui du paquet .deb, qui fonctionne aussi à GUI fermée (voir « Numérisations planifiées : les deux mécanismes »).
- **Paramètres** — socket de clamd, répertoire de quarantaine, dossiers surveillés pour le temps réel (avec indicateur « Temps réel actif sur N dossiers (M sous-dossiers inclus, récursif) »), autostart au login, démarrage minimisé dans le tray, mise à jour de la base au démarrage, contrôle des mises à jour de l'application (bouton et option au démarrage), intégration Dolphin.

## Surveillance temps réel : comment ça marche (0.1.5)

QFileSystemWatcher n'est pas récursif : il observe exactement les dossiers qu'on lui passe. Jusqu'à la 0.1.4 cela signifiait que les fichiers créés dans les SOUS-DOSSIERS d'un dossier surveillé ne généraient aucun événement — l'interface déclarait le temps réel actif sur un dossier en restant aveugle à tout ce qui se trouvait en dessous.

Depuis la 0.1.5 les dossiers configurés sont expansés récursivement (liens symboliques non suivis, dossiers cachés ignorés parce qu'ils sont le plus souvent des caches qui génèrent des événements en continu). Trois chemins tiennent la couverture à jour :

- sous-dossiers créés après le démarrage : rattachés tout de suite, à la réception de l'événement sur le dossier qui les contient ;
- arbres déplacés dedans avec mv ou extraits d'une archive : mv génère un seul événement au niveau supérieur, donc les sous-dossiers qui arrivent déjà pleins sont récupérés par la réconciliation périodique (jusqu'à 60s de latence) ;
- watches perdus (dossier supprimé et recréé) : même réconciliation.

Les fichiers déjà présents dans un dossier découvert APRÈS le démarrage sont analysés, et non mis en baseline : c'est le scénario de l'archive extraite ailleurs puis déplacée dedans, dans lequel sinon le contenu ne serait jamais numérisé. Au démarrage de l'application le comportement est l'opposé et voulu : les fichiers déjà présents deviennent l'état de référence sans être numérisés, sinon chaque redémarrage resnumériserait tout le dossier surveillé.

Chaque dossier observé consomme un watch inotify, et fs.inotify.max_user_watches est partagé avec TOUS les processus de l'utilisateur (IDE, synchroniseurs cloud, indexeurs). C'est pourquoi il y a un plafond (MAX_WATCH_DIRS, défaut 8000) : contrôle ton propre limite avec `cat /proc/sys/fs/inotify/max_user_watches` et règle-le en conséquence.

Tant l'atteinte du plafond que les échecs de addPaths() sont signalés explicitement dans l'indicateur d'état — un temps réel qui se déclare actif tout en étant aveugle sur la moitié des dossiers est pire qu'un temps réel éteint.

La version du programme est visible dans le titre de la fenêtre, dans le tooltip de repos de la system tray et dans le panneau Paramètres.

Tous les paramètres sont enregistrés avec QSettings (organisation « KlamAV-Py », application « KlamAV-Py » — sur Linux typiquement ~/.config/KlamAV-Py/KlamAV-Py.conf). Les paramètres des versions antérieures à la 0.1.3-2 (fichier ~/.config/KlamAV/KlamAV.conf) sont migrés automatiquement au premier démarrage ; l'ancien fichier reste sur disque.

## Choix de design intentionnels

Quarantaine manuelle en Numérisation, automatique en temps réel. Ce n'est pas une incohérence : les dossiers surveillés en temps réel sont typiquement des destinations de fichiers tout juste téléchargés/reçus de l'extérieur (ex. ~/Téléchargements) — le risque de faux positif sur un fichier neuf et jamais touché est faible, donc il est sensé d'agir tout de suite. La page Numérisation peut être pointée sur des dossiers contenant des données importantes, où un faux positif doit être vérifié avant de déplacer quoi que ce soit.

Garde « une numérisation à la fois » entre manuelle et planifiée. Une numérisation manuelle ne démarre pas si une planifiée est en cours (et inversement la planifiée saute, avec entrée explicite dans l'Historique et notification, si une manuelle est active). Raisons : contention sur clamd (deux parcours home-wide en parallèle doublent l'E/S et la mémoire du démon) et double charge sur le système de fichiers. Le temps réel est délibérément exempt de la garde : c'est la protection primaire, il agit sur des fichiers isolés (secondes), et clamd gère des connexions concurrentes par design — le suspendre pendant qu'une manuelle tourne serait pire. Une numérisation en pause reste « en cours » aux fins de la garde : elle bloque la planifiée tant qu'elle ne reprend pas ou n'est pas interrompue.

Exclusion du répertoire de quarantaine. Les fichiers en quarantaine sont les données gérées par l'application : ils sont exclus du parcours (pruning, ils ne sont même pas lus) et, comme défense en profondeur, quarantine_file() refuse explicitement de re-quarantener un fichier qui se trouve déjà dans le répertoire de quarantaine. Sans ces gardes, une numérisation home-wide détecterait les vieux EICAR en quarantaine à chaque cycle, gonflant pour toujours infectés et erreurs avec des « fantômes » déjà gérés.

## System tray et single-instance

L'app reste active dans la system tray même en fermant la fenêtre (la X la cache, ne la termine pas : on sort par le menu du tray ou par « Quitter »). Elle est pensée pour rester en arrière-plan pour la planification et le temps réel. À la sortie les threads en cours (numérisations, ping à clamd, contrôle des mises à jour) sont arrêtés et attendus au plus 3 secondes ; ceux bloqués sur de l'E/S au-delà de cette limite n'empêchent pas la fermeture.

Elle est aussi single-instance : si tu lances la GUI alors qu'une autre instance est déjà active, la seconde envoie son `--scan-target` (s'il est présent) à la première et se termine tout de suite, au lieu d'ouvrir une seconde fenêtre. C'est le mécanisme qui rend sensée l'intégration Dolphin : en cliquant « Scanner avec KlamAV-Py » sur plusieurs fichiers/dossiers, toutes les requêtes finissent dans la même fenêtre déjà ouverte.

Le socket IPC est dans le répertoire runtime de l'utilisateur (`$XDG_RUNTIME_DIR/klamav-py-ipc`, typiquement `/run/user/<uid>/`), et non dans `/tmp`, et avant d'envoyer quoi que ce soit la seconde instance vérifie avec `SO_PEERCRED` que le processus à l'écoute appartient au même utilisateur. S'il n'existe pas de répertoire runtime sûr, ou si le serveur IPC ne parvient pas à démarrer, la GUI démarre quand même sans single-instance et le signale : un second démarrage ouvrirait une autre copie.

En mettant à jour depuis la 0.1.6 ou antérieures, une ancienne instance encore ouverte utilise l'ancien socket et n'est pas reconnue par la nouvelle : ferme-la depuis le tray avant de redémarrer l'application.

## Intégration Dolphin

Depuis le panneau Paramètres on peut installer l'entrée « Scanner avec KlamAV-Py » dans le menu contextuel de Dolphin, en écrivant un fichier .desktop dans ~/.local/share/kservices5/ServiceMenus/ et ~/.local/share/kio/servicemenus/ et en régénérant le cache des services KDE (kbuildsycoca5/6). Le `Exec=` invoque directement la commande sans shell intermédiaire : un nom de fichier avec ", ` ou $ ne peut pas casser le quoting.

## Autostart

Si activé, écrit un fichier .desktop dans ~/.config/autostart/ qui lance la GUI avec l'interpréteur Python actuellement utilisé ; si désactivé, supprime le fichier.

## Contrôle des mises à jour de l'application

Le bouton « Contrôler les mises à jour » dans Paramètres interroge l'API des releases GitHub du projet et compare la dernière version publiée avec celle en usage. L'option « Contrôler les mises à jour au démarrage » est désactivée par défaut : sans elle l'application ne contacte pas GitHub sauf sur demande explicite. S'il y a une nouvelle version apparaît un lien vers la page de la release (et une notification dans le tray, pour le contrôle au démarrage) ; l'installation reste manuelle, via .deb ou AUR.

Le contrôle au démarrage se lance au plus une fois toutes les six heures : l'API GitHub non authentifiée accorde 60 requêtes par heure par adresse IP, et redémarrer souvent l'application ne doit pas les consommer. Le bouton dans Paramètres n'est pas soumis à la limite.

La réponse de GitHub est traitée comme donnée non fiable : elle est lue jusqu'à un maximum de 256 Ko, la version et les notes sont échappées avant d'être affichées, et le lien n'est rendu cliquable que s'il pointe vers les releases du dépôt officiel.

## Numérisations planifiées : les deux mécanismes

- **Timer de la GUI (page Planification) :** tourne seulement avec l'app active (même dans le tray), mais offre la progression en direct, le journal persistant et l'intégration complète avec l'UI.
- **Unit systemd utilisateur (installée par le .deb et le paquet Arch) :** démarre au login de chaque utilisateur sans configuration, fonctionne à GUI fermée. Pour faire tourner le timer même avec l'utilisateur déconnecté :
  `loginctl enable-linger $USER`. Utilise le même chemin de quarantaine par défaut que la GUI (~/.local/share/klamav-py/quarantine). Les erreurs et les dossiers non lisibles de la dernière numérisation, un par ligne, sont aussi dans `~/.local/state/log/klamav-py/scan-errors.log` (`$XDG_STATE_HOME/log/klamav-py`, le `LogsDirectory=` de la unit), en plus du journal.
  La quarantaine, la connexion à clamd et les dossiers exclus choisis dans la GUI arrivent au timer avec un drop-in, `~/.config/systemd/user/klamav-scan.service.d/50-klamav-py.conf`, reconnu par la première ligne `# Generato da KlamAV-Py: non modificare a mano.`. Ils sont écrits par les sauvegardes de Paramètres et Planification ; au démarrage la GUI réécrit aussi un drop-in qui serait resté à une version précédente (par exemple sans `--log-errors`) et le dit dans la page Planification. Un fichier de ce nom sans cette première ligne, c'est-à-dire écrit à la main, n'est jamais touché. Pour voir la commande que le timer utilisera : `systemctl --user cat klamav-scan.service`.

Une unit systemd de système n'est plus distribuée (depuis la 0.1.9) : elle ne pouvait pas lire les homes privés sans en élargir les permissions, tandis que la unit utilisateur tourne comme l'utilisateur lui-même et lit sa home telle quelle.

## Diagnostic des erreurs courantes

Sur une numérisation home-wide d'une machine de développement réelle (330k+ fichiers) les erreurs résiduelles sont physiologiques et reconnaissables :

- **« Pipe interrompue » sur de gros fichiers** — clamd a refusé le stream pour StreamMaxLength (25MB par défaut dans clamd.conf) en fermant la connexion à mi-envoi. Avec le pré-check dimensionnel actif c'est quasiment disparu : les fichiers au-delà du seuil sortent comme « non vérifiés » sans toucher la session. Pour relever la limite :
  `StreamMaxLength 100M` dans clamd.conf et redémarrage de clamd — avec le compromis qu'une limite plus haute rend plus facile de saturer clamd avec des streams énormes.

  Note d'honnêteté : même les fichiers dans la limite de stream restent soumis à MaxScanSize/MaxFileSize/MaxRecursion de clamd (défauts approximatifs 100M/25M/16) : une archive volumineuse peut ressortir « OK » avec numérisation partielle — limite de clamd lui-même, non détectable par ce projet.

- **Errno 13 « Permission refusée »** — permissions réelles du système de fichiers (ex. fichiers possédés par des containers Docker dans la home). Ce n'est pas un problème du projet.

- **Errno 2 « Fichier ou répertoire inexistant »** — depuis la 0.1.5 cela devrait être rare. Jusqu'à la 0.1.4 la cause dominante N'ÉTAIT PAS celle documentée ici (fichiers disparus pendant le parcours) : c'étaient les liens symboliques pendants, que os.walk() classe comme fichiers et que open() ne peut pas ouvrir. Sur des arbres avec node_modules ou des stores pnpm nettoyés c'étaient des centaines de lignes qui cachaient les vraies erreurs. Maintenant les liens symboliques sont écartés en amont. Ce qui reste est la cause originellement supposée — des fichiers vraiment disparus entre le parcours et la lecture (Trash vidé, sync cloud, navigateur qui réécrit le cache) — et sous cette forme c'est du bruit attendu.

- **Errno 6 « Périphérique ou adresse inexistante »** — sockets AF_UNIX (agent GPG, MEGAsync, hôte de native messaging des navigateurs). Comme ci-dessus : écartés en amont depuis la 0.1.5, ne devraient plus apparaître.

- **« timed out »** — là aussi l'explication jusqu'à la 0.1.4 était incomplète. Pas seulement la latence de montage réseau : le timeout unique de 30s couvrait à la fois l'envoi ET l'ATTENTE du verdict, et clamd peut mettre beaucoup plus sur des archives ou des PDF avec de nombreux objets imbriqués. Des fichiers locaux parfaitement lisibles ressortaient « session clamd interrompue : timed out » de manière reproductible. Depuis la 0.1.5 l'attente a son propre timeout (120s) et les timeouts ont un second essai. Si tu en vois encore, vérifie CommandReadTimeout/ReadTimeout dans clamd.conf avant de soupçonner le système de fichiers.

- **Erreurs en rafale d'un seul type (milliers)** — presque certainement clamd mort ou redémarré à mi-numérisation :
  `journalctl -u clamav-daemon.service` dans l'intervalle de la numérisation, et `dmesg | grep -i oom` pour le cas OOM. Le résumé par type de la CLI (ou le journal persistant de la planifiée) rend ce cas immédiatement distinguable du bruit physiologique.

## Correctifs de sécurité dans la 0.1.8

- **Données de l'application lisibles par d'autres utilisateurs locaux.** L'historique, les journaux des numérisations planifiées et les journaux d'erreurs de la CLI étaient créés avec les permissions par défaut de l'umask (répertoire 0755, fichier 0644) : ils énumèrent les chemins des fichiers numérisés, les noms des signatures et les fichiers infectés. Sur un système avec home 0755 — défaut historique, encore fréquent sur des installations mises à jour — ils étaient lisibles par un autre utilisateur local. Maintenant le répertoire des données est 0700 (resserré au démarrage, donc même les fichiers des versions précédentes deviennent inaccessibles) et les fichiers 0600. Le journal de `--log-errors` était le pire cas, parce que le README en suggérait l'usage dans /tmp, où un autre utilisateur pouvait le pré-créer pour en lire le contenu.
- **Fichier de configuration en 0644.** `~/.config/KlamAV-Py/KlamAV-Py.conf` énumère les dossiers surveillés par le temps réel et les chemins de numérisation et de quarantaine, mais il est écrit par Qt et échappait au traitement des autres données. Il est porté à 0600 au démarrage, avec le fichier de configuration legacy de la 0.1.3 s'il est encore présent.
- **Réponses de clamd sans plafond.** Les réponses étaient accumulées en mémoire tant que le socket en fournissait : un clamd distant via TCP, ou un socket dans un chemin configurable par l'utilisateur, pouvait épuiser la RAM. La lecture s'arrête maintenant à 1 MiB. Ne concerne pas le clamd du système, qui est root.

## Correctifs de sécurité dans la 0.1.7

- **Contournement de la quarantaine via hardlink + symlink.** La vérification de l'identité de l'inode après le déplacement (introduite dans la 0.1.4, voir ci-dessous) utilisait `os.stat()`, qui suit les liens symboliques. Un fichier infecté pouvait se créer un hardlink ailleurs et remplacer son propre chemin par un lien symbolique vers cet hardlink : le déplacement mettait en quarantaine seulement le lien, `os.stat()` retrouvait l'inode d'origine et la vérification passait. Le contenu infecté restait dehors et exécutable, l'index enregistrait une quarantaine réussie et « Supprimer définitivement » ne retirait que le lien. La vérification utilise maintenant `os.lstat()` et les permissions sont appliquées avec `fchmod()` sur le descripteur déjà vérifié. Nécessite de gagner une course avec un processus du même utilisateur, mais c'est répétable en boucle.
- **Socket IPC single-instance dans /tmp.** Jusqu'à la 0.1.6 le socket était `/tmp/klamav_py_ipc`, dans un répertoire partagé par tous les utilisateurs. `UserAccessOption` empêchait les autres de se connecter au socket de l'application, mais n'empêchait pas l'application de se connecter au socket de quelqu'un d'autre : un second utilisateur local pouvait occuper le nom le premier, recevoir les chemins des fichiers envoyés en numérisation et, en répondant à la connexion, faire croire à chaque démarrage qu'une instance était déjà active, empêchant totalement le démarrage de la GUI (pas de temps réel, pas de numérisations planifiées). Maintenant le socket est dans le répertoire runtime de l'utilisateur, le client vérifie le peer avec `SO_PEERCRED` et l'échec de `listen()` est signalé. Non exploitable sur un bureau avec un seul utilisateur.

## Correctifs de sécurité dans la 0.1.4

Cette version résout une série de problèmes de sécurité apparus lors d'un audit du code. Le modèle de menace de référence est un système multi-utilisateur et, pour la quarantaine, le fichier infecté lui-même traité comme contenu potentiellement hostile (pas un attaquant externe).

- **TOCTOU en quarantaine et restauration.** `quarantine_file()` ouvre le fichier avec O_NOFOLLOW et vérifie l'identité de l'inode (dev+ino) après le déplacement, au lieu de se fier à un contrôle is_file() séparé de l'opération de move : un fichier infecté pourrait tenter d'évader l'isolation en se substituant par un lien symbolique dans la fenêtre entre le contrôle et le déplacement. Les liens symboliques (et les fichiers spéciaux comme les FIFO) sont maintenant refusés explicitement. `restore()` réclame la destination de manière atomique avec O_CREAT|O_EXCL au lieu d'un exists() suivi de move(), éliminant la fenêtre dans laquelle un autre processus pouvait créer le fichier de destination au milieu.
- **Socket IPC single-instance restreint et validé.** Le QLocalServer utilise UserAccessOption : sans cela, sous Linux les permissions du socket dépendent de l'umask du processus et pourraient être accessibles à d'autres utilisateurs du système. Le chemin reçu par le socket est maintenant limité en taille (évite les payloads énormes pensés comme DoS) et décodé de manière robuste (un payload UTF-8 malformé est ignoré, ne propage pas d'exception). Depuis la 0.1.7 le socket n'est plus dans `/tmp` : voir la section précédente.
- **Mise à jour de la base déléguée au service système.** Le bouton « Mettre à jour la base » exécute seulement `pkexec /usr/bin/systemctl restart clamav-freshclam.service`, avec argv fixe et binaires vérifiés root-owned : freshclam télécharge les signatures comme utilisateur `clamav`, avec le sandboxing de l'unité de la distribution, au lieu de tourner en root. Le résultat se vérifie depuis la version du DB rapportée par clamd ; au démarrage la mise à jour ne démarre que si les signatures ont plus de 36 heures.
- **Régénération du cache de services KDE sans shell.** `os.system()` a été remplacé par `subprocess.run()` avec argv explicite (pas de shell).
- **Permissions des fichiers .desktop de l'intégration Dolphin passées de 0755 à 0644.**

  **ATTENTION** — cette modification était FAUSSE et a été révoquée dans la 0.1.5. Le raisonnement (les .desktop sont des fichiers de configuration, la spec freedesktop ne veut pas le bit d'exécution) vaut pour les lanceurs dans /usr/share/applications, PAS pour les servicemenu de KIO : depuis KFrameworks 5.85 ceux-ci doivent être exécutables, et avec 0644 Dolphin répond « Vous n'êtes pas autorisé à exécuter ce fichier » et l'entrée de menu cesse de fonctionner. Les deux destinations ont des exigences opposées : le lanceur reste 0644, le servicemenu repasse à 0755. Laissé ici documenté parce que la justification semble correcte lue seule et c'est exactement la manière dont la régression peut être réintroduite.

Les correctifs sont couverts par 9 nouveaux tests automatiques (refus de liens symboliques et de FIFO en quarantaine, vérification de la réclamation atomique de la destination lors de la restauration, validation du payload IPC).

## Note sur la 0.1.4-2 (exactitude, pas sécurité)

La racine de chaque numérisation est maintenant résolue (`Path.resolve()`) au début de `scan_stream`, en un seul point commun à toutes les manières de lancer une numérisation (CLI, GUI manuelle, intégration Dolphin/IPC, et prospectivement temps réel et planifiée). Ce n'est pas un correctif de sécurité : dans le cas de l'IPC le socket est déjà restreint au même utilisateur, donc il n'existe pas de frontière de privilège à protéger et un lien symbolique ne donne accès à rien que l'utilisateur ne puisse déjà lire. C'est un correctif d'exactitude : si le chemin à numériser est un lien symbolique-répertoire, `os.walk()` le suit quand même lorsqu'il est le point de départ (followlinks=False bloque seulement les liens symboliques internes à l'arbre, pas la racine), et sans résolution le rapport de numérisation afficherait les résultats sous le chemin du lien plutôt que sous celui réellement lu. Pour un antivirus compte le contenu réel contrôlé, pas l'alias par lequel on y est arrivé.

La résolution a lieu une seule fois sur la racine, pas pour chaque fichier : `_iter_files` (le parcours proprement dit) reste inchangé, donc tant le streaming à mémoire constante que le pruning des répertoires exclus (quarantaine incluse) continuent de fonctionner exactement comme avant. Les liens symboliques internes à l'arbre continuent de ne pas être suivis.

## Ce qui manque par rapport à KlamAV original (volontairement)

- **Aucun on-access scanning au niveau noyau.** La surveillance temps réel est basée sur QFileSystemWatcher (récursive depuis la 0.1.5, avec quelques secondes de latence et un plafond au nombre de dossiers observés) — ce n'est pas un substitut d'un vrai on-access scanner. Pour cela ClamAV fournit déjà clamonacc (basé sur fanotify) : il n'y a pas de sens à réimplémenter un équivalent de Dazuko. Si un on-access réel est nécessaire, on le configure via clamd.conf/clamav-clamonacc.service.
- **Aucune intégration client mail.** Si une numérisation de la messagerie est nécessaire, aujourd'hui il est plus pertinent côté serveur (milter) que côté client KMail/Evolution comme faisait klammail.

## Développement et tests

```bash
cd klamav-py
python3 -m venv venv        # si tu ne l'as pas déjà créé
source venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

Les tests couvrent la logique pure sans clamd réel (parsing du protocole clamd, gestion de la quarantaine sur système de fichiers temporaire, CLI) et la GUI avec instanciation offscreen des pages et simulation de signaux/actions (reprise Numérisation↔Quarantaine, élypsis d'état, presse-papiers, pause/reprise avec client factice injecté via client_factory).

Sur les distributions avec Python « externally managed » (Debian/Ubuntu récents, PEP 668), `pip install -r requirements-dev.txt` hors d'un venv échoue avec externally-managed-environment : comportement attendu du système, utilise le venv.

## Licence

KlamAV-Py est publié sous licence GPL-3.0-or-later (voir le fichier LICENSE). Code écrit de zéro : ne contient pas de code provenant de KlamAV 0.22, dont il n'est que l'héritier spirituel de l'idée.

## Paquetisation .deb

Le squelette Debian est dans debian/, testé avec build réel (dpkg-buildpackage) et installation/suppression complètes (dpkg -i / apt-get remove --purge).

Vérifie avant d'installer : le squelette a été validé dans un environnement Ubuntu 24.04, où python3-pyside6.qtcore et apparentés n'existent pas dans les dépôts (Ubuntu ne paquetise que PySide2). Sur Debian ils devraient exister, mais avant d'installer le .deb contrôle avec `apt-cache search pyside6` que les paquets listés dans debian/control (python3-pyside6.qtcore, .qtgui, .qtwidgets, .qtnetwork) existent dans ta version, et corrige les noms s'ils diffèrent.

### Ce que fait le squelette Debian

- Un seul paquet binaire klamav-py avec CLI et GUI (pas de split, tant que le projet est jeune).
- `pyproject.toml` définit deux entry points dans /usr/bin/ : klamav-py (CLI) et klamav-py-gui (GUI). La GUI les utilise pour auto-détecter la commande à écrire dans les .desktop d'autostart et de Dolphin (`_gui_relaunch_command()`), au lieu de supposer un checkout local avec venv.
- Le champ license utilise délibérément l'ancien format tableau (`{text = "..."}`) au lieu de la chaîne SPDX : setuptools récents déprécient le premier avec un warning, mais les setuptools anciens (vérifié : 68.1.2, Ubuntu 24.04) refusent le second avec une erreur fatale. Mieux un warning cosmétique partout qu'un build cassé sur certains systèmes.
- Unit systemd au niveau UTILISATEUR (systemd --user), et non de système (pourquoi, voir ci-dessous). Les fichiers s'appellent `debian/klamav-py.klamav-scan.user.service/.timer` : le segment `.user.` dans le nom est obligatoire pour que dh_installsystemduser les reconnaisse — découvert seulement en testant empiriquement un build réel. Ils finissent dans /usr/lib/systemd/user/.
- postinst/postrm utilisent deb-systemd-helper --user : le symlink d'activation va dans /etc/systemd/user/timers.target.wants/ (vaut pour tous les utilisateurs) et le timer démarre au prochain login de chacun, sans commandes manuelles.
- Une unit utilisateur ne peut pas s'ordonner de manière fiable par rapport à une unit de système comme clamav-daemon.service (managers systemd séparés, pas de dépendances cross-manager) : le .service n'a pas de After=/Requires= sur clamd. En pratique ce n'est pas un problème : clamd démarre bien avant le login utilisateur.

## Pourquoi des units utilisateur et non de système

La toute première version utilisait une unit de système avec DynamicUser=yes. Semblait propre (pas de useradd manuel), mais ne tient pas à l'usage réel :

- Les homes naissent 0700/0750 : un utilisateur dynamique ne lit rien, et la numérisation de /home ne produisait que des Permission denied en rafale.
- La quarantaine finissait dans /var/lib/klamav-py, propriété de l'utilisateur dynamique : la GUI ne pouvait ni la voir ni la gérer.

L'unit utilisateur tourne comme l'utilisateur, lit la home sans problème et utilise la même quarantaine par défaut que la GUI : les chemins coïncident.

### Mise à niveau depuis les versions précédentes (qui avaient la unit de système)

La suppression du fichier de l'ancienne unit n'arrête pas une instance déjà active — le symlink dans /etc/systemd/system/timers.target.wants/ n'est pas tracé par dpkg. C'est pourquoi postinst inclut une migration dédiée qui arrête et désactive explicitement l'ancienne unit de système avant d'activer la nouvelle utilisateur (vérifié en lisant le postinst du .deb compilé).

## Paquetisation Arch (AUR)

Le PKGBUILD est dans `arch/`, avec le script `klamav-py.install` et `test-local.sh`.

Le `source` pointe vers le tag GitHub de la release. Attention à un détail asymétrique : les tags du projet ont le préfixe `v` (`v0.1.6`) mais le répertoire à l'intérieur de l'archive non (`KlamAV-Py-0.1.6`), d'où le `v$pkgver` dans l'URL et le `$pkgver` dans les `cd` de `build()` et `package()`. Le fichier téléchargé est renommé en `$pkgname-$pkgver.tar.gz` pour ne pas entrer en collision dans `SRCDEST` avec des archives d'autres paquets.

`pkgver` doit être mis à jour à chaque release avec `klamav_py/__init__.py`, `debian/changelog` et `CHANGELOG.md` ; `tests/test_changelog.py` vérifie l'alignement et que les fichiers installés par `package()` existent vraiment. Le `sha256sums` doit être régénéré à chaque nouveau tag, avec `updpkgsums` sur une machine Arch ou à la main.

`arch/test-local.sh` permet de valider le paquet dans un container Arch sans avoir de machine Arch :

```sh
arch/test-local.sh --tag      # build depuis le tag GitHub, PKGBUILD non modifié
arch/test-local.sh            # build depuis l'arbre de travail courant
KLAMAV_TEST_GUI=1 arch/test-local.sh   # vérifie aussi le démarrage de la GUI
```

Le mode `--tag` est celui à utiliser avant de publier sur AUR : c'est le seul qui vérifie vraiment l'URL et le checksum, parce que les autres construisent depuis un tarball local et patchent le PKGBUILD à la volée.

## À revoir avant une release stable

- **Version :** `klamav_py/__init__.py` est la source de vérité. Depuis la 0.1.5 pyproject.toml la dérive de `klamav_py.__version__` (`[tool.setuptools.dynamic]`) au lieu de la redéclarer. Restent à aligner à la main `debian/changelog` (avec `dch`) et `CHANGELOG.md`, mais la cohérence est vérifiée par `tests/test_cli.py` et `tests/test_changelog.py`, qui tournent aussi en CI. Le désalignement n'était pas théorique, de deux manières différentes : la version dans pyproject.toml est restée figée à la 0.1.4 alors que les autres sources étaient déjà à la 0.1.5, et `CHANGELOG.md` est resté figé à la 0.1.3 pendant trois releases, parce qu'aucune étape du processus de release ne le touchait.
- **Changelog :** `CHANGELOG.md` est la chronologie pour les utilisateurs, indépendante de la distribution ; `debian/changelog` contient les métadonnées du paquet Debian ; `docs/CHANGELOG-archive.md` conserve le détail étendu jusqu'à la 0.1.3 et les passes d'audit. Après `dch`, `tools/changelog-stub.sh` génère le brouillon de la nouvelle entrée de `CHANGELOG.md` à partir de celle tout juste écrite dans `debian/changelog`, à raccourcir à la main.
- Aucun test automatique ne vérifie l'installation du .deb en soi (vérification manuelle) : si le projet grandit, vaut un job CI avec sbuild/pbuilder qui le reconstruit de zéro.

## Extensions naturelles

- **Circuit breaker sur les résultats :** si clamd meurt à mi-numérisation, chaque fichier restant produit une erreur « impossible d'ouvrir la session » (le mécanisme derrière un épisode observé de ~10.200 erreurs). Un compteur d'erreurs consécutives au-delà d'un seuil interromprait la numérisation avec un échec rapide et lisible au lieu du flood.
- **Exclusions configurables depuis l'UI** (aujourd'hui seulement via `--exclude` CLI) : caches, Trash, montages réseau — réduirait encore le bruit et accélérerait les numérisations home-wide.
- **Différencier le nom du QLocalServer par sys.prefix :** sur une machine de développement avec checkout+venv et .deb installés ensemble, l'installé et le checkout ne se disputeraient pas la même instance single-instance.
- **Notifications desktop natives** (notify-send ou D-Bus direct) en plus de QSystemTrayIcon.showMessage, pour les bureaux avec support tray irrégulier.
- **Export de l'index de quarantaine** dans un format lisible par un frontend web/TUI (ex. pour contrôler la quarantaine depuis le NAS).
- **Configurable depuis l'UI** si le temps réel doit mettre en quarantaine automatiquement ou seulement signaler.

## Vérification des releases

Depuis la 0.1.10 les tags des releases sont signés avec la clé du projet :

    gradia (KlamAV-Py release signing) <gradia@disroot.org>
    Empreinte : EBEE 3E80 EFA3 8B42 B147  F1B9 9D7A A4F1 971F EAA9

```bash
gpg --keyserver hkps://keys.openpgp.org --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
# ou, si keys.openpgp.org ne répond pas ou ne livre pas la clé :
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
git clone https://github.com/gradia64/KlamAV-Py.git && cd KlamAV-Py
git tag -v v0.1.10
```

Sans keyserver, la clé publique est aussi dans le dépôt :

```bash
gpg --import arch/klamav-py-release-key.asc
gpg --fingerprint EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
```

Dans tous les cas contrôle que l'empreinte rapportée par `gpg` coïncide avec
celle indiquée ci-dessus : le keyserver (ou le fichier dans le dépôt) garantit
seulement de livrer une clé, pas que ce soit la bonne. Indique toujours le
keyserver avec `--keyserver` : sans, `gpg` utilise celui configuré, qui peut ne
pas avoir la clé. Les tags précédents la 0.1.10 ne sont pas signés.

Depuis la 0.1.15 aussi les fichiers attachés à la release (`.deb`, paquet Arch,
source) ont une signature détachée `.sig`, plus `SHA256SUMS` signé. Avec la
clé importée comme ci-dessus :

```bash
gpg --verify SHA256SUMS.sig SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
gpg --verify klamav-py_0.1.15-1_all.deb.sig klamav-py_0.1.15-1_all.deb
```

Les pièces jointes sont signées par la CI avec une sous-clé dédiée,
`F9F4 2835 8660 2F8D D724  C467 0888 61B0 4D8D 328D` ; les tags les signe le
maintainer avec `FDC2 2208 6F32 BADB 0403  4BAF 707F 1CD9 C887 FD2E`.
`gpg` attribue les deux à la clé primaire indiquée ci-dessus. Si `gpg`
ne reconnaît pas la signature d'une pièce jointe, ta copie de la clé est
antérieure à la sous-clé de la CI : retélécharge-la depuis le keyserver ou le
dépôt comme ci-dessus.
