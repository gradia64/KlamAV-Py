"""Scansione programmata della GUI: scadenza su orologio reale e recupero."""
from __future__ import annotations

import pytest

from klamav_py import schedule as sched

H = 3600.0


def test_intervalli():
    assert sched.interval_seconds(24, "Ore") == 24 * H
    assert sched.interval_seconds(2, "Giorni") == 48 * H
    # Nessun tetto a ~24,8 giorni (era il limite di QTimer).
    assert sched.interval_seconds(30, "Giorni") == 30 * 24 * H
    assert sched.interval_seconds(0, "Ore") == H


def test_scadenza_sopravvive_al_riavvio():
    """Il caso che non funzionava: GUI riavviata ogni giorno, intervallo 24h."""
    last = 1_000_000.0
    # Il giorno dopo, a un nuovo avvio: la scadenza è calcolata dall'ultima
    # esecuzione salvata, non dall'avvio della GUI.
    assert sched.is_due(last + 25 * H, last, 24 * H)
    assert not sched.is_due(last + 23 * H, last, 24 * H)


def test_senza_base_non_scansiona():
    assert not sched.is_due(1_000_000.0, None, H)


def test_orologio_spostato_indietro():
    now = 1_000_000.0
    # Ultima esecuzione "nel futuro": non deve bloccare per sempre.
    assert sched.normalized_last_run(now + 100 * H, now) == now
    assert sched.is_due(now + 25 * H, now + 100 * H, 24 * H) is False
    assert sched.is_due(now + 24 * H, sched.normalized_last_run(now + 100 * H, now), 24 * H)


def test_descrizione():
    last = 1_000_000.0
    assert "in ritardo" in sched.describe_next(last + 30 * H, last, 24 * H)
    assert sched.describe_next(last + H, last, 24 * H).startswith("Prossima scansione programmata:")
    assert sched.describe_next(last, None, H) == ""


# --- _check_schedule su un oggetto minimo ------------------------------------------

pytest.importorskip("PySide6")
from klamav_py.gui import main_window as mw  # noqa: E402


class _Settings:
    def __init__(self, **values):
        self.values = values

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


class _Tray:
    def __init__(self):
        self.messages = []

    def showMessage(self, title, text, *a):
        self.messages.append(text)


class _Page:
    def __init__(self):
        self.next_run = None

    def set_next_run(self, text):
        self.next_run = text


class _Window:
    _check_schedule = mw.MainWindow._check_schedule
    _schedule_interval_s = mw.MainWindow._schedule_interval_s
    _schedule_last_run = mw.MainWindow._schedule_last_run
    _update_next_run_label = mw.MainWindow._update_next_run_label

    def __init__(self, last_run, grace_left=0.0):
        self.settings = _Settings(schedule_enabled=True, schedule_interval=24,
                                  schedule_unit="Ore", schedule_last_run=last_run)
        self.tray_icon = _Tray()
        self.scheduler_page = _Page()
        self._schedule_grace_until = mw.time.monotonic() + grace_left
        self._schedule_late_noted = False
        self.runs = 0

    def _run_scheduled_scan(self):
        self.runs += 1


def test_in_ritardo_durante_la_grazia_avvisa_una_volta():
    w = _Window(last_run=mw.time.time() - 30 * H, grace_left=300)
    w._check_schedule()
    w._check_schedule()
    assert w.runs == 0
    assert len(w.tray_icon.messages) == 1 and "in ritardo" in w.tray_icon.messages[0]


def test_in_ritardo_dopo_la_grazia_parte():
    w = _Window(last_run=mw.time.time() - 30 * H)
    w._check_schedule()
    assert w.runs == 1


def test_non_scaduta_non_parte():
    w = _Window(last_run=mw.time.time() - H)
    w._check_schedule()
    assert w.runs == 0 and not w.tray_icon.messages


def test_last_run_illeggibile_trattato_come_assente():
    w = _Window(last_run="non-un-numero")
    assert w._schedule_last_run() is None
    w._check_schedule()
    assert w.runs == 0
