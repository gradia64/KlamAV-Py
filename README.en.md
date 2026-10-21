<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-Italiano-green?style=for-the-badge" alt="Italiano"></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/Lang-English-blue?style=for-the-badge" alt="English"></a>
  <a href="README.fr.md"><img src="https://img.shields.io/badge/Lang-Français-blue?style=for-the-badge" alt="Français"></a>
  <a href="README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-blue?style=for-the-badge" alt="Deutsch"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

# KlamAV-Py

**License:** GNU General Public License v3.0

Minimal frontend for ClamAV via clamd — CLI and GUI for Linux.

<p align="center">
  <img src="docs/screenshots/scan.png" alt="KlamAV-Py — scan" width="45%" />
  <img src="docs/screenshots/quarantine.png" alt="Quarantine" width="45%" />
</p>
<p align="center">
  <img src="docs/screenshots/history.png" alt="History" width="45%" />
  <img src="docs/screenshots/settings.png" alt="Settings" width="45%" />
</p>

Minimal Python rewrite of the idea behind KlamAV 0.22 (frontend to ClamAV), without the parts that became dead technology (Dazuko, DCOP, Qt3) and without the security problems of the original (shell injection via KShellProcess): no external executable is invoked for scanning, paths never pass through a shell.

## Features

- Scanning via clamd's native protocol (INSTREAM/IDSESSION over Unix socket), CLI and GUI.
- CLI with no external dependencies (standard library only), GUI in PySide6.
- Quarantine with neutralized permissions (0400), unpredictable on-disk name, restoration of original permissions, shared index protected by a lock between CLI/GUI/worker.
- Scheduled scans: internal GUI timer and/or user systemd units (via the .deb package), with persistent result log.
- Recursive Real-Time monitoring of configured folders (QFileSystemWatcher, with subfolder expansion, periodic reconciliation of lost watches and a configurable cap to avoid exhausting fs.inotify.max_user_watches).
- Pause/Resume of scans, "one scan at a time" guard.
- Dimensional pre-check: files above StreamMaxLength are reported "not verified" without wasting the clamd session.
- Acknowledgement of not-moved findings (heuristic signatures, mail archives): after verifying them they are no longer counted among infected, until the content changes.
- Dolphin context menu integration, autostart, system tray, single-instance with IPC.
- Application update check via GitHub Releases, manual or at startup.

## Requirements

- clamav-daemon installed and active (clamd), not just the clamscan/freshclam binaries: this project talks to the daemon over a socket, it does not invoke external executables for scanning.
- Python 3.10+ as the declared baseline. The actual technical minimum is 3.9 (Path.is_relative_to); the walrus operator used in clamd_client.py exists since 3.8. Tested locally on 3.14 (Debian Sid) and in CI on 3.10–3.14.
- CLI: no external runtime dependencies — runs even with the system Python, no venv.
- GUI: PySide6, must be installed in a dedicated venv (see below), not in the system Python.
- GUI, only for updating the virus database via the "Update Database" button: freshclam in PATH and pkexec (PolicyKit) available, since the operation requires root privileges.
- GUI, only for integration with Dolphin's context menu: a KDE Plasma environment with kbuildsycoca5/kbuildsycoca6.
- Development/tests: pytest (see requirements-dev.txt), not required at runtime.

## Installation

### From prebuilt release (recommended for daily use)

Download the .deb package from the Releases page and install:

```bash
sudo dpkg -i klamav-py_*.deb
sudo apt-get install -f    # fix missing dependencies, if needed
```

Installs klamav-py (CLI) and klamav-py-gui in /usr/bin/, automatically enables the user systemd timer for scheduled scanning and registers the application in the menus. Details and warnings in the ".deb packaging" section below.

### From AUR (Arch Linux and derivatives)

```sh
git clone https://aur.archlinux.org/klamav-py.git
cd klamav-py && makepkg -si
```

Or with an AUR helper: `yay -S klamav-py`, `paru -S klamav-py`.

The package also applies to Manjaro, EndeavourOS, CachyOS and the other derivatives that use AUR. `pyside6` and `clamav` are optional dependencies at the pacman level: the CLI works without the GUI, but for normal use both are needed.

Unlike the `.deb`, the scheduled scan timer is **not** enabled during installation: on Arch the policy is that system state is decided by the user. The package prints the instruction to run:

```sh
systemctl --user enable --now klamav-scan.timer
```

### From source (development or package build)

**Build the .deb:**

```bash
sudo apt install devscripts debhelper dh-python pybuild-plugin-pyproject \
    python3-all python3-setuptools
cd klamav-py
dpkg-buildpackage -us -uc -b
sudo dpkg -i ../klamav-py_<version>_all.deb
```

**Development environment (GUI from checkout):**

