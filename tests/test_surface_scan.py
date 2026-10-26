"""
tests/test_surface_scan.py — Testes unitários para varredura de superfície e latência de blocos.
"""
from pathlib import Path

from core.hardware.surface_scan import scan_surface_latency


def test_surface_scan_measures_blocks(tmp_path: Path):
    disk_file = tmp_path / "mock_surface.img"
    total_bytes = 4 * 1024 * 1024 # 4MB = 8192 setores de 512
    disk_file.write_bytes(b"A" * total_bytes)

    results = []
    summary = scan_surface_latency(
        str(disk_file),
        start_lba=0,
        max_sectors=8192,
        block_sectors=2048, # 1MB por bloco = 4 blocos
        progress_cb=lambda r, s: results.append(r)
    )

    assert summary.total_tested_blocks == 4
    assert len(results) == 4
    assert summary.bad_count == 0
    assert (summary.fast_count + summary.good_count + summary.slow_count) == 4


def test_surface_scan_validates_positive_parameters(tmp_path: Path):
    """P1 Teste: Garante que parâmetros inválidos ou que gerariam loops infinitos sejam recusados."""
    import pytest
    disk_file = tmp_path / "mock_surface.img"
    disk_file.write_bytes(b"\x00" * 4096)

    # block_sectors <= 0
    with pytest.raises(ValueError, match="block_sectors deve ser estritamente positivo"):
        scan_surface_latency(str(disk_file), block_sectors=0)

    # max_sectors <= 0
    with pytest.raises(ValueError, match="max_sectors deve ser estritamente positivo"):
        scan_surface_latency(str(disk_file), max_sectors=0)

    # start_lba < 0
    with pytest.raises(ValueError, match="start_lba deve ser maior ou igual a zero"):
        scan_surface_latency(str(disk_file), start_lba=-1)


def test_surface_scan_distinguishes_critical_delay_from_bad(tmp_path: Path, monkeypatch):
    """P2 Teste: Distingue leitura lenta (>1000ms com sucesso) de erro de leitura irrecuperável (bad)."""
    import time
    disk_file = tmp_path / "mock_delay.img"
    disk_file.write_bytes(b"\x55" * (2048 * 512)) # 1 bloco de 1MB

    # Simula latência alta (1.2s) na leitura sem erro de I/O
    call_count = 0
    orig_perf_counter = time.perf_counter

    def mock_perf_counter():
        nonlocal call_count
        call_count += 1
        # t0 retorna 0.0, t1 retorna 1.2s (1200ms)
        return 1.2 if call_count % 2 == 0 else 0.0

    monkeypatch.setattr(time, "perf_counter", mock_perf_counter)

    results = []
    summary = scan_surface_latency(
        str(disk_file),
        start_lba=0,
        max_sectors=2048,
        block_sectors=2048,
        progress_cb=lambda r, s: results.append(r)
    )

    assert summary.total_tested_blocks == 1
    assert summary.critical_delay_count == 1
    assert summary.bad_count == 0
    assert results[0].status == "critical_delay"


def test_surface_scan_stride_spans_full_disk(tmp_path: Path):
    """Garante que stride_sectors varre de 0% a 100% da extensão do disco distribuindo zonas."""
    disk_file = tmp_path / "mock_stride.img"
    # 8192 setores de 512 bytes (4 MB)
    disk_file.write_bytes(b"\xAA" * (8192 * 512))

    lbas = []
    summary = scan_surface_latency(
        str(disk_file),
        start_lba=0,
        max_sectors=8192,
        block_sectors=256,
        stride_sectors=2048,
        progress_cb=lambda r, s: lbas.append(r.lba),
    )

    assert lbas == [0, 2048, 4096, 6144]
    assert summary.total_tested_blocks == 4
    assert summary.scanned_span_sectors == 8192
    assert summary.current_span_sector == 8192
    assert summary.bytes_read == 4 * 256 * 512



