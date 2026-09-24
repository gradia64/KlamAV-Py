import locale
import os
import stat
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from klamav_py import freshclam_service as fs
from klamav_py.db_freshness import (DbInfo, parse_version_reply,
                                    should_update_on_startup)
from klamav_py.freshclam_service import Outcome

REPLY = "ClamAV 1.4.2/27072/Tue Sep 22 09:25:00 2026\n"


# -- parser -------------------------------------------------------------

def test_parse_ok():
    info = parse_version_reply(REPLY)
    assert info == DbInfo("1.4.2", 27072, datetime(2026, 9, 22, 9, 25, 0))


def test_parse_single_digit_day_double_space():
    info = parse_version_reply("ClamAV 1.4.2/27052/Wed Sep  2 03:10:00 2026")
    assert info is not None and info.built.day == 2


def test_parse_nul_terminated():
    assert parse_version_reply(REPLY.strip() + "\0") is not None


@pytest.mark.parametrize("reply", [
    "", "ClamAV 1.4.2", "ClamAV 1.4.2/abc/Tue Sep 22 09:25:00 2026",
    "ClamAV 1.4.2/27072/Tue Foo 22 09:25:00 2026",
    "ClamAV 1.4.2/27072/Tue Sep 31 09:25:00 2026",
    "ClamAV 1.4.2/27072/Tue Sep 22 09:25 2026",
    "Other 1.0/1/Tue Sep 22 09:25:00 2026",
    "ERROR: something",
])
def test_parse_rejects_malformed(reply):
    assert parse_version_reply(reply) is None


def test_parse_independent_of_locale():
    for name in ("it_IT.UTF-8", "it_IT.utf8"):
        try:
            old = locale.setlocale(locale.LC_TIME)
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("locale italiano non disponibile")
    try:
        assert parse_version_reply(REPLY) is not None
    finally:
        locale.setlocale(locale.LC_TIME, old)


# -- gate ---------------------------------------------------------------

BUILT = datetime(2026, 9, 22, 9, 0, 0)
INFO = DbInfo("1.4.2", 27072, BUILT)


def test_gate_fresh_db_no_update():
    assert not should_update_on_startup(INFO, True, now=BUILT + timedelta(hours=10))


def test_gate_stale_db_updates():
    assert should_update_on_startup(INFO, True, now=BUILT + timedelta(hours=40))


def test_gate_disabled_or_unknown():
    later = BUILT + timedelta(days=5)
    assert not should_update_on_startup(INFO, False, now=later)
    assert not should_update_on_startup(None, True, now=later)


def test_age_never_negative():
    assert INFO.age(now=BUILT - timedelta(hours=3)) == timedelta(0)


# -- binari fidati --------------------------------------------------------

def test_trusted_binary_rejects_user_owned(tmp_path):
    exe = tmp_path / "systemctl"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    if os.getuid() == 0:
        pytest.skip("il test richiede un utente non root")
    assert fs.trusted_binary("systemctl", dirs=[str(tmp_path)]) is None


def test_trusted_binary_rejects_group_writable(tmp_path, monkeypatch):
    real_stat = os.stat
    target = str(tmp_path / "pkexec")

    def fake_stat(path, *a, **kw):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o775, st_uid=0)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(fs.os, "stat", fake_stat)
    assert fs.trusted_binary("pkexec", dirs=[str(tmp_path)]) is None


def test_trusted_binary_accepts_root_owned(tmp_path, monkeypatch):
    target = str(tmp_path / "pkexec")
    monkeypatch.setattr(fs.os, "stat", lambda p, *a, **kw: SimpleNamespace(
        st_mode=stat.S_IFREG | 0o755, st_uid=0) if p == target else (_ for _ in ()).throw(OSError()))
    assert fs.trusted_binary("pkexec", dirs=[str(tmp_path)]) == target


# -- risoluzione unità ----------------------------------------------------

def _fake_run(states):
    def run(argv, **kw):
        unit = argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout=states.get(unit, "not-found") + "\n")
    return run


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(fs, "trusted_binary", lambda name, dirs=None: f"/usr/bin/{name}")


def test_resolve_prefers_debian_name(trusted):
    run = _fake_run({"clamav-freshclam.service": "loaded", "freshclam.service": "loaded"})
    assert fs.resolve_unit(run) == "clamav-freshclam.service"


def test_resolve_fallback(trusted):
    assert fs.resolve_unit(_fake_run({"freshclam.service": "loaded"})) == "freshclam.service"


def test_resolve_skips_masked(trusted):
    assert fs.resolve_unit(_fake_run({"clamav-freshclam.service": "masked"})) is None


def test_restart_argv_is_fixed(trusted):
    assert fs.build_restart_argv("clamav-freshclam.service") == [
        "/usr/bin/pkexec", "/usr/bin/systemctl", "restart", "clamav-freshclam.service"]


def test_restart_argv_rejects_arbitrary_unit(trusted):
    with pytest.raises(ValueError):
        fs.build_restart_argv("sshd.service; rm -rf /")


# -- worker ---------------------------------------------------------------

pyside = pytest.importorskip("PySide6")
from klamav_py.gui.freshclam_restart_worker import FreshclamRestartWorker  # noqa: E402


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stderr = SimpleNamespace(read=lambda: "")

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setattr(fs, "resolve_unit", lambda run=None: "clamav-freshclam.service")
    monkeypatch.setattr(fs, "build_restart_argv", lambda unit: ["pkexec", "systemctl", "restart", unit])
    monkeypatch.setattr(fs, "is_active", lambda unit, run=None: True)
    monkeypatch.setattr(fs, "is_failed", lambda unit, run=None: False)
    monkeypatch.setattr(fs, "journal_lines", lambda unit, since, run=None: [])

    def set_rc(rc):
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _Proc(rc))
    return set_rc


def _worker(probes, timeout=0.5):
    it = iter(probes)
    last = [None]

    def probe():
        last[0] = next(it, last[0])
        return last[0]
    return FreshclamRestartWorker(probe, wait_timeout=timeout, poll_interval=0.05)


def test_worker_updated(worker_env):
    worker_env(0)
    new = DbInfo("1.4.2", 27073, BUILT + timedelta(days=1))
    result = _worker([INFO, INFO, new])._run()
    assert result.outcome is Outcome.UPDATED and result.db_info == new


def test_worker_unchanged_after_timeout(worker_env):
    worker_env(0)
    assert _worker([INFO])._run().outcome is Outcome.UNCHANGED


@pytest.mark.parametrize("rc,outcome", [
    (126, Outcome.CANCELLED), (127, Outcome.DENIED), (1, Outcome.FAILED)])
def test_worker_pkexec_codes(worker_env, rc, outcome):
    worker_env(rc)
    assert _worker([INFO])._run().outcome is outcome


def test_worker_unit_failed(worker_env, monkeypatch):
    worker_env(0)
    monkeypatch.setattr(fs, "is_failed", lambda unit, run=None: True)
    assert _worker([INFO])._run().outcome is Outcome.FAILED


def test_worker_clamd_down_before_is_not_updated(worker_env):
    worker_env(0)
    # Senza versione "prima" non si può dichiarare un aggiornamento.
    assert _worker([None, INFO])._run().outcome is Outcome.UNCHANGED