```bash
cd klamav-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

With Python 3.10 and 3.11, `requirements.txt` installs PySide6 6.11.x: wheels from 6.12 are built for Python 3.12 and on those versions abort the GUI after a few hundred signals (`Fatal Python error: bool_dealloc`; [PYSIDE-3474](https://qt-project.atlassian.net/browse/PYSIDE-3474)). In an existing venv just re-run `pip install -r requirements.txt`; to check: `python -c "import PySide6; print(PySide6.__version__)"` must say 6.11 or earlier.

With the venv active (or calling the venv interpreter directly):

```bash
python3 -m klamav_py.gui.app
```

The CLI instead does not need the venv and runs with the system Python.

Note for development: if the venv also contains an installed copy of the package (`pip install .` non-editable), the import of klamav_py may resolve there instead of in the checkout depending on the current directory. To avoid the trap use `pip install -e .`; and on a machine with both a checkout and an installed .deb, always verify which GUI instance is actually alive (`ps aux | grep klamav`): single-instance means the second launch is absorbed by the first existing instance, whatever it is.

## Quick usage

```bash
# CLI: check that clamd responds (works even without venv)
python3 -m klamav_py.cli ping

# CLI: scan with automatic quarantine
python3 -m klamav_py.cli scan /home/user/Downloads \
    --quarantine ~/.local/share/klamav-py/quarantine

# CLI: scan the home with exclusions and error log to file
python3 -m klamav_py.cli scan ~ \
    --exclude ~/.local/share/klamav-py \
    --exclude ~/.cache \
    --log-errors ~/klamav-errors.log

# GUI (requires active venv or venv interpreter, see above)
python3 -m klamav_py.gui.app

# or, with non-standard socket/quarantine:
python3 -m klamav_py.gui.app --socket /run/clamav/clamd.ctl \
    --quarantine-dir ~/.local/share/klamav-py/quarantine

