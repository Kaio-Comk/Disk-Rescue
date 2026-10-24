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
