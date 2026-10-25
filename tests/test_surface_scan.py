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