# or to start scanning a path directly (also used by the
# Dolphin integration, see below):
python3 -m klamav_py.gui.app --scan-target /path/to/scan
```

The directory passed to `--quarantine` is always excluded automatically from traversal, both in CLI and GUI: already-managed files must not be re-detected (and re-quarantined) on every scan covering their location.

## CLI: scan options

- `--quarantine DIR` — move infected files to DIR (created with permissions 0700).
- `--exclude DIR` — directory to exclude from recursive traversal (repeatable). Excluded directories are not even read (pruning): useful for caches, Trash, synced network mounts.
- `--max-stream-size BYTES` — threshold for the dimensional pre-check (default 25MB, aligned with StreamMaxLength in clamd.conf; 0 disables the pre-check).
- `--log-errors FILE` — detail of every error (path and reason) to file. The file is created with permissions 0600; symlinks, FIFOs and files already existing owned by another user are refused (exit 2). Avoid shared directories like /tmp anyway: the log lists paths of your files.
- `--version` — print the version and exit (global option, not of the scan subcommand).
- `--no-persistent` / `--session-batch-size N` — control of the persistent session (see dedicated section).

Exit codes of `scan` — useful for OnFailure= in systemd or for monitoring scripts. The first case that applies wins, in this order:

| Order | Exit | Case |
| --- | --- | --- |
| 1 | 2 | Blocking error, scan does not start or is interrupted: clamd unreachable; path nonexistent, broken link, neither directory nor regular file, or unreadable; quarantine or `--log-errors` unusable |
| 2 | 1 | At least one infection found, even with I/O failures |
| 3 | 2 | At least one I/O failure without infections (see below) |
| 4 | 0 | Otherwise |

Permission denied, files gone during the scan, files too large and clamd errors on a single file do not change the code. A subfolder with permission denied does not change the code: it appears on stderr as `CARTELLA NON LEGGIBILE` and has a separate counter in the summary; if expected (folders created with sudo, container bind mounts) exclude it with `--exclude`. Another read error, on a file or a subfolder (EIO of a degrading disk, ESTALE or ETIMEDOUT of an unresponsive NFS mount), is a failure: it counts among errors and, without infections, the scan exits with 2 because part of the tree was not checked; the last line of the summary says so even with exit 1. In the GUI the outcome is "Completed with errors", and the same phrase appears in the status and in the manual scan report, in the notification and in the internal scheduling log; History has an "I/O failures" column.

## Unmoved findings and acknowledgement

Heuristic signatures (`Heuristics.*`) and files in mail archives are only reported, never moved automatically: moving a message breaks the mail client's archive. Such a finding, however, repeated on every scan, and with the system timer every night the same notification arrived, until it stopped being looked at.

After verifying the file, delete it or record the acknowledgement:

```bash
journalctl --user -u klamav-scan.service | grep INFETTO
klamav-py --acknowledge ~/.local/share/local-mail/trash/new/1695.R42.host
klamav-py --list-acknowledged        # hash prefix, signature, path, dates
klamav-py --unacknowledge 3f2a9c01be47   # by hash prefix or by path
```

`--acknowledge` rescans the file with clamd and records the acknowledgement only if it is still infected with a "report-only" finding. The rule is that of the scan, so it must be called with the same `--report-only` (and `--quarantine-all`) used in scanning, given first: `klamav-py --report-only ~/Archive --acknowledge ~/Archive/old.exe`. Subsequent scans print `GIÀ VALUTATO` and do not count it among infected: a **new** finding still exits with 1 and notifies. The key is the SHA-256 of the content plus the signature, not the path: a message moving from `new/` to `cur/` stays acknowledged, changed content is reported again, and an acknowledgement never hides a finding that would be quarantined. The acknowledgement thus applies to content and signature, in any path: an identical copy elsewhere, with the same signature, is already acknowledged. Entries not found by any scan for 90 days are removed. Registry in `~/.local/share/klamav-py/acknowledged.json` (0600), shared with the GUI, where the **Findings** page offers the same actions (and deletion, only if the file is still the one detected; for a maildir managed by Akonadi, `akonadictl fsck` may then be needed). The page shows the findings of the current session of the window (manual, internal scheduled, Real-Time); those of the system timer are in the journal and are managed with the commands above.

## Persistent session, dimensional pre-check, reconstruction

By default `scan_stream` reuses a single connection (IDSESSION session) for several files in a row instead of opening a new one for each — on trees with tens of thousands of files this avoids repeated connect overhead. The session is still closed and reopened every 500 files (`--session-batch-size`) to limit the impact of undocumented limits on very long sessions, and recreated from scratch whenever something goes wrong: a problematic file does not compromise the rest of the scan.

When clamd closes the connection (typical case: rejection for StreamMaxLength), the session is marked dead and recreated before the next file, so subsequent files do not die in cascade on an already-closed connection.

Dimensional pre-check: files above the threshold (default 25MB, like StreamMaxLength) are not sent to clamd at all — they exit immediately with status TOO_LARGE ("not verified"), distinct from real errors in both CLI and GUI. Without the pre-check, clamd rejects the stream halfway through sending, closing the connection: the error the user would see would be a "broken pipe" on a file that is actually fine (see "Diagnosing common errors").

The pre-check has a known and accepted race — a file that grows above threshold between the check and the send is still handled by the fallback that classifies stream errors on above-threshold files as TOO_LARGE.

Non-regular entries (since 0.1.5): symlinks, sockets, FIFOs and devices are discarded during traversal and not sent to clamd.

`os.walk()` classifies as "file" everything that is not a directory, so they ended up in the list to scan with two consequences: dangling symlinks (very frequent in node_modules and in cleaned pnpm stores) produced hundreds of false ERROR "[Errno 2] No such file or directory", and `open()` on a FIFO without a writer BLOCKS indefinitely in the kernel — no exception, no timeout: a single FIFO in the tree hung the whole scan.

They are not coverage holes: a symlink has no content of its own (the target, if in the tree, is scanned as a real file), and sockets, FIFOs and devices have no persistent infectable content. That is why they do not appear in results and do not enter the invariant `scanned = clean + infected + errors + too_large`; they are counted separately and shown in the CLI summary. TOO_LARGE and permission denied remain visible precisely because they are real holes.

Opening uses `O_NOFOLLOW|O_NONBLOCK` with an `fstat()` check on the already-open descriptor: the type check concerns exactly the object that will be read, with no TOCTOU window relative to the path.

Timeout (since 0.1.5): waiting for clamd's verdict has its own timeout (120s), distinct from connection and send (30s). These are different things: sending bytes is fast, but analyzing an archive or a PDF with many nested objects can take much longer. With a single low value those files came out as "clamd session interrupted: timed out" despite being perfectly readable.

Timeouts earn a second attempt on a clean session (only timeouts: a broken pipe would mean rejection for StreamMaxLength and retrying would be waste); a file not verified even on the second attempt gets a distinct message, because that one really is a coverage hole. The value must be kept consistent with CommandReadTimeout/ReadTimeout in clamd.conf: if clamd gives up first, lengthening the client timeout is useless.

If you suspect a problem related to the persistent session, `--no-persistent` returns to the "one connection per file" behavior to isolate the case.

At the end of a scan the CLI prints a summary of errors grouped by type (e.g. "2 Permission denied", "1 Broken pipe"), instead of having to do manual grep/sort/uniq -c.

## GUI: interface overview

PySide6 single-window application with sidebar and a style inspired by KDE Plasma (always uses system palette colors, never hardcoded colors, except for semantic states like "infected"). Eight sections:

- **Scan** — manual scan on a chosen path, in a separate QThread (ScanWorker): the window stays responsive even on large directories, and the status bar shows the file being processed in real time (with text elision to keep the layout from growing on long paths). Counters distinguish scanned / infected / errors / not verified (too large), and only infected/errors/not-verified produce rows in the list.
- **Pause / Resume.** Pause has whole-file granularity: the streaming file completes, then the worker stops (the button follows the worker's signals, not the click, so the shown state is always the actual one). "Stop" stays active even while paused. After a pause longer than ~25s (close to clamd's IdleTimeout, 30s by default) the session is proactively recreated on resume, to avoid a fake error on the first file. With the window minimized to tray, the icon tooltip shows the counters of the running scan and the pause state.
- **Manual quarantine by default.** The "Automatically quarantine infected files" checkbox is disabled by default: reported files stay where they are and you move them with "Quarantine selected" — handy to evaluate a false positive before moving anything. "Copy log" copies all rows of the list (infected, errors, not verified) to the clipboard.
- **History** — persistent log (~/.local/share/klamav-py/history.json, last 1000 entries; directory is 0700 and files 0600, because they list scanned paths and infected files) with date/time, type, path, scanned, infected, errors, not verified, unreadable folders, already acknowledged and I/O failures (a subset of errors), plus the reason for an incomplete or deferred scan. Scheduled scans also index the detailed log on disk (tooltip on the row): the infected/error detail of a background scan, which passes through no UI list, thus stays always inspectable.
- **Quarantine** — reads the same JSON index used by the CLI (index.json in the quarantine directory): the two tools are interchangeable on the same data. Restore to original location (with restoration of original permissions, explicit refusal if the path has meanwhile been reoccupied) or permanent deletion.
- **Findings** — files detected but left in place (heuristic signatures, mail archives) in the session's scans, with "I have verified, do not report again" (acknowledgement, see "Unmoved findings and acknowledgement") and "Delete…" with confirmation; below, the recorded acknowledgements, revocable.
- **Updates** — downloads virus definitions by running `freshclam --stdout` via pkexec (PolicyKit authentication), stopping the system clamav-freshclam/freshclam daemon to avoid lock conflicts on the log file and restarting it at the end. Since 0.1.7 the restart also happens if the update is interrupted (logout, signal), and concerns only services that were active before: a daemon stopped on purpose stays stopped. Output shown in real time. Choosing "Quit" during the update, the app closes as soon as it finishes instead of interrupting it. By default it starts automatically 1.5s after app startup (disable in Settings).
- **Real-Time** — log of automatic scans on monitored folders (configurable in Settings). Uses QFileSystemWatcher: creation/modification of a file → scan after a 3s debounce (to avoid scanning files still being written), and infected files are always quarantined automatically. Files too large are labelled "Not verified", not "Safe" (see design choices).
- **Scheduling** — recurring scan (every N hours or days) on a chosen folder, via internal QTimer: requires the app running (even minimized to tray). During execution the page shows status and live counters; the tray tooltip reflects progress; at the end of the scan the detailed log is written to ~/.local/share/klamav-py/logs/scheduled-<timestamp>.log (rotation: last 10 runs) and referenced in History. It is not a system timer like the one in the .deb package, which works even with the GUI closed (see "Scheduled scans: the two mechanisms").
- **Settings** — clamd socket, quarantine directory, folders monitored for Real-Time (with indicator "Real-Time active on N folders (M subfolders included, recursive)"), autostart at login, start minimized to tray, DB update at startup, application update check (button and option at startup), Dolphin integration.

## Real-Time monitoring: how it works (0.1.5)

QFileSystemWatcher is not recursive: it watches exactly the folders passed to it. Until 0.1.4 this meant files created in SUBFOLDERS of a monitored folder generated no event at all — the interface declared Real-Time active on a folder while staying blind to everything underneath.

Since 0.1.5 the configured folders are expanded recursively (symlinks not followed, hidden folders skipped because they are mostly caches generating events continuously). Three paths keep coverage up to date:

- subfolders created after startup: attached immediately, on receiving the event on the folder containing them;
- trees moved in with mv or extracted from an archive: mv generates a single event at the top level, so subfolders that arrive already full are recovered by periodic reconciliation (up to 60s latency);
- lost watches (folder deleted and recreated): same reconciliation.

Files already present in a folder discovered AFTER startup are analyzed, not baselined: this is the scenario of an archive extracted elsewhere and then moved in, where otherwise the content would never be scanned. At application startup the behavior is the opposite and intended: files already present become the reference state without being scanned, otherwise every restart would rescan the entire monitored folder.

Every watched folder consumes an inotify watch, and fs.inotify.max_user_watches is shared with ALL of the user's processes (IDEs, cloud syncers, indexers). That is why there is a cap (MAX_WATCH_DIRS, default 8000): check your own limit with `cat /proc/sys/fs/inotify/max_user_watches` and tune accordingly.

Both reaching the cap and addPaths() failures are reported explicitly in the status indicator — a Real-Time that declares itself active while blind on half the folders is worse than one that is off.

The program version is visible in the window title, in the resting tooltip of the system tray and in the Settings panel.

All settings are saved with QSettings (organization "KlamAV-Py", application "KlamAV-Py" — on Linux typically ~/.config/KlamAV-Py/KlamAV-Py.conf). Settings from versions earlier than 0.1.3-2 (file ~/.config/KlamAV/KlamAV.conf) are migrated automatically on first startup; the old file stays on disk.

## Intentional design choices

Manual quarantine in Scan, automatic in Real-Time. Not an inconsistency: folders monitored in Real-Time are typically destinations of files just downloaded/received from outside (e.g. ~/Downloads) — the risk of a false positive on a new, never-touched file is low, so acting immediately makes sense. The Scan page can be pointed at folders with important data, where a false positive should be verified before moving anything.

"One scan at a time" guard between manual and scheduled. A manual scan does not start if a scheduled one is running (and vice versa the scheduled one skips, with an explicit entry in History and a notification, if a manual one is active). Reasons: contention on clamd (two home-wide traversals in parallel double the daemon's I/O and memory) and double load on the filesystem. Real-Time is deliberately exempt from the guard: it is the primary protection, acts on single files (seconds), and clamd handles concurrent connections by design — suspending it while a manual scan runs would be worse. A paused scan counts as "in progress" for the guard: it blocks scheduling until it resumes or is stopped.

Exclusion of the quarantine directory. Quarantined files are data managed by the application: they are excluded from traversal (pruning, not even read) and, as defense in depth, quarantine_file() explicitly refuses to re-quarantine a file already inside the quarantine directory. Without these guards, a home-wide scan would detect old EICARs in quarantine on every cycle, inflating infected and errors forever with "ghosts" already managed.

## System tray and single-instance

The app stays active in the system tray even when closing the window (the X hides it, does not terminate it: you exit from the tray menu or "Quit"). It is meant to stay in the background for scheduling and Real-Time. On exit the running threads (scans, clamd ping, update check) are stopped and waited for up to 3 seconds; those blocked on I/O beyond that limit do not prevent closing.

It is also single-instance: if you launch the GUI while another instance is already active, the second sends its own `--scan-target` (if present) to the first and exits immediately, instead of opening a second window. This is the mechanism that makes the Dolphin integration sensible: clicking "Scan with KlamAV-Py" on several files/folders, all requests end up in the already-open window.

The IPC socket is in the user's runtime directory (`$XDG_RUNTIME_DIR/klamav-py-ipc`, typically `/run/user/<uid>/`), not in `/tmp`, and before sending any data the second instance verifies with `SO_PEERCRED` that the listening process belongs to the same user. If no safe runtime directory exists, or the IPC server fails to start, the GUI starts anyway without single-instance and reports it: a second launch would open another copy.

Upgrading from 0.1.6 or earlier, an old instance still open uses the old socket and is not recognized by the new one: close it from the tray before restarting the application.

## Dolphin integration

From the Settings panel you can install the "Scan with KlamAV-Py" entry in Dolphin's context menu, by writing a .desktop file in ~/.local/share/kservices5/ServiceMenus/ and ~/.local/share/kio/servicemenus/ and regenerating the KDE service cache (kbuildsycoca5/6). The Exec= invokes the command directly without an intermediate shell: a file name with ", ` or $ cannot break the quoting.

