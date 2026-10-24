"""
tests/test_resilient_clone.py — Testes unitários para o motor de clonagem resiliente com pulo de bad sectors.
"""
from pathlib import Path

from core.recovery.resilient_clone import ResilientCloner, SECTOR_SIZE


def test_resilient_clone_clean_disk(tmp_path: Path):
    total_sectors = 500
    one_sector = (b"SECTOR_DATA_" * 50)[:SECTOR_SIZE]
    src_data = one_sector * total_sectors

    def mock_reader(lba: int, count: int) -> bytes:
        start = lba * SECTOR_SIZE
        end = start + count * SECTOR_SIZE
        return src_data[start:end]

    dst_img = tmp_path / "cloned.img"
    cloner = ResilientCloner(
        device_path="mock://drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=64,
        skip_step_sectors=128
    )

    stats_list = []
    success = cloner.run(progress_cb=stats_list.append, reader_fn=mock_reader)

    assert success is True
    assert dst_img.exists()
    assert dst_img.stat().st_size == total_sectors * SECTOR_SIZE
    assert dst_img.read_bytes() == src_data
    assert cloner.clone_map.bad_sectors == 0
    assert cloner.map_path.exists()


def test_resilient_clone_with_bad_sectors_skip_and_scrape(tmp_path: Path):
    total_sectors = 1000
    bad_lba_range = set(range(120, 128)) # 8 setores ruins simulados

    def mock_faulty_reader(lba: int, count: int) -> bytes:
        requested = set(range(lba, lba + count))
        # Se qualquer setor requisitado for bad, lança erro de I/O de disco
        if requested & bad_lba_range:
            raise OSError("Hardware I/O Error: Sector read timeout")
        return b"GOOD" * (count * (SECTOR_SIZE // 4))

    dst_img = tmp_path / "recovered_faulty.img"
    cloner = ResilientCloner(
        device_path="mock://faulty_drive",
        output_image_path=dst_img,
        total_sectors=total_sectors,
        block_sectors=32,
        skip_step_sectors=64
    )

    success = cloner.run(reader_fn=mock_faulty_reader)

    assert success is True
    assert dst_img.exists()
    assert dst_img.stat().st_size == total_sectors * SECTOR_SIZE
    # A maior parte dos setores deve ter sido resgatada com sucesso
    assert cloner.clone_map.rescued_sectors > 800
    assert cloner.clone_map.bad_sectors > 0
    assert cloner.map_path.exists()
