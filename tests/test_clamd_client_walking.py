"""
Test della traversata ricorsiva e del pre-check dimensionale di
clamd_client: logica pura su filesystem temporaneo, senza clamd reale
(né file da 25MB: la soglia è un parametro, si testano con file di
100 byte e soglie di 50).

Attenzione: la soglia è applicata dal pre-check PRIMA di toccare clamd,
quindi scan_stream con file tutti sotto la soglia di esclusione non
apre mai connessioni — i test possono usare un socket inesistente.
"""

from pathlib import Path

from klamav_py.clamd_client import ClamdClient, ScanResult


def make_tree(tmp_path: Path) -> Path:
    """
    root/
      a.txt
      sub/b.txt
      cache/c.txt
      cache/nested/d.txt
      qdir/e.txt
    """
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "cache" / "nested").mkdir(parents=True)
    (root / "qdir").mkdir()
    (root / "a.txt").write_bytes(b"a" * 100)
    (root / "sub" / "b.txt").write_bytes(b"b" * 100)
    (root / "cache" / "c.txt").write_bytes(b"c" * 100)
    (root / "cache" / "nested" / "d.txt").write_bytes(b"d" * 100)
    (root / "qdir" / "e.txt").write_bytes(b"e" * 100)
    return root


def collected(generator) -> set:
    return {str(Path(p).name) for p in generator}


def test_exclude_prunes_whole_directory(tmp_path):
    root = make_tree(tmp_path)
    files = collected(ClamdClient._iter_files(root, exclude_dirs=[root / "cache"]))
    # cache/ non viene né attraversata né letta: c.txt e d.txt assenti.
    assert files == {"a.txt", "b.txt", "e.txt"}


def test_exclude_nested_subdirectory(tmp_path):
    root = make_tree(tmp_path)
    files = collected(
        ClamdClient._iter_files(root, exclude_dirs=[root / "cache" / "nested"])
    )
    # L'esclusione annidata toglie solo d.txt: c.txt resta coperto.
    assert files == {"a.txt", "b.txt", "c.txt", "e.txt"}


def test_target_inside_excluded_dir_yields_nothing(tmp_path):
    root = make_tree(tmp_path)
    files = collected(
        ClamdClient._iter_files(root / "qdir", exclude_dirs=[root / "qdir"])
    )
    # Se per errore si chiede la scansione della directory stessa
    # esclusa (es. la directory di quarantena), non produrre nulla:
    # meglio zero risultati che ri-rilevare i file già gestiti.
    assert files == set()


def test_explicit_single_file_target_is_scanned_anyway(tmp_path):
    root = make_tree(tmp_path)
    # Un target SINGOLO esplicito vince sull'esclusione: il chiamante
    # ha chiesto quel file per nome. Documentato dal test perché è una
    # scelta deliberata (il check is_file() precede il check esclusione).
    files = collected(
        ClamdClient._iter_files(root / "a.txt", exclude_dirs=[root])
    )
    assert files == {"a.txt"}


def test_symlinked_directories_are_not_followed(tmp_path):
    root = make_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s" * 100)
    (root / "link").symlink_to(outside, target_is_directory=True)
    files = collected(ClamdClient._iter_files(root, exclude_dirs=None))
    # followlinks=False: le esclusioni non sono aggirabili attraverso
    # un symlink a directory dentro l'albero scansionato.
    assert files == {"a.txt", "b.txt", "c.txt", "d.txt", "e.txt"}


# --- Pre-check dimensionale -------------------------------------------------


def test_size_limit_result_thresholds(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 30)
    exact = tmp_path / "exact.bin"
    exact.write_bytes(b"x" * 50)

    # 100 > 50: il file NON viene inviato, esce come TOO_LARGE...
    result = ClamdClient._size_limit_result(big, 50)
    assert result is not None and result.too_large
    assert "50" in result.signature  # la soglia è riportata nel motivo

    # ...esattamente alla soglia invece viene inviato (<=, non <)...
    assert ClamdClient._size_limit_result(exact, 50) is None
    # ...sotto ovviamente sì...
    assert ClamdClient._size_limit_result(small, 50) is None
    # ...e con il pre-check disattivato non si valuta nulla.
    assert ClamdClient._size_limit_result(big, None) is None

    # Un file sparito tra l'attraversamento e il pre-check non deve
    # far fallire nulla: il flusso normale produrrà l'errore opportuno.
    assert ClamdClient._size_limit_result(tmp_path / "ghost.bin", 50) is None


def test_scan_stream_precheck_skips_large_file_without_clamd(tmp_path):
    # Socket inesistente DI PROPOSITO: se il pre-check funziona, non
    # viene mai aperta una connessione e il test non dipende da clamd.
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    client = ClamdClient(unix_socket="/non/esiste/clamd.ctl")
    results = list(client.scan_stream(big, max_stream_size=50))
    assert len(results) == 1
    assert results[0].too_large
    assert results[0].path == str(big)


def test_scan_stream_without_precheck_does_not_fabricate_too_large(tmp_path):
    # Senza pre-check, un file sopra (presunta) soglia viene inviato:
    # con clamd assente il risultato è un ERROR di sessione, NON un
    # TOO_LARGE inventato dal client (TOO_LARGE è solo di clamd vero
    # o del pre-check, mai una supposizione su file inviati).
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    client = ClamdClient(unix_socket="/non/esiste/clamd.ctl")
    results = list(client.scan_stream(big, max_stream_size=None))
    assert len(results) == 1
    assert results[0].status == "ERROR"
    assert not results[0].too_large


def test_stream_failure_above_threshold_is_too_large(tmp_path):
    # Il fallback: un fallimento di stream su un file sopra soglia è
    # quasi certamente clamd che rifiuta per StreamMaxLength a metà
    # invio (la "pipe interrotta" osservata sui file grandi) — va
    # classificato "non verificato", non come errore.
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    result = ClamdClient._stream_failure_result(big, BrokenPipeError("pipe"), 50)
    assert result.too_large
    assert "pipe" in result.signature.lower()


def test_stream_failure_below_threshold_is_error(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 30)
    result = ClamdClient._stream_failure_result(small, BrokenPipeError("pipe"), 50)
    assert result.status == "ERROR"
    assert not result.too_large