## Autostart

If enabled, writes a .desktop file in ~/.config/autostart/ that launches the GUI with the Python interpreter currently in use; if disabled, removes the file.

## Application update check

The "Check for updates" button in Settings queries the project's GitHub releases API and compares the latest published version with the one in use. The "Check for updates at startup" option is disabled by default: without it the application does not contact GitHub except on explicit request. If a new version exists a link to the release page appears (and a tray notification, for the startup check); installation remains manual, via .deb or AUR.

The startup check runs at most once every six hours: the unauthenticated GitHub API grants 60 requests per hour per IP address, and restarting the application often must not consume them. The button in Settings is not subject to the limit.

GitHub's response is treated as untrusted data: it is read up to a maximum of 256 KB, version and notes are escaped before being shown, and the link is made clickable only if it points to the official repository's releases.

## Scheduled scans: the two mechanisms

- **GUI timer (Scheduling page):** runs only with the app active (even in tray), but offers live progress, persistent log and full UI integration.
- **User systemd unit (installed by the .deb and the Arch package):** starts at each user's login without configuration, works with the GUI closed. To run the timer even with the user logged out:
  `loginctl enable-linger $USER`. Uses the same default quarantine path as the GUI (~/.local/share/klamav-py/quarantine). Errors and unreadable folders of the last scan, one per line, are also in `~/.local/state/log/klamav-py/scan-errors.log` (`$XDG_STATE_HOME/log/klamav-py`, the unit's `LogsDirectory=`), besides the journal.
  Quarantine, clamd connection and folders excluded chosen in the GUI reach the timer via a drop-in, `~/.config/systemd/user/klamav-scan.service.d/50-klamav-py.conf`, recognized by the first line `# Generated by KlamAV-Py: do not edit by hand.`. Written by Settings and Scheduling saves; on startup the GUI also rewrites a drop-in of its own left at an older version (for example without `--log-errors`) and says so on the Scheduling page. A file with that name without that first line, i.e. written by hand, is never touched. To see the command the timer will use: `systemctl --user cat klamav-scan.service`.

A system-level systemd unit is no longer distributed (since 0.1.9): it could not read private homes without widening their permissions, while the user unit runs as the user and reads their home as-is.

## Diagnosing common errors

On a home-wide scan of a real development machine (330k+ files) the residual errors are physiological and recognizable:

- **"Broken pipe" on large files** — clamd rejected the stream for StreamMaxLength (25MB by default in clamd.conf), closing the connection halfway through sending. With the dimensional pre-check active it has almost disappeared: files above threshold exit as "not verified" without touching the session. To raise the limit:
  `StreamMaxLength 100M` in clamd.conf and restart clamd — with the trade-off that a higher limit makes it easier to flood clamd with huge streams.

  Honesty note: even files within the stream limit remain subject to clamd's MaxScanSize/MaxFileSize/MaxRecursion (approximate defaults 100M/25M/16): a bulky archive may come out "OK" with a partial scan — a limit of clamd itself, not detectable by this project.

- **Errno 13 "Permission denied"** — real filesystem permissions (e.g. files owned by Docker containers inside the home). Not a problem of the project.

- **Errno 2 "No such file or directory"** — since 0.1.5 it should be rare. Until 0.1.4 the dominant cause was NOT the one documented here (files gone during traversal): they were dangling symlinks, which os.walk() classifies as files and open() cannot open. On trees with node_modules or cleaned pnpm stores they were hundreds of lines hiding the real errors. Now symlinks are discarded upstream. What remains is the originally hypothesized cause — files genuinely gone between traversal and reading (Trash emptied, cloud sync, browser rewriting cache) — and in that form it is expected noise.

- **Errno 6 "No such device or address"** — AF_UNIX sockets (GPG agent, MEGAsync, browser native messaging hosts). As above: discarded upstream since 0.1.5, should no longer appear.

- **"timed out"** — here too the explanation until 0.1.4 was incomplete. Not only network mount latency: the single 30s timeout covered both sending AND waiting for the verdict, and clamd can take much longer on archives or PDFs with many nested objects. Perfectly readable local files came out as "clamd session interrupted: timed out" reproducibly. Since 0.1.5 waiting has its own timeout (120s) and timeouts get a second attempt. If you still see them, check CommandReadTimeout/ReadTimeout in clamd.conf before suspecting the filesystem.

- **Bursts of errors of a single type (thousands)** — almost certainly clamd dead or restarted mid-scan:
  `journalctl -u clamav-daemon.service` over the scan interval, and `dmesg | grep -i oom` for the OOM case. The CLI's per-type summary (or the persistent log of the scheduled scan) makes this case immediately distinguishable from physiological noise.

## Security fixes in 0.1.8

- **Application data readable by other local users.** History, scheduled scan logs and CLI error logs were created with default umask permissions (directory 0755, files 0644): they list scanned file paths, signature names and infected files. On a system with 0755 home — historical default, still frequent on updated installations — they were readable by another local user. Now the data directory is 0700 (tightened at startup, so even files from previous versions become inaccessible) and files are 0600. The `--log-errors` log was the worst case, because the README suggested using it in /tmp, where another user could pre-create it to read its contents.

- **Configuration file at 0644.** `~/.config/KlamAV-Py/KlamAV-Py.conf` lists the folders monitored by Real-Time and the scan and quarantine paths, but it is written by Qt and escaped the treatment of the other data. It is brought to 0600 at startup, along with the legacy 0.1.3 config file if still present.

- **Unbounded clamd responses.** Responses were accumulated in memory until the socket provided them: a remote clamd over TCP, or a socket at a user-configurable path, could exhaust RAM. Reading now stops at 1 MiB. Does not concern the system clamd, which is root.

## Security fixes in 0.1.7

- **Quarantine bypass via hardlink + symlink.** The inode identity check after the move (introduced in 0.1.4, see below) used `os.stat()`, which follows symlinks. An infected file could create a hardlink elsewhere and replace its own path with a symlink to that hardlink: the move quarantined only the link, `os.stat()` found the original inode and the check passed. The infected content stayed outside and executable, the index recorded a successful quarantine and "Delete permanently" removed only the link. The check now uses `os.lstat()` and permissions are applied with `fchmod()` on the already-verified descriptor. Requires winning a race by a process of the same user, but is repeatable in a loop.

- **Single-instance IPC socket in /tmp.** Until 0.1.6 the socket was `/tmp/klamav_py_ipc`, in a directory shared by all users. `UserAccessOption` prevented others from connecting to the application's socket, but did not prevent the application from connecting to someone else's socket: a second local user could occupy the name first, receive the paths sent for scanning and, by answering the connection, make every startup believe an instance was already active, preventing the GUI from starting at all (no Real-Time, no scheduled scans). Now the socket is in the user's runtime directory, the client verifies the peer with `SO_PEERCRED` and `listen()` failure is reported. Not exploitable on a desktop with a single user.

## Security fixes in 0.1.4

This version resolves a series of security issues found in a code audit. The reference threat model is a multi-user system and, for quarantine, the infected file itself treated as potentially hostile content (not an external attacker).

- **TOCTOU in quarantine and restore.** `quarantine_file()` opens the file with O_NOFOLLOW and verifies inode identity (dev+ino) after the move, instead of trusting a separate is_file() check before the move operation: an infected file could try to evade isolation by replacing itself with a symlink in the window between check and move. Symlinks (and special files like FIFOs) are now refused explicitly. `restore()` claims the destination atomically with O_CREAT|O_EXCL instead of an exists() followed by move(), eliminating the window in which another process could create the destination file in between.

- **Single-instance IPC socket restricted and validated.** The QLocalServer uses UserAccessOption: without it, on Linux socket permissions depend on the process umask and could be accessible to other system users. The path received over the socket is now size-limited (avoids huge payloads intended as DoS) and decoded robustly (a malformed UTF-8 payload is ignored, does not propagate exceptions). Since 0.1.7 the socket is no longer in `/tmp`: see the previous section.

- **Database update delegated to the system service.** The "Update Database" button only runs `pkexec /usr/bin/systemctl restart clamav-freshclam.service`, with fixed argv and verified root-owned binaries: freshclam downloads signatures as the `clamav` user, with the distribution unit's sandboxing, instead of running as root. The outcome is verified from the DB version reported by clamd; at startup the update runs only if signatures are older than 36 hours.

- **KDE service cache regeneration without a shell.** `os.system()` was replaced with `subprocess.run()` with explicit argv (no shell).

- **Permissions of .desktop files for Dolphin integration changed from 0755 to 0644.**

  **WARNING** — this change was WRONG and was reverted in 0.1.5. The reasoning (the .desktop are config files, the freedesktop spec does not want the execute bit) applies to launchers in /usr/share/applications, NOT to KIO servicemenus: since KFrameworks 5.85 these must be executable, and with 0644 Dolphin replies "You are not authorized to execute this file" and the menu entry stops working. The two destinations have opposite requirements: the launcher stays 0644, the servicemenu goes back to 0755. Left documented here because the justification looks correct read on its own and is exactly how the regression can be reintroduced.

The fixes are covered by 9 new automated tests (refusal of symlinks and FIFOs in quarantine, verification of atomic claiming of the destination on restore, validation of the IPC payload).

## Note on 0.1.4-2 (correctness, not security)

The root of every scan is now resolved (`Path.resolve()`) at the start of `scan_stream`, in a single point common to all ways of starting a scan (CLI, manual GUI, Dolphin/IPC integration, and prospectively Real-Time and scheduled). It is not a security fix: in the IPC case the socket is already restricted to the same user, so there is no privilege boundary to protect and a symlink gives access to nothing the user cannot already read. It is a correctness fix: if the path to scan is a symlink-directory, `os.walk()` follows it anyway when it is the starting point (followlinks=False blocks only symlinks inside the tree, not the root), and without resolving it the scan report would show results under the symlink path instead of under the real one actually read. For an antivirus the real content checked matters, not the alias used to reach it.

Resolution happens once on the root, not per file: `_iter_files` (the actual traversal) is unchanged, so both constant-memory streaming and pruning of excluded directories (quarantine included) keep working exactly as before. Symlinks inside the tree continue not to be followed.

## What's missing vs original KlamAV (by design)

- **No kernel-level on-access scanning.** Real-Time monitoring is based on QFileSystemWatcher (recursive since 0.1.5, with a few seconds of latency and a cap on the number of watched folders) — it is not a substitute for a real on-access scanner. For that ClamAV already provides clamonacc (fanotify-based): reimplementing a Dazuko equivalent makes no sense. If real on-access is needed, configure it via clamd.conf/clamav-clamonacc.service.
- **No client-side mail integration.** If mail scanning is needed, today it makes more sense server-side (milter) than client-side KMail/Evolution as klammail did.

## Development and tests

```bash
cd klamav-py
python3 -m venv venv        # if you haven't created it yet
source venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

Tests cover pure logic without a real clamd (clamd protocol parsing, quarantine handling on a temporary filesystem, CLI) and the GUI with offscreen instantiation of pages and simulation of signals/actions (Scan↔Quarantine resume, status elision, clipboard, pause/resume with a fake client injected via client_factory).

On distributions with "externally managed" Python (recent Debian/Ubuntu, PEP 668), `pip install -r requirements-dev.txt` outside a venv fails with externally-managed-environment: expected system behavior, use the venv.

## License

KlamAV-Py is released under the GPL-3.0-or-later license (see the LICENSE file). Written from scratch: contains no code from KlamAV 0.22, of which it is only a spiritual heir of the idea.

## .deb packaging

The Debian skeleton is in debian/, tested with a real build (dpkg-buildpackage) and complete install/removal (dpkg -i / apt-get remove --purge).

Verify before installing: the skeleton was validated in an Ubuntu 24.04 environment, where python3-pyside6.qtcore and friends do not exist in the repositories (Ubuntu packages only PySide2). On Debian they should exist, but before installing the .deb check with `apt-cache search pyside6` that the packages listed in debian/control (python3-pyside6.qtcore, .qtgui, .qtwidgets, .qtnetwork) exist in your version, and correct the names if they differ.

### What the Debian skeleton does

- A single binary package klamav-py with CLI and GUI (no split, while the project is young).

- `pyproject.toml` defines two entry points in /usr/bin/: klamav-py (CLI) and klamav-py-gui (GUI). The GUI uses them to auto-detect the command to write in the autostart and Dolphin .desktop files (`_gui_relaunch_command()`), instead of assuming a local checkout with venv.
- The license field deliberately uses the old table format (`{text = "..."}`) instead of the SPDX string: recent setuptools deprecate the former with a warning, but old setuptools (verified: 68.1.2, Ubuntu 24.04) reject the latter with a fatal error. Better a cosmetic warning everywhere than a broken build on some systems.
- Systemd units at USER level (systemd --user), not system (because, see below). The files are named `debian/klamav-py.klamav-scan.user.service/.timer`: the `.user.` segment in the name is mandatory for dh_installsystemduser to recognize them — discovered only by empirically testing a real build. They end up in /usr/lib/systemd/user/.
- postinst/postrm use deb-systemd-helper --user: the enablement symlink goes in /etc/systemd/user/timers.target.wants/ (applies to all users) and the timer starts at each user's next login, without manual commands.
- A user unit cannot be reliably ordered relative to a system unit like clamav-daemon.service (separate systemd managers, no cross-manager dependencies): the .service has no After=/Requires= on clamd. In practice this is not a problem: clamd starts well before user login.

## Why user units and not system

The very first version used a system unit with DynamicUser=yes. It looked clean (no manual useradd), but does not hold up to real use:

- Homes are born 0700/0750: a dynamic user reads nothing, and scanning /home produced only Permission denied in bursts.
- Quarantine ended up in /var/lib/klamav-py, owned by the dynamic user: the GUI could neither see nor manage it.

The user unit runs as the user, reads the home without problems and uses the same default quarantine as the GUI: the paths coincide.

### Upgrade from previous versions (which had the system unit)

Removing the old unit's file does not stop an already-running instance — the symlink in /etc/systemd/system/timers.target.wants/ is not tracked by dpkg. That is why postinst includes a dedicated migration that stops and disables the old system unit explicitly before enabling the new user one (verified by reading the postinst of the built .deb).

## Arch packaging (AUR)

The PKGBUILD is in `arch/`, together with the `klamav-py.install` script and `test-local.sh`.

The `source` points to the GitHub release tag. Watch out for an asymmetric detail: project tags have the `v` prefix (`v0.1.6`) but the directory inside the archive does not (`KlamAV-Py-0.1.6`), hence `v$pkgver` in the URL and `$pkgver` in the `cd` of `build()` and `package()`. The downloaded file is renamed to `$pkgname-$pkgver.tar.gz` to avoid colliding in `SRCDEST` with archives of other packages.

`pkgver` must be updated at every release together with `klamav_py/__init__.py`, `debian/changelog` and `CHANGELOG.md`; `tests/test_changelog.py` verifies alignment and that files installed by `package()` actually exist. `sha256sums` must be regenerated at every new tag, with `updpkgsums` on an Arch machine or by hand.

`arch/test-local.sh` allows validating the package in an Arch container without an Arch machine:

```sh
arch/test-local.sh --tag      # build from the GitHub tag, PKGBUILD unmodified
arch/test-local.sh            # build from the current working tree
KLAMAV_TEST_GUI=1 arch/test-local.sh   # also verify GUI startup
```

The `--tag` mode is the one to use before publishing to AUR: it is the only one that truly verifies URL and checksum, because the others build from a local tarball and patch the PKGBUILD on the fly.

## To review before a stable release

- **Version:** `klamav_py/__init__.py` is the source of truth. Since 0.1.5 pyproject.toml derives it from `klamav_py.__version__` (`[tool.setuptools.dynamic]`) instead of redeclaring it. Still to be aligned by hand: `debian/changelog` (with `dch`) and `CHANGELOG.md`, but consistency is verified by `tests/test_cli.py` and `tests/test_changelog.py`, which also run in CI. The mismatch was not theoretical, in two different ways: the version in pyproject.toml stayed stuck at 0.1.4 while the other sources were already at 0.1.5, and `CHANGELOG.md` stayed stuck at 0.1.3 for three releases, because no release step touched it.
- **Changelog:** `CHANGELOG.md` is the user-facing history, independent of the distribution; `debian/changelog` contains Debian package metadata; `docs/CHANGELOG-archive.md` keeps the extended detail up to 0.1.3 and the audit rounds. After `dch`, `tools/changelog-stub.sh` generates the draft of the new `CHANGELOG.md` entry from the one just written in `debian/changelog`, to be shortened by hand.
- No automated test verifies the installation of the .deb itself (manual verification): if the project grows, a CI job with sbuild/pbuilder that rebuilds it from scratch is worth it.

## Natural extensions

- **Circuit breaker on results:** if clamd dies mid-scan, every remaining file produces an "unable to open session" error (the mechanism behind an observed episode of ~10,200 errors). A counter of consecutive errors above threshold would abort the scan with a fast, readable failure instead of the flood.
- **UI-configurable exclusions** (today only via `--exclude` CLI): caches, Trash, network mounts — would further reduce noise and speed up home-wide scans.
- **Differentiate the QLocalServer name by sys.prefix:** on a dev machine with checkout+venv and an installed .deb side by side, installed and checkout would not contend the same single-instance.
- **Native desktop notifications** (notify-send or direct D-Bus) besides QSystemTrayIcon.showMessage, for desktops with irregular tray support.
- **Export of the quarantine index** in a format readable by a web/TUI frontend (e.g. to inspect quarantine from the NAS).
- **UI-configurable** whether Real-Time should quarantine automatically or only report.

## Release verification

Since 0.1.10 release tags are signed with the project key:

    gradia (KlamAV-Py release signing) <gradia@disroot.org>
    Fingerprint: EBEE 3E80 EFA3 8B42 B147  F1B9 9D7A A4F1 971F EAA9

```bash
gpg --keyserver hkps://keys.openpgp.org --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
# or, if keys.openpgp.org does not respond or does not deliver the key:
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
git clone https://github.com/gradia64/KlamAV-Py.git && cd KlamAV-Py
git tag -v v0.1.10
```

Without a keyserver, the public key is also in the repository:

```bash
gpg --import arch/klamav-py-release-key.asc
gpg --fingerprint EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
```

In any case check that the fingerprint reported by `gpg` matches the one shown above: the keyserver (or the file in the repository) only guarantees delivering a key, not that it is the right one. Always specify the keyserver with `--keyserver`: without it, `gpg` uses the configured one, which may not have the key. Tags before 0.1.10 are not signed.

Since 0.1.15 the files attached to the release (`.deb`, Arch package, source) also have a detached `.sig` signature, plus a signed `SHA256SUMS`. With the key imported as above:

```bash
gpg --verify SHA256SUMS.sig SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
gpg --verify klamav-py_0.1.15-1_all.deb.sig klamav-py_0.1.15-1_all.deb
```

The attachments are signed by CI with a dedicated subkey, `F9F4 2835 8660 2F8D D724  C467 0888 61B0 4D8D 328D`; tags are signed by the maintainer with `FDC2 2208 6F32 BADB 0403  4BAF 707F 1CD9 C887 FD2E`. `gpg` attributes both to the primary key shown above. If `gpg` does not recognize an attachment's signature, your copy of the key predates the CI subkey: re-download it from the keyserver or the repository as above.
